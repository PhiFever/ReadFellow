# AGENTS.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

ReadFellow 是一个本地优先的 CLI 工作流：把长文档（主要是中文小说）切块、用 Ollama 生成 embedding、存进 zvec 本地向量库，让 agent 能按语义/全文/图谱检索出**原文段落**并带着精确出处与用户讨论。

三条贯穿全代码的不变量（改代码时必须保持）：

1. **源文档是唯一事实依据**；index、metadata、graph 都是派生物，派生物 stale 时必须 fail closed 而不是猜测。
2. **检索只是导航，原始 chunk 才是证据**。任何返回给用户的内容都应回落到存储的 chunk 原文 + `source_path:line_start-line_end`。
3. **防剧透优先**。一旦有阅读进度限制，超出进度的文本绝不能出现在输出里（包括 graph 的别名、类型这类无逐值 provenance 的聚合信息）。

## 常用命令

Python 相关一律走 `uv`（见 `~/.claude/CLAUDE.md`）。`index` / `search` / `hybrid` 要本地 Ollama 的 embedding 模型，`graph-index` 要本地生成模型，端点由 `config.yaml` 配置；`analyze` 默认走云端，需要 `LLM_API_KEY`（环境变量或 `.env`）。测试不需要模型服务。

```sh
uv run readfellow db-init                                            # MYSQL_URI 指定的数据库中建表
uv run readfellow import-json --collection sample                     # 一次性导入旧产物
uv run readfellow index corpus/samples/<doc>.txt --collection sample --rebuild --limit 8  # 冒烟索引前 8 个 chunk
uv run readfellow index corpus/samples/<doc>.txt --collection sample --rebuild            # 全量索引
uv run readfellow search "问题" --collection sample --top-k 5      # 向量检索
uv run readfellow fts "关键词" --collection sample --top-k 5       # zvec jieba 中文全文检索
uv run readfellow hybrid "问题" --collection sample --top-k 5      # 向量+FTS 两路融合，图谱标注结果
uv run readfellow fetch <chunk-id> --collection sample             # 取回单个 chunk 原文
uv run readfellow graph-index --collection sample --limit 20       # LLM 抽取实体/关系到 MySQL 当前运行版本
uv run readfellow graph-query "向山" --collection sample           # 按实体/别名/关系关键词查图谱
uv run readfellow analyze --collection sample --max-chapter 50     # LLM 章节级分析到 MySQL 当前运行版本
uv run readfellow status --collection sample                       # 集合体检：存量 + 重跑会续建还是重来 + 抽取质量

# 所有检索命令都支持进度限制：--max-chapter N / --max-line N / --max-chunk-index N
```

元数据及派生物使用 MySQL（SQLAlchemy Core），连接读取 `MYSQL_URI` 环境变量或 `.env`；`database_url` 配置可显式覆盖。离线测试注入 SQL 存储或使用临时 SQLite；`uv run pytest tests/test_artifacts.py --mysql -q` 在随机 MySQL 测试库验收，结束后删掉测试库。

测试与 lint（ruff 是 dev 依赖，走 `uv run` 保证版本与规则集一致）：

```sh
uv run pytest                                        # 全量（~3s，无网络依赖）
uv run pytest tests/test_graph.py -k alias -q        # 单个文件 / 单个用例
uvx ruff format . && uvx ruff check .          # 两者当前都保持 clean
```

## 架构

分层：CLI 只调用 app 工作流；app 编排领域逻辑并注入存储。`store` 封装 zvec，`artifacts` / `artifact_schema` 封装 SQLAlchemy；graph、analysis、extraction 不依赖 SQLAlchemy。`ollama` / `openai_compat` 封装模型接口，`openai` SDK 只在 `openai_compat.py` 里 import。

- **`cli.py`** — 薄 adapter。只做 argparse、进度打印、Evidence 格式化。所有默认值都从 `ReadFellowConfig` 取（`build_parser(config)`），全局 flag 通过 `apply_global_overrides` 覆盖成一份 effective config。新增命令时不要在这里写编排逻辑。
- **`app.py`** — 可复用的应用 workflow，是 CLI 之外（未来 MCP / library）唯一该调用的入口：`index_document`、`semantic_search`、`fts_search`、`fetch_chunk`、`build_graph`、`query_graph`、`collection_status`。签名统一为 `(config, ..., collection, *, progress: ProgressLimit, options: ...Options, on_progress: Callable[[Event], None])`；进度用 frozen dataclass 事件回调外传，**不在这一层 print**。所有外部依赖都是可注入的可选参数：检索/索引收 `store: ChunkStore`，派生管线收 `generator: JsonGenerator`，不传就现场构造真实实现。
- **`chunking.py`** — 先按空行切成 `TextUnit`（保留行号、字节偏移、当前章节标题），再按 `target_chars` 装窗 + 尾部 overlap 拼成 `Chunk`。章节靠 `CHAPTER_RE`（`第X章/节/卷/回` + 序章/楔子/番外等）识别。
- **`store.py`** — 唯一接触 zvec 的地方（这是事实，不是愿望：`app.py` 里没有 `import zvec`，也没有任何 `coll.*` 调用）。对外只有 `ChunkStore` Protocol 6 个方法：`upsert` / `commit` / `search_vector` / `search_fts` / `fetch` / `stats`（`StoreStats`：`doc_count` + `index_completeness`，只给 `status` 用来对账 manifest）。`ZvecChunkStore` 是生产 adapter（schema 定义、collection 打开/重建、`Doc` 转换、批内 text_hash 比对与 insert/update 拆分都在它里面），测试里的 `InMemoryChunkStore` 是第二个 adapter。`Doc` / `Status` / `CollectionOption` 一律不跨 seam：检索结果直接以 `Evidence` 返回。旧 manifest / chunks JSON 读写工具仍在这里，仅供导入与迁移测试；日常元数据读写走 `ArtifactStore`。
- **`artifacts.py` / `artifact_schema.py`** — MySQL 元数据与派生物的唯一持久化入口：版本化 source/chunks、图谱/分析 runs、关系表及事务。应用工作流统一支持 `artifacts: ArtifactStore | None` 注入，默认经 `open_artifacts(config)` 构造。`prepare` 按领域失效回调决定续建或新建 run，`save` 在单元事务内仅更新变化的记录。`legacy_import.py` 只供一次性导入，核对来源并保持旧版本信息。
- **`ollama.py`** — 纯 `urllib` 调 `/api/embed` 与 `/api/generate`，无第三方 SDK。生成走**约束解码**：`format` 传的是 JSON schema（`graph.GRAPH_RESPONSE_SCHEMA` / `analysis.ANALYSIS_RESPONSE_SCHEMA`）而不是 `"json"`，因为后者只保证能解析，裸字符串在数组里也是合法 JSON。采样参数全部来自 `DerivationSettings`（Qwen3 官方 non-thinking 推荐值，temperature 0.7，**不是贪心解码**——贪心会让 retry 逐字节重放同一个坏结果）。embedding 默认 L2 归一化。`parse_generate_response` 同时兼容单体 JSON 和逐行流式响应。
- **`openai_compat.py`** — OpenAI 兼容生成接口，使用 `json_schema` + `strict` 约束输出；写死关闭思考，并逐次校验响应里没有思考内容。429 由 SDK 按 `retry-after` 重试；key 优先从环境变量读取，其次从当前目录的 `.env` 读取。
- **`graph.py`** — 只剩图谱领域：prompt、实体/关系的归一化（`ENTITY_TYPES`/`RELATION_TYPES` 白名单）、合并、失效判定、查询、`graph_diagnostics`。**不接触网络**，generator 由 `app.build_graph` 注入（`derivation.JsonGenerator` Protocol：`generate_json(prompt) -> str`）。
- **`analysis.py`** — 章节级分析，与 `graph.py` 结构对称（prompt / 解析 / 合并 / 失效判定 / 进度过滤），generator 同样由 `app.build_analysis` 注入。
- **`extraction.py`** — `graph.py` 与 `analysis.py` 共用的抽取工具，只依赖 `models`：LLM JSON 读取（`parse_json_object`、`get_any`、`as_list`、`normalize_text`）、证据锚定（`locate_evidence`/`resolve_evidence`，宽松匹配后回读原文）、`Chunk | ChunkContext | Mapping` 归一（`chunk_context`、`int_value`）。**新的领域词汇不要往这里放**——只有第二个派生管线也要用的通用件才进来。
- **`derivation.py`** — graph 与 analysis 两条派生管线共用的骨架：`JsonGenerator` Protocol、`generate_with_retry`（生成+解析算一次尝试）、`write_json_document`（旧文件格式的迁移测试工具）、`derivation_status`（`empty`/`up_to_date`/`built`/`rebuilt`）。领域细节（prompt、解析、合并、各自的 staleness 检查）留在 `graph.py`/`analysis.py`。
- **`progress.py`** — 由章节/行号算出 `ProgressFilter`：既给 zvec 用的 `expression` 字符串，也给进程内用的 `allows()`。
- **`models.py`** — 所有 Pydantic 模型集中于此，默认 `extra="forbid"`，值对象多为 `frozen=True`。新增字段先改这里，不要在别处塞裸 dict。

产物布局：`indexes/<collection>/` 保存当前 zvec 索引；MySQL 保存元数据和全部派生运行历史；`metadata/<collection>/` 只保留旧 JSON 备份及一次性导入输入。`index --rebuild` 不删除备份。

### 数据流

索引：`chunk_document` → 用首个 chunk 探测 embedding 维度 → `ZvecChunkStore.open_for_write` → **先写 MySQL source_versions + chunks** → 按 `batch_size` 分批调 `store.upsert(batch, model=, embed=embedder.embed)`（比对 `text_hash` 决定 insert / update / skip，**只对真要写的 chunk 调 `embed`**）→ `store.commit(optimize=)`。

检索：读 manifest → `ZvecChunkStore.open_for_read`（`read_only=True, enable_mmap=True`）→ 构造 `ProgressFilter` → `store.search_vector/search_fts/fetch`，返回的已经是 `Evidence`。graph query 走另一路：图谱命中只给出 chunk id 与上下文，原文仍从数据库版本化 chunks 取（`_graph_evidence`）。

## 关键约束与易踩坑

- **运行历史**：图谱和分析各自按 `(collection, kind)` 查询最新创建的 `runs.id`，不因未完成、失败或失效回退旧版。重建创建新 run，续建沿用同一 run。关系联接必须包含 `run_id`；chunk 联接必须包含 `source_version_id`。历史切块不覆盖。
- **迁移范围**：MySQL 直接读写，不双写或回退 JSON；zvec 保留向量和中文 FTS。修改 SQL 持久化或导入流程前，阅读 `docs/spec/mysql-artifact-storage.md`；DataGrip 表说明与查询示例见 `docs/mysql-storage.md`。

- **chunk id = `source_hash[:12]_%06d`**。改 `chunk_chars`/`overlap_chars` 会让 id 与行范围全部漂移，必须 `--rebuild`，否则旧 doc 会残留在 collection 里。
- **换 embedding 模型必须 `--rebuild`**：`ZvecChunkStore.open_for_write` 只在维度不匹配时报错，同维度的不同模型不会被拦住。
- **`optimize()` 不能随便跳**。`--no-optimize` 只用于测写入速度；不 optimize 时持久化的中文 FTS 重开后可能查不到。
- **进度过滤有两条路径**，新入口必须两边都走：`ProgressFilter` 整体传给 `ChunkStore`（zvec adapter 用它的 `expression`）、进程内则用 `ProgressFilter.allows(fields)`（graph、fetch 走后者）。`ChunkStore.fetch` **不施加进度限制**——因为只有它要区分「没这个 chunk」和「还没读到」；`fetch_chunk` 拿到 Evidence 后自己判，返回 `found` / `not_found` / `outside_progress` 三态，越界时**不带 text**。
- **graph 在进度限制下会清空 `aliases` 与 `types`**（`graph._filter_entity`）——因为它们是跨 chunk 聚合、没有逐值 provenance。不要为了"信息更全"把这个行为改掉。
- **图谱在 `hybrid` 里只标注、不打分**（`app._annotate_with_graph` → `graph.annotate_chunks`）。融合只有 vector 和 FTS 两路，取 `top_k` 之后才用图谱给选中的 chunk 挂上实体与关系。不要把它改回召回通道：`query_graph` 按子串匹配（`needle in value`），自然语言问句匹配不到实体名；而唯一能大量命中的高频实体（主角覆盖过半 chunk）没有区分度——**桥接产出量与实体区分度互为倒数**。理由与实测见 `docs/graph-channel-demotion.md`。标注本身有**显示预算**（`graph.annotate_chunks`）：每 chunk 最多 3 实体 + 3 关系，只列已声明实体（`types` 或 `evidence` 非空，Tier 判定必须读**存储态**——`_filter_entity` 在进度限制下会清空 `types`），关系要求两端都已声明，再按跨越 chunk 数升序截断。这只收窄显示，`graph-query` 仍全量作答。
- **抽取出的 evidence 必须是所属 chunk 原文的精确子串**（`extraction._LOOSE_IN_EVIDENCE` 允许空白/引号/`…`/`【】`的差异，随后按偏移回读原文）。对不上的**单个条目**被丢弃并计入 `unanchored_count`（`extraction.EvidenceNotFound` + `collect_items`），不再终止整个 unit；**文档级失败（JSON 解析不了、缺 `summary`）仍然抛出并触发重试**——这条边界不要模糊。派生物因此是真实但不完整的子集。关系名走 `RELATION_TYPES` 白名单 + `_RELATION_ALIASES` 归一，白名单外的关系在证据校验**之前**就被丢弃（防止"相关""有关"这类泛化边）。
- **丢弃按成因分两类计数，只有 `unanchored_count` 是质量信号**。`extraction.Rejections` 把 `collect_items` 的损失拆成 `unanchored`（抛 `EvidenceNotFound`，引文不在 chunk 里）与 `unreadable`（`parse` 返回 `None`：谓词超出 `RELATION_TYPES`、实体没名字、字段缺失）；`rejected_count` 是两者之和，`unanchored_count` 单独落盘。2026-07-28 全量实测（2307 chunk）总丢弃 26.7% = **白名单拒绝 20.7% + 证据锚定失败 3.9%**，后者比加固时的 7.8% 还低。白名单拒绝掉的是 `怀疑`/`担心`/`回答`/`使用` 这类叙事长尾谓词，是封闭谓词表的**设计成本，不是缺陷**（9094a72 已统计过：词表外 156 个谓词、146 个只出现一次，没有可扩充的目标）——所以只有 `MAX_UNANCHORED_ITEM_SHARE` 有阈值，总数不设阈值。**不要再去调采样参数降这个数**：实测 `presence_penalty` 1.5→0.0 对丢弃率无影响（40 chunk 配对，29.3% vs 28.9%，sign test p=0.63）。
- **`unanchored_count` 是 `int | None`，`None` 表示"没测过"而不是 0**。拆分之前写的单元没有这个字段，读出来是 `None`；`derivation.sum_or_unknown` 保证只要有一个单元是 `None`，整份派生物的合计就是 `None`（旧图谱续建会同时含两种单元），`status` 于是显示 `cause not recorded` 并跳过告警。不要给它补默认值 0——那等于伪造一次没做过的测量。**这个字段刻意没有 bump `GRAPH_SCHEMA_VERSION`**：按 `docs/storage-engine-decision.md` 第 3 条，带默认值的可选字段旧文件直接能载入，只有语义改动（prompt 变了、抽取结果该不一样）才值得那 7 小时重建。抽取行为一个字节都没变，只是多记了个计数器。
- **改 prompt 必须 bump `GRAPH_PROMPT_VERSION`**（改图谱结构则 bump `GRAPH_SCHEMA_VERSION`）。`graph_staleness_reason` 会比对 schema/prompt 版本、生成模型、extraction settings、每个已处理 chunk 的 source/text hash 与位置；stale 时 `graph-index` 整图重建、`graph-query` 直接报错。仅新增 chunk 时是断点续建（`processed_chunk_ids`）。
- **失效指纹含生成接口地址 `llm_endpoint`**：云端记 base_url，Ollama 记空串；已有 run 读回也是空串，所以本地图谱不会因此失效。字段存在 `runs.extra` JSON 里、不单列，因为 `db-init` 不升级已有表，加列会让已有库读 `runs` 时报 unknown column。
- **`_validate_chunk_metadata_source`** 在 graph 路径上先校验数据库 chunks 的 source path 与源文件当前 hash，源文件被改过就要求重新 index。它被 `app._load_indexed_source` 包住——凡是应用层要读 chunks 原文的派生入口都走这个函数。**唯一例外是 `collection_status`**：它自己通过 `ArtifactStore.read_source` 读取 + 显式调 `_validate_chunk_metadata_source` 并把异常 catch 成 `source_error` 字段，因为 status 的职责就是把失效**报出来**而不是 fail closed。新增入口一律走 `_load_indexed_source`，不要照抄这个例外。
- **`config.graph` 与 `config.analysis` 是同一个 `DerivationConfig` 类的两个实例**，`KnowledgeGraph.extraction_settings` 与 `AnalysisDocument.settings` 也都是 `DerivationSettings`。但这两个**落盘字段名不能动**——改了会让已有 `graph.json`/`analysis.json` 被判 stale 而全量重建。
- **`status` 是唯一"报告失效而不 fail closed"的入口**（`app.collection_status`）。它做三件别处不做的事：失效判定用**配置里当前的模型与参数**（回答"重跑会续建还是从头来"，而不只是"现在坏没坏"）；`ChunkStore.stats()` 的 doc 数与 manifest 对账（这是检出"索引不是原子发布"的唯一手段）；`graph.graph_diagnostics` 的比值带实测参考值（`MIN_DECLARED_ENTITY_SHARE` / `MAX_UNANCHORED_ITEM_SHARE` / `MAX_SILENT_CHUNK_SHARE`，2026-07-28 赛博英雄传 + qwen3:8b 全量）。**丢弃总数没有阈值，只有 unanchored 那部分有**。**参考值是提示不是判死**，换书换模型必然漂移；它们是 read-side 常量，不落盘、不进失效指纹，可以随便调。
- 已知薄弱点：索引不是原子发布，embedding 中途失败会留下 metadata 完整而 collection 不完整的状态（见架构归档）。`status` 能检出，但写入本身仍未改成两阶段发布。派生物侧通过 MySQL 单元事务提交，不跨 zvec 事务。

## 测试约定

- 全部离线。存储走注入：`app` 的 `index_document` / `semantic_search` / `fts_search` / `fetch_chunk` / `hybrid_search` 都收 `store: ChunkStore | None`，测试传 `InMemoryChunkStore`（`tests/test_app.py`），**不要再 monkeypatch 存储相关的 `app` 模块级符号**。元数据/派生物走 `ArtifactStore`，不再 monkeypatch 文件读取函数；剩下的 `monkeypatch.setattr(app, ...)` 只用于 `OllamaEmbedder`（网络），generator 同样走注入（`FakeEmbedder`、`DeterministicGenerator`）。
- `tests/test_cli_evidence.py` 用 **真实 zvec**（`ZvecChunkStore.open_for_write` + `upsert` + `commit`）在 `tmp_path` 里建 collection，覆盖 optimize→重开→中文 FTS→进度过滤→fetch 原文这条链路（需要 `gc.collect()` 释放 collection 句柄）。**它必须保持用真实 zvec，不要换成 `InMemoryChunkStore`。**
- 按 `pyproject.toml` 的 `pythonpath = ["src"]` 直接 `from readfellow... import`，无需装包。
- 不要为健壮性过度加测试；优先补"证据/进度/失效"这三类语义的用例。

## 相关文档

- `docs/spec/narrative-index-plan.md`（中文）— 2026-09-14 按「剧情段高光 + 人物塑造」重定需求后的执行计划：`analyze` 升级为原子笔记、人物身份两遍归并、剧情段索引、评估集先行、图谱冻结、云端派生（b.ai `qwen3.8-flash`，关思考）；章节识别（Q5）已定为「修识别 + A」。**动 `analyze`、章节识别、图谱或检索排序前先读它的「不重新讨论的事」；实施从它的「执行顺序」第 0 步开始。**
- `docs/spec/derivation-hardening-plan.md`（中文）— 2026-07-27 排查出的三个根因（思考模式默认开 / 单条 quote 失败杀死整个 run / 宽松匹配字符类漏 `【】…`）与三项改动，均已实施并在 20 chunk 上验收。**动 `graph-index` / `analyze` 前先读它的「不重新讨论的事」。**
- `docs/mvp-runbook.md`（中文）— 全量跑通示例小说的执行步骤 + 2026-07-27 实测吞吐基线。
- `README.md`（中文）— 面向使用者的命令手册：全局参数、8 个子命令、进度限制、故障排查表。
- `docs/architecture-archive.md`（中文）— 架构不足清单 + 优先级路线图 + graph-index 成本估算 + zvec MCP 边界。优先级 1（app 层）、2（Evidence 模型）、3（graph 加固）、4（hybrid retrieval）均已完成。
- `docs/graph-channel-demotion.md`（中文）— 2026-07-27 把图谱从 hybrid 的打分通道降级为结果标注的论证与实测。**想把图谱改回召回通道前先读它的「不重新讨论的事」；想靠改 prompt 降实体噪声前先读它的「prompt 加严实测无效」。**
- `docs/storage-engine-decision.md`（中文）— 两次存储引擎选型的论证与实测：2026-07-27 的 sqlite（写盘只占全量 run 总耗时 1.8%、zvec 八项能力探针、碎文件 99% 来自 zvec 自身）与 2026-08-29 的 chromadb（内置 BM25 按空白分词，33 字中文只切出 3 个 term；`$contains` 无序，喂不了 rank-based RRF）。**想引入 sqlite、换 chromadb 或把图谱搬进 zvec 前先读它的「不重新讨论的事」；「可搬运性」那条是未决项，不适用该结论。**
- `docs/spec/module-deepening-plan.md`（中文）— 2026-07-26 架构评审的执行计划。阶段 A（拆 `graph.py`）、B（合并 graph/analysis 孪生管线）、C（补完 zvec seam）均已落地；D/E 与 B3 待触发条件。开工前先读它的「不重新讨论的事」。
- `.codex/skills/readfellow/SKILL.md` — 面向使用者的检索/引用/防剧透规则，回答用户关于语料内容的问题时按它执行。
- `AGENTS.md` — 仓库约定（源文档不可变、产物目录、provenance 字段要求、uv 工作流）与 zvec 能力背景。
