"""Versioned SQL persistence for metadata and derived artifacts.

The application injects this one boundary; SQLAlchemy objects never cross it.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Collection
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Literal

from sqlalchemy import create_engine, delete, event, insert, select, update
from sqlalchemy.engine import Connection, Engine, make_url

from . import artifact_schema as db
from .models import AnalysisDocument, Chunk, IndexManifest, KnowledgeGraph

if TYPE_CHECKING:
    from .config import ReadFellowConfig

Document = KnowledgeGraph | AnalysisDocument
Kind = Literal["graph", "analysis"]


@dataclass(frozen=True)
class StoredRun:
    id: int
    source_version_id: int
    document: Document


@dataclass(frozen=True)
class ImportResult:
    source_version_id: int
    graph_run_id: int | None
    analysis_run_id: int | None
    skipped: bool = False


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def make_engine(uri: str) -> Engine:
    url = make_url(uri)
    if url.drivername in {"mysql", "mysql+pymysql"}:
        url = url.set(drivername="mysql+pymysql").update_query_dict(
            {"charset": "utf8mb4"}
        )
    options = (
        {"isolation_level": "REPEATABLE READ"}
        if url.get_backend_name() == "mysql"
        else {}
    )
    engine = create_engine(url, pool_pre_ping=True, hide_parameters=True, **options)
    if url.get_backend_name() == "sqlite":

        @event.listens_for(engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    return engine


class ArtifactStore:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._snapshots: dict[int, dict[str, list[dict]]] = {}
        # The runs row as this store last read or wrote it, to catch a second writer.
        self._run_rows: dict[int, dict] = {}

    @classmethod
    def open(cls, uri: str) -> ArtifactStore:
        return cls(make_engine(uri))

    def initialize(self) -> None:
        db.metadata.create_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()

    def read_source(self, collection: str) -> tuple[int, IndexManifest, list[Chunk]]:
        with self.engine.connect() as connection:
            source_id, manifest = self._read_manifest_row(connection, collection)
            chunks = [
                Chunk.model_validate(
                    {k: v for k, v in item.items() if k != "source_version_id"}
                )
                for item in connection.execute(
                    select(db.chunks)
                    .where(db.chunks.c.source_version_id == source_id)
                    .order_by(db.chunks.c.chunk_index)
                ).mappings()
            ]
            return source_id, manifest, chunks

    def read_manifest(
        self, collection: str
    ) -> tuple[int, IndexManifest, set[tuple[str, str]]]:
        """The latest manifest and the distinct (source_path, source_hash) of its chunks.

        Enough to check the chunks against the source file without reading their text.
        """
        with self.engine.connect() as connection:
            source_id, manifest = self._read_manifest_row(connection, collection)
            identities = {
                (row.source_path, row.source_hash)
                for row in connection.execute(
                    select(db.chunks.c.source_path, db.chunks.c.source_hash)
                    .where(db.chunks.c.source_version_id == source_id)
                    .distinct()
                )
            }
            return source_id, manifest, identities

    def _read_manifest_row(self, connection, collection):
        row = (
            connection.execute(
                select(db.sources)
                .where(db.sources.c.collection == collection)
                .order_by(db.sources.c.id.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )
        if row is None:
            raise FileNotFoundError(
                f"chunk metadata not found for {collection}; run index or import-json first"
            )
        values = dict(row)
        source_id = values.pop("id")
        values.pop("fingerprint")
        values.update(values.pop("extra"))
        return source_id, IndexManifest.model_validate(values)

    def write_source(self, manifest: IndexManifest, chunks: list[Chunk]) -> int:
        with self.engine.begin() as connection:
            return self._write_source(connection, manifest, chunks)

    def _write_source(self, connection, manifest, chunks):
        digest = fingerprint(
            [
                manifest.model_dump(mode="json"),
                [c.model_dump(mode="json") for c in chunks],
            ]
        )
        latest = connection.execute(
            select(db.sources.c.id, db.sources.c.fingerprint)
            .where(db.sources.c.collection == manifest.collection)
            .order_by(db.sources.c.id.desc())
            .limit(1)
        ).first()
        if latest and latest.fingerprint == digest:
            return latest.id
        values = manifest.model_dump(mode="json")
        extra = {k: values.pop(k) for k in list(values) if k not in db.sources.c}
        source_id = connection.execute(
            insert(db.sources).values(**values, extra=extra, fingerprint=digest)
        ).inserted_primary_key[0]
        self._insert(
            connection,
            db.chunks,
            [
                dict(c.model_dump(mode="json"), source_version_id=source_id)
                for c in chunks
            ],
        )
        return source_id

    def latest(self, collection: str, kind: Kind) -> StoredRun | None:
        with self.engine.connect() as connection:
            row = self._latest_row(connection, collection, kind)
            return self._read_run(connection, row) if row else None

    def read_graph_slice(
        self, collection: str, chunk_ids: Collection[str]
    ) -> StoredRun | None:
        """The latest graph run, holding only what `graph.annotate_chunks` reads.

        Entities keep all their values, mentions and evidence, because ranking
        needs each entity's spread over the whole book. Run fields, extractions
        and fingerprints stay complete for the staleness check. The result is
        not the whole document, so it never becomes the baseline `save` diffs
        against.
        """
        with self.engine.connect() as connection:
            row = self._latest_row(connection, collection, "graph")
            if row is None:
                return None
            return self._assemble(
                row, self._read_slice_rows(connection, row, set(chunk_ids))
            )

    @staticmethod
    def _latest_row(connection, collection, kind):
        return (
            connection.execute(
                select(db.runs)
                .where(db.runs.c.collection == collection, db.runs.c.kind == kind)
                .order_by(db.runs.c.id.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )

    def read_run(self, run_id: int) -> StoredRun:
        with self.engine.connect() as connection:
            row = (
                connection.execute(select(db.runs).where(db.runs.c.id == run_id))
                .mappings()
                .one()
            )
            return self._read_run(connection, row)

    def prepare(
        self,
        source_id: int,
        empty: Document,
        *,
        stale: Callable[[Document], str | None],
        rebuild: bool,
    ) -> tuple[StoredRun, bool]:
        kind = "graph" if isinstance(empty, KnowledgeGraph) else "analysis"
        previous = self.latest(empty.collection, kind)
        reset = previous is not None and (
            rebuild or stale(previous.document) is not None
        )
        if previous is not None and not reset:
            # A source snapshot may have gained unprocessed chunks. The caller's
            # staleness check has established that every processed chunk matches.
            return StoredRun(previous.id, source_id, previous.document), False
        with self.engine.begin() as connection:
            run_id, run_row = self._create_run(connection, source_id, empty)
        self._run_rows[run_id] = run_row
        return StoredRun(run_id, source_id, empty), reset

    def save(self, run: StoredRun) -> None:
        with self.engine.begin() as connection:
            rows, run_row = self._save(connection, run)
        self._snapshots[run.id] = rows
        self._run_rows[run.id] = run_row

    def _create_run(self, connection, source_id, document):
        values = self._run_values(document)
        run_id = connection.execute(
            insert(db.runs).values(source_version_id=source_id, **values)
        ).inserted_primary_key[0]
        _, run_row = self._save(connection, StoredRun(run_id, source_id, document))
        return run_id, run_row

    @staticmethod
    def _run_values(document):
        graph = isinstance(document, KnowledgeGraph)
        excluded = (
            {
                "entities",
                "relations",
                "extractions",
                "source_chunk_hashes",
                "entity_count",
                "relation_count",
            }
            if graph
            else {"chapters", "chunk_text_hashes"}
        )
        values = document.model_dump(mode="json", exclude=excluded)
        values["settings"] = values.pop("extraction_settings" if graph else "settings")
        values["selected_count"] = values.pop(
            "selected_chunk_count" if graph else "selected_chapter_count"
        )
        values["processed_count"] = values.pop(
            "processed_chunk_count" if graph else "processed_chapter_count"
        )
        extra = {k: values.pop(k) for k in list(values) if k not in db.runs.c}
        if graph:
            extra.update(
                entity_count=document.entity_count,
                relation_count=document.relation_count,
            )
        return dict(values, kind="graph" if graph else "analysis", extra=extra)

    def _rows(self, run):
        rows = {table.name: [] for table in db.run_tables}

        def add(table, value, **parent):
            values = (
                value.model_dump(mode="json")
                if hasattr(value, "model_dump")
                else dict(value)
            )
            if table is db.entities:
                identity = values["name"]
            elif table is db.chapters:
                identity = [values["chapter_index"], values["chapter_title"]]
            elif table in (db.extractions, db.fingerprints):
                identity = values["chunk_id"]
            else:
                identity = [
                    values,
                    {k: v for k, v in parent.items() if k != "source_version_id"},
                ]
            position = fingerprint(identity)
            rows[table.name].append(
                dict(
                    values,
                    run_id=run.id,
                    position=position,
                    sort_order=len(rows[table.name]),
                    **parent,
                )
            )
            return position

        def context_add(table, value, **parent):
            add(table, value, source_version_id=run.source_version_id, **parent)

        document = run.document
        if isinstance(document, KnowledgeGraph):
            for entity in document.entities:
                i = add(db.entities, {"name": entity.name})
                for kind in ("aliases", "types"):
                    for value in getattr(entity, kind):
                        add(
                            db.entity_values,
                            {"kind": kind, "value": value},
                            entity_position=i,
                        )
                for table, values in (
                    (db.entity_mentions, entity.mentions),
                    (db.entity_evidence, entity.evidence),
                ):
                    for value in values:
                        context_add(table, value, entity_position=i)
            names = {
                entity.name: fingerprint(entity.name) for entity in document.entities
            }
            for relation in document.relations:
                context_add(
                    db.relations,
                    relation,
                    subject_position=names.get(
                        relation.subject_entity or relation.subject
                    ),
                    object_position=names.get(
                        relation.object_entity or relation.object
                    ),
                )
            for record in document.extractions:
                context_add(db.extractions, record)
            for chunk_id, hashes in document.source_chunk_hashes.items():
                context_add(
                    db.fingerprints, dict(hashes.model_dump(), chunk_id=chunk_id)
                )
        else:
            for chapter in document.chapters:
                i = add(
                    db.chapters,
                    chapter.model_dump(exclude={"chunk_ids", "characters", "events"}),
                )
                for chunk_id in chapter.chunk_ids:
                    context_add(
                        db.chapter_chunks, {"chunk_id": chunk_id}, chapter_position=i
                    )
                for table, values in (
                    (db.characters, chapter.characters),
                    (db.events, chapter.events),
                ):
                    for value in values:
                        context_add(table, value, chapter_position=i)
            for chunk_id, text_hash in document.chunk_text_hashes.items():
                context_add(
                    db.fingerprints,
                    dict(chunk_id=chunk_id, source_hash="", text_hash=text_hash),
                )
        return rows

    def _save(self, connection: Connection, run: StoredRun):
        # Lock one run while replacing its committed snapshot. The model call is
        # outside this transaction. Other runs and historical snapshots are untouched.
        current = dict(
            connection.execute(
                select(db.runs).where(db.runs.c.id == run.id).with_for_update()
            )
            .mappings()
            .one()
        )
        # One writer per run: a document merged from a stale read would diff
        # against rows it never saw, and silently keep or drop another writer's.
        expected = self._run_rows.get(run.id)
        if expected is not None and current != expected:
            raise RuntimeError(
                f"run {run.id} was written by another process since this one "
                "read it; rerun to resume from what that process saved"
            )
        rows = self._rows(run)
        old_rows = self._snapshots.get(run.id)
        if old_rows is None:
            old_rows = self._read_rows(connection, run.id)
        changes = {}
        for table in db.run_tables:
            old = {r["position"]: r for r in old_rows[table.name]}
            new = {r["position"]: r for r in rows[table.name]}
            if table in (
                db.entities,
                db.entity_values,
                db.entity_mentions,
                db.entity_evidence,
            ):
                # These lists have domain ordering (name/value/chunk position).
                # Adding a name must not rewrite every later entity's rows just
                # because its ordinal in the whole graph shifted.
                for key in old.keys() & new.keys():
                    new[key]["sort_order"] = old[key]["sort_order"]
            changes[table.name] = (old, new)
        # Delete children before parents; unchanged rows are never rewritten.
        for table in reversed(db.run_tables):
            old, new = changes[table.name]
            removed = [
                k
                for k in old
                if k not in new
                or (old[k] != new[k] and table not in (db.entities, db.chapters))
            ]
            for offset in range(0, len(removed), 500):
                connection.execute(
                    delete(table).where(
                        table.c.run_id == run.id,
                        table.c.position.in_(removed[offset : offset + 500]),
                    )
                )
        for table in db.run_tables:
            old, new = changes[table.name]
            added = []
            for key, row in new.items():
                if key in old and old[key] == row:
                    continue
                if key in old and table in (db.entities, db.chapters):
                    connection.execute(
                        update(table)
                        .where(table.c.run_id == run.id, table.c.position == key)
                        .values(**row)
                    )
                else:
                    added.append(row)
            self._insert(connection, table, added)
        connection.execute(
            update(db.runs)
            .where(db.runs.c.id == run.id)
            .values(
                source_version_id=run.source_version_id,
                **self._run_values(run.document),
            )
        )
        run_row = dict(
            connection.execute(select(db.runs).where(db.runs.c.id == run.id))
            .mappings()
            .one()
        )
        return rows, run_row

    @staticmethod
    def _insert(connection, table, rows):
        for offset in range(0, len(rows), 500):
            connection.execute(insert(table), rows[offset : offset + 500])

    @staticmethod
    def _read_rows(connection, run_id):
        return {
            table.name: [
                dict(r)
                for r in connection.execute(
                    select(table)
                    .where(table.c.run_id == run_id)
                    .order_by(table.c.sort_order)
                ).mappings()
            ]
            for table in db.run_tables
        }

    @staticmethod
    def _read_slice_rows(connection, row, chunk_ids):
        run_id = row["id"]

        def in_chunks(table):
            # Every row of a run carries the run's source version, since `_save`
            # rewrites them together; matching it lets the chunk index serve this.
            return (
                table.c.source_version_id == row["source_version_id"],
                table.c.chunk_id.in_(chunk_ids),
            )

        def rows(table, *conditions):
            return [
                dict(r)
                for r in connection.execute(
                    select(table)
                    .where(table.c.run_id == run_id, *conditions)
                    .order_by(table.c.sort_order)
                ).mappings()
            ]

        data = {table.name: [] for table in db.run_tables}
        for table in (db.extractions, db.fingerprints):
            data[table.name] = rows(table)
        data["relations"] = rows(db.relations, *in_chunks(db.relations))
        # Merging a relation mentions both endpoints in its chunk, so the
        # entities anchored to these chunks include every relation endpoint.
        positions = set()
        for table in (db.entity_mentions, db.entity_evidence):
            positions.update(
                connection.scalars(
                    select(table.c.entity_position)
                    .where(table.c.run_id == run_id, *in_chunks(table))
                    .distinct()
                )
            )
        data["entities"] = rows(db.entities, db.entities.c.position.in_(positions))
        for table in (db.entity_values, db.entity_mentions, db.entity_evidence):
            data[table.name] = rows(table, table.c.entity_position.in_(positions))
        return data

    def _read_run(self, connection, row):
        data = self._read_rows(connection, row["id"])
        self._snapshots[row["id"]] = data
        self._run_rows[row["id"]] = dict(row)
        return self._assemble(row, data)

    @staticmethod
    def _assemble(row, data):
        values = dict(row)
        run_id, source_id = values.pop("id"), values.pop("source_version_id")
        kind = values.pop("kind")
        values.update(values.pop("extra"))
        graph = kind == "graph"
        values["extraction_settings" if graph else "settings"] = values.pop("settings")
        values["selected_chunk_count" if graph else "selected_chapter_count"] = (
            values.pop("selected_count")
        )
        values["processed_chunk_count" if graph else "processed_chapter_count"] = (
            values.pop("processed_count")
        )
        grouped = {}
        for name, parent in (
            ("entity_values", "entity_position"),
            ("entity_mentions", "entity_position"),
            ("entity_evidence", "entity_position"),
            ("chapter_chunks", "chapter_position"),
            ("characters", "chapter_position"),
            ("events", "chapter_position"),
        ):
            grouped[name] = defaultdict(list)
            for item in data[name]:
                grouped[name][item[parent]].append(item)

        def clean(item, *omit):
            return {
                k: v
                for k, v in item.items()
                if k
                not in {"run_id", "position", "sort_order", "source_version_id", *omit}
            }

        if graph:
            values["entities"] = []
            for entity in sorted(data["entities"], key=lambda item: item["name"]):
                position = entity["position"]
                item = clean(entity)
                for kind in ("aliases", "types"):
                    item[kind] = sorted(
                        v["value"]
                        for v in grouped["entity_values"][position]
                        if v["kind"] == kind
                    )
                for field, table in (
                    ("mentions", "entity_mentions"),
                    ("evidence", "entity_evidence"),
                ):
                    item[field] = [
                        clean(v, "entity_position")
                        for v in sorted(
                            grouped[table][position],
                            key=lambda item: (
                                item["chunk_index"],
                                item["line_start"],
                                item["sort_order"],
                            ),
                        )
                    ]
                values["entities"].append(item)
            values["relations"] = [
                clean(v, "subject_position", "object_position")
                for v in data["relations"]
            ]
            values["extractions"] = [clean(v) for v in data["extractions"]]
            values["source_chunk_hashes"] = {
                v["chunk_id"]: {
                    "source_hash": v["source_hash"],
                    "text_hash": v["text_hash"],
                }
                for v in data["run_chunks"]
            }
            document = KnowledgeGraph.model_validate(values)
        else:
            values["chapters"] = []
            for chapter in data["chapters"]:
                position = chapter["position"]
                item = clean(chapter)
                item["chunk_ids"] = [
                    v["chunk_id"] for v in grouped["chapter_chunks"][position]
                ]
                for field in ("characters", "events"):
                    item[field] = [
                        clean(v, "chapter_position") for v in grouped[field][position]
                    ]
                values["chapters"].append(item)
            values["chunk_text_hashes"] = {
                v["chunk_id"]: v["text_hash"] for v in data["run_chunks"]
            }
            document = AnalysisDocument.model_validate(values)
        return StoredRun(run_id, source_id, document)

    def import_documents(
        self,
        digest: str,
        manifest: IndexManifest,
        chunks: list[Chunk],
        graph: KnowledgeGraph | None,
        analysis: AnalysisDocument | None,
    ) -> ImportResult:
        with self.engine.begin() as connection:
            existing = (
                connection.execute(
                    select(db.imports).where(db.imports.c.digest == digest)
                )
                .mappings()
                .first()
            )
            if existing:
                return ImportResult(
                    existing["source_version_id"],
                    existing["graph_run_id"],
                    existing["analysis_run_id"],
                    True,
                )
            source_id = self._write_source(connection, manifest, chunks)
            graph_id = (
                self._create_run(connection, source_id, graph)[0] if graph else None
            )
            analysis_id = (
                self._create_run(connection, source_id, analysis)[0]
                if analysis
                else None
            )
            connection.execute(
                insert(db.imports).values(
                    digest=digest,
                    collection=manifest.collection,
                    source_version_id=source_id,
                    graph_run_id=graph_id,
                    analysis_run_id=analysis_id,
                )
            )
            return ImportResult(source_id, graph_id, analysis_id)


def open_artifacts(config: ReadFellowConfig) -> ArtifactStore:
    """Resolve credentials only when opening storage; never print the URI."""
    from .config import read_secret

    uri = (
        config.database_url.get_secret_value()
        if config.database_url
        else read_secret("MYSQL_URI")
    )
    if not uri:
        raise RuntimeError("set MYSQL_URI in the environment or .env, then run db-init")
    return _artifact_store(uri)


@lru_cache(maxsize=8)
def _artifact_store(uri: str) -> ArtifactStore:
    return ArtifactStore.open(uri)
