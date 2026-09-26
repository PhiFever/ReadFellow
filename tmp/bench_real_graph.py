"""问题 1 真实测量：真 qwen3:8b + 真 MySQL（一次性库），逐 chunk 记 LLM / merge / save 耗时。

    uv run python tmp/bench_real_graph.py <chunk 数> <输出 csv>

在 MYSQL_URI 所在实例里建 readfellow_bench_<hex> 库，跑完删掉，不碰 read-fellow 库与 indexes/。
"""

import csv
import sys
import time
import uuid
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.engine import make_url

from readfellow import app
from readfellow.app import GraphBuildOptions
from readfellow.artifacts import ArtifactStore, make_engine
from readfellow.chunking import CHUNKER_VERSION, chunk_document
from readfellow.config import load_config
from readfellow.models import IndexManifest

total = int(sys.argv[1])
out = Path(sys.argv[2])
config = load_config()
uri = dotenv_values(".env")["MYSQL_URI"]
admin = make_engine(uri)
database = "readfellow_bench_" + uuid.uuid4().hex[:8]
with admin.begin() as c:
    c.execute(
        text(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin")
    )
print("database", database, flush=True)
try:
    store = ArtifactStore.open(
        make_url(uri).set(database=database).render_as_string(hide_password=False)
    )
    store.initialize()
    source = Path("corpus/samples/赛博英雄传.txt")
    chunks = chunk_document(
        source, source_path=str(source), target_chars=2400, overlap_chars=240
    )
    store.write_source(
        IndexManifest(
            collection="bench",
            collection_path="indexes/bench",
            source_path=str(source),
            model="qwen3-embedding:8b",
            embedding_dimension=4096,
            chunk_count=len(chunks),
            chunk_chars=2400,
            overlap_chars=240,
            chunker_version=CHUNKER_VERSION,
        ),
        chunks,
    )

    rows: list[dict] = []
    current: dict = {}

    class TimedGenerator:
        def __init__(self, inner):
            self.inner = inner

        def generate_json(self, *args, **kw):
            t = time.perf_counter()
            try:
                return self.inner.generate_json(*args, **kw)
            finally:
                current["llm"] = current.get("llm", 0.0) + time.perf_counter() - t

    real_merge = app.merge_extraction

    def timed_merge(graph, extraction, chunk):
        t = time.perf_counter()
        real_merge(graph, extraction, chunk)
        current["merge"] = time.perf_counter() - t

    app.merge_extraction = timed_merge
    real_save = store.save

    def timed_save(run):
        t = time.perf_counter()
        real_save(run)
        current["save"] = time.perf_counter() - t
        graph = run.document
        if "merge" in current:
            rows.append(
                dict(
                    n=graph.processed_chunk_count,
                    llm=current.get("llm", 0.0),
                    merge=current["merge"],
                    save=current["save"],
                    entities=len(graph.entities),
                    relations=len(graph.relations),
                )
            )
            r = rows[-1]
            print(
                f"{r['n']:>4} llm={r['llm']:6.2f}s merge={r['merge'] * 1000:7.1f}ms "
                f"save={r['save'] * 1000:7.1f}ms ent={r['entities']} rel={r['relations']}",
                flush=True,
            )
        current.clear()

    store.save = timed_save

    llm_model, _, settings = app._generation_plan(
        config, config.graph, llm_model=None, num_predict=None, retries=None
    )
    generator = TimedGenerator(
        app._default_generator(
            config, config.graph, llm_model=llm_model, settings=settings
        )
    )
    start = time.perf_counter()
    result = app.build_graph(
        config,
        "bench",
        options=GraphBuildOptions(limit=total, rebuild=True),
        generator=generator,
        artifacts=store,
    )
    wall = time.perf_counter() - start
    print(result, flush=True)
    print(f"wall {wall:.1f}s", flush=True)
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    store.close()
finally:
    with admin.begin() as c:
        c.execute(text(f"DROP DATABASE `{database}`"))
    admin.dispose()
    print("dropped", database, flush=True)
