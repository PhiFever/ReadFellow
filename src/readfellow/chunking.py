from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Iterable

from .models import Chunk, TextUnit


CHAPTER_RE = re.compile(
    r"^\s*(第[0-9零〇一二两三四五六七八九十百千万]+[章节卷回部篇].*|"
    r"(序章|楔子|引子|尾声|后记|番外).*)\s*$"
)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_text_units(path: Path) -> list[TextUnit]:
    data = path.read_bytes()
    text = data.decode("utf-8")

    units: list[TextUnit] = []
    pending: list[str] = []
    pending_line_start = 0
    pending_byte_start = 0
    pending_line_end = 0
    pending_byte_end = 0
    pending_chapter = ""
    current_chapter = ""

    byte_offset = 0
    for line_number, line in enumerate(text.splitlines(keepends=True), start=1):
        encoded_len = len(line.encode("utf-8"))
        stripped = line.strip()
        match = CHAPTER_RE.match(stripped)
        if match:
            current_chapter = stripped

        if stripped:
            if not pending:
                pending_line_start = line_number
                pending_byte_start = byte_offset
                pending_chapter = current_chapter
            pending.append(line)
            pending_line_end = line_number
            pending_byte_end = byte_offset + encoded_len
        elif pending:
            units.append(
                TextUnit(
                    text="".join(pending),
                    line_start=pending_line_start,
                    line_end=pending_line_end,
                    byte_start=pending_byte_start,
                    byte_end=pending_byte_end,
                    chapter=pending_chapter,
                )
            )
            pending = []

        byte_offset += encoded_len

    if pending:
        units.append(
            TextUnit(
                text="".join(pending),
                line_start=pending_line_start,
                line_end=pending_line_end,
                byte_start=pending_byte_start,
                byte_end=pending_byte_end,
                chapter=pending_chapter,
            )
        )

    return units


def split_large_unit(unit: TextUnit, max_chars: int) -> Iterable[TextUnit]:
    if len(unit.text) <= max_chars:
        yield unit
        return

    for start in range(0, len(unit.text), max_chars):
        part = unit.text[start : start + max_chars]
        yield TextUnit(
            text=part,
            line_start=unit.line_start,
            line_end=unit.line_end,
            byte_start=unit.byte_start,
            byte_end=unit.byte_end,
            chapter=unit.chapter,
        )


def chunk_document(
    path: Path,
    *,
    source_path: str,
    target_chars: int = 2400,
    overlap_chars: int = 240,
) -> list[Chunk]:
    if target_chars <= 0:
        raise ValueError("target_chars must be positive")
    if overlap_chars < 0:
        raise ValueError("overlap_chars cannot be negative")
    if overlap_chars >= target_chars:
        raise ValueError("overlap_chars must be smaller than target_chars")

    source_hash = sha256_file(path)
    source_key = source_hash[:12]
    units = [
        unit
        for original in read_text_units(path)
        for unit in split_large_unit(original, target_chars)
    ]

    chunks: list[Chunk] = []
    window: list[TextUnit] = []
    window_chars = 0

    def emit() -> None:
        nonlocal window, window_chars
        if not window:
            return
        index = len(chunks)
        text = "".join(unit.text for unit in window)
        chunks.append(
            Chunk(
                id=f"{source_key}_{index:06d}",
                source_path=source_path,
                source_hash=source_hash,
                chunk_index=index,
                text=text,
                text_hash=sha256_text(text),
                line_start=window[0].line_start,
                line_end=window[-1].line_end,
                byte_start=window[0].byte_start,
                byte_end=window[-1].byte_end,
                chapter=next(
                    (unit.chapter for unit in reversed(window) if unit.chapter), ""
                ),
            )
        )

        overlap: list[TextUnit] = []
        overlap_total = 0
        for unit in reversed(window):
            if overlap_total >= overlap_chars:
                break
            overlap.append(unit)
            overlap_total += len(unit.text)
        window = list(reversed(overlap))
        window_chars = sum(len(unit.text) for unit in window)

    for unit in units:
        unit_len = len(unit.text)
        if window and window_chars + unit_len > target_chars:
            emit()
        window.append(unit)
        window_chars += unit_len

    emit()
    return chunks
