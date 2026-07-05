from __future__ import annotations

import json
import math
from typing import Iterable
from urllib import error, request

from pydantic import ValidationError

from .models import (
    OllamaEmbedRequest,
    OllamaEmbedResponse,
    OllamaGenerateOptions,
    OllamaGenerateRequest,
)


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

        embed_request = OllamaEmbedRequest(
            model=self.model,
            input=batch,
            keep_alive=self.keep_alive,
        )
        body = embed_request.model_dump_json().encode("utf-8")
        req = request.Request(
            f"{self.base_url}/api/embed",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw_payload = response.read().decode("utf-8")
        except error.URLError as exc:
            raise OllamaError(f"failed to call Ollama at {self.base_url}: {exc}") from exc
        try:
            payload = OllamaEmbedResponse.model_validate_json(raw_payload)
        except ValidationError as exc:
            raise OllamaError(f"unexpected Ollama embed response: {raw_payload!r}") from exc

        embeddings = payload.embeddings
        if len(embeddings) != len(batch):
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
        num_predict: int = 2048,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.keep_alive = keep_alive
        self.timeout = timeout
        self.temperature = temperature
        self.num_predict = num_predict

    def generate_json(self, prompt: str) -> str:
        generate_request = OllamaGenerateRequest(
            model=self.model,
            prompt=prompt,
            keep_alive=self.keep_alive,
            options=OllamaGenerateOptions(
                temperature=self.temperature,
                num_predict=self.num_predict,
            ),
        )
        body = generate_request.model_dump_json().encode("utf-8")
        req = request.Request(
            f"{self.base_url}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw_payload = response.read().decode("utf-8")
        except error.URLError as exc:
            raise OllamaError(f"failed to call Ollama at {self.base_url}: {exc}") from exc

        return parse_generate_response(raw_payload)


def parse_generate_response(raw_payload: str) -> str:
    text = raw_payload.strip()
    if not text:
        raise OllamaError("empty Ollama generate response")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _parse_streaming_generate_response(text)

    return _response_from_payload(payload, raw_payload)


def _parse_streaming_generate_response(raw_payload: str) -> str:
    parts: list[str] = []
    parsed_any = False
    for line in raw_payload.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise OllamaError(
                f"unexpected Ollama generate response: {raw_payload!r}"
            ) from exc
        parts.append(_response_from_payload(payload, stripped))
        parsed_any = True

    if not parsed_any:
        raise OllamaError(f"unexpected Ollama generate response: {raw_payload!r}")
    return "".join(parts)


def _response_from_payload(payload: object, raw_payload: str) -> str:
    if not isinstance(payload, dict):
        raise OllamaError(f"unexpected Ollama generate response: {raw_payload!r}")
    if payload.get("error"):
        raise OllamaError(f"Ollama generate error: {payload['error']}")
    if "response" not in payload:
        raise OllamaError(f"unexpected Ollama generate response: {raw_payload!r}")
    return str(payload["response"])
