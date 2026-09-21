"""问题 1 · storage-engine-decision 的 1.8% 是 JSON 落盘时代测的。
同一份图谱，对比旧的 write_json_document 与现在的 artifacts.save。

关键观察：文档零变化时 save 仍要花掉几乎同样的时间——成本在「重算 + diff」，
不在实际写入量。

    uv run python tmp/bench_save.py [chunk数]
"""

import random
import sys
import time

from bench_graph import OUT, make_chunk, make_extraction, manifest_for

from readfellow.artifacts import ArtifactStore
from readfellow.derivation import write_json_document
from readfellow.graph import empty_graph, merge_extraction

if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    OUT.mkdir(parents=True, exist_ok=True)
    db = OUT / "bench_save.sqlite"
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

    last_save_ms = 0.0
    for i, chunk in enumerate(chunks):
        merge_extraction(graph, make_extraction(rng, chunk, i + 1), chunk)
        t = time.perf_counter()
        store.save(run_)
        last_save_ms = (time.perf_counter() - t) * 1000

    print(
        f"图谱规模: {len(graph.entities)} 实体 / {len(graph.relations)} 关系 / {n} chunk"
    )

    path = OUT / "bench_save_graph.json"
    t = time.perf_counter()
    for _ in range(3):
        write_json_document(path, graph)
    json_ms = (time.perf_counter() - t) / 3 * 1000

    # 零变化：新进程读回后立刻 save，文档与库里完全一致
    cold = ArtifactStore.open(f"sqlite:///{db}")
    stored = cold.latest("bench", "graph")
    t = time.perf_counter()
    cold.save(stored)
    unchanged_ms = (time.perf_counter() - t) * 1000

    print(
        f"\n旧 write_json_document (整份 {path.stat().st_size / 1e6:.1f} MB): {json_ms:7.1f} ms"
    )
    print(f"新 artifacts.save      (循环中的正常增量):      {last_save_ms:7.1f} ms")
    print(f"新 artifacts.save      (文档一个字节没变):      {unchanged_ms:7.1f} ms")
    print(
        f"\n零变化仍要 {unchanged_ms / json_ms:.1f}x 于整份 JSON 写盘 —— "
        "成本在重算+diff，与写入量无关"
    )
    store.close()
    cold.close()
