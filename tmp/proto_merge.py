"""问题 1 修法原型：增量索引 + 只在落盘前 finalize + 每 K 个 chunk 落盘一次。

    uv run python tmp/proto_merge.py <chunk 数> <K>

与 tmp/bench_graph.py 同一份合成负载、同一个 SQLite 落盘，只换合并与落盘节奏。
K=0 表示基线（现行代码：每 chunk merge_extraction + save）。
"""

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bench_graph import make_chunk, make_extraction, manifest_for  # noqa: E402

from readfellow import graph as g  # noqa: E402
from readfellow.artifacts import ArtifactStore  # noqa: E402
from readfellow.extraction import chunk_context  # noqa: E402
from readfellow.models import GraphEntity  # noqa: E402


class Merger:
    """把现行 merge_extraction 里每 chunk 重建的东西，改成跨 chunk 维护。"""

    def __init__(self, graph):
        self.graph = graph
        self.reindex()

    def reindex(self):
        graph = self.graph
        self.alias = g._build_alias_index(graph)
        self.rel_keys = {self._rel_key(r) for r in graph.relations}
        self.mention_keys = {
            id(e): {(m.chunk_id, m.line_start, m.line_end) for m in e.mentions}
            for e in graph.entities
        }
        self.evidence_keys = {
            id(e): {(v.chunk_id, v.text) for v in e.evidence} for e in graph.entities
        }
        self.extracted = {r.chunk_id for r in graph.extractions}

    @staticmethod
    def _rel_key(r):
        return (
            r.subject,
            r.relation,
            r.object,
            r.evidence,
            r.chunk_id,
            r.line_start,
            r.line_end,
        )

    def _entity(self, incoming):
        graph = self.graph
        name = g.normalize_text(incoming.name)
        aliases = [g.normalize_text(a) for a in g._as_strings(incoming.aliases)]
        aliases = [a for a in aliases if a and a != name]
        index = g._find_entity_index(self.alias, [name, *aliases])
        if index is None:
            entity = GraphEntity(name=name)
            graph.entities.append(entity)
            index = len(graph.entities) - 1
            self.mention_keys[id(entity)] = set()
            self.evidence_keys[id(entity)] = set()
        else:
            entity = graph.entities[index]
            if name and name != entity.name:
                aliases.append(name)
        for t in g._as_strings(incoming.types):
            n = g._normalize_entity_type(t)
            if n and n not in entity.types:
                entity.types.append(n)
        for a in aliases:
            if a not in entity.aliases:
                entity.aliases.append(a)
        mk, ek = self.mention_keys[id(entity)], self.evidence_keys[id(entity)]
        for m in incoming.mentions:
            key = (m.chunk_id, m.line_start, m.line_end)
            if key not in mk:
                mk.add(key)
                entity.mentions.append(m.model_copy(deep=True))
        for v in incoming.evidence:
            key = (v.chunk_id, v.text)
            if key not in ek:
                ek.add(key)
                entity.evidence.append(v.model_copy(deep=True))
        for value in [entity.name, *entity.aliases]:
            key = g._case_key(value)
            if key:
                self.alias[key] = index
        return entity

    def merge(self, extraction, chunk):
        for e in extraction.entities:
            self._entity(e)
        for r in extraction.relations:
            s = self._entity(GraphEntity(name=r.subject, mentions=[chunk_context(r)]))
            o = self._entity(GraphEntity(name=r.object, mentions=[chunk_context(r)]))
            merged = r.model_copy(
                update={"subject_entity": s.name, "object_entity": o.name}, deep=True
            )
            key = self._rel_key(merged)
            if key not in self.rel_keys:
                self.rel_keys.add(key)
                self.graph.relations.append(merged)
        if chunk.id not in self.extracted:
            self.extracted.add(chunk.id)
            g._mark_extracted(self.graph, chunk, extraction)  # 内部线性扫：原型里先绕开
        # 不 finalize

    def finalize(self):
        g.finalize_graph(self.graph)
        self.reindex()  # finalize 按名字重排实体，位置索引要跟着重建


def run(total, k):
    db = Path("tmp/out/proto.sqlite")
    db.parent.mkdir(parents=True, exist_ok=True)
    db.unlink(missing_ok=True)
    rng = random.Random(7)
    store = ArtifactStore.open(f"sqlite:///{db}")
    store.initialize()
    manifest = manifest_for(total)
    chunks = [make_chunk(i) for i in range(total)]
    sid = store.write_source(manifest, chunks)
    run_, _ = store.prepare(
        sid,
        g.empty_graph(collection="bench", manifest=manifest),
        stale=lambda _: None,
        rebuild=True,
    )
    graph = run_.document
    merger = None if k == 0 else Merger(graph)
    start = time.perf_counter()
    for i, chunk in enumerate(chunks):
        ex = make_extraction(rng, chunk, i + 1)
        if merger is None:
            g.merge_extraction(graph, ex, chunk)
            store.save(run_)
        else:
            merger.merge(ex, chunk)
            if (i + 1) % k == 0 or i + 1 == total:
                merger.finalize()
                store.save(run_)
    elapsed = time.perf_counter() - start
    store.close()
    return elapsed, graph


if __name__ == "__main__":
    total, k = int(sys.argv[1]), int(sys.argv[2])
    elapsed, graph = run(total, k)
    print(
        f"N={total} K={k}: {elapsed:.2f}s  entities={len(graph.entities)} relations={len(graph.relations)} processed={graph.processed_chunk_count}"
    )
