# 全量 MVP 执行 Runbook

日期：2026-07-27
状态：**检索链路可全量跑通；两条 LLM 派生链路（`graph-index` / `analyze`）存在确定性阻塞，全量跑不完**

本文档记录对 `corpus/samples/赛博英雄传.txt` 做一次完整分析的执行步骤、实测成本基线，以及当前挡住全量运行的问题。所有数字来自 2026-07-27 在本机（Ollama + `qwen3:8b` + `qwen3-embedding:8b`）的实测，不是估算。

## 结论前置

| 环节 | 状态 | 全量成本（实测外推） |
|---|---|---|
| `index` | ✅ 可全量跑 | 约 12 分钟 |
| `search` / `fts` / `hybrid` / `fetch` | ✅ 可用 | 秒级 |
| `graph-index` | ❌ 中途确定性终止 | 若能跑完约 7 小时 |
| `analyze` | ❌ 中途确定性终止 | 若能跑完约 4.5 小时 |

**代码质量本身不是阻塞**：`uv run pytest` 40 passed（约 2.4s，全离线），`uvx ruff check` / `ruff format --check` 均 clean。挡路的是模型输出与 evidence 校验规则的配合。

## 实测基线

### 语料与切分

```
源文档     corpus/samples/赛博英雄传.txt
字符数     4,085,830（Python 文本模式读入，\n 口径）
行数       176,083
章节标题   1,210（CHAPTER_RE 实际匹配数）
```

按当前 `CHUNKER_VERSION = 2`、`chunk_chars=2400`、`overlap_chars=240` 实跑 `chunk_document`（纯本地，0.3 秒）：

```
chunks         2,307
章节分组       1,208
可分析完整章   1,207   （最后一组永不分析，无法与被 --limit 截断的章区分）
每章字符预算   17,532  = (16384 − 4096 − 600) × 1.5
超预算被跳过   2 / 1,207 （0.2%）
```

超长的两章是「第四卷的长度比前面三卷的哪一卷都更长」（30,905 字符）和「第一百七十五章 第二例飞升」（19,152 字符）。0.2% 的跳过率说明 `num_ctx=16384` 对这本书是合适的，不需要调。

章体积中位数 2,566 / 均值 3,431 / 最大 30,905 字符。

### 吞吐

| 环节 | 实测 | 全量外推 |
|---|---|---|
| 切块 | 2,307 chunks / 0.3s | 可忽略 |
| `index` | **3.30 chunks/s**（12 chunks / 9.5s，含维度探测与 optimize） | 2,307 chunks ≈ **12 分钟** |
| `graph-index` | 约 11s / chunk（含失败重试） | 2,307 chunks ≈ **7 小时** |
| `analyze` | 约 13s / 章 | 1,207 章 ≈ **4.5 小时** |

`graph-index` 关闭思考模式后每次生成省约 20% token（`eval_count` 1182–1211 → 841–1012），全量耗时应相应下降，待改动落地后重测。

`index` 比 `docs/architecture-archive.md` 的旧估算表快得多——那张表把 embedding 和生成混在一起估了。**embedding 不是瓶颈，生成才是。**

### 已有产物的状态

- `metadata/sample/` 是 **2026-07-05 的旧产物，已失效**：`manifest.json` 没有 `chunker_version` 字段（即 0），当前是 2，`_validate_chunk_metadata_source` 会 fail closed 并要求 `index --rebuild`。chunk 数也从 1,952 变成 2,307，印证切分逻辑确实变过。
- `metadata/sample/graph-index.log` 停在 `[1/1952]`，那次全量 graph-index 只处理了 1 个 chunk。现存 `graph.json` 无论如何都会被判 stale 整图重建。
- `metadata/ch5/`（12 chunks）是当前 chunker 版本下的小规模验收产物，有效。

**端到端此前只在 12 个 chunk 的规模上验证过。**

## 执行步骤

### 阶段 0 · 前置检查

```sh
curl -s http://127.0.0.1:11434/api/tags     # 需返回模型列表；503 表示 ollama serve 没起
uv run pytest -q                            # 期望 40 passed
```

### 阶段 1 · 全量索引（约 12 分钟，可全量跑）

```sh
uv run readfellow index corpus/samples/赛博英雄传.txt --collection sample --rebuild
```

必须带 `--rebuild`：旧 `sample` 集合是 chunker v0 的产物，且 chunk 数会从 1,952 变成 2,307，不重建会留下旧 chunk 残骸。

预期尾部输出：

```
done: collection=sample, chunks=2307, inserted=2307, skipped=0
```

**不要加 `--no-optimize`**，否则重开集合后中文 FTS 可能查不到。

验收（不依赖生成模型，秒级）：

```sh
uv run readfellow search "尤基的父亲" --collection sample --top-k 3
uv run readfellow fts    "回收站镇"   --collection sample --top-k 3
uv run readfellow hybrid "基因税"     --collection sample --top-k 3
uv run readfellow fetch  <上面返回的 chunk-id> --collection sample --max-chapter 1
```

最后一条应对越界 chunk 返回 `outside_progress` 且**不打印原文**——这是防剧透不变量的直接验收点。

### 阶段 2 · 派生管线（当前受阻，见下节）

原计划：

```sh
uv run readfellow analyze     --collection sample --max-chapter 50
uv run readfellow graph-index --collection sample --max-chapter 50
```

两者都逐单元落盘、支持断点续建，因此**本应**可以分批推进、随时 Ctrl-C。实际会在遇到第一个"硬骨头"单元时抛异常终止，且重跑仍卡在同一处。

## 阻塞详情

### 现象

`graph-index`，`--retries 6`：

```
[    1/8] extracting graph from bdd935754e17_000000
        entities=0, relations=0
[    2/8] extracting graph from bdd935754e17_000001
        retry 1/6: Expecting ',' delimiter: line 1 column 3218 (char 3217)
        retry 2/6: entity evidence is not an exact substring of chunk bdd935754e17_000001
        retry 3/6: entity evidence is not an exact substring of chunk bdd935754e17_000001
        retry 4/6: entity evidence is not an exact substring of chunk bdd935754e17_000001
        retry 5/6: entity evidence is not an exact substring of chunk bdd935754e17_000001
        retry 6/6: entity evidence is not an exact substring of chunk bdd935754e17_000001
error: failed to extract graph for chunk bdd935754e17_000001
```

`analyze`，同一份语料：

```
[    1/3] analyzing
        characters=1, events=1
[    2/3] analyzing 第一章 生锈的智人
        retry 1/2: character evidence is not an exact substring of chunk bdd935754e17_000001
        retry 2/2: event evidence is not an exact substring of chunk bdd935754e17_000002
error: failed to analyze chapter 第一章 生锈的智人
```

### 根因（2026-07-27 实测排查，结论见 `docs/derivation-hardening-plan.md`）

三个独立根因叠加，**不是单一问题**：

1. **一条 quote 失败杀死整个 run**（量级最大）。每 chunk 约 18.7 条 quote，单条失败率 7.2–9.4%，全清 chunk 只有 35%。`resolve_evidence` 抛异常 → 重试整块 → 耗尽后终止整个 run。所以不是"某些 chunk 是硬骨头"，而是几乎每个 chunk 都有至少一条坏 quote，一条就够全灭；2,307 个 chunk 连续全清的概率约等于 0。

2. **宽松匹配字符类漏了 `…` 与 `【】`**。prompt 限制证据长度，模型按中文惯例用 `……` 标注截断；本书大量用 `【】` 表示 AI／系统消息，模型会补齐右括号。补上这两类可修好 9/16 条失败。剩余失败以模型把代词换成人名（`他`→`亚宁平`）为主，那是真改写，不应放宽。

3. **JSON 收尾丢失**。原以为是 `num_predict` 截断，**实测证伪**：4 个失败样本补一个 `}` 后全部可解析，各含 12–13 个实体、13–19 条关系，内容完整。真实原因是 qwen3 思考模式在 Ollama 里默认开启（`OllamaGenerateRequest` 没有 `think` 字段），模型答完后不发收尾大括号而是空转到 `num_predict` 耗尽。传 `think: false` 后 20/20 全部成功。

### 关键判断：重试无效

重试只换一次采样，不改 prompt 也不放宽校验。而失败源于**每条 quote 的独立小概率出错叠加到每 chunk 约 18.7 条**，不是某几个 chunk 特别难——所以人工反复重启同样没用。

另外 chunk 0 抽出 `entities=0, relations=0`：那一段是小说开头，人物地点密集，抽零个说明 prompt 与 `qwen3:8b` 的配合本身偏弱，不只是校验太严。

### 当前可行的绕行

用进度参数跳过卡住的位置，分段推进；已完成部分因逐次落盘而保留：

```sh
uv run readfellow graph-index --collection sample --max-chunk-index 0
# 卡在 N 号 chunk 时，从 N+1 之后继续需要手工分段
uv run readfellow analyze --collection sample --max-chapter 30
```

代价是图谱/分析不完整，且需要人工盯着。**不适合无人值守的全量跑。**

### 修复计划

三项改动已决策，执行细节（含受影响的 4 个测试与验收判据）见 **`docs/derivation-hardening-plan.md`**：

1. `OllamaGenerateRequest` 增加 `think: bool = False`
2. 单条 evidence 锚定失败改为丢弃该条目并计数上报，不再终止整个 unit
3. `_LOOSE_IN_EVIDENCE` 补上 `【】…`

注意：早先列过的「放宽 chunk 归属」**不需要做**——`analysis._resolve_evidence`（`analysis.py:398-409`）在 claimed chunk 匹配不上时已会遍历本章所有 chunk，报错里的 chunk id 只是回退失败后沿用的 claimed 值。

## 刻意没做

- **没有改任何代码**。本轮只做了可行性实测与文档。
- **没有跑全量 `index`**。12 分钟的成本不高，但阶段 2 受阻时先跑完它没有意义，留给决策之后。
- **没有清理 `metadata/sample/` 的旧产物**。它会被 `--rebuild` 覆盖，且当前 fail closed 行为正确，留着能验证 staleness 检查。
- **没有调 `num_ctx` / `num_predict`**。超预算章节只有 0.2%，JSON 截断只是三个失败模式里最次要的一个，调它救不了主因。

## 附：冒烟用的一次性集合

实测用的 `smoke` 集合（12 chunks）可以随时重建或删除：

```sh
uv run readfellow index corpus/samples/赛博英雄传.txt --collection smoke --rebuild --limit 12
rm -rf indexes/smoke metadata/smoke     # 不再需要时
```
