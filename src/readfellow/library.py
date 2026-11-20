from __future__ import annotations

import logging
import sqlite3
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

import jieba
import numpy as np

from .models import ChapterBoundary, Chunk, Evidence, IndexManifest, ProgressFilter

jieba.setLogLevel(logging.WARNING)

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE book (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    source_path TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    chunker_version INTEGER NOT NULL,
    chunk_chars INTEGER NOT NULL,
    overlap_chars INTEGER NOT NULL,
    model TEXT NOT NULL,
    embedding_dimension INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL
);
CREATE TABLE chapters (
    ordinal INTEGER PRIMARY KEY,
    volume INTEGER NOT NULL,
    number INTEGER,
    title TEXT NOT NULL,
    line_start INTEGER NOT NULL UNIQUE,
    line_end INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE chunks (
    id TEXT PRIMARY KEY NOT NULL,
    chunk_index INTEGER NOT NULL UNIQUE,
    chapter_ordinal INTEGER REFERENCES chapters(ordinal),
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    byte_start INTEGER NOT NULL,
    byte_end INTEGER NOT NULL,
    chapter TEXT NOT NULL,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    tokens TEXT NOT NULL,
    embedding BLOB NOT NULL
);
CREATE VIRTUAL TABLE chunk_fts USING fts5(tokens);
"""

EVIDENCE_COLUMNS = """
    id AS chunk_id, chunk_index, line_start, line_end, byte_start, byte_end,
    chapter, text, text_hash, book.source_path
"""


def _tokens(text: str) -> list[str]:
    return [
        token
        for token in jieba.cut(text)
        if any(
            not (c.isspace() or unicodedata.category(c).startswith("P")) for c in token
        )
    ]


def _limits(progress: ProgressFilter) -> tuple[str, list[int]]:
    clauses = []
    values = []
    for column, maximum in (
        ("line_end", progress.max_line_end),
        ("chunk_index", progress.max_chunk_index),
    ):
        if maximum is not None:
            clauses.append(f" AND {column} <= ?")
            values.append(maximum)
    return "".join(clauses), values


@dataclass(frozen=True)
class UpsertOutcome:
    written: int
    skipped: int


class SqliteChunkStore:
    def __init__(self, connection: sqlite3.Connection):
        self._db = connection

    @classmethod
    @contextmanager
    def open(
        cls, path: Path, *, write: bool = False, rebuild: bool = False
    ) -> Iterator[SqliteChunkStore]:
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)
            if rebuild:
                path.unlink(missing_ok=True)
        elif not path.is_file():
            raise FileNotFoundError(
                f"collection does not exist: {path}; run the index command first"
            )
        new = write and not path.exists()
        uri = path.resolve().as_uri() + ("?mode=rwc" if write else "?mode=ro")
        with closing(sqlite3.connect(uri, uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            if new:
                db.executescript(
                    f"BEGIN;\n{SCHEMA}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
                )
            if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported library schema in {path}; run index --rebuild"
                )
            if not write:
                db.execute("BEGIN")
            yield cls(db)

    def manifest(self) -> IndexManifest | None:
        row = self._db.execute("SELECT * FROM book").fetchone()
        if row is None:
            return None
        return IndexManifest(**{k: row[k] for k in IndexManifest.model_fields})

    def chapters(self) -> list[ChapterBoundary]:
        rows = self._db.execute("""
            SELECT ordinal, volume, number, title, line_start, line_end, content_hash
            FROM chapters ORDER BY ordinal
        """)
        return [ChapterBoundary(**row) for row in rows]

    def prepare(self, book: IndexManifest, chapters: list[ChapterBoundary]) -> None:
        with self._db as db:
            db.execute("BEGIN IMMEDIATE")
            existing = self.manifest()
            if existing is not None:
                if existing.model_dump(exclude={"chunk_count"}) != book.model_dump(
                    exclude={"chunk_count"}
                ):
                    raise RuntimeError(
                        "source, chunking or embedding settings changed; run index --rebuild"
                    )
                db.execute("UPDATE book SET chunk_count = ?", (book.chunk_count,))
                return
            db.execute(
                """
                INSERT INTO book VALUES (1, :source_path, :source_hash, :chunker_version,
                    :chunk_chars, :overlap_chars, :model, :embedding_dimension, :chunk_count)
            """,
                book.model_dump(),
            )
            db.executemany(
                """
                INSERT INTO chapters VALUES (:ordinal, :volume, :number, :title,
                    :line_start, :line_end, :content_hash, :text)
            """,
                (chapter.model_dump() for chapter in chapters),
            )

    def upsert(
        self,
        chunks: Sequence[Chunk],
        *,
        model: str,
        embed: Callable[[list[str]], list[list[float]]],
    ) -> UpsertOutcome:
        pending = []
        for chunk in chunks:
            row = self._db.execute(
                "SELECT text_hash FROM chunks WHERE id = ?", (chunk.id,)
            ).fetchone()
            if row is None or row[0] != chunk.text_hash:
                pending.append(chunk)
        if pending:
            vectors = embed([chunk.text for chunk in pending])
            with self._db as db:
                dimension = db.execute(
                    "SELECT embedding_dimension FROM book"
                ).fetchone()[0]
                for chunk, vector in zip(pending, vectors, strict=True):
                    array = np.asarray(vector, dtype="<f4")
                    if array.shape != (dimension,):
                        raise ValueError(
                            f"expected embedding dimension {dimension}, got {array.shape}"
                        )
                    tokens = " ".join(_tokens(chunk.text))
                    values = chunk.model_dump() | {
                        "embedding": array.tobytes(),
                        "tokens": tokens,
                    }
                    # An explicit rowid keeps the FTS entry paired on replacement.
                    db.execute(
                        """
                        INSERT OR REPLACE INTO chunks (
                            rowid, id, chunk_index, chapter_ordinal, line_start, line_end,
                            byte_start, byte_end, chapter, text, text_hash, tokens, embedding
                        ) VALUES (:chunk_index + 1, :id, :chunk_index,
                            (SELECT ordinal FROM chapters
                             WHERE :line_start BETWEEN line_start AND line_end
                             ORDER BY line_start DESC LIMIT 1),
                            :line_start, :line_end, :byte_start, :byte_end, :chapter,
                            :text, :text_hash, :tokens, :embedding)
                    """,
                        values,
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO chunk_fts(rowid, tokens) VALUES (?, ?)",
                        (chunk.chunk_index + 1, tokens),
                    )
        return UpsertOutcome(len(pending), len(chunks) - len(pending))

    def commit(self, *, optimize: bool) -> None:
        if optimize:
            with self._db:
                self._db.execute("INSERT INTO chunk_fts(chunk_fts) VALUES ('optimize')")

    def search_vector(
        self, vector: list[float], *, top_k: int, progress: ProgressFilter
    ) -> list[Evidence]:
        limits, values = _limits(progress)
        rows = self._db.execute(
            "SELECT id, embedding FROM chunks WHERE 1"
            + limits
            + " ORDER BY chunk_index",
            values,
        ).fetchall()
        if not rows:
            return []
        matrix = np.stack(
            [np.frombuffer(row["embedding"], dtype="<f4") for row in rows]
        )
        scores = matrix @ np.asarray(vector, dtype="<f4")
        result = []
        for i in np.argsort(-scores, kind="stable")[:top_k]:
            row = self._evidence_row(rows[i]["id"])
            result.append(
                Evidence(**row, retrieval_mode="vector", score=float(scores[i]))
            )
        return result

    def search_fts(
        self, query: str, *, top_k: int, progress: ProgressFilter
    ) -> list[Evidence]:
        terms = ['"' + token.replace('"', '""') + '"' for token in _tokens(query)]
        if not terms:
            return []
        limits, values = _limits(progress)
        rows = self._db.execute(
            f"""
            SELECT {EVIDENCE_COLUMNS}, bm25(chunk_fts) AS score
            FROM chunk_fts JOIN chunks ON chunks.rowid = chunk_fts.rowid CROSS JOIN book
            WHERE chunk_fts MATCH ? {limits}
            ORDER BY score, chunk_index LIMIT ?
        """,
            [" OR ".join(terms), *values, top_k],
        )
        return [Evidence(**row, retrieval_mode="fts") for row in rows]

    def _evidence_row(self, chunk_id: str) -> sqlite3.Row | None:
        return self._db.execute(
            f"SELECT {EVIDENCE_COLUMNS} FROM chunks CROSS JOIN book WHERE id = ?",
            (chunk_id,),
        ).fetchone()

    def fetch(self, chunk_id: str) -> Evidence | None:
        row = self._evidence_row(chunk_id)
        return Evidence(**row, retrieval_mode="fetch") if row else None

    def chunk_count(self) -> int:
        return self._db.execute("SELECT count(*) FROM chunks").fetchone()[0]
