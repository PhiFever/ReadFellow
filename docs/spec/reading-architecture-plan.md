# 阅读架构规格：章节为骨架、单文件存储、事件图

日期：2026-11-16
状态：**第 1 步已完成**，从文末「执行顺序」第 2 步接着做。
来源：一次架构评审会话。前提是只依据项目最初的目的（让 LLM 能读不在训练数据里的小说并与用户讨论），允许推翻此前任何文档结论。

与 `narrative-index-plan.md` 的关系：**本文件接管执行顺序**。那份计划里 `analyze` v2、人物身份归并、剧情段、Q15 人名检查、评估集的设计仍然有效，本文件不重复，只在「对叙事索引计划的改动」里列出被改写的条目。两边冲突时以本文件为准。

领域文档（`docs/retrieval.md`、`docs/derivation.md`、`CODING_STANDARDS.md`、`AGENTS.md` 的模块地图）描述的是当前代码，随各步的代码改动同步更新，不提前改。

## 核心判断

现在的主干是检索引擎：切块 → embedding → top-k → 逐块 `fetch`。讨论一本没读过的小说，要的是**读过这本书的记忆，加上能翻回原文核对**。人物弧光、某段剧情、伏笔这类问题靠连续阅读和分层笔记回答，相似度 top-k 只解决「某件事在哪」。所以主干换成「章节结构 + 连续阅读 + 逐章笔记 + 事件图」，检索降为辅助工具。

## 不重新讨论的事

- **章节是一等公民，chunk 只是索引的实现细节。** 进度、笔记、阅读、引文都以章节为单位；chunk 只服务 embedding 和全文检索。
- **阅读进度持久化，默认生效，未设置时拒绝读取。** 防剧透不再依赖 agent 每次记得带参数。
- **派生笔记一章一行，以章节内容哈希和生成配置为键。** 不再有「整份文档失效」。
- **删除现有图谱实现。** 事件图建在逐章笔记之上：节点是笔记里的事件，边只从后面的章节指向前面的章节。
- **人物关系不建成静态边。** 人物是事件的参与者，两个人物的关系就是他们共同参与的事件序列，关系变化本身是一个事件。
- **每本书一个 SQLite 文件。** 向量存 BLOB、用 numpy 暴力检索。去掉 zvec、MySQL、SQLAlchemy、pymysql。
- **不做跨书联合检索。** 用户确认这个场景不会出现，所以不为它预留向量扩展。
- **人工修正章节识别通过改源文件完成。** 源文档是唯一事实依据，不引入覆盖表。

## 已核实的事实

**在这台机器上实测**（2026-11-16，树莓派，Python 自带 SQLite 3.46.1；没有 MySQL，没有 Ollama）

- **现有持久化层已经能跑在 SQLite 上。** `MYSQL_URI=sqlite:///<path>` 时 `db-init` 建表成功，`status` 能读库并按预期报「collection 没有索引」。`artifacts.make_engine` 本来就处理了 SQLite，测试默认也跑在 SQLite 上（`tests/conftest.py`）。
- **FTS5 的 trigram 分词不能用于中文人名。** 两个字的「尤基」`MATCH` 返回 0 条，`LIKE '%尤基%'` 命中；三个字以上 `MATCH` 才命中。
- **预分词后存进 FTS5 可用。** 把文本按词用空格隔开存入（默认 unicode61 分词器），两字词 `MATCH` 命中，`bm25()` 可排序。测试用的是手工分好词的文本，没有装 jieba。
- **暴力向量检索够快。** 2307 条 × 4096 维 float32（随机数据，规模取自 `narrative-index-plan.md` 记录的全书 chunk 数）：从 SQLite 读出全部向量 109 ms，带进度掩码算内积并取 top-50 用 11 ms。读取用的是内存库，SD 卡上的耗时没测。
- **numpy 目前是 zvec 间接带进来的**（`uv tree --package zvec`），删 zvec 时要加为直接依赖。

**读代码得到的事实**

- chunk 原文存了三份：源文件、zvec 的 `text` 字段（`store.py`）、MySQL 的 `chunks` 表（`artifact_schema.py`）。`IndexReport.is_complete`（`app.py`）是为这种跨库不一致加的检查。
- 所有命令都要连数据库，包括 `fetch`：`app.py` 里每个入口都先 `open_artifacts(config)`。
- 进度是每条命令的可选参数，不传就是全书（`app.progress_filter_from_limit`）。
- 章节结构在三处各算一遍：`chunking.iter_headings`、每次查询重新扫源文件的 `progress.chapter_boundaries`、从 chunk 标题字符串反推的 `analysis.group_chapters`。
- `analyze` 的产物没有只读入口，唯一打印它的是构建命令本身（`cli.command_analyze`）。
- 笔记的有效性绑定在 chunk id 和 chunk 哈希上（`analysis.analysis_staleness_reason`）。chunk id 以源文件哈希为前缀，所以改切块参数或改源文件任何一处，整份笔记都失效。
- 派生物在内存里是一整份文档，保存时与快照逐表 diff（`artifacts._save`），因此需要 `app._Checkpoint` 节流。
- 图谱抽取以单个 chunk 为单位（`graph.build_extraction_prompt`），每条关系只锚定一个 chunk（`models.GraphRelation`），查询是子串匹配（`graph._matches_entity`、`_matches_relation`），没有遍历。
- 检索结果只显示前 260 字（`cli.print_evidence`），要全文得逐条 `fetch`。

**没有验证的**

- Python 的 jieba 与 zvec 内置分词的结果差多少。第 3 步对照时量。
- 模型能否在大量旧事件里认出该连的那条。第 8 步先试跑。
- 「章节表 + 读原文 + 精确搜索」是否不输 `hybrid`。第 5 步量。

## 设计

### 一本书一个文件

路径 `library/<collection>.db`，git 忽略。表分三组：

- **索引组**，`index --rebuild` 清空重写：
  - `book`：源路径、源文件哈希、切块参数、embedding 模型与维度、应有的 chunk 数。
  - `chapters`：序号、卷、书内章号、标题、行区间、内容哈希、原文（含空行）。
  - `chunks`：所属章节、行区间、正文、分词后的文本、向量。
  - chunk 的 FTS5 全文索引。
- **笔记组**，重建索引不碰：生成配置、逐章笔记及其条目、事件边、剧情段。见「逐章笔记缓存」和「事件图」。
- **读者状态**：阅读进度。

约定：

- chunk 行和向量在同一个事务里写入，不会出现有元数据没向量的行。中断的索引表现为行数不足，`status` 照常报告 n/N，再跑 `index` 续上。
- 库里存章节原文，读取命令不依赖源文件在场。这样把文件拷到另一台机器后，目录、读原文、读笔记、精确搜索都能用；只有向量检索需要 embedding 模型。
- **源文件校验**：源文件在场且哈希与库里记录的不一致时，一律 fail closed，要求重新 `index`。源文件不在场时照常读库。理由：进度改由库里的章节表换算，和存储的行号永远在同一坐标系，旧文档担心的「两套行号错位放行越界章节」不再可能发生；哈希不一致只说明索引落后于源文件。
- 用标准库 `sqlite3`。只剩一个后端，不需要 SQLAlchemy 这层抽象。

### 章节表

- 识别规则不变，沿用 `narrative-index-plan.md` 第 2 步定下并实测过的规则（分隔线后的首个非空行，或匹配 `CHAPTER_RE` 的行）。
- 索引时算一次，写进 `chapters`。进度换算、笔记分组、读取命令都读这张表，`progress.chapter_boundaries` 和 `analysis.group_chapters` 随之删除。
- 章节内容哈希取该章原文（标题行到下一个标题前一行）的 sha256。

### 进度

- `progress set <卷:章号>`、`progress set --line N`、`progress set --all`（已读完）、`progress show`。存在本书的库里，同时记下设置时的源文件哈希。
- 读取类命令默认套用已存的进度。**从未设置时报错并提示先设置**，不默认为全书。
- 单次命令的 `--max-chapter` / `--max-line` 只能比已存进度更严，超出时报错。要读得更远，只能 `progress set`。
- 重新 `index` 后源文件哈希变了，已存进度失效，读取命令报错直到重新设置。
- 构建命令（`index`、`analyze`、连边）处理全书，不看阅读进度；只构建一部分时用章节范围参数。它们只打印计数，不打印笔记内容。
- **局限**：CLI 模式下 agent 仍然能直接读源文件，这一点只能靠 `SKILL.md` 约束。硬隔离需要把语料放在 agent 工作目录之外并只暴露工具，见「未决」里的 MCP。

### 读取命令

全部套用进度，全部支持 `--json`。章节范围以 `toc` 给出的序号为准，因为书内章号有撞号（见 `narrative-index-plan.md` Q5）。

| 命令 | 返回 |
|---|---|
| `toc` | 进度内的章节表：序号、卷:章号、标题、行区间、字数、是否已有笔记 |
| `read --chapters a-b` 或 `--lines a-b` | 带行号的原文，截到进度以内。超过 6 万字时截断并提示缩小范围 |
| `find <词>` | 精确子串搜索，按出现位置排序，给出行号、所在章和每章命中数 |
| `notes --chapters a-b` | 逐章梗概与条目。整章都在进度内才显示。第 6 步提供 |

`read` 取代 `narrative-index-plan.md` 里的 `fetch-range`。「前情提要」由 `notes` 读进度内的全部章梗概解决，不另建派生物。

### 检索

- **向量通道**：读出进度内 chunk 的向量，numpy 算内积取 top-k。进度过滤是布尔掩码，天然精确。
- **关键词通道**：索引时用 jieba 分词，词间加空格存进 FTS5；查询同样分词后用 `bm25()` 排序。目的是在换存储时保持 `hybrid` 的行为接近 zvec 的 jieba 全文检索，避免同时改变检索质量。jieba 成为直接依赖。
- **精确词**走 `find`，不经过分词。人名、物品、术语不会因为分词而漏掉。
- `search` / `fts` / `hybrid` / `fetch` 的命令名和输出在第 10 步评估之前保持不变，只是改读新库并套用已存进度。这样第 5 步量的旧系统基线和原来是同一口径。
- `ChunkStore` 接口保留，`SqliteChunkStore` 是新的生产实现。

### 逐章笔记缓存

- **生成配置**一行记录一组指纹：prompt 版本、schema 版本、模型、接口地址、`response_format`、影响输出的采样参数。口径沿用 `derivation.settings_changed`，`retries` 和 `num_ctx` 不算。
- **逐章笔记**的主键是「生成配置 + 章节内容哈希」。
- **有效性**：读取时用「最新的生成配置 + 当前章节哈希」查，查不到就是未分析。改源文件只影响内容变了的章，改切块参数不影响任何笔记。旧配置的行留在库里，不删。
- **续跑**就是跳过已有的键。每章的写入是一个事务。并发只用于模型调用，写入仍是单线程（沿用现有决策）。
- **`analyze` 不再经过 chunk。** prompt 直接给章节原文，引文在章节原文里定位（沿用 `extraction.locate_evidence` 的宽松匹配和「单条对不上只丢该条并计数」）。
- **引文位置存章内相对行号**，读取时由章节表换算成源文件行号。原因：前面的章节增删了行，本章哈希不变，绝对行号却会错位。
- 随之删除：`app._Checkpoint`、两个 `*_staleness_reason`、`update_*_metadata`、`ArtifactStore.prepare` / `StoredRun` / 快照 diff、`legacy_import.py`。

### 事件图

- **节点**是 `analyze` v2 的 moments（定义见 `narrative-index-plan.md`「`analyze` v2」），不另建事件抽取管线。时间顺序由章节序号给出。
- **边只允许从较晚章节的事件指向较早章节的事件**，含义是「承接或回收」。每条边带较晚一端所在章的原文引文，锚定规则同其他条目。
- 由此两个性质自动成立：
  - 无环由构造保证。
  - 一条边在进度覆盖它较晚一端所在的整章时可见，不需要另外的防剧透规则。
- **构建**是按章节顺序串行的一遍，在 moments 和人物身份归并之后。输入是本章正文、本章 moments、候选的旧事件；输出是边。
- **键**是「生成配置 + 前缀哈希」。前缀哈希由第 1 章到本章的内容哈希按顺序再哈希得到。前面任何一章内容变了，之后各章的连边都算未做。
- **查询**：`trace <事件>` 沿边回溯到源头，`--forward` 看进度内的后续。没有遍历命令就不建边。
- **局限**：边表示叙事上的承接，不严格等于故事时间里的因果，遇到倒叙两者会不一致。

### 删除的东西

- 图谱：`graph.py`、`graph-index`、`graph-query`、`hybrid` 的图谱标注、`config.graph`、相关模型和测试。`extraction.py` 留下，`analyze` 还在用。现有图谱在 `narrative-index-plan.md` 第 2 步重建索引后已经失效，标注已停，删除不改变 `hybrid` 当前的输出。
- 存储：zvec、MySQL、`artifacts.py`、`artifact_schema.py`、`legacy_import.py`、`db-init`、`import-json`、`docs/mysql-*.md`。
- `analyze` v1 的编排和持久化在第 2 步下线，第 6 步以 v2 重新上线。v1 的产物本来就没有只读入口，现有 run 也已失效，中间这段不损失功能。`analysis.py` 里 prompt、解析、锚定的纯函数和它们的单元测试留着，供 v2 改写。

## 对叙事索引计划的改动

- **「图谱冻结」（Q8）** 改为删除。原第 8 步「用户决定图谱是否退役」不再需要。
- **「原地升级 `analyze`」（Q12）** 仍然成立，但持久化改成「逐章笔记缓存」。`ANALYSIS_SCHEMA_VERSION` / `ANALYSIS_PROMPT_VERSION` 成为生成配置指纹的一部分。
- **引文行号**：原文要求解析到精确的源文件行号，现在存章内相对行号。
- **`fetch-range`** 改名 `read`，6 万字的输出预算不变。
- **查询命令（Q16）** 增加 `toc`、`find`、`notes`、`trace`、`progress`。`arcs`、`arc`、`timeline` 不变。
- **进度**：评估框架里「包装脚本强制加进度」变成产品自身的行为。
- **「检索粒度解耦」** 从搁置里拿出一半：引用、进度、抽取、阅读已与 chunk 解耦；embedding 的粒度仍然搁置，等评估结果。
- **评估集（原第 3 步）** 增加一个被测系统「无索引」：只用 `toc`、`read`、`find`。
- **原第 0 步** 里「API key 和 `MYSQL_URI` 一样从环境变量读」：`MYSQL_URI` 不再存在，API key 的读法不变。
- **Q13 的别名表** 多一个用途：`find` 按进度内已确立的别名展开查询。

## 未决

- **连边的候选怎么收窄。** 初始方案是「与本章事件有共同人物的旧事件（按进度内的别名表）+ 最近若干章的事件」。试跑后定。
- **连边与身份归并第二遍是否合成一次调用。** 两者都按章节顺序串行。身份归并第二遍刻意不给正文，连边需要正文来取引文，所以初始方案是分开。
- **embedding 的去留。** 第 5 步和第 10 步的评估结果出来后由用户决定。如果「无索引」不输 `hybrid`，向量通道和查询时常驻的 embedding 模型可以整体变成可选。
- **MCP 适配器做不做。** `app.py` 已经是干净的入口，适配成本低；它带来的是硬隔离（agent 拿不到源文件）和免去每次调用的进程启动。放在最后，由用户决定。

## 搁置与剪掉

**搁置**

- **embedding 粒度**（小段检索、大段给上下文）：等评估显示召回不够再开。
- **前缀摘要树、预写的人物小传**：沿用 `narrative-index-plan.md` 的搁置理由。
- **检索命令收敛**（`search` 直接返回全文、合并相邻命中、删掉 `fetch`）：第 10 步评估之后做，之前保持旧口径。

**剪掉**

- **封闭词表的三元组**：15 个关系词装不下小说里有价值的关系，不带着它进新设计。
- **向量扩展（sqlite-vec 之类）**：单本书的规模用不上，跨书检索已确认不做。
- **先把 `MYSQL_URI` 指向 SQLite 作为过渡**：零代码改动、已验证可行，但第 2 步会整体换掉这层，不值得为它重建一次索引。在没有 MySQL 的机器上想提前跑现有代码时可以用。
- **章节识别的覆盖表**：改源文件即可。

## 执行顺序

每步做完、满足完成标准之后，再进入下一步。每步结束时更新受影响的领域文档，并把结果补写在这一步末尾。

1. **删除图谱。**
   - 范围见「删除的东西」。`Evidence.graph_context`、`retrieval_mode` 里的 `graph`、`status` 的图谱诊断一并删掉。
   - **完成标准**：`uv run pytest` 通过，ruff clean；`rg -n -i graph src/readfellow tests | rg -v paragraph` 没有输出；`uv run readfellow --help` 不再列出 `graph-index` 和 `graph-query`。
   - **结果**（2026-11-20）：完成标准全部满足，46 个测试通过。`src/` 从 6696 行减到 4610 行，`tests/` 从 3311 行减到 1743 行。删了 `graph.py`、`tests/test_graph.py`、六张图谱专用表和 `artifact_imports.graph_run_id`；`extraction.py`、`derivation.py` 一行没动，里面的函数 `analyze` 都还在用。真实语料上 `fts` 输出逐字节不变，`status` 只少了 graph-index 那一段。MySQL 里已有的图谱表和数据没有动，第 2 步整库去掉。
2. **新库落地并切换。**
   - 实现「一本书一个文件」的索引组和「章节表」。`index` 写新库，`SqliteChunkStore` 实现 `ChunkStore`，`search` / `fts` / `hybrid` / `fetch` / `status` 改读新库，进度由章节表换算。
   - 删除 MySQL 持久化层及其命令和依赖；`analyze` 命令下线。numpy、jieba 加为直接依赖。zvec 留到第 3 步。
   - **完成标准**：不设置 `MYSQL_URI` 时 `uv run pytest` 通过，ruff clean；`rg -n "sqlalchemy|pymysql|MYSQL_URI" src pyproject.toml` 没有输出；对真实语料 `index` 后 `status` 显示 N/N；中途杀掉 `index` 再跑一次能续上。
3. **与 zvec 对照，然后删除 zvec。**
   - 同一份源文件分别建两个库。固定一份查询清单（评估集 10 道题的原话，加若干两字和三字人名），不设进度和设一个中间进度各跑一遍。
   - **完成标准**：向量通道 top-50 的 chunk id 序列一致（分数并列处的次序除外）；关键词通道 top-10 的重合率已记录，不一致的条目逐条看过原因；越界条数为 0。然后 `rg -n zvec src pyproject.toml` 没有输出，`tests/test_cli_evidence.py` 改成覆盖「重开 → 中文全文 → 进度过滤 → 取原文」的 SQLite 链路并通过。
4. **进度持久化与读取命令。**
   - 实现「进度」和 `toc`、`read`、`find`。旧的检索命令套用已存进度，输出不变。
   - 更新 `SKILL.md`：流程改成先 `toc`、再 `read` 或 `find`；删掉对源文件直接 `rg` 的建议；元数据位置、前置条件与代码对齐。
   - 测试只覆盖规定的语义：未设置进度时读取命令报错；`read` 截到进度以内；`find` 能命中两字词且不返回进度外的行；单次参数不能放宽进度；源文件哈希变化后进度失效。
   - **完成标准**：`uv run pytest` 通过，ruff clean。
5. **评估集与基线。**
   - 接着做 `narrative-index-plan.md` 第 3 步，口径不变。被测系统两个：旧系统（`hybrid` + `fetch`）和「无索引」（`toc` + `read` + `find`）。
   - **完成标准**：那份计划第 3 步的完成标准，加上两个系统的场景召回率和越界行数都已记录。
6. **`analyze` v2，落在逐章笔记缓存上。**
   - 设计见 `narrative-index-plan.md`「`analyze` v2」和第 4 步，持久化按本文件「逐章笔记缓存」。同时提供 `notes` 命令。
   - **完成标准**：那份计划第 4 步的完成标准；另外，改切块参数重新 `index --rebuild` 后 `analyze` 报告 0 章待处理；改动源文件里的一章后只有这一章待处理。
7. **人物身份归并。** 同 `narrative-index-plan.md` 第 5 步。
8. **事件连边。**
   - 先在前 50 章左右试跑，人工核对一批边，再由用户决定是否全量。
   - **完成标准**：试跑的边数、锚定失败占比、人工核对的正确率已记录；用户已决定是否全量。全量后，每条边的两端都在库里，且较晚一端的章节序号严格大于较早一端。
9. **剧情段与查询命令。** 同 `narrative-index-plan.md` 第 6、7 步，加上 `trace` 和 `find` 的别名展开。
10. **在评估集上对比，并收敛检索命令。**
    - 同 `narrative-index-plan.md` 第 8 步，去掉「决定图谱是否退役」。
    - **完成标准**：新旧系统的场景召回率对比已记录，所有题的越界行数为 0；用户已决定 embedding 的去留；「搁置」里的检索命令收敛已按决定执行或明确放弃。
11. **MCP 适配器。** 做不做见「未决」。
