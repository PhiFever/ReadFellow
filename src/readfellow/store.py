from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import zvec
from zvec import (
    CollectionSchema,
    DataType,
    Doc,
    FieldSchema,
    FtsIndexParam,
    Query,
    VectorSchema,
)
from zvec.model.param.query import Fts

from .models import (
    Chunk,
    Evidence,
    IndexManifest,
    ProgressFilter,
    QueryChunkFields,
    ZvecChunkFields,
)

EMBEDDING_FIELD = "embedding"
TEXT_FIELD = "text"

STORED_OUTPUT_FIELDS = [
    "source_path",
    "chunk_index",
    "line_start",
    "line_end",
    "byte_start",
    "byte_end",
    "chapter",
    "text_hash",
    TEXT_FIELD,
]


def collection_path(index_dir: Path, collection: str) -> Path:
    return index_dir / collection


def metadata_path(metadata_dir: Path, collection: str) -> Path:
    return metadata_dir / collection


def create_schema(collection: str, dimension: int) -> CollectionSchema:
    return CollectionSchema(
        name=collection,
        fields=[
            FieldSchema("source_path", DataType.STRING),
            FieldSchema("source_hash", DataType.STRING),
            FieldSchema("chunk_index", DataType.UINT64),
            FieldSchema("line_start", DataType.UINT64),
            FieldSchema("line_end", DataType.UINT64),
            FieldSchema("byte_start", DataType.UINT64),
            FieldSchema("byte_end", DataType.UINT64),
            FieldSchema("chapter", DataType.STRING),
            FieldSchema("text_hash", DataType.STRING),
            FieldSchema("char_count", DataType.UINT32),
            FieldSchema("model", DataType.STRING),
            FieldSchema(
                TEXT_FIELD,
                DataType.STRING,
                index_param=FtsIndexParam(
                    tokenizer_name="jieba", filters=["lowercase"]
                ),
            ),
        ],
        vectors=VectorSchema(EMBEDDING_FIELD, DataType.VECTOR_FP32, dimension),
    )


@dataclass(frozen=True)
class UpsertOutcome:
    written: int
    skipped: int


@dataclass(frozen=True)
class StoreStats:
    """What the storage engine itself says it holds.

    `doc_count` is the only way to catch an index that was published as complete
    metadata over an incomplete collection, and `index_completeness` maps each
    indexed field to the fraction of documents that field has actually indexed.
    """

    doc_count: int
    index_completeness: dict[str, float]


class ChunkStore(Protocol):
    """Where indexed chunks live, and the only thing that knows the storage engine.

    Retrieval crosses this seam as `Evidence`, never as a storage record: whatever
    a store returns already carries the provenance an answer must cite.
    """

    def upsert(
        self,
        chunks: Sequence[Chunk],
        *,
        model: str,
        embed: Callable[[list[str]], list[list[float]]],
    ) -> UpsertOutcome:
        """Store the chunks whose text is new or changed, and only those.

        `embed` is called once, with the texts actually being written, so that
        re-indexing an unchanged document costs no embeddings at all.
        """

    def commit(self, *, optimize: bool) -> None:
        """Make everything written so far survive reopening."""

    def search_vector(
        self,
        vector: list[float],
        *,
        top_k: int,
        progress: ProgressFilter,
    ) -> list[Evidence]: ...

    def search_fts(
        self,
        query: str,
        *,
        top_k: int,
        progress: ProgressFilter,
    ) -> list[Evidence]: ...

    def fetch(self, chunk_id: str) -> Evidence | None:
        """The stored chunk, *without* applying any progress limit.

        Fetch is the one entry that must tell "no such chunk" apart from "not read
        yet", so the caller applies the limit and decides which answer to give.
        """

    def stats(self) -> StoreStats:
        """What the store holds, for checking it against the metadata."""


class ZvecChunkStore:
    """A `ChunkStore` backed by a local zvec collection."""

    def __init__(self, collection) -> None:
        self._collection = collection

    @classmethod
    def open_for_read(cls, *, index_dir: Path, collection: str) -> ZvecChunkStore:
        path = collection_path(index_dir, collection)
        if not path.exists():
            raise FileNotFoundError(
                f"collection does not exist: {path}; run the index command first"
            )
        return cls(
            zvec.open(
                str(path), zvec.CollectionOption(read_only=True, enable_mmap=True)
            )
        )

    @classmethod
    def open_for_write(
        cls,
        *,
        index_dir: Path,
        metadata_dir: Path,
        collection: str,
        dimension: int,
        rebuild: bool,
    ) -> ZvecChunkStore:
        path = collection_path(index_dir, collection)
        meta = metadata_path(metadata_dir, collection)
        if rebuild:
            if path.exists():
                shutil.rmtree(path)
            if meta.exists():
                shutil.rmtree(meta)

        index_dir.mkdir(parents=True, exist_ok=True)
        metadata_dir.mkdir(parents=True, exist_ok=True)

        if not path.exists():
            return cls(
                zvec.create_and_open(str(path), create_schema(collection, dimension))
            )

        coll = zvec.open(str(path))
        vector_schema = coll.schema.vector(EMBEDDING_FIELD)
        if vector_schema is None:
            raise ValueError(
                f"collection is missing required vector field {EMBEDDING_FIELD!r}; "
                "use --rebuild to recreate it"
            )
        existing_dim = vector_schema.dimension
        if existing_dim != dimension:
            raise ValueError(
                f"collection dimension is {existing_dim}, but embedding dimension is {dimension}; "
                "use --rebuild to recreate it"
            )
        return cls(coll)

    def upsert(
        self,
        chunks: Sequence[Chunk],
        *,
        model: str,
        embed: Callable[[list[str]], list[list[float]]],
    ) -> UpsertOutcome:
        stored_hashes = self._stored_text_hashes(chunks)
        missing = [chunk for chunk in chunks if chunk.id not in stored_hashes]
        changed = [
            chunk
            for chunk in chunks
            if chunk.id in stored_hashes and stored_hashes[chunk.id] != chunk.text_hash
        ]
        pending = missing + changed
        if not pending:
            return UpsertOutcome(written=0, skipped=len(chunks))

        vectors = embed([chunk.text for chunk in pending])
        docs = [
            _chunk_to_doc(chunk, vector, model=model)
            for chunk, vector in zip(pending, vectors, strict=True)
        ]
        statuses = []
        if missing:
            statuses.extend(self._collection.insert(docs[: len(missing)]))
        if changed:
            statuses.extend(self._collection.update(docs[len(missing) :]))
        failed = [str(status) for status in statuses if not status.ok()]
        if failed:
            raise RuntimeError(f"zvec write failed: {failed[:3]}")
        return UpsertOutcome(written=len(pending), skipped=len(chunks) - len(pending))

    def commit(self, *, optimize: bool) -> None:
        if optimize:
            self._collection.optimize()
        self._collection.flush()

    def search_vector(
        self,
        vector: list[float],
        *,
        top_k: int,
        progress: ProgressFilter,
    ) -> list[Evidence]:
        return self._search(
            Query(field_name=EMBEDDING_FIELD, vector=vector),
            top_k=top_k,
            progress=progress,
            retrieval_mode="vector",
        )

    def search_fts(
        self,
        query: str,
        *,
        top_k: int,
        progress: ProgressFilter,
    ) -> list[Evidence]:
        return self._search(
            Query(field_name=TEXT_FIELD, fts=Fts(match_string=query)),
            top_k=top_k,
            progress=progress,
            retrieval_mode="fts",
        )

    def fetch(self, chunk_id: str) -> Evidence | None:
        docs = self._collection.fetch(
            chunk_id, output_fields=None, include_vector=False
        )
        doc = docs.get(chunk_id)
        return None if doc is None else _evidence_from_doc(doc, retrieval_mode="fetch")

    def stats(self) -> StoreStats:
        stats = self._collection.stats
        return StoreStats(
            doc_count=stats.doc_count,
            index_completeness=dict(stats.index_completeness),
        )

    def _stored_text_hashes(self, chunks: Sequence[Chunk]) -> dict[str, str]:
        stored = self._collection.fetch(
            [chunk.id for chunk in chunks],
            output_fields=["text_hash"],
            include_vector=False,
        )
        return {
            chunk.id: QueryChunkFields.model_validate(stored[chunk.id].fields).text_hash
            for chunk in chunks
            if stored.get(chunk.id) is not None
        }

    def _search(
        self,
        query: Query,
        *,
        top_k: int,
        progress: ProgressFilter,
        retrieval_mode: Literal["vector", "fts"],
    ) -> list[Evidence]:
        docs = self._collection.query(
            query,
            topk=top_k,
            filter=progress.expression,
            output_fields=STORED_OUTPUT_FIELDS,
        )
        return [_evidence_from_doc(doc, retrieval_mode=retrieval_mode) for doc in docs]


def _chunk_to_doc(chunk: Chunk, vector: list[float], *, model: str) -> Doc:
    fields = ZvecChunkFields.from_chunk(chunk, model=model)
    return Doc(
        id=chunk.id,
        fields=fields.model_dump(mode="json"),
        vectors={EMBEDDING_FIELD: vector},
    )


def _evidence_from_doc(
    doc: Doc,
    *,
    retrieval_mode: Literal["vector", "fts", "fetch"],
) -> Evidence:
    fields = QueryChunkFields.model_validate(doc.fields)
    return Evidence(
        chunk_id=doc.id,
        source_path=fields.source_path,
        chunk_index=fields.chunk_index,
        line_start=fields.line_start,
        line_end=fields.line_end,
        byte_start=fields.byte_start,
        byte_end=fields.byte_end,
        chapter=fields.chapter,
        text_hash=fields.text_hash,
        text=fields.text,
        retrieval_mode=retrieval_mode,
        score=doc.score,
    )


def write_manifest(
    *,
    metadata_dir: Path,
    collection: str,
    manifest: IndexManifest,
    chunks: list[Chunk],
) -> None:
    meta = metadata_path(metadata_dir, collection)
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    with (meta / "chunks.jsonl").open("w", encoding="utf-8") as file:
        for chunk in chunks:
            file.write(
                json.dumps(chunk.model_dump(mode="json"), ensure_ascii=False) + "\n"
            )


def read_chunks(*, metadata_dir: Path, collection: str) -> list[Chunk]:
    path = metadata_path(metadata_dir, collection) / "chunks.jsonl"
    if not path.is_file():
        raise FileNotFoundError(
            f"chunk metadata not found: {path}; run the index command first"
        )

    chunks: list[Chunk] = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                chunks.append(Chunk.model_validate_json(stripped))
            except ValueError as exc:
                raise ValueError(
                    f"invalid chunk metadata in {path}:{line_number}: {exc}"
                ) from exc
    return chunks


def read_manifest(*, metadata_dir: Path, collection: str) -> IndexManifest:
    path = metadata_path(metadata_dir, collection) / "manifest.json"
    return IndexManifest.model_validate_json(path.read_text(encoding="utf-8"))
