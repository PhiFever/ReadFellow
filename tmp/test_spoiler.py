"""问题 2 · 源文件在 index 之后被编辑，--max-chapter 是否还挡得住剧透。

这个是 pytest 用例且自包含（不依赖 tests/conftest.py），可以直接在 tmp/ 下跑：

    uv run pytest tmp/test_spoiler.py -q -s

**当前预期：失败。** 失败即复现了问题 2——修好之后它应该变绿，
届时可以整体搬进 tests/ 当回归用例。
"""

from __future__ import annotations

import gc
from pathlib import Path

import pytest

from readfellow.artifacts import open_artifacts
from readfellow.cli import main
from readfellow.config import ReadFellowConfig
from readfellow.models import Chunk, IndexManifest
from readfellow.store import ZvecChunkStore, collection_path


@pytest.fixture
def artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("MYSQL_URI", f"sqlite:///{tmp_path / 'artifacts.sqlite'}")
    store = open_artifacts(ReadFellowConfig())
    store.initialize()
    yield store
    store.close()


def test_edited_source_breaks_max_chapter(tmp_path: Path, capsys, artifacts) -> None:
    index_dir, metadata_dir = tmp_path / "indexes", tmp_path / "metadata"
    source = tmp_path / "novel.txt"

    # --- index 时的源文件 ---
    ch1 = "第一章 开始\n向山被人称作武神。"
    filler = "\n".join(f"占位行 {i}" for i in range(3, 20))
    ch2 = "第二章 之后\n向山死了，这是天大的剧透。"
    source.write_text(f"{ch1}\n{filler}\n{ch2}\n", encoding="utf-8")
    prefix_bytes = len(f"{ch1}\n{filler}\n".encode())

    early = Chunk(
        id="c0",
        source_path=str(source),
        source_hash="h",
        chunk_index=0,
        text=ch1,
        text_hash="t0",
        line_start=1,
        line_end=2,
        byte_start=0,
        byte_end=len(ch1.encode()),
        chapter="第一章 开始",
    )
    late = Chunk(
        id="c1",
        source_path=str(source),
        source_hash="h",
        chunk_index=1,
        text=ch2,
        text_hash="t1",
        line_start=20,
        line_end=21,
        byte_start=prefix_bytes,
        byte_end=prefix_bytes + len(ch2.encode()),
        chapter="第二章 之后",
    )

    store = ZvecChunkStore.open_for_write(
        index_dir=index_dir,
        metadata_dir=metadata_dir,
        collection="books",
        dimension=2,
        rebuild=True,
    )
    store.upsert(
        [early, late], model="e", embed=lambda t: [[1.0, 0.0], [0.0, 1.0]][: len(t)]
    )
    store.commit(optimize=True)
    del store
    gc.collect()
    artifacts.write_source(
        manifest=IndexManifest(
            collection="books",
            collection_path=str(collection_path(index_dir, "books")),
            source_path=str(source),
            model="e",
            embedding_dimension=2,
            chunk_count=2,
            chunk_chars=100,
            overlap_chars=10,
            chunker_version=3,
        ),
        chunks=[early, late],
    )

    def run_fts():
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

    # --- 编辑前：--max-chapter 1 正确挡住第二章 ---
    code, out = run_fts()
    print(f"\n[编辑前] exit={code}  第二章泄露={'剧透' in out}")
    assert code == 0 and "剧透" not in out, "基线就该挡住"

    # --- 用户在卷首加了 30 行（真实操作：补卷首语/修错字）——不重新 index ---
    source.write_text(
        "卷首语\n" * 30 + source.read_text(encoding="utf-8"), encoding="utf-8"
    )

    code, out = run_fts()
    leaked = "剧透" in out
    print(f"[编辑后] exit={code}  第二章泄露={leaked}")
    assert not leaked, "源文件被编辑后，--max-chapter 放行了超出进度的章节"
