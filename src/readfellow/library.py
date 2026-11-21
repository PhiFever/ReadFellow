from __future__ import annotations

import json
import logging
import sqlite3
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

import jieba
import numpy as np

from .models import (
    ChapterBoundary,
    ChapterMoments,
    ChapterSummary,
    Chunk,
    Evidence,
    IdentityLink,
    IndexManifest,
    LinkEvidence,
    NoteConfig,
    ProgressFilter,
    ReaderState,
)

jieba.setLogLevel(logging.WARNING)

SCHEMA_VERSION = 4
NOTE_CONFIG_SCHEMA = """
CREATE TABLE IF NOT EXISTS note_configs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    activated INTEGER NOT NULL
)
"""
NOTE_SCHEMA = """
CREATE TABLE IF NOT EXISTS chapter_notes (
    config_id INTEGER NOT NULL REFERENCES note_configs(id),
    content_hash TEXT NOT NULL,
    body TEXT NOT NULL,
    spoiler INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (config_id, content_hash)
)
"""
LINK_SCHEMA = """
CREATE TABLE IF NOT EXISTS identity_links (
    config_id INTEGER NOT NULL REFERENCES note_configs(id),
    input_hash TEXT NOT NULL,
    body TEXT NOT NULL,
    current INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (config_id, input_hash)
)
"""
LATEST_NOTE = (
    "(SELECT id FROM note_configs WHERE kind=:kind ORDER BY activated DESC LIMIT 1)"
)
READER_SCHEMA = """
CREATE TABLE IF NOT EXISTS reader_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    kind TEXT NOT NULL CHECK (kind IN ('all', 'chapter', 'line')),
    value TEXT,
    line_limit INTEGER,
    source_hash TEXT NOT NULL,
    set_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((kind = 'all' AND value IS NULL AND line_limit IS NULL)
        OR (kind != 'all' AND value IS NOT NULL AND line_limit IS NOT NULL AND line_limit > 0))
)
"""
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
        elif not path.is_file():
            raise FileNotFoundError(
                f"collection does not exist: {path}; run the index command first"
            )
        uri = path.resolve().as_uri() + ("?mode=rwc" if write else "?mode=ro")
        with closing(sqlite3.connect(uri, uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version < SCHEMA_VERSION and not write:
                raise RuntimeError("run progress set to upgrade the library")
            if version != SCHEMA_VERSION and not (
                write and (version in (0, 1, 2, 3) or rebuild)
            ):
                raise RuntimeError(
                    f"unsupported library schema in {path}; run index --rebuild"
                )
            if write:
                if rebuild:
                    for name in ("chunk_fts", "chunks", "chapters", "book"):
                        db.execute(f"DROP TABLE IF EXISTS {name}")
                if version == 0 or rebuild:
                    for statement in SCHEMA.strip().rstrip(";").split(";"):
                        db.execute(statement)
                if version != SCHEMA_VERSION:
                    db.execute(READER_SCHEMA)
                    db.execute(NOTE_CONFIG_SCHEMA)
                    db.execute(NOTE_SCHEMA)
                    db.execute(LINK_SCHEMA)
                    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            yield cls(db)
            if write:
                db.commit()

    def activate_config(self, kind: str, config: NoteConfig) -> int:
        fingerprint = json.dumps(
            config.model_dump(), sort_keys=True, ensure_ascii=False
        )
        with self._db as db:
            db.execute(
                "INSERT OR IGNORE INTO note_configs(kind, fingerprint, activated) VALUES (?, ?, 0)",
                (kind, fingerprint),
            )
            return db.execute(
                "UPDATE note_configs SET activated=(SELECT max(activated)+1 FROM note_configs) WHERE fingerprint=? RETURNING id",
                (fingerprint,),
            ).fetchone()[0]

    def cached_note_hashes(self, config_id: int) -> set[str]:
        return {
            row[0]
            for row in self._db.execute(
                "SELECT content_hash FROM chapter_notes WHERE config_id=?", (config_id,)
            )
        }

    def notes[Note: (ChapterSummary, ChapterMoments)](
        self,
        model: type[Note],
        *,
        ordinals: tuple[int, int] | None = None,
        visible_only: bool = False,
        end: int | None = None,
    ) -> dict[str, Note]:
        first, last = ordinals or (None, None)
        rows = self._db.execute(
            f"""
            SELECT n.content_hash, n.body FROM chapters c JOIN chapter_notes n USING(content_hash)
            WHERE n.config_id={LATEST_NOTE} AND (:first IS NULL OR c.ordinal BETWEEN :first AND :last)
            AND (:end IS NULL OR c.line_end <= :end) AND (:visible=0 OR n.spoiler=0)
        """,
            {
                "kind": model.note_kind,
                "first": first,
                "last": last,
                "end": end,
                "visible": visible_only,
            },
        )
        return {key: model.model_validate_json(body) for key, body in rows}

    def write_note(
        self, config_id: int, key: str, note: ChapterSummary | ChapterMoments
    ) -> None:
        with self._db as db:
            # Keep unscanned notes hidden even if the process is interrupted.
            db.execute(
                "INSERT OR IGNORE INTO chapter_notes(config_id, content_hash, body, spoiler) VALUES (?, ?, ?, 1)",
                (config_id, key, note.model_dump_json()),
            )

    @contextmanager
    def summary_scan(self) -> Iterator[None]:
        with self._db as db:
            db.execute("BEGIN IMMEDIATE")
            yield

    def set_spoilers(
        self,
        kind: str,
        flags: dict[str, bool],
        bodies: dict[str, ChapterMoments] | None = None,
    ) -> None:
        self._db.executemany(
            f"UPDATE chapter_notes SET spoiler=:flag, body=coalesce(:body, body) WHERE config_id={LATEST_NOTE} AND content_hash=:key",
            (
                {
                    "flag": flag,
                    "body": bodies[key].model_dump_json() if bodies else None,
                    "kind": kind,
                    "key": key,
                }
                for key, flag in flags.items()
            ),
        )

    def note_counts(self, kind: str) -> tuple[int, int] | None:
        row = self._db.execute(
            f"""
            SELECT count(c.ordinal), coalesce(sum(n.spoiler), 0)
            FROM chapters c JOIN chapter_notes n USING(content_hash)
            WHERE n.config_id={LATEST_NOTE} HAVING {LATEST_NOTE} IS NOT NULL
        """,
            {"kind": kind},
        ).fetchone()
        return (row[0], row[1]) if row else None

    def cached_link_hashes(self, config_id: int) -> set[str]:
        rows = self._db.execute(
            "SELECT input_hash FROM identity_links WHERE config_id=?", (config_id,)
        )
        return {key for (key,) in rows}

    def write_link(self, config_id: int, key: str, link: IdentityLink) -> None:
        with self._db as db:
            db.execute(
                "INSERT OR IGNORE INTO identity_links(config_id, input_hash, body) VALUES (?, ?, ?)",
                (config_id, key, link.model_dump_json()),
            )

    def publish_links(self, config_id: int, links: dict[str, LinkEvidence]) -> set[str]:
        (latest,) = self._db.execute(
            f"SELECT {LATEST_NOTE}", {"kind": "links"}
        ).fetchone()
        if latest != config_id:
            raise RuntimeError(
                "links configuration changed during analyze; run analyze again"
            )
        # A cached verdict may now cite a different absolute line after a rebuild.
        self._db.execute("UPDATE identity_links SET current=0 WHERE current=1")
        self._db.executemany(
            "UPDATE identity_links SET current=1, body=json_patch(body, ?) WHERE config_id=? AND input_hash=?",
            ((link.model_dump_json(), config_id, key) for key, link in links.items()),
        )

        rows = self._db.execute(
            "SELECT input_hash FROM identity_links WHERE config_id=? AND current=1 AND json_extract(body, '$.same')",
            (config_id,),
        )
        return {key for (key,) in rows}

    def visible_links(self, end: int | None) -> Iterator[IdentityLink]:
        rows = self._db.execute(
            f"""SELECT l.body, c.line_start, c.text FROM identity_links l JOIN chapters c
                ON c.ordinal=json_extract(l.body, '$.ordinal')
                AND c.content_hash=json_extract(l.body, '$.content_hash')
                WHERE l.config_id={LATEST_NOTE} AND l.current=1
                AND json_extract(l.body, '$.same') AND (:end IS NULL OR c.line_end<=:end)
                ORDER BY c.ordinal, json_extract(l.body, '$.line_start'), l.input_hash""",
            {"kind": "links", "end": end},
        )
        last_ordinal = None
        for body, chapter_start, text in rows:
            link = IdentityLink.model_validate_json(body)
            if link.ordinal != last_ordinal:
                lines = text.splitlines(keepends=True)
                last_ordinal = link.ordinal
            # Earlier chapters can shift this citation without changing its chapter hash.
            first = link.line_start - chapter_start
            last = link.line_end - chapter_start
            if 0 <= first <= last < len(lines) and link.evidence in "".join(
                lines[first : last + 1]
            ):
                yield link

    def link_counts(self) -> tuple[int, int] | None:
        row = self._db.execute(
            f"""SELECT count(*), coalesce(sum(json_extract(body, '$.same')), 0)
                FROM identity_links WHERE config_id={LATEST_NOTE} AND current=1
                HAVING {LATEST_NOTE} IS NOT NULL""",
            {"kind": "links"},
        ).fetchone()
        return (row[0], row[1]) if row else None

    def reader_state(self) -> ReaderState | None:
        row = self._db.execute(
            "SELECT kind, value, line_limit, source_hash, set_at FROM reader_state"
        ).fetchone()
        return ReaderState(**row) if row else None

    def save_progress(
        self, kind: str, value: str | None, line_limit: int | None, source_hash: str
    ) -> ReaderState:
        self._db.execute(
            "INSERT OR REPLACE INTO reader_state (singleton, kind, value, line_limit, source_hash) VALUES (1, ?, ?, ?, ?)",
            (kind, value, line_limit, source_hash),
        )
        state = self.reader_state()
        assert state is not None
        return state

    def chunk_line_limit(self, index: int) -> int:
        return self._db.execute(
            "SELECT coalesce(max(line_end), 0) FROM chunks WHERE chunk_index <= ?",
            (index,),
        ).fetchone()[0]

    def chapter_texts(
        self, end: int | None, ordinals: tuple[int, int] | None, start: int = 1
    ) -> Iterator[ChapterBoundary]:
        first, last = ordinals or (None, None)
        rows = self._db.execute(
            """SELECT * FROM chapters
               WHERE line_end >= :start AND (:end IS NULL OR line_start <= :end)
                 AND (:first IS NULL OR ordinal BETWEEN :first AND :last)
               ORDER BY ordinal""",
            {"start": start, "end": end, "first": first, "last": last},
        )
        for row in rows:
            yield ChapterBoundary(**row)

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
