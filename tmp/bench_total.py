"""问题 1 · 阶数判定：同一工作负载跑 N 和 2N，看总耗时是 2 倍（线性）还是 4 倍（二次）。

顺带算出 architecture-archive 旧版推荐的「用前 20 chunk 外推全书」会差多少。

    uv run python tmp/bench_total.py [N] [2N]
"""

import random
import sys
import time
from pathlib import Path

from bench_graph import OUT, make_chunk, make_extraction, manifest_for

from readfellow.artifacts import ArtifactStore
from readfellow.graph import empty_graph, merge_extraction

FULL_BOOK = 2307  # 赛博英雄传全量 chunk 数，用于外推对比


def total_for(n: int, db: Path) -> tuple[float, float]:
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    rng = random.Random(7)
    store = ArtifactStore.open(f"sqlite:///{db}")
    store.initialize()
    manifest = manifest_for(n)
    chunks = [make_chunk(i) for i in range(n)]
    source_id = store.write_source(manifest, chunks)
    run_, _ = store.prepare(
        source_id,
        empty_graph(collection="bench", manifest=manifest),
        stale=lambda _: None,
        rebuild=True,
    )
    graph = run_.document
    at20 = 0.0
    start = time.perf_counter()
    for i, chunk in enumerate(chunks):
        merge_extraction(graph, make_extraction(rng, chunk, i + 1), chunk)
        store.save(run_)
        if i + 1 == 20:
            at20 = time.perf_counter() - start
    total = time.perf_counter() - start
    store.close()
    return total, at20


if __name__ == "__main__":
    n1 = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    n2 = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    t1, _ = total_for(n1, OUT / "total_n1.sqlite")
    t2, a2 = total_for(n2, OUT / "total_n2.sqlite")
    print(f"\n{n1} chunk 总耗时: {t1:7.1f}s")
    print(f"{n2} chunk 总耗时: {t2:7.1f}s")
    print(f"规模翻倍 -> 耗时 x{t2 / t1:.2f}   (线性应 x2.0，二次应 x4.0)")
    print(f"\n前 20 chunk 耗时 {a2:.1f}s；按旧版文档推荐的 t20/20*N 外推 {n2} chunk:")
    print(
        f"  外推 {a2 / 20 * n2:7.1f}s   实测 {t2:7.1f}s   低估 {t2 / (a2 / 20 * n2):.1f} 倍"
    )
    print(f"外推到真实全书 {FULL_BOOK} chunk:")
    print(f"  线性外推 {a2 / 20 * FULL_BOOK / 60:7.1f} 分钟")
    print(f"  二次外推 {t2 * (FULL_BOOK / n2) ** 2 / 60:7.1f} 分钟")
