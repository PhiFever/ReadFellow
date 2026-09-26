"""问题 1 在 analyze 侧：全书 1242 章时，每章一次 merge_chapter + save 要多久（真 MySQL，一次性库）。

用真实 sample 的 analysis run（只读取出）当样本，把它的章节平移复制到 1242 章。
"""

import time
import uuid

from dotenv import dotenv_values
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from readfellow import artifact_schema as db
from readfellow.analysis import ChapterGroup, merge_chapter
from readfellow.artifacts import ArtifactStore, StoredRun, make_engine
from readfellow.models import Chunk, IndexManifest

TARGET = 1242
uri = dotenv_values(".env")["MYSQL_URI"]
real = ArtifactStore.open(uri)
stored = real.latest("sample", "analysis")
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
            select(db.chunks).where(
                db.chunks.c.source_version_id == stored.source_version_id
            )
        ).mappings()
    ]
real.close()
sample = stored.document.chapters
per = sum(len(ch.characters) + len(ch.events) for ch in sample) / len(sample)
print(
    f"sample analysis: {len(sample)} chapters, {per:.1f} characters+events per chapter"
)

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
    document = stored.document.model_copy(deep=True)
    document.chapters = []
    document.chunk_text_hashes = {}
    run, _ = store.prepare(
        sid, document.model_copy(deep=True), stale=lambda _: None, rebuild=True
    )
    run = StoredRun(run.id, sid, document)
    by_id = {c.id: c for c in chunks}
    marks = {1, 100, 300, 600, 900, TARGET}
    for i in range(TARGET):
        base = sample[i % len(sample)]
        analysis = base.model_copy(
            update={"chapter_index": i + 1, "chapter_title": f"第{i + 1}章"}, deep=True
        )
        group = ChapterGroup(
            index=i + 1,
            title=analysis.chapter_title,
            chunks=tuple(by_id[x] for x in base.chunk_ids),
        )
        t = time.perf_counter()
        merge_chapter(document, analysis, group)
        m = time.perf_counter() - t
        t = time.perf_counter()
        store.save(run)
        s = time.perf_counter() - t
        if i + 1 in marks:
            print(
                f"  chapters={i + 1:>5}  merge_chapter {m * 1000:7.1f} ms   save {s * 1000:7.1f} ms",
                flush=True,
            )
    store.close()
finally:
    with admin.begin() as c:
        c.execute(text(f"DROP DATABASE `{database}`"))
    admin.dispose()
    print("dropped", database)
