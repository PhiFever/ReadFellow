# 章节分析

第 2 步已下线 `analyze`。第 6 步将以逐章笔记缓存重新提供章节分析。

`analysis.py` 保留构造章节提示词、估算上下文预算、去除重叠文本、解析人物与事件、将引文锚定到原文的纯函数，以及 prompt 和 schema 版本常量。`extraction.py` 提供通用解析与证据定位工具，领域词汇留在 `analysis.py`。`derivation.py` 保留 `JsonGenerator`、`generate_with_retry`（生成加解析算一次尝试）和 `sum_or_unknown`。

## 生成接口

- `ollama.py` 用纯 `urllib` 调 `/api/generate`。生成走**约束解码**：`format` 传 JSON schema（`ANALYSIS_RESPONSE_SCHEMA`），不传 `"json"`，因为后者只保证能解析，裸字符串在数组里也是合法 JSON。
- 采样参数全部来自 `DerivationSettings`（Qwen3 官方 non-thinking 推荐值，temperature 0.7）。**不用贪心解码**：贪心会让 retry 逐字节重放同一个坏结果。
- `openai_compat.py` 用 `json_schema` + `strict` 约束输出，写死关闭思考并逐次校验响应里没有思考内容；429 由 SDK 按 `retry-after` 重试。

## 证据锚定与丢弃计数

- 抽取出的 evidence 必须是原文的精确子串。`extraction._LOOSE_IN_EVIDENCE` 允许空白、引号、`…`、`【】` 的差异，匹配后按偏移回读原文。
- **单个条目**对不上就丢弃并计数（`EvidenceNotFound` + `collect_items`），不终止整章；**整章失败**（JSON 解析不了、缺梗概）仍然抛出并触发重试。结果因此是真实但不完整的子集。
- `extraction.Rejections` 把丢弃拆成两类：`unanchored`（引文不在原文里）与 `unreadable`（`parse` 返回 `None`：人物没名字、事件没描述、字段缺失）。`rejected_count` 是两者之和，`unanchored_count` 单独记录。只有 `unanchored` 是质量信号。
- **`unanchored_count` 是 `int | None`**，`None` 表示「没测过」。合计走 `sum_or_unknown`，只要有一个是 `None`，合计就是 `None`，不补默认值 0。
