from __future__ import annotations

import json

import pytest

from readfellow.graph import (
    empty_graph,
    merge_extraction,
    parse_graph_extraction,
    query_graph,
)
from readfellow.models import Chunk
from readfellow.progress import ProgressFilter


def chunk(
    *,
    chunk_index: int = 0,
    line_start: int = 1,
    line_end: int = 8,
    chapter: str = "第一章 开始",
    text: str = "向山被人称作武神。他帮助了尤基。",
) -> Chunk:
    return Chunk(
        id=f"chunk_{chunk_index:06d}",
        source_path="corpus/samples/novel.txt",
        source_hash="source-hash",
        chunk_index=chunk_index,
        text=text,
        text_hash=f"text-hash-{chunk_index}",
        line_start=line_start,
        line_end=line_end,
        byte_start=0,
        byte_end=len(text.encode("utf-8")),
        chapter=chapter,
    )


def test_parse_and_merge_graph_extractions() -> None:
    graph = empty_graph(collection="sample")
    first = chunk()
    payload = {
        "entities": [
            {
                "name": "向山",
                "type": "人物",
                "aliases": ["武神"],
                "evidence": "向山被人称作武神",
            },
            {"name": "尤基", "type": "人物"},
        ],
        "relations": [
            {
                "subject": "向山",
                "relation": "身份是",
                "object": "武神",
                "evidence": "向山被人称作武神",
            },
            {
                "subject": "向山",
                "relation": "帮助",
                "object": "尤基",
                "evidence": "他帮助了尤基",
            },
        ],
    }

    extraction = parse_graph_extraction(json.dumps(payload, ensure_ascii=False), first)
    merge_extraction(graph, extraction, first)

    second = chunk(
        chunk_index=1,
        line_start=9,
        line_end=14,
        text="老向认识尤基。",
    )
    second_payload = {
        "entities": [
            {"name": "向山", "types": ["人物"], "aliases": ["老向"]},
        ],
        "relations": [
            {
                "subject": "老向",
                "relation": "认识",
                "object": "尤基",
                "evidence": "老向认识尤基",
            },
        ],
    }
    merge_extraction(graph, parse_graph_extraction(second_payload, second), second)

    xiangshan = next(entity for entity in graph.entities if entity.name == "向山")
    assert xiangshan.types == ["人物"]
    assert xiangshan.aliases == ["武神", "老向"]
    assert len(xiangshan.mentions) == 2
    assert graph.entity_count == 2
    assert graph.relation_count == 3
    identity = next(
        relation for relation in graph.relations if relation.relation == "身份是"
    )
    assert identity.object == "武神"
    assert identity.object_entity == "向山"


def test_graph_query_matches_entity_alias_and_relation_keyword() -> None:
    graph = empty_graph(collection="sample")
    data = {
        "entities": [
            {"name": "向山", "type": "人物", "aliases": ["武神"]},
            {"name": "尤基", "type": "人物"},
        ],
        "relations": [
            {
                "subject": "向山",
                "relation": "帮助",
                "object": "尤基",
                "evidence": "他帮助了尤基",
            },
        ],
    }
    merge_extraction(graph, parse_graph_extraction(data, chunk()), chunk())

    alias_result = query_graph(graph, "武神")
    assert [entity.name for entity in alias_result.entities] == ["向山"]

    relation_result = query_graph(graph, "帮助")
    assert [relation.relation for relation in relation_result.relations] == ["帮助"]
    assert {entity.name for entity in relation_result.entities} == {"向山", "尤基"}


def test_parse_skips_unknown_relation_types() -> None:
    extraction = parse_graph_extraction(
        {
            "entities": [{"name": "向山", "type": "人物"}],
            "relations": [
                {"subject": "向山", "relation": "相关", "object": "武道"},
                {"subject": "向山", "relation": "帮助", "object": "尤基"},
            ],
        },
        chunk(),
    )

    assert [
        (relation.relation, relation.object) for relation in extraction.relations
    ] == [("帮助", "尤基")]


def test_parse_rejects_evidence_not_found_verbatim_in_chunk() -> None:
    with pytest.raises(ValueError, match="entity evidence is not an exact substring"):
        parse_graph_extraction(
            {
                "entities": [
                    {
                        "name": "向山",
                        "type": "人物",
                        "evidence": "向山打败了尤基",
                    }
                ],
                "relations": [],
            },
            chunk(),
        )


def test_parse_stores_the_source_wording_of_a_loosely_quoted_evidence() -> None:
    extraction = parse_graph_extraction(
        {
            "entities": [
                {
                    "name": "向山",
                    "type": "人物",
                    "evidence": "向山被人称作武神。‘他帮助了尤基。’",
                }
            ],
            "relations": [],
        },
        chunk(text="向山被人称作武神。\r\n    “他帮助了尤基。”\r\n"),
    )

    assert (
        extraction.entities[0].evidence[0].text
        == "向山被人称作武神。\r\n    “他帮助了尤基。"
    )


def test_parse_rejects_relation_evidence_not_found_verbatim_in_chunk() -> None:
    with pytest.raises(ValueError, match="relation evidence is not an exact substring"):
        parse_graph_extraction(
            {
                "entities": [],
                "relations": [
                    {
                        "subject": "向山",
                        "relation": "帮助",
                        "object": "尤基",
                        "evidence": "向山打败了尤基",
                    }
                ],
            },
            chunk(),
        )


def test_graph_query_progress_filters_future_relations() -> None:
    graph = empty_graph(collection="sample")
    early = chunk(chunk_index=0, line_start=1, line_end=8)
    late = chunk(
        chunk_index=4,
        line_start=30,
        line_end=40,
        chapter="第二章 之后",
        text="组织A开始敌对向山。",
    )
    merge_extraction(
        graph,
        parse_graph_extraction(
            {
                "entities": [
                    {"name": "向山", "type": "人物"},
                    {"name": "尤基", "type": "人物"},
                ],
                "relations": [
                    {
                        "subject": "向山",
                        "relation": "帮助",
                        "object": "尤基",
                        "evidence": "他帮助了尤基",
                    }
                ],
            },
            early,
        ),
        early,
    )
    merge_extraction(
        graph,
        parse_graph_extraction(
            {
                "entities": [
                    {"name": "向山", "type": "人物"},
                    {"name": "组织A", "type": "组织"},
                ],
                "relations": [
                    {
                        "subject": "组织A",
                        "relation": "敌对",
                        "object": "向山",
                        "evidence": "组织A开始敌对向山",
                    }
                ],
            },
            late,
        ),
        late,
    )
    progress = ProgressFilter(
        expression="line_end <= 20 and chunk_index <= 2",
        description="test progress",
        max_line_end=20,
        max_chunk_index=2,
    )

    result = query_graph(graph, "向山", progress=progress)

    assert [(r.relation, r.line_end, r.chunk_index) for r in result.relations] == [
        ("帮助", 8, 0)
    ]
    assert {entity.name for entity in result.entities} == {"向山", "尤基"}
    assert all(
        mention.line_end <= 20
        for entity in result.entities
        for mention in entity.mentions
    )
