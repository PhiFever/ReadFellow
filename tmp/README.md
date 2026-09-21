# 2026-09-21 架构评审的复现脚本

`docs/architecture-archive.md` 末尾「2026-09-21 架构评审」六个问题的复现代码。问题都还没修，这些脚本现在跑出来应该复现问题本身。

**临时目录，问题修完即可删。** 其中 `test_spoiler.py` 是个例外——它是 pytest 用例，问题 2 修好后应该搬进 `tests/` 当回归用例（符合 AGENTS.md「已复现问题」这条测试约定）。

## 跑之前

全部离线：用合成数据 + SQLite，**不需要 Ollama、MySQL 或显卡**，也不碰 `indexes/` 与 `metadata/`。产物写在 `tmp/out/`，已在 `.gitignore` 里。

```sh
uv run python tmp/bench_graph.py            # 问题 1 · 规模曲线（~3 分钟 @ Pi）
uv run python tmp/bench_total.py            # 问题 1 · 阶数判定（~3 分钟 @ Pi）
uv run python tmp/bench_save.py             # 问题 1 · JSON vs MySQL save
uv run python tmp/bench_read.py             # 问题 3 + 4（需先跑 bench_graph.py）
uv run pytest tmp/test_spoiler.py -q -s     # 问题 2 · 预期失败
uv run python tmp/test_parallel.py          # 问题 5 · graph 并行
uv run python tmp/test_parallel_analysis.py # 问题 5 · analysis 并行
```

`bench_total.py` / `bench_save.py` / `bench_read.py` 从 `bench_graph.py` 导入合成负载，所以四个 bench 脚本要放在同一目录里。

**别用小参数试跑然后下结论。** `bench_total.py` 默认 150/300，跑出 ×3.93；同一脚本传 `30 60` 只有 ×2.97——小 N 下二次项还没主导。这正是问题 1 本身的形状，也是旧版外推公式（基于 `--limit 20`）会错的原因。要判定阶数，两个规模点都得足够大。

## 树莓派基线（2026-09-21，供对比）

Linux 6.8 aarch64，SQLite 本地。**绝对值看硬件，有意义的是比值和阶数**——换到快机器上每一行都会变小，但 ×3.93 和「零变化仍要 650 ms」不会变。

| 脚本 | 结果 |
| --- | --- |
| `bench_total.py` | 150 chunk 34.7s / 300 chunk 136.6s → **×3.93**（线性应 ×2.0，二次应 ×4.0） |
| `bench_graph.py` | 单次 `merge_extraction` 9.3 → 112.0 ms（10 → 300 chunk）；`save` 56 → 668 ms |
| `bench_save.py` | 旧 JSON 整份写 439 ms；新 `save` 正常增量 668 ms；**文档零变化 650 ms** |
| `bench_read.py` | `retries` / `num_ctx` 一动即「整图重建」；`latest('graph')` 680 ms @ 300 chunk |
| `test_spoiler.py` | 编辑前不泄露，编辑后 `exit=0` 且第二章泄露 |
| `test_parallel.py` | B worker `IntegrityError: UNIQUE constraint failed: entities.run_id, entities.position` |
| `test_parallel_analysis.py` | 不报错，4 章都写进去了，但 `processed_count=2` |

## 到有显卡的设备上做正式测试时

这些脚本**测不到 LLM 那一半**，它们刻意把 LLM 调用摘掉了，量的是纯进程内 + 落盘开销。所以在快机器上跑它们只会得到更小的绝对值，不会得到新结论。

真正需要在显卡设备上补的是**两者的比例**——也就是「CPU 开销究竟占 run 的多少」，这才是决定问题 1 值不值得修的数字：

1. 先跑 `bench_total.py` 拿到该机器的 CPU 侧曲线。
2. 再跑一次真实 `graph-index`，**至少两个规模点**（如 `--limit 150` 和 `--limit 300`，各配 `--rebuild`），记总耗时。
3. 两者相减得到 LLM 侧耗时，算出 CPU 占比，并看它从 150 到 300 是怎么变的。

第 2 步不要只测 `--limit 20`——20 chunk 恰好是二次项最不显眼的地方，旧版文档的外推公式就是这么错的（详见 `docs/architecture-archive.md` 的「图谱索引成本说明」）。

另外 `analysis.backend` 当前已是 `openai`（云端 flash），`graph` 仍是 `ollama`。如果要评估「graph 也切云端」的影响，CPU 占比要按云端的单次延迟重算，而不是本地的。
