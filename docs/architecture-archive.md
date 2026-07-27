# ReadFellow 架构归档

日期：2026-07-06

本文档归档 ReadFellow 当前 demo 状态、已知架构不足、GraphRAG 方向、zvec MCP 集成边界，以及本地索引成本估算相关讨论。

## 当前定位

ReadFellow 是一个本地优先的 CLI 工作流，用于辅助 agent 阅读长文档。当前最重要的设计规则是：

- 检索只是导航。
- 原始 chunk 才是证据。
- 源文档始终是事实依据。

当前实现是一个可用的 demo，还不是稳定的文档检索平台。它可以切分 UTF-8 文本、构建 zvec 索引、通过向量检索或中文 FTS 查询、取回原始 chunk 文本，并基于已存储的 chunk 构建轻量 JSON 知识图谱。

## 主要架构不足

### CLI 承担了过多应用逻辑

`src/readfellow/cli.py` 目前直接编排 chunking、embedding、zvec 写入、metadata 写入、图谱抽取、进度过滤和输出格式化。

这对 demo 是可以接受的，但会让未来接口变困难：

- MCP server 工具会需要重复 CLI 编排逻辑。
- TUI、批处理任务或 Python library 接口会重复同一套流程。
- 测试会自然地面向实现细节，而不是稳定的工作流接口。

推荐方向：让 CLI 变成更深模块之上的薄 adapter，例如 `index_document`、`semantic_search`、`fts_search`、`fetch_chunk`、`build_graph` 和 `query_graph`。

### metadata 与 zvec 的一致性较弱

索引流程会先写 `metadata/<collection>/manifest.json` 和 `chunks.jsonl`，再分批写入 zvec。如果 embedding 或 zvec 写入中途失败，metadata 可能显示完整 chunk 集，但 collection 实际不完整。

需要补强：

- 先构建到临时 metadata/index 位置。
- 显式标记构建状态：started、complete、failed。
- 原子发布已完成的构建。
- 增加校验命令，检查 manifest、chunk metadata、zvec docs、embedding 维度和 source hash 是否一致。

### 缺少统一项目配置

index 目录、metadata 目录、Ollama URL、embedding 模型、生成模型、chunk 大小、overlap 和 keep-alive 等默认值目前分散在 CLI 参数和常量中。

推荐方向：引入一个 `ReadFellowConfig` 模型，供 CLI、未来 MCP 工具、测试和 library 调用者共用。

### 外部依赖 seam 还不够清晰

Ollama 和 zvec 访问已经被拆到单独模块，但应用工作流仍直接依赖具体 adapter。这会限制测试和替换提供方。

有价值的 seam：

- `Embedder`：生产 adapter 是 Ollama；测试可以使用确定性向量。
- `Generator`：生产 adapter 是 Ollama；测试可以使用预置 JSON 抽取结果。
- `ChunkStore` 或 `SearchStore`：生产 adapter 是 zvec；测试可在可行时使用小型本地替身或内存替身。

不要过早抽象。只有至少两个 adapter 真实存在时，seam 才有价值：生产加测试，或生产加另一个提供方。

### 检索结果模型不统一

向量检索、FTS、fetch 和 graph query 返回不同形状。这会阻碍高质量 hybrid retrieval。

推荐方向：引入统一 evidence 模型：

- `chunk_id`
- `source_path`
- `line_start`
- `line_end`
- `chapter`
- `text`
- `retrieval_mode`
- `score`
- 适用时包含 `graph_context`

所有面向用户的回答都应该基于这个 evidence 模型落回原文证据。

### 防剧透控制容易被新入口绕过

`ProgressFilter` 是好的基础，但每个命令都必须记得主动应用它。未来 MCP 工具、graph retrieval 或 hybrid retrieval 都可能意外绕过它。

推荐方向：把 progress filtering 放进共享检索接口中，而不是作为每个命令可选的粘合逻辑。

### 文档输入层过窄

当前 reader 假设输入是 UTF-8 文本，并使用中文小说风格章节标题。这足够支撑最初实验，但长文档阅读最终需要文档 reader 层。

可能的 adapter：

- 纯 UTF-8 文本。
- Markdown。
- EPUB。
- 带页码 provenance 的 PDF。
- HTML。

所有 reader 都应该产出带稳定 provenance 的 text unit：source path、可用时的 byte/page/line offset、chapter/title metadata 和原始文本。

### 缺少 corpus 和 build 概念

项目当前主要用 collection 名组织数据。面对多文档、多模型版本和重复索引构建时，这会变得含混。

有用的领域概念：

- `Corpus`
- `SourceDocument`
- `ChunkSet`
- `IndexBuild`
- `Collection`
- `GraphBuild`

这些概念不应过早替代简单 CLI，但 metadata 模型应逐步朝它们演进。

### 测试缺少端到端覆盖

当前测试覆盖了 chunking、progress filtering、Ollama 响应解析、graph 合并和查询。缺失的覆盖包括：

- zvec 写入、查询、fetch 集成测试。
- `optimize` 后中文 FTS 持久化测试。
- CLI smoke test：index -> search -> fetch。
- 索引失败恢复。
- 使用确定性 fake generator 的 graph extraction 测试。

## GraphRAG 评估

### ReadFellow 当前具备什么

当前 graph 实现更准确地说是轻量 graph-assisted retrieval，而不是完整 GraphRAG。

它会：

- 从 `metadata/<collection>/chunks.jsonl` 读取 chunk。
- 调用 Ollama 生成模型抽取实体和关系。
- 通过 prompt 和 parser 规则限制实体类型和关系类型。
- 将可审计 JSON 图谱存到 `metadata/<collection>/graph.json`。
- 合并实体别名。
- 保留 chunk 位置和 evidence 文本。
- 按实体名、别名或关系关键词查询。
- 遵守阅读进度限制。

这足够用于：

- 查询人物、别名、地点、组织、物品和概念。
- 查找简单关系，例如帮助、认识、拥有、属于、身份是、别名是。
- 从 graph 命中导航回源 chunk。
- 在已知阅读范围内进行非剧透图谱查询。

### 当前还不具备什么

它不具备完整 GraphRAG 能力：

- 没有 community detection。
- 没有 community hierarchy。
- 没有 community summaries 或 reports。
- 没有 global search。
- 没有 local graph traversal search。
- 没有 DRIFT 风格 query expansion。
- 没有 graph node embeddings。
- 没有 graph/vector/FTS 混合排序。
- 没有 Cypher 或 property graph 查询语言。
- 没有稳健的实体消歧。
- 没有冲突处理。
- 没有针对原始 chunk 文本的 evidence 校验。

### 建议

暂时不要引入重型 GraphRAG 实现。

先把当前轻量 graph 加强成 graph-assisted retrieval：

1. 针对 chunk 文本校验抽取出的 evidence。
2. 让 graph query 返回 chunk id 和源证据，而不只是打印图谱事实。
3. 增加 hybrid retrieval 路径，组合 graph hits、FTS hits、vector hits 和最终 chunk fetch。
4. 记录 prompt version、graph schema version、generator model、extraction settings 和 source chunk hashes。
5. 当 chunk 或 source hash 改变时，加入失效与重建逻辑。
6. 建立一小组真实用户问题评估集，再决定是否需要更重的 GraphRAG。

### 什么时候值得上重型 GraphRAG

只有真实问题需要下列能力时，才考虑 Microsoft GraphRAG、LlamaIndex PropertyGraphIndex、Neo4j 或类似更重的方案：

- 全 corpus 主题总结。
- 跨文档全局模式发现。
- 超出简单关系查找的多跳图遍历。
- 围绕实体的稳定子图检索。
- community-level summaries。
- 同时需要全局和局部 graph context 的 query planning。

这些能力有价值，但会显著增加索引时间、依赖、存储复杂度和 prompt 维护成本。

## 图谱索引成本说明

当前 `graph-index` 成本大致与 chunk 数量线性相关。它会对每个 chunk 调用一次本地生成模型。

默认 chunk 设置：

- 目标 chunk 大小：2400 字符。
- Overlap：240 字符。
- 有效步长：约 2160 字符。

对于本地 RTX 4070 Ti Super + Ollama + Qwen 8B 生成模型，主要成本是等待时间，而不是现金成本。

轻量 `graph-index` 粗略耗时范围：

| 文本规模 | 约合 chunk 数 | 估计耗时 |
| ---: | ---: | ---: |
| 100,000 中文字符 | 45-50 | 5-20 分钟 |
| 1,000,000 中文字符 | 450-500 | 1-3 小时 |
| 3,000,000 中文字符 | 约 1400 | 3-8 小时 |
| 10,000,000 中文字符 | 约 4600 | 10-30 小时 |

重型 GraphRAG 可能贵几倍，因为它可能增加实体摘要、community detection、community reports、global/local/DRIFT 查询结构和额外 LLM pass。对同一 corpus，按 3-10 倍 LLM 工作量做规划是现实的。

推荐本地 benchmark：

```sh
time uv run readfellow graph-index --collection sample --llm-model qwen3:8b --limit 20 --rebuild
wc -l metadata/sample/chunks.jsonl
```

然后估算：

```text
estimated_total_time = measured_20_chunk_time / 20 * total_chunk_count
```

## zvec MCP 集成

zvec 有官方 MCP server，但现阶段它不应该替代 ReadFellow 的核心代码。

适合的用途：

- 从 AI 客户端进行外部调试。
- 检查 zvec collections。
- 运行临时 vector query。
- 检查 collection schema 或 document fetch。

不适合的用途：

- 让 ReadFellow 的核心索引和检索依赖通用 zvec MCP。
- 让通用 MCP 工具绕过 ReadFellow 的 provenance 和 progress-limit 规则。
- 在没有严格维度/模型控制的情况下，把它默认的 OpenAI-compatible embedding 流程与 ReadFellow 的 Ollama `qwen3-embedding:8b` collection 混用。

推荐方向：

- ReadFellow 核心工作流继续直接调用 Python zvec。
- 之后暴露保留项目语义的 ReadFellow 专用 MCP 工具：
  - `index_document`
  - `semantic_search`
  - `fts_search`
  - `hybrid_search`
  - `fetch_chunk`
  - `fetch_range`
  - `graph_query`

## 近期优先级计划

### 优先级 1：让核心工作流可复用

把 CLI 编排逻辑抽到小接口的应用模块中：

- `index_document(config, source, collection, options)`
- `semantic_search(config, query, collection, progress)`
- `fts_search(config, query, collection, progress)`
- `fetch_chunk(config, chunk_id, collection, progress)`
- `build_graph(config, collection, progress, options)`
- `query_graph(config, query, collection, progress)`

CLI 应只负责解析参数、调用这些模块并格式化输出。

### 优先级 2：增加可靠 evidence flow

把所有检索输出统一成 evidence 模型。每条回答路径最终都应该落到带精确 source location 的原始 chunk 文本。

这比增加重量级 graph stack 更重要。

### 优先级 3：加固 graph extraction

增加：

- Evidence substring validation。
- Extraction prompt versioning。
- 带 source chunk hashes 的 graph metadata。
- 当 chunk metadata 改变时的 rebuild/invalidate 行为。
- 使用确定性 generator adapter 的测试。

### 优先级 4：增加 hybrid retrieval（已完成，见 `docs/hybrid-retrieval-mvp.md`）

先实现简单本地策略，再考虑引入更重框架：

1. 运行 graph query 获取实体/关系线索。
2. 运行 FTS 获取精确名称和短语。
3. 运行 vector search 获取模糊语义匹配。
4. 按 chunk id 合并。
5. Fetch 原始 chunks。
6. 返回排序后的 evidence。

已由 `hybrid` 子命令 / `app.hybrid_search` 实现，用 RRF（k=60、等权、每路召回 `top_k * 10`）融合三路名次。

### 优先级 5：引入重型 GraphRAG 前先评估

创建小型评估集：

- 20-50 个真实问题。
- 预期 source chunks 或 line ranges。
- 类别：精确查找、别名查找、关系查找、时间线、模糊主题、跨章节关系。

用它比较：

- 仅 FTS。
- 仅 vector。
- 仅 graph。
- Hybrid retrieval。

只有当评估显示轻量 hybrid 方法不足时，才引入更重的 GraphRAG stack。

## 当前决策总结

- 保持 ReadFellow 本地优先、基于源文档证据。
- 把 zvec 视为 vector/FTS store，而不是完整应用层。
- 把 graph extraction 视为导航辅助，而不是权威知识。
- 在有证据表明轻量 hybrid retrieval 无法回答真实问题前，不引入重型 GraphRAG。
- 在增加 MCP 等新的用户入口前，先构建更深的内部模块。

## 2026-07-06 重构记录

已完成本文档中两项优先不足的第一轮重构：

- `src/readfellow/cli.py` 已收缩为薄 adapter，主要负责 argparse 参数解析、进度消息和结果格式化。
- 可复用应用 workflow 已移入 `src/readfellow/app.py`，包括 `index_document`、`semantic_search`、`fts_search`、`fetch_chunk`、`build_graph` 和 `query_graph`。
- 项目默认值已统一到根目录 `config.yaml`，并由 `ReadFellowConfig` 加载，供 CLI、测试和未来 library/MCP 入口共用。
- CLI 参数仍可覆盖配置文件中的默认路径、Ollama 端点、模型、chunk 参数、搜索 top-k 和 graph extraction 设置。

## 2026-07-13 Evidence MVP 记录

已完成优先级 2 的最小可用实现：

- 新增统一 `Evidence` 模型，包含 chunk id/index、来源路径、行/字节范围、章节、text hash、原始 chunk 文本、检索模式、可选分数和可选 graph context。
- `semantic_search`、`fts_search`、`fetch_chunk` 和 `query_graph` 均返回 Evidence，不再向 CLI 泄漏 zvec `Doc`。
- `fetch_chunk` 使用 `found`、`not_found`、`outside_progress` 明确区分结果；越过阅读进度时不返回原文。
- graph query 根据命中的 chunk id 回取已存储的完整原始 chunk，实体与关系只作为导航上下文，不作为权威正文。
- graph alias/type 尚无逐值 provenance；设置阅读进度时保守地禁用它们的匹配和输出，避免未读信息侧漏。
- CLI 统一格式化 Evidence，同时保留来源文件、精确行范围和章节。
- 增加真实临时 zvec smoke test，覆盖 optimize 后重开、中文 FTS、阅读进度过滤和 fetch 原文。

本轮没有实现 hybrid ranking、重型 GraphRAG、MCP、reader adapters 或索引原子发布。下一步仍按优先级 3 单独加固 graph extraction。

## 2026-07-13 Graph Extraction MVP 记录

已完成优先级 3 的最小可用实现：

- 实体和关系的非空 evidence 必须是对应原始 chunk 的精确子串；无效 evidence 会触发现有重试，不能落入 graph。
- graph schema 提升到 v2，并记录独立的 extraction prompt version、生成模型和有效 extraction settings。
- 每条 extraction 记录 source/text hash、行/字节范围；graph metadata 同时保存按 chunk id 索引的 source chunk fingerprints。
- graph workflow 会先核对原始 source hash 与 chunk metadata，再校验已处理 chunk 的版本、位置和 hashes；任一派生层 stale 时都 fail closed。
- `graph-index` 遇到旧 schema、prompt/model/settings 变化或已处理 chunk metadata 变化时自动整图重建；仅新增 chunk 仍断点续建。
- `build_graph` 接受最小 `GraphGenerator` adapter，确定性测试覆盖无效 evidence 重试、落盘重开、up-to-date 跳过及 stale rebuild。

本轮没有实现逐 alias/type provenance、局部 graph surgery、provider registry、hybrid retrieval 或重型 GraphRAG。下一步按优先级 4 实现简单本地 hybrid retrieval。

## 2026-07-26 Hybrid Retrieval MVP 记录

已完成优先级 4 的最小可用实现（设计论证见 `docs/hybrid-retrieval-mvp.md`）：

- 新增 `hybrid` 子命令与 `app.hybrid_search`，组合既有的 `semantic_search` / `fts_search` / `query_graph`，不重复三者的 manifest / collection / progress filter 前置逻辑。
- 用 RRF（`Σ 1/(60 + rank)`）按名次融合，不做分数归一化——实测 vector 余弦区间 0.458–0.544 与 FTS 的 0.972–1.945 量纲不可比，且 graph 路本就没有分数。k、权重、召回倍数均不进 config。
- graph 路在融合层按 `(关系数, 实体数)` 降序定名次，`graph-query` 自身的输出顺序不变。
- `graph.json` 缺失或 stale 时**跳过该通道并结构化上报**（`HybridSearchResult.channels`），不整体失败：跳过整路即最严格的 fail closed，stale 的实体/关系/别名一个字进不了输出；而 vector 路故障（Ollama 不可达）则整体失败，避免静默退化成纯关键词匹配。
- `Evidence` 增加 `matches: list[EvidenceMatch]`（通道 + 该通道内名次），使融合排序可解释；`matches` 对其他命令恒为空，它们的输出逐字节不变。

本轮没有实现二次 rerank、评估集、MCP 暴露或重型 GraphRAG。下一步按优先级 5 建立评估集，用数据判断是否需要调 RRF 参数或引入更重的 stack。

已知遗留：`graph-index` 在 `corpus/samples/赛博英雄传.txt` 上用 qwen3:8b 会稳定触发「evidence 不是原文精确子串」，重试 5 次仍无法通过，因此该语料目前建不出非空图谱；hybrid 的 graph 通道端到端验证是在一份短小规整的中文样本上完成的。

## 2026-07-26 模块深化评审

优先级 1-4 落地后做了一轮完整架构评审（读完全部 src），识别出 6 个 deepening 候选。执行计划见 `docs/module-deepening-plan.md`，此处只记结论：

- **阶段 A（先做）**：`graph.py` 824 行装了三件事——知识图谱领域、通用 LLM-JSON + 证据锚定工具、`chunks.jsonl` 读取。拆出 `extraction.py`；`read_chunks` 移回 `store.py` 与其写方 `write_manifest` 同处。seam 位置由现状证明：`analysis.py` 从 `graph.py` 导入的 7 个符号全部是通用工具，且无任何测试导入它们。
- **阶段 B**：`build_graph` 与 `build_analysis` 12 个阶段有 10 个相同，另有 15 对镜像符号。抽出共享的重试、三态加载、落盘、status 判定与公共 staleness 检查；合并字段完全相同的 `GraphExtractionSettings`/`AnalysisSettings` 与 `GraphConfig`/`AnalysisConfig`。**刻意不做**完整 `DerivationSpec` Protocol——它需要约 14 个成员，用宽 interface 换掉实现重复并不提升 depth，留到第三个派生物出现时再评估。
- **阶段 C**：`store.py` 是自由函数加裸 `coll` 句柄，不构成 module 的 interface。后果是 `app.py` 内联 `import zvec`、直接调 5 个 zvec collection 方法、直接读 `Doc.fields`，「`store.py` 是唯一接触 zvec 的地方」这条已声明的不变量实际已破；测试也只能 monkeypatch `app` 上的 5 个模块级符号。目标是 `ChunkStore` interface + zvec/InMemory 两个 adapter。
- **阶段 D/E（待触发）**：防剧透规则在 4 处手写、chunk 字段集手抄 11 遍。D 等第三个派生物出现再做；E 只做无歧义的部分（两处逐字相同的 `output_fields` 字面量、`Evidence` 构造器）。
- **不做**：`FusedEvidence` 拆分。hybrid MVP 刻意保持单一扁平模型以让其他命令输出逐字节不变，只有第二个消费者需要跨通道比较分数时才重开。

## 2026-07-26 阶段 A 落地记录：拆出 `extraction.py`

按 `docs/module-deepening-plan.md` 阶段 A 执行完毕，纯搬迁，无行为变更：

- 新增 `src/readfellow/extraction.py`（125 行），只依赖 `models`：LLM JSON 读取（`parse_json_object`、`get_any`、`as_list`、`normalize_text`）、证据锚定（`_LOOSE_IN_EVIDENCE`、`locate_evidence`、`resolve_evidence`）、chunk 归一（`chunk_context`、`int_value`）。`_int_value` 因为跨两个 module 被用（`chunk_context` 与 `finalize_graph` 的 7 处排序键）而公开为 `int_value`。
- `read_chunks` 从 `graph.py` 移到 `store.py`，紧邻写方 `write_manifest`——`chunks.jsonl` 的读写方从此同处。`graph.py` 仍 import `store.metadata_path` 用于 `graph_path`，这是对的：派生物存在哪儿本就是领域模块该知道的事。
- 依赖图从 `analysis → graph` 变为 `analysis → extraction ← graph`，两条派生管线不再互相伸手，`graph.py` 824 → 697 行。

**刻意留在 `graph.py`**：`_as_strings`、`_case_key`、`_has_same_values`、`_chunk_text`、`_chunk_value`。它们都只有 graph 一个调用方，搬进 `extraction.py` 等于造没有第二个 adapter 的假 seam，还会撑宽 `extraction.py` 的 interface。（计划文档 A.1 原本把后两个列进了搬迁表，已按此更正。）

验证：`uv run pytest` 39 passed，**一行测试代码都没改**——这正是阶段 A 事先定下的验收信号，说明搬走的确实是没有领域耦合的通用件；`uvx ruff format`/`check` clean。

已知遗留：`_chunk_text(chunk)` 与 `_chunk_value(chunk, "text")` 语义完全等价（`Chunk` 是 `BaseModel`，`getattr` 分支能覆盖），属评审前就存在的冗余，本轮未动。下一步进入阶段 B（B1 + B2）。

## 2026-07-26 阶段 B 落地记录：`derivation.py` 与孪生管线合并

按 `docs/module-deepening-plan.md` 阶段 B 执行 B1 + B2，B3 仍按计划待定。

**B1 · 新增 `src/readfellow/derivation.py`（81 行）**，只依赖 pydantic 与标准库：

- `JsonGenerator` Protocol —— 原 `app.GraphGenerator`。它同时服务两条管线，名字里的 "Graph" 已不成立；且 `generate_with_retry` 需要它，留在 `app.py` 会造成循环 import，所以随之迁移改名。
- `generate_with_retry` —— 原来抄了两份（`build_graph` 内联 27 行 + `_analyze_chapter` 21 行）。**生成与解析算同一次尝试**：模型答出没法用的 JSON 和答不出来一样是失败。`build_graph` 那份的 `for/else` 分支在 `retries >= 0` 时不可达，合并时收敛成 analysis 版的 "no attempt was made" 写法。
- `load_or_reset` —— 不存在 / rebuild / stale 三态，是 fail closed（不变量 1）在派生路径上的**单一落点**。合并前两边逻辑等价但写法已经漂移（graph 先读再重置，analysis 先建空再覆盖）。
- `write_json_document`、`derivation_status`。

**B2 · 模型与配置合并**：`GraphExtractionSettings` + `AnalysisSettings` → `models.DerivationSettings`；`GraphConfig` + `AnalysisConfig` → `config.DerivationConfig`（`config.yaml` 仍是 `graph:` / `analysis:` 两个独立键）。**落盘字段名 `KnowledgeGraph.extraction_settings` 与 `AnalysisDocument.settings` 未动**，因此已有 `graph.json` / `analysis.json` 不会被判 stale。

**计划外补做**（同属"孪生管线"的逐字重复，B1 原文漏列）：`app._load_indexed_source` 统一 `read_manifest` + `read_chunks` + `_validate_chunk_metadata_source` + `progress_filter_from_limit`（`build_graph` / `build_analysis` / `query_graph` 三个 adapter），顺带把"读 chunk 前必须校验源文件"焊进读取路径；`app._generation_plan` 统一 per-run override 阶梯（此前 `llm_model` 用 `or`、`num_predict`/`retries` 用 `is None` 的不对称抄了两份）。

**刻意没做**：

- **B1 的第 5 项 `common_staleness_reason`**。两边前 5 项检查同构，但抽出来要 9 个参数配 11 行函数体——interface 复杂度追平 implementation，正是本计划拒绝 `DerivationSpec` 的同一理由；调用点净省 2 行，还要改 2 条用户可见的 stale 消息。
- **`if generator is None: OllamaGenerator(...)` 那 6 行**。纯 pass-through 构造，抽出来不隐藏任何决策。
- **`GraphBuildOptions`/`Event`/`Result` 与 analysis 对应物的合并**，按计划留给 B3。

**行为等价性验证**（这是阶段 B 的真正验收标准）：

- `uv run pytest` 39 passed；`uvx ruff format`/`check` clean。测试改动只有 1 行：`tests/test_cli_config.py` 的 `GraphConfig` → `DerivationConfig` import（类改名的必然结果，不是迁就）。
- 真实产物：`graph-query` 在 `ch5`/`sample`/`toy` 三个 collection 上输出与改动前**逐字节一致**；`analyze --collection ch5` 仍从第 4 章续建（即 `analysis.json` 未被判 stale），重试计数与最终错误消息格式不变；`metadata/ch5/{graph,analysis}.json` 文件内容未被改写。

已知遗留：

- `query_graph` 里 "graph.json 缺失" 的检查现在排在源文件校验之后。仅当**chunk 元数据已 stale 且 graph.json 又不存在**时，报错从 "graph index not found" 变成 "chunk metadata is stale"——后者才是该先修的问题，且这个组合无测试覆盖。
- `store.write_manifest` 里还有第三份 `json.dumps(..., ensure_ascii=False, indent=2)`，与 `write_json_document` 重复。属评审前就存在的冗余，且跨到 store 层，本轮未动；等阶段 C 动 `store.py` 时一并处理。

下一步：阶段 C（补完 zvec seam）。

---

## 2026-07-27 阶段 C 落地记录：ChunkStore seam

**做了什么**：把 zvec 收进 `store.py`，让「`store.py` 是唯一接触 zvec 的地方」从一句愿望变成可 grep 验证的事实（`grep zvec src/readfellow/app.py` 现在为空）。

`store.py` 对外的 interface 收成一个 `ChunkStore` Protocol，5 个方法：`upsert` / `commit` / `search_vector` / `search_fts` / `fetch`。两个 adapter：`ZvecChunkStore`（生产）、`InMemoryChunkStore`（`tests/test_app.py`）。

三个设计决定与计划原文不同，理由记在 `docs/module-deepening-plan.md` 的阶段 C 小节：`open` 不进 Protocol（改为两个 classmethod）；`upsert` 收 `embed` callback 而不是算好的 `vectors`（否则"跳过未变 chunk"省不掉唯一昂贵的那步）；`filter: str` 改成 `progress: ProgressFilter`（filter 表达式是 zvec 方言）。

**检索结果以 `Evidence` 跨 seam**：`_evidence_from_doc` 从 `app.py` 移进 `store.py` 变私有，`Doc` / `Status` / `CollectionOption` / `QueryChunkFields` 都不再出现在 `app.py`。这直接服务不变量 2——store 递出来的东西已经带着 `source_path:line_start-line_end`。

顺带修复：两份逐字相同的 9 元素 `output_fields` 合并为 `STORED_OUTPUT_FIELDS`，两次 `coll.query` 收进 `_search`。

**测试形态的变化**（这才是 seam 的实际收益）：`app` 的 5 个入口都加了可选的 `store: ChunkStore | None`，与已有的 `generator: JsonGenerator | None` 同构。测试从 monkeypatch 4 个存储符号（`open_existing_collection`、`query_vector`、`query_fts`、`fetch_stored_chunk`）改成注入一个 adapter，**测试文件里不再 import 任何 zvec 符号**（`from zvec import Doc` 已删）。剩下的 `monkeypatch.setattr(app, ...)` 只剩 `read_manifest`（文件）与 `OllamaEmbedder`（网络）。

`tests/test_cli_evidence.py` 保持真实 zvec，但改走 `ZvecChunkStore.open_for_write` + `upsert` + `commit`——顺带让它第一次覆盖了真实的写入协议（此前它自己拼 `Doc` 调 `collection.insert`，绕过了 `index_document` 的整条批处理路径）。

**刻意没做**：

- **没有换用 zvec 的 `Collection.upsert`**。它存在，用它能让 `missing`/`changed` 拆分整个消失，但那是一次没有测试托底的写语义变更，且拆分已经藏在 adapter 内部、不再有 interface 成本。
- **`InMemoryChunkStore` 放在 `tests/` 而不是 `src/`**。src 里没有生产调用点的 fake 是投机代码。
- **`read_manifest` / `read_chunks` / `write_manifest` 没有进 `ChunkStore`**。它们读写 `metadata/` 下的 JSON，与 zvec 无关，塞进来只会让 interface 变宽。

**行为等价性验证**：

- `uv run pytest` 40 passed（新增 1 个：re-index 未改动文档时 `embed` 一次都不该被调用——这正是刚搬进 `upsert` 的那段逻辑，此前无任何测试覆盖）；`uvx ruff format`/`check` clean。
- 真实 zvec + 真实 Ollama，改动前后跑同一串命令：新建索引 `indexed 8, skipped 0`、重跑 `skipped existing chunks` / `inserted=0, skipped=8`、`--chunk-chars 500` 触发 update 路径 `indexed 7, skipped 1`、`fts` 与 `fetch` 输出逐字节一致。四条写路径（insert / skip / update / commit）全部覆盖。唯一差异是一条向量检索得分的第 4 位小数（0.446121 → 0.446391），来自 rebuild 后重新生成 embedding 的浮点漂移，id、排序、行范围均相同。
- `graph-query` 在 `toy` 上输出正常，`sample` 仍按 chunker 版本 fail closed。

**已知遗留**：

- `hybrid_search` 现在只开一个只读句柄并传给 `semantic_search` / `fts_search`（此前各开一个）。行为无变化，但 collection 不存在时的报错点从 `semantic_search` 内部前移到了 `hybrid_search` 开头——错误消息相同。
- `store.write_manifest` 里那份 `json.dumps(..., ensure_ascii=False, indent=2)` 仍与 `derivation.write_json_document` 重复。阶段 B 留的这条本想在 C 处理，但 `write_manifest` 同时还写 `chunks.jsonl`（逐行、无 indent），只有前半段能复用，且 `store` → `derivation` 是一条新的模块依赖。收益（3 行）不抵这条依赖，继续留着。
- 索引仍不是原子发布：embedding 中途失败会留下 metadata 完整而 collection 不完整的状态。seam 让这件事**变得可修**（`upsert` + `commit` 已经是两阶段的形状），但本轮没有动。

下一步：阶段 D（ReadingWindow）与阶段 E 均为触发条件驱动，暂不启动。

## 2026-07-27 图谱通道降级为标注

`hybrid` 从 vector / FTS / graph 三路 RRF 融合改为**两路打分 + 图谱标注**。详细论证与实测见 `docs/graph-channel-demotion.md`。

**为什么**：`graph._matches_entity` 按 `needle in value` 匹配，要求查询串是实体名的子串，而 `hybrid` 把用户原始问句整个喂了进去——`graph-query "基因税"` 命中，`"基因税是什么"` 返回空。三路融合在真实提问上长期只有两路在跑。

两个修法都被实测否决：切词取并集需要一份自己维护的中文停用词表（`的` 在 1580 实体的部分图谱上命中 832 个实体、1160 条关系，而目标「基因税」只命中 4 个），且 `jieba` 是全新依赖、zvec 不暴露分词 API；实体桥接则撞上一条硬约束——**桥接产出量与实体区分度互为倒数**（尤基覆盖 53.8% 的已处理 chunk，全量外推约 1241 块；而 79.5% 的实体只跨 1 个 chunk，桥接产出为零）。两者缺的是同一样东西：按语料内稀有度加权的选择函数，而 FTS 通道的 BM25 已经免费提供了它。

**做了什么**：新增 `graph.annotate_chunks`（给定 chunk id 返回挂在其上的实体/关系，走同一套进度过滤）与 `app._annotate_with_graph`；`app._load_graph` 收拢 `query_graph` 与标注路径共用的加载+staleness 校验；`_context_by_chunk` 从 `_graph_evidence` 抽出，`query=None` 即标注语义。`ChannelMode` 与 `EvidenceMatch.mode` 收窄为 `vector | fts`。

**刻意没做**：

- **没改 `graph-query`**。它是关键词工具，子串匹配是它的正确语义，输出逐字不变。
- **没改 RRF 参数**。等权、`RRF_K=60` 不进 config 的决定沿用 `hybrid-retrieval-mvp.md`。
- **没修实体抽取噪声**（整句话、对白、`一个人`/`世界` 这类泛化词被抽成实体）。改 prompt 要 bump `GRAPH_PROMPT_VERSION` 并整图重建，等全量 `graph-index` 跑完有完整样本再一次性评估。
- **没修 `graph.json` 的非原子写**。`write_json_document` 直接 `write_text`，全量约 17 MB，并发读有概率拿到截断 JSON。既有行为，与「索引不是原子发布」同类。

**验证**：`uv run pytest` 42 passed（净 +2）、ruff clean。全量 `sample` 上（`graph-index` 运行中）`hybrid "尤基的父亲是怎么死的"` 得到 `3/3 results annotated`，`--max-chapter 3` 时标注全部来自第一章、无越界。

下一步：等全量 `graph-index` / `analyze` 出完整产物后评估实体抽取质量。阶段 D / E 仍为触发条件驱动。

## 2026-07-27 `status` 子命令与派生物原子发布

起因是 `graph-index` 跑完之后没有任何办法验证结果，顺带引出「是否值得引入 sqlite」。选型结论与实测见 `docs/storage-engine-decision.md`（结论：不引入，也不把图谱搬进 zvec）。本条只记代码变更。

**做了什么**：

- `derivation.write_json_document` 改为临时文件 + `os.replace`。这直接勾掉了上一条「刻意没做」里的第四项：`graph.json` 的非原子写。索引侧仍未改。
- `ChunkStore` 加第 6 个方法 `stats() -> StoreStats`（`doc_count` + `index_completeness`）。`ZvecChunkStore` 读 zvec 的 `collection.stats`，`InMemoryChunkStore` 报自己存了多少。这是**唯一**能检出「索引不是原子发布」现场的手段——manifest 承诺 2307 而 collection 只有 1800 时，此前没有任何命令会说话。
- `graph.graph_diagnostics(graph) -> GraphDiagnostics`：已声明实体占比、丢弃条目占比、零产出 chunk 数、实体跨 chunk 数的 p50/p90/max，以及三条带实测参考值的告警。
- `app.collection_status(config, collection, *, store) -> CollectionStatus`：manifest + 索引对账 + graph/analysis 的失效原因与续建/重建判定 + 图谱诊断，全部只读。
- `cli.py` 加 `status` 子命令，只做格式化。

**关键设计点**：`status` 是唯一「报告失效而不 fail closed」的入口。它自己 `read_chunks` 并显式调 `_validate_chunk_metadata_source`，把异常 catch 成一个字段——这是 CLAUDE.md 里「凡是读 `chunks.jsonl` 都走 `_load_indexed_source`」那条规则的唯一例外，已在该处标注。失效判定传入**配置里当前的**模型与参数，因此回答的是「重跑会续建还是从头重来」。

**刻意没做**：

- **没给 `status` 加 `--json`**。它的读者是人和 agent，两者都读得懂这段文本；加一份机器格式就要维护两套输出契约。
- **没定义退出码策略**。stale 不等于坏，incomplete 才是坏，而这条界线一旦编进退出码就成了 API。集合读不出来时照旧走 `main` 的通用 `except` 返回 1。
- **没修索引的原子发布**。`status` 让它可检出，没让它可避免。
- **没为 `os.replace` 写并发测试**。真正验证需要造并发，成本远超收益。

**验证**：`uv run pytest` 46 passed（净 +2，另有 1 条断言加进既有的真 zvec 用例）、ruff clean。五个真实 collection 上跑过 `status`：`sample` 报 584/2307 可续建、`smoke` 全绿、`ch5` 同时命中 stale 与「零产出 chunk」告警、`toy` 命中源文件缺失。
