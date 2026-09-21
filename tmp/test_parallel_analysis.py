"""问题 5 · analysis 侧的并行：章节不重叠，不会撞 UNIQUE。那它会静默错在哪。

答案：4 章全写进去了，但 runs.processed_count 被后写的 worker 覆盖成 2。
status 和 analyze 的完成度报告从此骗人。

    uv run python tmp/test_parallel_analysis.py
"""

from bench_graph import OUT

from readfellow.analysis import ChapterGroup, empty_analysis, merge_chapter
from readfellow.artifacts import ArtifactStore
from readfellow.models import ChapterAnalysis, Chunk, IndexManifest

OUT.mkdir(parents=True, exist_ok=True)
db = OUT / "parallel_analysis.sqlite"
if db.exists():
    db.unlink()


def chunk(i):
    return Chunk(
        id=f"c_{i:06d}",
        source_path="x.txt",
        source_hash="h" * 64,
        chunk_index=i,
        text=f"正文{i}",
        text_hash=f"{i:064d}",
        line_start=i,
        line_end=i,
        byte_start=i,
        byte_end=i + 1,
        chapter=f"第{i + 1}章",
    )


manifest = IndexManifest(
    collection="b",
    collection_path="p",
    source_path="x.txt",
    model="m",
    embedding_dimension=8,
    chunk_count=4,
    chunk_chars=100,
    overlap_chars=10,
    chunker_version=3,
)
chunks = [chunk(i) for i in range(4)]

setup = ArtifactStore.open(f"sqlite:///{db}")
setup.initialize()
source_id = setup.write_source(manifest, chunks)
setup.close()

a = ArtifactStore.open(f"sqlite:///{db}")
b = ArtifactStore.open(f"sqlite:///{db}")
run_a, _ = a.prepare(
    source_id,
    empty_analysis(collection="b", manifest=manifest),
    stale=lambda _: None,
    rebuild=False,
)
run_b, _ = b.prepare(
    source_id,
    empty_analysis(collection="b", manifest=manifest),
    stale=lambda _: None,
    rebuild=False,
)
print(f"A run_id={run_a.id}, B run_id={run_b.id}  (同一个 run)")


def analyze(store, run, i, c):
    group = ChapterGroup(index=i, title=c.chapter, chunks=(c,))
    merge_chapter(
        run.document,
        ChapterAnalysis(
            chapter_index=i,
            chapter_title=c.chapter,
            source_path=c.source_path,
            line_start=c.line_start,
            line_end=c.line_end,
            chunk_ids=[c.id],
            summary=f"第{i}章梗概",
        ),
        group,
    )
    store.save(run)


# A 领章节 1-2，B 领章节 3-4：单元不重叠、各有独立验收标准的标准切分
for i, c in enumerate(chunks[:2], start=1):
    analyze(a, run_a, i, c)
print(f"A 内存里: {len(run_a.document.chapters)} 章")
for i, c in enumerate(chunks[2:], start=3):
    analyze(b, run_b, i, c)
print(f"B 内存里: {len(run_b.document.chapters)} 章  (没有报错)")

check = ArtifactStore.open(f"sqlite:///{db}")
final = check.latest("b", "analysis").document
print(f"\n库里最终: processed_chapter_count={final.processed_chapter_count}")
print(f"  实际存下来的章节: {sorted(c.chapter_index for c in final.chapters)}")
print(f"  chunk_text_hashes 覆盖: {sorted(final.chunk_text_hashes)}")
print("  两个 worker 一共分析了 4 章")
