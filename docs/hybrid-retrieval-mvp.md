# Hybrid Retrieval MVP 设计归档

日期：2026-07-26
状态：**待实施**（决策部分写于动手之前）

本文档归档一次 grilling 会话的结论：实现 `docs/architecture-archive.md` 路线图的**优先级 4 —— hybrid retrieval**。目标是把现有的 vector / FTS / graph 三路检索按 chunk id 合并后统一排序，返回单一的 evidence 列表。

不在范围内：重型 GraphRAG、二次 rerank（cross-encoder / LLM rerank）、评估集（那是优先级 5）、MCP 暴露。

## 调研事实

以下事实来自本次会话对代码和现有 `ch5` collection 的实测，是后续所有决策的依据。

### 分数量纲不可比

对 `ch5` collection 实测：

| 通道 | 查询 | 分数区间 | 性质 |
| --- | --- | --- | --- |
| vector | 尤基的母亲为什么卖掉身体 | 0.458 – 0.544（5 条） | 余弦相似度，有界，区间窄 |
| FTS | 铜糖 | 0.972 – 1.945（4 条） | BM25 类，无上界 |
| graph | — | 无分数 | `_graph_evidence` 不设 `score` |

### 三路的行为差异

- vector / FTS 走同一个 `coll.query` 和同一份 `output_fields`（`store.py:133-168`），返回字段**逐字相同**。
- graph 路从 `chunks.jsonl` 读 `Chunk` 对象构造 Evidence（`app.py:993-1016`），**不排序、不截断**，按 chunk_index 升序全量返回。
- graph 路独有三个硬失败点：`graph.json` 不存在（`FileNotFoundError`）、图谱 stale（`RuntimeError`）、源文件 hash 变化（`_validate_chunk_metadata_source`）。vector / FTS 一个都不查。
- vector 是稠密检索，**永远填满窗口**；FTS 是稀疏的，无匹配词的 chunk 根本不返回。

### 现有契约

- `Evidence.retrieval_mode` 在 CLI 中**从未被打印**，只在 `app.py` 构造时设置、在 `tests/test_app.py` 断言。扩展其 Literal 不破坏输出契约。
- `--full-text` 开关不存在；`full_text=True` 只在 `fetch` 内部硬编码（`cli.py:272`）。
- 同一进程内对同一 collection 反复 `read_only + mmap` open **已实测无异常**（连开三次）。

## 决策

### 1. 接口形态：新增独立子命令

新增 `hybrid` 子命令 + `app.hybrid_search()`，与 `search` / `fts` 并列，不做成 `search --hybrid`。

依据：`docs/architecture-archive.md:250` 在规划 MCP 工具面时就把 `hybrid_search` 列为独立工具；三路的失败语义与 `search` 不同（见决策 6/7），塞进同一命令会让 `search` 的错误契约变含糊。

不叫 `ask`——该命令只返回排序后的 evidence，不生成答案。

### 2. 融合算法：RRF，k=60

```
fused(chunk) = Σ_channel  1 / (60 + rank_channel)
```

只用名次，不用原始分。依据：

- min-max 归一化在此退化——vector 5 条跨度仅 0.086，归一化后把「五条差不多」硬拉成「第一名压倒性胜出」；z-score 同理（每路只有 top_k 个样本）。
- graph 路没有分数，任何基于分数的方案都要为它造假分。
- RRF 奖励多路一致：只在一路拿第 1 = `1/61 = 0.0164`，两路都拿第 5 = `2/65 = 0.0308`，后者胜出。

k=60 是 Cormack 原论文经验值，也是 Elastic / Weaviate 默认值。**不进 config**——没有评估集之前调参就是拍脑袋。

明确排除：不做二次 rerank。

### 3. graph 路的名次：按关系数排序

在 hybrid 层按 `(len(graph_context.relations), len(graph_context.entities))` 降序、`chunk_index` 升序兜底定名次。**不改 `_graph_evidence`**，`graph-query` 的输出顺序不变。

依据：信号已算好，白拿；直接用 chunk 顺序当名次是系统性偏袒开头，会通过 RRF 污染最终排序。

否决的更简方案：所有 graph 命中并列第 1（退化成布尔加分）。查主角名时全书每个 chunk 都命中，等于给所有候选加同一常数，对排序零贡献。

### 4. 召回深度：每路 `top_k * 10`

模块常量 `FAN_OUT_MULTIPLIER = 10`，融合后返回 `top_k`。不加 config 项、不加 CLI 参数。

依据：RRF 质量直接取决于窗口大小——某 chunk 在 vector 排 2、FTS 排 18，若 FTS 只取 15 条则「两路一致」的信号丢失。10x 对齐 Elasticsearch `rank_window_size` 默认值（100 配 size 10）。成本可忽略：vector 路唯一真实开销是 query embedding，**与 fan-out 无关**（只 embed 一次）。

graph 路按决策 3 排序后同样截断到 `top_k * 10`。

不处理 `top_k=1` 时窗口偏小（10 条）——1% 场景。

### 5. 通道权重：等权

三路权重都是 1.0，不进 config。RRF 的核心卖点是免调参；权重要调应先做优先级 5 的评估集，用数据定。

已知不对称（不需要用权重压制）：vector 永远填满窗口，会把大量弱匹配灌进候选池，但其贡献 `1/(60+40) = 0.010` 永远压不过任意两路一致的最低组合 `2/(60+50) = 0.018`，且最终只返回 top_k。

这个不对称正说明 hybrid 的价值：**FTS 和 graph 是高精度低召回信号，vector 是高召回低精度信号，RRF 让前两者给后者的候选池投票。**

### 6. graph 缺失或 stale：跳过该通道 + 结构化上报

不整体失败。降级信息必须是 `HybridSearchResult` 上的机器可读字段，不能只打 stderr。

核心论证——**「fail closed」不等于「fail the request」**：

不变量 1 要求 stale 派生物**不出现在输出里**，而非「有 stale 派生物就什么都不给」。跳过整条 graph 路恰是最严格的 fail closed：stale 的实体名、关系、别名一个字都进不了 Evidence 和 `graph_context`（防剧透不变量同时保住）。没有猜任何东西。

为什么 `graph-query` 仍应硬失败而 hybrid 不应：`graph-query` 的**全部输出就是图谱数据**，剔掉 stale 部分后一无所剩，「失败请求」与「fail closed」在那里是同一件事。hybrid 里 graph 只是三分之一路，剔掉后 vector+FTS 仍是完全合法、完全有出处的答案。让辅助通道的失效毙掉整个查询，会让 hybrid 比 `search` 更脆弱。

缺失与 stale 性质不同但结论相同：`graph.json` 不存在**不是 stale**，只是可选通道没建——全书 `graph-index` 要 1952 次 LLM 调用，强制要求先付这个代价会让 hybrid 基本作废。

机器可读的必要性：`app.hybrid_search` 将来会被 MCP/agent 调用，只打 stderr 的话 agent 看不见，会误以为「图谱也查过了、没有相关关系」，从而做出比实际更强的断言。

### 7. vector 路故障：整体失败

规则：

> **通道依赖的派生物「没建」或「不可信」→ 跳过并上报；通道本该能跑却抛异常（环境/运行时故障）→ 整个请求失败。**

依据：

- graph.json 不存在是**正常预期状态**；Ollama 连不上是**故障**，十秒可修。
- 风险不对称：graph 掉线后剩 vector+FTS 仍是标准能力；vector 掉线后剩纯关键词匹配是**能力断崖**，语义类问题会给出看似正常、实则严重变差的结果，用户扫一眼难以察觉。
- 不能让更高级的命令反而更会藏错——`search` 遇 Ollama 故障即报错，若 `hybrid` 反倒静默成功，用户会误以为 hybrid 更稳。

实现要求：**先做 query embedding，fail fast**。

FTS 路无独立失败模式（与 vector 读同一 collection，打不开则三路皆无，本就整体失败）。此规则实际只对 vector 生效。

### 8. 结果模型

`models.py`：

```python
class EvidenceMatch(ReadFellowModel):          # 新增，frozen
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["vector", "fts", "graph"]
    rank: int                                   # 该通道内 1-based 名次

class Evidence(ReadFellowModel):
    retrieval_mode: Literal["vector", "fts", "fetch", "graph", "hybrid"]   # 加一个值
    score: float | None = None                  # hybrid 时 = 融合后 RRF 分
    matches: list[EvidenceMatch] = []           # 新增；非 hybrid 时为空
    graph_context: EvidenceGraphContext | None = None   # 不变
```

`app.py`：

```python
@dataclass(frozen=True)
class ChannelStatus:
    mode: Literal["vector", "fts", "graph"]
    candidates: int
    skipped_reason: str | None = None

@dataclass(frozen=True)
class HybridSearchResult:
    progress: ProgressFilter
    channels: list[ChannelStatus]
    evidence: list[Evidence]
```

理由：

- **带 `rank` 而非只记通道名**：融合分是 0.03 量级的抽象数字，本身无法解释排序。排序看着不对时第一个问题必然是「它凭什么排第一」，`matches=[{vector,1},{fts,7}]` 一眼可答。这是可解释性，不是调参旋钮。
- **不记每路原生分**：决策 2 已论证跨通道原生分不可比，记下来无人能用。
- **扩展 `Evidence` 而非新建 `HybridEvidence`**：包一层会让 `print_evidence` 和未来 MCP 的 evidence 处理各自分叉；Evidence 的存在意义就是统一形状（`architecture-archive.md:62` 明确把「四路返回不同形状」列为阻碍 hybrid 的问题）。
- **`HybridSearchResult` 是新类型而非给 `SearchResult` 加 `channels`**：否则 `search`/`fts` 结果里会永远挂一个空列表，是骗人的字段。

### 9. 字段合并规则

同一 chunk 被多路命中时：

1. **`text` 及全部定位字段**（`source_path` / `line_*` / `byte_*` / `chapter` / `text_hash`）取 **zvec 来源**（vector 或 FTS，二者逐字相同）；仅 graph 单独命中时才用 `chunks.jsonl` 的值。
2. **`graph_context`**：graph 命中即带上（并集，非冲突——其他两路无此字段）。
3. **`matches`**：三路并集，按 `mode` 固定顺序（vector, fts, graph）排列，保证输出稳定。
4. **`score`** = 融合 RRF 分；**`retrieval_mode` 一律 `"hybrid"`**，哪怕只有一路命中——使 `matches` 成为唯一的通道来源，无需两字段对账。

第 1 条偏向 zvec 的理由：**collection 才是本次实际搜的东西**，而 `chunks.jsonl` 可能超前（已知薄弱点「索引不是原子发布」会让 metadata 写完但 collection 不完整）。

**刻意不做**：不校验两来源的 `text_hash` 一致性。两份数据由同一次 `index_document` 从同一批 `Chunk` 写出，构造上相同；要使其分歧需手工把两个产物目录搞到不同步，属于「为不可能场景加错误处理」。

### 10. 实现方式：复用现有三个 app 函数

`hybrid_search` 直接调 `semantic_search` / `fts_search` / `query_graph`，自身只做融合。执行顺序 **vector → FTS → graph**。

理由：

- **零重复**。自行调 store 层要把「读 manifest → 建 progress filter → 打开 collection → 构造 Evidence」抄第四遍。
- **决策 6/7 的错误语义白拿**：graph 跳过 = 把 `query_graph` 包进 `try/except (FileNotFoundError, RuntimeError)`；vector 异常直接上抛 = 整体失败。
- **进度过滤自动一致**：三路各自从同一 `ProgressLimit` + 同一 manifest 构造 `ProgressFilter`，结果必然相同；两条既有过滤路径（zvec `expression` / 进程内 `allows()`）在原函数里已各自走对，hybrid 不需碰。
- 重复 open 的安全性已实测排除。

顺序理由：vector 第一是决策 7 的 fail fast；graph 最后因其最重（读 chunks.jsonl + 校验 + 读 graph.json）且最可能被跳过。

返回的 `progress` 取 vector 那一路的（必定运行）。

代价：多读 2 次 manifest（小 JSON）、多开 1 次 collection（read-only + mmap）。本地 CLI 可忽略。

### 11. CLI 输出

扩展现有 `print_evidence`，不写第二个 printer。参数面照抄 `search`（`--collection`、`--top-k` + `add_progress_args`），不加其他。

仅在 `matches` 非空时多打一行：

```
[1] id=bdd935754e17_000003 score=0.032787 corpus/samples/赛博英雄传.txt:228-316
matched: vector#3, fts#1, graph#2
chapter: 第二章 铜糖与戴森原则
graph entities: 约格, 尤基
graph relation: 以诺 --警告--> 约格
铜糖是这个镇子唯一的收入来源……
```

`matches` 对其他三个命令恒为空，其输出**逐字节不变**；另写 printer 则要复制整个函数体（chapter / graph_context / 截断逻辑）。

通道状态打一行到 **stderr**，紧邻现有 `progress limit:`：

```
progress limit: through chapter 3: 第三章 人类文明之敌
channels: vector=50, fts=4, graph=skipped (graph index not found)
```

- 走 stderr：它是查询元信息/诊断，与 `print_progress` 同类；stdout 只留 evidence 本身便于管道处理。
- **正常时也打**：`graph=12` 明确告知图谱确实参与了检索；只在出错时打则用户无法区分「graph 没命中」与「graph 没跑」。
- 数字是各通道**进入融合的候选数**（截断后），非最终 top_k。

## 改动范围

| 文件 | 改动 |
| --- | --- |
| `src/readfellow/models.py` | 新增 `EvidenceMatch`；`Evidence` 加 `matches`、扩 `retrieval_mode` Literal |
| `src/readfellow/app.py` | 新增 `ChannelStatus`、`HybridSearchResult`、`hybrid_search`、RRF 融合函数 |
| `src/readfellow/cli.py` | 新增 `hybrid` 子命令 + handler；`print_evidence` 加 `matched:` 行；通道状态打印 |
| `tests/test_app.py` | 新增 3 个用例（其一 parametrize 两种异常） |
| `docs/architecture-archive.md` | 更新优先级 4 状态与「下一步」指向优先级 5 |

不改：`store.py`、`graph.py`、`chunking.py`、`progress.py`、`analysis.py`、`ollama.py`、`config.py`。

## 验收标准

### 离线用例（`tests/test_app.py`，monkeypatch 低层符号）

低层符号指 `read_manifest` / `open_existing_collection` / `OllamaEmbedder` / `query_vector` / `query_fts`，与既有用例一致——让真实的组合逻辑与融合逻辑跑起来，而非 mock 掉被测对象。

1. `test_hybrid_ranks_multi_channel_agreement_above_a_single_channel_top_hit`
   构造 vector 列表使 chunk A 排 1、chunk B 排 3；FTS 列表使 B 排 3、A 不出现。断言 **B 排在 A 之前**，且 `B.matches == [(vector,3),(fts,3)]`。一个用例同时锁死 RRF 公式、名次记录、多路一致优先。
2. `test_hybrid_skips_the_graph_channel_and_reports_why`
   对**缺失（`FileNotFoundError`）与 stale（`RuntimeError`）两种异常 parametrize**。断言 evidence 仍从另两路正常返回，`channels` 中 graph 标 skipped + 原因。两种异常进同一 except，但一个是「没建」一个是「不可信」，语义不同；将来若有人想把 stale 改成硬失败，会撞到此用例并被迫读决策 6。
3. `test_hybrid_fails_when_the_embedding_service_is_down`
   `FakeEmbedder` 抛异常，断言 hybrid **抛出**而非降级成 FTS-only。锁死决策 7。

**不加**：

- 不在 `test_cli_evidence.py` 加真实 zvec 用例。hybrid 的 vector 路需 embedder，真实 zvec + 假 embedder 组装成本不低，而其覆盖的 zvec 查询路径已被既有 FTS→fetch 用例与 `search`/`fts` 覆盖；新增逻辑全在融合层，该层不碰 zvec。
- 不测进度过滤。三路过滤都在各自原函数内，已有用例覆盖；hybrid 是纯组合，重测即重复。
- 不测字段合并优先级（决策 9）。构造两路返回不同 text 需人为把产物搞到不同步，正是决策 9 判定的不可能场景。

### 手工验收（真实 Ollama，不进 pytest）

对现有 `ch5` collection：

1. 先跑一次 `graph-index --collection ch5`（7 个 chunk），使 graph 通道有内容。
2. 跑 `hybrid`，对照 `search` / `fts` / `graph-query` 三条单路结果，人眼确认：融合排序合理、`matches` 的名次与单路名次对得上、`channels` 行数字正确。
3. 临时移走 `metadata/ch5/graph.json`，确认降级提示正确且 evidence 仍正常返回。
4. 带 `--max-chapter` 跑一次，确认进度过滤未被融合层绕过。

## 主要风险

`ch5` 只有 7 个 chunk，融合效果的观察样本极小——多路一致的场景可能凑不出几个。若手工验收看不出 hybrid 相对单路的差异，这是**语料规模问题而非实现问题**，判定依据应回到离线用例对 RRF 公式的断言，不要因此去调 k 或权重（那需要优先级 5 的评估集）。

## 执行顺序

1. `models.py`：`EvidenceMatch` + `Evidence` 两处改动 → 验证：`uv run pytest` 全绿（既有用例不受影响，新字段有默认值）
2. `app.py`：`ChannelStatus` / `HybridSearchResult` / RRF 融合 / `hybrid_search` → 验证：`uvx pyright` 无新增错误
3. `tests/test_app.py`：3 个离线用例 → 验证：`uv run pytest` 全绿
4. `cli.py`：子命令 + `print_evidence` 扩展 + 通道状态打印 → 验证：`uv run readfellow hybrid --help`；`uv run readfellow search`/`fts` 输出与改动前逐字节一致
5. `uvx ruff format . && uvx ruff check .` → 验证：clean
6. 手工验收（真实 Ollama + `ch5`），按上节四步 → 验证：四步全部符合预期
7. 更新 `docs/architecture-archive.md` 的优先级状态，并在本文档追加「实施结果」
