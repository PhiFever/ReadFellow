from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from readfellow import app
from readfellow.app import (
    GraphBuildOptions,
    IndexDocumentOptions,
    ProgressLimit,
    build_graph,
    collection_status,
    fetch_chunk,
    fts_search,
    hybrid_search,
    index_document,
    query_graph,
    semantic_search,
)
from readfellow.chunking import (
    CHUNKER_VERSION,
    chunk_document,
    sha256_file,
    sha256_text,
)
from readfellow.config import OllamaConfig, PathConfig, ReadFellowConfig, SearchConfig
from readfellow.graph import (
    empty_graph,
    graph_path,
    merge_extraction,
    parse_graph_extraction,
    read_graph,
    write_graph,
)
from readfellow.models import Chunk, Evidence, IndexManifest, ProgressFilter
from readfellow.store import StoreStats, UpsertOutcome, write_manifest


def manifest_for(source: Path) -> IndexManifest:
    return IndexManifest(
        collection="books",
        collection_path="indexes/books",
        source_path=str(source),
        model="manifest-embedding",
        embedding_dimension=2,
        chunk_count=1,
        chunk_chars=100,
        overlap_chars=10,
        chunker_version=CHUNKER_VERSION,
    )


class DeterministicGenerator:
    """Canned answers. A `str` is returned verbatim, so it can be unparsable."""

    def __init__(self, responses: list[dict[str, object] | str]) -> None:
        self._responses = iter(responses)
        self.prompts: list[str] = []

    def generate_json(self, prompt: str, schema: dict[str, object]) -> str:
        self.prompts.append(prompt)
        response = next(self._responses)
        if isinstance(response, str):
            return response
        return json.dumps(response, ensure_ascii=False)


class InMemoryChunkStore:
    """The second `ChunkStore` adapter: canned hits, no zvec and no filesystem."""

    def __init__(
        self,
        *,
        vector_hits: list[Evidence] | None = None,
        fts_hits: list[Evidence] | None = None,
        fetched: Evidence | None = None,
    ) -> None:
        self.vector_hits = vector_hits or []
        self.fts_hits = fts_hits or []
        self.fetched = fetched
        self.calls: dict[str, dict[str, Any]] = {}
        self.embedded: list[str] = []
        self.text_hashes: dict[str, str] = {}
        self.optimized: bool | None = None

    def upsert(
        self,
        chunks: list[Chunk],
        *,
        model: str,
        embed: Any,
    ) -> UpsertOutcome:
        pending = [
            chunk
            for chunk in chunks
            if self.text_hashes.get(chunk.id) != chunk.text_hash
        ]
        if not pending:
            return UpsertOutcome(written=0, skipped=len(chunks))
        vectors = embed([chunk.text for chunk in pending])
        assert len(vectors) == len(pending)
        self.embedded.extend(chunk.id for chunk in pending)
        self.text_hashes.update({chunk.id: chunk.text_hash for chunk in pending})
        return UpsertOutcome(written=len(pending), skipped=len(chunks) - len(pending))

    def commit(self, *, optimize: bool) -> None:
        self.optimized = optimize

    def search_vector(
        self,
        vector: list[float],
        *,
        top_k: int,
        progress: ProgressFilter,
    ) -> list[Evidence]:
        self.calls["search_vector"] = {
            "vector": vector,
            "top_k": top_k,
            "filter": progress.expression,
        }
        return self.vector_hits

    def search_fts(
        self,
        query: str,
        *,
        top_k: int,
        progress: ProgressFilter,
    ) -> list[Evidence]:
        self.calls["search_fts"] = {
            "query": query,
            "top_k": top_k,
            "filter": progress.expression,
        }
        return self.fts_hits

    def fetch(self, chunk_id: str) -> Evidence | None:
        self.calls["fetch"] = {"chunk_id": chunk_id}
        return self.fetched

    def stats(self) -> StoreStats:
        return StoreStats(doc_count=len(self.text_hashes), index_completeness={})


def test_graph_build_records_versioned_source_fingerprints(
    tmp_path: Path,
) -> None:
    source = tmp_path / "novel.txt"
    source.write_text("第一章 开始\n\n向山帮助了尤基。\n", encoding="utf-8")
    chunks = chunk_document(
        source,
        source_path=str(source),
        target_chars=100,
        overlap_chars=0,
    )
    config = ReadFellowConfig(
        paths=PathConfig(
            index_dir=tmp_path / "indexes",
            metadata_dir=tmp_path / "metadata",
        )
    )
    write_manifest(
        metadata_dir=config.paths.metadata_dir,
        collection="books",
        manifest=manifest_for(source),
        chunks=chunks,
    )
    generator = DeterministicGenerator(
        [
            {
                "entities": [
                    {
                        "name": "向山",
                        "type": "人物",
                        "evidence": "向山帮助了尤基",
                    },
                    {"name": "尤基", "type": "人物"},
                ],
                "relations": [
                    {
                        "subject": "向山",
                        "relation": "帮助",
                        "object": "尤基",
                        "evidence": "向山帮助了尤基",
                    }
                ],
            }
        ]
    )

    result = build_graph(
        config,
        "books",
        options=GraphBuildOptions(retries=0),
        generator=generator,
    )

    assert result.status == "built"
    assert len(generator.prompts) == 1
    assert "prompt_version: graph-extraction-v1" in generator.prompts[0]
    graph = read_graph(result.graph_path)
    assert graph.prompt_version == "graph-extraction-v1"
    assert graph.model_dump(mode="json")["source_chunk_hashes"] == {
        chunks[0].id: {
            "source_hash": chunks[0].source_hash,
            "text_hash": chunks[0].text_hash,
        }
    }
    assert graph.extraction_settings.model_dump() == {
        "temperature": 0.7,
        "top_p": 0.8,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 1.5,
        "repeat_penalty": 1.08,
        "num_predict": config.graph.num_predict,
        "num_ctx": config.ollama.num_ctx,
        "retries": 0,
    }
    assert graph.extractions[0].model_dump() == {
        "chunk_id": chunks[0].id,
        "source_path": chunks[0].source_path,
        "chunk_index": chunks[0].chunk_index,
        "line_start": chunks[0].line_start,
        "line_end": chunks[0].line_end,
        "byte_start": chunks[0].byte_start,
        "byte_end": chunks[0].byte_end,
        "chapter": chunks[0].chapter,
        "source_hash": chunks[0].source_hash,
        "text_hash": chunks[0].text_hash,
        "entity_count": 2,
        "relation_count": 1,
        "rejected_count": 0,
        "unanchored_count": 0,
    }

    fail_if_called = DeterministicGenerator([])
    second = build_graph(
        config,
        "books",
        options=GraphBuildOptions(retries=0),
        generator=fail_if_called,
    )

    assert second.status == "up_to_date"
    assert fail_if_called.prompts == []

    graph.prompt_version = "legacy"
    write_graph(result.graph_path, graph)
    refreshed = DeterministicGenerator(
        [
            {
                "entities": [
                    {
                        "name": "向山",
                        "type": "人物",
                        "evidence": "向山帮助了尤基",
                    }
                ],
                "relations": [],
            }
        ]
    )
    rebuilt = build_graph(
        config,
        "books",
        options=GraphBuildOptions(retries=0),
        generator=refreshed,
    )

    assert rebuilt.status == "rebuilt"
    assert len(refreshed.prompts) == 1


def test_graph_build_drops_evidence_that_is_not_source_grounded(
    tmp_path: Path,
) -> None:
    source = tmp_path / "novel.txt"
    source.write_text("第一章 开始\n\n向山帮助了尤基。\n", encoding="utf-8")
    chunks = chunk_document(
        source,
        source_path=str(source),
        target_chars=100,
        overlap_chars=0,
    )
    config = ReadFellowConfig(
        paths=PathConfig(
            index_dir=tmp_path / "indexes",
            metadata_dir=tmp_path / "metadata",
        )
    )
    write_manifest(
        metadata_dir=config.paths.metadata_dir,
        collection="books",
        manifest=manifest_for(source),
        chunks=chunks,
    )
    generator = DeterministicGenerator(
        [
            {
                "entities": [],
                "relations": [
                    {
                        "subject": "向山",
                        "relation": "帮助",
                        "object": "尤基",
                        "evidence": "向山打败了尤基",
                    },
                    {
                        "subject": "向山",
                        "relation": "认识",
                        "object": "尤基",
                        "evidence": "向山帮助了尤基",
                    },
                ],
            },
        ]
    )

    result = build_graph(
        config,
        "books",
        options=GraphBuildOptions(retries=1),
        generator=generator,
    )

    graph = read_graph(result.graph_path)
    assert len(generator.prompts) == 1
    assert [relation.evidence for relation in graph.relations] == ["向山帮助了尤基"]
    assert graph.rejected_count == 1
    assert graph.unanchored_count == 1


def test_graph_build_survives_a_chunk_it_cannot_parse(tmp_path: Path) -> None:
    source = tmp_path / "novel.txt"
    source.write_text(
        "第一章 开始\n\n向山帮助了尤基。\n\n尤基认识向山。\n", encoding="utf-8"
    )
    chunks = chunk_document(
        source,
        source_path=str(source),
        target_chars=12,
        overlap_chars=0,
    )
    assert len(chunks) > 1
    config = ReadFellowConfig(
        paths=PathConfig(
            index_dir=tmp_path / "indexes",
            metadata_dir=tmp_path / "metadata",
        )
    )
    write_manifest(
        metadata_dir=config.paths.metadata_dir,
        collection="books",
        manifest=manifest_for(source),
        chunks=chunks,
    )
    answer: dict[str, object] = {"entities": [{"name": "向山", "type": "人物"}]}
    generator = DeterministicGenerator(['{"entities": [{"name":', *[answer] * 9])

    result = build_graph(
        config,
        "books",
        options=GraphBuildOptions(retries=0),
        generator=generator,
    )

    assert result.failed_chunk_count == 1
    assert len(generator.prompts) == len(chunks)
    graph = read_graph(result.graph_path)
    assert [record.chunk_id for record in graph.extractions] == [
        chunk.id for chunk in chunks[1:]
    ]

    # The chunk was skipped, not recorded as empty, so it is still pending.
    retry = build_graph(
        config,
        "books",
        options=GraphBuildOptions(retries=0),
        generator=DeterministicGenerator([answer]),
    )

    assert retry.failed_chunk_count == 0
    assert len(read_graph(retry.graph_path).extractions) == len(chunks)


def test_changed_chunk_metadata_invalidates_queries_and_rebuilds_graph(
    tmp_path: Path,
) -> None:
    source = tmp_path / "novel.txt"
    source.write_text("第一章 开始\n\n向山帮助了尤基。\n", encoding="utf-8")
    first_chunks = chunk_document(
        source,
        source_path=str(source),
        target_chars=100,
        overlap_chars=0,
    )
    config = ReadFellowConfig(
        paths=PathConfig(
            index_dir=tmp_path / "indexes",
            metadata_dir=tmp_path / "metadata",
        )
    )
    write_manifest(
        metadata_dir=config.paths.metadata_dir,
        collection="books",
        manifest=manifest_for(source),
        chunks=first_chunks,
    )
    build_graph(
        config,
        "books",
        options=GraphBuildOptions(retries=0),
        generator=DeterministicGenerator(
            [
                {
                    "entities": [
                        {
                            "name": "向山",
                            "type": "人物",
                            "evidence": "向山帮助了尤基",
                        }
                    ],
                    "relations": [],
                }
            ]
        ),
    )

    source.write_text("第一章 开始\n\n尤基离开了房间。\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="chunk metadata is stale"):
        query_graph(config, "向山", "books")

    changed = chunk_document(
        source,
        source_path=str(source),
        target_chars=100,
        overlap_chars=0,
    )[0].model_copy(update={"id": first_chunks[0].id})
    write_manifest(
        metadata_dir=config.paths.metadata_dir,
        collection="books",
        manifest=manifest_for(source),
        chunks=[changed],
    )

    with pytest.raises(RuntimeError, match="graph index is stale"):
        query_graph(config, "向山", "books")

    result = build_graph(
        config,
        "books",
        options=GraphBuildOptions(retries=0),
        generator=DeterministicGenerator(
            [
                {
                    "entities": [
                        {
                            "name": "尤基",
                            "type": "人物",
                            "evidence": "尤基离开了房间",
                        }
                    ],
                    "relations": [],
                }
            ]
        ),
    )

    graph = read_graph(result.graph_path)
    assert result.status == "rebuilt"
    assert [entity.name for entity in graph.entities] == ["尤基"]
    assert graph.extractions[0].source_hash == changed.source_hash
    assert graph.extractions[0].text_hash == changed.text_hash


def test_semantic_search_is_configured_application_workflow(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "novel.txt"
    source_text = "第一章 开始\n这是作为回答依据的原始段落。"
    source.write_text(f"{source_text}\n", encoding="utf-8")
    config = ReadFellowConfig(
        paths=PathConfig(
            index_dir=tmp_path / "indexes", metadata_dir=tmp_path / "metadata"
        ),
        ollama=OllamaConfig(
            base_url="http://localhost:9999",
            embedding_model="config-embedding",
            keep_alive="10m",
        ),
        search=SearchConfig(top_k=11),
    )
    captured: dict[str, Any] = {}

    class FakeEmbedder:
        def __init__(self, *, base_url: str, model: str, keep_alive: str) -> None:
            captured["embedder"] = {
                "base_url": base_url,
                "model": model,
                "keep_alive": keep_alive,
            }

        def embed_one(self, text: str) -> list[float]:
            captured["query"] = text
            return [0.25, 0.75]

    store = InMemoryChunkStore(
        vector_hits=[
            Evidence(
                chunk_id="chunk_000001",
                source_path=str(source),
                chunk_index=1,
                line_start=1,
                line_end=2,
                byte_start=0,
                byte_end=len(source_text.encode("utf-8")),
                chapter="第一章 开始",
                text_hash="chunk-hash",
                text=source_text,
                retrieval_mode="vector",
                score=0.8125,
            )
        ]
    )

    monkeypatch.setattr(app, "read_manifest", lambda **_: manifest_for(source))
    monkeypatch.setattr(app, "OllamaEmbedder", FakeEmbedder)

    result = semantic_search(
        config,
        "要查的问题",
        "books",
        progress=ProgressLimit(max_chunk_index=3),
        store=store,
    )

    assert captured["embedder"] == {
        "base_url": "http://localhost:9999",
        "model": "manifest-embedding",
        "keep_alive": "10m",
    }
    assert captured["query"] == "要查的问题"
    assert store.calls["search_vector"] == {
        "vector": [0.25, 0.75],
        "top_k": 11,
        "filter": "chunk_index <= 3",
    }
    assert [evidence.model_dump(mode="json") for evidence in result.evidence] == [
        {
            "chunk_id": "chunk_000001",
            "source_path": str(source),
            "chunk_index": 1,
            "line_start": 1,
            "line_end": 2,
            "byte_start": 0,
            "byte_end": len(source_text.encode("utf-8")),
            "chapter": "第一章 开始",
            "text_hash": "chunk-hash",
            "text": source_text,
            "retrieval_mode": "vector",
            "score": 0.8125,
            "matches": [],
            "graph_context": None,
        }
    ]
    assert result.progress.description == "through chunk index 3"


def test_reindexing_an_unchanged_document_embeds_nothing(
    monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "novel.txt"
    source.write_text(
        "第一章 开始\n\n向山帮助了尤基。\n\n尤基离开了房间。\n", encoding="utf-8"
    )
    config = ReadFellowConfig(
        paths=PathConfig(
            index_dir=tmp_path / "indexes", metadata_dir=tmp_path / "metadata"
        )
    )

    class FakeEmbedder:
        def __init__(self, **_: Any) -> None:
            pass

        def embed_one(self, text: str) -> list[float]:
            return [1.0, 0.0]

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(app, "OllamaEmbedder", FakeEmbedder)
    store = InMemoryChunkStore()
    options = IndexDocumentOptions(chunk_chars=20, overlap_chars=5, batch_size=1)

    first = index_document(config, source, "books", options=options, store=store)

    assert first.chunk_count > 1
    assert (first.inserted, first.skipped) == (first.chunk_count, 0)
    assert len(store.embedded) == first.chunk_count
    assert store.optimized is True

    second = index_document(config, source, "books", options=options, store=store)

    assert (second.inserted, second.skipped) == (0, second.chunk_count)
    assert len(store.embedded) == first.chunk_count


def test_fts_search_returns_source_grounded_evidence(
    monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "novel.txt"
    source_text = "第一章 开始\n这里包含需要精确查找的关键词。"
    source.write_text(f"{source_text}\n", encoding="utf-8")
    config = ReadFellowConfig(
        paths=PathConfig(
            index_dir=tmp_path / "indexes", metadata_dir=tmp_path / "metadata"
        )
    )
    store = InMemoryChunkStore(
        fts_hits=[
            Evidence(
                chunk_id="chunk_000002",
                source_path=str(source),
                chunk_index=2,
                line_start=1,
                line_end=2,
                byte_start=0,
                byte_end=len(source_text.encode("utf-8")),
                chapter="第一章 开始",
                text_hash="fts-hash",
                text=source_text,
                retrieval_mode="fts",
                score=1.25,
            )
        ]
    )

    monkeypatch.setattr(app, "read_manifest", lambda **_: manifest_for(source))

    result = fts_search(config, "关键词", "books", top_k=4, store=store)

    assert len(result.evidence) == 1
    assert result.evidence[0].retrieval_mode == "fts"
    assert result.evidence[0].chunk_id == "chunk_000002"
    assert result.evidence[0].text == source_text
    assert result.evidence[0].score == 1.25


def test_fetch_chunk_does_not_expose_text_outside_progress(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "novel.txt"
    source_text = "第一章 开始\n这段原文位于读者当前进度之后。"
    source.write_text(f"{source_text}\n", encoding="utf-8")
    config = ReadFellowConfig(
        paths=PathConfig(
            index_dir=tmp_path / "indexes", metadata_dir=tmp_path / "metadata"
        )
    )
    stored = Evidence(
        chunk_id="chunk_000003",
        source_path=str(source),
        chunk_index=3,
        line_start=1,
        line_end=2,
        byte_start=0,
        byte_end=len(source_text.encode("utf-8")),
        chapter="第一章 开始",
        text_hash="future-hash",
        text=source_text,
        retrieval_mode="fetch",
    )

    monkeypatch.setattr(app, "read_manifest", lambda **_: manifest_for(source))

    result = fetch_chunk(
        config,
        stored.chunk_id,
        "books",
        progress=ProgressLimit(max_line=1),
        store=InMemoryChunkStore(fetched=stored),
    )

    assert result.status == "outside_progress"
    assert result.evidence is None


def test_graph_query_returns_original_chunk_as_evidence(tmp_path: Path) -> None:
    source = tmp_path / "novel.txt"
    original_text = "向山帮助了尤基，随后两人继续赶路。"
    unrelated_text = "尤基独自整理行囊。"
    source.write_text(
        f"第一章 开始\n{original_text}\n{unrelated_text}\n",
        encoding="utf-8",
    )
    config = ReadFellowConfig(
        paths=PathConfig(
            index_dir=tmp_path / "indexes", metadata_dir=tmp_path / "metadata"
        )
    )
    manifest = manifest_for(source).model_copy(update={"chunk_count": 2})
    source_hash = sha256_file(source)
    original_start = len("第一章 开始\n".encode())
    original = Chunk(
        id="chunk_000004",
        source_path=str(source),
        source_hash=source_hash,
        chunk_index=4,
        text=original_text,
        text_hash=sha256_text(original_text),
        line_start=2,
        line_end=2,
        byte_start=original_start,
        byte_end=original_start + len(original_text.encode("utf-8")),
        chapter="第一章 开始",
    )
    unrelated = original.model_copy(
        update={
            "id": "chunk_000005",
            "chunk_index": 5,
            "text": unrelated_text,
            "text_hash": sha256_text(unrelated_text),
            "line_start": 3,
            "line_end": 3,
            "byte_start": original.byte_end + 1,
            "byte_end": original.byte_end + 1 + len(unrelated_text.encode("utf-8")),
        }
    )
    write_manifest(
        metadata_dir=config.paths.metadata_dir,
        collection="books",
        manifest=manifest,
        chunks=[original, unrelated],
    )
    graph = empty_graph(collection="books", manifest=manifest)
    extraction = parse_graph_extraction(
        {
            "entities": [
                {"name": "向山", "type": "人物", "aliases": ["武神"]},
                {"name": "尤基", "type": "人物"},
            ],
            "relations": [
                {
                    "subject": "向山",
                    "relation": "帮助",
                    "object": "尤基",
                    "evidence": "向山帮助了尤基",
                }
            ],
        },
        original,
    )
    merge_extraction(graph, extraction, original)
    merge_extraction(
        graph,
        parse_graph_extraction(
            {"entities": [{"name": "尤基", "type": "人物"}], "relations": []},
            unrelated,
        ),
        unrelated,
    )
    write_graph(graph_path(config.paths.metadata_dir, "books"), graph)

    result = query_graph(config, "帮助", "books")

    assert len(result.evidence) == 1
    evidence = result.evidence[0]
    assert evidence.chunk_id == original.id
    assert evidence.text == original.text
    assert evidence.retrieval_mode == "graph"
    assert evidence.score is None
    assert evidence.graph_context is not None
    assert evidence.graph_context.entities == ["向山", "尤基"]
    assert evidence.graph_context.relations == ["向山 --帮助--> 尤基"]

    alias_result = query_graph(config, "武神", "books")
    assert [item.chunk_id for item in alias_result.evidence] == [original.id]


def test_graph_query_does_not_match_alias_learned_after_progress(
    tmp_path: Path,
) -> None:
    source = tmp_path / "novel.txt"
    early_text = "第一章 开始\n向山独自赶路。"
    late_text = "第二章 之后\n人们开始称向山为武神。"
    source.write_text(f"{early_text}\n{late_text}\n", encoding="utf-8")
    config = ReadFellowConfig(
        paths=PathConfig(
            index_dir=tmp_path / "indexes", metadata_dir=tmp_path / "metadata"
        )
    )
    manifest = manifest_for(source).model_copy(update={"chunk_count": 2})
    source_hash = sha256_file(source)
    early = Chunk(
        id="chunk_000006",
        source_path=str(source),
        source_hash=source_hash,
        chunk_index=6,
        text=early_text,
        text_hash=sha256_text(early_text),
        line_start=1,
        line_end=2,
        byte_start=0,
        byte_end=len(early_text.encode("utf-8")),
        chapter="第一章 开始",
    )
    late_start = len(f"{early_text}\n".encode())
    late = Chunk(
        id="chunk_000007",
        source_path=str(source),
        source_hash=source_hash,
        chunk_index=7,
        text=late_text,
        text_hash=sha256_text(late_text),
        line_start=3,
        line_end=4,
        byte_start=late_start,
        byte_end=late_start + len(late_text.encode("utf-8")),
        chapter="第二章 之后",
    )
    write_manifest(
        metadata_dir=config.paths.metadata_dir,
        collection="books",
        manifest=manifest,
        chunks=[early, late],
    )
    graph = empty_graph(collection="books", manifest=manifest)
    merge_extraction(
        graph,
        parse_graph_extraction(
            {"entities": [{"name": "向山", "type": "人物"}], "relations": []},
            early,
        ),
        early,
    )
    merge_extraction(
        graph,
        parse_graph_extraction(
            {
                "entities": [{"name": "向山", "type": "人物", "aliases": ["武神"]}],
                "relations": [],
            },
            late,
        ),
        late,
    )
    write_graph(graph_path(config.paths.metadata_dir, "books"), graph)

    result = query_graph(
        config,
        "武神",
        "books",
        progress=ProgressLimit(max_line=2),
    )

    assert result.evidence == []


def hybrid_workspace(tmp_path: Path) -> tuple[ReadFellowConfig, Path, list[Chunk]]:
    source = tmp_path / "novel.txt"
    source.write_text("第一章 开始\n\n向山帮助了尤基。\n", encoding="utf-8")
    config = ReadFellowConfig(
        paths=PathConfig(
            index_dir=tmp_path / "indexes",
            metadata_dir=tmp_path / "metadata",
        ),
        search=SearchConfig(top_k=5),
    )
    chunks = chunk_document(
        source,
        source_path=str(source),
        target_chars=100,
        overlap_chars=0,
    )
    write_manifest(
        metadata_dir=config.paths.metadata_dir,
        collection="books",
        manifest=manifest_for(source),
        chunks=chunks,
    )
    return config, source, chunks


def hybrid_hit(
    chunk_id: str,
    chunk_index: int,
    *,
    retrieval_mode: str,
) -> Evidence:
    return Evidence(
        chunk_id=chunk_id,
        source_path="corpus/novel.txt",
        chunk_index=chunk_index,
        line_start=chunk_index,
        line_end=chunk_index + 1,
        byte_start=0,
        byte_end=10,
        chapter="第一章 开始",
        text_hash=f"hash-{chunk_id}",
        text=f"{chunk_id} 的正文",
        retrieval_mode=retrieval_mode,
        score=0.5,
    )


def stub_channels(
    monkeypatch,
    *,
    vector_hits: list[Evidence],
    fts_hits: list[Evidence],
) -> InMemoryChunkStore:
    class FakeEmbedder:
        def __init__(self, **_: Any) -> None:
            pass

        def embed_one(self, text: str) -> list[float]:
            return [0.25, 0.75]

    monkeypatch.setattr(app, "OllamaEmbedder", FakeEmbedder)
    return InMemoryChunkStore(vector_hits=vector_hits, fts_hits=fts_hits)


def test_hybrid_ranks_multi_channel_agreement_above_a_single_channel_top_hit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config, _, _ = hybrid_workspace(tmp_path)
    store = stub_channels(
        monkeypatch,
        vector_hits=[
            hybrid_hit("chunk_a", 1, retrieval_mode="vector"),
            hybrid_hit("chunk_x", 3, retrieval_mode="vector"),
            hybrid_hit("chunk_b", 2, retrieval_mode="vector"),
        ],
        fts_hits=[
            hybrid_hit("chunk_y", 4, retrieval_mode="fts"),
            hybrid_hit("chunk_z", 5, retrieval_mode="fts"),
            hybrid_hit("chunk_b", 2, retrieval_mode="fts"),
        ],
    )

    result = hybrid_search(config, "尤基", "books", store=store)

    assert store.calls["search_vector"]["top_k"] == 50
    assert store.calls["search_fts"]["top_k"] == 50
    assert [item.chunk_id for item in result.evidence] == [
        "chunk_b",
        "chunk_a",
        "chunk_y",
        "chunk_x",
        "chunk_z",
    ]
    fused = result.evidence[0]
    assert fused.retrieval_mode == "hybrid"
    assert fused.score == pytest.approx(2 / 63)
    assert [(match.mode, match.rank) for match in fused.matches] == [
        ("vector", 3),
        ("fts", 3),
    ]


def test_hybrid_annotates_results_a_graph_query_could_never_have_matched(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config, _, chunks = hybrid_workspace(tmp_path)
    build_graph(
        config,
        "books",
        options=GraphBuildOptions(retries=0),
        generator=DeterministicGenerator(
            [
                {
                    "entities": [
                        {"name": "向山", "type": "人物", "evidence": "向山帮助了尤基"},
                        {"name": "尤基", "type": "人物", "evidence": "帮助了尤基"},
                    ],
                    "relations": [
                        {
                            "subject": "向山",
                            "relation": "帮助",
                            "object": "尤基",
                            "evidence": "向山帮助了尤基",
                        }
                    ],
                }
            ]
        ),
    )
    store = stub_channels(
        monkeypatch,
        vector_hits=[hybrid_hit(chunks[0].id, 0, retrieval_mode="vector")],
        fts_hits=[hybrid_hit(chunks[0].id, 0, retrieval_mode="fts")],
    )
    question = "向山对尤基做了什么"

    # The graph query matches by substring, so a question never hits an entity.
    assert query_graph(config, question, "books").evidence == []

    result = hybrid_search(config, question, "books", store=store)

    assert result.graph_annotation.annotated == 1
    assert result.graph_annotation.skipped_reason is None
    context = result.evidence[0].graph_context
    assert context is not None
    assert context.entities == ["向山", "尤基"]
    assert context.relations == ["向山 --帮助--> 尤基"]


def test_hybrid_annotation_obeys_the_reading_progress(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config, _, chunks = hybrid_workspace(tmp_path)
    build_graph(
        config,
        "books",
        options=GraphBuildOptions(retries=0),
        generator=DeterministicGenerator(
            [
                {
                    "entities": [
                        {"name": "向山", "type": "人物", "evidence": "向山帮助了尤基"}
                    ],
                    "relations": [],
                }
            ]
        ),
    )
    store = stub_channels(
        monkeypatch,
        vector_hits=[hybrid_hit(chunks[0].id, 0, retrieval_mode="vector")],
        fts_hits=[],
    )

    result = hybrid_search(
        config,
        "向山对尤基做了什么",
        "books",
        store=store,
        progress=ProgressLimit(max_line=1),
    )

    assert result.graph_annotation.annotated == 0
    assert result.evidence[0].graph_context is None


@pytest.mark.parametrize(
    ("graph_state", "expected_reason"),
    [("missing", "graph index not found"), ("stale", "graph index is stale")],
)
def test_hybrid_skips_the_graph_annotation_and_reports_why(
    monkeypatch,
    tmp_path: Path,
    graph_state: str,
    expected_reason: str,
) -> None:
    config, source, chunks = hybrid_workspace(tmp_path)
    if graph_state == "stale":
        build_graph(
            config,
            "books",
            options=GraphBuildOptions(retries=0),
            generator=DeterministicGenerator(
                [
                    {
                        "entities": [
                            {
                                "name": "向山",
                                "type": "人物",
                                "evidence": "向山帮助了尤基",
                            }
                        ],
                        "relations": [],
                    }
                ]
            ),
        )
        source.write_text("第一章 开始\n\n尤基离开了房间。\n", encoding="utf-8")
        changed = chunk_document(
            source,
            source_path=str(source),
            target_chars=100,
            overlap_chars=0,
        )[0].model_copy(update={"id": chunks[0].id})
        write_manifest(
            metadata_dir=config.paths.metadata_dir,
            collection="books",
            manifest=manifest_for(source),
            chunks=[changed],
        )

    store = stub_channels(
        monkeypatch,
        vector_hits=[hybrid_hit("chunk_a", 1, retrieval_mode="vector")],
        fts_hits=[hybrid_hit("chunk_a", 1, retrieval_mode="fts")],
    )

    result = hybrid_search(config, "向山", "books", store=store)

    assert [item.chunk_id for item in result.evidence] == ["chunk_a"]
    assert [(channel.mode, channel.candidates) for channel in result.channels] == [
        ("vector", 1),
        ("fts", 1),
    ]
    assert result.graph_annotation.annotated == 0
    assert expected_reason in (result.graph_annotation.skipped_reason or "")


def test_status_reports_a_stale_graph_and_a_short_index_without_repairing_either(
    tmp_path: Path,
) -> None:
    config, _, _ = hybrid_workspace(tmp_path)
    build_graph(
        config,
        "books",
        options=GraphBuildOptions(retries=0),
        generator=DeterministicGenerator(
            [
                {
                    "entities": [
                        {"name": "向山", "type": "人物", "evidence": "向山帮助了尤基"}
                    ],
                    "relations": [
                        {
                            "subject": "向山",
                            "relation": "帮助",
                            "object": "尤基",
                            "evidence": "向山帮助了尤基",
                        }
                    ],
                }
            ]
        ),
    )
    path = graph_path(config.paths.metadata_dir, "books")
    stored = read_graph(path)
    stored.prompt_version = "legacy"
    write_graph(path, stored)

    # An empty store against a manifest promising one chunk is the shape an
    # index killed between writing metadata and writing embeddings leaves behind.
    status = collection_status(config, "books", store=InMemoryChunkStore())

    assert status.graph.stale_reason == "graph extraction prompt changed"
    assert (status.graph.processed, status.graph.total) == (1, 1)
    assert (status.index.stored_doc_count, status.index.expected_doc_count) == (0, 1)
    assert status.index.is_complete is False
    assert status.diagnostics is not None
    assert status.diagnostics.declared_entity_count == 1
    assert status.analysis.exists is False
    # Reporting a stale derivative must not be a way of rebuilding it.
    assert read_graph(path).prompt_version == "legacy"


def test_hybrid_fails_when_the_embedding_service_is_down(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config, _, _ = hybrid_workspace(tmp_path)
    store = stub_channels(
        monkeypatch,
        vector_hits=[],
        fts_hits=[hybrid_hit("chunk_a", 1, retrieval_mode="fts")],
    )

    class BrokenEmbedder:
        def __init__(self, **_: Any) -> None:
            pass

        def embed_one(self, text: str) -> list[float]:
            raise RuntimeError("ollama is unreachable")

    monkeypatch.setattr(app, "OllamaEmbedder", BrokenEmbedder)

    with pytest.raises(RuntimeError, match="ollama is unreachable"):
        hybrid_search(config, "向山", "books", store=store)
