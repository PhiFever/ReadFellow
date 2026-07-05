from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ReadFellowModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextUnit(ReadFellowModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    line_start: int
    line_end: int
    byte_start: int
    byte_end: int
    chapter: str


class Chunk(ReadFellowModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    source_path: str
    source_hash: str
    chunk_index: int
    text: str
    text_hash: str
    line_start: int
    line_end: int
    byte_start: int
    byte_end: int
    chapter: str

    @property
    def char_count(self) -> int:
        return len(self.text)


class IndexManifest(ReadFellowModel):
    model_config = ConfigDict(extra="allow")

    collection: str
    collection_path: str
    source_path: str
    model: str
    embedding_dimension: int
    chunk_count: int
    chunk_chars: int
    overlap_chars: int


class ZvecChunkFields(ReadFellowModel):
    source_path: str
    source_hash: str
    chunk_index: int
    line_start: int
    line_end: int
    byte_start: int
    byte_end: int
    chapter: str
    text_hash: str
    char_count: int
    model: str
    text: str

    @classmethod
    def from_chunk(cls, chunk: Chunk, *, model: str) -> ZvecChunkFields:
        return cls(
            source_path=chunk.source_path,
            source_hash=chunk.source_hash,
            chunk_index=chunk.chunk_index,
            line_start=chunk.line_start,
            line_end=chunk.line_end,
            byte_start=chunk.byte_start,
            byte_end=chunk.byte_end,
            chapter=chunk.chapter,
            text_hash=chunk.text_hash,
            char_count=chunk.char_count,
            model=model,
            text=chunk.text,
        )


class QueryChunkFields(ReadFellowModel):
    model_config = ConfigDict(extra="allow")

    source_path: str = ""
    chunk_index: int = 0
    line_start: int = 0
    line_end: int = 0
    chapter: str = ""
    text_hash: str = ""
    text: str = ""


class ChapterBoundary(ReadFellowModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int
    title: str
    line_start: int


class ProgressFields(ReadFellowModel):
    model_config = ConfigDict(extra="allow", from_attributes=True)

    line_end: int
    chunk_index: int


class ProgressFilter(ReadFellowModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expression: str | None
    description: str
    max_line_end: int | None = None
    max_chunk_index: int | None = None

    def allows(self, fields: ProgressFields | BaseModel | Mapping[str, object]) -> bool:
        progress_fields = ProgressFields.model_validate(fields)
        if (
            self.max_line_end is not None
            and progress_fields.line_end > self.max_line_end
        ):
            return False
        if (
            self.max_chunk_index is not None
            and progress_fields.chunk_index > self.max_chunk_index
        ):
            return False
        return True


class ChunkContext(ReadFellowModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str = ""
    source_path: str = ""
    chunk_index: int = 0
    line_start: int = 0
    line_end: int = 0
    chapter: str = ""


class GraphEvidence(ChunkContext):
    text: str = ""


class GraphEntity(ReadFellowModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    types: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    mentions: list[ChunkContext] = Field(default_factory=list)
    evidence: list[GraphEvidence] = Field(default_factory=list)


class GraphRelation(ChunkContext):
    subject: str
    relation: str
    object: str
    evidence: str = ""
    subject_entity: str = ""
    object_entity: str = ""


class GraphExtractionRecord(ChunkContext):
    entity_count: int = 0
    relation_count: int = 0


class GraphExtraction(ReadFellowModel):
    entities: list[GraphEntity] = Field(default_factory=list)
    relations: list[GraphRelation] = Field(default_factory=list)


class KnowledgeGraph(ReadFellowModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int = 1
    collection: str = ""
    source_path: str = ""
    llm_model: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    progress_limit: str = ""
    selected_chunk_count: int = 0
    processed_chunk_count: int = 0
    entity_count: int = 0
    relation_count: int = 0
    entities: list[GraphEntity] = Field(default_factory=list)
    relations: list[GraphRelation] = Field(default_factory=list)
    extractions: list[GraphExtractionRecord] = Field(default_factory=list)


class GraphQueryResult(ReadFellowModel):
    entities: list[GraphEntity] = Field(default_factory=list)
    relations: list[GraphRelation] = Field(default_factory=list)


class OllamaEmbedRequest(ReadFellowModel):
    model: str
    input: list[str]
    keep_alive: str


class OllamaEmbedResponse(ReadFellowModel):
    embeddings: list[list[float]]


class OllamaGenerateOptions(ReadFellowModel):
    temperature: float = 0.0
    num_predict: int = 2048
    repeat_penalty: float = 1.08


class OllamaGenerateRequest(ReadFellowModel):
    model: str
    prompt: str
    format: Literal["json"] = "json"
    stream: bool = False
    keep_alive: str
    options: OllamaGenerateOptions


class OllamaGenerateResponse(ReadFellowModel):
    model_config = ConfigDict(extra="allow")

    response: str
