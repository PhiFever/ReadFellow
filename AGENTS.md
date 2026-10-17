# AGENTS.md

## 项目定位

ReadFellow 是一个本地优先的 CLI 工作流：把长文档（主要是中文小说）切块、用 Ollama 生成 embedding、存进 zvec 本地向量库，让 agent 能按语义/全文检索出**原文段落**，并带着精确出处与用户讨论。

三条贯穿全代码的不变量：

1. **源文档是唯一事实依据**；index、metadata、graph 都是派生物，派生物 stale 时 fail closed，不猜测。
2. **检索只是导航，原始 chunk 才是证据**。返回给用户的内容都回落到存储的 chunk 原文 + `source_path:line_start-line_end`。
3. **防剧透优先**。有阅读进度限制时，超出进度的文本绝不出现在输出里，包括 graph 的别名、类型这类没有逐值 provenance 的聚合信息。

## 常用命令

Python 一律走 `uv`。`index` / `search` / `hybrid` 要本地 Ollama 的 embedding 模型，`graph-index` 要本地生成模型，端点在 `config.yaml`；`analyze` 默认走云端，需要 `LLM_API_KEY`（环境变量或 `.env`）。元数据存 MySQL，连接读 `MYSQL_URI`（环境变量或 `.env`）。子命令和参数看 `uv run readfellow --help` 或 `README.md`。

```sh
uv run readfellow index corpus/samples/<doc>.txt --collection sample --rebuild --limit 8  # 冒烟索引前 8 个 chunk
uv run readfellow hybrid "问题" --collection sample --top-k 5 --max-chapter 1:50       # 检索 + 进度限制
uv run readfellow status --collection sample                                            # 集合体检
uv run pytest                                        # 全量，离线，不需要模型服务；pre-commit hook 会跑它
uv run pytest tests/test_artifacts.py --mysql -q     # 在随机 MySQL 测试库验收，结束后删库
uvx ruff format . && uvx ruff check .                # ruff 不进 dev 依赖；import 边界由 TID251 检查
```

## 模块地图

CLI 只调用 app 工作流；app 编排领域逻辑并注入存储与模型接口。

- `cli.py`：argparse 薄 adapter。
- `app.py`：CLI 之外唯一该调用的入口（`index_document`、`semantic_search`、`hybrid_search`、`build_graph`、`build_analysis`、`collection_status` 等）。
- `chunking.py`：切块与章节识别。`progress.py`：由「卷:书内章号」或行号算出 `ProgressFilter`。
- `store.py`：唯一接触 zvec 的模块，对外是 `ChunkStore` Protocol。
- `artifacts.py` / `artifact_schema.py`：MySQL 元数据与派生物的唯一持久化入口。`legacy_import.py`：一次性导入旧 JSON。
- `ollama.py` / `openai_compat.py`：embedding 与生成接口。
- `graph.py` / `analysis.py`：两条对称的派生管线；`extraction.py`、`derivation.py` 是它们共用的工具与骨架。
- `models.py`：所有 Pydantic 模型。

产物：`indexes/<collection>/` 是当前 zvec 索引；MySQL 存元数据和全部派生运行历史；`metadata/<collection>/` 只留旧 JSON 备份和评估集（git 忽略）。

## 文档

- 改 `index`、检索命令、进度过滤或 `store.py` 前，读 `docs/retrieval.md`。
- 改 `graph-index`、`analyze`、失效指纹、MySQL 持久化或 `status` 前，读 `docs/derivation.md`。
- 提议改设计前，查 `docs/decisions.md`：已定的决策各一行，附原文出处。
- 审查 diff 时按 `CODING_STANDARDS.md` 逐条核对。
- 当前进行中的工作是 `docs/spec/narrative-index-plan.md`：动 `analyze`、章节识别、图谱或检索排序前读它，实施从「执行顺序」里状态行指明的那一步接着做。
- 跑全量或估算耗时，看 `docs/mvp-runbook.md`。用 DataGrip 查 MySQL，看 `docs/mysql-storage.md`。
- `docs/archive/` 是已完成的 spec，只在 `decisions.md` 指过去时读。`docs/architecture-archive.md` 是早期架构记录，同样按需读。
- 回答用户关于语料内容的问题时，按 readfellow skill（`.claude/skills/readfellow`）执行。
