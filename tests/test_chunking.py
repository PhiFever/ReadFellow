from __future__ import annotations

from pathlib import Path

from readfellow.chunking import chunk_document, read_text_units


def test_read_text_units_preserves_offsets_and_chapter(tmp_path: Path) -> None:
    source = tmp_path / "novel.txt"
    # write_bytes, not write_text: text mode would rewrite every "\n" as
    # os.linesep, turning the CRLF this case is about into CRCRLF on Windows.
    source.write_bytes(
        "第一章 开始\r\n\r\n这是第一段。\r\n第二行。\r\n\r\n尾声\r\n结束。\r\n".encode()
    )

    units = read_text_units(source)

    assert [unit.line_start for unit in units] == [1, 3, 6]
    assert units[0].chapter == "第一章 开始"
    assert units[1].chapter == "第一章 开始"
    assert units[2].chapter == "尾声"
    assert source.read_bytes()[units[1].byte_start : units[1].byte_end].decode(
        "utf-8"
    ) == ("这是第一段。\r\n第二行。\r\n")


def test_chunk_document_uses_stable_ids_and_overlap(tmp_path: Path) -> None:
    source = tmp_path / "novel.txt"
    source.write_text(
        "第一章 开始\n\n一" * 20 + "\n\n" + "二" * 20 + "\n\n" + "三" * 20 + "\n",
        encoding="utf-8",
    )

    chunks = chunk_document(
        source,
        source_path="corpus/samples/novel.txt",
        target_chars=35,
        overlap_chars=10,
    )

    assert len(chunks) >= 2
    assert chunks[0].id.endswith("_000000")
    assert chunks[1].id.endswith("_000001")
    assert chunks[0].source_path == "corpus/samples/novel.txt"
    assert chunks[0].chapter == "第一章 开始"
    assert chunks[0].text_hash != chunks[1].text_hash


def test_chunk_document_never_straddles_a_chapter_boundary(tmp_path: Path) -> None:
    source = tmp_path / "novel.txt"
    source.write_text(
        "第一章 开始\n\n"
        + "一" * 20
        + "\n\n"
        + "壹" * 20
        + "\n\n第二章 之后\n\n"
        + "二" * 20
        + "\n\n"
        + "贰" * 20
        + "\n",
        encoding="utf-8",
    )

    chunks = chunk_document(
        source,
        source_path="corpus/samples/novel.txt",
        target_chars=200,
        overlap_chars=20,
    )

    assert [chunk.chapter for chunk in chunks] == ["第一章 开始", "第二章 之后"]
    for chunk in chunks:
        assert ("第一章" in chunk.text) != ("第二章" in chunk.text)
    assert "壹" not in chunks[1].text
