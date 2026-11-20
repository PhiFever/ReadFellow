from typing import ClassVar

import pytest

from readfellow import app
from readfellow.config import PathConfig, ReadFellowConfig


@pytest.fixture
def embedder(monkeypatch):
    class Embedder:
        batches: ClassVar[list[list[str]]] = []
        queries: ClassVar[list[str]] = []
        settings: ClassVar[list[dict[str, str]]] = []

        def __init__(self, **settings):
            self.settings.append(settings)

        def embed_one(self, text):
            self.queries.append(text)
            return [1.0, 0.0]

        def embed(self, texts):
            self.batches.append(texts)
            return [[1.0, 0.0] if "第一章" in text else [0.0, 1.0] for text in texts]

    monkeypatch.setattr(app, "OllamaEmbedder", Embedder)
    return Embedder


@pytest.fixture
def book(tmp_path, embedder):
    config = ReadFellowConfig(paths=PathConfig(library_dir=tmp_path / "library"))
    source = tmp_path / "novel.txt"
    source.write_text(
        "第一章 开始\n尤基修好了机器。\n第二章 之后\n尤基发现了秘密。\n",
        encoding="utf-8",
    )
    return config, source
