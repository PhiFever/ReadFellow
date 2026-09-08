"""Relational artifact schema. Domain validation remains in the Pydantic models."""

from sqlalchemy import (
    JSON,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import LONGTEXT

metadata = MetaData()
long_text = Text().with_variant(LONGTEXT(), "mysql")


def table(name, *columns):
    return Table(
        name,
        metadata,
        *columns,
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_bin",
    )


def key(name="id"):
    return Column(name, Integer, primary_key=True, autoincrement=True)


def text(name):
    return Column(name, long_text, nullable=False)


def number(name, *, nullable=False):
    return Column(name, Integer, nullable=nullable)


def context():
    return [
        Column("chunk_id", String(128), nullable=False),
        text("source_path"),
        number("chunk_index"),
        number("line_start"),
        number("line_end"),
        number("byte_start"),
        number("byte_end"),
        text("chapter"),
    ]


def counts():
    return [number("rejected_count"), number("unanchored_count", nullable=True)]


sources = table(
    "source_versions",
    key(),
    Column("collection", String(128), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    text("collection_path"),
    text("source_path"),
    text("model"),
    number("embedding_dimension"),
    number("chunk_count"),
    number("chunk_chars"),
    number("overlap_chars"),
    number("chunker_version"),
    Column("extra", JSON, nullable=False),
    Index("ix_source_latest", "collection", "id"),
)
chunks = table(
    "chunks",
    number("source_version_id"),
    Column("id", String(128), nullable=False),
    text("source_path"),
    Column("source_hash", String(64), nullable=False),
    number("chunk_index"),
    text("text"),
    Column("text_hash", String(64), nullable=False),
    number("line_start"),
    number("line_end"),
    number("byte_start"),
    number("byte_end"),
    text("chapter"),
    UniqueConstraint("source_version_id", "id"),
    ForeignKeyConstraint(["source_version_id"], ["source_versions.id"]),
)
runs = table(
    "runs",
    key(),
    Column("collection", String(128), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("source_version_id", ForeignKey("source_versions.id"), nullable=False),
    number("schema_version"),
    text("prompt_version"),
    text("source_path"),
    text("llm_model"),
    Column("settings", JSON, nullable=False),
    text("created_at"),
    text("updated_at"),
    text("progress_limit"),
    number("selected_count"),
    number("processed_count"),
    *counts(),
    Column("extra", JSON, nullable=False),
    Index("ix_run_latest", "collection", "kind", "id"),
)


def run_columns():
    return [
        Column("run_id", ForeignKey("runs.id"), nullable=False),
        Column("position", String(64), nullable=False),
        number("sort_order"),
    ]


def chunk_reference():
    return [
        number("source_version_id"),
        ForeignKeyConstraint(
            ["source_version_id", "chunk_id"], ["chunks.source_version_id", "chunks.id"]
        ),
    ]


entities = table(
    "entities", *run_columns(), text("name"), UniqueConstraint("run_id", "position")
)


def entity_reference():
    return [
        Column("entity_position", String(64), nullable=False),
        ForeignKeyConstraint(
            ["run_id", "entity_position"], ["entities.run_id", "entities.position"]
        ),
    ]


entity_values = table(
    "entity_values",
    *run_columns(),
    *entity_reference(),
    Column("kind", String(16), nullable=False),
    text("value"),
)
entity_mentions = table(
    "entity_mentions",
    *run_columns(),
    *entity_reference(),
    *context(),
    *chunk_reference(),
)
entity_evidence = table(
    "entity_evidence",
    *run_columns(),
    *entity_reference(),
    *context(),
    *chunk_reference(),
    text("text"),
)
relations = table(
    "relations",
    *run_columns(),
    *context(),
    *chunk_reference(),
    text("subject"),
    text("relation"),
    text("object"),
    text("evidence"),
    text("subject_entity"),
    text("object_entity"),
    Column("subject_position", String(64)),
    Column("object_position", String(64)),
    ForeignKeyConstraint(
        ["run_id", "subject_position"], ["entities.run_id", "entities.position"]
    ),
    ForeignKeyConstraint(
        ["run_id", "object_position"], ["entities.run_id", "entities.position"]
    ),
    Index("ix_relation_subject", "run_id", "subject_position"),
    Index("ix_relation_object", "run_id", "object_position"),
)
extractions = table(
    "extractions",
    *run_columns(),
    *context(),
    *chunk_reference(),
    text("source_hash"),
    text("text_hash"),
    number("entity_count"),
    number("relation_count"),
    *counts(),
    UniqueConstraint("run_id", "chunk_id"),
)
fingerprints = table(
    "run_chunks",
    *run_columns(),
    Column("chunk_id", String(128), nullable=False),
    *chunk_reference(),
    text("source_hash"),
    text("text_hash"),
    UniqueConstraint("run_id", "chunk_id"),
)
chapters = table(
    "chapters",
    *run_columns(),
    number("chapter_index"),
    text("chapter_title"),
    text("source_path"),
    number("line_start"),
    number("line_end"),
    text("summary"),
    *counts(),
    UniqueConstraint("run_id", "position"),
)


def chapter_reference():
    return [
        Column("chapter_position", String(64), nullable=False),
        ForeignKeyConstraint(
            ["run_id", "chapter_position"], ["chapters.run_id", "chapters.position"]
        ),
    ]


chapter_chunks = table(
    "chapter_chunks",
    *run_columns(),
    *chapter_reference(),
    Column("chunk_id", String(128), nullable=False),
    *chunk_reference(),
)
characters = table(
    "characters",
    *run_columns(),
    *chapter_reference(),
    *context(),
    *chunk_reference(),
    text("name"),
    text("role_in_chapter"),
    text("evidence"),
)
events = table(
    "events",
    *run_columns(),
    *chapter_reference(),
    *context(),
    *chunk_reference(),
    number("order"),
    text("description"),
    text("evidence"),
)
imports = table(
    "artifact_imports",
    Column("digest", String(64), primary_key=True),
    Column("collection", String(128), nullable=False),
    Column("source_version_id", ForeignKey("source_versions.id"), nullable=False),
    Column("graph_run_id", ForeignKey("runs.id")),
    Column("analysis_run_id", ForeignKey("runs.id")),
)

# Reverse dependency order is used when publishing an updated run in a transaction.
run_tables = (
    entities,
    entity_values,
    entity_mentions,
    entity_evidence,
    relations,
    extractions,
    fingerprints,
    chapters,
    chapter_chunks,
    characters,
    events,
)
