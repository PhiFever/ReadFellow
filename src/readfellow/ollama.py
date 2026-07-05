from __future__ import annotations

import json
import math
from typing import Iterable
from urllib import error, request


class OllamaError(RuntimeError):
    pass


def normalize_vector(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


class OllamaEmbedder:
    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "qwen3-embedding:8b",
        keep_alive: str = "30m",
        timeout: int = 600,
        normalize: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.keep_alive = keep_alive
        self.timeout = timeout
        self.normalize = normalize

    def embed(self, texts: Iterable[str]) -> list[list[float]]:
        batch = list(texts)
        if not batch:
            return []

        body = json.dumps(
            {
                "model": self.model,
                "input": batch,
                "keep_alive": self.keep_alive,
            }
        ).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/api/embed",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.URLError as exc:
            raise OllamaError(f"failed to call Ollama at {self.base_url}: {exc}") from exc

        embeddings = payload.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(batch):
            raise OllamaError(f"unexpected Ollama embed response: {payload!r}")

        vectors = [[float(value) for value in embedding] for embedding in embeddings]
        if self.normalize:
            return [normalize_vector(vector) for vector in vectors]
        return vectors

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


class OllamaGenerator:
    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "qwen3:8b",
        keep_alive: str = "30m",
        timeout: int = 600,
        temperature: float = 0.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.keep_alive = keep_alive
        self.timeout = timeout
        self.temperature = temperature

    def generate_json(self, prompt: str) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "prompt": prompt,
                "format": "json",
                "stream": False,
                "keep_alive": self.keep_alive,
                "options": {"temperature": self.temperature},
            }
        ).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.URLError as exc:
            raise OllamaError(f"failed to call Ollama at {self.base_url}: {exc}") from exc

        generated = payload.get("response")
        if not isinstance(generated, str):
            raise OllamaError(f"unexpected Ollama generate response: {payload!r}")
        return generated
