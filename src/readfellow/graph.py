from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

from .progress import ProgressFilter
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

_TYPE_ALIASES = {
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

_RELATION_ALIASES = {
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

_NAME_KEYS = ("name", "名称", "entity", "实体", "text", "文本")
_TYPE_KEYS = ("type", "types", "类型", "entity_type", "entity_types")
_ALIAS_KEYS = ("aliases", "alias", "别名", "又名", "also_known_as")
_SUBJECT_KEYS = ("subject", "主体", "source", "head", "from")
_RELATION_KEYS = ("relation", "predicate", "关系", "关系类型", "type")
_OBJECT_KEYS = ("object", "客体", "target", "tail", "to")
_EVIDENCE_KEYS = ("evidence", "evidence_text", "证据", "证据文本", "quote", "原文")


def graph_path(metadata_dir: Path, collection: str) -> Path:
    return metadata_path(metadata_dir, collection) / "graph.json"


def read_chunks(*, metadata_dir: Path, collection: str) -> list[dict[str, Any]]:
    path = metadata_path(metadata_dir, collection) / "chunks.jsonl"
    if not path.is_file():
        raise FileNotFoundError(
            f"chunk metadata not found: {path}; run the index command first"
        )

    chunks: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                chunks.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON in {path}:{line_number}: {exc}") from exc
    return chunks


def read_graph(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_graph(path: Path, graph: dict[str, Any]) -> None:
    finalize_graph(graph)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(graph, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def empty_graph(
    *,
    collection: str,
    manifest: dict[str, Any] | None = None,
    llm_model: str = "",
) -> dict[str, Any]:
    now = _utc_now()
    return {
        "schema_version": GRAPH_SCHEMA_VERSION,
        "collection": collection,
        "source_path": manifest.get("source_path", "") if manifest else "",
        "llm_model": llm_model,
        "created_at": now,
        "updated_at": now,
        "progress_limit": "",
        "processed_chunk_count": 0,
        "entity_count": 0,
        "relation_count": 0,
        "entities": [],
        "relations": [],
        "extractions": [],
    }


def update_graph_metadata(
    graph: dict[str, Any],
    *,
    collection: str,
    manifest: dict[str, Any],
    llm_model: str,
    progress: ProgressFilter,
    selected_chunk_count: int,
) -> None:
    ensure_graph_shape(graph)
    graph["schema_version"] = GRAPH_SCHEMA_VERSION
    graph["collection"] = collection
    graph["source_path"] = manifest.get("source_path", "")
    graph["llm_model"] = llm_model
    graph["updated_at"] = _utc_now()
    graph["progress_limit"] = progress.description
    graph["selected_chunk_count"] = selected_chunk_count
    graph["processed_chunk_count"] = len(graph["extractions"])
    graph["entity_count"] = len(graph["entities"])
    graph["relation_count"] = len(graph["relations"])


def ensure_graph_shape(graph: dict[str, Any]) -> None:
    now = _utc_now()
    graph.setdefault("schema_version", GRAPH_SCHEMA_VERSION)
    graph.setdefault("collection", "")
    graph.setdefault("source_path", "")
    graph.setdefault("llm_model", "")
    graph.setdefault("created_at", now)
    graph.setdefault("updated_at", now)
    graph.setdefault("progress_limit", "")
    graph.setdefault("entities", [])
    graph.setdefault("relations", [])
    graph.setdefault("extractions", [])
    graph.setdefault("processed_chunk_count", len(graph["extractions"]))
    graph.setdefault("entity_count", len(graph["entities"]))
    graph.setdefault("relation_count", len(graph["relations"]))


def processed_chunk_ids(graph: dict[str, Any]) -> set[str]:
    ensure_graph_shape(graph)
    return {
        str(extraction.get("chunk_id", ""))
        for extraction in graph["extractions"]
        if extraction.get("chunk_id")
    }


def build_extraction_prompt(chunk: dict[str, Any]) -> str:
    entity_types = "、".join(ENTITY_TYPES)
    relation_types = "、".join(RELATION_TYPES)
    chapter = chunk.get("chapter") or "(无章节)"
    return (
        "你是一个面向小说和长文档的本地知识图谱抽取器。只根据给定片段抽取事实，"
        "不要补充片段之外的信息。\n\n"
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
        f"关系尽量从这些类型中选择：{relation_types}。\n"
        "证据必须是片段中的原文短句或短语。没有可抽取内容时返回空数组。\n\n"
        f"chunk_id: {chunk.get('id', '')}\n"
        f"source_path: {chunk.get('source_path', '')}\n"
        f"chapter: {chapter}\n"
        f"lines: {chunk.get('line_start', '')}-{chunk.get('line_end', '')}\n\n"
        "片段正文：\n"
        f"{chunk.get('text', '')}"
    )


def parse_graph_extraction(raw: str | dict[str, Any], chunk: dict[str, Any]) -> dict[str, Any]:
    payload = _parse_json_object(raw)
    context = _chunk_context(chunk)
    entities: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []

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

    return {"entities": entities, "relations": relations}


def merge_extraction(
    graph: dict[str, Any],
    extraction: dict[str, Any],
    chunk: dict[str, Any],
) -> None:
    ensure_graph_shape(graph)
    alias_index = _build_alias_index(graph)

    for entity in extraction.get("entities", []):
        _merge_entity(graph, entity, alias_index)

    for relation in extraction.get("relations", []):
        subject = _merge_entity(
            graph,
            {
                "name": relation["subject"],
                "types": [],
                "aliases": [],
                "mentions": [_chunk_context(relation)],
                "evidence": [],
            },
            alias_index,
        )
        object_ = _merge_entity(
            graph,
            {
                "name": relation["object"],
                "types": [],
                "aliases": [],
                "mentions": [_chunk_context(relation)],
                "evidence": [],
            },
            alias_index,
        )
        relation = deepcopy(relation)
        relation["subject_entity"] = subject["name"]
        relation["object_entity"] = object_["name"]
        if not _has_mapping(
            graph["relations"],
            relation,
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
            graph["relations"].append(relation)

    _mark_extracted(graph, chunk, extraction)
    finalize_graph(graph)


def finalize_graph(graph: dict[str, Any]) -> None:
    ensure_graph_shape(graph)
    for entity in graph["entities"]:
        entity["types"] = sorted(set(_as_strings(entity.get("types"))))
        aliases = [
            alias
            for alias in _as_strings(entity.get("aliases"))
            if alias != entity.get("name")
        ]
        entity["aliases"] = sorted(set(aliases))
        entity["mentions"] = sorted(
            entity.get("mentions", []),
            key=lambda item: (
                _int_value(item.get("chunk_index")),
                _int_value(item.get("line_start")),
            ),
        )
        entity["evidence"] = sorted(
            entity.get("evidence", []),
            key=lambda item: (
                _int_value(item.get("chunk_index")),
                _int_value(item.get("line_start")),
            ),
        )

    graph["entities"] = sorted(graph["entities"], key=lambda item: item["name"])
    graph["relations"] = sorted(
        graph["relations"],
        key=lambda item: (
            _int_value(item.get("chunk_index")),
            _int_value(item.get("line_start")),
            item.get("subject", ""),
            item.get("relation", ""),
            item.get("object", ""),
        ),
    )
    graph["extractions"] = sorted(
        graph["extractions"],
        key=lambda item: (
            _int_value(item.get("chunk_index")),
            str(item.get("chunk_id", "")),
        ),
    )
    graph["processed_chunk_count"] = len(graph["extractions"])
    graph["entity_count"] = len(graph["entities"])
    graph["relation_count"] = len(graph["relations"])


def query_graph(
    graph: dict[str, Any],
    query: str,
    *,
    progress: ProgressFilter | None = None,
) -> dict[str, list[dict[str, Any]]]:
    ensure_graph_shape(graph)
    needle = _case_key(query)
    if not needle:
        return {"entities": [], "relations": []}

    relations: list[dict[str, Any]] = []
    relation_entities: set[str] = set()
    for relation in graph["relations"]:
        if not _allowed(progress, relation):
            continue
        if _matches_relation(relation, needle):
            relations.append(deepcopy(relation))
            relation_entities.add(str(relation.get("subject", "")))
            relation_entities.add(str(relation.get("object", "")))
            relation_entities.add(str(relation.get("subject_entity", "")))
            relation_entities.add(str(relation.get("object_entity", "")))

    entities: list[dict[str, Any]] = []
    for entity in graph["entities"]:
        filtered = _filter_entity(entity, progress)
        if filtered is None:
            continue
        if _matches_entity(filtered, needle) or filtered["name"] in relation_entities:
            entities.append(filtered)

    return {"entities": entities, "relations": relations}


def _parse_json_object(raw: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, dict):
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

    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def _parse_entity(item: Any, context: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(item, str):
        name = _normalize_text(item)
        types: list[str] = []
        aliases: list[str] = []
        evidence = ""
    elif isinstance(item, dict):
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

    entity = {
        "name": name,
        "types": [value for value in types if value],
        "aliases": [alias for alias in aliases if alias and alias != name],
        "mentions": [context],
        "evidence": [],
    }
    if evidence:
        entity["evidence"].append({**context, "text": evidence})
    return entity


def _parse_relation(item: Any, context: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None

    subject = _normalize_text(_get_any(item, _SUBJECT_KEYS, ""))
    relation = _normalize_relation(_get_any(item, _RELATION_KEYS, ""))
    object_ = _normalize_text(_get_any(item, _OBJECT_KEYS, ""))
    evidence = _normalize_text(_get_any(item, _EVIDENCE_KEYS, ""))
    if not subject or not relation or not object_:
        return None

    return {
        "subject": subject,
        "relation": relation,
        "object": object_,
        "evidence": evidence,
        **context,
    }


def _merge_entity(
    graph: dict[str, Any],
    incoming: dict[str, Any],
    alias_index: dict[str, int],
) -> dict[str, Any]:
    name = _normalize_text(incoming.get("name", ""))
    aliases = [_normalize_text(alias) for alias in _as_strings(incoming.get("aliases"))]
    aliases = [alias for alias in aliases if alias and alias != name]
    index = _find_entity_index(alias_index, [name, *aliases])

    if index is None:
        entity = {
            "name": name,
            "types": [],
            "aliases": [],
            "mentions": [],
            "evidence": [],
        }
        graph["entities"].append(entity)
        index = len(graph["entities"]) - 1
    else:
        entity = graph["entities"][index]
        if name and name != entity["name"]:
            aliases.append(name)

    for type_ in _as_strings(incoming.get("types")):
        normalized = _normalize_entity_type(type_)
        if normalized and normalized not in entity["types"]:
            entity["types"].append(normalized)
    for alias in aliases:
        if alias not in entity["aliases"]:
            entity["aliases"].append(alias)
    for mention in incoming.get("mentions", []):
        if not _has_mapping(
            entity["mentions"],
            mention,
            keys=("chunk_id", "line_start", "line_end"),
        ):
            entity["mentions"].append(deepcopy(mention))
    for evidence in incoming.get("evidence", []):
        if not _has_mapping(entity["evidence"], evidence, keys=("chunk_id", "text")):
            entity["evidence"].append(deepcopy(evidence))

    for value in [entity["name"], *entity["aliases"]]:
        key = _case_key(value)
        if key:
            alias_index[key] = index
    return entity


def _mark_extracted(
    graph: dict[str, Any],
    chunk: dict[str, Any],
    extraction: dict[str, Any],
) -> None:
    record = _chunk_context(chunk)
    if _has_mapping(graph["extractions"], record, keys=("chunk_id",)):
        return
    graph["extractions"].append(
        {
            **record,
            "entity_count": len(extraction.get("entities", [])),
            "relation_count": len(extraction.get("relations", [])),
        }
    )


def _build_alias_index(graph: dict[str, Any]) -> dict[str, int]:
    index: dict[str, int] = {}
    for entity_index, entity in enumerate(graph.get("entities", [])):
        for value in [entity.get("name", ""), *_as_strings(entity.get("aliases"))]:
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
    entity: dict[str, Any],
    progress: ProgressFilter | None,
) -> dict[str, Any] | None:
    filtered = deepcopy(entity)
    if progress is None:
        return filtered

    filtered["mentions"] = [
        mention for mention in filtered.get("mentions", []) if _allowed(progress, mention)
    ]
    filtered["evidence"] = [
        evidence for evidence in filtered.get("evidence", []) if _allowed(progress, evidence)
    ]
    if not filtered["mentions"] and not filtered["evidence"]:
        return None
    return filtered


def _matches_entity(entity: dict[str, Any], needle: str) -> bool:
    values = [
        entity.get("name", ""),
        *_as_strings(entity.get("aliases")),
        *_as_strings(entity.get("types")),
        *[evidence.get("text", "") for evidence in entity.get("evidence", [])],
    ]
    return any(needle in _case_key(value) for value in values)


def _matches_relation(relation: dict[str, Any], needle: str) -> bool:
    values = (
        relation.get("subject", ""),
        relation.get("subject_entity", ""),
        relation.get("relation", ""),
        relation.get("object", ""),
        relation.get("object_entity", ""),
        relation.get("evidence", ""),
    )
    return any(needle in _case_key(value) for value in values)


def _allowed(progress: ProgressFilter | None, fields: dict[str, Any]) -> bool:
    if progress is None:
        return True
    try:
        return progress.allows(fields)
    except KeyError:
        return False


def _chunk_context(chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "chunk_id": str(chunk.get("chunk_id") or chunk.get("id") or ""),
        "source_path": str(chunk.get("source_path", "")),
        "chunk_index": _int_value(chunk.get("chunk_index")),
        "line_start": _int_value(chunk.get("line_start")),
        "line_end": _int_value(chunk.get("line_end")),
        "chapter": str(chunk.get("chapter", "")),
    }


def _get_any(mapping: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
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
    return _RELATION_ALIASES.get(text.casefold(), _RELATION_ALIASES.get(text, text))


def _case_key(value: Any) -> str:
    return _normalize_text(value).casefold()


def _has_mapping(
    items: list[dict[str, Any]],
    candidate: dict[str, Any],
    *,
    keys: tuple[str, ...],
) -> bool:
    return any(all(item.get(key) == candidate.get(key) for key in keys) for item in items)


def _int_value(value: Any) -> int:
    if value in (None, ""):
        return 0
    return int(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
