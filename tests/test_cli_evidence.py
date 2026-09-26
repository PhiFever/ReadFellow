from __future__ import annotations

import gc
from pathlib import Path

from readfellow.chunking import CHUNKER_VERSION, sha256_file
from readfellow.cli import main
from readfellow.models import Chunk, IndexManifest
from readfellow.store import ZvecChunkStore, collection_path


def _index_two_chapters(
    tmp_path: Path, artifacts, late_text: str
) -> tuple[Path, Path, Path, Chunk]:
    """A real zvec collection over a two-chapter source, and its chunk metadata."""
    index_dir = tmp_path / "indexes"
    metadata_dir = tmp_path / "metadata"
    source = tmp_path / "novel.txt"
    early_text = "第一章 开始\n向山被人称作武神。"
    filler = "\n".join(f"占位行 {line}" for line in range(3, 20))
    late_prefix = f"{early_text}\n{filler}\n"
    source.write_text(f"{late_prefix}{late_text}\n", encoding="utf-8")
    source_hash = sha256_file(source)
    early = Chunk(
        id="chunk_000000",
        source_path=str(source),
        source_hash=source_hash,
        chunk_index=0,
        text=early_text,
        text_hash="early-hash",
        line_start=1,
        line_end=2,
        byte_start=0,
        byte_end=len(early_text.encode("utf-8")),
        chapter="第一章 开始",
    )
    late = Chunk(
        id="chunk_000001",
        source_path=str(source),
        source_hash=source_hash,
        chunk_index=1,
        text=late_text,
        text_hash="late-hash",
        line_start=20,
        line_end=21,
        byte_start=len(late_prefix.encode("utf-8")),
        byte_end=len(late_prefix.encode("utf-8")) + len(late_text.encode("utf-8")),
        chapter="第二章 之后",
    )
    store = ZvecChunkStore.open_for_write(
        index_dir=index_dir,
        metadata_dir=metadata_dir,
        collection="books",
        dimension=2,
        rebuild=True,
    )
    outcome = store.upsert(
        [early, late],
        model="test-embedding",
        embed=lambda texts: [[1.0, 0.0], [0.0, 1.0]][: len(texts)],
    )
    assert (outcome.written, outcome.skipped) == (2, 0)
    store.commit(optimize=True)
    del store
    gc.collect()
    artifacts.write_source(
        manifest=IndexManifest(
            collection="books",
            collection_path=str(collection_path(index_dir, "books")),
            source_path=str(source),
            model="test-embedding",
            embedding_dimension=2,
            chunk_count=2,
            chunk_chars=100,
            overlap_chars=10,
            chunker_version=CHUNKER_VERSION,
        ),
        chunks=[early, late],
    )
    return index_dir, metadata_dir, source, early


def test_cli_fts_then_fetch_prints_persisted_source_evidence(
    tmp_path: Path,
    capsys,
    artifacts,
) -> None:
    backup = tmp_path / "metadata" / "books" / "graph.json"
    backup.parent.mkdir(parents=True)
    backup.write_text("legacy backup", encoding="utf-8")
    index_dir, metadata_dir, source, early = _index_two_chapters(
        tmp_path, artifacts, "第二章 之后\n另一位武神出现。"
    )
    assert backup.read_text(encoding="utf-8") == "legacy backup"

    fts_exit = main(
        [
            "--index-dir",
            str(index_dir),
            "--metadata-dir",
            str(metadata_dir),
            "fts",
            "武神",
            "--collection",
            "books",
            "--max-line",
            "10",
        ]
    )
    fts_output = capsys.readouterr()

    assert fts_exit == 0
    assert "chunk_000000" in fts_output.out
    assert f"{source}:1-2" in fts_output.out
    assert "向山被人称作武神" in fts_output.out
    assert "chunk_000001" not in fts_output.out

    fetch_exit = main(
        [
            "--index-dir",
            str(index_dir),
            "--metadata-dir",
            str(metadata_dir),
            "fetch",
            early.id,
            "--collection",
            "books",
            "--max-line",
            "10",
        ]
    )
    fetch_output = capsys.readouterr()

    assert fetch_exit == 0
    assert early.text in fetch_output.out
    assert f"{source}:1-2" in fetch_output.out

    source.write_text(source.read_text(encoding="utf-8") + "补记\n", encoding="utf-8")
    status_exit = main(
        [
            "--index-dir",
            str(index_dir),
            "--metadata-dir",
            str(metadata_dir),
            "status",
            "--collection",
            "books",
        ]
    )
    status_output = capsys.readouterr()

    # Once the source file changes, every other command refuses to run on the
    # chunk metadata. Status is the one that has to say so and keep going, and it
    # reads the doc count out of zvec itself.
    assert status_exit == 0
    assert "index:       2/2 docs" in status_output.out
    assert "⚠ chunk metadata" in status_output.out


def test_cli_fts_refuses_after_source_is_edited(
    tmp_path: Path,
    capsys,
    artifacts,
) -> None:
    index_dir, metadata_dir, source, _ = _index_two_chapters(
        tmp_path, artifacts, "第二章 之后\n向山死了，这是天大的剧透。"
    )

    def run_fts() -> tuple[int, str]:
        code = main(
            [
                "--index-dir",
                str(index_dir),
                "--metadata-dir",
                str(metadata_dir),
                "fts",
                "向山",
                "--collection",
                "books",
                "--max-chapter",
                "1",
            ]
        )
        return code, capsys.readouterr().out

    code, out = run_fts()
    assert code == 0
    assert "向山被人称作武神" in out
    assert "剧透" not in out

    # Thirty lines prepended shift every stored line range: chapter 1 now ends
    # past line 21, and the stored chapter-2 chunk would pass as within progress.
    source.write_text(
        "卷首语\n" * 30 + source.read_text(encoding="utf-8"), encoding="utf-8"
    )

    code, out = run_fts()
    assert code != 0
    assert "剧透" not in out
