# 2026-09-21 架构评审的修复方案

日期：2026-09-26
状态：**实施中**，已完成：问题 2、问题 4、问题 3、问题 5、问题 1。剩第 6 步重建索引，由用户执行。Codex 额度用完，由 Claude Code 直接实现，不走委派。
来源：`docs/architecture-archive.md` 末尾「2026-09-21 架构评审」的六个问题

本文先在有显卡的机器上复测六个问题，再按实测结果定修法。结论和评审原文有两处出入：问题 1 在本机的实际代价比原文估的小得多；问题 5 要的并行不需要改持久化模型。

## 复测

机器：WSL2，20 核，RTX 4070 Ti SUPER，本地 MySQL，Ollama `qwen3:8b`。本轮新增的测量脚本也放在 `tmp/`，用法见 `tmp/README.md`。所有写库的测量都在一次性库 `readfellow_bench_*` 里做，跑完即删，没碰 `read-fellow` 库和 `indexes/`。

### `tmp/` 下七个脚本全部复现

| 脚本 | 本机 | 树莓派基线 |
| --- | --- | --- |
| `bench_total.py` 150→300 | 8.5s → 23.9s，×2.83 | ×3.93 |
| `bench_graph.py` @300 | merge 9.7 ms，save 109 ms | 112 / 668 ms |
| `bench_save.py` 零变化 save | 77 ms（整份 JSON 52 ms） | 650 / 439 ms |
| `bench_read.py` | `retries`、`num_ctx` 一改就整图重建 | 同 |
| `test_parallel.py` | B 撞 `UNIQUE (run_id, position)` | 同 |
| `test_parallel_analysis.py` | 4 章都写进去了，`processed_count=2` | 同 |
| `test_spoiler.py` | 源文件编辑后第二章泄露 | 同 |

本机算得快，150→300 时二次项还没占主导，所以比值是 ×2.83，不是 ×3.93。

### 问题 1 放到真实规模上看

评审原文只测了 CPU 这一半，本机补上了 LLM 那一半和全书规模：

| 测量 | 结果 |
| --- | --- |
| 真实 `graph-index`（qwen3:8b + MySQL，91 个 chunk） | LLM 平均 **8.95 s/chunk**；第 90 个 chunk 时 merge 3.5 ms，save 37 ms |
| 真实 sample 图谱（2307 chunk，12288 实体 / 13382 关系）拷入一次性库 | 末端每 chunk：merge ≈ 90 ms，save ≈ 630 ms，零变化 save 603 ms |
| 真实 sample 分析复制到 1242 章 | 末端每章：merge 2.7 ms，save 155 ms |

折算下来：

- **graph，本地 Ollama**：到全书末端，每个 chunk 的 CPU 开销是 0.72 s，约为 LLM 的 8%。CPU 开销从 0 线性涨到 0.72 s，整轮平均约 0.36 s/chunk，合计约 14 分钟，占 5.7 小时的 **4%**。而且图谱已经冻结（`narrative-index-plan` Q8），近期不会重建。
- **analyze，云端**：单章约 84 s，按 8 路并发折成 10–25 s/章；末端 save 155 ms，**不到 1.5%**。

结论：二次项确实存在，但在这台机器上是 4% 量级的问题，排不到第一。它变严重要满足两个条件之一：graph 改走云端（单次延迟降到秒级），或者书的规模大到几倍。

还有一条原型结论：CPU 开销里 87% 是 save，merge 只占一小部分。原型保持合并逻辑不变，只改落盘节奏，300 chunk 的合成负载上：

| 做法 | 耗时 |
| --- | ---: |
| 现行（每 chunk merge + save） | 25.1 s |
| 合并改用增量索引，仍每 chunk save | 24.3 s |
| 每 7 个 chunk save 一次 | 5.3 s |

增量索引几乎没用，落盘节奏才是杠杆。每 7 个 chunk 存一次得到的图谱与现行逐字节一致（时间戳除外）。

### 问题 2 放到真实 sample 上看

`status` 显示 sample 当前的 chunk metadata 已经 stale。逐个比对 2326 个 chunk 的首行，只有最后 4 个对不上：它们整个落在当前文件末尾之外（L175871–176084，当前文件 175868 行）。

- **目前没有泄露**：删的是尾部，前面行号没变；这 4 个 chunk 的行号超出任何进度上限，都会被挡掉。
- **但引用已经错了**：不加进度限制检索时，这 4 个 chunk 仍会出现在结果里，出处指向文件里不存在的行。

## 修复方案

执行顺序（每步单独提交，`uv run pytest` 与 ruff 保持全绿）：

1. 问题 2：检索路径校验源文件。问题 4 的前一半（检索路径白读正文）在这一步一起修。
2. 问题 4 的后一半：`hybrid` 标注只读 top-k 相关的图谱行。评估集要用 `hybrid`，必须在第 6 步重建索引之前完成。
3. 问题 3：`retries`、`num_ctx` 移出失效指纹。
4. 问题 5：多写者检测。
5. 问题 1：按耗时比例决定落盘时机。
6. 交用户执行 `index --rebuild --collection sample`（约 12 分钟），然后用 `status` 确认 chunk metadata 不再 stale。这一步会改 `indexes/sample` 和生产库，由用户决定何时执行。

问题 6 的回归用例分散在各步里。

### 问题 2 · 检索路径校验源文件（先做）

**做法**：`search` / `fts` / `fetch` 统一校验源文件 hash，不一致就拒绝服务；`hybrid` 经由这几条路径，自动受约束。

- `ArtifactStore` 加 `read_manifest(collection)`，返回 `(source_id, manifest, 身份集合)`。身份集合是 chunks 表里去重后的 `(source_path, source_hash)`，一条 `SELECT DISTINCT` 就能拿到，不用拉正文。
- `_validate_chunk_metadata_source` 改为接收身份集合。`_load_indexed_source` 从 chunks 构造这个集合后传进去，派生路径的行为不变。
- `semantic_search` / `fts_search` / `fetch_chunk` 改走新的 `_load_manifest`：先读 manifest，再校验，最后构造 `ProgressFilter`。
- 问题 4 的前一半顺带解决：这三条路径不再为了拿 manifest 把全书正文读回来（真实 sample 每次 140 ms）。

**为什么不只修坐标系**：另一条路是索引时把章节边界存进 manifest，让 `--max-chapter` 不再读当前源文件。这样剧透能挡住，但出处照样指向错的行，违反不变量 1、2。校验 hash 一步就能同时堵住两处。

**代价**：源文件一改，检索就停，直到重新 index。对 sample 来说，这意味着上线即不可用，要先 `index --rebuild`（约 12 分钟）。这本来就是 `narrative-index-plan` 第 3 步的待办；图谱和分析现在已经 stale，重建索引不会让它们多失效一次。

**测试**：

- `tmp/test_spoiler.py` 移到 `tests/`。基线改用真实的 `sha256_file`，编辑后的断言改为「非零退出，且输出不含越界正文」。
- `tests/test_cli_evidence.py` 的 chunk 改用真实 hash，这样第 139 行注释描述的才是真实行为。

### 问题 3 · 运行时参数移出失效指纹

**做法**：`DerivationSettings` 的字段和落盘格式都不动，只改比较方式。

- `derivation.py` 加 `RUNTIME_SETTINGS = frozenset({"retries", "num_ctx"})`，以及 `settings_changed(stored, current)`，比较时排除这两个字段。
- `graph_staleness_reason`（`graph.py:251`）和 `analysis_staleness_reason`（`analysis.py:348`）改用它。
- 改 `DerivationSettings` 上的注释：标准仍然是「塑造答案的旋钮」，但要注明这两个字段存下来只供查阅，不参与判定。

旧 run 读回后不会被判 stale，也不用 bump 版本。`num_ctx` 在 analyze 里还决定哪些章节因超预算被跳过。这影响的是「选哪些章」，不改变已分析章节的内容：调大后续建会补上原先跳过的章，调小时已处理的章也不会被重做，所以同样可以移出。

**测试**：只改 `retries` 后再跑，graph 和 analysis 都应返回 `built`，而不是 `rebuilt`。

### 问题 5 · 先让多写者大声失败，并行留到进程内做

规格第 4 步要的「并行」，实际是**同时发多个 LLM 请求**：云端并发上限约 8，单章 84 s。这用进程内并发就够了。线程池里并发跑 `generate_with_retry`，主线程按完成顺序依次 merge 和 save，写者始终只有一个，持久化层不用改。所以不采纳评审原文「和 append-only 一起设计」的建议；append-only 只有多进程写同一个 run 时才需要，而没有任何计划要这样做。线程池放在第 4 步 `analyze` v2 里实现，不属于本次修复。

本次要做的是**多写者检测**：把 analysis 那种静默写坏变成报错。

- `ArtifactStore` 按 run 记下自己上次读到或写入的 `runs` 行。
- `_save` 在 `FOR UPDATE` 之后重读这一行，对不上就抛出 `RuntimeError`，报「run N 已被另一个进程写过」，并回滚本次事务。
- 验收：`tmp/test_parallel*.py` 改写成回归用例放进 `tests/`，断言 worker B 报错，库里保持 A 写入后的一致状态（`processed_count` 与实际章节数相等）。

**实施结果（2026-09-26）**：回归用例只写了 analysis 这一侧，因为静默写坏的只有它；graph 那侧原本就会撞 `UNIQUE` 报错，现在改为提前报同一个 `RuntimeError`（已用原脚本确认）。新建 run 时 `prepare` 也记下 run 行，否则「A 新建、B 续建并先写、A 再写」时，A 没有记录可比，会删掉 B 写的行。只有从没读过这个 run 的 store 才跳过检查，那种情况下没有基准可比。

### 问题 1 · 按耗时比例决定落盘时机

**做法**：merge 不动，只改 save 的时机。`build_graph` / `build_analysis` 在满足下面任一条件时落盘：

- 距上次落盘已超过 `max(30 s, 20 × 上次落盘耗时)`；
- 循环结束，或者抛出异常（包括 Ctrl-C）时，在 `finally` 里存一次。

前一个条件让 CPU 开销的占比封顶在约 5%，不管 N 多大。崩溃时最多丢一个间隔的 LLM 工作，续建时重抽即可。

**不做的事**：

- 不把 `finalize_graph` 从 `merge_extraction` 里拿出来。它按名字重排实体，`_build_alias_index` 用 `setdefault` 取第一个命中。推迟重排会改变别名撞键时归到哪个实体，合成负载测不出这种差异，收益却只有 3%。
- 不改增量索引。实测只省 3%。
- 不改 append-only。

**验收**：

- `tmp/bench_total.py` 改用新节奏后，300 chunk 的耗时应降到现行的 1/4 以下。
- 现有 `build_graph` / `build_analysis` 测试全绿。落盘节奏不改变文档内容，所以不需要再跑真实模型核对结果。
- 加一个用例：生成器在第 k 个单元抛出 `KeyboardInterrupt`，此前已完成的单元仍然落盘，再跑一次能从第 k 个接着做。这条对应 `finally` 落盘这个需求。

**实施结果（2026-09-26）**：

- 落盘逻辑收在 `app._Checkpoint`，两条管线共用。merge 做到一半被打断时不补存，因为那时文档只合并了一半；欠着的单元下次重抽。
- 合成负载 300 chunk，给 `_Checkpoint` 一个每 chunk 前进 8.95 s 的时钟（本机实测 LLM 延迟），CPU 时间从 26.6 s 降到 9.6 s，是原来的 0.36，**没达到 1/4**。原因是 1/4 这个目标来自「每 7 个 chunk 存一次」的原型，而 LLM 单次 9 s 时，30 s 下限意味着每 4 个 chunk 存一次（300 个 chunk 存 75 次）。两种节奏存下的文档逐字节一致（时间戳除外）。
- 真正要守的是占比：LLM 耗时是 300 × 8.95 s = 2685 s，CPU 占比从 1% 降到 0.36%。全书末端单次 save 约 0.63 s，每 4 个 chunk 摊一次，占比从约 8% 降到约 1.8%。按同样的方法估整轮，CPU 开销从约 14 分钟降到约 5 分钟。
- 中断用例写在 graph 这一侧；analysis 走同一个 `_Checkpoint`。去掉 `finally` 补存后该用例会失败，已验证。

### 问题 4 · `hybrid` 标注只读 top-k 相关的图谱行（重建索引前做）

检索路径白读正文的问题，在问题 2 里一并修掉。

剩下的是 `hybrid` 每次查询都把整张图谱读进内存：真实 sample 上 `latest('graph')` 每次 1.3 s。眼下源文件 hash 对不上，`_load_indexed_source` 会先报错，走不到读图谱这一步。但问题 2 修完、重建索引之后，每次 `hybrid` 都要先花 1.3 s 读完整张已经 stale 的图谱，才能发现标注用不了。评估集要跑很多次 `hybrid`（用户 2026-09-26 确认），这 1.3 s 会乘上查询次数，所以要在重建索引之前修。

**`annotate_chunks` 实际要用的数据**（`graph.py:528`）：

- top-k chunk 里出现过的实体，每个都要带**全部** mentions 与 evidence，因为 `_chunk_spread` 按实体跨越的 chunk 数排序；
- top-k chunk 里的关系；
- 这些关系两端的实体，用来判断两端是否都已声明（`types` 或 `evidence` 非空）。

失效判断 `graph_staleness_reason` 只用 run 行上的版本与来源字段，以及 extractions、fingerprints 两张表，不需要实体和关系。

**做法**：

- `ArtifactStore` 加 `read_graph_slice(collection, chunk_ids) -> StoredRun | None`。它读最新 graph run 的 run 行、全部 extractions 与 fingerprints，再读三类行：`chunk_id` 在集合内的 relations；`chunk_id` 在集合内的 mentions/evidence 所属的实体位置，加上这些 relations 的 `subject_position` / `object_position`；以及这些实体的全部 values、mentions、evidence。组装成一个只含这部分实体和关系、其余字段完整的 `KnowledgeGraph`。组装逻辑与 `_read_run` 共用，不复制一份。
- 这条读路径不写 `_snapshots`，因为切片不是完整文档，拿它当 save 的 diff 基准会把其余行全删掉。
- `app._annotate_with_graph` 改用切片：先 `_load_indexed_source`（问题 2 之后换成更轻的 manifest 校验），再对切片做 `graph_staleness_reason`，最后 `annotate_chunks`。`graph-query` 仍读整图，因为它按子串匹配全部实体和关系。

**验收**：

- 等价性测试：同一个图谱、同一组 chunk id、带进度限制和不带进度限制两种情况下，对切片和对整图调用 `annotate_chunks`，结果相等。
- 耗时：`tmp/bench_fullscale.py` 加一项，在真实 sample 图谱的拷贝上测 `read_graph_slice`（取 5 个 chunk），目标比 `latest('graph')` 的 1.3 s 小一个数量级以上。

**实施结果（2026-09-26）**：

- 耗时从 1352 ms 降到 182 ms，快了 7.4 倍，**没达到一个数量级**。剩下的时间大多花在 pymysql 用纯 Python 解码行上，这些行切片都要用：失效判断要全部 2307 条 extractions 和 fingerprints；5 个 chunk 里出现的 70 个实体中，高频实体带着约 7200 条 mentions 和 evidence，排序要靠它们算跨度。再往下压，就得让 `annotate_chunks` 改收 SQL 算好的跨度计数，为了约 45 ms 不值得改领域接口。
- 按 `chunk_id` 过滤的三条查询带上了 run 的 `source_version_id`，这样 MySQL 能走 `(source_version_id, chunk_id)` 外键索引，不用扫整个 run。前提是 run 的每一行都带着 run 行上的 `source_version_id`：`_save` 在同一个事务里重写这些行和 run 行，所以成立。这一项省了约 70 ms。
- 原方案要单独读 relations 两端的实体。实际不需要：合并关系时两端都会在该 chunk 记一条 mention，按 mention 找到的实体已经包括所有端点。这条已经写进代码注释。
- `_annotate_with_graph` 仍然走 `_load_indexed_source` 读全部 chunk（约 140 ms），没有换成只读 manifest，因为 `graph_staleness_reason` 要逐条比对 chunk 的 hash 和位置。标注这一段合计从约 1.5 s 降到约 0.33 s。

**顺带发现、暂不处理**：`_chunk_spread` 统计的是全书范围的跨度，不受进度限制。它只影响排序，不输出任何内容，但排序本身用到了进度之外的信息。这个问题要单独评估，不在本次范围内。

`_snapshots` 常驻内存的问题，只有做 MCP 常驻进程时才会暴露，到那时再让快照跟着一次 build 走，不再挂在进程级单例上。

### 问题 6 · 测试补什么

- 回归用例按上面各节补：问题 2、3、5 各一个，都是已复现的问题。
- 规模不进 pytest：计时断言在不同机器上必然不稳定。问题 1 的验收用 `tmp/bench_total.py`，修完后连同 `tmp/` 一起删掉，只把本文的实测表留作记录。

## 不重新讨论的事

- 检索路径遇到源文件 hash 不一致时 fail closed，不做「只在有进度限制时才拦」的折中。出处错误本身就违反不变量 1、2。
- 并行只做进程内的 LLM 并发，单写者；不为并行引入 append-only 或多进程写同一个 run。
- 问题 1 只改落盘节奏。等 graph 改走云端或语料规模扩大几倍后再重新评估；评估前先按本文「复测」一节的方法，把 LLM 那一半一起测上。
