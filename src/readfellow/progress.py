from __future__ import annotations

import re

from .models import ChapterBoundary, ProgressFilter


def line_limit_for_chapter(
    chapters: list[ChapterBoundary], ref: str, *, source: str = "collection"
) -> tuple[int, ChapterBoundary]:
    match = re.fullmatch(r"(?:(\d+):)?(\d+)", ref.strip())
    if (
        match is None
        or int(match[2]) < 1
        or (match[1] is not None and int(match[1]) < 1)
    ):
        raise ValueError(
            "--max-chapter must be N or V:N with positive numbers, e.g. 50 or 2:50"
        )
    volume = int(match[1]) if match[1] is not None else None
    number = int(match[2])
    if not chapters:
        raise ValueError(f"no chapter headings found in {source}")
    candidates = [
        chapter
        for chapter in chapters
        if chapter.number == number and (volume is None or chapter.volume == volume)
    ]
    if not candidates:
        location = f"volume {volume} " if volume is not None else ""
        raise ValueError(f"{location}chapter {number} not found in {source}")

    if len(candidates) > 1:
        details = [
            f"--max-chapter {ref.strip()} is ambiguous; add a volume or use --max-line"
        ]
        details.extend(
            f"volume {chapter.volume}, line_start={chapter.line_start}, "
            f"{chapter.title}: --max-line {chapter.line_end}"
            for chapter in candidates
        )
        raise ValueError("\n".join(details))
    current = candidates[0]
    return current.line_end, current


def build_progress_filter(
    *,
    chapters: list[ChapterBoundary],
    source: str = "collection",
    max_chapter: str | None = None,
    max_line: int | None = None,
    max_chunk_index: int | None = None,
) -> ProgressFilter:
    clauses: list[str] = []
    descriptions: list[str] = []
    max_line_end: int | None = None

    if max_chapter is not None:
        line_limit, chapter = line_limit_for_chapter(
            chapters, max_chapter, source=source
        )
        max_line_end = line_limit
        descriptions.append(
            f"through volume {chapter.volume} chapter {chapter.number}: {chapter.title}"
        )

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
