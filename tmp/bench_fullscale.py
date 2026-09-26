"""问题 1/4 全书规模：把真实 sample 图谱（只读取出）拷进一次性 MySQL 库，测末端单 chunk 开销与读路径。

uv run python tmp/bench_fullscale.py
"""

import random
import sys
import time
import uuid
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).parent))
from bench_graph import make_extraction  # noqa: E402

from readfellow import artifact_schema as db  # noqa: E402
from readfellow.artifacts import ArtifactStore, StoredRun, make_engine  # noqa: E402
from readfellow.graph import empty_graph, merge_extraction  # noqa: E402
from readfellow.models import Chunk, IndexManifest  # noqa: E402


def timed(label, fn, repeat=3):
    best = []
    for _ in range(repeat):
        t = time.perf_counter()
        value = fn()
        best.append(time.perf_counter() - t)
    print(f"  {label:<42} min {min(best) * 1000:8.1f} ms  (n={repeat})", flush=True)
    return value


uri = dotenv_values(".env")["MYSQL_URI"]
real = ArtifactStore.open(uri)
t = time.perf_counter()
source_id, manifest, chunks = real.read_source("sample")
print(
    f"real read_source('sample'): {(time.perf_counter() - t) * 1000:.0f} ms, {len(chunks)} chunks"
)
t = time.perf_counter()
stored = real.latest("sample", "graph")
print(
    f"real latest('sample','graph'): {(time.perf_counter() - t) * 1000:.0f} ms, "
    f"{len(stored.document.entities)} entities / {len(stored.document.relations)} relations"
)
# 图谱引用的是它自己那一版切块（外键到 chunks），不是最新一版
with real.engine.connect() as c:
    row = dict(
        c.execute(select(db.sources).where(db.sources.c.id == stored.source_version_id))
        .mappings()
        .one()
    )
    row.pop("id")
    row.pop("fingerprint")
    row.update(row.pop("extra"))
    manifest = IndexManifest.model_validate(row)
    chunks = [
        Chunk.model_validate({k: v for k, v in r.items() if k != "source_version_id"})
        for r in c.execute(
            select(db.chunks)
            .where(db.chunks.c.source_version_id == stored.source_version_id)
            .order_by(db.chunks.c.chunk_index)
        ).mappings()
    ]
print(
    f"graph source_version_id={stored.source_version_id} (latest {source_id}), {len(chunks)} chunks"
)
real.close()

admin = make_engine(uri)
database = "readfellow_bench_" + uuid.uuid4().hex[:8]
with admin.begin() as c:
    c.execute(
        text(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin")
    )
try:
    store = ArtifactStore.open(
        make_url(uri).set(database=database).render_as_string(hide_password=False)
    )
    store.initialize()
    sid = store.write_source(manifest, chunks)
    run, _ = store.prepare(
        sid,
        empty_graph(collection="sample", manifest=manifest),
        stale=lambda _: None,
        rebuild=True,
    )
    graph = stored.document
    run = StoredRun(run.id, sid, graph)
    t = time.perf_counter()
    store.save(run)
    print(f"initial full insert: {time.perf_counter() - t:.1f} s")

    print("per-chunk cost at N=2307 (the tail of a full graph-index run):")
    timed("save, document unchanged", lambda: store.save(run))
    processed = {r.chunk_id for r in graph.extractions}
    pending = [c for c in chunks if c.id not in processed]
    rng = random.Random(7)
    for i, chunk in enumerate((pending or chunks[-3:])[:3]):
        ex = make_extraction(rng, chunk, 2307 + i)
        t = time.perf_counter()
        merge_extraction(graph, ex, chunk)
        m = time.perf_counter() - t
        t = time.perf_counter()
        store.save(run)
        s = time.perf_counter() - t
        print(
            f"  merge_extraction {m * 1000:8.1f} ms   save {s * 1000:8.1f} ms",
            flush=True,
        )

    print("read paths at N=2307 (problem 4):")
    timed("read_source (all chunk text)", lambda: store.read_source("sample"))
    fresh = ArtifactStore(store.engine)
    timed("latest('graph') (whole graph)", lambda: fresh.latest("sample", "graph"))
    store.close()
finally:
    with admin.begin() as c:
        c.execute(text(f"DROP DATABASE `{database}`"))
    admin.dispose()
    print("dropped", database)
