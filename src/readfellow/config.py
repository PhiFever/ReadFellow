from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import ConfigDict, SecretStr

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


class OpenAIConfig(ReadFellowModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "https://api.b.ai/v1"
    generation_model: str = "qwen3.8-flash"
    num_ctx: int = 65536


class IndexingConfig(ReadFellowModel):
    model_config = ConfigDict(extra="forbid")

    default_collection: str = "sample"
    chunk_chars: int = 2400
    overlap_chars: int = 240
    batch_size: int = 8


class SearchConfig(ReadFellowModel):
    model_config = ConfigDict(extra="forbid")

    top_k: int = 5


class DerivationConfig(ReadFellowModel):
    """Generation limits for one derived artifact; `graph` and `analysis` differ."""

    model_config = ConfigDict(extra="forbid")

    backend: Literal["ollama", "openai"] = "ollama"
    num_predict: int = 4096
    retries: int = 2


class ReadFellowConfig(ReadFellowModel):
    model_config = ConfigDict(extra="forbid")

    database_url: SecretStr | None = None
    paths: PathConfig = PathConfig()
    ollama: OllamaConfig = OllamaConfig()
    openai: OpenAIConfig = OpenAIConfig()
    indexing: IndexingConfig = IndexingConfig()
    search: SearchConfig = SearchConfig()
    graph: DerivationConfig = DerivationConfig()
    analysis: DerivationConfig = DerivationConfig()

    def generation_model(self, derivation: DerivationConfig) -> str:
        backend = self.openai if derivation.backend == "openai" else self.ollama
        return backend.generation_model

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


def read_secret(name: str) -> str | None:
    """Environment first, then ./.env. Callers must never print the value."""
    from dotenv import dotenv_values

    return os.environ.get(name) or dotenv_values(".env").get(name)


def load_config(
    path: Path | str = CONFIG_FILE,
    *,
    missing_ok: bool = True,
) -> ReadFellowConfig:
    return ReadFellowConfig.load(path, missing_ok=missing_ok)
