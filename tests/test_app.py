from __future__ import annotations

from pathlib import Path
from typing import Any

from readfellow import app
from readfellow.app import ProgressLimit, semantic_search
from readfellow.config import OllamaConfig, PathConfig, ReadFellowConfig, SearchConfig
from readfellow.models import IndexManifest


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


def test_semantic_search_is_configured_application_workflow(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "novel.txt"
    source.write_text("第一章 开始\n内容\n", encoding="utf-8")
    config = ReadFellowConfig(
        paths=PathConfig(index_dir=tmp_path / "indexes", metadata_dir=tmp_path / "metadata"),
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
    ) -> list[str]:
        captured["query_vector"] = {
            "coll": coll,
            "vector": vector,
            "top_k": top_k,
            "filter": filter,
        }
        return ["doc-1"]

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
    assert result.docs == ["doc-1"]
    assert result.progress.description == "through chunk index 3"
