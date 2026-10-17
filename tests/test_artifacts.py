from pathlib import Path

import pytest
from sqlalchemy import event, func, select, text
from test_app import DeterministicGenerator

from readfellow import artifact_schema as db
from readfellow.analysis import (
    ChapterGroup,
    empty_analysis,
    merge_chapter,
    write_analysis,
)
from readfellow.app import (
    AnalysisBuildOptions,
    GraphBuildOptions,
    build_analysis,
    build_graph,
    import_json,
    query_graph,
)
from readfellow.artifacts import ArtifactStore
from readfellow.chunking import CHUNKER_VERSION, chunk_document
from readfellow.config import PathConfig, ReadFellowConfig
from readfellow.graph import annotate_chunks, write_graph
from readfellow.models import ChapterAnalysis, IndexManifest
from readfellow.progress import build_progress_filter
from readfellow.store import write_manifest


def workspace(tmp_path, artifacts):
    source = tmp_path / "novel.txt"
    source.write_text(
        "第一章 开始\n\n甲帮助了乙。\n\n第二章 之后\n\n乙帮助了丙。\n", encoding="utf-8"
    )
    chunks = chunk_document(
        source, source_path=str(source), target_chars=22, overlap_chars=0
    )
    manifest = IndexManifest(
        collection="books",
        collection_path="indexes/books",
        source_path=str(source),
        model="test",
        embedding_dimension=2,
        chunk_count=len(chunks),
        chunk_chars=22,
        overlap_chars=0,
        chunker_version=CHUNKER_VERSION,
    )
    artifacts.write_source(manifest, chunks)
    config = ReadFellowConfig(paths=PathConfig(metadata_dir=tmp_path / "metadata"))
    return config, manifest, chunks


def generator():
    return DeterministicGenerator(
        [
            {
                "entities": [{"name": "甲", "type": "人物", "evidence": "甲帮助了乙"}],
                "relations": [
                    {
                        "subject": "甲",
                        "relation": "帮助",
                        "object": "乙",
                        "evidence": "甲帮助了乙",
                    }
                ],
            },
            {
                "entities": [{"name": "丙", "type": "人物", "evidence": "乙帮助了丙"}],
                "relations": [
                    {
                        "subject": "乙",
                        "relation": "帮助",
                        "object": "丙",
                        "evidence": "乙帮助了丙",
                    }
                ],
            },
        ]
    )


def test_latest_run_history_resume_and_relational_evidence(tmp_path, artifacts):
    config, _, chunks = workspace(tmp_path, artifacts)
    assert len(chunks) == 2
    first = build_graph(
        config,
        "books",
        options=GraphBuildOptions(retries=0),
        generator=generator(),
        artifacts=artifacts,
    )
    first_document = artifacts.read_run(first.run_id).document.model_dump(mode="json")
    # A real SQL join traverses two edges within a run; stub endpoints remain navigable.
    with artifacts.engine.connect() as connection:
        paths = connection.execute(
            text("""
            SELECT a.subject, a.object, b.object, c.text
            FROM relations a JOIN relations b
              ON b.run_id = a.run_id AND b.subject_position = a.object_position
            JOIN chunks c ON c.source_version_id = b.source_version_id AND c.id = b.chunk_id
            WHERE a.run_id = :run AND a.subject = :name
        """),
            {"run": first.run_id, "name": "甲"},
        ).all()
    assert len(paths) == 1
    assert paths[0][:3] == ("甲", "乙", "丙")
    assert "乙帮助了丙" in paths[0][3]
    second = build_graph(
        config,
        "books",
        options=GraphBuildOptions(rebuild=True, limit=1, retries=0),
        generator=generator(),
        artifacts=artifacts,
    )
    assert second.run_id > first.run_id
    assert query_graph(config, "丙", "books", artifacts=artifacts).evidence == []
    assert (
        artifacts.read_run(first.run_id).document.model_dump(mode="json")
        == first_document
    )
    continuation = DeterministicGenerator(
        [
            {
                "entities": [],
                "relations": [
                    {
                        "subject": "乙",
                        "relation": "帮助",
                        "object": "丙",
                        "evidence": "乙帮助了丙",
                    }
                ],
            }
        ]
    )
    resumed = build_graph(
        config,
        "books",
        options=GraphBuildOptions(retries=0),
        generator=continuation,
        artifacts=artifacts,
    )
    assert resumed.run_id == second.run_id
    assert len(continuation.prompts) == 1
    fresh = ArtifactStore(artifacts.engine)
    assert len(fresh.latest("books", "graph").document.extractions) == 2
    assert not config.paths.metadata_dir.exists()


def test_failed_rebuild_publishes_latest_empty_run(tmp_path, artifacts):
    config, _, _ = workspace(tmp_path, artifacts)
    previous = build_graph(
        config,
        "books",
        options=GraphBuildOptions(retries=0),
        generator=generator(),
        artifacts=artifacts,
    )
    failed = build_graph(
        config,
        "books",
        options=GraphBuildOptions(retries=0, rebuild=True),
        generator=DeterministicGenerator([]),
        artifacts=artifacts,
    )
    result = query_graph(config, "甲", "books", artifacts=artifacts)
    assert result.run_id == failed.run_id > previous.run_id
    assert result.processed == 0
    assert result.evidence == []
    assert len(artifacts.read_run(previous.run_id).document.extractions) == 2


def test_unit_rollback_keeps_completed_work_and_can_resume(tmp_path, artifacts):
    config, _, _ = workspace(tmp_path, artifacts)
    first = build_graph(
        config,
        "books",
        options=GraphBuildOptions(retries=0, limit=1),
        generator=generator(),
        artifacts=artifacts,
    )
    before = artifacts.read_run(first.run_id).document.model_dump(mode="json")

    def fail_record(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO extractions"):
            raise RuntimeError("simulated unit commit failure")

    event.listen(artifacts.engine, "before_cursor_execute", fail_record)
    try:
        with pytest.raises(RuntimeError, match="simulated unit commit failure"):
            build_graph(
                config,
                "books",
                options=GraphBuildOptions(retries=0),
                generator=DeterministicGenerator([{"entities": [], "relations": []}]),
                artifacts=artifacts,
            )
    finally:
        event.remove(artifacts.engine, "before_cursor_execute", fail_record)
    after = ArtifactStore(artifacts.engine).read_run(first.run_id).document
    assert after.extractions == artifacts.read_run(first.run_id).document.extractions
    assert after.model_dump(mode="json")["relations"] == before["relations"]
    assert len(after.extractions) == 1
    resumed = build_graph(
        config,
        "books",
        options=GraphBuildOptions(retries=0),
        generator=DeterministicGenerator([{"entities": [], "relations": []}]),
        artifacts=artifacts,
    )
    assert resumed.run_id == first.run_id
    assert resumed.processed_chunk_count == 2


def test_rechunk_keeps_history_bound_to_original_source_version(tmp_path, artifacts):
    config, manifest, chunks = workspace(tmp_path, artifacts)
    first = build_graph(
        config,
        "books",
        options=GraphBuildOptions(retries=0),
        generator=generator(),
        artifacts=artifacts,
    )
    old_run = artifacts.read_run(first.run_id)
    source = Path(manifest.source_path)
    changed = chunk_document(
        source, source_path=str(source), target_chars=5, overlap_chars=0
    )
    assert changed[0].id == chunks[0].id and changed[0].text != chunks[0].text
    new_source_id = artifacts.write_source(
        manifest.model_copy(update={"chunk_count": len(changed), "chunk_chars": 5}),
        changed,
    )
    assert new_source_id != old_run.source_version_id
    with pytest.raises(RuntimeError, match="stale"):
        query_graph(config, "甲", "books", artifacts=artifacts)
    with artifacts.engine.connect() as connection:
        stored_text = connection.execute(
            select(db.chunks.c.text).where(
                db.chunks.c.source_version_id == old_run.source_version_id,
                db.chunks.c.id == chunks[0].id,
            )
        ).scalar_one()
    assert stored_text == chunks[0].text
    assert (
        artifacts.read_run(first.run_id).source_version_id == old_run.source_version_id
    )


def test_legacy_import_is_lossless_idempotent_and_rejects_bad_provenance(
    tmp_path, artifacts
):
    config, manifest, chunks = workspace(tmp_path, artifacts)
    result = build_graph(
        config,
        "books",
        options=GraphBuildOptions(retries=0),
        generator=generator(),
        artifacts=artifacts,
    )
    legacy = artifacts.read_run(result.run_id).document
    legacy.prompt_version = "historical-prompt"
    legacy.unanchored_count = None
    for record in legacy.extractions:
        record.unanchored_count = None
    write_manifest(
        metadata_dir=config.paths.metadata_dir,
        collection="books",
        manifest=manifest,
        chunks=chunks,
    )
    path = config.paths.metadata_dir / "books" / "graph.json"
    write_graph(path, legacy)
    analysis_result = build_analysis(
        config,
        "books",
        options=AnalysisBuildOptions(retries=0),
        generator=DeterministicGenerator(
            [
                {
                    "summary": "甲帮助乙",
                    "characters": [{"name": "甲", "evidence": "甲帮助了乙"}],
                    "events": [],
                }
            ]
        ),
        artifacts=artifacts,
    )
    legacy_analysis = artifacts.read_run(analysis_result.run_id).document
    write_analysis(
        config.paths.metadata_dir / "books" / "analysis.json", legacy_analysis
    )
    original_bytes = path.read_bytes()
    imported = import_json(config, "books", artifacts=artifacts)
    assert imported.graph_run_id != result.run_id
    assert artifacts.read_run(imported.graph_run_id).document.model_dump(
        mode="json"
    ) == legacy.model_dump(mode="json")
    assert artifacts.read_run(imported.analysis_run_id).document == legacy_analysis
    repeated = import_json(config, "books", artifacts=artifacts)
    assert repeated.skipped and repeated.graph_run_id == imported.graph_run_id
    assert path.read_bytes() == original_bytes
    with artifacts.engine.connect() as connection:
        count = connection.scalar(select(func.count()).select_from(db.runs))
    legacy.relations[0].evidence = "这句话不在原文中"
    write_graph(path, legacy)
    with pytest.raises(ValueError, match="provenance/evidence mismatch"):
        import_json(config, "books", artifacts=artifacts)
    with artifacts.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(db.runs)) == count


def test_graph_slice_annotates_like_the_whole_graph(tmp_path, artifacts):
    source = tmp_path / "novel.txt"
    source.write_text(
        "第一章 开始\n\n甲帮助了乙。\n\n第二章 之后\n\n乙帮助了丙。\n\n"
        "第三章 最后\n\n甲认识丙。\n",
        encoding="utf-8",
    )
    chunks = chunk_document(
        source, source_path=str(source), target_chars=22, overlap_chars=0
    )
    assert len(chunks) == 3
    artifacts.write_source(
        IndexManifest(
            collection="books",
            collection_path="indexes/books",
            source_path=str(source),
            model="test",
            embedding_dimension=2,
            chunk_count=len(chunks),
            chunk_chars=22,
            overlap_chars=0,
            chunker_version=CHUNKER_VERSION,
        ),
        chunks,
    )
    config = ReadFellowConfig(paths=PathConfig(metadata_dir=tmp_path / "metadata"))

    def relation(subject, object_, evidence):
        return {
            "subject": subject,
            "relation": "帮助",
            "object": object_,
            "evidence": evidence,
        }

    # 乙 is only an endpoint in the first chunk and is declared in the second;
    # 甲 and 丙 span two chunks each, so ranking needs their rows elsewhere.
    build_graph(
        config,
        "books",
        options=GraphBuildOptions(retries=0),
        generator=DeterministicGenerator(
            [
                {
                    "entities": [{"name": "甲", "type": "人物", "evidence": "甲"}],
                    "relations": [relation("甲", "乙", "甲帮助了乙")],
                },
                {
                    "entities": [
                        {"name": "乙", "type": "人物", "evidence": "乙帮助了丙"},
                        {"name": "丙", "type": "人物", "evidence": "丙"},
                    ],
                    "relations": [relation("乙", "丙", "乙帮助了丙")],
                },
                {
                    "entities": [
                        {"name": "甲", "type": "人物", "evidence": "甲认识丙"},
                        {"name": "丙", "type": "人物", "evidence": "丙"},
                    ],
                    "relations": [relation("甲", "丙", "甲认识丙")],
                },
            ]
        ),
        artifacts=artifacts,
    )
    wanted = [chunks[0].id, chunks[2].id]
    whole = artifacts.latest("books", "graph").document
    part = artifacts.read_graph_slice("books", wanted).document

    by_name = {entity.name: entity for entity in whole.entities}
    assert [entity.name for entity in part.entities] == ["丙", "乙", "甲"]
    assert all(entity == by_name[entity.name] for entity in part.entities)
    assert part.relations == [r for r in whole.relations if r.chunk_id in wanted]
    assert part.extractions == whole.extractions
    for progress in (None, build_progress_filter(max_line=chunks[1].line_end)):
        expected = annotate_chunks(whole, wanted, progress=progress)
        assert expected
        assert annotate_chunks(part, wanted, progress=progress) == expected


def test_a_second_writer_to_one_run_fails_instead_of_corrupting_it(tmp_path, artifacts):
    _, manifest, chunks = workspace(tmp_path, artifacts)
    source_id, _, _ = artifacts.read_source("books")
    # Two stores stand for two processes that each resumed the latest run.
    a, b = ArtifactStore(artifacts.engine), ArtifactStore(artifacts.engine)
    runs = [
        store.prepare(
            source_id,
            empty_analysis(collection="books", manifest=manifest),
            stale=lambda _: None,
            rebuild=False,
        )[0]
        for store in (a, b)
    ]
    assert runs[0].id == runs[1].id

    def analyze(store, run, index, chunk):
        merge_chapter(
            run.document,
            ChapterAnalysis(
                chapter_index=index,
                chapter_title=chunk.chapter,
                source_path=chunk.source_path,
                line_start=chunk.line_start,
                line_end=chunk.line_end,
                chunk_ids=[chunk.id],
                summary="梗概",
            ),
            ChapterGroup(index=index, title=chunk.chapter, chunks=(chunk,)),
        )
        store.save(run)

    analyze(a, runs[0], 1, chunks[0])
    # Disjoint chapters collide on no key, so this used to succeed and leave
    # two chapters behind a run that counted one.
    with pytest.raises(RuntimeError, match="another process"):
        analyze(b, runs[1], 2, chunks[1])

    stored = ArtifactStore(artifacts.engine).latest("books", "analysis").document
    assert [chapter.chapter_index for chapter in stored.chapters] == [1]
    assert stored.processed_chapter_count == 1
