from pathlib import Path

import pytest
from sqlalchemy import event, func, select, text

from readfellow import artifact_schema as db
from readfellow.app import (
    GraphBuildOptions,
    AnalysisBuildOptions,
    build_analysis,
    build_graph,
    import_json,
    query_graph,
)
from readfellow.artifacts import ArtifactStore
from readfellow.chunking import CHUNKER_VERSION, chunk_document
from readfellow.config import PathConfig, ReadFellowConfig
from readfellow.graph import write_graph
from readfellow.analysis import write_analysis
from readfellow.models import IndexManifest
from readfellow.store import write_manifest
from test_app import DeterministicGenerator


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
