# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

ReadFellow 是一个本地优先的 CLI 工作流：把长文档（主要是中文小说）切块、用 Ollama 生成 embedding、存进 zvec 本地向量库，让 agent 能按语义/全文/图谱检索出**原文段落**并带着精确出处与用户讨论。

三条贯穿全代码的不变量（改代码时必须保持）：

1. **源文档是唯一事实依据**；index、metadata、graph 都是派生物，派生物 stale 时必须 fail closed 而不是猜测。
2. **检索只是导航，原始 chunk 才是证据**。任何返回给用户的内容都应回落到存储的 chunk 原文 + `source_path:line_start-line_end`。
3. **防剧透优先**。一旦有阅读进度限制，超出进度的文本绝不能出现在输出里（包括 graph 的别名、类型这类无逐值 provenance 的聚合信息）。

## 常用命令

Python 相关一律走 `uv`（见 `~/.claude/CLAUDE.md`）。除 `fts` / `fetch` / `graph-query` 外都需要本地 Ollama 在 `config.yaml` 配置的端点上运行（`index` / `search` / `hybrid` 要 embedding 模型，`graph-index` / `analyze` 要生成模型）；测试不需要。

```sh
uv run readfellow index corpus/samples/<doc>.txt --collection sample --rebuild --limit 8  # 冒烟索引前 8 个 chunk
uv run readfellow index corpus/samples/<doc>.txt --collection sample --rebuild            # 全量索引
uv run readfellow search "问题" --collection sample --top-k 5      # 向量检索
uv run readfellow fts "关键词" --collection sample --top-k 5       # zvec jieba 中文全文检索
uv run readfellow hybrid "问题" --collection sample --top-k 5      # 向量+FTS 两路融合，图谱标注结果
uv run readfellow fetch <chunk-id> --collection sample             # 取回单个 chunk 原文
uv run readfellow graph-index --collection sample --limit 20       # LLM 抽取实体/关系到 graph.json
uv run readfellow graph-query "向山" --collection sample           # 按实体/别名/关系关键词查图谱
uv run readfellow analyze --collection sample --max-chapter 50     # LLM 章节级分析到 analysis.json

# 所有检索命令都支持进度限制：--max-chapter N / --max-line N / --max-chunk-index N
```

测试与 lint（ruff 是 dev 依赖，走 `uv run` 保证版本与规则集一致）：

```sh
uv run pytest                                        # 全量（~3s，无网络依赖）
uv run pytest tests/test_graph.py -k alias -q        # 单个文件 / 单个用例
uvx ruff format . && uvx ruff check .          # 两者当前都保持 clean
```

## 架构

分层严格单向：`cli.py → app.py → {chunking, store, ollama, graph, analysis, progress} → {extraction.py, derivation.py} → models.py / config.py`

- **`cli.py`** — 薄 adapter。只做 argparse、进度打印、Evidence 格式化。所有默认值都从 `ReadFellowConfig` 取（`build_parser(config)`），全局 flag 通过 `apply_global_overrides` 覆盖成一份 effective config。新增命令时不要在这里写编排逻辑。
- **`app.py`** — 可复用的应用 workflow，是 CLI 之外（未来 MCP / library）唯一该调用的入口：`index_document`、`semantic_search`、`fts_search`、`fetch_chunk`、`build_graph`、`query_graph`。签名统一为 `(config, ..., collection, *, progress: ProgressLimit, options: ...Options, on_progress: Callable[[Event], None])`；进度用 frozen dataclass 事件回调外传，**不在这一层 print**。所有外部依赖都是可注入的可选参数：检索/索引收 `store: ChunkStore`，派生管线收 `generator: JsonGenerator`，不传就现场构造真实实现。
- **`chunking.py`** — 先按空行切成 `TextUnit`（保留行号、字节偏移、当前章节标题），再按 `target_chars` 装窗 + 尾部 overlap 拼成 `Chunk`。章节靠 `CHAPTER_RE`（`第X章/节/卷/回` + 序章/楔子/番外等）识别。
- **`store.py`** — 唯一接触 zvec 的地方（这是事实，不是愿望：`app.py` 里没有 `import zvec`，也没有任何 `coll.*` 调用）。对外只有 `ChunkStore` Protocol 5 个方法：`upsert` / `commit` / `search_vector` / `search_fts` / `fetch`。`ZvecChunkStore` 是生产 adapter（schema 定义、collection 打开/重建、`Doc` 转换、批内 text_hash 比对与 insert/update 拆分都在它里面），测试里的 `InMemoryChunkStore` 是第二个 adapter。`Doc` / `Status` / `CollectionOption` 一律不跨 seam：检索结果直接以 `Evidence` 返回。manifest 与 `chunks.jsonl` 读写也在这里，但和 zvec 无关。
- **`ollama.py`** — 纯 `urllib` 调 `/api/embed` 与 `/api/generate`（`format=json`，temperature 0），无第三方 SDK。embedding 默认 L2 归一化。`parse_generate_response` 同时兼容单体 JSON 和逐行流式响应。
- **`graph.py`** — 只剩图谱领域：prompt、实体/关系的归一化（`ENTITY_TYPES`/`RELATION_TYPES` 白名单）、合并、失效判定、查询。**不接触网络**，generator 由 `app.build_graph` 注入（`derivation.JsonGenerator` Protocol：`generate_json(prompt) -> str`）。
- **`analysis.py`** — 章节级分析，与 `graph.py` 结构对称（prompt / 解析 / 合并 / 失效判定 / 进度过滤），generator 同样由 `app.build_analysis` 注入。
- **`extraction.py`** — `graph.py` 与 `analysis.py` 共用的抽取工具，只依赖 `models`：LLM JSON 读取（`parse_json_object`、`get_any`、`as_list`、`normalize_text`）、证据锚定（`locate_evidence`/`resolve_evidence`，宽松匹配后回读原文）、`Chunk | ChunkContext | Mapping` 归一（`chunk_context`、`int_value`）。**新的领域词汇不要往这里放**——只有第二个派生管线也要用的通用件才进来。
- **`derivation.py`** — graph 与 analysis 两条派生管线共用的骨架：`JsonGenerator` Protocol、`generate_with_retry`（生成+解析算一次尝试）、`load_or_reset`（不存在 / rebuild / stale 三态，是 fail closed 的单一落点）、`write_json_document`、`derivation_status`（`empty`/`up_to_date`/`built`/`rebuilt`）。领域细节（prompt、解析、合并、各自的 staleness 检查）留在 `graph.py`/`analysis.py`。
- **`progress.py`** — 由章节/行号算出 `ProgressFilter`：既给 zvec 用的 `expression` 字符串，也给进程内用的 `allows()`。
- **`models.py`** — 所有 Pydantic 模型集中于此，默认 `extra="forbid"`，值对象多为 `frozen=True`。新增字段先改这里，不要在别处塞裸 dict。

产物布局：`indexes/<collection>/`（zvec 数据）、`metadata/<collection>/{manifest.json, chunks.jsonl, graph.json}`。两者都被 git 忽略，`corpus/` 也未纳入版本控制。

### 数据流

索引：`chunk_document` → 用首个 chunk 探测 embedding 维度 → `ZvecChunkStore.open_for_write` → **先写 manifest + chunks.jsonl** → 按 `batch_size` 分批调 `store.upsert(batch, model=, embed=embedder.embed)`（比对 `text_hash` 决定 insert / update / skip，**只对真要写的 chunk 调 `embed`**）→ `store.commit(optimize=)`。

检索：读 manifest → `ZvecChunkStore.open_for_read`（`read_only=True, enable_mmap=True`）→ 构造 `ProgressFilter` → `store.search_vector/search_fts/fetch`，返回的已经是 `Evidence`。graph query 走另一路：图谱命中只给出 chunk id 与上下文，原文仍从 `chunks.jsonl` 的 chunk 取（`_graph_evidence`）。

## 关键约束与易踩坑

- **chunk id = `source_hash[:12]_%06d`**。改 `chunk_chars`/`overlap_chars` 会让 id 与行范围全部漂移，必须 `--rebuild`，否则旧 doc 会残留在 collection 里。
- **换 embedding 模型必须 `--rebuild`**：`ZvecChunkStore.open_for_write` 只在维度不匹配时报错，同维度的不同模型不会被拦住。
- **`optimize()` 不能随便跳**。`--no-optimize` 只用于测写入速度；不 optimize 时持久化的中文 FTS 重开后可能查不到。
- **进度过滤有两条路径**，新入口必须两边都走：`ProgressFilter` 整体传给 `ChunkStore`（zvec adapter 用它的 `expression`）、进程内则用 `ProgressFilter.allows(fields)`（graph、fetch 走后者）。`ChunkStore.fetch` **不施加进度限制**——因为只有它要区分「没这个 chunk」和「还没读到」；`fetch_chunk` 拿到 Evidence 后自己判，返回 `found` / `not_found` / `outside_progress` 三态，越界时**不带 text**。
- **graph 在进度限制下会清空 `aliases` 与 `types`**（`graph._filter_entity`）——因为它们是跨 chunk 聚合、没有逐值 provenance。不要为了"信息更全"把这个行为改掉。
- **图谱在 `hybrid` 里只标注、不打分**（`app._annotate_with_graph` → `graph.annotate_chunks`）。融合只有 vector 和 FTS 两路，取 `top_k` 之后才用图谱给选中的 chunk 挂上实体与关系。不要把它改回召回通道：`query_graph` 按子串匹配（`needle in value`），自然语言问句匹配不到实体名；而唯一能大量命中的高频实体（主角覆盖过半 chunk）没有区分度——**桥接产出量与实体区分度互为倒数**。理由与实测见 `docs/graph-channel-demotion.md`。
- **抽取出的 evidence 必须是所属 chunk 原文的精确子串**（`extraction._LOOSE_IN_EVIDENCE` 允许空白/引号/`…`/`【】`的差异，随后按偏移回读原文）。对不上的**单个条目**被丢弃并计入 `rejected_count`（`extraction.EvidenceNotFound` + `collect_items`），不再终止整个 unit；**文档级失败（JSON 解析不了、缺 `summary`）仍然抛出并触发重试**——这条边界不要模糊。派生物因此是真实但不完整的子集，实测约丢 7%。关系名走 `RELATION_TYPES` 白名单 + `_RELATION_ALIASES` 归一，白名单外的关系在证据校验之前就被静默丢弃（防止"相关""有关"这类泛化边），**不计入 `rejected_count`**。
- **改 prompt 必须 bump `GRAPH_PROMPT_VERSION`**（改图谱结构则 bump `GRAPH_SCHEMA_VERSION`）。`graph_staleness_reason` 会比对 schema/prompt 版本、生成模型、extraction settings、每个已处理 chunk 的 source/text hash 与位置；stale 时 `graph-index` 整图重建、`graph-query` 直接报错。仅新增 chunk 时是断点续建（`processed_chunk_ids`）。
- **`_validate_chunk_metadata_source`** 在 graph 路径上先校验 `chunks.jsonl` 的 source path 与源文件当前 hash，源文件被改过就要求重新 index。它被 `app._load_indexed_source` 包住——凡是要读 `chunks.jsonl` 的入口都走这个函数，别再单独 `read_chunks`。
- **`config.graph` 与 `config.analysis` 是同一个 `DerivationConfig` 类的两个实例**，`KnowledgeGraph.extraction_settings` 与 `AnalysisDocument.settings` 也都是 `DerivationSettings`。但这两个**落盘字段名不能动**——改了会让已有 `graph.json`/`analysis.json` 被判 stale 而全量重建。
- 已知薄弱点：索引不是原子发布，embedding 中途失败会留下 metadata 完整而 collection 不完整的状态（见架构归档）。

## 测试约定

- 全部离线。存储走注入：`app` 的 `index_document` / `semantic_search` / `fts_search` / `fetch_chunk` / `hybrid_search` 都收 `store: ChunkStore | None`，测试传 `InMemoryChunkStore`（`tests/test_app.py`），**不要再 monkeypatch 存储相关的 `app` 模块级符号**。剩下的 `monkeypatch.setattr(app, ...)` 只用于 `read_manifest`（文件）与 `OllamaEmbedder`（网络），generator 同样走注入（`FakeEmbedder`、`DeterministicGenerator`）。
- `tests/test_cli_evidence.py` 用 **真实 zvec**（`ZvecChunkStore.open_for_write` + `upsert` + `commit`）在 `tmp_path` 里建 collection，覆盖 optimize→重开→中文 FTS→进度过滤→fetch 原文这条链路（需要 `gc.collect()` 释放 collection 句柄）。**它必须保持用真实 zvec，不要换成 `InMemoryChunkStore`。**
- 按 `pyproject.toml` 的 `pythonpath = ["src"]` 直接 `from readfellow... import`，无需装包。
- 不要为健壮性过度加测试；优先补"证据/进度/失效"这三类语义的用例。

## 相关文档

- `docs/derivation-hardening-plan.md`（中文）— 2026-07-27 排查出的三个根因（思考模式默认开 / 单条 quote 失败杀死整个 run / 宽松匹配字符类漏 `【】…`）与三项改动，均已实施并在 20 chunk 上验收。**动 `graph-index` / `analyze` 前先读它的「不重新讨论的事」。**
- `docs/mvp-runbook.md`（中文）— 全量跑通示例小说的执行步骤 + 2026-07-27 实测吞吐基线。
- `README.md`（中文）— 面向使用者的命令手册：全局参数、8 个子命令、进度限制、故障排查表。
- `docs/architecture-archive.md`（中文）— 架构不足清单 + 优先级路线图 + graph-index 成本估算 + zvec MCP 边界。优先级 1（app 层）、2（Evidence 模型）、3（graph 加固）、4（hybrid retrieval）均已完成。
- `docs/graph-channel-demotion.md`（中文）— 2026-07-27 把图谱从 hybrid 的打分通道降级为结果标注的论证与实测。**想把图谱改回召回通道前先读它的「不重新讨论的事」。**
- `docs/module-deepening-plan.md`（中文）— 2026-07-26 架构评审的执行计划。阶段 A（拆 `graph.py`）、B（合并 graph/analysis 孪生管线）、C（补完 zvec seam）均已落地；D/E 与 B3 待触发条件。开工前先读它的「不重新讨论的事」。
- `.codex/skills/readfellow/SKILL.md` — 面向使用者的检索/引用/防剧透规则，回答用户关于语料内容的问题时按它执行。
- `AGENTS.md` — 仓库约定（源文档不可变、产物目录、provenance 字段要求、uv 工作流）与 zvec 能力背景。
