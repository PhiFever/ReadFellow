# 章节分析

`analyze [--chapters A-B]` 读取书库中的整章原文，为每章生成一句摘要、最多六个人物称呼和情节标签。解析失败、人物称呼在全书找不到或首次出现晚于本章末行时，会重新生成本章。

摘要轮结束后，每章再调用一次模型，同时生成关键时刻（moments）和别名关系（identities）。两轮各用一个生成器。摘要继续用配置中的 `analysis.num_predict`，moments 上限为 3072 个 token。JSON 解析或结构校验失败时会重试整章，次数用尽后报告失败。摘要和 moments 遇到 `GenerationRejected` 时都立即报告该章失败，跳过整章重试。

每条引文都要在整章原文中定位。保存命中的原文和章内相对行号。别名关系还要求两个称呼都出现在同一段引文中。引文检查失败时只丢该条，计入 `unanchored_count`。人物称呼只去掉首尾空白，首次出现晚于本章末行或全书找不到时，丢弃该条并计入 `name_rejected_count`。没有参与人物的关键时刻可以保留。

新写入的笔记在扫描前不可见。每次运行结束时，即使两轮都命中缓存，也会在同一事务里重扫全书。人名取自当前配置下的摘要人物、关键时刻参与者和别名关系的两个称呼。摘要提到后文才出现的名字时隐藏整章摘要；关键时刻的描述或性格字段提到这样的名字时，只隐藏该条。其他条目照常可读。

`note_configs` 记录提示词和结构版本、模型、端点、输出格式和采样参数。摘要和 moments 按种类分别启用对应配置，旧配置仍可复用。`chapter_notes` 按配置和章节正文哈希保存笔记、剧透标记和生成时间。工作线程并发请求模型，主线程逐章提交结果。切块参数变化后可以复用所有笔记，修改正文后只需补写内容变化的章。

`analysis.py` 保留构造章节提示词、估算上下文预算、去除重叠文本、解析人物与事件、将引文锚定到原文的纯函数，以及 prompt 和 schema 版本常量。`extraction.py` 提供通用解析与证据定位工具，领域词汇留在 `analysis.py`。`derivation.py` 保留 `JsonGenerator`、`generate_with_retry`（生成加解析算一次尝试）和 `sum_or_unknown`。

## 生成接口

- `ollama.py` 用纯 `urllib` 调 `/api/generate`。生成走**约束解码**：`format` 传 JSON schema（`ANALYSIS_RESPONSE_SCHEMA`），不传 `"json"`，因为后者只保证能解析，裸字符串在数组里也是合法 JSON。
- 采样参数全部来自 `DerivationSettings`（Qwen3 官方 non-thinking 推荐值，temperature 0.7）。**不用贪心解码**：贪心会让 retry 逐字节重放同一个坏结果。
- `openai_compat.py` 用 `json_schema` + `strict` 约束输出，采样参数只传 `temperature`、`top_p` 和 `max_tokens`。响应含有思考内容或思考 token 数非零时，生成器立即抛出 `GenerationRejected`。首次请求与 429 重试共用每分钟的请求配额。429 优先按 `retry-after` 等待，缺少该响应头时依次等 2、4、8 秒，最多等 30 秒，最多重试 15 次。

## 证据锚定与丢弃计数

- 抽取出的 evidence 必须是原文的精确子串。`extraction._LOOSE_IN_EVIDENCE` 允许空白、引号、`…`、`【】` 的差异，匹配后按偏移回读原文。
- **单个条目**对不上就丢弃并计数（`EvidenceNotFound` + `collect_items`），不终止整章；**整章失败**（JSON 解析不了、缺梗概）仍然抛出并触发重试。结果因此是真实但不完整的子集。
- `extraction.Rejections` 把丢弃拆成两类：`unanchored`（引文不在原文里）与 `unreadable`（`parse` 返回 `None`：人物没名字、事件没描述、字段缺失）。`rejected_count` 是两者之和，`unanchored_count` 单独记录。只有 `unanchored` 是质量信号。
- **`unanchored_count` 是 `int | None`**，`None` 表示「没测过」。合计走 `sum_or_unknown`，只要有一个是 `None`，合计就是 `None`，不补默认值 0。
