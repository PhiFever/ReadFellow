from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from test_analysis import build_collection

from readfellow import app
from readfellow.app import (
    IndexDocumentOptions,
    ProgressLimit,
    collection_status,
    fetch_chunk,
    fts_search,
    hybrid_search,
    index_document,
    semantic_search,
)
from readfellow.artifacts import open_artifacts
from readfellow.chunking import (
    CHUNKER_VERSION,
    chunk_document,
)
from readfellow.config import (
    DerivationConfig,
    OllamaConfig,
    PathConfig,
    ReadFellowConfig,
    SearchConfig,
)
from readfellow.models import Chunk, Evidence, IndexManifest, ProgressFilter
from readfellow.store import StoreStats, UpsertOutcome


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

    open_artifacts(config).write_source(manifest_for(source), [])
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

    open_artifacts(config).write_source(manifest_for(source), [])

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

    open_artifacts(config).write_source(manifest_for(source), [])

    result = fetch_chunk(
        config,
        stored.chunk_id,
        "books",
        progress=ProgressLimit(max_line=1),
        store=InMemoryChunkStore(fetched=stored),
    )

    assert result.status == "outside_progress"
    assert result.evidence is None


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
    open_artifacts(config).write_source(manifest=manifest_for(source), chunks=chunks)
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


def test_hybrid_reports_channel_candidates(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config, _, _ = hybrid_workspace(tmp_path)
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


def test_status_reports_a_short_index(
    tmp_path: Path,
) -> None:
    config, _, _ = hybrid_workspace(tmp_path)
    # An empty store against a manifest promising one chunk is the shape an
    # index killed between writing metadata and writing embeddings leaves behind.
    status = collection_status(config, "books", store=InMemoryChunkStore())

    assert (status.index.stored_doc_count, status.index.expected_doc_count) == (0, 1)
    assert status.index.is_complete is False
    assert status.analysis.exists is False


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


def test_concurrent_analysis_build_merges_in_chapter_order(tmp_path: Path) -> None:
    config = build_collection(tmp_path)
    config.analysis = DerivationConfig(concurrency=2)
    second_started = threading.Event()
    wait_results: list[bool] = []

    class ConcurrentGenerator:
        def generate_json(self, prompt: str, schema: dict[str, object]) -> str:
            if "第一章 开始" in prompt:
                wait_results.append(second_started.wait(timeout=5))
            elif "第二章 之后" in prompt:
                second_started.set()
            return json.dumps({"summary": "章节梗概", "characters": [], "events": []})

    result = app.build_analysis(config, "books", generator=ConcurrentGenerator())

    assert wait_results == [True]
    document = open_artifacts(config).read_run(result.run_id).document
    assert [chapter.chapter_title for chapter in document.chapters] == [
        "第一章 开始",
        "第二章 之后",
    ]
