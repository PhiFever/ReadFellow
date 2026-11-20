from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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


class IndexManifest(ReadFellowModel):
    source_path: str
    source_hash: str
    model: str
    embedding_dimension: int
    chunk_count: int
    chunk_chars: int
    overlap_chars: int
    chunker_version: int = 0


class EvidenceMatch(ReadFellowModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

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
    retrieval_mode: Literal["vector", "fts", "fetch", "hybrid"]
    score: float | None = None
    matches: list[EvidenceMatch] = Field(default_factory=list)


class ChapterBoundary(ReadFellowModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ordinal: int
    line_end: int
    content_hash: str
    text: str | None = None
    title: str
    line_start: int
    volume: int
    number: int | None


class ReaderState(ReadFellowModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["all", "chapter", "line"]
    value: str | None
    line_limit: int | None
    source_hash: str
    set_at: str


class TocEntry(ReadFellowModel):
    ordinal: int
    reference: str
    title: str
    line_start: int
    line_end: int
    characters: int
    partial: bool


class TocResult(ReadFellowModel):
    progress: str
    chapters: list[TocEntry]


class ReadLine(ReadFellowModel):
    number: int
    text: str

    def numbered(self) -> str:
        return f"{self.number:6}\t{self.text}"


class ReadResult(ReadFellowModel):
    progress: str
    lines: list[ReadLine]
    clipped_at: int | None
    truncated: bool


class ChapterHits(ReadFellowModel):
    ordinal: int
    title: str
    count: int


class LineHit(ReadFellowModel):
    number: int
    ordinal: int
    title: str
    snippet: str


class FindResult(ReadFellowModel):
    progress: str
    total_lines: int
    total_chapters: int
    chapters: list[ChapterHits]
    hits: list[LineHit]


class ProgressFields(ReadFellowModel):
    model_config = ConfigDict(extra="allow", from_attributes=True)

    line_end: int
    chunk_index: int


class ProgressFilter(ReadFellowModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

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
    # `num_ctx` and `retries` are stored too, for reference only: they decide
    # which units a build attempts and how often, not what an answer says, so
    # `derivation.settings_changed` leaves them out.
    temperature: float = 0.7
    top_p: float = 0.8
    top_k: int = 20
    min_p: float = 0.0
    presence_penalty: float = 1.5
    repeat_penalty: float = 1.08
    num_predict: int = 0
    num_ctx: int = 0
    retries: int = 0


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
    unanchored_count: int | None = None


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
    # promises the answer parses: a chapter event answered as a flat string
    # instead of an object is valid JSON, and the parser drops it. The schema
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
