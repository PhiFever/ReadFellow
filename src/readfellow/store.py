from __future__ import annotations

import json
from pathlib import Path
import shutil

import zvec
from zvec import CollectionSchema, DataType, Doc, FieldSchema, FtsIndexParam, Query
from zvec import VectorSchema
from zvec.model.param.query import Fts

from .models import Chunk, IndexManifest, ZvecChunkFields


EMBEDDING_FIELD = "embedding"
TEXT_FIELD = "text"


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
                index_param=FtsIndexParam(tokenizer_name="jieba", filters=["lowercase"]),
            ),
        ],
        vectors=VectorSchema(EMBEDDING_FIELD, DataType.VECTOR_FP32, dimension),
    )


def open_or_create_collection(
    *,
    index_dir: Path,
    metadata_dir: Path,
    collection: str,
    dimension: int,
    rebuild: bool,
):
    path = collection_path(index_dir, collection)
    meta = metadata_path(metadata_dir, collection)
    if rebuild:
        if path.exists():
            shutil.rmtree(path)
        if meta.exists():
            shutil.rmtree(meta)

    index_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir.mkdir(parents=True, exist_ok=True)

    if path.exists():
        coll = zvec.open(str(path))
        existing_dim = coll.schema.vector(EMBEDDING_FIELD).dimension
        if existing_dim != dimension:
            raise ValueError(
                f"collection dimension is {existing_dim}, but embedding dimension is {dimension}; "
                "use --rebuild to recreate it"
            )
        return coll

    return zvec.create_and_open(str(path), create_schema(collection, dimension))


def chunk_to_doc(chunk: Chunk, vector: list[float], *, model: str) -> Doc:
    fields = ZvecChunkFields.from_chunk(chunk, model=model)
    return Doc(
        id=chunk.id,
        fields=fields.model_dump(mode="json"),
        vectors={EMBEDDING_FIELD: vector},
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
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with (meta / "chunks.jsonl").open("w", encoding="utf-8") as file:
        for chunk in chunks:
            file.write(json.dumps(chunk.model_dump(mode="json"), ensure_ascii=False) + "\n")


def read_manifest(*, metadata_dir: Path, collection: str) -> IndexManifest:
    path = metadata_path(metadata_dir, collection) / "manifest.json"
    return IndexManifest.model_validate_json(path.read_text(encoding="utf-8"))


def query_vector(coll, vector: list[float], *, top_k: int, filter: str | None = None):
    return coll.query(
        Query(field_name=EMBEDDING_FIELD, vector=vector),
        topk=top_k,
        filter=filter,
        output_fields=[
            "source_path",
            "chunk_index",
            "line_start",
            "line_end",
            "byte_start",
            "byte_end",
            "chapter",
            "text_hash",
            TEXT_FIELD,
        ],
    )


def query_fts(coll, query: str, *, top_k: int, filter: str | None = None):
    return coll.query(
        Query(field_name=TEXT_FIELD, fts=Fts(match_string=query)),
        topk=top_k,
        filter=filter,
        output_fields=[
            "source_path",
            "chunk_index",
            "line_start",
            "line_end",
            "byte_start",
            "byte_end",
            "chapter",
            "text_hash",
            TEXT_FIELD,
        ],
    )


def fetch_chunk(coll, chunk_id: str):
    docs = coll.fetch(chunk_id, output_fields=None, include_vector=False)
    return docs.get(chunk_id)
