# 派生管线加固执行计划

日期：2026-07-27
状态：**已决策，待实施**
触发：`graph-index` / `analyze` 全量跑不完（见 `docs/mvp-runbook.md`）

本文是 2026-07-27 对「派生管线抛异常终止」的排查结论与执行计划。所有数字来自本机实测（Ollama 0.32.4 + `qwen3:8b`，语料 `corpus/samples/赛博英雄传.txt`），不是估算。

## 排查结论：三个独立根因

抛异常终止不是单一原因，是三个正交问题叠加。下表是 20 个 chunk 的实测：

| 根因 | 现象 | 实测 |
|---|---|---|
| A · qwen3 思考模式默认开 | JSON 收尾丢失 | 12 个 chunk 里 4 个失败（33%） |
| B · 一条 quote 失败杀死整个 run | 全量必然跑不完 | 每 chunk 约 18.7 条 quote，单条失败率 7.2%，全清 chunk 仅 35% |
| C · 宽松匹配字符类不完整 | evidence 误判为非原文 | 单条失败率 9.4% → 7.2% |

### 根因 A · 思考模式导致 JSON 收尾丢失

模型答完正文、闭合了 `relations` 数组之后，不输出收尾的 `}`，改为连续输出空行直到 `num_predict` 耗尽。**内容是完整的**——4 个失败样本补一个 `}` 后全部可解析，各含 12–13 个实体、13–19 条关系。

原因在 Ollama 服务端（`ollama/ollama` `server/routes.go`）：

```go
if slices.Contains(modelCaps, model.CapabilityThinking) {
    if req.Think == nil { req.Think = &api.ThinkValue{Value: true} }   // 不传 = 开启
} else {
    if req.Think != nil && req.Think.Bool() {                          // 只有传 true 才报错
        ..."does not support thinking"
    }
}
```

`OllamaGenerateRequest`（`models.py:330`）没有 `think` 字段，于是 qwen3 一直在思考模式下运行。prompt 里那句「不要输出思考过程，不要使用 `<think>` 标签」是提示层的，压不住模板层；而 `format=json` 的 grammar 允许任意空白，也拦不住空行循环。

对照实验（4 个已知失败 chunk）：

| 变体 | JSON 成功 | eval_count |
|---|---|---|
| 现状 | 2/4 | 1182–1211 |
| **`think: false`** | **4/4** | 841–1012 |
| 去掉 `format=json` | 4/4 | 1463–2273 |
| 加 `stop` 序列 | 1/4（会截断正常输出，更糟） | — |

`think: false` 在 20 chunk 上复测 **JSON 失败 0/20**，并且省约 20% 生成 token。上面那段 Go 代码同时证明：传 `false` 对不支持思考的模型是安全的（错误只在传 `true` 时触发），所以不需要可选字段或降级处理。

### 根因 B · 一条 quote 失败杀死整个 run

量级最大的问题。`resolve_evidence`（`extraction.py:89`）抛 `ValueError` → `parse_graph_extraction` 不捕获 → `generate_with_retry` 重试**整块** → 耗尽后 `RuntimeError` → `app.py:498` 裸调用不捕获 → 整个 run 终止。

所以**不是「某些 chunk 是硬骨头」**，而是几乎每个 chunk 都有至少一条坏 quote，一条就够全灭。即使同时上了根因 A 与 C 的修复，全清 chunk 仍只有 35%，2307 个 chunk 连续全清的概率约等于 0。

现有代码在同一个函数里的三种处理不自洽：

| 情况 | 现有行为 |
|---|---|
| entity 没有 name | 静默丢弃（`return None`） |
| entity **完全没有** evidence | **接受**（`resolve_evidence` 返回 `""`） |
| entity 的 evidence 多了一个 `……` | **杀死整个 run** |

没引用直接放行，引用得稍微不准就全灭。

### 根因 C · 宽松匹配字符类不完整

`_LOOSE_IN_EVIDENCE`（`extraction.py:57`）是 `[\s“”‘’「」『』\"']`，剥掉了空白和各种引号，但没剥省略号 `…` 与方头括号 `【】`。而：

- prompt 要求「证据不超过 60 个汉字」，模型按中文惯例用 `……` 标注截断；
- 本书大量用 `【】` 表示 AI／系统消息，模型会补齐右括号。

```
模型: 特工会使用一具动物义体，潜入敌营之中……
分解: '特工会使用一具动物义体，潜入敌营之中' ⟂ '……'      ← 只多了个省略号

模型: 秘书官分析：【而且他的颅内只有一块驱动芯片——他练不成任何内家武学】
分解: '秘书官分析：【而且...他练不成任何内家武学' ⟂ '】'   ← 只多了个右括号
```

字符类变体实测（只统计能修好多少条失败）：`…` 单独 4/16、`【】` 单独 5/16、两者合计 9/16、再加破折号 `—` 仍是 9/16。**所以只加 `…【】`。**

剩余失败以模型**把代词换成人名**为主（`他`→`亚宁平`、`他`→`男人`），这类是真改写，**不放宽**——放宽等于伪造出处。

## 不重新讨论的事

- **三条都做。** 根因 A、C 是纯技术修复；根因 B 已确认要做。
- **根因 B 改变派生物语义契约**：图谱从「要么完整要么没有」变成「完整但可能少约 7% 条目」。这不违反项目三条不变量——留下的每一条仍能回落到原文精确子串（不变量 2 保持）；不变量 1 的 fail closed 针对的是**派生物与源文档不一致**，模型引错不属于这个范畴，派生物仍是真实但不完整的子集。
- **丢弃必须计数并落盘上报**，不能静默；否则「模型没抽到」与「引用被拒」无法区分。
- **不放宽代词替换类失败。**
- **不改 prompt。** analysis 的 prompt 已经写了「不要改写、不要拼接、不要加省略号」仍然无效，提示层收益低；且改 prompt 要 bump `*_PROMPT_VERSION`。
- **「放宽 chunk 归属」不用做——已经实现了。** `analysis._resolve_evidence`（`analysis.py:398-409`）在 claimed chunk 匹配不上时已会遍历本章所有 chunk。`docs/mvp-runbook.md` 旧版把它列为待办，那是误判。

## 三项改动

### 改动 1 · 关闭思考模式

`src/readfellow/models.py` — `OllamaGenerateRequest` 增加 `think: bool = False`。

`ReadFellowModel` 是 `extra="forbid"`，所以必须显式加字段，不能靠透传。硬编码为 `False`，不进 `config.yaml`、不进 `DerivationSettings`（没有让它可配的需求）。

验证：20 chunk 冒烟 JSON 失败为 0。

### 改动 2 · 单条 evidence 失败改为丢弃该条目

**专用异常。** `extraction.py` 增加 `class EvidenceNotFound(ValueError)`，`resolve_evidence` 改抛它。用专用类型是为了和 pydantic 的 `ValidationError`（也是 `ValueError` 子类）区分开，避免把模型校验失败一并吞掉。

**捕获点。** 只在四个 `_parse_*` 的调用处捕获 `EvidenceNotFound` 并丢弃该条目：

- `graph._parse_entity`（`graph.py:453`）、`graph._parse_relation`（`graph.py:484`）
- `analysis._parse_character`（`analysis.py:357`）、`analysis._parse_event`（`analysis.py:377`）

**文档级失败仍然抛出并触发重试**：JSON 解析失败、`summary` 缺失。只有条目级的证据锚定失败降级为丢弃。这条边界要在代码注释里写清楚。

**空 evidence 的处理保持现状，两条管线各自不变**：graph 接受（实体仍有 `mentions` 指向 chunk，仍然可锚定）；analysis 丢弃（它的 chunk 归属是**从 evidence 匹配结果反推**的，没有 evidence 就没有 chunk 可归，条目根本构造不出来）。这不是不一致，是两边锚定方式不同。

**计数与落盘。**

- `GraphExtraction` 增加 `rejected_count: int = 0`，由 `parse_graph_extraction` 统计
- `GraphExtractionRecord` 增加 `rejected_count: int = 0`，由 `_mark_extracted` 写入
- `KnowledgeGraph` 增加 `rejected_count: int = 0`，在 `finalize_graph` 里按 `extractions` 求和（与现有 `entity_count` / `relation_count` 同一模式）
- `ChapterAnalysis` 增加 `rejected_count: int = 0`，由 `parse_chapter_analysis` 统计
- `AnalysisDocument` 增加 `rejected_count: int = 0`，在 `finalize_analysis` 里求和

**事件与 CLI 上报。** `GraphBuildEvent` / `AnalysisBuildEvent` 增加 `rejected_count: int = 0`，`app` 在 `extracted` / `analyzed` 阶段带上，`cli` 打印成 `entities=12, relations=18, rejected=2`。`rejected=0` 时不打印，避免噪音。

**版本号。** 落盘结构变了，按 CLAUDE.md 的规矩：`GRAPH_SCHEMA_VERSION` 2 → 3、`ANALYSIS_SCHEMA_VERSION` 1 → 2。已有 `graph.json` / `analysis.json` 会被判 stale 全量重建——它们本来就已失效，无额外代价。`GRAPH_PROMPT_VERSION` / `ANALYSIS_PROMPT_VERSION` 不动（prompt 没改）。

`DerivationSettings` 的落盘字段名一个都不动。

### 改动 3 · 补全宽松匹配字符类

`extraction.py:57` — `_LOOSE_IN_EVIDENCE` 加 `【】…`，并更新上方注释说明新增的两类（省略截断标记、方头括号）。

不会引入误匹配：省略号被从**两侧**同时剥除，模型若真的省略了中间内容，被省掉的原文仍在 haystack 里，照样匹配不上——只有装饰性的首尾省略号会被消化。`locate_evidence` 按偏移回读原文，落盘的仍是原文自己的措辞（含原有的 `……` 与换行），provenance 无损。

## 受影响的测试

4 个用例编码了旧契约，必须改写：

| 位置 | 现有断言 | 改成 |
|---|---|---|
| `tests/test_graph.py:149` | `pytest.raises` entity evidence | 该 entity 被丢弃，`rejected_count == 1` |
| `tests/test_graph.py:187` | `pytest.raises` relation evidence | 该 relation 被丢弃，`rejected_count == 1` |
| `tests/test_app.py:262` | 重试 2 次后取到好 evidence（`len(prompts) == 2`） | 不再重试，坏 relation 直接丢弃，`len(prompts) == 1` |
| `tests/test_analysis.py:92` | `pytest.raises(RuntimeError)` | 该 character 被丢弃，章节仍产出 |

`tests/test_graph.py:166`（宽松引用回读原文）保持不变，它验证的正是 provenance 不受损。

按 CLAUDE.md「不要为健壮性过度加测试」：**不新增用例**，只改写这 4 个。字符类的两个新字符不单独补测——`test_graph.py:166` 已覆盖「宽松匹配后回读原文」这条语义。

## 验收

```sh
uv run pytest                                   # 40 passed（4 个改写，不增不减）
uvx ruff format . && uvx ruff check .           # 两者 clean
uv run readfellow index corpus/samples/赛博英雄传.txt --collection smoke --rebuild --limit 20
uv run readfellow graph-index --collection smoke     # 期望：跑完 20 个 chunk，0 次 abort
uv run readfellow analyze     --collection smoke     # 期望：跑完，0 次 abort
```

成功判据：

1. `graph-index` 与 `analyze` 在 20 chunk 上**不再中途终止**
2. 输出里出现 `rejected=N`，且 `graph.json` / `analysis.json` 里有 `rejected_count`
3. 丢弃率约 7%（实测基线：373 条 quote 里 27 条，7.2%）——显著高于这个数说明改动引入了新问题
4. 全程无 JSON 解析失败

过了之后再跑全量（`index` 约 12 分钟；`graph-index` 约 7 小时、`analyze` 约 4.5 小时，见 runbook）。

## 刻意不做

- **不改 prompt**，理由见「不重新讨论的事」。
- **不调 `num_ctx` / `num_predict`。** 超预算章节只占 0.2%；JSON 失败的根因是思考模式而非预算不足。
- **不让 `think` 可配。** 没有需要打开思考模式的场景，加配置项是投机的灵活性。
- **不动 `_resolve_evidence` 的跨 chunk 回退逻辑**，它已经工作正常。
- **不处理代词替换类失败。** 放宽会伪造出处；这部分条目按设计就该被丢弃。
- **不做索引的原子发布。** 已知薄弱点，与本次阻塞无关（见 `docs/architecture-archive.md`）。
