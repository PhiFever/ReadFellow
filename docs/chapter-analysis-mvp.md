# 章节级分析 MVP 设计归档

日期：2026-07-26
状态：**设计已确认，尚未实施**（本文档写于动手之前）

本文档归档一次 grilling 会话的结论：为 ReadFellow 增加「章节级分析」能力的最小可用实现。范围限定为 `corpus/samples/赛博英雄传.txt` 的前几章，不跑全本，验收标准是切分后的单节/多节测试。

注意：这**不是**路线图里的优先级 4（hybrid retrieval）。判断依据是「不必完整跑完全本」只对按单元调 LLM 的路径才有意义，而 hybrid retrieval 不调 LLM、跑全本也只要几秒。优先级 4 仍然挂起。

## 调研事实

以下事实来自本次会话对代码和语料的实测，是后续所有决策的依据。

### 语料

`corpus/samples/赛博英雄传.txt`，176083 行，3909747 字符。

按 `chunking.CHAPTER_RE` 实际匹配 **1210 章**（用更窄的 `第X[章节卷回]` 只匹配到 1138，差额来自 `部/篇/序章/楔子` 等）。

章长度分布：

| 统计量 | 字符数 |
| --- | ---: |
| min | 24 |
| median | 2421 |
| mean | 3435 |
| p90 | 3721 |
| p99 | 12938 |
| max | 35019 |

超长章占比：>6000 字符 63 章（5.2%），>8000 字符 52 章（4.3%），>12000 字符 23 章（1.9%），>18000 字符 2 章（0.2%）。

前 10 章均在 3500-4200 字符之间 —— **MVP 验收范围内不会碰到超长章**。

`chunk_chars=2400`，所以平均 1.7 chunk/章，**跨章是常态而非例外**。

### 代码

- `chunking.py:164-169` — 装窗只看 `target_chars`，从不在章节标题处断开。
- `chunking.py:148-150` — 跨章 chunk 的 `chapter` 取窗口内**最后一个**非空章节名，即前半段实际属于上一章却被标成下一章。
- `progress.py:9` — `chapter_boundaries(source)` 已存在，章节→行号映射现成。
- `models.py:133` — `ChapterBoundary(index, title, line_start)` 已存在。
- `app.py:163-165, 196` — `index --limit N` 直接截断 chunk 列表，manifest 写 `chunk_count=N`、chunks.jsonl 只写 N 条。**截断后的索引在内部看起来完整，没有任何字段记录它是前缀。**
- `app.py:499-524` — `build_graph` 重试耗尽直接 `raise RuntimeError` 中断；但**每处理完一个单元就 `write_graph` 落盘**，所以中断可续。
- `app.py:626` — `_validate_chunk_metadata_source` 只校验 source path 与源文件当前 hash，**不校验 chunker 行为**。
- `graph.py:445` — `_parse_json_object`，LLM JSON 容错解析，analysis 需要复用。
- `graph.py:464-535` — evidence 子串校验藏在 `_parse_entity` / `_parse_relation` 里，analysis 需要复用。
- `graph.py:617` — `_filter_entity` 在有进度限制时清空 `aliases`/`types`，理由是它们跨 chunk 聚合、无逐值 provenance。**summary 属于同一类，照此办理。**
- `ollama.py:113-116` — `OllamaGenerateOptions` 只设 `temperature`/`num_predict`/`repeat_penalty`，**`num_ctx` 从未设置**。Ollama 默认 `num_ctx` 为 4096 tokens，且 prompt 超限时**静默从左侧截断，不报错**。

按 qwen 中文约 1.5 字符/token 估算：现有 graph 路径 2400 字符 chunk ≈ 1600 tokens，加 `num_predict=4096` 已达 5700 > 4096，**现有 graph 抽取很可能已经在踩这个坑**。

### 现有产物

`metadata/sample/` 与 `indexes/sample/` 是 2026-07-05 建的全本索引：1952 chunks、4096 维、`qwen3-embedding:8b`。

`metadata/sample/graph.json` 是旧结构（45 实体 / 31 关系，缺 `prompt_version` 与 `source_chunk_hashes`），按现行 `graph_staleness_reason` 必然判定为 stale。

## 决策

### 1. 产物形态：章节级分析

新增 `analyze` 路径，对指定章节范围产出结构化分析，每条结论带 provenance。

### 2. 章节边界：改 chunker，章内硬断

在 `chunk_document` 里，当遇到属于新章节的 unit 时强制 `emit()` 且**不向下一章带 overlap**：

```python
for unit in units:
    if window and unit.chapter != window_chapter:
        emit(carry_overlap=False)   # 新增：章节边界硬断
    elif window and window_chars + len(unit.text) > target_chars:
        emit(carry_overlap=True)
    window.append(unit)
```

理由：章节级分析要求章边界精确，否则分析第一章时 chunk 里混着第二章开头，直接违反不变量 3。改后 `chapter` 字段变精确，章节→chunks 就是 group by。

代价：chunk id 全部漂移，旧索引必须 `--rebuild`。MVP 只索引切片到新 collection，代价为零。

### 3. 输出 schema：梗概 + 人物 + 事件

```json
{
  "chapter": "第一章 生锈的智人",
  "chunk_ids": ["a1b2c3d4e5f6_000000", "..._000001"],
  "summary": "...",
  "characters": [
    {"name": "尤基", "role_in_chapter": "...",
     "chunk_id": "..._000000", "evidence": "<chunk 原文子串>"}
  ],
  "events": [
    {"order": 1, "description": "...",
     "chunk_id": "..._000000", "evidence": "<chunk 原文子串>"}
  ]
}
```

关键的非对称性：**`summary` 是生成的新文本，无法做子串校验**，只锚 chunk id 范围；`characters` / `events` 每条必须带精确子串 evidence，复用 graph 的校验+重试。

### 4. LLM 调用粒度：整章一次

一章 = 一次 `generate`，prompt 内按 chunk 分块并标注 chunk_id：

```
[chunk a1b2c3_000000] “哐当！……”伴随着一连串……
[chunk a1b2c3_000001] 尤基的亲生父亲在好多年前……   ← overlap 前缀已裁
```

理由：`events` 顺序与 `summary` 需要全章视野；按 chunk 分块呈现让 LLM 抄出的 evidence 天然是某个 chunk 的子串，chunk_id 也不用猜。裁掉的是 overlap **前缀**，evidence 仍是原 `chunk.text` 的子串，校验不受影响。

### 5. 代码归属：新模块 + 新命令

- 新增 `analysis.py`：prompt / 解析 / 章分组 / 完整性判定 / 合并 / staleness / 进度过滤。**不接触网络**，generator 由 `app` 注入（沿用 `GraphGenerator` Protocol）。
- 新增 `app.build_analysis`。
- 新增 CLI `analyze`。
- 产物 `metadata/<collection>/analysis.json`，与 `graph.json` 并列。
- `graph.py` 只把 `_parse_json_object` 和 evidence 子串校验改成公开名，**其余一行不动**。

理由：chunk 粒度与章粒度的 staleness 判定若塞进同一个 json 会纠缠，且无法只重建其中一个。但复制解析/校验逻辑会让两处容错行为逐渐飘移，所以只提取真正双方都调用的那两处。

### 6. 失效链：与 graph 同构 + 断点续建

```
analysis_staleness_reason(analysis, chunks, ...):
  schema_version != ANALYSIS_SCHEMA_VERSION      → stale
  prompt_version != ANALYSIS_PROMPT_VERSION      → stale
  llm_model / extraction_settings 变化            → stale
  已处理章覆盖的某 chunk 不存在 / text_hash 变    → stale
  否则 → 仅对 processed_chapters 之外的章跑 LLM
```

存储结构：扁平的 `chunk_id → (source_hash, text_hash)` 指纹 dict + `processed_chapters` 列表。章的稳定标识用 `chapter_boundaries` 的 `index` + `title` 组合。

断点续建是「不必跑完全本」的直接要求：先跑第 1 章、再跑第 1-3 章时只应新跑第 2、3 章。

### 7. 防剧透：两级过滤

- `characters` / `events`：逐条按其 `chunk_id` 对应 chunk 的 `line_end` / `chunk_index` 过滤，走 `ProgressFilter.allows()`。
- `summary`：仅当该章覆盖的**全部** chunk 都在进度内才输出，否则整段抑制（章仍列出并标注不可用）。

```
进度：--max-line 300（落在第二章中间）

第一章 生锈的智人   summary ✓  characters 3  events 4
第二章 铜糖与…      summary ✗ (超出阅读进度)
                    characters 1  events 1   ← 仅进度内 chunk
```

### 8. 范围选择：复用 `--max-chapter`

不加新参数。`--max-chapter 1` 是单节测试，`--max-chapter 3` 是多节测试。

已知取舍：`--max-chapter` 在此一词二义（构建时是「分析范围」，读取时是「读者进度」）。`graph-index`/`graph-query` 已经是这个模式，至少一致。无法「只重跑第 5 章」，那是投机性功能，不做。

### 9. 章完整性判定：看后继章是否存在

```python
visible_chunks = [c for c in chunks if progress.allows(c)]
# 章 K 完整 ⟺ 存在 chunk.chapter == 章 K+1
# 不完整 → 不跑 LLM / 不输出 summary，仅标注 incomplete
```

因为章边界硬断后，章 K 的 chunk 全部排在章 K+1 的第一个 chunk 之前，这个判定成立且极简。

它同时解决两件事：`index --limit N` 在章中间截断（见调研事实：截断后的索引看起来完整），以及 `--max-line` 切在章中间。零新字段、零 manifest 改动。

### 10. 读取入口：不加命令

`analyze` 重跑即读取。因为有断点续建 + staleness 判定，第二次跑时全部 up-to-date、一次 LLM 都不调；而进度过滤是在**打印时**应用的，所以同一份 `analysis.json` 配不同 `--max-line` 会打印不同内容。

`graph` 拆成 index/query 是因为它有按实体名检索的需求，analysis 只是按章顺序列出，不对称是合理的。

### 11. 现有索引：新 collection + chunker 版本防护

MVP 用新 collection（`ch5`），`sample` 一行不碰。

同时给 `IndexManifest` 加 `chunker_version` 字段（`extra="allow"`，旧 manifest 读为默认值），analysis / graph 路径比对不上就报错要求重建。

理由：改 chunker 会让所有现存索引静默失效，而 `_validate_chunk_metadata_source` 只校验源文件 hash（源文件根本没变），一路放行。manifest 虽存了 `chunk_chars`/`overlap_chars`，但这次改的是**行为**不是参数。这个地雷是本次改动造成的，属于该收尾的 orphan。

### 12. num_ctx：显式设置 + 超长章 fail closed

```yaml
ollama:
  num_ctx: 16384          # 新增
```

```
budget = num_ctx - num_predict - 模板开销
if 章字符数 > budget * 1.5:
    该章 skip，记 reason="chapter too long for num_ctx"
```

`num_ctx` 加在 `OllamaGenerateOptions` 上，graph 路径共用同一个 `OllamaGenerator`，会一起生效 —— 这是复用同一个类的必然结果，不是顺手改进。副作用：`extraction_settings` 变化会触发现有 `graph.json` 重建（它本来就已 stale）。

16384 覆盖 98% 的章。本 MVP 前 10 章 3500-4200 字符，零风险。

## 改动范围

```
chunking.py    章边界硬断（~6 行）
models.py      ChapterAnalysis / CharacterMention / ChapterEvent / AnalysisDocument
               IndexManifest 加 chunker_version；OllamaGenerateOptions 加 num_ctx
analysis.py    新增：prompt / 解析 / 章分组 / 完整性判定 / 合并 / staleness / 进度过滤
app.py         新增 build_analysis；index_document 写 chunker_version
graph.py       只把 _parse_json_object 和 evidence 子串校验改成公开名
cli.py         analyze 子命令 + 输出格式化
config.py      ollama.num_ctx；analysis 段（num_predict / retries）
config.yaml    对应默认值
tests/test_analysis.py  5 个用例
```

约定的默认值：

- `ANALYSIS_SCHEMA_VERSION = 1`
- `ANALYSIS_PROMPT_VERSION = "chapter-analysis-v1"`
- CLI 输出复用现有 Evidence 风格：`source_path:line_start-line_end` + 章节名
- 分析路径同样走 `_validate_chunk_metadata_source`

## 验收标准

### 离线用例（`tests/test_analysis.py`，合成 fixture + DeterministicGenerator）

pytest **必须用合成 fixture**：`.gitignore` 里有 `corpus/samples`，语料不在版本控制内，用真实小说切片当 fixture 会让测试依赖本地文件。

1. chunker 不跨章：合成两章文本 → 无 chunk 同时含两章内容
2. evidence 非子串 → 触发重试 → 耗尽则 `RuntimeError`
3. 断点续建：先 `--max-chapter 1` 再 `--max-chapter 2`，仅第 2 章调 generator
4. staleness：改 `prompt_version` / 改 `text_hash` → 整份重建
5. 进度过滤：`--max-line` 切在章中间 → summary 抑制、events 只剩进度内的

### 手工验收（真实 Ollama，不进 pytest）

```sh
uv run readfellow index corpus/samples/赛博英雄传.txt \
    --collection ch5 --limit 12 --rebuild
uv run readfellow analyze --collection ch5 --max-chapter 3
```

`--limit 12` 覆盖前 5-6 章，保证第 3 章有后继章（见决策 9）。

## 主要风险

**整章一次调用 + 三段嵌套 schema + evidence 必须精确子串，对 `qwen3:8b` 是不小的压力。** graph 路径只要求扁平的实体/关系列表就已经需要重试兜底，analysis 的 schema 更复杂，首次成功率大概率更低。手工验收就是用来暴露这一点的；如果失败率过高，退路是决策 4 改为按 chunk 抽取 + 章级摘要两轮 pass。

## 执行顺序

1. chunker 硬断 + 用例 1 → 验证：合成两章，无 chunk 同时含两章内容
2. models + config + num_ctx → 验证：`uv run pytest` 现有用例不回归
3. `analysis.py` + `app.build_analysis` → 验证：用例 2/3/4
4. CLI `analyze` + 进度过滤 → 验证：用例 5
5. `uvx ruff format . && uvx ruff check .` → 验证：clean
6. 真实 Ollama 跑前 3 章 → 人工检查输出质量与 evidence 命中率
