from __future__ import annotations

from pathlib import Path

import pytest

from readfellow.config import ReadFellowConfig


def test_root_config_yaml_loads() -> None:
    config = ReadFellowConfig.load(Path("config.yaml"))

    assert config.paths.index_dir == Path("indexes")
    assert config.paths.metadata_dir == Path("metadata")
    assert config.ollama.embedding_model == "qwen3-embedding:8b"
    assert config.ollama.generation_model == "qwen3:8b"
    assert config.indexing.chunk_chars == 2400
    assert config.indexing.overlap_chars == 240


def test_config_loads_nested_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
paths:
  index_dir: custom-indexes
ollama:
  base_url: http://localhost:9999
  embedding_model: embed-test
indexing:
  default_collection: books
  batch_size: 3
search:
  top_k: 12
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = ReadFellowConfig.load(config_path)

    assert config.paths.index_dir == Path("custom-indexes")
    assert config.paths.metadata_dir == Path("metadata")
    assert config.ollama.base_url == "http://localhost:9999"
    assert config.ollama.embedding_model == "embed-test"
    assert config.ollama.generation_model == "qwen3:8b"
    assert config.indexing.default_collection == "books"
    assert config.indexing.batch_size == 3
    assert config.search.top_k == 12


def test_explicit_missing_config_can_be_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"

    with pytest.raises(FileNotFoundError, match="config file not found"):
        ReadFellowConfig.load(missing, missing_ok=False)
