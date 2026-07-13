from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from zvec import Doc

from readfellow import app
from readfellow.app import (
    GraphBuildOptions,
    ProgressLimit,
    build_graph,
    fetch_chunk,
    fts_search,
    query_graph,
    semantic_search,
)
from readfellow.chunking import chunk_document, sha256_file, sha256_text
from readfellow.config import OllamaConfig, PathConfig, ReadFellowConfig, SearchConfig
from readfellow.graph import (
    empty_graph,
    graph_path,
    merge_extraction,
    parse_graph_extraction,
    read_graph,
    write_graph,
)
from readfellow.models import Chunk, IndexManifest
from readfellow.store import write_manifest


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
    )


class DeterministicGenerator:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self._responses = iter(responses)
        self.prompts: list[str] = []

    def generate_json(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps(next(self._responses), ensure_ascii=False)


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
        "temperature": 0.0,
        "num_predict": config.graph.num_predict,
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


def test_graph_build_retries_evidence_that_is_not_source_grounded(
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
                    }
                ],
            },
            {
                "entities": [],
                "relations": [
                    {
                        "subject": "向山",
                        "relation": "帮助",
                        "object": "尤基",
                        "evidence": "向山帮助了尤基",
                    }
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
    assert len(generator.prompts) == 2
    assert [relation.evidence for relation in graph.relations] == [
        "向山帮助了尤基"
    ]


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

    fake_collection = object()

    def fake_query_vector(
        coll,
        vector: list[float],
        *,
        top_k: int,
        filter: str | None = None,
    ) -> list[Doc]:
        captured["query_vector"] = {
            "coll": coll,
            "vector": vector,
            "top_k": top_k,
            "filter": filter,
        }
        return [
            Doc(
                id="chunk_000001",
                score=0.8125,
                fields={
                    "source_path": str(source),
                    "chunk_index": 1,
                    "line_start": 1,
                    "line_end": 2,
                    "byte_start": 0,
                    "byte_end": len(source_text.encode("utf-8")),
                    "chapter": "第一章 开始",
                    "text_hash": "chunk-hash",
                    "text": source_text,
                },
            )
        ]

    monkeypatch.setattr(app, "read_manifest", lambda **_: manifest_for(source))
    monkeypatch.setattr(app, "open_existing_collection", lambda *_: fake_collection)
    monkeypatch.setattr(app, "OllamaEmbedder", FakeEmbedder)
    monkeypatch.setattr(app, "query_vector", fake_query_vector)

    result = semantic_search(
        config,
        "要查的问题",
        "books",
        progress=ProgressLimit(max_chunk_index=3),
    )

    assert captured["embedder"] == {
        "base_url": "http://localhost:9999",
        "model": "manifest-embedding",
        "keep_alive": "10m",
    }
    assert captured["query"] == "要查的问题"
    assert captured["query_vector"] == {
        "coll": fake_collection,
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
            "graph_context": None,
        }
    ]
    assert result.progress.description == "through chunk index 3"


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
    fake_collection = object()

    monkeypatch.setattr(app, "read_manifest", lambda **_: manifest_for(source))
    monkeypatch.setattr(app, "open_existing_collection", lambda *_: fake_collection)
    monkeypatch.setattr(
        app,
        "query_fts",
        lambda *_args, **_kwargs: [
            Doc(
                id="chunk_000002",
                score=1.25,
                fields={
                    "source_path": str(source),
                    "chunk_index": 2,
                    "line_start": 1,
                    "line_end": 2,
                    "byte_start": 0,
                    "byte_end": len(source_text.encode("utf-8")),
                    "chapter": "第一章 开始",
                    "text_hash": "fts-hash",
                    "text": source_text,
                },
            )
        ],
    )

    result = fts_search(config, "关键词", "books", top_k=4)

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
    stored = Doc(
        id="chunk_000003",
        fields={
            "source_path": str(source),
            "chunk_index": 3,
            "line_start": 1,
            "line_end": 2,
            "byte_start": 0,
            "byte_end": len(source_text.encode("utf-8")),
            "chapter": "第一章 开始",
            "text_hash": "future-hash",
            "text": source_text,
        },
    )

    monkeypatch.setattr(app, "read_manifest", lambda **_: manifest_for(source))
    monkeypatch.setattr(app, "open_existing_collection", lambda *_: object())
    monkeypatch.setattr(app, "fetch_stored_chunk", lambda *_: stored)

    result = fetch_chunk(
        config,
        stored.id,
        "books",
        progress=ProgressLimit(max_line=1),
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
    original_start = len("第一章 开始\n".encode("utf-8"))
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
    late_start = len(f"{early_text}\n".encode("utf-8"))
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
