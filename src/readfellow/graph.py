from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel

from .models import (
    Chunk,
    ChunkContext,
    GraphEntity,
    GraphEvidence,
    GraphExtraction,
    GraphExtractionRecord,
    GraphQueryResult,
    GraphRelation,
    IndexManifest,
    KnowledgeGraph,
    ProgressFilter,
    utc_now_iso,
)
from .store import metadata_path


GRAPH_SCHEMA_VERSION = 1
ENTITY_TYPES = ("人物", "地点", "组织", "物品", "事件", "概念")
RELATION_TYPES = (
    "是",
    "属于",
    "位于",
    "出现于",
    "认识",
    "敌对",
    "帮助",
    "伤害",
    "寻找",
    "拥有",
    "说过",
    "导致",
    "参与事件",
    "身份是",
    "别名是",
)

_TYPE_ALIASES = MappingProxyType(
    {
        "person": "人物",
        "people": "人物",
        "character": "人物",
        "角色": "人物",
        "人": "人物",
        "place": "地点",
        "location": "地点",
        "loc": "地点",
        "地点": "地点",
        "organization": "组织",
        "organisation": "组织",
        "org": "组织",
        "组织": "组织",
        "item": "物品",
        "object": "物品",
        "thing": "物品",
        "物": "物品",
        "物品": "物品",
        "event": "事件",
        "事件": "事件",
        "concept": "概念",
        "概念": "概念",
    }
)

_RELATION_ALIASES = MappingProxyType(
    {
        "参加": "参与事件",
        "参与": "参与事件",
        "参与了": "参与事件",
        "别名": "别名是",
        "化名": "别名是",
        "身份": "身份是",
        "说": "说过",
        "说过": "说过",
        "造成": "导致",
        "引发": "导致",
    }
)

_NAME_KEYS = ("name", "名称", "entity", "实体", "text", "文本")
_TYPE_KEYS = ("type", "types", "类型", "entity_type", "entity_types")
_ALIAS_KEYS = ("aliases", "alias", "别名", "又名", "also_known_as")
_SUBJECT_KEYS = ("subject", "主体", "source", "head", "from")
_RELATION_KEYS = ("relation", "predicate", "关系", "关系类型", "type")
_OBJECT_KEYS = ("object", "客体", "target", "tail", "to")
_EVIDENCE_KEYS = ("evidence", "evidence_text", "证据", "证据文本", "quote", "原文")


def graph_path(metadata_dir: Path, collection: str) -> Path:
    return metadata_path(metadata_dir, collection) / "graph.json"


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
                raise ValueError(f"invalid chunk metadata in {path}:{line_number}: {exc}") from exc
    return chunks


def read_graph(path: Path) -> KnowledgeGraph:
    return KnowledgeGraph.model_validate_json(path.read_text(encoding="utf-8"))


def write_graph(path: Path, graph: KnowledgeGraph) -> None:
    finalize_graph(graph)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(graph.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def empty_graph(
    *,
    collection: str,
    manifest: IndexManifest | None = None,
    llm_model: str = "",
) -> KnowledgeGraph:
    now = utc_now_iso()
    return KnowledgeGraph(
        schema_version=GRAPH_SCHEMA_VERSION,
        collection=collection,
        source_path=manifest.source_path if manifest else "",
        llm_model=llm_model,
        created_at=now,
        updated_at=now,
    )


def update_graph_metadata(
    graph: KnowledgeGraph,
    *,
    collection: str,
    manifest: IndexManifest,
    llm_model: str,
    progress: ProgressFilter,
    selected_chunk_count: int,
) -> None:
    graph.schema_version = GRAPH_SCHEMA_VERSION
    graph.collection = collection
    graph.source_path = manifest.source_path
    graph.llm_model = llm_model
    graph.updated_at = utc_now_iso()
    graph.progress_limit = progress.description
    graph.selected_chunk_count = selected_chunk_count
    graph.processed_chunk_count = len(graph.extractions)
    graph.entity_count = len(graph.entities)
    graph.relation_count = len(graph.relations)


def processed_chunk_ids(graph: KnowledgeGraph) -> set[str]:
    return {
        extraction.chunk_id
        for extraction in graph.extractions
        if extraction.chunk_id
    }


def build_extraction_prompt(chunk: Chunk | ChunkContext | Mapping[str, Any]) -> str:
    context = _chunk_context(chunk)
    text = _chunk_text(chunk)
    entity_types = "、".join(ENTITY_TYPES)
    relation_types = "、".join(RELATION_TYPES)
    chapter = context.chapter or "(无章节)"
    return (
        "你是一个面向小说和长文档的本地知识图谱抽取器。只根据给定片段抽取事实，"
        "不要补充片段之外的信息。不要输出思考过程，不要使用 <think> 标签。\n\n"
        "返回严格 JSON，不要 Markdown，不要解释。JSON schema:\n"
        "{\n"
        '  "entities": [\n'
        '    {"name": "实体名", "type": "人物", "aliases": ["别名"], "evidence": "片段中的短证据"}\n'
        "  ],\n"
        '  "relations": [\n'
        '    {"subject": "主体", "relation": "关系", "object": "客体", "evidence": "片段中的短证据"}\n'
        "  ]\n"
        "}\n"
        f"实体类型只能从这些类型中选择：{entity_types}。\n"
        f"关系字段只能从这些类型中选择：{relation_types}。"
        "不能使用“关系”“相关”“有关”等泛化关系。\n"
        "最多抽取 12 个最重要实体和 18 条最重要关系。"
        "同一主体、关系、客体只能出现一次。"
        "证据必须是片段中的原文短句或短语，不超过 60 个汉字。"
        "不要把整段正文放入证据。没有可抽取内容时返回空数组。\n\n"
        f"chunk_id: {context.chunk_id}\n"
        f"source_path: {context.source_path}\n"
        f"chapter: {chapter}\n"
        f"lines: {context.line_start}-{context.line_end}\n\n"
        "片段正文：\n"
        f"{text}"
    )


def parse_graph_extraction(
    raw: str | Mapping[str, Any],
    chunk: Chunk | ChunkContext | Mapping[str, Any],
) -> GraphExtraction:
    payload = _parse_json_object(raw)
    context = _chunk_context(chunk)
    entities: list[GraphEntity] = []
    relations: list[GraphRelation] = []

    for item in _as_list(_get_any(payload, ("entities", "实体"), [])):
        entity = _parse_entity(item, context)
        if entity is not None:
            entities.append(entity)

    for item in _as_list(
        _get_any(payload, ("relations", "关系", "triples", "edges"), [])
    ):
        relation = _parse_relation(item, context)
        if relation is not None:
            relations.append(relation)

    return GraphExtraction(entities=entities, relations=relations)


def merge_extraction(
    graph: KnowledgeGraph,
    extraction: GraphExtraction,
    chunk: Chunk | ChunkContext | Mapping[str, Any],
) -> None:
    alias_index = _build_alias_index(graph)

    for entity in extraction.entities:
        _merge_entity(graph, entity, alias_index)

    for relation in extraction.relations:
        subject = _merge_entity(
            graph,
            GraphEntity(
                name=relation.subject,
                mentions=[_chunk_context(relation)],
            ),
            alias_index,
        )
        object_ = _merge_entity(
            graph,
            GraphEntity(
                name=relation.object,
                mentions=[_chunk_context(relation)],
            ),
            alias_index,
        )
        merged_relation = relation.model_copy(
            update={
                "subject_entity": subject.name,
                "object_entity": object_.name,
            },
            deep=True,
        )
        if not _has_same_values(
            graph.relations,
            merged_relation,
            keys=(
                "subject",
                "relation",
                "object",
                "evidence",
                "chunk_id",
                "line_start",
                "line_end",
            ),
        ):
            graph.relations.append(merged_relation)

    _mark_extracted(graph, chunk, extraction)
    finalize_graph(graph)


def finalize_graph(graph: KnowledgeGraph) -> None:
    for entity in graph.entities:
        entity.types = sorted(set(_as_strings(entity.types)))
        entity.aliases = sorted(
            {
                alias
                for alias in _as_strings(entity.aliases)
                if alias != entity.name
            }
        )
        entity.mentions = sorted(
            entity.mentions,
            key=lambda item: (
                _int_value(item.chunk_index),
                _int_value(item.line_start),
            ),
        )
        entity.evidence = sorted(
            entity.evidence,
            key=lambda item: (
                _int_value(item.chunk_index),
                _int_value(item.line_start),
            ),
        )

    graph.entities = sorted(graph.entities, key=lambda item: item.name)
    graph.relations = sorted(
        graph.relations,
        key=lambda item: (
            _int_value(item.chunk_index),
            _int_value(item.line_start),
            item.subject,
            item.relation,
            item.object,
        ),
    )
    graph.extractions = sorted(
        graph.extractions,
        key=lambda item: (
            _int_value(item.chunk_index),
            item.chunk_id,
        ),
    )
    graph.processed_chunk_count = len(graph.extractions)
    graph.entity_count = len(graph.entities)
    graph.relation_count = len(graph.relations)


def query_graph(
    graph: KnowledgeGraph,
    query: str,
    *,
    progress: ProgressFilter | None = None,
) -> GraphQueryResult:
    needle = _case_key(query)
    if not needle:
        return GraphQueryResult()

    relations: list[GraphRelation] = []
    relation_entities: set[str] = set()
    for relation in graph.relations:
        if not _allowed(progress, relation):
            continue
        if _matches_relation(relation, needle):
            relations.append(relation.model_copy(deep=True))
            relation_entities.add(relation.subject)
            relation_entities.add(relation.object)
            relation_entities.add(relation.subject_entity)
            relation_entities.add(relation.object_entity)

    entities: list[GraphEntity] = []
    for entity in graph.entities:
        filtered = _filter_entity(entity, progress)
        if filtered is None:
            continue
        if _matches_entity(filtered, needle) or filtered.name in relation_entities:
            entities.append(filtered)

    return GraphQueryResult(entities=entities, relations=relations)


def _parse_json_object(raw: str | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw

    text = raw.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(text[start : end + 1])

    if not isinstance(parsed, Mapping):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def _parse_entity(item: Any, context: ChunkContext) -> GraphEntity | None:
    if isinstance(item, str):
        name = _normalize_text(item)
        types: list[str] = []
        aliases: list[str] = []
        evidence = ""
    elif isinstance(item, Mapping):
        name = _normalize_text(_get_any(item, _NAME_KEYS, ""))
        types = [
            _normalize_entity_type(value)
            for value in _as_strings(_get_any(item, _TYPE_KEYS, []))
        ]
        aliases = [
            _normalize_text(value)
            for value in _as_strings(_get_any(item, _ALIAS_KEYS, []))
        ]
        evidence = _normalize_text(_get_any(item, _EVIDENCE_KEYS, ""))
    else:
        return None

    if not name:
        return None

    entity = GraphEntity(
        name=name,
        types=[value for value in types if value],
        aliases=[alias for alias in aliases if alias and alias != name],
        mentions=[context.model_copy(deep=True)],
    )
    if evidence:
        entity.evidence.append(
            GraphEvidence(**context.model_dump(mode="json"), text=evidence)
        )
    return entity


def _parse_relation(item: Any, context: ChunkContext) -> GraphRelation | None:
    if not isinstance(item, Mapping):
        return None

    subject = _normalize_text(_get_any(item, _SUBJECT_KEYS, ""))
    relation = _normalize_relation(_get_any(item, _RELATION_KEYS, ""))
    object_ = _normalize_text(_get_any(item, _OBJECT_KEYS, ""))
    evidence = _normalize_text(_get_any(item, _EVIDENCE_KEYS, ""))
    if not subject or not relation or not object_:
        return None

    return GraphRelation(
        subject=subject,
        relation=relation,
        object=object_,
        evidence=evidence,
        **context.model_dump(mode="json"),
    )


def _merge_entity(
    graph: KnowledgeGraph,
    incoming: GraphEntity,
    alias_index: dict[str, int],
) -> GraphEntity:
    name = _normalize_text(incoming.name)
    aliases = [_normalize_text(alias) for alias in _as_strings(incoming.aliases)]
    aliases = [alias for alias in aliases if alias and alias != name]
    index = _find_entity_index(alias_index, [name, *aliases])

    if index is None:
        entity = GraphEntity(name=name)
        graph.entities.append(entity)
        index = len(graph.entities) - 1
    else:
        entity = graph.entities[index]
        if name and name != entity.name:
            aliases.append(name)

    for type_ in _as_strings(incoming.types):
        normalized = _normalize_entity_type(type_)
        if normalized and normalized not in entity.types:
            entity.types.append(normalized)
    for alias in aliases:
        if alias not in entity.aliases:
            entity.aliases.append(alias)
    for mention in incoming.mentions:
        if not _has_same_values(
            entity.mentions,
            mention,
            keys=("chunk_id", "line_start", "line_end"),
        ):
            entity.mentions.append(mention.model_copy(deep=True))
    for evidence in incoming.evidence:
        if not _has_same_values(entity.evidence, evidence, keys=("chunk_id", "text")):
            entity.evidence.append(evidence.model_copy(deep=True))

    for value in [entity.name, *entity.aliases]:
        key = _case_key(value)
        if key:
            alias_index[key] = index
    return entity


def _mark_extracted(
    graph: KnowledgeGraph,
    chunk: Chunk | ChunkContext | Mapping[str, Any],
    extraction: GraphExtraction,
) -> None:
    record = _chunk_context(chunk)
    if _has_same_values(graph.extractions, record, keys=("chunk_id",)):
        return
    graph.extractions.append(
        GraphExtractionRecord(
            **record.model_dump(mode="json"),
            entity_count=len(extraction.entities),
            relation_count=len(extraction.relations),
        )
    )


def _build_alias_index(graph: KnowledgeGraph) -> dict[str, int]:
    index: dict[str, int] = {}
    for entity_index, entity in enumerate(graph.entities):
        for value in [entity.name, *_as_strings(entity.aliases)]:
            key = _case_key(value)
            if key:
                index.setdefault(key, entity_index)
    return index


def _find_entity_index(alias_index: dict[str, int], values: list[str]) -> int | None:
    for value in values:
        key = _case_key(value)
        if key in alias_index:
            return alias_index[key]
    return None


def _filter_entity(
    entity: GraphEntity,
    progress: ProgressFilter | None,
) -> GraphEntity | None:
    filtered = entity.model_copy(deep=True)
    if progress is None:
        return filtered

    filtered.mentions = [
        mention for mention in filtered.mentions if _allowed(progress, mention)
    ]
    filtered.evidence = [
        evidence for evidence in filtered.evidence if _allowed(progress, evidence)
    ]
    if not filtered.mentions and not filtered.evidence:
        return None
    return filtered


def _matches_entity(entity: GraphEntity, needle: str) -> bool:
    values = [
        entity.name,
        *_as_strings(entity.aliases),
        *_as_strings(entity.types),
        *[evidence.text for evidence in entity.evidence],
    ]
    return any(needle in _case_key(value) for value in values)


def _matches_relation(relation: GraphRelation, needle: str) -> bool:
    values = (
        relation.subject,
        relation.subject_entity,
        relation.relation,
        relation.object,
        relation.object_entity,
        relation.evidence,
    )
    return any(needle in _case_key(value) for value in values)


def _allowed(progress: ProgressFilter | None, fields: BaseModel) -> bool:
    if progress is None:
        return True
    try:
        return progress.allows(fields)
    except ValueError:
        return False


def _chunk_context(chunk: Chunk | ChunkContext | Mapping[str, Any]) -> ChunkContext:
    if isinstance(chunk, Chunk):
        return ChunkContext(
            chunk_id=chunk.id,
            source_path=chunk.source_path,
            chunk_index=chunk.chunk_index,
            line_start=chunk.line_start,
            line_end=chunk.line_end,
            chapter=chunk.chapter,
        )
    if isinstance(chunk, ChunkContext):
        return ChunkContext.model_validate(chunk)
    if isinstance(chunk, BaseModel):
        data = chunk.model_dump(mode="json")
    else:
        data = chunk
    return ChunkContext(
        chunk_id=str(data.get("chunk_id") or data.get("id") or ""),
        source_path=str(data.get("source_path", "")),
        chunk_index=_int_value(data.get("chunk_index")),
        line_start=_int_value(data.get("line_start")),
        line_end=_int_value(data.get("line_end")),
        chapter=str(data.get("chapter", "")),
    )


def _chunk_text(chunk: Chunk | ChunkContext | Mapping[str, Any]) -> str:
    if isinstance(chunk, Chunk):
        return chunk.text
    if isinstance(chunk, BaseModel):
        return str(getattr(chunk, "text", ""))
    return str(chunk.get("text", ""))


def _get_any(mapping: Mapping[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        if "、" in value:
            return [_normalize_text(part) for part in value.split("、")]
        return [_normalize_text(value)]
    if isinstance(value, (list, tuple, set)):
        return [_normalize_text(item) for item in value]
    return [_normalize_text(value)]


def _normalize_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text.strip("`\"'“”‘’")


def _normalize_entity_type(value: Any) -> str:
    text = _normalize_text(value)
    return _TYPE_ALIASES.get(text.casefold(), _TYPE_ALIASES.get(text, text))


def _normalize_relation(value: Any) -> str:
    text = _normalize_text(value)
    normalized = _RELATION_ALIASES.get(
        text.casefold(),
        _RELATION_ALIASES.get(text, text),
    )
    if normalized not in RELATION_TYPES:
        return ""
    return normalized


def _case_key(value: Any) -> str:
    return _normalize_text(value).casefold()


def _has_same_values(
    items: list[BaseModel],
    candidate: BaseModel,
    *,
    keys: tuple[str, ...],
) -> bool:
    return any(
        all(getattr(item, key) == getattr(candidate, key) for key in keys)
        for item in items
    )


def _int_value(value: Any) -> int:
    if value in (None, ""):
        return 0
    return int(value)
