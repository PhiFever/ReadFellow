"""Shared plumbing for turning an LLM's JSON answer about a chunk into models."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from .models import Chunk, ChunkContext


def parse_json_object(raw: str | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw

    text = raw.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(text[start : end + 1])

    if not isinstance(parsed, Mapping):
        raise TypeError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def get_any(
    mapping: Mapping[str, Any], keys: tuple[str, ...], default: Any = None
) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def normalize_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text.strip("`\"'“”‘’")


# Characters an extracted quote may legitimately differ in: models drop the line
# break and the paragraph indent a quote spans, and swap the full-width quote
# marks around dialogue. Both sides are matched with these removed, and the span
# is then read back out of the chunk so the stored evidence stays the source's
# own wording.
_LOOSE_IN_EVIDENCE = re.compile(r"[\s“”‘’「」『』\"']")


def locate_evidence(evidence: str, chunk_text: str) -> str | None:
    """The chunk's own wording for a quote, or None when it is not quoted from it."""
    needle = _LOOSE_IN_EVIDENCE.sub("", evidence)
    if not needle:
        return None

    offsets = [
        index
        for index, char in enumerate(chunk_text)
        if not _LOOSE_IN_EVIDENCE.match(char)
    ]
    haystack = "".join(chunk_text[index] for index in offsets)
    position = haystack.find(needle)
    if position < 0:
        return None
    return chunk_text[offsets[position] : offsets[position + len(needle) - 1] + 1]


def resolve_evidence(
    evidence: str,
    chunk_text: str,
    *,
    label: str,
    chunk_id: str,
) -> str:
    if not evidence:
        return ""
    located = locate_evidence(evidence, chunk_text)
    if located is None:
        raise ValueError(
            f"{label} evidence is not an exact substring of chunk {chunk_id}"
        )
    return located


def chunk_context(chunk: Chunk | ChunkContext | Mapping[str, Any]) -> ChunkContext:
    if isinstance(chunk, Chunk):
        return ChunkContext(
            chunk_id=chunk.id,
            source_path=chunk.source_path,
            chunk_index=chunk.chunk_index,
            line_start=chunk.line_start,
            line_end=chunk.line_end,
            byte_start=chunk.byte_start,
            byte_end=chunk.byte_end,
            chapter=chunk.chapter,
        )
    if isinstance(chunk, ChunkContext):
        return ChunkContext.model_validate(chunk)
    data = chunk.model_dump(mode="json") if isinstance(chunk, BaseModel) else chunk
    return ChunkContext(
        chunk_id=str(data.get("chunk_id") or data.get("id") or ""),
        source_path=str(data.get("source_path", "")),
        chunk_index=int_value(data.get("chunk_index")),
        line_start=int_value(data.get("line_start")),
        line_end=int_value(data.get("line_end")),
        byte_start=int_value(data.get("byte_start")),
        byte_end=int_value(data.get("byte_end")),
        chapter=str(data.get("chapter", "")),
    )


def int_value(value: Any) -> int:
    if value in (None, ""):
        return 0
    return int(value)
