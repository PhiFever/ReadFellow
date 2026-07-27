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
- **实体抽取噪声**：整句话（`带走十八个婴儿，作为这次的基因税`）、对白（`不，在我看来，这多半是贝尔尼尼阁下的错误吧？`）、泛化词（`一个人`、`世界`、`一致性`）都被抽成了实体。当天做了对照实验，见下节——**不是 prompt 措辞问题**。

## 后续 · prompt 加严实测无效

在 `graph-index --collection sample` 跑到 496/2307 时暂停，做了一轮对照。

**基线是白送的**：`build_graph` 只读 `manifest.json` + `chunks.jsonl`，不碰 zvec。所以把 sample 的前 60 个 chunk 的 metadata 复制成 `evalp` 集合就能跑，零 embedding 成本；而这 60 个 chunk 的老 prompt 结果已经躺在 sample 的 `graph.json` 里。只需跑新 prompt 一次。

改动是在 `build_extraction_prompt` 里加一条：「实体的 name 必须是片段中出现的专有名词或固定称谓，不超过 12 个汉字，且不能是整句话、对白、动作描述或代词。不要抽取泛指词……」，并 bump 到 `graph-extraction-v2`。

| 指标（同 60 chunk，qwen3:8b） | v1 | v2 | |
|---|---|---|---|
| 实体 | 445 | 373 | −16% |
| 关系 | 435 | 325 | −25% |
| 垃圾名（>12 字或含句读） | 38（8.5%） | 37（9.9%） | 没降 |
| 名字 >20 字 | 8 | 13 | 更差 |
| 最长实体名 | 44 字 | 67 字 | 更差 |
| rejected/chunk | 1.32 | 1.12 | 略好，在噪声内 |

**花掉 16% 实体和 25% 关系，目标指标纹丝不动。**「不超过 12 个汉字」被直接无视——最长名字反而翻倍。丢掉的里面有 `人类基因多样性`（即本文档开头那条 `基因税 --导致--> 人类基因多样性`）、`PSPACE问题`、`加密WiFi信号`、`强规范化语言`。v2 也确实清掉了一些表层指标看不见的垃圾（`尤基的爸爸问题`、`四个孩子病死`）并新增 107 个干净实体，所以是大量 churn 而非单向损失，但净额是 −71 个干净实体。

已回滚，`GRAPH_PROMPT_VERSION` 保持 `graph-extraction-v1`，sample 的断点续建不受影响。

### 为什么无效：噪声不在实体抽取，在关系端点

`_merge_extraction`（`graph.py:299-315`）给每条关系的 subject / object **自动建实体桩**，只带一个 mention，永远没有 `types`、没有引文。`_parse_relation` 只校验关系类型白名单和证据锚定，**不校验端点的形态**。

按「有 `types` 或有 `evidence`」把 496 chunk 的图谱分成两级：

| | 数量 | 垃圾名 |
|---|---|---|
| Tier 1 已声明实体 | 1599 | 25（**1.6%**） |
| Tier 2 关系端点桩 | 1640 | 370（**22.6%**） |
| 全量 | 3239 | 395（12.2%） |

93.7% 的垃圾名是 Tier 2。**改实体 prompt 改错了地方**——那些名字根本不是从 `entities[]` 来的。

### 表层过滤不可用

「>12 字或含句读」这个 heuristic 两头都不干净：flagged 里混着 `《聊斋志异》`、`联合国‘罗摩特别项目`（真实体被标点误伤），12 字以下放过的又有 `我之前和那边的同事讨论过`、`对美以美大厦形成包围之势` 这种从句。Tier 2 里被它判为「干净」的还有 `阅读记忆`、`预判`、`工作`、`天空`、`纸`、`行动` —— 正是泛化词那一类。

结构特征单用也不行：直接丢弃 Tier 2 的召回率 93.7%，但精确率只有 22.6%，会砍掉全部实体的 50.6%，其中包括 `巴贝奇`、`精锐部队`、`侠客的记忆存储设备` 这些真实体。

### 结论

- **不要再调 prompt 措辞。** 已实测：加严只会让 qwen3:8b 整体变保守，噪声率不动。
- 便宜的方向是**按 provenance 分级显示**（读侧，零重建）：标注只列 Tier 1，垃圾率 12.2% → 1.6%。关系行独立显示端点名，不受影响。
- 从源头治要改 `_parse_relation` 校验端点形态（解析侧，不是 prompt），但会改变图谱内容，需 bump `GRAPH_SCHEMA_VERSION` 整图重建。

评测集合 `evalp`（60 chunk 纯 metadata）留在 `metadata/` 下可复用；任何后续尝试都应按同样口径量。
