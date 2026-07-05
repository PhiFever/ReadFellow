from __future__ import annotations

from pathlib import Path

import pytest

from readfellow.progress import build_progress_filter, chapter_boundaries


def test_chapter_boundaries_and_progress_filter(tmp_path: Path) -> None:
    source = tmp_path / "novel.txt"
    source.write_text(
        "第一章 开始\n"
        "一\n"
        "\n"
        "第二章 继续\n"
        "二\n"
        "\n"
        "第三章 之后\n"
        "三\n",
        encoding="utf-8",
    )

    chapters = chapter_boundaries(source)

    assert [(c.index, c.title, c.line_start) for c in chapters] == [
        (1, "第一章 开始", 1),
        (2, "第二章 继续", 4),
        (3, "第三章 之后", 7),
    ]

    progress = build_progress_filter(
        manifest={"source_path": str(source)},
        max_chapter=2,
    )

    assert progress.expression == "line_end <= 6"
    assert "第二章 继续" in progress.description
    assert progress.allows({"line_end": 6, "chunk_index": 10})
    assert not progress.allows({"line_end": 7, "chunk_index": 10})


def test_progress_filter_combines_line_and_chunk_limits(tmp_path: Path) -> None:
    source = tmp_path / "novel.txt"
    source.write_text("第一章 开始\n一\n第二章 继续\n二\n", encoding="utf-8")

    progress = build_progress_filter(
        manifest={"source_path": str(source)},
        max_chapter=2,
        max_line=3,
        max_chunk_index=4,
    )

    assert progress.expression == "line_end <= 3 and chunk_index <= 4"
    assert progress.allows({"line_end": 3, "chunk_index": 4})
    assert not progress.allows({"line_end": 4, "chunk_index": 4})
    assert not progress.allows({"line_end": 3, "chunk_index": 5})


def test_progress_rejects_unknown_chapter(tmp_path: Path) -> None:
    source = tmp_path / "novel.txt"
    source.write_text("第一章 开始\n一\n", encoding="utf-8")

    with pytest.raises(ValueError, match="exceeds detected chapter count"):
        build_progress_filter(manifest={"source_path": str(source)}, max_chapter=2)
