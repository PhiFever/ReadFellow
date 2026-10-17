# 编码规范

审查 diff 时逐条核对。import 边界已由 ruff `TID251` 强制，这里只收 lint 查不出、要靠判断的规则。机制和理由见 `docs/retrieval.md`、`docs/derivation.md`。

## 不变量

- 返回给用户的内容都回落到存储的 chunk 原文 + `source_path:line_start-line_end`。
- 派生物与源文档不一致时 fail closed，不猜。
- 有进度限制时，超出进度的文本不出现在任何输出里，包括没有逐值 provenance 的聚合信息。graph 在进度限制下清空 `aliases` 与 `types`（`graph._filter_entity`），这个行为保留。

## 分层

- `cli.py` 只做 argparse、进度打印、Evidence 格式化；默认值从 `ReadFellowConfig` 取。编排逻辑放进 `app.py`。
- `app.py` 的 workflow 签名统一为 `(config, ..., collection, *, progress, options, on_progress)`，进度用 frozen dataclass 事件回调外传，这一层不 print。外部依赖（`store`、`artifacts`、`generator`）都是可注入的可选参数。
- Pydantic 模型集中在 `models.py`，默认 `extra="forbid"`，值对象用 `frozen=True`。新字段先加到这里，不在别处传裸 dict。
- `extraction.py` 只收两条派生管线都要用的通用件，领域词汇留在 `graph.py` / `analysis.py`。

## 检索入口

- 新的检索或派生入口经 `app._load_manifest` 或 `app._load_indexed_source` 校验源文件；`collection_status` 的 catch-and-report 是唯一例外，不照抄。
- 新入口两条进度过滤路径都要走：`ProgressFilter` 传给 `ChunkStore`，进程内用 `ProgressFilter.allows()`。
- 图谱在 `hybrid` 里只给 top-k 结果挂标注，不参与打分或召回。

## 派生物与持久化

- 改 prompt 时 bump `GRAPH_PROMPT_VERSION` / `ANALYSIS_PROMPT_VERSION`；改结构时 bump `GRAPH_SCHEMA_VERSION` / `ANALYSIS_SCHEMA_VERSION`。只加带默认值的可选字段、抽取行为不变时不 bump。
- 已落盘的字段名保持原样（如 `extraction_settings`、`settings`），改名会让已有派生物全量重建。
- `unanchored_count` 等「可能没测过」的计数用 `None` 表示未知，合计走 `derivation.sum_or_unknown`，不补默认值 0。
- 单个条目锚定失败只丢该条并计数；文档级失败（JSON 解析不了、缺必需字段）抛出并重试。
- SQL 里关系联接带 `run_id`，chunk 联接带 `source_version_id`。
- 同一个 run 只有一个写者；并发只用于 LLM 调用，merge 与 save 由单线程按顺序执行。

## 测试

- 全部离线。存储走注入：`store` 传 `InMemoryChunkStore`，元数据传 `ArtifactStore`，generator 传 `DeterministicGenerator` 一类的假实现；`monkeypatch.setattr(app, ...)` 只用于 `OllamaEmbedder`。
- `tests/test_cli_evidence.py` 使用真实 zvec（`ZvecChunkStore`），覆盖 optimize → 重开 → 中文 FTS → 进度过滤 → fetch 原文这条链路。
- 新用例优先覆盖证据、进度、失效三类语义。
