from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ConfigDict

from .models import ReadFellowModel

CONFIG_FILE = Path("config.yaml")


class PathConfig(ReadFellowModel):
    model_config = ConfigDict(extra="forbid")

    index_dir: Path = Path("indexes")
    metadata_dir: Path = Path("metadata")


class OllamaConfig(ReadFellowModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "http://127.0.0.1:11434"
    embedding_model: str = "qwen3-embedding:8b"
    generation_model: str = "qwen3:8b"
    keep_alive: str = "30m"
    num_ctx: int = 16384


class IndexingConfig(ReadFellowModel):
    model_config = ConfigDict(extra="forbid")

    default_collection: str = "sample"
    chunk_chars: int = 2400
    overlap_chars: int = 240
    batch_size: int = 8


class SearchConfig(ReadFellowModel):
    model_config = ConfigDict(extra="forbid")

    top_k: int = 5


class GraphConfig(ReadFellowModel):
    model_config = ConfigDict(extra="forbid")

    num_predict: int = 4096
    retries: int = 2


class AnalysisConfig(ReadFellowModel):
    model_config = ConfigDict(extra="forbid")

    num_predict: int = 4096
    retries: int = 2


class ReadFellowConfig(ReadFellowModel):
    model_config = ConfigDict(extra="forbid")

    paths: PathConfig = PathConfig()
    ollama: OllamaConfig = OllamaConfig()
    indexing: IndexingConfig = IndexingConfig()
    search: SearchConfig = SearchConfig()
    graph: GraphConfig = GraphConfig()
    analysis: AnalysisConfig = AnalysisConfig()

    @classmethod
    def load(
        cls,
        path: Path | str = CONFIG_FILE,
        *,
        missing_ok: bool = True,
    ) -> ReadFellowConfig:
        config_path = Path(path)
        if not config_path.exists():
            if not missing_ok:
                raise FileNotFoundError(f"config file not found: {config_path}")
            return cls()

        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise TypeError(f"config file must contain a YAML mapping: {config_path}")
        return cls.model_validate(payload)


def load_config(
    path: Path | str = CONFIG_FILE,
    *,
    missing_ok: bool = True,
) -> ReadFellowConfig:
    return ReadFellowConfig.load(path, missing_ok=missing_ok)
