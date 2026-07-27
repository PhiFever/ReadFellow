from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .derivation import write_json_document
from .extraction import (
    EvidenceNotFound,
    as_list,
    chunk_context,
    collect_items,
    get_any,
    locate_evidence,
    normalize_text,
    parse_json_object,
    resolve_evidence,
)
from .models import (
    AnalysisDocument,
    ChapterAnalysis,
    ChapterEvent,
    CharacterMention,
    Chunk,
    DerivationSettings,
    IndexManifest,
    ProgressFilter,
    utc_now_iso,
)
from .store import metadata_path

ANALYSIS_SCHEMA_VERSION = 2
ANALYSIS_PROMPT_VERSION = "chapter-analysis-v1"

# Rough qwen ratio for Chinese prose, used only to keep a chapter prompt inside
# num_ctx. Ollama truncates an over-long prompt from the left without warning.
CHARS_PER_TOKEN = 1.5
PROMPT_OVERHEAD_TOKENS = 600

_SUMMARY_KEYS = ("summary", "梗概", "摘要")
_CHARACTER_KEYS = ("characters", "人物", "角色")
_EVENT_KEYS = ("events", "事件")
_NAME_KEYS = ("name", "姓名", "名称")
_ROLE_KEYS = ("role_in_chapter", "role", "作用", "角色作用")
_DESCRIPTION_KEYS = ("description", "描述", "内容", "事件")
_ORDER_KEYS = ("order", "序号", "顺序")
_CHUNK_ID_KEYS = ("chunk_id", "chunkid", "chunk", "片段")
_EVIDENCE_KEYS = ("evidence", "证据", "原文", "quote")


@dataclass(frozen=True)
class ChapterGroup:
    index: int
    title: str
    chunks: tuple[Chunk, ...]


def analysis_path(metadata_dir: Path, collection: str) -> Path:
    return metadata_path(metadata_dir, collection) / "analysis.json"


def read_analysis(path: Path) -> AnalysisDocument:
    return AnalysisDocument.model_validate_json(path.read_text(encoding="utf-8"))


def write_analysis(path: Path, document: AnalysisDocument) -> None:
    finalize_analysis(document)
    write_json_document(path, document)


def empty_analysis(
    *,
    collection: str,
    manifest: IndexManifest | None = None,
    llm_model: str = "",
    settings: DerivationSettings | None = None,
) -> AnalysisDocument:
    now = utc_now_iso()
    return AnalysisDocument(
        schema_version=ANALYSIS_SCHEMA_VERSION,
        prompt_version=ANALYSIS_PROMPT_VERSION,
        collection=collection,
        source_path=manifest.source_path if manifest else "",
        llm_model=llm_model,
        settings=settings or DerivationSettings(),
        created_at=now,
        updated_at=now,
    )


def update_analysis_metadata(
    document: AnalysisDocument,
    *,
    collection: str,
    manifest: IndexManifest,
    llm_model: str,
    settings: DerivationSettings,
    progress: ProgressFilter,
    selected_chapter_count: int,
) -> None:
    document.schema_version = ANALYSIS_SCHEMA_VERSION
    document.prompt_version = ANALYSIS_PROMPT_VERSION
    document.collection = collection
    document.source_path = manifest.source_path
    document.llm_model = llm_model
    document.settings = settings
    document.updated_at = utc_now_iso()
    document.progress_limit = progress.description
    document.selected_chapter_count = selected_chapter_count
    document.processed_chapter_count = len(document.chapters)


def group_chapters(chunks: Sequence[Chunk]) -> list[ChapterGroup]:
    groups: list[ChapterGroup] = []
    current: list[Chunk] = []
    current_title = ""
    for chunk in chunks:
        if current and chunk.chapter != current_title:
            groups.append(ChapterGroup(len(groups) + 1, current_title, tuple(current)))
            current = []
        current_title = chunk.chapter
        current.append(chunk)
    if current:
        groups.append(ChapterGroup(len(groups) + 1, current_title, tuple(current)))
    return groups


def complete_chapters(groups: Sequence[ChapterGroup]) -> list[ChapterGroup]:
    """Chapters proven finished, i.e. a later chapter exists in the index.

    The last group may have been cut short by `index --limit`, so it can never be
    told apart from a complete chapter and is never analyzed.
    """
    return list(groups[:-1])


def chapter_char_budget(*, num_ctx: int, num_predict: int) -> int:
    tokens = num_ctx - num_predict - PROMPT_OVERHEAD_TOKENS
    if tokens <= 0:
        return 0
    return int(tokens * CHARS_PER_TOKEN)


def chapter_body(group: ChapterGroup) -> str:
    parts: list[str] = []
    previous = ""
    for chunk in group.chunks:
        parts.append(
            f"[chunk {chunk.id}]\n{strip_overlap(previous, chunk.text).strip()}"
        )
        previous = chunk.text
    return "\n\n".join(parts)


def strip_overlap(previous_text: str, text: str) -> str:
    """Drop the leading overlap a chunk repeats from its predecessor.

    Only the prompt is shortened; evidence is still validated against the stored
    chunk text, so an inexact match here cannot weaken provenance.
    """
    for size in range(min(len(previous_text), len(text)), 0, -1):
        if text.startswith(previous_text[-size:]):
            return text[size:]
    return text


def build_chapter_prompt(group: ChapterGroup) -> str:
    chunk_ids = "、".join(chunk.id for chunk in group.chunks)
    title = group.title or "(无章节)"
    return (
        f"prompt_version: {ANALYSIS_PROMPT_VERSION}\n"
        "你是一个面向中文小说的章节分析器。只根据给定章节正文作答，"
        "不要补充正文之外的信息。不要输出思考过程，不要使用 <think> 标签。\n\n"
        "返回严格 JSON，不要 Markdown，不要解释。JSON schema:\n"
        "{\n"
        '  "summary": "本章梗概",\n'
        '  "characters": [\n'
        '    {"name": "人物名", "role_in_chapter": "该人物在本章中的作用",\n'
        '     "chunk_id": "证据所在片段 id", "evidence": "片段中的原文短句"}\n'
        "  ],\n"
        '  "events": [\n'
        '    {"order": 1, "description": "事件描述",\n'
        '     "chunk_id": "证据所在片段 id", "evidence": "片段中的原文短句"}\n'
        "  ]\n"
        "}\n"
        "summary 用 3-5 句话概括本章，只写本章发生的事。\n"
        "最多列出 8 个人物和 10 个事件，events 按发生先后编号。\n"
        "每个人物和事件都必须给出 chunk_id 与 evidence。"
        "evidence 必须逐字复制自对应片段，不超过 60 个汉字，"
        "不要改写、不要拼接、不要加省略号。\n"
        f"chunk_id 只能取自：{chunk_ids}\n\n"
        f"chapter: {title}\n"
        f"source_path: {group.chunks[0].source_path}\n"
        f"lines: {group.chunks[0].line_start}-{group.chunks[-1].line_end}\n\n"
        "章节正文（按片段给出）：\n"
        f"{chapter_body(group)}"
    )


def parse_chapter_analysis(
    raw: str | Mapping[str, Any],
    group: ChapterGroup,
) -> ChapterAnalysis:
    payload = parse_json_object(raw)
    summary = normalize_text(get_any(payload, _SUMMARY_KEYS, ""))
    if not summary:
        raise ValueError(f"chapter analysis has no summary for {group.title}")

    characters, rejected_characters = collect_items(
        as_list(get_any(payload, _CHARACTER_KEYS, [])),
        lambda _, item: _parse_character(item, group),
    )
    events, rejected_events = collect_items(
        as_list(get_any(payload, _EVENT_KEYS, [])),
        lambda position, item: _parse_event(item, group, position=position),
    )

    return ChapterAnalysis(
        chapter_index=group.index,
        chapter_title=group.title,
        source_path=group.chunks[0].source_path,
        line_start=group.chunks[0].line_start,
        line_end=group.chunks[-1].line_end,
        chunk_ids=[chunk.id for chunk in group.chunks],
        summary=summary,
        characters=characters,
        events=events,
        rejected_count=rejected_characters + rejected_events,
    )


def merge_chapter(
    document: AnalysisDocument,
    analysis: ChapterAnalysis,
    group: ChapterGroup,
) -> None:
    document.chapters = [
        chapter
        for chapter in document.chapters
        if chapter.chapter_index != analysis.chapter_index
    ]
    document.chapters.append(analysis)
    for chunk in group.chunks:
        document.chunk_text_hashes[chunk.id] = chunk.text_hash
    finalize_analysis(document)


def finalize_analysis(document: AnalysisDocument) -> None:
    document.chapters = sorted(
        document.chapters, key=lambda chapter: chapter.chapter_index
    )
    for chapter in document.chapters:
        chapter.events = sorted(
            chapter.events, key=lambda event: (event.order, event.chunk_index)
        )
    referenced = {
        chunk_id for chapter in document.chapters for chunk_id in chapter.chunk_ids
    }
    document.chunk_text_hashes = {
        chunk_id: text_hash
        for chunk_id, text_hash in document.chunk_text_hashes.items()
        if chunk_id in referenced
    }
    document.processed_chapter_count = len(document.chapters)
    document.rejected_count = sum(
        chapter.rejected_count for chapter in document.chapters
    )


def processed_chapter_keys(document: AnalysisDocument) -> set[tuple[int, str]]:
    return {
        (chapter.chapter_index, chapter.chapter_title) for chapter in document.chapters
    }


def analysis_staleness_reason(
    document: AnalysisDocument,
    groups: Sequence[ChapterGroup],
    *,
    collection: str,
    source_path: str,
    llm_model: str | None = None,
    settings: DerivationSettings | None = None,
) -> str | None:
    if document.schema_version != ANALYSIS_SCHEMA_VERSION:
        return "analysis schema version changed"
    if document.prompt_version != ANALYSIS_PROMPT_VERSION:
        return "analysis prompt changed"
    if document.collection != collection or document.source_path != source_path:
        return "analysis source metadata changed"
    if llm_model is not None and document.llm_model != llm_model:
        return "analysis generator model changed"
    if settings is not None and document.settings != settings:
        return "analysis settings changed"

    indexes = [chapter.chapter_index for chapter in document.chapters]
    if len(indexes) != len(set(indexes)):
        return "analysis chapter metadata is inconsistent"

    groups_by_index = {group.index: group for group in groups}
    referenced: set[str] = set()
    for chapter in document.chapters:
        group = groups_by_index.get(chapter.chapter_index)
        if group is None or group.title != chapter.chapter_title:
            return "analysis chapters no longer match the indexed chunks"
        if [chunk.id for chunk in group.chunks] != chapter.chunk_ids:
            return "analysis chapter chunks changed"
        for chunk in group.chunks:
            if document.chunk_text_hashes.get(chunk.id) != chunk.text_hash:
                return "analysis source chunk hashes changed"
            referenced.add(chunk.id)
    if set(document.chunk_text_hashes) != referenced:
        return "analysis source chunk hashes are incomplete"
    return None


def filter_chapter(
    chapter: ChapterAnalysis,
    chunks_by_id: Mapping[str, Chunk],
    progress: ProgressFilter | None,
) -> ChapterAnalysis | None:
    filtered = chapter.model_copy(deep=True)
    if progress is None or (
        progress.max_line_end is None and progress.max_chunk_index is None
    ):
        return filtered

    visible = [
        chunks_by_id[chunk_id]
        for chunk_id in chapter.chunk_ids
        if chunk_id in chunks_by_id and progress.allows(chunks_by_id[chunk_id])
    ]
    if not visible:
        return None

    filtered.characters = [
        mention for mention in filtered.characters if progress.allows(mention)
    ]
    filtered.events = [event for event in filtered.events if progress.allows(event)]
    if len(visible) != len(chapter.chunk_ids):
        # The summary is generated across the whole chapter and has no
        # per-sentence provenance, so it cannot be shown once part of the
        # chapter is beyond the reading progress.
        filtered.summary = ""
        filtered.line_end = max(chunk.line_end for chunk in visible)
    return filtered


def _parse_character(item: Any, group: ChapterGroup) -> CharacterMention | None:
    if not isinstance(item, Mapping):
        return None
    name = normalize_text(get_any(item, _NAME_KEYS, ""))
    if not name:
        return None

    evidence = normalize_text(get_any(item, _EVIDENCE_KEYS, ""))
    chunk, evidence = _resolve_evidence(evidence, item, group, label="character")
    return CharacterMention(
        name=name,
        role_in_chapter=normalize_text(get_any(item, _ROLE_KEYS, "")),
        evidence=evidence,
        **chunk_context(chunk).model_dump(mode="json"),
    )


def _parse_event(
    item: Any, group: ChapterGroup, *, position: int
) -> ChapterEvent | None:
    if not isinstance(item, Mapping):
        return None
    description = normalize_text(get_any(item, _DESCRIPTION_KEYS, ""))
    if not description:
        return None

    order = get_any(item, _ORDER_KEYS, None)
    evidence = normalize_text(get_any(item, _EVIDENCE_KEYS, ""))
    chunk, evidence = _resolve_evidence(evidence, item, group, label="event")
    return ChapterEvent(
        order=order if isinstance(order, int) else position,
        description=description,
        evidence=evidence,
        **chunk_context(chunk).model_dump(mode="json"),
    )


def _resolve_evidence(
    evidence: str,
    item: Mapping[str, Any],
    group: ChapterGroup,
    *,
    label: str,
) -> tuple[Chunk, str]:
    """The chapter chunk a quote comes from, plus that chunk's own wording for it.

    A chapter spans several chunks, so which one an item belongs to is derived
    from where its quote matches. An item without a usable quote therefore has
    no chunk to attribute and cannot be built at all.
    """
    if not evidence:
        raise EvidenceNotFound(f"{label} in chapter {group.title} has no evidence")

    claimed = normalize_text(get_any(item, _CHUNK_ID_KEYS, ""))
    chunk = next(
        (candidate for candidate in group.chunks if candidate.id == claimed), None
    )
    if chunk is None or locate_evidence(evidence, chunk.text) is None:
        chunk = next(
            (
                candidate
                for candidate in group.chunks
                if locate_evidence(evidence, candidate.text) is not None
            ),
            chunk,
        )
    if chunk is None:
        raise EvidenceNotFound(
            f"{label} evidence matches no chunk in chapter {group.title}"
        )
    return chunk, resolve_evidence(evidence, chunk.text, label=label, chunk_id=chunk.id)
