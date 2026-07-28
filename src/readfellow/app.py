from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .analysis import (
    ANALYSIS_RESPONSE_SCHEMA,
    ChapterGroup,
    analysis_path,
    analysis_staleness_reason,
    build_chapter_prompt,
    chapter_body,
    chapter_char_budget,
    complete_chapters,
    empty_analysis,
    filter_chapter,
    group_chapters,
    merge_chapter,
    parse_chapter_analysis,
    processed_chapter_keys,
    read_analysis,
    update_analysis_metadata,
    write_analysis,
)
from .chunking import CHUNKER_VERSION, chunk_document, sha256_file
from .config import DerivationConfig, ReadFellowConfig
from .derivation import (
    JsonGenerator,
    derivation_status,
    generate_with_retry,
    load_or_reset,
)
from .graph import (
    GRAPH_RESPONSE_SCHEMA,
    GraphDiagnostics,
    annotate_chunks,
    build_extraction_prompt,
    empty_graph,
    graph_diagnostics,
    graph_path,
    graph_staleness_reason,
    merge_extraction,
    parse_graph_extraction,
    processed_chunk_ids,
    read_graph,
    update_graph_metadata,
    write_graph,
)
from .graph import (
    query_graph as query_knowledge_graph,
)
from .models import (
    ChapterAnalysis,
    Chunk,
    DerivationSettings,
    Evidence,
    EvidenceGraphContext,
    EvidenceMatch,
    GraphQueryResult,
    IndexManifest,
    KnowledgeGraph,
    ProgressFilter,
)
from .ollama import OllamaEmbedder, OllamaGenerator
from .progress import build_progress_filter, source_from_manifest
from .store import (
    ChunkStore,
    ZvecChunkStore,
    collection_path,
    read_chunks,
    read_manifest,
    write_manifest,
)


RRF_K = 60
FAN_OUT_MULTIPLIER = 10

ChannelMode = Literal["vector", "fts"]


@dataclass(frozen=True)
class ProgressLimit:
    max_chapter: int | None = None
    max_line: int | None = None
    max_chunk_index: int | None = None


@dataclass(frozen=True)
class IndexDocumentOptions:
    chunk_chars: int | None = None
    overlap_chars: int | None = None
    batch_size: int | None = None
    limit: int = 0
    rebuild: bool = False
    optimize: bool = True


@dataclass(frozen=True)
class IndexProgressEvent:
    stage: str
    model: str = ""
    processed: int = 0
    total: int = 0
    inserted: int = 0
    skipped: int = 0
    rate: float = 0.0
    skipped_existing: bool = False


@dataclass(frozen=True)
class IndexDocumentResult:
    collection: str
    chunk_count: int
    inserted: int
    skipped: int
    collection_path: Path


@dataclass(frozen=True)
class SearchResult:
    progress: ProgressFilter
    evidence: list[Evidence]


@dataclass(frozen=True)
class FetchChunkResult:
    progress: ProgressFilter
    status: Literal["found", "not_found", "outside_progress"]
    evidence: Evidence | None


@dataclass(frozen=True)
class GraphBuildOptions:
    limit: int = 0
    llm_model: str | None = None
    num_predict: int | None = None
    retries: int | None = None
    rebuild: bool = False


@dataclass(frozen=True)
class GraphBuildEvent:
    stage: str
    progress: ProgressFilter | None = None
    index: int = 0
    total: int = 0
    chunk_id: str = ""
    attempt: int = 0
    retries: int = 0
    error: str = ""
    entity_count: int = 0
    relation_count: int = 0
    rejected_count: int = 0
    unanchored_count: int = 0


@dataclass(frozen=True)
class GraphBuildResult:
    collection: str
    graph_path: Path
    status: str
    selected_chunk_count: int
    entity_count: int
    relation_count: int
    failed_chunk_count: int = 0


@dataclass(frozen=True)
class GraphSearchResult:
    progress: ProgressFilter
    evidence: list[Evidence]


@dataclass(frozen=True)
class ChannelStatus:
    mode: ChannelMode
    candidates: int


@dataclass(frozen=True)
class GraphAnnotationStatus:
    annotated: int
    skipped_reason: str | None = None


@dataclass(frozen=True)
class HybridSearchResult:
    progress: ProgressFilter
    channels: list[ChannelStatus]
    graph_annotation: GraphAnnotationStatus
    evidence: list[Evidence]


@dataclass(frozen=True)
class IndexReport:
    collection_path: Path
    expected_doc_count: int
    stored_doc_count: int
    index_completeness: dict[str, float]
    error: str | None = None

    @property
    def is_complete(self) -> bool:
        """Whether the collection really holds what the metadata promises.

        Indexing publishes the metadata before the embeddings, so a run killed
        halfway leaves a manifest claiming chunks the collection never got.
        """
        return (
            self.error is None
            and self.stored_doc_count == self.expected_doc_count
            and all(share >= 1.0 for share in self.index_completeness.values())
        )


@dataclass(frozen=True)
class DerivationReport:
    path: Path
    exists: bool
    processed: int
    total: int
    rejected_count: int = 0
    unanchored_count: int | None = None
    stale_reason: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class CollectionStatus:
    collection: str
    manifest: IndexManifest
    source_error: str | None
    index: IndexReport
    graph: DerivationReport
    analysis: DerivationReport
    diagnostics: GraphDiagnostics | None


@dataclass(frozen=True)
class AnalysisBuildOptions:
    llm_model: str | None = None
    num_predict: int | None = None
    retries: int | None = None
    rebuild: bool = False


@dataclass(frozen=True)
class AnalysisBuildEvent:
    stage: str
    progress: ProgressFilter | None = None
    index: int = 0
    total: int = 0
    chapter_title: str = ""
    attempt: int = 0
    retries: int = 0
    error: str = ""
    reason: str = ""
    character_count: int = 0
    event_count: int = 0
    rejected_count: int = 0
    unanchored_count: int | None = None


@dataclass(frozen=True)
class AnalysisBuildResult:
    collection: str
    analysis_path: Path
    status: str
    progress: ProgressFilter
    chapters: list[ChapterAnalysis]
    skipped: list[tuple[str, str]]


def index_document(
    config: ReadFellowConfig,
    source: Path,
    collection: str,
    *,
    options: IndexDocumentOptions | None = None,
    on_progress: Callable[[IndexProgressEvent], None] | None = None,
    store: ChunkStore | None = None,
) -> IndexDocumentResult:
    options = options or IndexDocumentOptions()
    chunk_chars = options.chunk_chars or config.indexing.chunk_chars
    overlap_chars = options.overlap_chars or config.indexing.overlap_chars
    batch_size = options.batch_size or config.indexing.batch_size

    relative_source = _relative_source_path(source)
    chunks = chunk_document(
        source,
        source_path=relative_source,
        target_chars=chunk_chars,
        overlap_chars=overlap_chars,
    )
    if options.limit:
        chunks = chunks[: options.limit]
    if not chunks:
        raise ValueError("no chunks produced")

    embedder = OllamaEmbedder(
        base_url=config.ollama.base_url,
        model=config.ollama.embedding_model,
        keep_alive=config.ollama.keep_alive,
    )
    _emit(
        on_progress,
        IndexProgressEvent(stage="probe", model=config.ollama.embedding_model),
    )
    probe_vector = embedder.embed_one(chunks[0].text)
    dimension = len(probe_vector)

    store = store or ZvecChunkStore.open_for_write(
        index_dir=config.paths.index_dir,
        metadata_dir=config.paths.metadata_dir,
        collection=collection,
        dimension=dimension,
        rebuild=options.rebuild,
    )

    manifest = IndexManifest(
        collection=collection,
        collection_path=str(collection_path(config.paths.index_dir, collection)),
        source_path=relative_source,
        model=config.ollama.embedding_model,
        embedding_dimension=dimension,
        chunk_count=len(chunks),
        chunk_chars=chunk_chars,
        overlap_chars=overlap_chars,
        chunker_version=CHUNKER_VERSION,
    )
    write_manifest(
        metadata_dir=config.paths.metadata_dir,
        collection=collection,
        manifest=manifest,
        chunks=chunks,
    )

    start = time.monotonic()
    inserted = 0
    skipped = 0
    for offset in range(0, len(chunks), batch_size):
        batch = chunks[offset : offset + batch_size]
        outcome = store.upsert(
            batch,
            model=config.ollama.embedding_model,
            embed=embedder.embed,
        )
        inserted += outcome.written
        skipped += outcome.skipped
        elapsed = time.monotonic() - start
        _emit(
            on_progress,
            IndexProgressEvent(
                stage="batch",
                processed=offset + len(batch),
                total=len(chunks),
                inserted=inserted,
                skipped=skipped,
                rate=inserted / elapsed if elapsed else 0,
                skipped_existing=outcome.written == 0,
            ),
        )

    if options.optimize:
        _emit(on_progress, IndexProgressEvent(stage="optimize"))
    store.commit(optimize=options.optimize)
    return IndexDocumentResult(
        collection=collection,
        chunk_count=len(chunks),
        inserted=inserted,
        skipped=skipped,
        collection_path=collection_path(config.paths.index_dir, collection),
    )


def semantic_search(
    config: ReadFellowConfig,
    query: str,
    collection: str,
    *,
    top_k: int | None = None,
    progress: ProgressLimit | None = None,
    store: ChunkStore | None = None,
) -> SearchResult:
    manifest = read_manifest(
        metadata_dir=config.paths.metadata_dir, collection=collection
    )
    store = store or _open_store(config, collection)
    progress_filter = progress_filter_from_limit(progress, manifest=manifest)
    embedder = OllamaEmbedder(
        base_url=config.ollama.base_url,
        model=manifest.model or config.ollama.embedding_model,
        keep_alive=config.ollama.keep_alive,
    )
    vector = embedder.embed_one(query)
    return SearchResult(
        progress=progress_filter,
        evidence=store.search_vector(
            vector,
            top_k=top_k or config.search.top_k,
            progress=progress_filter,
        ),
    )


def fts_search(
    config: ReadFellowConfig,
    query: str,
    collection: str,
    *,
    top_k: int | None = None,
    progress: ProgressLimit | None = None,
    store: ChunkStore | None = None,
) -> SearchResult:
    manifest = read_manifest(
        metadata_dir=config.paths.metadata_dir, collection=collection
    )
    store = store or _open_store(config, collection)
    progress_filter = progress_filter_from_limit(progress, manifest=manifest)
    return SearchResult(
        progress=progress_filter,
        evidence=store.search_fts(
            query,
            top_k=top_k or config.search.top_k,
            progress=progress_filter,
        ),
    )


def fetch_chunk(
    config: ReadFellowConfig,
    chunk_id: str,
    collection: str,
    *,
    progress: ProgressLimit | None = None,
    store: ChunkStore | None = None,
) -> FetchChunkResult:
    manifest = read_manifest(
        metadata_dir=config.paths.metadata_dir, collection=collection
    )
    store = store or _open_store(config, collection)
    progress_filter = progress_filter_from_limit(progress, manifest=manifest)
    evidence = store.fetch(chunk_id)
    if evidence is None:
        return FetchChunkResult(
            progress=progress_filter,
            status="not_found",
            evidence=None,
        )
    if not progress_filter.allows(evidence):
        return FetchChunkResult(
            progress=progress_filter,
            status="outside_progress",
            evidence=None,
        )
    return FetchChunkResult(
        progress=progress_filter,
        status="found",
        evidence=evidence,
    )


def build_graph(
    config: ReadFellowConfig,
    collection: str,
    *,
    progress: ProgressLimit | None = None,
    options: GraphBuildOptions | None = None,
    on_progress: Callable[[GraphBuildEvent], None] | None = None,
    generator: JsonGenerator | None = None,
) -> GraphBuildResult:
    options = options or GraphBuildOptions()
    manifest, all_chunks, progress_filter = _load_indexed_source(
        config, collection, progress
    )
    chunks = [chunk for chunk in all_chunks if progress_filter.allows(chunk)]
    if options.limit:
        chunks = chunks[: options.limit]

    path = graph_path(config.paths.metadata_dir, collection)
    llm_model, extraction_settings = _generation_plan(
        config,
        config.graph,
        llm_model=options.llm_model,
        num_predict=options.num_predict,
        retries=options.retries,
    )
    graph, rebuilt = load_or_reset(
        path,
        empty_graph(
            collection=collection,
            manifest=manifest,
            llm_model=llm_model,
            extraction_settings=extraction_settings,
        ),
        read=read_graph,
        stale=lambda stored: graph_staleness_reason(
            stored,
            all_chunks,
            collection=collection,
            source_path=manifest.source_path,
            llm_model=llm_model,
            extraction_settings=extraction_settings,
        ),
        rebuild=options.rebuild,
    )

    processed = processed_chunk_ids(graph)
    pending = [chunk for chunk in chunks if chunk.id not in processed]
    update_graph_metadata(
        graph,
        collection=collection,
        manifest=manifest,
        llm_model=llm_model,
        extraction_settings=extraction_settings,
        progress=progress_filter,
        selected_chunk_count=len(chunks),
    )
    _emit(
        on_progress,
        GraphBuildEvent(stage="selected", progress=progress_filter),
    )

    if not pending:
        write_graph(path, graph)
        return GraphBuildResult(
            collection=collection,
            graph_path=path,
            status=derivation_status(selected=len(chunks), pending=0, rebuilt=rebuilt),
            selected_chunk_count=len(chunks),
            entity_count=graph.entity_count,
            relation_count=graph.relation_count,
        )

    if generator is None:
        generator = OllamaGenerator(
            base_url=config.ollama.base_url,
            model=llm_model,
            keep_alive=config.ollama.keep_alive,
            settings=extraction_settings,
        )

    failed_chunk_count = 0
    for index, chunk in enumerate(pending, start=1):
        chunk_id = chunk.id
        _emit(
            on_progress,
            GraphBuildEvent(
                stage="extracting",
                index=index,
                total=len(pending),
                chunk_id=chunk_id,
            ),
        )
        try:
            extraction = generate_with_retry(
                generator,
                build_extraction_prompt(chunk),
                lambda raw: parse_graph_extraction(raw, chunk),
                schema=GRAPH_RESPONSE_SCHEMA,
                retries=extraction_settings.retries,
                label=f"failed to extract graph for chunk {chunk_id}",
                on_retry=lambda attempt, exc: _emit(
                    on_progress,
                    GraphBuildEvent(
                        stage="retry",
                        index=index,
                        total=len(pending),
                        chunk_id=chunk_id,
                        attempt=attempt,
                        retries=extraction_settings.retries,
                        error=str(exc),
                    ),
                ),
            )
        except RuntimeError as exc:
            # One chunk the model never answered usably costs that chunk. A book
            # is thousands of them, and the run has already paid for every one
            # before it. The chunk stays unprocessed rather than being recorded
            # as empty, so a later run picks it up again — with sampling, a
            # second run is a genuinely different attempt.
            failed_chunk_count += 1
            _emit(
                on_progress,
                GraphBuildEvent(
                    stage="failed",
                    index=index,
                    total=len(pending),
                    chunk_id=chunk_id,
                    error=str(exc),
                ),
            )
            continue

        merge_extraction(graph, extraction, chunk)
        update_graph_metadata(
            graph,
            collection=collection,
            manifest=manifest,
            llm_model=llm_model,
            extraction_settings=extraction_settings,
            progress=progress_filter,
            selected_chunk_count=len(chunks),
        )
        write_graph(path, graph)
        _emit(
            on_progress,
            GraphBuildEvent(
                stage="extracted",
                index=index,
                total=len(pending),
                chunk_id=chunk_id,
                entity_count=len(extraction.entities),
                relation_count=len(extraction.relations),
                rejected_count=extraction.rejected_count,
                unanchored_count=extraction.unanchored_count,
            ),
        )

    return GraphBuildResult(
        collection=collection,
        graph_path=path,
        status=derivation_status(
            selected=len(chunks), pending=len(pending), rebuilt=rebuilt
        ),
        selected_chunk_count=len(chunks),
        entity_count=graph.entity_count,
        relation_count=graph.relation_count,
        failed_chunk_count=failed_chunk_count,
    )


def build_analysis(
    config: ReadFellowConfig,
    collection: str,
    *,
    progress: ProgressLimit | None = None,
    options: AnalysisBuildOptions | None = None,
    on_progress: Callable[[AnalysisBuildEvent], None] | None = None,
    generator: JsonGenerator | None = None,
) -> AnalysisBuildResult:
    options = options or AnalysisBuildOptions()
    manifest, all_chunks, progress_filter = _load_indexed_source(
        config, collection, progress
    )

    groups = group_chapters(all_chunks)
    selected = [
        group
        for group in complete_chapters(groups)
        if all(progress_filter.allows(chunk) for chunk in group.chunks)
    ]

    path = analysis_path(config.paths.metadata_dir, collection)
    llm_model, settings = _generation_plan(
        config,
        config.analysis,
        llm_model=options.llm_model,
        num_predict=options.num_predict,
        retries=options.retries,
    )

    document, rebuilt = load_or_reset(
        path,
        empty_analysis(
            collection=collection,
            manifest=manifest,
            llm_model=llm_model,
            settings=settings,
        ),
        read=read_analysis,
        stale=lambda stored: analysis_staleness_reason(
            stored,
            groups,
            collection=collection,
            source_path=manifest.source_path,
            llm_model=llm_model,
            settings=settings,
        ),
        rebuild=options.rebuild,
    )

    processed = processed_chapter_keys(document)
    budget = chapter_char_budget(
        num_ctx=config.ollama.num_ctx, num_predict=settings.num_predict
    )
    pending: list[ChapterGroup] = []
    skipped: list[tuple[str, str]] = []
    for group in selected:
        if (group.index, group.title) in processed:
            continue
        size = len(chapter_body(group))
        if budget and size > budget:
            skipped.append(
                (group.title, f"chapter is too long for num_ctx ({size} > {budget})")
            )
            continue
        pending.append(group)

    _emit(
        on_progress,
        AnalysisBuildEvent(
            stage="selected", progress=progress_filter, total=len(selected)
        ),
    )
    for title, reason in skipped:
        _emit(
            on_progress,
            AnalysisBuildEvent(stage="skipped", chapter_title=title, reason=reason),
        )

    if generator is None:
        generator = OllamaGenerator(
            base_url=config.ollama.base_url,
            model=llm_model,
            keep_alive=config.ollama.keep_alive,
            settings=settings,
        )

    for index, group in enumerate(pending, start=1):
        _emit(
            on_progress,
            AnalysisBuildEvent(
                stage="analyzing",
                index=index,
                total=len(pending),
                chapter_title=group.title,
            ),
        )
        analysis = generate_with_retry(
            generator,
            build_chapter_prompt(group),
            lambda raw: parse_chapter_analysis(raw, group),
            schema=ANALYSIS_RESPONSE_SCHEMA,
            retries=settings.retries,
            label=f"failed to analyze chapter {group.title}",
            on_retry=lambda attempt, exc: _emit(
                on_progress,
                AnalysisBuildEvent(
                    stage="retry",
                    index=index,
                    total=len(pending),
                    chapter_title=group.title,
                    attempt=attempt,
                    retries=settings.retries,
                    error=str(exc),
                ),
            ),
        )
        merge_chapter(document, analysis, group)
        update_analysis_metadata(
            document,
            collection=collection,
            manifest=manifest,
            llm_model=llm_model,
            settings=settings,
            progress=progress_filter,
            selected_chapter_count=len(selected),
        )
        write_analysis(path, document)
        _emit(
            on_progress,
            AnalysisBuildEvent(
                stage="analyzed",
                index=index,
                total=len(pending),
                chapter_title=group.title,
                character_count=len(analysis.characters),
                event_count=len(analysis.events),
                rejected_count=analysis.rejected_count,
                unanchored_count=analysis.unanchored_count,
            ),
        )

    update_analysis_metadata(
        document,
        collection=collection,
        manifest=manifest,
        llm_model=llm_model,
        settings=settings,
        progress=progress_filter,
        selected_chapter_count=len(selected),
    )
    write_analysis(path, document)

    chunks_by_id = {chunk.id: chunk for chunk in all_chunks}
    chapters = [
        filtered
        for filtered in (
            filter_chapter(chapter, chunks_by_id, progress_filter)
            for chapter in document.chapters
        )
        if filtered is not None
    ]
    return AnalysisBuildResult(
        collection=collection,
        analysis_path=path,
        status=derivation_status(
            selected=len(selected), pending=len(pending), rebuilt=rebuilt
        ),
        progress=progress_filter,
        chapters=chapters,
        skipped=skipped,
    )


def query_graph(
    config: ReadFellowConfig,
    query: str,
    collection: str,
    *,
    progress: ProgressLimit | None = None,
) -> GraphSearchResult:
    graph, all_chunks, progress_filter = _load_graph(config, collection, progress)
    graph_result = query_knowledge_graph(graph, query, progress=progress_filter)
    chunks = [chunk for chunk in all_chunks if progress_filter.allows(chunk)]
    return GraphSearchResult(
        progress=progress_filter,
        evidence=_graph_evidence(graph_result, chunks, query=query),
    )


def collection_status(
    config: ReadFellowConfig,
    collection: str,
    *,
    store: ChunkStore | None = None,
) -> CollectionStatus:
    """What the collection holds and what a rebuild would do to it, changing nothing.

    Every other entry fails closed the moment a derivative is stale. This is the
    one that has to say so instead, so each check that would otherwise raise is
    caught and reported. The staleness questions are asked with the *configured*
    model and settings, because "would graph-index resume or start over" is the
    thing worth knowing before spending another eight hours.
    """
    manifest = read_manifest(
        metadata_dir=config.paths.metadata_dir, collection=collection
    )
    chunks = read_chunks(metadata_dir=config.paths.metadata_dir, collection=collection)
    try:
        _validate_chunk_metadata_source(manifest, chunks)
        source_error = None
    except (RuntimeError, FileNotFoundError) as exc:
        source_error = str(exc)

    graph_file = graph_path(config.paths.metadata_dir, collection)
    graph, graph_error = _read_derivation(graph_file, read_graph)
    graph_model, graph_settings = _generation_plan(
        config, config.graph, llm_model=None, num_predict=None, retries=None
    )
    graph_report = DerivationReport(
        path=graph_file,
        exists=graph is not None,
        processed=graph.processed_chunk_count if graph else 0,
        total=len(chunks),
        rejected_count=graph.rejected_count if graph else 0,
        unanchored_count=graph.unanchored_count if graph else 0,
        stale_reason=graph_staleness_reason(
            graph,
            chunks,
            collection=collection,
            source_path=manifest.source_path,
            llm_model=graph_model,
            extraction_settings=graph_settings,
        )
        if graph
        else None,
        error=graph_error,
    )

    groups = complete_chapters(group_chapters(chunks))
    analysis_file = analysis_path(config.paths.metadata_dir, collection)
    analysis, analysis_error = _read_derivation(analysis_file, read_analysis)
    analysis_model, analysis_settings = _generation_plan(
        config, config.analysis, llm_model=None, num_predict=None, retries=None
    )
    analysis_report = DerivationReport(
        path=analysis_file,
        exists=analysis is not None,
        processed=analysis.processed_chapter_count if analysis else 0,
        total=len(groups),
        rejected_count=analysis.rejected_count if analysis else 0,
        unanchored_count=analysis.unanchored_count if analysis else 0,
        stale_reason=analysis_staleness_reason(
            analysis,
            groups,
            collection=collection,
            source_path=manifest.source_path,
            llm_model=analysis_model,
            settings=analysis_settings,
        )
        if analysis
        else None,
        error=analysis_error,
    )

    return CollectionStatus(
        collection=collection,
        manifest=manifest,
        source_error=source_error,
        index=_index_report(config, collection, manifest, store),
        graph=graph_report,
        analysis=analysis_report,
        diagnostics=graph_diagnostics(graph) if graph else None,
    )


def _index_report(
    config: ReadFellowConfig,
    collection: str,
    manifest: IndexManifest,
    store: ChunkStore | None,
) -> IndexReport:
    path = collection_path(config.paths.index_dir, collection)
    try:
        stats = (store or _open_store(config, collection)).stats()
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        return IndexReport(
            collection_path=path,
            expected_doc_count=manifest.chunk_count,
            stored_doc_count=0,
            index_completeness={},
            error=str(exc),
        )
    return IndexReport(
        collection_path=path,
        expected_doc_count=manifest.chunk_count,
        stored_doc_count=stats.doc_count,
        index_completeness=stats.index_completeness,
    )


def _read_derivation(
    path: Path,
    read: Callable[[Path], Any],
) -> tuple[Any, str | None]:
    """The stored document, or why it cannot be reported on."""
    if not path.is_file():
        return None, None
    try:
        return read(path), None
    except (ValueError, OSError) as exc:
        return None, str(exc)


def _load_graph(
    config: ReadFellowConfig,
    collection: str,
    progress: ProgressLimit | None,
) -> tuple[KnowledgeGraph, list[Chunk], ProgressFilter]:
    manifest, all_chunks, progress_filter = _load_indexed_source(
        config, collection, progress
    )
    path = graph_path(config.paths.metadata_dir, collection)
    if not path.is_file():
        raise FileNotFoundError(f"graph index not found: {path}; run graph-index first")

    graph = read_graph(path)
    stale_reason = graph_staleness_reason(
        graph,
        all_chunks,
        collection=collection,
        source_path=manifest.source_path,
    )
    if stale_reason is not None:
        raise RuntimeError(
            f"graph index is stale ({stale_reason}); run graph-index to rebuild it"
        )
    return graph, all_chunks, progress_filter


def hybrid_search(
    config: ReadFellowConfig,
    query: str,
    collection: str,
    *,
    top_k: int | None = None,
    progress: ProgressLimit | None = None,
    store: ChunkStore | None = None,
) -> HybridSearchResult:
    limit = top_k or config.search.top_k
    fan_out = limit * FAN_OUT_MULTIPLIER
    store = store or _open_store(config, collection)

    vector_result = semantic_search(
        config, query, collection, top_k=fan_out, progress=progress, store=store
    )
    fts_result = fts_search(
        config, query, collection, top_k=fan_out, progress=progress, store=store
    )

    channels: list[tuple[ChannelMode, list[Evidence]]] = [
        ("vector", vector_result.evidence),
        ("fts", fts_result.evidence),
    ]
    evidence, annotation = _annotate_with_graph(
        _fuse_channels(channels)[:limit], config, collection, progress=progress
    )
    return HybridSearchResult(
        progress=vector_result.progress,
        channels=[
            ChannelStatus(mode=mode, candidates=len(items)) for mode, items in channels
        ],
        graph_annotation=annotation,
        evidence=evidence,
    )


def _annotate_with_graph(
    evidence: list[Evidence],
    config: ReadFellowConfig,
    collection: str,
    *,
    progress: ProgressLimit | None,
) -> tuple[list[Evidence], GraphAnnotationStatus]:
    """The same results, carrying whatever the graph recorded about them.

    The graph is a derivative, so a missing or stale one costs the annotation
    and nothing else — the two scored channels already stand on the chunks
    themselves.
    """
    try:
        graph, _, progress_filter = _load_graph(config, collection, progress)
    except (FileNotFoundError, RuntimeError) as exc:
        return evidence, GraphAnnotationStatus(annotated=0, skipped_reason=str(exc))

    context_by_chunk = annotate_chunks(
        graph, [item.chunk_id for item in evidence], progress=progress_filter
    )
    annotated = [
        item.model_copy(update={"graph_context": context_by_chunk.get(item.chunk_id)})
        for item in evidence
    ]
    return annotated, GraphAnnotationStatus(
        annotated=sum(1 for item in annotated if item.graph_context is not None)
    )


def _open_store(config: ReadFellowConfig, collection: str) -> ChunkStore:
    return ZvecChunkStore.open_for_read(
        index_dir=config.paths.index_dir, collection=collection
    )


def progress_filter_from_limit(
    progress: ProgressLimit | None,
    *,
    manifest: IndexManifest | None,
) -> ProgressFilter:
    progress = progress or ProgressLimit()
    return build_progress_filter(
        manifest=manifest,
        max_chapter=progress.max_chapter,
        max_line=progress.max_line,
        max_chunk_index=progress.max_chunk_index,
    )


def _load_indexed_source(
    config: ReadFellowConfig,
    collection: str,
    progress: ProgressLimit | None,
) -> tuple[IndexManifest, list[Chunk], ProgressFilter]:
    """The manifest, chunks and progress filter a derivation reads the corpus through.

    The chunks are checked against the source file here, so no path that reaches
    the stored chunks can skip that check.
    """
    manifest = read_manifest(
        metadata_dir=config.paths.metadata_dir, collection=collection
    )
    chunks = read_chunks(metadata_dir=config.paths.metadata_dir, collection=collection)
    _validate_chunk_metadata_source(manifest, chunks)
    return manifest, chunks, progress_filter_from_limit(progress, manifest=manifest)


def _generation_plan(
    config: ReadFellowConfig,
    derivation: DerivationConfig,
    *,
    llm_model: str | None,
    num_predict: int | None,
    retries: int | None,
) -> tuple[str, DerivationSettings]:
    """The model and settings one build runs with, after per-run overrides."""
    num_predict = derivation.num_predict if num_predict is None else num_predict
    retries = derivation.retries if retries is None else retries
    return llm_model or config.ollama.generation_model, DerivationSettings(
        num_predict=num_predict,
        num_ctx=config.ollama.num_ctx,
        retries=retries,
    )


def _validate_chunk_metadata_source(
    manifest: IndexManifest,
    chunks: list[Chunk],
) -> None:
    if not chunks:
        return
    if manifest.chunker_version != CHUNKER_VERSION:
        raise RuntimeError(
            f"chunk metadata was produced by chunker version "
            f"{manifest.chunker_version} (current {CHUNKER_VERSION}); "
            "run index --rebuild"
        )
    if any(chunk.source_path != manifest.source_path for chunk in chunks):
        raise RuntimeError(
            "chunk metadata is stale (source path changed); run index to rebuild it"
        )

    source = source_from_manifest(manifest)
    if not source.is_file():
        raise FileNotFoundError(f"source file for chunk metadata not found: {source}")
    current_hash = sha256_file(source)
    if any(chunk.source_hash != current_hash for chunk in chunks):
        raise RuntimeError(
            "chunk metadata is stale (source file hash changed); "
            "run index to rebuild it"
        )


def _relative_source_path(source: Path) -> str:
    try:
        return str(source.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(source.resolve())


def _context_by_chunk(
    result: GraphQueryResult,
    *,
    query: str,
) -> dict[str, EvidenceGraphContext]:
    """Per chunk, the graph items anchored to it, narrowed to what `query` hit.

    An entity matched by name carries all of its anchors; one matched only
    through a quote carries just the quotes that contain the query.
    """
    entities_by_chunk: defaultdict[str, set[str]] = defaultdict(set)
    relations_by_chunk: defaultdict[str, set[str]] = defaultdict(set)

    for relation in result.relations:
        entities_by_chunk[relation.chunk_id].update(
            value
            for value in (
                relation.subject_entity or relation.subject,
                relation.object_entity or relation.object,
            )
            if value
        )
        relations_by_chunk[relation.chunk_id].add(
            f"{relation.subject} --{relation.relation}--> {relation.object}"
        )

    needle = query.strip().casefold()
    for entity in result.entities:
        if any(
            needle in value.casefold()
            for value in (entity.name, *entity.aliases, *entity.types)
        ):
            anchors = [*entity.mentions, *entity.evidence]
        else:
            anchors = [
                item for item in entity.evidence if needle in item.text.casefold()
            ]
        for item in anchors:
            if item.chunk_id:
                entities_by_chunk[item.chunk_id].add(entity.name)

    return {
        chunk_id: EvidenceGraphContext(
            entities=sorted(entities_by_chunk.get(chunk_id, ())),
            relations=sorted(relations_by_chunk.get(chunk_id, ())),
        )
        for chunk_id in {*entities_by_chunk, *relations_by_chunk}
    }


def _graph_evidence(
    result: GraphQueryResult,
    chunks: list[Chunk],
    *,
    query: str,
) -> list[Evidence]:
    context_by_chunk = _context_by_chunk(result, query=query)
    return [
        Evidence(
            chunk_id=chunk.id,
            source_path=chunk.source_path,
            chunk_index=chunk.chunk_index,
            line_start=chunk.line_start,
            line_end=chunk.line_end,
            byte_start=chunk.byte_start,
            byte_end=chunk.byte_end,
            chapter=chunk.chapter,
            text_hash=chunk.text_hash,
            text=chunk.text,
            retrieval_mode="graph",
            graph_context=context_by_chunk[chunk.id],
        )
        for chunk in chunks
        if chunk.id in context_by_chunk
    ]


def _fuse_channels(
    channels: list[tuple[ChannelMode, list[Evidence]]],
) -> list[Evidence]:
    scores: defaultdict[str, float] = defaultdict(float)
    matches: defaultdict[str, list[EvidenceMatch]] = defaultdict(list)
    docs: dict[str, Evidence] = {}

    for mode, items in channels:
        for rank, item in enumerate(items, start=1):
            scores[item.chunk_id] += 1.0 / (RRF_K + rank)
            matches[item.chunk_id].append(EvidenceMatch(mode=mode, rank=rank))
            docs.setdefault(item.chunk_id, item)

    fused = [
        docs[chunk_id].model_copy(
            update={
                "retrieval_mode": "hybrid",
                "score": score,
                "matches": matches[chunk_id],
            }
        )
        for chunk_id, score in scores.items()
    ]
    fused.sort(key=lambda item: (-(item.score or 0.0), item.chunk_index))
    return fused


def _emit(
    callback: Callable[[Any], None] | None,
    event: Any,
) -> None:
    if callback is not None:
        callback(event)
