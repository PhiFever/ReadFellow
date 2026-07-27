# 存储引擎:为什么现在不引入 sqlite

2026-07-27 的讨论存档。起因是 `graph-index` 跑完之后没有任何办法验证结果,顺带问出「是否值得引入 sqlite 获得更好的事务和文件迁移能力」。

结论:**现在不引入 sqlite,也不把图谱搬进 zvec。** 但**可搬运性**这条动机在讨论末尾才提出来,没有评估完,它是唯一可能推翻本结论的方向——见「未决」。

## 不重新讨论的事

1. **要搬只能搬 zvec,不能上 sqlite。** sqlite 在这里能给的能力 zvec 全给,而且已经在依赖树里。引入 sqlite = 仓库里两个存储引擎。八项探针实测见下。
2. **写放大不是理由。** 每 chunk 整文件重写累计约 40 GB 看着吓人,换算成时间是 8 小时 run 里的 9 分钟(1.8%)。其余 98% 在等 Ollama。
3. **「迁移能力」归错了地方。** 8 小时重建的触发器是 `graph.py` 里的一行 policy(`if graph.schema_version != GRAPH_SCHEMA_VERSION`),不是格式限制。给模型加一个带默认值的可选字段,旧 `graph.json` 直接能载入,根本不需要 bump。而真正必须 bump 的是**语义**改动(prompt 变了 → 抽取结果就该不一样),那个**没有任何存储引擎能迁移**——新字段的值只有 LLM 能给。
4. **撕裂读用 `os.replace` 解决,不需要引擎。** 三行:临时文件 + 原子 rename。

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

也就是说,如果痛点确实是「复制导出」,那么代价最低的解法是打包命令,而不是换存储引擎;而如果目标是「一个 collection 就是一个文件」,那要付的是替换 zvec 的代价,不是加一层 sqlite。这条留待下次讨论。

## 已决定要做的

1. `derivation.write_json_document` → 临时文件 + `os.replace`(根治撕裂读)
2. 新增 `status` 子命令:操作 + 诊断一体,人类可读,不带 `--json`
3. `ChunkStore` 加第 6 个方法 `stats() -> StoreStats`,让 `status` 能校验索引完整性
4. 诊断数字带实测参考值,超出打 ⚠;参考值来自赛博英雄传 + qwen3:8b,**换书或换模型会误报,是参考不是判死**

## 什么时候重开这个讨论

任一条成立:

- `graph.json` 全量载入的 RSS 超过 500 MB,或 `graph-query` / `hybrid` 单次调用的反序列化超过 1 秒
- 需要多本书的图谱同时在线(现在是每 collection 一份,一次载一个)
- 「复制导出」的痛点被打包命令验证为**没解决**
- 出现必须按图谱结构做联表/聚合的查询(现在是 Python 全量扫描 + 子串匹配)
