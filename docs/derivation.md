# 派生管线：章节分析与持久化

改 `analyze`、失效指纹、`artifacts.py` 或 `status` 之前读这份。审查规则见 `CODING_STANDARDS.md`，已定的设计决策见 `docs/decisions.md`。

## 模块分工

- `analysis.py` 负责 prompt、解析、合并、失效判定和进度过滤，不接触网络。generator 由 `app.build_analysis` 注入（`derivation.JsonGenerator` Protocol：`generate_json(prompt) -> str`）。
- `extraction.py` 提供抽取工具：LLM JSON 读取（`parse_json_object`、`get_any`、`as_list`、`normalize_text`）、证据锚定（`locate_evidence` / `resolve_evidence`）、`Chunk | ChunkContext | Mapping` 归一。领域词汇留在 `analysis.py`。
- `derivation.py` 是共用骨架：`JsonGenerator`、`generate_with_retry`（生成+解析算一次尝试）、`derivation_status`（`empty` / `up_to_date` / `built` / `rebuilt`）、`settings_changed`、`sum_or_unknown`。
- `config.analysis` 是 `DerivationConfig`；`AnalysisDocument.settings` 是 `DerivationSettings`。**落盘字段名不能改**，改了已有派生物会被判 stale 而全量重建。

## 持久化与运行历史

MySQL 直接读写，不双写也不回退 JSON；zvec 只保留向量和中文 FTS。改 SQL 持久化或导入流程前读 `docs/archive/mysql-artifact-storage.md`；表说明与 DataGrip 查询见 `docs/mysql-storage.md`。

- 章节分析按 `(collection, kind="analysis")` 取最新创建的 `runs.id`，不因未完成、失败或失效回退旧版。重建创建新 run，续建沿用同一 run。
- 关系联接必须包含 `run_id`；chunk 联接必须包含 `source_version_id`。历史切块不覆盖。
- `prepare` 按领域失效回调决定续建或新建 run，`save` 在单元事务内只更新变化的记录。
- **单写者**：`ArtifactStore` 记下自己上次读到或写入的 runs 行，`_save` 加锁后发现这一行被别的进程改过就报错回滚。要并行就在进程内并发 LLM 调用，由一个线程按顺序 merge 和 save。
- **落盘节奏**：`build_analysis` 不逐单元 save，而是经 `app._Checkpoint` 按耗时落盘（`SAVE_INTERVAL_SECONDS`、`SAVE_COST_RATIO`），循环的 `finally` 里补存；merge 做到一半被打断时不补存，免得存下半合并的文档。
- `legacy_import.py` 只供一次性导入 chunks、manifest 和 analysis，核对来源并保持旧版本信息。

## 失效判定

`analysis_staleness_reason` 比对 schema / prompt 版本、生成模型、settings（`retries`、`num_ctx` 除外，见 `settings_changed`）、已分析章节的序号、标题、chunk id 与 text hash。stale 时 `analyze` 创建新 run；已分析章节仍匹配时，按 `processed_chapter_keys` 断点续建。

- prompt 与 schema 版本常量是 `ANALYSIS_PROMPT_VERSION` / `ANALYSIS_SCHEMA_VERSION`。只加带默认值的可选字段、抽取行为不变时不 bump，旧数据直接能载入（理由见 `docs/storage-engine-decision.md` 第 3 条）。
- 指纹含生成接口地址 `llm_endpoint`：云端记 base_url，Ollama 记空串，旧 run 读回也是空串，所以本地分析不会因此失效。
- `llm_endpoint` 与 `response_format` 存在 `runs.extra` JSON 里，不单列：`db-init` 不升级已有表，加列会让已有库读 `runs` 时报 unknown column。
- 旧 run 的 `response_format` 为 `None` 时不判失效：缺失代表设置未知，猜默认值可能误触发重建，或把 `json_object` 误记为 `json_schema`。续建时记下当前值，之后的变化才检测。

## 证据锚定与丢弃计数

- 抽取出的 evidence 必须是所属 chunk 原文的精确子串。`extraction._LOOSE_IN_EVIDENCE` 允许空白、引号、`…`、`【】` 的差异，匹配后按偏移回读原文。
- **单个条目**对不上就丢弃并计数（`EvidenceNotFound` + `collect_items`），不终止整个 unit；**文档级失败**（JSON 解析不了、缺 `summary`）仍然抛出并触发重试。派生物因此是真实但不完整的子集。
- `extraction.Rejections` 把丢弃拆成两类：`unanchored`（引文不在 chunk 里）与 `unreadable`（`parse` 返回 `None`：人物没名字、事件没描述、字段缺失）。`rejected_count` 是两者之和，`unanchored_count` 单独落盘。只有 `unanchored` 是质量信号。
- **`unanchored_count` 是 `int | None`**，`None` 表示「没测过」。拆分前写的单元读出来是 `None`；`sum_or_unknown` 保证只要有一个单元是 `None`，合计就是 `None`，`status` 于是显示 `cause not recorded`。

## `status`

`app.collection_status` 是唯一报告失效而不 fail closed 的入口。它做两件别处不做的事：

- 失效判定用**配置里当前的模型与参数**，回答「重跑会续建还是从头来」。
- `ChunkStore.stats()` 的 doc 数与 manifest 对账，这是检出「索引不是原子发布」的唯一手段。
