from readfellow.analysis import (
    ChapterGroup,
    build_chapter_prompt,
    parse_chapter_analysis,
)
from readfellow.chunking import chunk_document


def test_analysis_drops_evidence_not_found_verbatim_in_chapter(tmp_path):
    source = tmp_path / "novel.txt"
    source.write_text(
        "第一章 开始\n向山帮助了尤基。\n尤基修好了机器。\n", encoding="utf-8"
    )
    chunks = chunk_document(source, source_path=str(source))
    group = ChapterGroup(1, "第一章 开始", tuple(chunks))
    assert "prompt_version: chapter-analysis-v1" in build_chapter_prompt(group)
    payload = {
        "summary": "梗概",
        "characters": [
            {"name": "向山", "evidence": "向山打败了尤基"},
            {"name": "尤基", "evidence": "尤基修好了机器"},
        ],
        "events": [],
    }
    chapter = parse_chapter_analysis(payload, group)
    assert [mention.name for mention in chapter.characters] == ["尤基"]
    assert chapter.rejected_count == 1
    assert chapter.unanchored_count == 1
