"""Shared plumbing for artifacts derived from chunks one LLM call at a time."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

Document = TypeVar("Document", bound=BaseModel)
Parsed = TypeVar("Parsed")


class JsonGenerator(Protocol):
    def generate_json(self, prompt: str, schema: dict[str, Any]) -> str: ...


def generate_with_retry(
    generator: JsonGenerator,
    prompt: str,
    parse: Callable[[str], Parsed],
    *,
    schema: dict[str, Any],
    retries: int,
    label: str,
    on_retry: Callable[[int, Exception], None],
) -> Parsed:
    """The parsed answer, retrying generation and parsing together.

    A model that answers with unusable JSON has failed just as much as one that
    fails to answer, so the whole round trip is one attempt.

    Retrying only buys anything because generation samples. Under greedy
    decoding an attempt is a pure repeat of the one before it, down to the
    column the JSON broke at, which is why `DerivationSettings` does not offer
    temperature 0 as its default.
    """
    for attempt in range(retries + 1):
        try:
            return parse(generator.generate_json(prompt, schema))
        except Exception as exc:
            if attempt >= retries:
                raise RuntimeError(f"{label}: {exc}") from exc
            on_retry(attempt + 1, exc)
    raise RuntimeError(f"{label}: no attempt was made")


def load_or_reset(
    path: Path,
    empty: Document,
    *,
    read: Callable[[Path], Document],
    stale: Callable[[Document], str | None],
    rebuild: bool,
) -> tuple[Document, bool]:
    """The stored document when it is still trustworthy, else `empty`.

    The flag says whether an existing document was discarded, which is what
    separates a rebuilt run from a first build.
    """
    if not path.exists():
        return empty, False
    if rebuild:
        return empty, True
    stored = read(path)
    if stale(stored) is not None:
        return empty, True
    return stored, False


def write_json_document(path: Path, document: BaseModel) -> None:
    """Publish the document in one step, replacing any previous version.

    A derivation rewrites the whole file after every unit and a run lasts hours,
    so anything reading it meanwhile — `status`, an editor, a second command —
    would otherwise be free to catch a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f"{path.name}.tmp")
    pending.write_text(
        json.dumps(document.model_dump(mode="json"), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    os.replace(pending, path)


def derivation_status(*, selected: int, pending: int, rebuilt: bool) -> str:
    if not selected:
        return "empty"
    if not pending:
        return "up_to_date"
    return "rebuilt" if rebuilt else "built"
