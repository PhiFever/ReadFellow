"""Shared plumbing for turning an LLM's JSON answer about a chunk into models."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from .models import Chunk, ChunkContext

ParsedItem = TypeVar("ParsedItem")


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
# break and the paragraph indent a quote spans, swap the full-width quote marks
# around dialogue, mark a quote they cut short with an ellipsis, and close a 【】
# the source opened. Both sides are matched with these removed, and the span is
# then read back out of the chunk so the stored evidence stays the source's own
# wording.
_LOOSE_IN_EVIDENCE = re.compile(r"[\s“”‘’「」『』【】…\"']")


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


class EvidenceNotFound(ValueError):
    """One extracted item quotes something the chunk does not say.

    Item-level, so it costs that item and nothing else. Its own type keeps it
    apart from the document-level failures — unparsable JSON, a missing
    summary — which are still worth another generation.
    """


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
        raise EvidenceNotFound(
            f"{label} evidence is not an exact substring of chunk {chunk_id}"
        )
    return located


@dataclass(frozen=True)
class Rejections:
    """Items the model produced and the parser could not keep, by cause.

    The two are worth telling apart because only one of them says anything about
    how well the model read its chunk. `unanchored` is a quote the chunk does not
    contain — the model paraphrasing instead of quoting, which is exactly what
    the evidence check exists to catch. `unreadable` is everything `parse` could
    not build at all, and its largest class by far is a relation whose predicate
    is outside the vocabulary: a closed predicate list refusing narration is that
    list working, not the extraction failing. Summed into one ratio the second
    buries the first, and the total reads far worse than the run deserves.
    """

    unanchored: int = 0
    unreadable: int = 0

    @property
    def total(self) -> int:
        return self.unanchored + self.unreadable

    def __add__(self, other: Rejections) -> Rejections:
        return Rejections(
            unanchored=self.unanchored + other.unanchored,
            unreadable=self.unreadable + other.unreadable,
        )


def collect_items(
    items: list[Any],
    parse: Callable[[int, Any], ParsedItem | None],
) -> tuple[list[ParsedItem], Rejections]:
    """The items that parsed, plus what the model produced and lost, by cause.

    A dropped item is a loss the caller has to report, so it is counted rather
    than folded into the items a model simply did not produce. `parse` raising
    `EvidenceNotFound` is the unanchored half; `parse` returning None is the
    unreadable one — an entity with no name, a relation whose predicate is
    outside the vocabulary or which is missing an end, or, before decoding was
    schema constrained, a whole relation flattened into a bare string.
    """
    parsed: list[ParsedItem] = []
    unanchored = 0
    unreadable = 0
    for position, item in enumerate(items, start=1):
        try:
            value = parse(position, item)
        except EvidenceNotFound:
            unanchored += 1
            continue
        if value is None:
            unreadable += 1
            continue
        parsed.append(value)
    return parsed, Rejections(unanchored=unanchored, unreadable=unreadable)


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
