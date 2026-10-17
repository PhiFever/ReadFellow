# 索引与检索：机制和易踩的坑

改 `index`、`search` / `fts` / `fetch` / `hybrid`、进度过滤或 `store.py` 之前读这份。审查规则见 `CODING_STANDARDS.md`，已定的设计决策见 `docs/decisions.md`。

## 数据流

索引：`chunk_document` → 用首个 chunk 探测 embedding 维度 → `ZvecChunkStore.open_for_write` → **先写 MySQL source_versions + chunks** → 按 `batch_size` 分批调 `store.upsert(batch, model=, embed=embedder.embed)`（比对 `text_hash` 决定 insert / update / skip，**只对真要写的 chunk 调 `embed`**）→ `store.commit(optimize=)`。

检索：读 manifest → `ZvecChunkStore.open_for_read`（`read_only=True, enable_mmap=True`）→ 构造 `ProgressFilter` → `store.search_vector/search_fts/fetch`，返回的已经是 `Evidence`。graph query 走另一路：图谱命中只给出 chunk id 与上下文，原文仍从数据库版本化 chunks 取（`_graph_evidence`）。

## zvec seam（`store.py`）

`store.py` 是唯一接触 zvec 的模块（ruff `TID251` 强制）。对外只有 `ChunkStore` Protocol 的 6 个方法：`upsert` / `commit` / `search_vector` / `search_fts` / `fetch` / `stats`。`stats` 返回 `StoreStats`（`doc_count` + `index_completeness`），只给 `status` 对账 manifest 用。

`ZvecChunkStore` 是生产 adapter，schema 定义、collection 打开/重建、`Doc` 转换、批内 text_hash 比对与 insert/update 拆分都在里面；测试里的 `InMemoryChunkStore` 是第二个 adapter。`Doc` / `Status` / `CollectionOption` 不跨 seam，检索结果直接以 `Evidence` 返回。旧 manifest / chunks JSON 读写工具仍在 `store.py`，只供导入与迁移测试。

## 切块与重建

- 切块：先按空行切成 `TextUnit`（保留行号、字节偏移、当前章节标题），再按 `target_chars` 装窗 + 尾部 overlap 拼成 `Chunk`。章节由 `iter_headings` 识别：分隔线后的首个非空行，或匹配 `CHAPTER_RE` 的行；与当前标题只差空白的重复行不另分章。
- **chunk id = `source_hash[:12]_%06d`**。改 `chunk_chars` / `overlap_chars` 会让 id 与行范围全部漂移，必须 `--rebuild`，否则旧 doc 残留在 collection 里。
- `index --rebuild` 不删除 `metadata/<collection>/` 下的旧 JSON 备份。
- **换 embedding 模型必须 `--rebuild`**：`open_for_write` 只在维度不匹配时报错，同维度的不同模型拦不住。
- **`optimize()` 不能随便跳**。`--no-optimize` 只用于测写入速度；不 optimize 时持久化的中文 FTS 重开后可能查不到。
- **索引不是原子发布**：embedding 中途失败会留下 metadata 完整而 collection 不完整的状态。`status` 用 `ChunkStore.stats()` 对账能检出，但写入本身仍未改成两阶段发布。

## Embedding 与生成接口

- `ollama.py` 用纯 `urllib` 调 `/api/embed` 与 `/api/generate`。embedding 默认 L2 归一化。
- 生成走**约束解码**：`format` 传 JSON schema（`GRAPH_RESPONSE_SCHEMA` / `ANALYSIS_RESPONSE_SCHEMA`），不传 `"json"`，因为后者只保证能解析，裸字符串在数组里也是合法 JSON。
- 采样参数全部来自 `DerivationSettings`（Qwen3 官方 non-thinking 推荐值，temperature 0.7）。**不用贪心解码**：贪心会让 retry 逐字节重放同一个坏结果。
- `openai_compat.py` 用 `json_schema` + `strict` 约束输出，写死关闭思考并逐次校验响应里没有思考内容；429 由 SDK 按 `retry-after` 重试。

## 进度过滤

`progress.py` 由「卷:书内章号」或行号算出 `ProgressFilter`，卷号由章号回到 1 推出，章号撞号时报错并列出每个候选的 `--max-line`。

过滤有两条路径，新入口两边都要走：

- `ProgressFilter` 整体传给 `ChunkStore`，zvec adapter 用它的 `expression`。
- 进程内用 `ProgressFilter.allows(fields)`，graph 与 fetch 走这条。

`ChunkStore.fetch` **不施加进度限制**，因为只有它要区分「没这个 chunk」和「还没读到」。`fetch_chunk` 拿到 Evidence 后自己判，返回 `found` / `not_found` / `outside_progress` 三态，越界时不带 text。

graph 在进度限制下清空 `aliases` 与 `types`（`graph._filter_entity`），因为它们是跨 chunk 聚合，没有逐值 provenance。

## 源文件校验

`_validate_chunk_metadata_source` 校验数据库 chunks 的 source path 与源文件当前 hash，源文件改过就要求重新 index。它接收去重后的 `(source_path, source_hash)` 集合，外面包了两层：

- `app._load_indexed_source`：给要读 chunks 原文的派生入口。
- `app._load_manifest`：给 `search` / `fts` / `fetch`（`hybrid` 经由前两者），只查身份集合（`ArtifactStore.read_manifest`），不拉正文。

检索路径也必须校验：源文件改过之后，存储的行号和 `--max-chapter` 算出的行号不在同一坐标系，会放行越界章节。

唯一例外是 `collection_status`：它用 `ArtifactStore.read_source` 读取，再显式调 `_validate_chunk_metadata_source`，把异常 catch 成 `source_error` 字段，因为 status 的职责是把失效报出来。

## hybrid 的图谱标注

融合只有 vector 和 FTS 两路，取 `top_k` 之后才用图谱给选中的 chunk 挂上实体与关系（`app._annotate_with_graph` → `graph.annotate_chunks`）。为什么不做召回通道见 `docs/graph-channel-demotion.md`。

- 标注读 `ArtifactStore.read_graph_slice`：只含 top-k chunk 里的关系和实体（实体带全部 mentions/evidence，排序要算跨度），不读整图。切片不是完整文档，不写 `_snapshots`，不能拿去 `save`。
- **显示预算**：每 chunk 最多 3 实体 + 3 关系，只列已声明实体（`types` 或 `evidence` 非空），关系要求两端都已声明，再按跨越 chunk 数升序截断。Tier 判定必须读**存储态**，因为 `_filter_entity` 在进度限制下会清空 `types`。这只收窄显示，`graph-query` 仍全量作答。
