"""问题 5 · narrative-index-plan 第 4 步计划「analyze v2，并行运行」。
两个 worker 续建同一个 run（当前 prepare 的语义就是复用最新 run）会怎样。

graph 侧：第一个共享实体就撞 UNIQUE。

    uv run python tmp/test_parallel.py
"""

from bench_graph import OUT

from readfellow.artifacts import ArtifactStore
from readfellow.graph import empty_graph, merge_extraction
from readfellow.models import (
    Chunk,
    ChunkContext,
    GraphEntity,
    GraphExtraction,
    GraphRelation,
    IndexManifest,
)

OUT.mkdir(parents=True, exist_ok=True)
db = OUT / "parallel_graph.sqlite"
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
        chapter="第一章",
    )


def extraction(c, name):
    ctx = ChunkContext(
        chunk_id=c.id,
        source_path=c.source_path,
        chunk_index=c.chunk_index,
        line_start=c.line_start,
        line_end=c.line_end,
        byte_start=c.byte_start,
        byte_end=c.byte_end,
        chapter=c.chapter,
    )
    return GraphExtraction(
        entities=[GraphEntity(name=name, types=["人物"], mentions=[ctx])],
        relations=[
            GraphRelation(
                subject=name, relation="位于", object="北京", **ctx.model_dump()
            )
        ],
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

# 两个独立 worker，各开自己的 ArtifactStore（= 两个进程的情形）
a = ArtifactStore.open(f"sqlite:///{db}")
b = ArtifactStore.open(f"sqlite:///{db}")
run_a, _ = a.prepare(
    source_id,
    empty_graph(collection="b", manifest=manifest),
    stale=lambda _: None,
    rebuild=False,
)
run_b, _ = b.prepare(
    source_id,
    empty_graph(collection="b", manifest=manifest),
    stale=lambda _: None,
    rebuild=False,
)
print(f"worker A 拿到 run_id={run_a.id}")
print(f"worker B 拿到 run_id={run_b.id}")

# A 处理前两个 chunk，B 处理后两个——单元不重叠的标准并行切分
for c in chunks[:2]:
    merge_extraction(run_a.document, extraction(c, "向山"), c)
    a.save(run_a)
print(f"A 写完: {len(run_a.document.entities)} 实体")

try:
    for c in chunks[2:]:
        merge_extraction(run_b.document, extraction(c, "向山"), c)
        b.save(run_b)
    print(f"B 写完: {len(run_b.document.entities)} 实体")
except Exception as exc:  # noqa: BLE001 —— 就是要看它怎么失败
    print(f"B 失败: {type(exc).__name__}: {str(exc)[:160]}")

check = ArtifactStore.open(f"sqlite:///{db}")
final = check.latest("b", "graph").document
print(
    f"\n库里最终: processed_chunk_count={final.processed_chunk_count}, "
    f"实体={len(final.entities)}, 关系={len(final.relations)}"
)
print(f"实际抽过的 chunk: {sorted(e.chunk_id for e in final.extractions)}")
print("两个 worker 一共抽了 4 个 chunk")
