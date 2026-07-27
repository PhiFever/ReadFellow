from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


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
    chunker_version: int = 0


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
    byte_start: int = 0
    byte_end: int = 0
    chapter: str = ""
    text_hash: str = ""
    text: str = ""


class EvidenceGraphContext(ReadFellowModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entities: list[str] = Field(default_factory=list)
    relations: list[str] = Field(default_factory=list)


class EvidenceMatch(ReadFellowModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Only the channels that are scored and ranked. The graph annotates the
    # results the other two found; it never contributes a rank of its own.
    mode: Literal["vector", "fts"]
    rank: int


class Evidence(ReadFellowModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str
    source_path: str
    chunk_index: int
    line_start: int
    line_end: int
    byte_start: int
    byte_end: int
    chapter: str = ""
    text_hash: str
    text: str
    retrieval_mode: Literal["vector", "fts", "fetch", "graph", "hybrid"]
    score: float | None = None
    matches: list[EvidenceMatch] = Field(default_factory=list)
    graph_context: EvidenceGraphContext | None = None


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
        return not (
            self.max_chunk_index is not None
            and progress_fields.chunk_index > self.max_chunk_index
        )


class ChunkContext(ReadFellowModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str = ""
    source_path: str = ""
    chunk_index: int = 0
    line_start: int = 0
    line_end: int = 0
    byte_start: int = 0
    byte_end: int = 0
    chapter: str = ""


class DerivationSettings(ReadFellowModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Qwen3's published non-thinking defaults. Its model card names greedy
    # decoding as a cause of "performance degradation and endless repetitions",
    # and qwen3:8b reproduces exactly that on some chunks: it answers a relation
    # as the flat string "subject: X, relation: Y, ..." instead of an object,
    # identically on every retry. Sampling is what stops that, so temperature is
    # a stored setting rather than a constant, and every knob that shapes an
    # answer belongs here — a derived document is stale when any of them moves.
    temperature: float = 0.7
    top_p: float = 0.8
    top_k: int = 20
    min_p: float = 0.0
    presence_penalty: float = 1.5
    repeat_penalty: float = 1.08
    num_predict: int = 0
    num_ctx: int = 0
    retries: int = 0


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
    source_hash: str = ""
    text_hash: str = ""
    entity_count: int = 0
    relation_count: int = 0
    rejected_count: int = 0


class GraphExtraction(ReadFellowModel):
    entities: list[GraphEntity] = Field(default_factory=list)
    relations: list[GraphRelation] = Field(default_factory=list)
    rejected_count: int = 0


class GraphChunkFingerprint(ReadFellowModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_hash: str = ""
    text_hash: str = ""


class KnowledgeGraph(ReadFellowModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int = 1
    prompt_version: str = ""
    collection: str = ""
    source_path: str = ""
    llm_model: str = ""
    extraction_settings: DerivationSettings = DerivationSettings()
    source_chunk_hashes: dict[str, GraphChunkFingerprint] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    progress_limit: str = ""
    selected_chunk_count: int = 0
    processed_chunk_count: int = 0
    entity_count: int = 0
    relation_count: int = 0
    rejected_count: int = 0
    entities: list[GraphEntity] = Field(default_factory=list)
    relations: list[GraphRelation] = Field(default_factory=list)
    extractions: list[GraphExtractionRecord] = Field(default_factory=list)


class GraphQueryResult(ReadFellowModel):
    entities: list[GraphEntity] = Field(default_factory=list)
    relations: list[GraphRelation] = Field(default_factory=list)


class CharacterMention(ChunkContext):
    name: str
    role_in_chapter: str = ""
    evidence: str = ""


class ChapterEvent(ChunkContext):
    order: int = 0
    description: str = ""
    evidence: str = ""


class ChapterAnalysis(ReadFellowModel):
    chapter_index: int
    chapter_title: str
    source_path: str = ""
    line_start: int = 0
    line_end: int = 0
    chunk_ids: list[str] = Field(default_factory=list)
    summary: str = ""
    characters: list[CharacterMention] = Field(default_factory=list)
    events: list[ChapterEvent] = Field(default_factory=list)
    rejected_count: int = 0


class AnalysisDocument(ReadFellowModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int = 1
    prompt_version: str = ""
    collection: str = ""
    source_path: str = ""
    llm_model: str = ""
    settings: DerivationSettings = DerivationSettings()
    chunk_text_hashes: dict[str, str] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    progress_limit: str = ""
    selected_chapter_count: int = 0
    processed_chapter_count: int = 0
    rejected_count: int = 0
    chapters: list[ChapterAnalysis] = Field(default_factory=list)


class OllamaEmbedRequest(ReadFellowModel):
    model: str
    input: list[str]
    keep_alive: str


class OllamaEmbedResponse(ReadFellowModel):
    model_config = ConfigDict(extra="allow")

    embeddings: list[list[float]]


class OllamaGenerateOptions(ReadFellowModel):
    """The wire form of `DerivationSettings`, which is where the values live."""

    temperature: float
    top_p: float
    top_k: int
    min_p: float
    presence_penalty: float
    repeat_penalty: float
    num_predict: int
    num_ctx: int

    @classmethod
    def from_settings(cls, settings: DerivationSettings) -> OllamaGenerateOptions:
        return cls(**settings.model_dump(exclude={"retries"}))


class OllamaGenerateRequest(ReadFellowModel):
    model: str
    prompt: str
    # A JSON schema rather than "json". Both constrain decoding, but "json" only
    # promises the answer parses: a relation answered as a flat string instead of
    # an object is valid JSON, and `_parse_relation` drops it. The schema is what
    # makes the shape a guarantee instead of a request the prompt has to win.
    format: dict[str, Any]
    stream: bool = False
    # Ollama turns thinking on for a model that supports it unless the field is
    # sent, and a thinking qwen3 stops emitting the closing brace of its JSON
    # answer and pads with blank lines until num_predict runs out. Sending false
    # is accepted by models without the capability, so it is safe to hardcode.
    think: bool = False
    keep_alive: str
    options: OllamaGenerateOptions


class OllamaGenerateResponse(ReadFellowModel):
    model_config = ConfigDict(extra="allow")

    response: str
