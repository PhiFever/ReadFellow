import pytest
from sqlalchemy import func, select
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
    build_analysis,
    import_json,
)
from readfellow.artifacts import ArtifactStore
from readfellow.chunking import CHUNKER_VERSION, chunk_document
from readfellow.config import PathConfig, ReadFellowConfig
from readfellow.models import ChapterAnalysis, IndexManifest
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


def test_legacy_import_is_lossless_idempotent_and_rejects_bad_provenance(
    tmp_path, artifacts
):
    config, manifest, chunks = workspace(tmp_path, artifacts)
    write_manifest(
        metadata_dir=config.paths.metadata_dir,
        collection="books",
        manifest=manifest,
        chunks=chunks,
    )
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
    path = config.paths.metadata_dir / "books" / "analysis.json"
    write_analysis(path, legacy_analysis)
    original_bytes = path.read_bytes()
    imported = import_json(config, "books", artifacts=artifacts)
    assert imported.analysis_run_id != analysis_result.run_id
    assert artifacts.read_run(imported.analysis_run_id).document == legacy_analysis
    repeated = import_json(config, "books", artifacts=artifacts)
    assert repeated.skipped and repeated.analysis_run_id == imported.analysis_run_id
    assert path.read_bytes() == original_bytes
    with artifacts.engine.connect() as connection:
        count = connection.scalar(select(func.count()).select_from(db.runs))
    legacy_analysis.chapters[0].characters[0].evidence = "这句话不在原文中"
    write_analysis(path, legacy_analysis)
    with pytest.raises(ValueError, match="provenance/evidence mismatch"):
        import_json(config, "books", artifacts=artifacts)
    with artifacts.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(db.runs)) == count


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
