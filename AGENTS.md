# AGENTS.md

## 项目定位

ReadFellow 是一个本地优先的 CLI 工作流：把长文档（主要是中文小说）切块、用 Ollama 生成 embedding、存进 zvec 本地向量库，让 agent 能按语义/全文检索出**原文段落**，并带着精确出处与用户讨论。

三条贯穿全代码的不变量：

1. **源文档是唯一事实依据**；index、metadata、analysis 都是派生物，派生物 stale 时 fail closed，不猜测。
2. **检索只是导航，原始 chunk 才是证据**。返回给用户的内容都回落到存储的 chunk 原文 + `source_path:line_start-line_end`。
3. **防剧透优先**。有阅读进度限制时，超出进度的文本绝不出现在输出里，包括没有逐值 provenance 的聚合信息。

## 常用命令

Python 一律走 `uv`。`index` / `search` / `hybrid` 要本地 Ollama 的 embedding 模型，端点在 `config.yaml`。子命令和参数看 `uv run readfellow --help` 或 `README.md`。

```sh
uv run readfellow index corpus/samples/<doc>.txt --collection sample --rebuild --limit 8  # 冒烟索引前 8 个 chunk
uv run readfellow hybrid "问题" --collection sample --top-k 5 --max-chapter 1:50       # 检索 + 进度限制
uv run readfellow status --collection sample                                            # 集合体检
uv run pytest                                        # 全量，离线，不需要模型服务；pre-commit hook 会跑它
uvx ruff format . && uvx ruff check .                # ruff 不进 dev 依赖；import 边界由 TID251 检查
```

## 模块地图

CLI 只调用 app 工作流；app 编排领域逻辑并调用存储与模型接口。

- `cli.py`：argparse 薄 adapter。
- `app.py`：CLI 之外唯一该调用的入口（`index_document`、`semantic_search`、`hybrid_search`、`collection_status` 等）。
- `chunking.py`：切块与章节识别。`progress.py`：由「卷:书内章号」或行号算出 `ProgressFilter`。
- `store.py`：唯一接触 zvec 的模块，对外是 `ChunkStore` Protocol。
- `library.py` 负责 SQLite 表结构、事务和检索，是唯一接触 sqlite3 的模块。
- `ollama.py` / `openai_compat.py`：embedding 与生成接口。
- `analysis.py`：章节提示词、解析和引文锚定；`extraction.py`、`derivation.py` 提供抽取工具与派生骨架。
- `models.py`：所有 Pydantic 模型。

当前索引存放在 `library/<collection>.db`。第 3 步对照用的旧库位于 `indexes/<collection>/`。

## 文档

- 改 `index`、检索命令、进度过滤、`store.py`、`library.py` 或 `status` 前，读 `docs/retrieval.md`。
- 改章节分析的提示词、解析或引文锚定前，读 `docs/derivation.md`。
- 提议改设计前，查 `docs/decisions.md`：已定的决策各一行，附原文出处。
- 审查 diff 时按 `CODING_STANDARDS.md` 逐条核对。
- 当前进行中的工作是 `docs/spec/reading-architecture-plan.md`：动存储、进度、命令、`analyze`、章节识别或检索排序前读它，实施从「执行顺序」里状态行指明的那一步接着做。它引用的 `analyze` v2、身份归并、剧情段、评估集设计在 `docs/spec/narrative-index-plan.md`。
- 跑全量或估算耗时，看 `docs/mvp-runbook.md`。
- `docs/archive/` 是已完成的 spec，只在 `decisions.md` 指过去时读。`docs/architecture-archive.md` 是早期架构记录，同样按需读。
- 回答用户关于语料内容的问题时，按 readfellow skill（`.claude/skills/readfellow`）执行。
