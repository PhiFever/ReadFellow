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

### 优先级 4：增加 hybrid retrieval

先实现简单本地策略，再考虑引入更重框架：

1. 运行 graph query 获取实体/关系线索。
2. 运行 FTS 获取精确名称和短语。
3. 运行 vector search 获取模糊语义匹配。
4. 按 chunk id 合并。
5. Fetch 原始 chunks。
6. 返回排序后的 evidence。

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
