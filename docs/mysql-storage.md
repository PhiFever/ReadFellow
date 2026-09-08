# MySQL 存储与查询

ReadFellow 通过 SQLAlchemy Core 读写 MySQL。`MYSQL_URI` 从环境变量读取，未设置时读取当前目录的 `.env`；配置中的 `database_url` 可显式覆盖。连接示例：

```dotenv
MYSQL_URI=mysql+pymysql://user:password@127.0.0.1:3306/readfellow
```

数据库需预先存在。URL 中密码的特殊字符需要 URL 编码，`.env` 已被 Git 忽略。`db-init` 创建缺失的表，不创建数据库，也不修改已有表结构。

```sh
uv sync
uv run readfellow db-init
uv run readfellow import-json --collection sample
```

`import-json` 从 `paths.metadata_dir/<collection>` 读取 manifest、chunks 和存在的 graph / analysis。一次事务提交整个集合，重复导入相同输入会报告 `already imported`。导入不调用 Ollama、不改变 zvec，也不删除文件。引用、hash 或证据不一致时报告错误；旧提示词版本可以存入数据库，但不会因此变成当前有效版本。

完成导入后，原有 `index`、`graph-index`、`analyze`、检索和 `status` 命令直接使用数据库，不需要再次导入。数据库不可用时命令报错，不回退 JSON。

## DataGrip 中的表

| 表 | 用途 |
|---|---|
| `source_versions`、`chunks` | manifest 与按源数据版本保存的原文切块 |
| `runs` | 每个集合的图谱/章节分析运行历史、参数、版本和进度 |
| `entities`、`entity_values` | 实体名称、别名和类型；`kind` 区分别名与类型 |
| `entity_mentions`、`entity_evidence` | 实体的提及位置和原文证据 |
| `relations` | 有向关系、两端实体、证据和 chunk 引用 |
| `extractions`、`run_chunks` | 已处理 chunk 的计数与指纹 |
| `chapters`、`chapter_chunks` | 章节摘要及所覆盖的 chunk |
| `characters`、`events` | 章节中的人物和事件及证据 |
| `artifact_imports` | 已导入输入的指纹，防止重复导入 |

派生记录的 `position` 是内容身份的稳定摘要，`sort_order` 是保存列表顺序的辅助值；图谱实体按名称、别名与类型按值、提及与证据按 chunk 和行号还原顺序。`entity_position`、`subject_position`、`object_position`、`chapter_position` 与对应表的 `position` 关联时，必须同时关联 `run_id`。关系端点没有可关联的声明实体时允许为空，不会为了建外键丢掉关系。

联查原文使用 `(source_version_id, chunk_id)`，对应 `chunks` 的 `(source_version_id, id)`。不要只关联 chunk id：重新切块时，同一个 id 可能对应不同文本。

[mysql-queries.sql](mysql-queries.sql) 包含可直接执行的版本列表、一跳、二跳、分组统计、别名、章节分析和跨版本对比示例。DataGrip 查询面向人工全量分析，不自动执行 CLI 的防剧透限制。

## 版本和事务

- `run_id` 是 `runs.id`，与格式版本、提示词版本不同。图谱和章节分析按各自的 `kind` 选择最大的 id。
- 重建创建新 run；中断续建沿用同一 run。最新 run 即使为空、失败或未完成，CLI 也不回退旧版。
- 每个成功的 chunk / 章节及其处理记录在同一事务提交；模型调用发生在事务外。持久化层只更新变化的关系记录，原文快照和其他运行版本不被覆盖。
- 历史 run 保留对应的原文切块。只有未失效且已处理 chunk 一致的续建，才允许关联增加了新 chunk 的源数据版本。
- MySQL 与 zvec 不在同一事务中。索引中途失败时仍需用 `status` 检查两者的存量是否一致。

## 验证

```sh
uv run pytest -q                              # 离线，不使用本机 MySQL 或 Ollama
uv run pytest tests/test_artifacts.py --mysql -q
```

`--mysql` 使用 `MYSQL_URI` 对应服务，但每个测试创建独立的随机测试数据库，结束后删除该测试库；账号需要建库和删库权限。它验证真实 MySQL 上的关系联查、历史保留、最新版本、事务回滚和导入，不把 SQLite 通过当成 MySQL 验收。
