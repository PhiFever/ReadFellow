# ReadFellow

ReadFellow 是一个本地优先的 CLI 工作流：把长文档（主要是中文小说）切块、用 Ollama 生成 embedding、存进每本书独立的 SQLite 库，让 agent 能按语义 / 全文检索出**原文段落**，并带着精确出处（`source_path:line_start-line_end`）与用户讨论。

三条贯穿全流程的不变量：

1. **源文档是唯一事实依据**。index、metadata、analysis 都是派生物，派生物 stale 时 fail closed，不猜测。
2. **讨论内容要有原文依据**。按章节阅读或检索到段落后，引用书库中的原文和对应的源文件行号。
3. **防剧透优先**。读取前必须设置进度，输出只包含进度以内的内容。

---

## 1. 前置条件

**Python 依赖**走 `uv`，无需手动装包（`pyproject.toml` 已配 `pythonpath = ["src"]`）：

```sh
uv sync
```

**Ollama** 必须运行在 `config.yaml` 配置的端点上，并已拉取 embedding 模型：

```sh
ollama serve                        # 另开一个终端
ollama pull qwen3-embedding:8b      # embedding，4096 维
```

自检（返回模型列表即正常；连不上会是 `503`）：

```sh
curl -s http://127.0.0.1:11434/api/tags
```

`index`、`search` 和 `hybrid` 需要 embedding 服务。其他读取命令、进度管理、`status` 和测试都可以离线运行。

---

## 2. 五分钟上手

```sh
# 1) 冒烟索引前 12 个 chunk，确认 Ollama 通路正常
uv run readfellow index corpus/samples/赛博英雄传.txt --collection smoke --rebuild --limit 12

# 2) 按实际阅读进度设置边界，例如已读完第一卷第二章
uv run readfellow progress set 1:2 --collection smoke
uv run readfellow toc --collection smoke
uv run readfellow read --chapters 2 --collection smoke
uv run readfellow find 尤基 --collection smoke

# 语义检索
uv run readfellow search "尤基的父亲" --collection smoke --top-k 2

# 3) 中文全文检索
uv run readfellow fts "回收站镇" --collection smoke --top-k 2

# 4) 融合检索（向量+全文打分）
uv run readfellow hybrid "基因税" --collection smoke --top-k 3

# 5) 将下面的 id 换成上一步返回的 id，取回原文
uv run readfellow fetch bdd935754e17_000001 --collection smoke
```

去掉 `--limit 12` 再跑一次 `index`，即可在冒烟库上补齐全书。续跑时省略 `--rebuild`。

---

## 3. 命令手册

所有默认值都取自根目录 `config.yaml`；命令行参数只覆盖单次运行。

### 3.1 全局参数

置于子命令**之前**，对所有子命令生效：

| 参数 | 默认值来源 | 说明 |
|---|---|---|
| `--config` | `config.yaml` | 换一份配置文件 |
| `--library-dir` | `paths.library_dir` | 书库目录，默认 `library/` |
| `--ollama-url` | `ollama.base_url` | Ollama 端点 |
| `--model` | `ollama.embedding_model` | 索引使用的 embedding 模型 |
| `--keep-alive` | `ollama.keep_alive` | 模型驻留时间 |

```sh
uv run readfellow --ollama-url http://192.168.1.10:11434 search "问题" --collection sample
```

### 3.2 `index` — 切块并索引

```sh
uv run readfellow index <文档.txt> --collection sample --rebuild
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--collection` | `sample` | 集合名，支持字母、数字、下划线和连字符 |
| `--chunk-chars` | 2400 | 目标 chunk 字符数 |
| `--overlap-chars` | 240 | 相邻 chunk 的尾部重叠 |
| `--batch-size` | 8 | 每批 embedding 数量 |
| `--limit` | 0（不限） | 只索引前 N 个 chunk |
| `--rebuild` | off | 重建集合，换模型 / 换切块参数时必须加 |
| `--no-optimize` | off | 跳过最后的 FTS5 优化 |

每批成功后，原文、分词索引和向量一起提交。重跑会跳过已存且文本哈希一致的 chunk。源文件、切块参数、模型或维度改变时需要 `--rebuild`。

### 3.3 `search` / `fts` / `hybrid` — 三种检索

```sh
uv run readfellow search "要查询的问题" --collection sample --top-k 5   # 向量语义
uv run readfellow fts    "关键词"       --collection sample --top-k 5   # jieba 分词和 FTS5 全文检索
uv run readfellow hybrid "问题或关键词" --collection sample --top-k 5   # 向量+全文融合
```

`hybrid` 用向量和全文两路打分，融合为同一份证据列表：

```
channels: vector=12, fts=7
```

命中项带 `matched: vector#1, fts#3`，标明它在两条打分通道里的排名。

### 3.4 `fetch` — 按 id 取回原文

```sh
uv run readfellow fetch <chunk-id> --collection sample
```

打印完整原文（不截断）。三种结果：`found`、`not_found`（无此 chunk）、`outside_progress`（超出阅读进度，**不带原文**）。

### 3.5 `status` — 查看索引进度

```sh
uv run readfellow status --collection sample
```

输出包含源路径、全书章节数、切块参数、向量模型和维度。`index: 8/16 docs` 表示应有 16 个 chunk，已经提交 8 个。沿用原参数再跑 `index`，省略 `--rebuild`，即可续上。

### 3.6 `toc` / `read` / `find` — 按章节阅读

`toc [--chapters A-B]` 给出目录序号、卷章、标题、行范围和可读字数。用 `read --chapters A-B` 按目录序号翻阅，或用 `read --lines A-B` 按源文件行号读取。两个范围都接受单个数字。输出超过 60000 字符时可缩小范围继续阅读。

`find 词 [--chapters A-B] [--limit N]` 在章节原文中查找区分大小写的精确子串，汇总可读范围里的命中行数和每章计数，默认展示前 50 条。整章可读且摘要通过防剧透检查时，`find` 在每章计数后附上摘要，`toc` 同时列出摘要和标签。三个命令都支持 `--json`。

`uv run readfellow notes --collection sample --chapters 2-5` 按目录序号查看关键时刻和别名关系。每条附原文、源路径和行号。只列整章已读的章节，没有可见条目时显示 `no notes`。支持 `--json`，超过 60000 字符时会提示缩小范围。

`uv run readfellow aliases 铁拳 --collection sample` 查询进度内已确认的别名，包括通过其他称呼连起来的关系。每对称呼附最早的引文、章节和源文件行号。支持 `--json` 和与 `read` 相同的单次进度限制。

### 逐章生成摘要和笔记

`uv run readfellow analyze --collection sample [--chapters A-B]` 将所选章节的全文发送到配置的 OpenAI 兼容端点。默认模型为 DeepSeek-V4-Flash，密钥取自环境变量或项目 `.env` 中的 `LLM_API_KEY`。省略范围时处理全书，生成范围独立于阅读进度。先生成摘要，再逐章生成关键时刻和别名关系。两轮独立缓存，每章成功后立即保存。已有的 6a 摘要可以直接复用。最后从所有已有 moments 笔记的章节中找出别名候选，逐条判定。即使指定了 `--chapters`，这一步也覆盖所有有笔记的章节。命令汇报三轮结果，links 一行列出候选、缓存命中、新判定、失败和确认的数量。任一轮失败都会列出类型、章节序号和原因，退出码为 1。

`status` 的 `summaries` 行显示当前配置已生成多少章、还缺多少章，以及有多少章因剧透隐藏了摘要。`moments` 行列出已有笔记和待生成的章数、因剧透隐藏的关键时刻条数，以及因引文或人物称呼检查失败而丢弃的条数。`links` 行显示本轮候选中已有判定的条数，以及其中已确认的条数。

## 4. 防剧透：阅读进度限制

每本书单独保存进度。首次读取前运行 `progress set V:N` 或 `progress set --line N`。已读完全书可用 `progress set --all`。用 `progress show` 查看。读取命令都接受以下单次限制。

| 参数 | 语义 |
|---|---|
| `--max-chapter N` 或 `V:N` | 按书内章号定位，可以加卷号（如 `2:50`）。卷号由章号回到「第一章」推出。撞号时报错，并列出每个候选对应的 `--max-line`。只用结束行在下一节标题之前的 chunk；下一节不一定有章号，比如作者的话 |
| `--max-line N` | 只用结束行号 ≤ N 的 chunk |
| `--max-chunk-index N` | 只用 `chunk_index` ≤ N 的 chunk |

```sh
uv run readfellow search "他的真实身份"  --collection sample --max-chapter 1:50
uv run readfellow hybrid "武神"          --collection sample --max-chapter 1:50
uv run readfellow fetch  <chunk-id>      --collection sample --max-chapter 1:50
```

检索在 SQL 中按结束行和 chunk 序号过滤，`fetch` 在输出前检查进度。省略单次参数时使用已存进度。要读到更远处，需要重新运行 `progress set`。

---

## 5. 书库与源文件

`library/<collection>.db` 保存书目、完整章节、chunk 原文、FTS5 索引和向量。可以单独复制这个文件。源文件在场时会核对哈希，内容发生变化后需要重新运行 `index --rebuild`，然后重新设置进度。源文件缺失时仍可用书库中的行号限制进度，检索和取回原文。

## 6. 开发

```sh
uv run pytest                                   # 全量测试（约 3s，完全离线）
uv run pytest tests/test_analysis.py -q         # 单文件 / 单用例
uvx ruff format . && uvx ruff check .           # 格式化与 lint，两者当前均 clean
```

架构与设计文档见 [`CLAUDE.md`](CLAUDE.md)、[`AGENTS.md`](AGENTS.md) 和 [`docs/`](docs/)。
