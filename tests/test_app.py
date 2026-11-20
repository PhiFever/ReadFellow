import pytest

from readfellow import app
from readfellow.app import IndexDocumentOptions, ProgressLimit
from readfellow.library import SqliteChunkStore


def test_semantic_search_is_configured_application_workflow(book, embedder):
    config, source = book
    config.ollama.base_url = "http://localhost:9999"
    config.ollama.keep_alive = "5m"
    config.ollama.embedding_model = "manifest-embedding"
    app.index_document(config, source, "books")
    app.set_progress(config, "books")
    config.ollama.embedding_model = "other"
    result = app.semantic_search(config, "修理", "books", top_k=1)
    assert embedder.settings[-1] == {
        "base_url": "http://localhost:9999",
        "model": "manifest-embedding",
        "keep_alive": "5m",
    }
    assert embedder.queries[-1] == "修理"
    assert result.evidence[0].score == 1.0
    assert result.evidence[0].chunk_index == 0


def test_reindexing_an_unchanged_document_embeds_nothing(book, embedder):
    config, source = book
    app.index_document(config, source, "books")
    embedder.batches.clear()
    result = app.index_document(config, source, "books")
    assert (result.inserted, result.skipped) == (0, 2)
    assert embedder.batches == []


def test_status_reports_a_short_index_and_resume_embeds_only_missing(book, embedder):
    config, source = book

    def interrupt(event):
        if event.stage == "batch":
            raise InterruptedError

    with pytest.raises(InterruptedError):
        app.index_document(
            config,
            source,
            "books",
            options=IndexDocumentOptions(batch_size=1),
            on_progress=interrupt,
        )
    report = app.collection_status(config, "books").index
    assert (report.stored_doc_count, report.expected_doc_count, report.is_complete) == (
        1,
        2,
        False,
    )
    embedder.batches.clear()
    result = app.index_document(config, source, "books")
    assert (result.inserted, result.skipped) == (1, 1)
    assert embedder.batches == [["第二章 之后\n尤基发现了秘密。\n"]]
    assert app.collection_status(config, "books").index.is_complete


@pytest.mark.parametrize("change", ["chunk_chars", "source"])
def test_identity_change_requires_rebuild_without_writing(book, embedder, change):
    config, source = book
    result = app.index_document(config, source, "books")
    before = result.collection_path.read_bytes()
    if change == "chunk_chars":
        config.indexing.chunk_chars += 1
    else:
        other = source.with_name("other.txt")
        other.write_bytes(source.read_bytes())
        source = other
    embedder.batches.clear()
    with pytest.raises(RuntimeError, match="--rebuild"):
        app.index_document(config, source, "books")
    assert not embedder.batches
    assert result.collection_path.read_bytes() == before


def test_front_matter_keeps_chunk_id_and_has_no_chapter(book):
    config, source = book
    source.write_bytes(b"\r\n------------\r\n\r\n" + source.read_bytes())
    result = app.index_document(config, source, "books")
    with SqliteChunkStore.open(result.collection_path) as store:
        rows = store._db.execute(
            "SELECT id, chapter_ordinal FROM chunks ORDER BY chunk_index"
        ).fetchall()
        assert rows[0][0].endswith("_000000")
        assert [row[1] for row in rows] == [None, 1, 2]


def test_fts_search_returns_source_grounded_evidence(readable_book):
    config, source = readable_book
    result = app.fts_search(
        config, "尤基", "books", progress=ProgressLimit(max_chunk_index=0)
    )
    (hit,) = result.evidence
    assert (hit.retrieval_mode, hit.source_path, hit.line_start, hit.line_end) == (
        "fts",
        str(source),
        1,
        2,
    )


def test_fetch_chunk_does_not_expose_text_outside_progress(readable_book):
    config, _ = readable_book
    (hit,) = app.fts_search(config, "秘密", "books").evidence
    result = app.fetch_chunk(
        config, hit.chunk_id, "books", progress=ProgressLimit(max_line=2)
    )
    assert result.status == "outside_progress"
    assert result.evidence is None


def test_hybrid_ranks_multi_channel_agreement_above_a_single_channel_top_hit(
    readable_book,
):
    config, _ = readable_book
    result = app.hybrid_search(config, "秘密", "books")
    assert result.evidence[0].chunk_index == 1
    assert [(m.mode, m.rank) for m in result.evidence[0].matches] == [
        ("vector", 2),
        ("fts", 1),
    ]


def test_hybrid_reports_channel_candidates(readable_book):
    config, _ = readable_book
    result = app.hybrid_search(config, "秘密", "books", top_k=1)
    assert [(c.mode, c.candidates) for c in result.channels] == [
        ("vector", 2),
        ("fts", 1),
    ]
    assert len(result.evidence) == 1


def test_hybrid_fails_when_the_embedding_service_is_down(
    readable_book, embedder, monkeypatch
):
    config, _ = readable_book

    def failed(self, text):
        raise RuntimeError("ollama is unreachable")

    monkeypatch.setattr(embedder, "embed_one", failed)
    with pytest.raises(RuntimeError, match="ollama is unreachable"):
        app.hybrid_search(config, "秘密", "books")
