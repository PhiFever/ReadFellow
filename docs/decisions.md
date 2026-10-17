# 已定的设计决策

提议改设计之前先查这里。每条决策只有一行，理由和实测在链接的原文里；想推翻某条，先读原文，拿出原文没覆盖的新证据。

## 存储

- 不引入 sqlite，也不把图谱搬进 zvec；写放大不构成理由。→ `storage-engine-decision.md`「不重新讨论的事」1–4
- 不换 chromadb：它的内置 BM25 按空白分词，切不开中文。→ `storage-engine-decision.md`「chromadb 评估」
- 「可搬运性」还没评估完，是唯一可能推翻上面两条的方向。→ `storage-engine-decision.md`「未决 · 可搬运性」
- MySQL 直接读写，不双写也不回退 JSON。→ `archive/mysql-artifact-storage.md`
- 并行只在进程内并发 LLM 调用，保持单写者，不为并行引入 append-only 或多进程写同一个 run。→ `archive/architecture-review-fix-plan.md`「不重新讨论的事」

## 检索

- 图谱在 `hybrid` 里只标注、不打分，不改回召回通道；不引入分词依赖，不做实体桥接，不改 `graph-query` 的子串语义。→ `graph-channel-demotion.md`「不重新讨论的事」
- RRF 参数（`RRF_K=60`、`FAN_OUT_MULTIPLIER=10`、等权）不进 config。→ `archive/hybrid-retrieval-mvp.md`
- 检索路径遇到源文件 hash 不一致时一律 fail closed，不做「只在有进度限制时才拦」的折中。→ `archive/architecture-review-fix-plan.md`「不重新讨论的事」
- 不引入重型 GraphRAG，除非评估集证明轻量 hybrid 不够。→ `architecture-archive.md`「GraphRAG 评估」
- 不拆 `FusedEvidence`，等出现第二个需要跨通道比较分数的消费者再说。→ `archive/module-deepening-plan.md`「不重新讨论的事」

## 派生管线

- 单条引文对不上只丢该条并计数，文档级失败仍然重试；不放宽代词替换类失败。→ `archive/derivation-hardening-plan.md`「不重新讨论的事」
- 降实体噪声不靠改 prompt：prompt 加严实测无效，噪声在关系端点。→ `graph-channel-demotion.md`「后续 · prompt 加严实测无效」
- 不靠调采样参数降丢弃率；白名单拒绝是封闭谓词表的设计成本，不扩词表。→ `derivation.md`「证据锚定与丢弃计数」
- 落盘节奏之外不再优化派生管线的规模成本，等 graph 改走云端或语料扩大几倍再评估。→ `archive/architecture-review-fix-plan.md`「不重新讨论的事」

## 叙事索引（进行中）

核心需求、查询由 agent 规划、云端派生、评估集先行、原子笔记、图谱冻结、原地升级 `analyze`、人物身份两遍归并、剧情段连续切分、人名检查兜底，共 10 条。→ `spec/narrative-index-plan.md`「不重新讨论的事」
