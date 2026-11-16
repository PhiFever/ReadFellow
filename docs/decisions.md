# 已定的设计决策

提议改设计之前先查这里。每条决策只有一行，理由和实测在链接的原文里；想推翻某条，先读原文，拿出原文没覆盖的新证据。

## 存储

- 每本书一个 SQLite 文件，向量存 BLOB 暴力检索；去掉 zvec、MySQL、SQLAlchemy。不做跨书联合检索，不引入向量扩展。→ `spec/reading-architecture-plan.md`「一本书一个文件」
- 源文件在场且哈希不一致时 fail closed；不在场时照常读库。→ `spec/reading-architecture-plan.md`「一本书一个文件」
- 不换 chromadb：它的内置 BM25 按空白分词，切不开中文。→ `storage-engine-decision.md`「chromadb 评估」
- 并行只在进程内并发 LLM 调用，保持单写者，不为并行引入 append-only 或多进程写同一份笔记。→ `archive/architecture-review-fix-plan.md`「不重新讨论的事」

## 检索

- 关键词通道用 jieba 预分词后存进 FTS5；精确词走 `find` 的子串搜索，不经过分词。→ `spec/reading-architecture-plan.md`「检索」
- 阅读进度持久化、默认生效，未设置时拒绝读取；单次参数只能收紧。→ `spec/reading-architecture-plan.md`「进度」
- RRF 参数（`RRF_K=60`、`FAN_OUT_MULTIPLIER=10`、等权）不进 config。→ `archive/hybrid-retrieval-mvp.md`
- 检索路径遇到源文件 hash 不一致时一律 fail closed，不做「只在有进度限制时才拦」的折中。→ `archive/architecture-review-fix-plan.md`「不重新讨论的事」（源文件不在场的情形见「存储」第 2 条）
- 不引入重型 GraphRAG，除非评估集证明轻量 hybrid 不够。→ `architecture-archive.md`「GraphRAG 评估」
- 不拆 `FusedEvidence`，等出现第二个需要跨通道比较分数的消费者再说。→ `archive/module-deepening-plan.md`「不重新讨论的事」

## 派生管线

- 单条引文对不上只丢该条并计数，文档级失败仍然重试；不放宽代词替换类失败。→ `archive/derivation-hardening-plan.md`「不重新讨论的事」
- 派生笔记一章一行，以「生成配置 + 章节内容哈希」为键；`analyze` 不经过 chunk，引文存章内相对行号。→ `spec/reading-architecture-plan.md`「逐章笔记缓存」
- 删除按 chunk 抽取的图谱；事件图建在逐章笔记上，边只从后面的章节指向前面的章节；人物关系不建成静态边。→ `spec/reading-architecture-plan.md`「事件图」

## 阅读架构（进行中）

章节一等公民、进度持久化、逐章笔记缓存、事件图、单文件存储、不做跨书检索、改源文件修正章节识别，共 8 条。→ `spec/reading-architecture-plan.md`「不重新讨论的事」

叙事索引的 10 条里，「图谱冻结」改为删除，其余 9 条（核心需求、查询由 agent 规划、云端派生、评估集先行、原子笔记、原地升级 `analyze`、人物身份两遍归并、剧情段连续切分、人名检查兜底）仍然有效。→ `spec/narrative-index-plan.md`「不重新讨论的事」；改动见 `spec/reading-architecture-plan.md`「对叙事索引计划的改动」

## 2026-11-16 推翻的条目

代码在 `spec/reading-architecture-plan.md` 对应步骤落地之前，仍是旧文档描述的样子。

- 「不引入 sqlite」「MySQL 直接读写」「可搬运性未决」。新证据：现有持久化层已能跑在 SQLite 上；单本书规模下暴力向量检索是毫秒级，用不上 ANN；预分词后存进 FTS5 能命中两字词并按 bm25 排序，可以替代 zvec 的中文全文检索；所有命令依赖 MySQL 服务，没有 MySQL 的机器上一条都跑不了。→ 原文 `storage-engine-decision.md`、`archive/mysql-artifact-storage.md`
- 「图谱只标注不打分」「不引入分词依赖」「不改 `graph-query`」「不扩关系词表」「降实体噪声不靠改 prompt」。图谱整体删除后这些条目不再有对象。分词依赖当初是为切词查图谱而否决的，现在引入是为了替代 zvec 内置的分词。→ 原文 `graph-channel-demotion.md`、`derivation.md`「证据锚定与丢弃计数」
- 「落盘节奏之外不再优化派生管线的规模成本」。一章一行之后没有整份文档的保存，这个问题不再存在。→ 原文 `archive/architecture-review-fix-plan.md`
