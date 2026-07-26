from __future__ import annotations

import json
from pathlib import Path

import pytest

from readfellow.analysis import read_analysis, write_analysis
from readfellow.app import AnalysisBuildOptions, ProgressLimit, build_analysis
from readfellow.chunking import CHUNKER_VERSION, chunk_document
from readfellow.config import PathConfig, ReadFellowConfig
from readfellow.models import IndexManifest
from readfellow.store import write_manifest

TWO_CHAPTERS = (
    "第一章 开始\n\n向山帮助了尤基。\n\n尤基修好了机器。\n\n"
    "第二章 之后\n\n组织A开始敌对向山。\n\n向山离开了城市。\n\n"
    "第三章 结束\n\n尤基找到了答案。\n"
)

FIRST_CHAPTER_PAYLOAD = {
    "summary": "向山帮助尤基修好机器。",
    "characters": [
        {
            "name": "向山",
            "role_in_chapter": "帮助者",
            "evidence": "向山帮助了尤基",
        }
    ],
    "events": [
        {"order": 1, "description": "向山施以援手", "evidence": "向山帮助了尤基"},
        {"order": 2, "description": "机器被修好", "evidence": "尤基修好了机器"},
    ],
}

SECOND_CHAPTER_PAYLOAD = {
    "summary": "组织A敌对向山，向山离开城市。",
    "characters": [
        {"name": "组织A", "role_in_chapter": "敌人", "evidence": "组织A开始敌对向山"}
    ],
    "events": [
        {"order": 1, "description": "组织A发难", "evidence": "组织A开始敌对向山"},
        {"order": 2, "description": "向山出走", "evidence": "向山离开了城市"},
    ],
}


class DeterministicGenerator:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def generate_json(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps(self._responses.pop(0), ensure_ascii=False)


def build_collection(tmp_path: Path) -> ReadFellowConfig:
    source = tmp_path / "novel.txt"
    source.write_text(TWO_CHAPTERS, encoding="utf-8")
    chunks = chunk_document(
        source,
        source_path=str(source),
        target_chars=20,
        overlap_chars=0,
    )
    config = ReadFellowConfig(
        paths=PathConfig(
            index_dir=tmp_path / "indexes",
            metadata_dir=tmp_path / "metadata",
        )
    )
    write_manifest(
        metadata_dir=config.paths.metadata_dir,
        collection="books",
        manifest=IndexManifest(
            collection="books",
            collection_path="indexes/books",
            source_path=str(source),
            model="manifest-embedding",
            embedding_dimension=2,
            chunk_count=len(chunks),
            chunk_chars=20,
            overlap_chars=0,
            chunker_version=CHUNKER_VERSION,
        ),
        chunks=chunks,
    )
    return config


def test_analysis_rejects_evidence_not_found_verbatim_in_chapter(
    tmp_path: Path,
) -> None:
    config = build_collection(tmp_path)
    invalid = {
        "summary": "梗概",
        "characters": [{"name": "向山", "evidence": "向山打败了尤基"}],
        "events": [],
    }
    generator = DeterministicGenerator([invalid, invalid])

    with pytest.raises(RuntimeError, match="failed to analyze chapter 第一章 开始"):
        build_analysis(
            config,
            "books",
            progress=ProgressLimit(max_chapter=1),
            options=AnalysisBuildOptions(retries=1),
            generator=generator,
        )

    assert len(generator.prompts) == 2


def test_analysis_resumes_and_only_analyzes_new_chapters(tmp_path: Path) -> None:
    config = build_collection(tmp_path)

    first = DeterministicGenerator([FIRST_CHAPTER_PAYLOAD])
    first_result = build_analysis(
        config,
        "books",
        progress=ProgressLimit(max_chapter=1),
        options=AnalysisBuildOptions(retries=0),
        generator=first,
    )
    assert first_result.status == "built"
    assert len(first.prompts) == 1
    assert "prompt_version: chapter-analysis-v1" in first.prompts[0]
    assert "第二章" not in first.prompts[0]
    assert [chapter.chapter_title for chapter in first_result.chapters] == [
        "第一章 开始"
    ]

    second = DeterministicGenerator([SECOND_CHAPTER_PAYLOAD])
    second_result = build_analysis(
        config,
        "books",
        progress=ProgressLimit(max_chapter=2),
        options=AnalysisBuildOptions(retries=0),
        generator=second,
    )
    assert len(second.prompts) == 1
    assert "第二章 之后" in second.prompts[0]
    assert [chapter.chapter_title for chapter in second_result.chapters] == [
        "第一章 开始",
        "第二章 之后",
    ]

    third = DeterministicGenerator([])
    third_result = build_analysis(
        config,
        "books",
        progress=ProgressLimit(max_chapter=2),
        options=AnalysisBuildOptions(retries=0),
        generator=third,
    )
    assert third_result.status == "up_to_date"
    assert third.prompts == []


def test_analysis_rebuilds_when_prompt_version_or_chunk_hash_changes(
    tmp_path: Path,
) -> None:
    config = build_collection(tmp_path)
    build_analysis(
        config,
        "books",
        progress=ProgressLimit(max_chapter=1),
        options=AnalysisBuildOptions(retries=0),
        generator=DeterministicGenerator([FIRST_CHAPTER_PAYLOAD]),
    )
    path = config.paths.metadata_dir / "books" / "analysis.json"

    document = read_analysis(path)
    document.prompt_version = "chapter-analysis-v0"
    write_analysis(path, document)
    rebuilt = build_analysis(
        config,
        "books",
        progress=ProgressLimit(max_chapter=1),
        options=AnalysisBuildOptions(retries=0),
        generator=DeterministicGenerator([FIRST_CHAPTER_PAYLOAD]),
    )
    assert rebuilt.status == "rebuilt"

    document = read_analysis(path)
    first_chunk_id = document.chapters[0].chunk_ids[0]
    document.chunk_text_hashes[first_chunk_id] = "changed"
    write_analysis(path, document)
    rebuilt_again = build_analysis(
        config,
        "books",
        progress=ProgressLimit(max_chapter=1),
        options=AnalysisBuildOptions(retries=0),
        generator=DeterministicGenerator([FIRST_CHAPTER_PAYLOAD]),
    )
    assert rebuilt_again.status == "rebuilt"


def test_analysis_suppresses_summary_when_progress_cuts_a_chapter(
    tmp_path: Path,
) -> None:
    config = build_collection(tmp_path)
    build_analysis(
        config,
        "books",
        progress=ProgressLimit(max_chapter=2),
        options=AnalysisBuildOptions(retries=0),
        generator=DeterministicGenerator(
            [FIRST_CHAPTER_PAYLOAD, SECOND_CHAPTER_PAYLOAD]
        ),
    )

    stored = read_analysis(config.paths.metadata_dir / "books" / "analysis.json")
    assert len(stored.chapters) == 2

    # Line 9 is the middle of chapter two: chapter one stays whole, chapter two
    # keeps only the events proven to come from an already-read chunk.
    cut = build_analysis(
        config,
        "books",
        progress=ProgressLimit(max_line=9),
        options=AnalysisBuildOptions(retries=0),
        generator=DeterministicGenerator([]),
    )
    first, second = cut.chapters
    assert first.summary == "向山帮助尤基修好机器。"
    assert second.chapter_title == "第二章 之后"
    assert second.summary == ""
    assert [event.description for event in second.events] == ["组织A发难"]
    assert [mention.name for mention in second.characters] == ["组织A"]

    # Line 3 leaves only the first chunk of chapter one; chapter two disappears.
    early = build_analysis(
        config,
        "books",
        progress=ProgressLimit(max_line=3),
        options=AnalysisBuildOptions(retries=0),
        generator=DeterministicGenerator([]),
    )
    assert [chapter.chapter_title for chapter in early.chapters] == ["第一章 开始"]
    assert early.chapters[0].summary == ""
    assert [event.description for event in early.chapters[0].events] == ["向山施以援手"]
