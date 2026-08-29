# 存储引擎:为什么现在不换 sqlite 或 chromadb

2026-07-27 的讨论存档。起因是 `graph-index` 跑完之后没有任何办法验证结果,顺带问出「是否值得引入 sqlite 获得更好的事务和文件迁移能力」。

结论:**现在不引入 sqlite,也不把图谱搬进 zvec。** 但**可搬运性**这条动机在讨论末尾才提出来,没有评估完,它是唯一可能推翻本结论的方向——见「未决」。

2026-08-29 追加第二次选型评估(chromadb),结论相同:**不换 zvec**,拦路的是中文 BM25 而不是 MCP。见「chromadb 评估」。

## 不重新讨论的事

1. **要搬只能搬 zvec,不能上 sqlite。** sqlite 在这里能给的能力 zvec 全给,而且已经在依赖树里。引入 sqlite = 仓库里两个存储引擎。八项探针实测见下。
2. **写放大不是理由。** 每 chunk 整文件重写累计约 40 GB 看着吓人,换算成时间是 8 小时 run 里的 9 分钟(1.8%)。其余 98% 在等 Ollama。
3. **「迁移能力」归错了地方。** 8 小时重建的触发器是 `graph.py` 里的一行 policy(`if graph.schema_version != GRAPH_SCHEMA_VERSION`),不是格式限制。给模型加一个带默认值的可选字段,旧 `graph.json` 直接能载入,根本不需要 bump。而真正必须 bump 的是**语义**改动(prompt 变了 → 抽取结果就该不一样),那个**没有任何存储引擎能迁移**——新字段的值只有 LLM 能给。
4. **撕裂读用 `os.replace` 解决,不需要引擎。** 三行:临时文件 + 原子 rename。
5. **chromadb 不是备选,拦路的不是 MCP 而是中文 BM25。** chroma 内置 BM25 的分词器按空白切词,中文整句成一个 token;`$contains` 无序无分,喂不了 rank-based RRF。实测见「chromadb 评估」。

## 实测数据

### 写盘成本

在 `metadata/sample/graph.json`(7,721,942 B @ 515 chunks)上实测:

```
model_dump   0.012s
json.dumps   0.085s
write_text   0.006s
─────────────────────
合计         0.103s
```

`write_graph` 在 `app.py` 的 per-chunk 循环体内,每个 chunk 后整文件重写。

| | 值 |
|---|---|
| 单次写盘(7.7 MB) | 0.103 s |
| 单次写盘(外推 35 MB / 2307 chunks) | ≈ 0.47 s |
| 全程均值 × 2307 次 | ≈ 530 s ≈ **9 分钟** |
| 全量 run 总耗时 | ≈ 8 小时 |
| **写盘占比** | **≈ 1.8%** |
| 累计落盘 Σn×15KB | ≈ 40 GB(随 chunk 数二次增长) |

### 载入成本

| | 时间 | RSS 增量 |
|---|---|---|
| `graph.json` 7.72 MB @ 515 chunks | 0.083 s | +55 MB |
| 外推全量(×4.5) | ≈ 0.37 s | ≈ +250 MB |
| `chunks.jsonl` 13.1 MB / 2307 chunks | 0.159 s | +14 MB |

全量下 `hybrid` / `graph-query` 每次调用约付 0.5 s 反序列化 + 270 MB RSS。还不痛。

### zvec 能力探针(zvec 0.6.0)

八项全过:

| # | 能力 | 结果 |
|---|---|---|
| 1 | `CollectionSchema(vectors=None)` 无向量 collection | OK |
| 2 | `upsert` | OK |
| 3 | `query(queries=None, filter=...)` 纯过滤扫描 | OK |
| 4 | 重开后 filter 扫描 | OK |
| 5 | `add_column(FieldSchema, expression="1")` 在线加列 + 回填 | 5 行回填成功 |
| 6 | `update` 单行(不重写全表) | OK |
| 7 | `stats` | `{"doc_count":5, ...}` |
| 8 | `LIKE '%子串%'` | OK |

补充事实:

- zvec 的 filter 是一个真 SQL 引擎(`sqlengine_impl.cc`),文法含 `AND/OR/NOT/IN/BETWEEN/LIKE/WHERE/ORDER BY/LIMIT`。**等值是 `=` 不是 `==`**,写 `==` 报语法错。
- `indexes/<c>/manifest.N` + `LOCK` 是版本化的原子发布。
- 活的 sample collection:`{"doc_count":2307, "index_completeness":{"embedding":1.000000}}`。这正好覆盖架构归档里记的已知薄弱点(索引不是原子发布,embedding 中途失败会留下 metadata 完整而 collection 不完整),是 `status` 决定开 zvec 的直接理由。

探针脚本见会话 scratchpad,未纳入仓库。

### 搬图谱进 zvec 的真实成本

不是「换个文件格式」:`Doc` 是扁平 field map,而 `GraphEntity.mentions` / `.evidence` 是嵌套列表。要让进度过滤下推到 zvec,它们必须拆成独立的行 → entities / mentions / relations 三个 collection。这是**把文档模型重建成关系模型**,外加一个和 `ChunkStore` 对称的 `GraphStore` seam + 内存测试替身 + 一次性转换器。估 400–600 行。

另有两条:

- `build_graph` 目前**完全不碰 zvec**(只读 `manifest.json` + `chunks.jsonl`),搬进去就产生耦合。
- 未验风险:CLAUDE.md 记了「不 optimize 时持久化的中文 FTS 重开后可能查不到」。跑的过程中 `status` 要读到最新行,`flush` 够不够没验。

## chromadb 评估(2026-08-29)

问题:「既然已经不怎么需要 zvec 自带的 MCP 功能,为什么不迁移到相对更方便的 chromadb」。结论:**不换,拦路的不是 MCP 而是中文 BM25。**

### 前提不成立:MCP 从来不是 zvec 的选型理由

`rg -il mcp src/ tests/` 零命中。`docs/architecture-archive.md` 的「zvec MCP 集成」一节当初就否掉了这条路:「zvec 有官方 MCP server,但现阶段它不应该替代 ReadFellow 的核心代码……ReadFellow 核心工作流继续直接调用 Python zvec」。所以「不再需要 MCP」减掉的是一个从未计入的权重,不产生迁移动能。真正的支点是下面几条能力。

### 实测:chromadb 1.5.9 没有可用的中文 BM25

临时环境 `uv run --no-project --with chromadb`,未动项目依赖。

**1. 内置 `ChromaBm25EmbeddingFunction` 的分词器是 `text.lower().split()`**(`chromadb/utils/embedding_functions/schemas/bm25_tokenizer.py`:去标点后按空白切,再过 English Snowball stemmer,缺 `snowballstemmer` 时直接抛错):

| 输入 | 字符数 | BM25 term 数 |
|---|---|---|
| `向山抬起头，看着远处的星舰缓缓降落。基因税是这个时代最沉重的枷锁。` | 33 | **3** |
| `The starship descended slowly over the ridge with heavy gene taxes.` | 67 | 7 |

中文只按标点切成 3 段,每段整体是一个 token。查「基因税」永远匹配不到 term `基因税是这个时代最沉重的枷锁`。BM25 在中文上直接失效。

**2. `$contains` 能做中文子串,但它不是检索通道。** 本地版没有 Cloud 文档写的 3 字面字符下限,实测 `基因税` / `向山` / `税` 都能命中,也能叠 metadata filter(`{"line_end": {"$lte": 60}}`)。但它是 `get()` 上的布尔过滤器——无 score、无序。而 `app._fuse_channels` 是 rank-based RRF:

```python
scores[item.chunk_id] += 1.0 / (RRF_K + rank)
```

它要的是一个**有序 top-k**。`$contains` 给不出排名,`fts --top-k` 和 `hybrid` 的 FTS 通道同时塌掉。

**3. 代价会外溢到图谱决策。** `docs/graph-channel-demotion.md` 记录的图谱降级理由正是「缺的是按语料内稀有度加权的选择函数,而 FTS 通道的 BM25 已经免费提供了它」。丢掉 BM25,`hybrid` 退化成单通道向量检索,而当初被否掉的图谱通道也补不回这个位置。

**4. 补回来只有一条路,而它已经被否过。** 自己引 jieba 分词 + 自己维护 idf / avg_doc_length 算 BM25 稀疏向量,塞进 chroma 的 sparse embedding 字段(`KnnFactory(key="sparse_embedding")`);chroma 另一个稀疏方案 Splade 要 Chroma Cloud API key,与本地优先直接冲突。archive 记的否决理由同样适用——jieba 是全新依赖,且需要自维护中文停用词表。

### 顺带损失与可平迁的部分

会丢:

- `StoreStats.index_completeness` 没有等价物,chroma 只有 `count()`。`status` 检出「索引不是原子发布」的能力只剩 doc 数对账这一半。

不构成障碍(这些是 `ChunkStore` seam 已经买下的):

- `ChunkStore` 只有 6 个方法,`ZvecChunkStore` 约 150 行,换 adapter 本身不贵。
- `ProgressFilter.expression` 那点 SQL 换成 `{"$and": [{"line_end": {"$lte": N}}]}`,实测可用。
- 重建成本只有 12 分钟的 `index`(runbook 基线 3.30 chunks/s):`build_graph` 完全不碰 zvec(只读 `manifest.json` + `chunks.jsonl`),换引擎不需要重跑 7 小时的 `graph-index`。

代价侧另有一条:直接依赖从 3 个变成 chromadb 拖进来的 80 个包(`uv --with chromadb` 实测安装数)。

## 未决 · 可搬运性

这条是 2026-07-27 讨论末尾提出的,**没有评估完**,不适用上面的结论。原话:「复制导出这种大量的碎文件会很痛苦」。

实测文件数:

| 路径 | 文件数 | 目录数 | 大小 | 文件大小中位数 |
|---|---|---|---|---|
| `indexes/sample` | 37 | 6 | 85 MB | **72 B** |
| `metadata/sample` | 3 | 1 | 21 MB | — |
| 全部(5 个 collection) | **154** | — | 123 MB | — |

关键事实:**碎文件几乎全部来自 zvec 自身**,不来自我们写的 JSON。`indexes/sample` 里是 RocksDB 的簿记文件(`idmap.0/` 下的 `CURRENT` / `IDENTITY` / `LOCK` / `LOG` / `MANIFEST-*` / `OPTIONS-*` / `*.sst` / `*.log`)、proxima 向量索引(`embedding.index.2.proxima`)、RocksDB FTS(`fts.1.rocksdb`)、`scalar.0.ipc`。中位文件大小 72 字节,最大 39.6 MB。`metadata/` 那边一共只有 3 个文件。

因此三条路的性质完全不同:

| 路径 | 对碎文件的作用 | 成本 |
|---|---|---|
| 打包导出命令(`export` / `import` 打成单个 tar) | **解决搬运**,不动引擎 | ≈ 30 行 |
| 图谱搬进 zvec | **加重**——再多一个 collection 目录 | 400–600 行 |
| 全部搬进单文件 sqlite | 真正解决 | 要**换掉 zvec 本身**(放弃 proxima ANN + jieba FTS),是换核心依赖的项目 |
| 换成 chromadb | **解决搬运**(实测 200 doc 只落 5 个文件) | 同样是换掉 zvec 本身,代价见「chromadb 评估」 |

也就是说,如果痛点确实是「复制导出」,那么代价最低的解法是打包命令,而不是换存储引擎;而如果目标是「一个 collection 就是一个文件」,那要付的是替换 zvec 的代价,不是加一层 sqlite。这条留待下次讨论。

**2026-08-29 补充**:chromadb 是目前唯一实测过的「一个 collection 近似一个文件」候选。`PersistentClient` 写 200 doc 后落盘 **5 个文件**——`chroma.sqlite3` 加 hnswlib 的 `data_level0.bin` / `header.bin` / `length.bin` / `link_lists.bin`,对比 `indexes/sample` 的 37 个文件、中位 72 B。搬运这条它确实赢,但它要付的是中文 BM25,所以本条未决项仍然未决:**打包命令依旧是代价最低的解法**。

## 已决定要做的(四项均已实施)

1. `derivation.write_json_document` → 临时文件 + `os.replace`(根治撕裂读)
2. 新增 `status` 子命令:操作 + 诊断一体,人类可读,不带 `--json`
3. `ChunkStore` 加第 6 个方法 `stats() -> StoreStats`,让 `status` 能校验索引完整性
4. 诊断数字带实测参考值,超出打 ⚠;参考值来自赛博英雄传 + qwen3:8b,**换书或换模型会误报,是参考不是判死**

### 落地时定下的参考值

`graph.py` 里三个 read-side 常量(不落盘、不进失效指纹,可以随便调):`MIN_DECLARED_ENTITY_SHARE = 0.35` / `MAX_DROPPED_ITEM_SHARE = 0.20` / `MAX_SILENT_CHUNK_SHARE = 0.05`。依据是五个 collection 的实测:

| collection | 已处理 chunk | 已声明实体占比 | 丢弃条目占比 | 零产出 chunk |
|---|---|---|---|---|
| sample | 584 | 48.5% | 6.9% | 0 |
| smoke | 20 | 56.6% | 7.8% | 0 |
| evalp | 7 | 58.5% | 9.0% | 0 |
| toy | 3 | 77.8% | 0.0% | 0 |
| ch5 | 1 | — | — | 1/1 |

已声明占比随 run 变长而**下降**(关系端点桩的累积快于声明),所以阈值卡的是下限而不是区间。丢弃率 6.9%–9.0% 与 README §6.1 记的「约 7%」一致。五个里唯一被标出来的 `ch5` 确实是一个什么都没产出的废 run——阈值在真实数据上既没漏报也没误报。

另外,`status` 的失效判定用**配置里当前的模型与参数**,所以它回答的是「重跑会续建还是从头重来」,而不只是「现在坏没坏」。这是它相对其他入口多做的一件事,也是当初提这个命令的原始动机。

## 什么时候重开这个讨论

任一条成立:

- `graph.json` 全量载入的 RSS 超过 500 MB,或 `graph-query` / `hybrid` 单次调用的反序列化超过 1 秒
- 需要多本书的图谱同时在线(现在是每 collection 一份,一次载一个)
- 「复制导出」的痛点被打包命令验证为**没解决**
- 出现必须按图谱结构做联表/聚合的查询(现在是 Python 全量扫描 + 子串匹配)
- 决定放弃中文 FTS 通道(`hybrid` 只留向量),或者找到愿意长期维护的 jieba + BM25 稀疏编码方案——那时 chromadb 重新成为候选
- zvec 出现维护中断或 Python 版本兼容问题
