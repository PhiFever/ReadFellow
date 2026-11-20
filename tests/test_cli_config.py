from __future__ import annotations

from pathlib import Path

from readfellow.cli import build_parser
from readfellow.config import (
    DerivationConfig,
    IndexingConfig,
    OllamaConfig,
    OpenAIConfig,
    PathConfig,
    ReadFellowConfig,
    SearchConfig,
)


def test_cli_defaults_come_from_config() -> None:
    config = ReadFellowConfig(
        paths=PathConfig(
            library_dir=Path("books"),
        ),
        ollama=OllamaConfig(
            base_url="http://localhost:9999",
            embedding_model="embed-test",
            generation_model="generate-test",
            keep_alive="5m",
        ),
        indexing=IndexingConfig(
            default_collection="books",
            chunk_chars=1200,
            overlap_chars=120,
            batch_size=3,
        ),
        search=SearchConfig(top_k=9),
        analysis=DerivationConfig(backend="openai"),
        openai=OpenAIConfig(generation_model="cloud-test"),
    )
    parser = build_parser(config)

    index_args = parser.parse_args(["index", "novel.txt"])
    assert index_args.library_dir == Path("books")
    assert index_args.ollama_url == "http://localhost:9999"
    assert index_args.model == "embed-test"
    assert index_args.keep_alive == "5m"
    assert index_args.collection == "books"
    assert index_args.chunk_chars == 1200
    assert index_args.overlap_chars == 120
    assert index_args.batch_size == 3

    search_args = parser.parse_args(["search", "question"])
    assert search_args.collection == "books"
    assert search_args.top_k == 9
