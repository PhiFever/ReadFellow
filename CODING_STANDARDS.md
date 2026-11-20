# 编码规范

审查 diff 时逐条核对。import 边界已由 ruff `TID251` 强制，这里只收 lint 查不出、要靠判断的规则。机制和理由见 `docs/retrieval.md`、`docs/derivation.md`。

## 不变量

- 返回给用户的内容都回落到存储的 chunk 原文 + `source_path:line_start-line_end`。
- 派生物与源文档不一致时 fail closed，不猜。
- 有进度限制时，超出进度的文本不出现在任何输出里，包括没有逐值 provenance 的聚合信息。

## 分层

- `cli.py` 只做 argparse、进度打印、Evidence 格式化；默认值从 `ReadFellowConfig` 取。编排逻辑放进 `app.py`。
- `app.py` 的 workflow 签名统一为 `(config, ..., collection, *, progress, options, on_progress)`，进度用 frozen dataclass 事件回调外传，这一层不 print。
- Pydantic 模型集中在 `models.py`，默认 `extra="forbid"`，值对象用 `frozen=True`。新字段先加到这里，不在别处传裸 dict。
- `extraction.py` 只收通用解析与证据锚定工具，领域词汇留在 `analysis.py`。

## 检索入口

- 新的检索或派生入口经 `app._read_collection` 校验源文件；`collection_status` 的 catch-and-report 是唯一例外，不照抄。
- 新入口两条进度过滤路径都要走：`ProgressFilter` 传给 `ChunkStore`，进程内用 `ProgressFilter.allows()`。

## 派生物与持久化

- 改 prompt 时 bump `ANALYSIS_PROMPT_VERSION`；改结构时 bump `ANALYSIS_SCHEMA_VERSION`。只加带默认值的可选字段、抽取行为不变时不 bump。
- `unanchored_count` 等「可能没测过」的计数用 `None` 表示未知，合计走 `derivation.sum_or_unknown`，不补默认值 0。
- 单个条目锚定失败只丢该条并计数；文档级失败（JSON 解析不了、缺必需字段）抛出并重试。
- 每批 chunk 原文、向量和 FTS5 行在同一个事务内提交，章节和书目一起提交。

## 测试

- 测试使用临时目录里的真实 SQLite 库，通过 monkeypatch 替换 OllamaEmbedder，保持全量离线。
- `tests/test_cli_evidence.py` 从 CLI 建库，重开后验证中文全文检索、向量检索、进度过滤和按 id 取回原文。
- 新用例优先覆盖证据、进度、失效三类语义。
