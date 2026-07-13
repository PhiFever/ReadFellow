from __future__ import annotations

import gc
from pathlib import Path

from readfellow.cli import main
from readfellow.models import Chunk, IndexManifest
from readfellow.store import (
    chunk_to_doc,
    collection_path,
    open_or_create_collection,
    write_manifest,
)


def test_cli_fts_then_fetch_prints_persisted_source_evidence(
    tmp_path: Path,
    capsys,
) -> None:
    index_dir = tmp_path / "indexes"
    metadata_dir = tmp_path / "metadata"
    source = tmp_path / "novel.txt"
    early_text = "第一章 开始\n向山被人称作武神。"
    filler = "\n".join(f"占位行 {line}" for line in range(3, 20))
    late_text = "第二章 之后\n另一位武神出现。"
    late_prefix = f"{early_text}\n{filler}\n"
    source.write_text(f"{late_prefix}{late_text}\n", encoding="utf-8")
    early = Chunk(
        id="chunk_000000",
        source_path=str(source),
        source_hash="source-hash",
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
        source_hash="source-hash",
        chunk_index=1,
        text=late_text,
        text_hash="late-hash",
        line_start=20,
        line_end=21,
        byte_start=len(late_prefix.encode("utf-8")),
        byte_end=len(late_prefix.encode("utf-8")) + len(late_text.encode("utf-8")),
        chapter="第二章 之后",
    )
    collection = open_or_create_collection(
        index_dir=index_dir,
        metadata_dir=metadata_dir,
        collection="books",
        dimension=2,
        rebuild=True,
    )
    statuses = collection.insert(
        [
            chunk_to_doc(early, [1.0, 0.0], model="test-embedding"),
            chunk_to_doc(late, [0.0, 1.0], model="test-embedding"),
        ]
    )
    assert all(status.ok() for status in statuses)
    collection.optimize()
    collection.flush()
    del collection
    gc.collect()
    write_manifest(
        metadata_dir=metadata_dir,
        collection="books",
        manifest=IndexManifest(
            collection="books",
            collection_path=str(collection_path(index_dir, "books")),
            source_path=str(source),
            model="test-embedding",
            embedding_dimension=2,
            chunk_count=2,
            chunk_chars=100,
            overlap_chars=10,
        ),
        chunks=[early, late],
    )

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
