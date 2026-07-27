# ReadFellow

ReadFellow 是一个本地优先的 CLI 工作流：把长文档（主要是中文小说）切块、用 Ollama 生成 embedding、存进 zvec 本地向量库，让 agent 能按语义 / 全文 / 图谱检索出**原文段落**，并带着精确出处（`source_path:line_start-line_end`）与用户讨论。

三条贯穿全流程的不变量：

1. **源文档是唯一事实依据**。index、metadata、graph 都是派生物，派生物 stale 时 fail closed，不猜测。
2. **检索只是导航，原始 chunk 才是证据**。任何输出都回落到存储的 chunk 原文 + 精确出处。
3. **防剧透优先**。一旦设定阅读进度，超出进度的文本绝不出现在输出里。

---

## 1. 前置条件

**Python 依赖**走 `uv`，无需手动装包（`pyproject.toml` 已配 `pythonpath = ["src"]`）：

```sh
uv sync
```

**Ollama** 必须运行在 `config.yaml` 配置的端点上，并已拉取两个模型：

```sh
ollama serve                        # 另开一个终端
ollama pull qwen3-embedding:8b      # embedding，4096 维
ollama pull qwen3:8b                # 生成，供 graph-index / analyze 使用
```

自检（返回模型列表即正常；连不上会是 `503`）：

```sh
curl -s http://127.0.0.1:11434/api/tags
```

只做 `index` / `search` / `fts` / `hybrid` / `fetch` 时只需要 embedding 模型；`graph-index` 和 `analyze` 才需要生成模型。**跑测试不需要 Ollama**（全离线）。

---

## 2. 五分钟上手

```sh
# 1) 冒烟索引前 12 个 chunk，确认 Ollama 通路正常
uv run readfellow index corpus/samples/赛博英雄传.txt --collection smoke --rebuild --limit 12

# 2) 语义检索
uv run readfellow search "尤基的父亲" --collection smoke --top-k 2

# 3) 中文全文检索
uv run readfellow fts "回收站镇" --collection smoke --top-k 2

# 4) 融合检索（向量+全文打分，图谱标注）
uv run readfellow hybrid "基因税" --collection smoke --top-k 3

# 5) 按 id 取回单个 chunk 原文
uv run readfellow fetch bdd935754e17_000001 --collection smoke
```

全量索引把 `--limit 12` 去掉即可。示例小说 408 万字 / 2307 chunks，实测约 **12 分钟**（3.3 chunks/s）。

---

## 3. 命令手册

所有默认值都取自根目录 `config.yaml`；命令行参数只覆盖单次运行。

### 3.1 全局参数

置于子命令**之前**，对所有子命令生效：

| 参数 | 默认值来源 | 说明 |
|---|---|---|
| `--config` | `config.yaml` | 换一份配置文件 |
| `--index-dir` | `paths.index_dir` | zvec 数据目录 |
| `--metadata-dir` | `paths.metadata_dir` | manifest / chunks / graph / analysis 目录 |
| `--ollama-url` | `ollama.base_url` | Ollama 端点 |
| `--model` | `ollama.embedding_model` | **embedding** 模型（生成模型是各子命令的 `--llm-model`） |
| `--keep-alive` | `ollama.keep_alive` | 模型驻留时间 |

```sh
uv run readfellow --ollama-url http://192.168.1.10:11434 search "问题" --collection sample
```

### 3.2 `index` — 切块并索引

```sh
uv run readfellow index <文档.txt> --collection sample --rebuild
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--collection` | `sample` | 集合名，只允许字母数字下划线 |
| `--chunk-chars` | 2400 | 目标 chunk 字符数 |
| `--overlap-chars` | 240 | 相邻 chunk 的尾部重叠 |
| `--batch-size` | 8 | 每批 embedding 数量 |
| `--limit` | 0（不限） | 只索引前 N 个 chunk |
| `--rebuild` | off | 重建集合，换模型 / 换切块参数时必须加 |
| `--no-optimize` | off | 跳过 `zvec.optimize()`，**仅用于测写入速度** |

流程：切块 → 用首个 chunk 探测 embedding 维度 → 先写 `manifest.json` + `chunks.jsonl` → 分批比对 `text_hash` 决定 insert/update/skip（只对真要写的 chunk 调 embedding）→ `optimize()`。

> **`--no-optimize` 不要用于正式索引**：不 optimize 时持久化的中文 FTS 在集合重开后可能查不到。

### 3.3 `search` / `fts` / `hybrid` — 三种检索

```sh
uv run readfellow search "要查询的问题" --collection sample --top-k 5   # 向量语义
uv run readfellow fts    "关键词"       --collection sample --top-k 5   # zvec jieba 中文全文
uv run readfellow hybrid "问题或关键词" --collection sample --top-k 5   # 向量+全文融合，图谱做标注
```

`hybrid` 用向量和全文两路打分，融合后再用图谱标注选中的结果：

```
channels: vector=12, fts=7
graph context: 3/5 results annotated
```

命中项带 `matched: vector#1, fts#3`，标明它在两条打分通道里的排名；带图谱标注的还会多出 `graph entities:` 与 `graph relation:` 行。

**图谱不参与打分,只做标注**。它按子串匹配查询串（见 3.5），自然语言问句几乎不可能匹配到实体名，所以曾经的图谱召回通道对真实提问长期是空的；而它能产出结果的高频实体（主角出现在过半 chunk 里）又恰恰没有区分度。标注不查询、只回答「这个 chunk 上挂着什么」，因此对任何提问都有效。图谱缺失或 stale 时只丢标注，两路打分照常返回：

```
graph context: skipped (graph index not found: ...; run graph-index first)
```

### 3.4 `fetch` — 按 id 取回原文

```sh
uv run readfellow fetch <chunk-id> --collection sample
```

打印完整原文（不截断）。三种结果：`found`、`not_found`（无此 chunk）、`outside_progress`（超出阅读进度，**不带原文**）。

### 3.5 `graph-index` / `graph-query` — 知识图谱

> ⚠️ 全量运行当前存在已知阻塞，见 [§6 已知问题](#6-已知问题)。

```sh
uv run readfellow graph-index --collection sample --limit 20
uv run readfellow graph-query "向山" --collection sample
```

`graph-index` 读 `chunks.jsonl`，对每个 chunk 调一次生成模型抽取实体/关系，写入 `metadata/<collection>/graph.json`。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--limit` | 0 | 只抽取前 N 个符合条件的 chunk |
| `--llm-model` | `ollama.generation_model` | 生成模型 |
| `--num-predict` | `graph.num_predict` (4096) | 每 chunk 最大生成 token |
| `--retries` | `graph.retries` (2) | 每 chunk 失败重试次数 |
| `--rebuild` | off | 整图重建 |

**每个 chunk 抽完立即落盘**，中断安全；重跑靠 `processed_chunk_ids` 断点续建，不会重做已完成的部分。

`graph-query` 按实体名、别名或关系关键词查询；图谱只给出 chunk id 与上下文，**原文仍从 `chunks.jsonl` 取**。

### 3.6 `analyze` — 章节级分析

> ⚠️ 全量运行当前存在已知阻塞，见 [§6 已知问题](#6-已知问题)。

```sh
uv run readfellow analyze --collection sample --max-chapter 50
```

按检测到的章节分组，对每个**完整章节**调一次生成模型，产出梗概、人物、事件，写入 `metadata/<collection>/analysis.json`。参数与 `graph-index` 同（除 `--limit`，`analyze` 用 `--max-chapter` 分批）。

两条规则：

- **最后一组永不分析**。它可能被 `index --limit` 截断，无法与完整章节区分。
- **超出 `num_ctx` 预算的章节被跳过**并打印原因。预算 = `(num_ctx − num_predict − 600) × 1.5` 字符；当前配置为 17532 字符。示例小说 1207 章中仅 2 章超标（0.2%）。

**每章分析完立即落盘**，中断安全，重跑按 `(章序号, 章标题)` 续建。

---

## 4. 防剧透：阅读进度限制

所有检索命令和两个派生命令都接受同一组进度参数：

| 参数 | 语义 |
|---|---|
| `--max-chapter N` | 只用完整落在第 N 章及之前的 chunk（**排除**跨入第 N+1 章的 chunk） |
| `--max-line N` | 只用结束行号 ≤ N 的 chunk |
| `--max-chunk-index N` | 只用 `chunk_index` ≤ N 的 chunk |

```sh
uv run readfellow search "他的真实身份"  --collection sample --max-chapter 50
uv run readfellow hybrid "武神"          --collection sample --max-chapter 50
uv run readfellow fetch  <chunk-id>      --collection sample --max-chapter 50
uv run readfellow graph-query "向山"     --collection sample --max-chapter 10
```

进度限制在两条路径上生效：整体传给 zvec（转成过滤表达式），以及进程内逐 chunk 判定（graph、fetch 走这条）。

**图谱在进度限制下会清空实体的 `aliases` 和 `types`** —— 它们是跨 chunk 聚合、没有逐值出处，保留会泄露进度之外的信息。这是刻意行为。

---

## 5. 产物布局

```
indexes/<collection>/                 # zvec 数据
metadata/<collection>/
  ├── manifest.json                   # 集合、源文档、模型、维度、chunk 数、切块参数
  ├── chunks.jsonl                    # 全部 chunk 原文与出处（证据的最终来源）
  ├── graph.json                      # graph-index 产物
  └── analysis.json                   # analyze 产物
```

`indexes/`、`metadata/`、`corpus/` 均不纳入版本控制。

**Evidence 模型**：向量检索、全文检索、fetch、图谱查询返回同一个 `Evidence`，包含原文、chunk id、源路径、行范围、章节、chunk 序号、字节范围、text hash、检索模式和可选分数。图谱查询把命中的实体/关系作为 `graph_context` 附加，证据本身仍是存储的原文 chunk。

---

## 6. 已知问题

### 6.1 派生管线会丢弃引文对不上的条目

抽取出的 evidence 必须是所属 chunk 原文的精确子串。锚定不上的条目会被**丢弃并计数**，其余照常入库：

```
[    2/20] extracting graph from bdd935754e17_000001
        entities=12, relations=14, rejected=1
```

`rejected=N` 就是这一个 unit 丢掉的条目数，落盘在 `graph.json` / `analysis.json` 的 `rejected_count`。实测约 7%（graph 7.8%、analysis 6.9%），主要是模型把代词换成人名这类改写——放宽匹配等于伪造出处，所以按设计丢弃。**显著高于 7% 说明模型或 prompt 出了问题，值得查。**

这意味着图谱与分析是**真实但不完整**的子集：留下的每一条都能回落到原文精确子串，但模型引错的那部分不会出现。

2026-07-27 之前，一条引文对不上会耗尽重试并终止整个 run，全量因此跑不完。根因排查与修复见 [`docs/derivation-hardening-plan.md`](docs/derivation-hardening-plan.md)，执行步骤与成本基线见 [`docs/mvp-runbook.md`](docs/mvp-runbook.md)。

### 6.2 索引不是原子发布

embedding 中途失败会留下 metadata 完整而 collection 不完整的状态。用 `--rebuild` 重跑可恢复。

### 6.3 换模型 / 换切块参数必须 `--rebuild`

- chunk id = `source_hash[:12]_%06d`。改 `--chunk-chars` / `--overlap-chars` 会让 id 与行范围全部漂移。
- 换 embedding 模型时，**只有维度不匹配才会报错**；同维度的不同模型不会被拦住，必须自己记得 `--rebuild`。

---

## 7. 故障排查

| 症状 | 原因与处理 |
|---|---|
| `error loading config: ...` | `config.yaml` 语法错误，或 `--config` 指向的文件不存在 |
| Ollama 返回 `503` / 连接被拒 | `ollama serve` 没起来，或 `--ollama-url` 端点不对 |
| `run index --rebuild`（chunker 版本不符） | `chunks.jsonl` 由旧版切块器产出，重新索引 |
| `chunk metadata is stale (source path changed)` | 源文档被移动或改名，重新索引 |
| 源文件 hash 不符 | 源文档被修改过。源文档不可变是第一不变量，必须重新索引 |
| `fts` 查不到内容但 `search` 正常 | 索引时用了 `--no-optimize`，重新索引且不要跳过 optimize |
| 输出里 `rejected=N` 偏高 | 引文对不上原文的条目被丢弃了，见 §6.1 |
| `graph-query` 报 stale | prompt/schema 版本、生成模型或 chunk hash 变了，重跑 `graph-index --rebuild` |

---

## 8. 开发

```sh
uv run pytest                                   # 全量测试（约 3s，完全离线）
uv run pytest tests/test_graph.py -k alias -q   # 单文件 / 单用例
uvx ruff format . && uvx ruff check .           # 格式化与 lint，两者当前均 clean
```

架构与设计文档见 [`CLAUDE.md`](CLAUDE.md)、[`AGENTS.md`](AGENTS.md) 和 [`docs/`](docs/)。
