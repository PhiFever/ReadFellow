# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

ReadFellow 是一个本地优先的 CLI 工作流：把长文档（主要是中文小说）切块、用 Ollama 生成 embedding、存进 zvec 本地向量库，让 agent 能按语义/全文/图谱检索出**原文段落**并带着精确出处与用户讨论。

三条贯穿全代码的不变量（改代码时必须保持）：

1. **源文档是唯一事实依据**；index、metadata、graph 都是派生物，派生物 stale 时必须 fail closed 而不是猜测。
2. **检索只是导航，原始 chunk 才是证据**。任何返回给用户的内容都应回落到存储的 chunk 原文 + `source_path:line_start-line_end`。
3. **防剧透优先**。一旦有阅读进度限制，超出进度的文本绝不能出现在输出里（包括 graph 的别名、类型这类无逐值 provenance 的聚合信息）。

## 常用命令

Python 相关一律走 `uv`（见 `~/.claude/CLAUDE.md`）。`index` / `search` / `graph-index` 需要本地 Ollama 在 `config.yaml` 配置的端点上运行；测试不需要。

```sh
uv run readfellow index corpus/samples/<doc>.txt --collection sample --rebuild --limit 8  # 冒烟索引前 8 个 chunk
uv run readfellow index corpus/samples/<doc>.txt --collection sample --rebuild            # 全量索引
uv run readfellow search "问题" --collection sample --top-k 5      # 向量检索
uv run readfellow fts "关键词" --collection sample --top-k 5       # zvec jieba 中文全文检索
uv run readfellow fetch <chunk-id> --collection sample             # 取回单个 chunk 原文
uv run readfellow graph-index --collection sample --limit 20       # LLM 抽取实体/关系到 graph.json
uv run readfellow graph-query "向山" --collection sample           # 按实体/别名/关系关键词查图谱

# 所有检索命令都支持进度限制：--max-chapter N / --max-line N / --max-chunk-index N
```

测试与 lint（ruff 是 dev 依赖，走 `uv run` 保证版本与规则集一致）：

```sh
uv run pytest                                        # 全量（~3s，无网络依赖）
uv run pytest tests/test_graph.py -k alias -q        # 单个文件 / 单个用例
uvx ruff format . && uvx ruff check .          # 两者当前都保持 clean
```

## 架构

分层严格单向：`cli.py → app.py → {chunking, store, ollama, graph, analysis, progress} → extraction.py → models.py / config.py`

- **`cli.py`** — 薄 adapter。只做 argparse、进度打印、Evidence 格式化。所有默认值都从 `ReadFellowConfig` 取（`build_parser(config)`），全局 flag 通过 `apply_global_overrides` 覆盖成一份 effective config。新增命令时不要在这里写编排逻辑。
- **`app.py`** — 可复用的应用 workflow，是 CLI 之外（未来 MCP / library）唯一该调用的入口：`index_document`、`semantic_search`、`fts_search`、`fetch_chunk`、`build_graph`、`query_graph`。签名统一为 `(config, ..., collection, *, progress: ProgressLimit, options: ...Options, on_progress: Callable[[Event], None])`；进度用 frozen dataclass 事件回调外传，**不在这一层 print**。
- **`chunking.py`** — 先按空行切成 `TextUnit`（保留行号、字节偏移、当前章节标题），再按 `target_chars` 装窗 + 尾部 overlap 拼成 `Chunk`。章节靠 `CHAPTER_RE`（`第X章/节/卷/回` + 序章/楔子/番外等）识别。
- **`store.py`** — 唯一接触 zvec 的地方：schema 定义（`text` 字段挂 jieba `FtsIndexParam`）、collection 打开/重建、`Doc` 转换、vector/FTS query、manifest 与 `chunks.jsonl` 读写。
- **`ollama.py`** — 纯 `urllib` 调 `/api/embed` 与 `/api/generate`（`format=json`，temperature 0），无第三方 SDK。embedding 默认 L2 归一化。`parse_generate_response` 同时兼容单体 JSON 和逐行流式响应。
- **`graph.py`** — 只剩图谱领域：prompt、实体/关系的归一化（`ENTITY_TYPES`/`RELATION_TYPES` 白名单）、合并、失效判定、查询。**不接触网络**，generator 由 `app.build_graph` 注入（`GraphGenerator` Protocol：`generate_json(prompt) -> str`）。
- **`analysis.py`** — 章节级分析，与 `graph.py` 结构对称（prompt / 解析 / 合并 / 失效判定 / 进度过滤），generator 同样由 `app.build_analysis` 注入。
- **`extraction.py`** — `graph.py` 与 `analysis.py` 共用的抽取工具，只依赖 `models`：LLM JSON 读取（`parse_json_object`、`get_any`、`as_list`、`normalize_text`）、证据锚定（`locate_evidence`/`resolve_evidence`，宽松匹配后回读原文）、`Chunk | ChunkContext | Mapping` 归一（`chunk_context`、`int_value`）。**新的领域词汇不要往这里放**——只有第二个派生管线也要用的通用件才进来。
- **`progress.py`** — 由章节/行号算出 `ProgressFilter`：既给 zvec 用的 `expression` 字符串，也给进程内用的 `allows()`。
- **`models.py`** — 所有 Pydantic 模型集中于此，默认 `extra="forbid"`，值对象多为 `frozen=True`。新增字段先改这里，不要在别处塞裸 dict。

产物布局：`indexes/<collection>/`（zvec 数据）、`metadata/<collection>/{manifest.json, chunks.jsonl, graph.json}`。两者都被 git 忽略，`corpus/` 也未纳入版本控制。

### 数据流

索引：`chunk_document` → 用首个 chunk 探测 embedding 维度 → 建/开 collection → **先写 manifest + chunks.jsonl** → 按 `batch_size` 分批：`coll.fetch` 比对 `text_hash` 决定 insert / update / skip → `optimize()` + `flush()`。

检索：读 manifest → 只读方式打开 collection（`read_only=True, enable_mmap=True`）→ 构造 `ProgressFilter` → query → 统一转成 `Evidence`（`_evidence_from_doc`）。graph query 走另一路：图谱命中只给出 chunk id 与上下文，原文仍从 `chunks.jsonl` 的 chunk 取（`_graph_evidence`）。

## 关键约束与易踩坑

- **chunk id = `source_hash[:12]_%06d`**。改 `chunk_chars`/`overlap_chars` 会让 id 与行范围全部漂移，必须 `--rebuild`，否则旧 doc 会残留在 collection 里。
- **换 embedding 模型必须 `--rebuild`**：`open_or_create_collection` 只在维度不匹配时报错，同维度的不同模型不会被拦住。
- **`optimize()` 不能随便跳**。`--no-optimize` 只用于测写入速度；不 optimize 时持久化的中文 FTS 重开后可能查不到。
- **进度过滤有两条路径**，新入口必须两边都走：给 zvec 的 `ProgressFilter.expression` 和进程内的 `ProgressFilter.allows(fields)`（graph、fetch 走后者）。`fetch_chunk` 返回 `found` / `not_found` / `outside_progress` 三态，越界时**不带 text**。
- **graph 在进度限制下会清空 `aliases` 与 `types`**（`graph._filter_entity`）——因为它们是跨 chunk 聚合、没有逐值 provenance。不要为了"信息更全"把这个行为改掉。
- **抽取出的 evidence 必须是所属 chunk 原文的精确子串**，否则 `parse_graph_extraction` 抛错并触发重试。关系名走 `RELATION_TYPES` 白名单 + `_RELATION_ALIASES` 归一，白名单外的关系被静默丢弃（防止"相关""有关"这类泛化边）。
- **改 prompt 必须 bump `GRAPH_PROMPT_VERSION`**（改图谱结构则 bump `GRAPH_SCHEMA_VERSION`）。`graph_staleness_reason` 会比对 schema/prompt 版本、生成模型、extraction settings、每个已处理 chunk 的 source/text hash 与位置；stale 时 `graph-index` 整图重建、`graph-query` 直接报错。仅新增 chunk 时是断点续建（`processed_chunk_ids`）。
- **`_validate_chunk_metadata_source`** 在 graph 路径上先校验 `chunks.jsonl` 的 source path 与源文件当前 hash，源文件被改过就要求重新 index。
- 已知薄弱点：索引不是原子发布，embedding 中途失败会留下 metadata 完整而 collection 不完整的状态（见架构归档）。

## 测试约定

- 全部离线。`FakeEmbedder`、`DeterministicGenerator`（吐预置 JSON）、以及 `monkeypatch.setattr(app, "read_manifest"/"open_existing_collection"/"query_vector", ...)` 打 `app` 模块级符号。
- `tests/test_cli_evidence.py` 用 **真实 zvec** 在 `tmp_path` 里建 collection，覆盖 optimize→重开→中文 FTS→进度过滤→fetch 原文这条链路（需要 `gc.collect()` 释放 collection 句柄）。
- 按 `pyproject.toml` 的 `pythonpath = ["src"]` 直接 `from readfellow... import`，无需装包。
- 不要为健壮性过度加测试；优先补"证据/进度/失效"这三类语义的用例。

## 相关文档

- `docs/architecture-archive.md`（中文）— 架构不足清单 + 优先级路线图 + graph-index 成本估算 + zvec MCP 边界。优先级 1（app 层）、2（Evidence 模型）、3（graph 加固）、4（hybrid retrieval）均已完成。
- `docs/module-deepening-plan.md`（中文）— 2026-07-26 架构评审的执行计划，**当前进行中的工作**。顺序：阶段 A 拆 `graph.py` → 阶段 B 合并 graph/analysis 孪生管线 → 阶段 C 补完 zvec seam；D/E 待触发条件。开工前先读它的「不重新讨论的事」与各阶段验证方式。
- `.codex/skills/readfellow/SKILL.md` — 面向使用者的检索/引用/防剧透规则，回答用户关于语料内容的问题时按它执行。
- `AGENTS.md` — 仓库约定（源文档不可变、产物目录、provenance 字段要求、uv 工作流）与 zvec 能力背景。
