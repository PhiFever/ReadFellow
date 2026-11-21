from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, get_args

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
    ChapterAnalysis,
    ChapterBoundary,
    ChapterEvent,
    ChapterMoments,
    ChapterSummary,
    CharacterMention,
    Chunk,
    GeneratedMoments,
    Identity,
    IdentityFields,
    Moment,
    MomentKind,
)

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


ANALYSIS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role_in_chapter": {"type": "string"},
                    "chunk_id": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["name", "role_in_chapter", "chunk_id", "evidence"],
            },
        },
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "order": {"type": "integer"},
                    "description": {"type": "string"},
                    "chunk_id": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["order", "description", "chunk_id", "evidence"],
            },
        },
    },
    "required": ["summary", "characters", "events"],
}


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
    rejected = rejected_characters + rejected_events

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
        rejected_count=rejected.total,
        unanchored_count=rejected.unanchored,
    )


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


SUMMARY_PROMPT_VERSION = "chapter-summary-v1"
SUMMARY_SCHEMA_VERSION = 1
KINDS = list(get_args(MomentKind))
SUMMARY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "characters", "tags"],
    "properties": {
        "summary": {"type": "string"},
        "characters": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string", "enum": KINDS}},
    },
}
SUMMARY_PROMPT = """你在为一部长篇小说建立逐章索引。下面是其中一章的全文。

只依据这一章的原文，输出一个 JSON 对象：
- summary：一句话，60 字以内，只写本章发生的事。不写前情，不推测后续，不评价。
- characters：本章的主要人物，照原文里的称呼写，最多 6 个。不要加括号或注释。
- tags：本章包含的情节类型，从「抉择、冲突、战斗、揭示、情感、关系变化、成长、其他」里选，可以多选。

章节标题：{title}

{text}"""


def parse_summary(raw: str) -> ChapterSummary:
    note = ChapterSummary.model_validate(json.loads(raw))
    note.summary = note.summary.strip()
    note.characters = list(
        dict.fromkeys(name.strip() for name in note.characters if name.strip())
    )
    note.tags = list(dict.fromkeys(note.tags))
    if (
        not note.summary
        or len(note.characters) > 6
        or any(tag not in KINDS for tag in note.tags)
    ):
        raise ValueError("invalid chapter summary")
    return note


MOMENTS_PROMPT_VERSION = "chapter-moments-v2"
MOMENTS_SCHEMA_VERSION = 1
MOMENTS_MAX_TOKENS = 3072
MOMENTS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["moments", "identities"],
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "characters",
                    "kind",
                    "description",
                    "trait",
                    "importance",
                    "evidence",
                ],
                "properties": {
                    "characters": {"type": "array", "items": {"type": "string"}},
                    "kind": {"type": "string", "enum": KINDS},
                    "description": {"type": "string"},
                    "trait": {"type": "string"},
                    "importance": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                    "evidence": {"type": "string"},
                },
            },
        },
        "identities": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["alias", "name", "evidence"],
                "properties": {
                    "alias": {"type": "string"},
                    "name": {"type": "string"},
                    "evidence": {"type": "string"},
                },
            },
        },
    },
}
MOMENTS_PROMPT = """你在为一部长篇小说建立逐章笔记。下面是其中一章的全文。

只依据这一章的原文，输出一个 JSON 对象：
- moments：本章值得记住的关键时刻，按发生先后排列。每条包含：
  - characters：参与的人物，照原文里的称呼写，每个称呼单独一项，不加括号、斜杠或注释。
  - kind：从「抉择、冲突、战斗、揭示、情感、关系变化、成长、其他」里选一个。
  - description：一句话写清发生了什么。分清谁知道什么；话是说出口的还是心里想的；事情已经发生还是只是承诺或计划；是叙述者交代的还是人物亲眼所见。
  - trait：这一刻体现的人物性格侧面，没有就写空字符串。
  - importance：1 到 5，只和本章其他时刻比。5 是本章的转折或高潮。
  - evidence：支撑这一条的原文，逐字复制一句，不超过 60 字，不改写、不拼接、不加省略号。
- identities：本章原文里明确写出「两个称呼指同一个人」的地方，比如外号、代号、化名、头衔紧挨着姓名（「“铁拳”张三」「副队长李四」），改口换称呼（「老王……不对，该叫你王队长了」），或者说明真名。
  alias 和 name 都必须是 evidence 那一句里原样出现的称呼，不要换成你知道的全名、正式名或别处的叫法。比如那句只写了「老王」和「王队长」，就写 alias「老王」、name「王队长」，即使你知道他全名叫王大锤。两个称呼不能相同，也不能是「某某的母亲」「AI」「巨汉」这类身份描述。
  evidence 逐字复制一句同时出现这两个称呼的原文，不超过 60 字。前面章节交代过的关系，本章原文再次明写，也照样列出；同一对称呼本章只列一次。原文没有明说就不要列，不要推测。

不写前情，不推测后续，不评价。

章节标题：{title}

{text}"""


def parse_moments(
    raw: str, chapter: ChapterBoundary, first_line: Callable[[str], int]
) -> ChapterMoments:
    generated = GeneratedMoments.model_validate_json(raw)
    result = ChapterMoments(
        moments=[], identities=[], unanchored_count=0, name_rejected_count=0
    )
    text = chapter.text
    assert text is not None
    for item in [*generated.moments, *generated.identities]:
        if isinstance(item, IdentityFields):
            item.alias, item.name = item.alias.strip(), item.name.strip()
            names = [item.alias, item.name]
        else:
            item.characters = [name.strip() for name in item.characters]
            names = item.characters
        hit = locate_evidence(item.evidence, text)
        if hit is not None and isinstance(item, IdentityFields):
            short, long = sorted(names, key=len)
            short_evidence = hit.replace(long, "") if short in long else hit
            if (
                short == long
                or not locate_evidence(long, hit)
                or not locate_evidence(short, short_evidence)
            ):
                hit = None
        if hit is None:
            result.unanchored_count += 1
            continue
        if any(first_line(name) > chapter.line_end for name in names):
            result.name_rejected_count += 1
            continue
        pos = text.find(hit)
        start = text.count("\n", 0, pos) + 1
        fields = item.model_dump()
        fields.update(evidence=hit, line_start=start, line_end=start + hit.count("\n"))
        if isinstance(item, IdentityFields):
            result.identities.append(Identity(**fields))
        else:
            result.moments.append(Moment(**fields, hidden=False))
    return result


LINKS_PROMPT_VERSION = "identity-links-v1"
LINKS_SCHEMA_VERSION = 1
LINKS_MAX_TOKENS = 512
LINKS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["same", "reason"],
    "properties": {"same": {"type": "boolean"}, "reason": {"type": "string"}},
}
LINKS_PROMPT = """下面是一部长篇小说里的一段原文。

只根据这段原文判断：读到这里的读者，能不能确定「{a}」和「{b}」指的是同一个人物？
- 能确定，same 写 true。比如外号、头衔紧挨着姓名，人物改口换称呼，自报或说明真名，简称和全名。
- 下面几种情况 same 写 false：两个称呼写的是不同的人，比如「某某的亲兵」「某某的姐姐」；其中一个只是泛称或身份描述，比如「巨汉」「镇长」「匪徒」；只凭这段原文确定不了。
- reason 用一句话说明依据。

原文：
{context}"""
