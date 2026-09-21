"""问题 1 · 规模曲线：graph-index 的非 LLM 开销随已处理 chunk 数如何增长。

模拟真实抽取产出（每 chunk ~8 实体 + ~10 关系，主角高频复用 + 长尾），
分别计量 merge_extraction（内存合并）与 artifacts.save（落盘 diff）。

    uv run python tmp/bench_graph.py [总chunk数] [采样点,逗号分隔]
"""

import random
import sys
import time
from pathlib import Path

from readfellow.artifacts import ArtifactStore
from readfellow.graph import empty_graph, merge_extraction
from readfellow.models import (
    Chunk,
    ChunkContext,
    GraphEntity,
    GraphEvidence,
    GraphExtraction,
    GraphRelation,
    IndexManifest,
)

OUT = Path(__file__).parent / "out"
RELATIONS = ("认识", "帮助", "伤害", "位于", "拥有", "说过", "导致")
MAIN = ["向山", "李轩", "阿姆斯特朗", "赛博英雄", "太一"]


def make_chunk(i: int) -> Chunk:
    text = f"第{i}段正文。" * 200
    return Chunk(
        id=f"abcdef123456_{i:06d}",
        source_path="corpus/x.txt",
        source_hash="s" * 64,
        chunk_index=i,
        text=text,
        text_hash=f"{i:064d}",
        line_start=i * 10,
        line_end=i * 10 + 9,
        byte_start=i * 100,
        byte_end=i * 100 + 99,
        chapter=f"第{i // 6 + 1}章",
    )


def names(rng, i):
    """主角占一半，其余从缓慢增长的长尾里取——真实图谱的实体分布。"""
    out = []
    for _ in range(8):
        if rng.random() < 0.5:
            out.append(rng.choice(MAIN))
        else:
            out.append(f"配角{rng.randrange(max(1, i))}")
    return out


def make_extraction(rng, chunk, i) -> GraphExtraction:
    ctx = ChunkContext(
        chunk_id=chunk.id,
        source_path=chunk.source_path,
        chunk_index=chunk.chunk_index,
        line_start=chunk.line_start,
        line_end=chunk.line_end,
        byte_start=chunk.byte_start,
        byte_end=chunk.byte_end,
        chapter=chunk.chapter,
    )
    ns = names(rng, i)
    ents = [
        GraphEntity(
            name=n,
            types=["人物"],
            mentions=[ctx.model_copy(deep=True)],
            evidence=[GraphEvidence(**ctx.model_dump(), text=f"{n}走了过来。")],
        )
        for n in ns
    ]
    rels = [
        GraphRelation(
            subject=rng.choice(ns),
            relation=rng.choice(RELATIONS),
            object=rng.choice(ns),
            evidence=f"证据{k}",
            **ctx.model_dump(),
        )
        for k in range(10)
    ]
    return GraphExtraction(
        entities=ents, relations=rels, rejected_count=3, unanchored_count=1
    )


def manifest_for(total: int) -> IndexManifest:
    return IndexManifest(
        collection="bench",
        collection_path="indexes/bench",
        source_path="corpus/x.txt",
        model="m",
        embedding_dimension=8,
        chunk_count=total,
        chunk_chars=2400,
        overlap_chars=240,
        chunker_version=3,
    )


def run(total: int, sample_at, db: Path):
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    rng = random.Random(7)
    store = ArtifactStore.open(f"sqlite:///{db}")
    store.initialize()
    manifest = manifest_for(total)
    chunks = [make_chunk(i) for i in range(total)]
    source_id = store.write_source(manifest, chunks)
    run_, _ = store.prepare(
        source_id,
        empty_graph(collection="bench", manifest=manifest),
        stale=lambda _: None,
        rebuild=True,
    )
    graph = run_.document

    print(
        f"{'chunk#':>7} {'merge_ms':>9} {'save_ms':>9} {'entities':>9} {'relations':>10}",
        flush=True,
    )
    for i, chunk in enumerate(chunks):
        ex = make_extraction(rng, chunk, i + 1)
        t0 = time.perf_counter()
        merge_extraction(graph, ex, chunk)
        t1 = time.perf_counter()
        store.save(run_)
        t2 = time.perf_counter()
        if i + 1 in sample_at:
            print(
                f"{i + 1:>7} {(t1 - t0) * 1000:>9.1f} {(t2 - t1) * 1000:>9.1f} "
                f"{len(graph.entities):>9} {len(graph.relations):>10}",
                flush=True,
            )
    store.close()


if __name__ == "__main__":
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    marks = (
        {int(x) for x in sys.argv[2].split(",")}
        if len(sys.argv) > 2
        else {10, 25, 50, 75, 100, 150, 200, 250, 300}
    )
    run(total, marks, OUT / "bench_graph.sqlite")
