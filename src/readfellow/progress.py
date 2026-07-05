from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .chunking import CHAPTER_RE, read_text_units


@dataclass(frozen=True)
class ChapterBoundary:
    index: int
    title: str
    line_start: int


@dataclass(frozen=True)
class ProgressFilter:
    expression: str | None
    description: str
    max_line_end: int | None = None
    max_chunk_index: int | None = None

    def allows(self, fields: dict[str, Any]) -> bool:
        if self.max_line_end is not None and int(fields["line_end"]) > self.max_line_end:
            return False
        if (
            self.max_chunk_index is not None
            and int(fields["chunk_index"]) > self.max_chunk_index
        ):
            return False
        return True


def chapter_boundaries(source: Path) -> list[ChapterBoundary]:
    boundaries: list[ChapterBoundary] = []
    for unit in read_text_units(source):
        for offset, line in enumerate(unit.text.splitlines()):
            stripped = line.strip()
            if CHAPTER_RE.match(stripped):
                boundaries.append(
                    ChapterBoundary(
                        index=len(boundaries) + 1,
                        title=stripped,
                        line_start=unit.line_start + offset,
                    )
                )
    return boundaries


def source_from_manifest(manifest: dict[str, Any]) -> Path:
    source = Path(str(manifest["source_path"]))
    return source if source.is_absolute() else Path.cwd() / source


def line_limit_for_chapter(source: Path, max_chapter: int) -> tuple[int, ChapterBoundary]:
    if max_chapter < 1:
        raise ValueError("--max-chapter must be positive")

    chapters = chapter_boundaries(source)
    if not chapters:
        raise ValueError(f"no chapter headings found in {source}")
    if max_chapter > len(chapters):
        raise ValueError(
            f"--max-chapter {max_chapter} exceeds detected chapter count {len(chapters)}"
        )

    current = chapters[max_chapter - 1]
    if max_chapter < len(chapters):
        return chapters[max_chapter].line_start - 1, current

    return len(source.read_text(encoding="utf-8").splitlines()), current


def build_progress_filter(
    *,
    manifest: dict[str, Any] | None = None,
    max_chapter: int | None = None,
    max_line: int | None = None,
    max_chunk_index: int | None = None,
) -> ProgressFilter:
    clauses: list[str] = []
    descriptions: list[str] = []
    max_line_end: int | None = None

    if max_chapter is not None:
        if manifest is None:
            raise ValueError("--max-chapter requires collection metadata")
        source = source_from_manifest(manifest)
        if not source.is_file():
            raise FileNotFoundError(f"source file for progress limit not found: {source}")
        line_limit, chapter = line_limit_for_chapter(source, max_chapter)
        max_line_end = line_limit
        descriptions.append(f"through chapter {chapter.index}: {chapter.title}")

    if max_line is not None:
        if max_line < 1:
            raise ValueError("--max-line must be positive")
        max_line_end = max_line if max_line_end is None else min(max_line_end, max_line)
        descriptions.append(f"through line {max_line}")

    if max_line_end is not None:
        clauses.append(f"line_end <= {max_line_end}")

    if max_chunk_index is not None:
        if max_chunk_index < 0:
            raise ValueError("--max-chunk-index cannot be negative")
        clauses.append(f"chunk_index <= {max_chunk_index}")
        descriptions.append(f"through chunk index {max_chunk_index}")

    return ProgressFilter(
        expression=" and ".join(clauses) if clauses else None,
        description="; ".join(descriptions),
        max_line_end=max_line_end,
        max_chunk_index=max_chunk_index,
    )
