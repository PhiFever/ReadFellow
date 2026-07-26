# 模块深化计划（Module Deepening Plan）

日期：2026-07-26
基线 commit：`18c5500`（行号均以此为准，改动后会漂移，请以**符号名**为主要定位手段）

本文档是 2026-07-26 架构评审的执行计划。评审读完了全部 3,613 行 src，用 deep module 的词汇（module / interface / depth / seam / adapter / leverage / locality）识别出 6 个候选，本文档确定其中 5 个的实施顺序与范围。

## 不重新讨论的事

以下已有结论，本计划不触碰：

- **重型 GraphRAG** —— `architecture-archive.md` 已定：在评估集证明轻量 hybrid 不够之前不引入。
- **RRF 参数进 config** —— `hybrid-retrieval-mvp.md` 已定：k、权重、召回倍数刻意不进 config。
- **索引非原子发布** —— archive 已记录为已知弱点。这是正确性缺口，不是深化，不在本计划内。
- **候选 6（`FusedEvidence` 拆分）** —— 不做。hybrid MVP 刻意选了单一扁平模型以保证其他命令输出逐字节不变。只有当第二个消费者（MCP、reranker）需要跨通道比较分数时才重开。

## 三条不变量（每个阶段的验收前提）

任何阶段完成后这三条都必须仍然成立，它们优先于本文档的任何设计意图：

1. 源文档是唯一事实依据，派生物 stale 时 fail closed。
2. 检索只是导航，返回给用户的内容必须回落到 chunk 原文 + `source_path:line_start-line_end`。
3. 防剧透优先，超出进度的文本绝不出现在输出里。

## 执行顺序

```
阶段 A（= 候选 3）  拆 graph.py            ← 先做，是 B 的前置
阶段 B（= 候选 1）  合并孪生派生管线        ← 分 B1 / B2 两步
阶段 C（= 候选 2）  补完 zvec seam          ← 可与 A/B 并行，独立收益最大
阶段 D（= 候选 4）  ReadingWindow           ← 第三个派生物出现时再做
阶段 E（= 候选 5）  chunk 字段集去重        ← 只做无歧义的那一半
```

**为什么 A 在 B 之前**：B 要把 `graph.py` 和 `analysis.py` 的公共骨架抽出来，但今天 `analysis.py` 直接 import `graph.py` 的 7 个 helper——两个模块还互相伸手，此时抽骨架等于在纠缠的依赖上做手术。A 做完后两者只依赖下层，B 才有干净的操作面。

---

# 阶段 A · 拆 `graph.py`（候选 3）

`graph.py` 824 行里装了三件事：知识图谱领域、通用 LLM-JSON + 证据锚定工具、`chunks.jsonl` 读取。

## A 的判据已经由现有代码给出

不需要猜 seam 在哪，现状直接证明了：

- `analysis.py:9-17` 从 `graph.py` 导入 7 个符号：`as_list`、`chunk_context`、`get_any`、`locate_evidence`、`normalize_text`、`parse_json_object`、`resolve_evidence`。**这 7 个全部属于通用工具，一个领域符号都没有。**
- 测试只导入 graph 的领域符号（`test_graph.py:7-12` 取 `empty_graph`/`merge_extraction`/`parse_graph_extraction`/`query_graph`；`test_app.py:28-35` 另加 `graph_path`/`read_graph`/`write_graph`）。**没有任何测试导入那 7 个 helper。**

结论：seam 位置由两个真实 adapter（graph 域、analysis 域）确定，不是假设。拆分不会破坏任何测试的 import 语句。

## A.1 新建 `src/readfellow/extraction.py`

从 `graph.py` **原样移入**（不改实现，只搬家）：

| 符号 | 当前行 | 备注 |
| --- | --- | --- |
| `parse_json_object` | 488 | |
| `get_any` | 759 | |
| `as_list` | 768 | |
| `normalize_text` | 784 | |
| `_LOOSE_IN_EVIDENCE` + 其上注释块 | 445-450 | 注释解释了宽松匹配的理由，必须一起搬 |
| `locate_evidence` | 453 | |
| `resolve_evidence` | 471 | |
| `chunk_context` | 715 | |
| `_chunk_text` | 742 | |
| `_chunk_value` | 750 | |
| `_int_value` | 821 | **改名为公开的 `int_value`**，见下 |

`_int_value` 有两类调用方：`chunk_context`（随之搬走）和 `finalize_graph` 的 7 处排序键（留在 graph）。因此它必须公开：在 `extraction.py` 里定义为 `int_value`，`graph.py` 从 extraction 导入。

`extraction.py` 只依赖 `models`（`ChunkContext`、`Chunk`）和标准库，不依赖 `graph`、不依赖 `store`。

## A.2 留在 `graph.py` 的私有 helper

这三个只有 graph 域在用，**不要**顺手搬进 extraction（那会造出没有第二个 adapter 的假 seam）：

- `_as_strings`（9 处调用，全在 graph 域；它调 `normalize_text`，改为从 extraction 导入）
- `_case_key`（6 处，全在 graph 查询/别名索引）
- `_has_same_values`（4 处，全在 graph 合并）

同样留下：`ENTITY_TYPES`、`RELATION_TYPES`、`_TYPE_ALIASES`、`_RELATION_ALIASES`、`_NAME_KEYS` 等键名元组、`_normalize_entity_type`、`_normalize_relation`。

## A.3 `read_chunks` 搬回 `store.py`

`chunks.jsonl` 由 `store.write_manifest`（store.py:107-126）写、由 `graph.read_chunks`（graph.py:106-125）读——同一个文件格式的读写方分散在两个不相干的 module 里。

把 `read_chunks` 移到 `store.py`，紧挨 `write_manifest`。改调用方：

- `app.py:38` 的 `from .graph import (... read_chunks ...)` → 改为从 `.store` 导入
- `analysis.py` 不调用 `read_chunks`，无需改

注意：`graph_path` 仍然用 `metadata_path`，所以 `graph.py` 仍 import `store.metadata_path`。这是对的——"这个派生物存在哪儿"本就是领域模块该知道的事。

## A.4 改 import

- `analysis.py:9-17`：7 个符号的来源从 `.graph` 改为 `.extraction`
- `graph.py`：新增 `from .extraction import (as_list, chunk_context, get_any, int_value, locate_evidence, normalize_text, parse_json_object, resolve_evidence, _chunk_text, _chunk_value)`（`_chunk_text`/`_chunk_value` 若只在 graph 用可考虑一起公开命名）
- `app.py`：`read_chunks` 改从 `.store` 导入，其余不变

**做完后 `analysis.py` 不再 import `graph.py`。** 这是本阶段的核心验收信号。

## A 的验证

```sh
uv run pytest                                   # 全绿，且一行测试代码都不用改
grep -n "from .graph import" src/readfellow/analysis.py   # 应无输出
uvx ruff format . && uvx ruff check .
```

若有任何测试需要修改才能通过，说明搬错了东西——回退重做，不要改测试迁就。

---

# 阶段 B · 合并孪生派生管线（候选 1）

`app.build_graph`（459-640，182 行）和 `app.build_analysis`（643-819，177 行）12 个阶段里有 10 个完全相同。另有 15 对镜像符号：

```
graph_path ↔ analysis_path              read_graph ↔ read_analysis
write_graph ↔ write_analysis            empty_graph ↔ empty_analysis
update_graph_metadata ↔ update_analysis_metadata
processed_chunk_ids ↔ processed_chapter_keys
graph_staleness_reason ↔ analysis_staleness_reason
finalize_graph ↔ finalize_analysis      _filter_entity ↔ filter_chapter
GraphExtractionSettings ↔ AnalysisSettings    （字段完全相同）
GraphConfig ↔ AnalysisConfig                  （字段完全相同）
GraphBuildOptions ↔ AnalysisBuildOptions
GraphBuildEvent ↔ AnalysisBuildEvent
GraphBuildResult ↔ AnalysisBuildResult
build_graph 内联重试循环 ↔ _analyze_chapter
```

## B 分三步做，理由

评审报告画的"一个 `Derivation` 深模块 + 两个 adapter"是终局形态。但直接落地它需要一个约 14 个成员的 `DerivationSpec` Protocol（path/select/read/write/empty/staleness/processed_keys/key_of/label/prompt/parse/merge/update_metadata/counts）——**那本身就是一个宽 interface**，用 interface 的复杂度换掉了实现的重复，depth 并没有真正提升。

所以拆成三步：

| 步骤 | 内容 | 状态 |
| --- | --- | --- |
| B1 | 抽出两条管线逐字相同的逻辑 | 必做 |
| B2 | 合并字段完全相同的 settings / config 模型 | 必做，与 B1 同批 |
| B3 | 完整 `DerivationSpec` Protocol | **待定**，触发条件见下 |

B1+B2 把两条管线各自砍到约 90 行。B3 留到出现第三个派生物时再评估——那时才有第三个 adapter 证明 spec 的每个成员都真的在变。这符合"两个 adapter 才算真 seam"，也避免为可配置性而可配置。

## B1 · 抽出无歧义共享部分

新建 `src/readfellow/derivation.py`，只放两条管线**逐字相同**的逻辑：

1. **`generate_with_retry(generator, prompt, parse, *, retries, on_retry)`**
   现在写了两遍：`app.py:582-608`（内联在 build_graph 里）和 `app.py:822-852`（`_analyze_chapter`）。两者的重试语义完全相同。
   注意 `app.py:582-608` 的 `for/else` 有一处冗余——`break` 之前的分支已经在 `attempt >= retries` 时 raise，`else` 分支实际不可达。合并时收敛成一个写法。

2. **`load_or_reset(path, *, read, empty, staleness_reason, rebuild) -> tuple[D, bool]`**
   即 `app.py:494-520` 与 `app.py:685-706` 的三态加载：文件不存在 / 存在但 rebuild / 存在且 stale → 返回空文档 + `rebuilt=True`；存在且新鲜 → 返回已存文档。
   两处目前有**行为差异**：graph 版在 stale 时置 `rebuilt=True` 并重置；analysis 版逻辑等价但写法不同。合并时以 graph 版为准，并确认 `test_analysis.py:161` 的 rebuild 用例仍绿。

3. **`write_json_document(path, document, finalize)`**
   `write_graph`（graph.py:132-138）与 `write_analysis`（analysis.py:65-72）除了 finalize 函数外逐字相同（`mkdir(parents=True, exist_ok=True)` + `json.dumps(..., ensure_ascii=False, indent=2) + "\n"`）。

4. **`derivation_status(*, selected, pending, rebuilt) -> str`**
   `empty` / `up_to_date` / `built` / `rebuilt` 的判定，现在 graph 版散在三个 return（app.py:538-558、633-640），analysis 版集中在 796-801。语义相同。

5. **`common_staleness_reason(doc, *, collection, source_path, llm_model, settings)`**
   `graph_staleness_reason` 前 5 项检查（graph.py:200-212）与 `analysis_staleness_reason` 前 5 项（analysis.py:290-299）结构完全一致，只有错误消息里的名词不同。抽成带 `label: str` 参数的公共函数，各自的领域检查（chunk fingerprint / chapter 匹配）留在原模块。

## B2 · 模型与配置合并（与 B1 同批）

这部分风险低、收益直接：

- `models.GraphExtractionSettings`（226-232）与 `models.AnalysisSettings`（293-299）字段完全相同（`temperature`/`num_predict`/`num_ctx`/`retries`）→ 合并为 `DerivationSettings`。
  **注意**：`KnowledgeGraph.extraction_settings` 与 `AnalysisDocument.settings` 是已落盘的 JSON 字段名，合并模型类**不要**改这两个字段名，否则已有 `graph.json`/`analysis.json` 会被判 stale 而全量重建。
- `config.GraphConfig`（45-49）与 `config.AnalysisConfig`（52-56）字段相同 → 合并为一个模型类，但 `config.yaml` 保持 `graph:` 和 `analysis:` 两个独立键（它们的值可以不同，这是真实需求）。

`GraphBuildOptions ↔ AnalysisBuildOptions`、`GraphBuildEvent ↔ AnalysisBuildEvent`、`GraphBuildResult ↔ AnalysisBuildResult` **暂不合并**——它们的差异字段（`chunk_id` vs `chapter_title`、`entity_count/relation_count` vs `character_count/event_count`、`chapters`/`skipped`）是真实的领域差异，合并会逼出一个宽松的可选字段袋子，CLI 的 `print_graph_progress` / `print_analysis_progress` 也会失去类型区分。留到 B3 一并评估。

## B3 · 完整 `DerivationSpec` Protocol（待定）

**触发条件：出现第三个派生物时。** 在那之前不做——理由见本阶段开头。届时需要重新确认的是：spec 的 14 个成员里，是否每一个都真的随 adapter 变化；不变的那些应该沉进 `derivation.py` 的实现，而不是留在 interface 上。

## B 的验证

```sh
uv run pytest                     # 全绿
uv run pytest tests/test_analysis.py tests/test_app.py -q   # 重点：断点续建 / stale 重建 / 重试
```

行为等价性检查（这是 B 的真正验收标准）：

- 已有的 `metadata/<collection>/graph.json` 与 `analysis.json` 在改动后**不被判 stale**。跑一次 `graph-query` 和 `analyze`，不应出现 "stale ... run graph-index to rebuild"。
- `build_graph` / `build_analysis` 的 `status` 取值在四种情形下与改动前一致。

---

# 阶段 C · 补完 zvec seam（候选 2）

可与 A/B 并行，互不冲突。这是唯一一个**修复代码库自己已声明却已违反的不变量**的阶段。

## 现状：不变量已破

`CLAUDE.md` 写着「`store.py` —— 唯一接触 zvec 的地方」。实际上：

- `app.py:939-947` `open_existing_collection` 内联 `import zvec`，直接调 `zvec.open(..., zvec.CollectionOption(read_only=True, enable_mmap=True))`
- `app.py:290`/`335`/`337`/`358`/`359` 直接调 `coll.fetch` / `coll.insert` / `coll.update` / `coll.optimize` / `coll.flush`
- `app.py:296-297`、`445`、`1004` 直接读 zvec `Doc.fields`
- `store.py` 的 `query_vector`/`query_fts`/`fetch_chunk` 签名是无类型的 `coll` 位置参数——它不是一个 module 的 interface，是一堆自由函数加一个裸句柄

后果直接体现在测试上：因为没有可替换的 interface，测试只能 monkeypatch `app` 上的 5 个模块级符号（`read_manifest`、`open_existing_collection`、`query_vector`、`query_fts`、`fetch_stored_chunk`，见 `test_app.py:404-407, 462-464, 522-524, 773-776`），而不是注入一个 adapter。

## C 的目标

一个 `ChunkStore` module，interface 约 6 个方法：

```
open(config, collection, *, mode)   # read_only / writable
upsert(chunks, vectors, *, model)   # 吸收 batch 内 fetch 比对 text_hash、insert/update 拆分、状态检查
search_vector(vector, *, top_k, filter)
search_fts(query, *, top_k, filter)
fetch(chunk_id)
commit(*, optimize)                 # optimize() + flush()
```

两个 adapter：`ZvecChunkStore`（生产）、`InMemoryChunkStore`（测试）。两个 adapter 都真实存在 → seam 是真的。

`app.index_document` 里 `coll.fetch` 比对 hash、拆 insert/update、检查 `status.ok()` 这一整段（app.py:288-354）搬进 `ZvecChunkStore.upsert`——这是让 interface 变窄、implementation 变厚的关键一步，也是 depth 的实际来源。

`Doc`、`Status`、`CollectionOption` 不再越过 seam；`app.py` 里 `import zvec` 与所有 `coll.*` 调用清零。

## C 的顺带修复

`store.query_vector`（138-148）与 `store.query_fts`（157-167）的 `output_fields` 是两份逐字相同的 9 元素字面量列表，且必须与 `models.QueryChunkFields`（95-106）保持同步——因为 `QueryChunkFields` 每个字段都有 `""`/`0` 默认值，漏掉字段不会报错，只会让 Evidence 静默丢失 provenance。抽成一个 `STORED_OUTPUT_FIELDS` 常量。（这也是阶段 E 里唯一无歧义的那一半，在这里顺手做掉。）

## C 的验证

```sh
uv run pytest
grep -rn "zvec" src/readfellow/app.py            # 应只剩错误消息里的字符串，无 import、无 coll.*
grep -n "monkeypatch.setattr(app" tests/test_app.py   # 应显著减少
uv run readfellow index corpus/samples/<doc>.txt --collection sample --rebuild --limit 8
uv run readfellow fts "关键词" --collection sample --top-k 5
uv run readfellow fetch <chunk-id> --collection sample
```

`tests/test_cli_evidence.py` 用真实 zvec 跑 optimize→重开→中文 FTS→进度过滤→fetch 这条链路，是 C 的主要安全网，**必须保持用真实 zvec，不要换成 InMemory adapter**。

---

# 阶段 D · ReadingWindow（候选 4）

**触发条件：第三个派生物出现时再做。** 现在做属于为两个调用点建抽象，收益不足以抵消风险。

## 问题

防剧透是项目排第一的不变量，但目前靠每个调用点自己记得两件事：走 `ProgressFilter.expression`（给 zvec）还是 `.allows()`（进程内），以及自己手写"无逐值 provenance 的聚合信息要扣掉"这条规则。同一条规则在 4 处手写：

- `graph.py:677-678` 清空 `aliases` / `types`
- `analysis.py:349` 清空 `summary`
- `app.py:446-451` `fetch_chunk` 越界时不带 `text`
- `search`/`fts` 走 zvec filter 表达式

新入口忘了应用 → 静默泄漏，不报错。

## 方向（不要过度设计）

`ProgressFilter` 深化为 `ReadingWindow`，interface 保持 2 个方法：`visible(chunks)` 和 `redact(obj)`。`redact` 由模型字段上的一个标记驱动（"这个字段是跨 chunk 聚合、无逐值 provenance"），调用点只声明**什么是聚合**，由 module 决定**何时扣**。

**明确不做**：完整的逐值 provenance 系统。archive 已把它列为超范围。

---

# 阶段 E · chunk 字段集去重（候选 5）

**只做无歧义的一半，其余不动。**

chunk 的 provenance 字段集在 4 个模块里手抄了 11 遍（`ZvecChunkFields.from_chunk`、`QueryChunkFields`、`ChunkContext`、`create_schema`、两处 `output_fields`、`_evidence_from_doc`、`_graph_evidence`、`chunk_context` 的两个分支、`Evidence`）。

- **做**：两处 `output_fields` 字面量合并为常量（已并入阶段 C）。
- **做**：`Evidence.from_doc` / `Evidence.from_chunk` 构造器，替掉 `app._evidence_from_doc`（999-1018）与 `app._graph_evidence`（1064-1082）里两段各 11 行的手写构造。
- **不做**：把 `ZvecChunkFields` / `QueryChunkFields` / `ChunkContext` / `Evidence` 从 `Chunk` 自动派生。它们的默认值、`extra` 策略、frozen 与否都真实不同，过度派生是用显式换取小聪明。

---

# 各阶段完成后要更新的文档

每个阶段落地后，在 `docs/architecture-archive.md` 末尾追加一条与现有格式一致的记录（参照 "2026-07-26 Hybrid Retrieval MVP 记录" 的写法：做了什么、刻意没做什么、下一步）。

`CLAUDE.md` 的「架构」小节在阶段 A 和 C 之后需要同步：

- A 之后：模块清单加 `extraction.py`，`graph.py` 的描述去掉"通用解析"部分，`read_chunks` 的归属改到 `store.py`
- C 之后：「`store.py` —— 唯一接触 zvec 的地方」从描述变成事实，可去掉「易踩坑」里与裸 `coll` 相关的提示
