# 图谱通道降级为标注

日期：2026-07-27
状态：**已实施**

`hybrid` 曾是 vector / FTS / graph 三路 RRF 融合（见 `docs/hybrid-retrieval-mvp.md`）。本次把图谱从**打分通道**降级为**结果标注**：融合只剩两路，取 `top_k` 之后再用图谱给选中的 chunk 挂上实体与关系。

起因是一次实测：`hybrid "基因税是什么"` 打印 `graph=0`，而图谱里明明有「基因税」这个实体。

## 不重新讨论的事

- **不引入分词依赖**（jieba 或其他）。理由见下「方案一」。
- **不做实体桥接**（用命中 chunk 反查实体、再用实体的其他 evidence chunk 扩召回）。理由见下「方案二」。
- **不改 `graph-query`**。它是关键词工具，按子串匹配是它的正确语义，输出逐字不变。
- **不改 RRF 参数**。`RRF_K=60`、`FAN_OUT_MULTIPLIER=10`、等权，按 `hybrid-retrieval-mvp.md` 刻意不进 config。

## 根因

`graph._matches_entity` / `_matches_relation` 的判定是 `needle in _case_key(value)`——**要求查询串是实体名/别名/证据文本的子串**。而 `app.hybrid_search` 把用户的原始问句整个喂了进去。

```
graph-query "基因税"       → 命中，并带出 基因税 --导致--> 人类基因多样性
graph-query "基因税是什么"  → no results
```

所以三路融合在真实提问上长期只有两路在跑。这不是 `graph-query` 的 bug，是 hybrid 复用它时没做查询改写。

## 两个修法都被实测否决

### 方案一 · 切词后逐词查图谱取并集

在 288/2307 chunk 的部分图谱（1580 实体 / 1769 关系）上量化每个词元会拉进多少条目：

| 词元 | 命中实体 | 命中关系 |
|---|---|---|
| 基因税 | 4 | 5 |
| 基因 | 14 | 8 |
| 税 | 11 | 12 |
| 什么 | 47 | 60 |
| 是 | 425 | 532 |
| 的 | 832 | 1160 |

「基因税是什么」切开取并集，信号占比不到 1%。`_matches_entity` 还会匹配 `evidence.text`（整句原文），所以高频词的命中面比实体名本身大得多。

**这个方案的真实内容不是「引入 jieba」，是「引入 jieba + 自己维护一份中文停用词表」。** 后者没有标准答案，且随语料漂移——本书里 `一个人`、`世界`、`一致性` 已经被抽成实体，`人` 命中 364 个。

依赖成本也是实打实的：`jieba` 不在 `pyproject.toml`（`import jieba` 报 `ModuleNotFoundError`）；zvec 的 jieba FTS 在 Rust 侧，Python 面只暴露 `get_default_jieba_dict_dir` / `set_default_jieba_dict_dir`，**没有分词 API**，复用不了。

还有一处倒挂：`app._rank_graph_evidence` 按 `(-relations, -entities, chunk_index)` 排序，**命中条目越多的 chunk 排越前**。切词并集会让「蹭到多个中频词的 chunk」压过「精确命中基因税的 chunk」——噪声不只是稀释信号，是主动顶掉它。

### 方案二 · 实体桥接

用 vector/FTS 已命中的 chunk 反查实体，再用实体的**其他** evidence chunk 扩召回。零新依赖，看起来更贴「检索只是导航」。

实测（288 chunk，按 `mentions ∪ evidence` 统计每个实体跨越的 chunk 数）：

```
跨 1 个 chunk    1509 实体  (79.5%)
跨 >1 个 chunk    388 实体  (20.5%)
```

跨得最多的：

| 实体 | 覆盖已处理 chunk | 全量外推 |
|---|---|---|
| 尤基 | 53.8% | ~1241 chunk |
| 约格 | 49.3% | ~1137 chunk |
| 尤利娅 | 27.4% | ~632 chunk |
| Z组织 | 14.6% | ~336 chunk |

**桥接的产出量与实体的区分度互为倒数。** 跨 chunk 多的（尤基、约格）是高频、无区分度，桥接产出半本书而 `fan_out` 只有 50；有区分度的（基因税 4 块）落在 79.5% 只跨 1 个 chunk 的那堆里，桥接产出为零。这就是 IDF，换了个说法。

两个方案缺的是同一样东西：**一个按语料内稀有度加权的选择函数**。而那个东西 FTS 通道里已经免费有了（BM25 自带 IDF）——图谱通道是唯一一个在做无权重子串匹配的。

## 决定

图谱不做召回，只做标注。判据是一个没人能回答的问题：

> 举出一个具体查询，是「只标注」答不了、而切词或桥接答得了的。

举不出来。而代价是明确的：RRF 等权无加权，图谱 rank 1 得 `1/(60+1)=0.0164`，直接压过向量 rank 5 的 `1/65=0.0154`——图谱每多命中一条，就挤掉一条真向量结果。在实体里还躺着 `一个人`、`世界`、`不，在我看来，这多半是贝尔尼尼阁下的错误吧？` 的现在，这个交换不划算。

关键点：**标注不查询**。它回答「这个 chunk 上挂着什么」，与查询串无关，所以对任何提问都有效——这正是被降级的召回通道做不到的。

## 改动

| 位置 | 改动 |
|---|---|
| `graph.annotate_chunks` | 新增。给定 chunk id 集合，返回挂在这些 chunk 上的实体与关系，走同一套 `_filter_entity` / `_allowed` 进度过滤 |
| `app._annotate_with_graph` | 新增。融合取 `top_k` 后标注；图谱缺失/stale 只丢标注并记原因 |
| `app._load_graph` | 新增。`query_graph` 与标注路径共用的加载+staleness 校验，消除重复 |
| `app._context_by_chunk` | 从 `_graph_evidence` 抽出。`query=None` 时保留全部锚点（标注），传 query 时按原语义收窄（`graph-query`） |
| `app.hybrid_search` | `channels` 只剩 vector/FTS；新增 `graph_annotation` |
| `app._rank_graph_evidence` | 删除（本次改动使其成为孤儿） |
| `app.ChannelMode` / `models.EvidenceMatch.mode` | 收窄为 `vector` \| `fts`——graph 再也不可能产生一个名次 |
| `app.ChannelStatus.skipped_reason` | 删除。跳过原因移到 `GraphAnnotationStatus` |
| `cli.print_graph_annotation` | 新增输出行 |

`Evidence.retrieval_mode` **保留** `"graph"`——`graph-query` 仍然产出它。

## 验收

`uv run pytest` 42 passed（原 40，净 +2）、`uvx ruff format` / `ruff check` clean。

测试改动：`test_hybrid_skips_the_graph_channel_and_reports_why` → `..._annotation_...`（缺失/stale 两参数化例保留，断言改为标注被跳过而结果照常返回）；新增 `test_hybrid_annotates_results_a_graph_query_could_never_have_matched`（同一个自然语言问句，`query_graph` 返回空而 hybrid 标注成功——把「标注不查询」钉死）与 `test_hybrid_annotation_obeys_the_reading_progress`。

全量 `sample` 实测（`graph-index` 运行中，约 300/2307 chunk）：

```
hybrid "尤基的父亲是怎么死的"  → graph context: 3/3 results annotated
hybrid "基因税是什么"          → graph context: 0/3   （命中 chunk 尚未被抽取到）
hybrid "尤基和他的家人" --max-chapter 3
                              → 2/2，标注内容全部来自第一章，无越界
```

## 顺带发现，未处理

- **`graph.json` 不是原子写**（`derivation.write_json_document` 直接 `write_text`）。`graph-index` 每处理完一个 chunk 就整文件重写，全量后约 17 MB；此时并发跑 `hybrid` / `graph-query` 有概率读到截断的 JSON 而抛 `JSONDecodeError`（`_annotate_with_graph` 只捕获 `FileNotFoundError` / `RuntimeError`）。这是既有行为，本次未改。同类问题见 `architecture-archive.md` 的「索引不是原子发布」。
- **实体抽取噪声**：整句话（`带走十八个婴儿，作为这次的基因税`）、对白（`不，在我看来，这多半是贝尔尼尼阁下的错误吧？`）、泛化词（`一个人`、`世界`、`一致性`）都被抽成了实体。这是 prompt 层面的问题，改 prompt 要 bump `GRAPH_PROMPT_VERSION` 并整图重建，等全量跑完有完整样本再一次性评估。
