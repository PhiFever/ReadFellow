from __future__ import annotations

import re
from pathlib import Path

from .chunking import iter_headings
from .models import ChapterBoundary, IndexManifest, ProgressFilter


_CHAPTER_NUMBER_RE = re.compile(r"^\d*第([0-9零〇一二两三四五六七八九十百千万]+)章")


def _chapter_number(title: str) -> int | None:
    match = _CHAPTER_NUMBER_RE.match(title)
    if match is None:
        return None
    value = match[1]
    if value.isdecimal():
        return int(value)
    digits = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    total = section = digit = 0
    for char in value:
        if char in digits:
            digit = digits[char]
        elif char in units:
            unit = units[char]
            if unit == 10000:
                total += (section + digit) * unit
                section = 0
            else:
                section += (digit or 1) * unit
            digit = 0
        else:
            return None
    return total + section + digit


def chapter_boundaries(source: Path) -> list[ChapterBoundary]:
    boundaries: list[ChapterBoundary] = []
    volume = 1
    seen_number = False
    # Split lines the way read_text_units does, so boundaries and chunks agree
    # on line numbers.
    lines = source.read_text(encoding="utf-8").splitlines()
    for line_start, title in iter_headings(lines):
        number = _chapter_number(title)
        if number == 1 and seen_number:
            volume += 1
        if number is not None:
            seen_number = True
        boundaries.append(
            ChapterBoundary(
                index=len(boundaries) + 1,
                title=title,
                line_start=line_start,
                volume=volume,
                number=number,
            )
        )
    return boundaries


def source_from_manifest(manifest: IndexManifest) -> Path:
    source = Path(manifest.source_path)
    return source if source.is_absolute() else Path.cwd() / source


def line_limit_for_chapter(source: Path, ref: str) -> tuple[int, ChapterBoundary]:
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
    chapters = chapter_boundaries(source)
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

    def line_limit(chapter: ChapterBoundary) -> int:
        if chapter.index < len(chapters):
            return chapters[chapter.index].line_start - 1
        return len(source.read_text(encoding="utf-8").splitlines())

    if len(candidates) > 1:
        details = [
            f"--max-chapter {ref.strip()} is ambiguous; add a volume or use --max-line"
        ]
        details.extend(
            f"volume {chapter.volume}, line_start={chapter.line_start}, "
            f"{chapter.title}: --max-line {line_limit(chapter)}"
            for chapter in candidates
        )
        raise ValueError("\n".join(details))
    current = candidates[0]
    return line_limit(current), current


def build_progress_filter(
    *,
    manifest: IndexManifest | None = None,
    max_chapter: str | None = None,
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
            raise FileNotFoundError(
                f"source file for progress limit not found: {source}"
            )
        line_limit, chapter = line_limit_for_chapter(source, max_chapter)
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
