from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .analysis import (
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
    build_extraction_prompt,
    empty_graph,
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
    ProgressFilter,
    QueryChunkFields,
)
from .ollama import OllamaEmbedder, OllamaGenerator
from .progress import build_progress_filter, source_from_manifest
from .store import (
    chunk_to_doc,
    collection_path,
    open_or_create_collection,
    query_fts,
    query_vector,
    read_chunks,
    read_manifest,
    write_manifest,
)
from .store import (
    fetch_chunk as fetch_stored_chunk,
)


RRF_K = 60
FAN_OUT_MULTIPLIER = 10

ChannelMode = Literal["vector", "fts", "graph"]


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


@dataclass(frozen=True)
class GraphBuildResult:
    collection: str
    graph_path: Path
    status: str
    selected_chunk_count: int
    entity_count: int
    relation_count: int


@dataclass(frozen=True)
class GraphSearchResult:
    progress: ProgressFilter
    evidence: list[Evidence]


@dataclass(frozen=True)
class ChannelStatus:
    mode: ChannelMode
    candidates: int
    skipped_reason: str | None = None


@dataclass(frozen=True)
class HybridSearchResult:
    progress: ProgressFilter
    channels: list[ChannelStatus]
    evidence: list[Evidence]


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

    coll = open_or_create_collection(
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
        existing = coll.fetch(
            [chunk.id for chunk in batch],
            output_fields=["text_hash"],
            include_vector=False,
        )
        existing_hashes = {
            chunk.id: QueryChunkFields.model_validate(
                existing[chunk.id].fields
            ).text_hash
            for chunk in batch
            if existing.get(chunk.id) is not None
        }
        missing = [chunk for chunk in batch if chunk.id not in existing_hashes]
        changed = [
            chunk
            for chunk in batch
            if chunk.id in existing_hashes
            and existing_hashes[chunk.id] != chunk.text_hash
        ]
        pending = missing + changed
        skipped += len(batch) - len(pending)
        processed = offset + len(batch)
        if not pending:
            _emit(
                on_progress,
                IndexProgressEvent(
                    stage="batch",
                    processed=processed,
                    total=len(chunks),
                    inserted=inserted,
                    skipped=skipped,
                    skipped_existing=True,
                ),
            )
            continue

        texts = [chunk.text for chunk in pending]
        vectors = embedder.embed(texts)
        docs = [
            chunk_to_doc(chunk, vector, model=config.ollama.embedding_model)
            for chunk, vector in zip(pending, vectors, strict=True)
        ]
        missing_count = len(missing)
        statuses = []
        if missing_count:
            statuses.extend(coll.insert(docs[:missing_count]))
        if len(docs) > missing_count:
            statuses.extend(coll.update(docs[missing_count:]))
        if not all(status.ok() for status in statuses):
            failed = [str(status) for status in statuses if not status.ok()]
            raise RuntimeError(f"zvec write failed: {failed[:3]}")
        inserted += len(docs)
        elapsed = time.monotonic() - start
        rate = inserted / elapsed if elapsed else 0
        _emit(
            on_progress,
            IndexProgressEvent(
                stage="batch",
                processed=processed,
                total=len(chunks),
                inserted=inserted,
                skipped=skipped,
                rate=rate,
            ),
        )

    if options.optimize:
        _emit(on_progress, IndexProgressEvent(stage="optimize"))
        coll.optimize()
    coll.flush()
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
) -> SearchResult:
    manifest = read_manifest(
        metadata_dir=config.paths.metadata_dir, collection=collection
    )
    coll = open_existing_collection(config, collection)
    progress_filter = progress_filter_from_limit(progress, manifest=manifest)
    embedder = OllamaEmbedder(
        base_url=config.ollama.base_url,
        model=manifest.model or config.ollama.embedding_model,
        keep_alive=config.ollama.keep_alive,
    )
    vector = embedder.embed_one(query)
    docs = query_vector(
        coll,
        vector,
        top_k=top_k or config.search.top_k,
        filter=progress_filter.expression,
    )
    return SearchResult(
        progress=progress_filter,
        evidence=[_evidence_from_doc(doc, retrieval_mode="vector") for doc in docs],
    )


def fts_search(
    config: ReadFellowConfig,
    query: str,
    collection: str,
    *,
    top_k: int | None = None,
    progress: ProgressLimit | None = None,
) -> SearchResult:
    manifest = read_manifest(
        metadata_dir=config.paths.metadata_dir, collection=collection
    )
    coll = open_existing_collection(config, collection)
    progress_filter = progress_filter_from_limit(progress, manifest=manifest)
    docs = query_fts(
        coll,
        query,
        top_k=top_k or config.search.top_k,
        filter=progress_filter.expression,
    )
    return SearchResult(
        progress=progress_filter,
        evidence=[_evidence_from_doc(doc, retrieval_mode="fts") for doc in docs],
    )


def fetch_chunk(
    config: ReadFellowConfig,
    chunk_id: str,
    collection: str,
    *,
    progress: ProgressLimit | None = None,
) -> FetchChunkResult:
    manifest = read_manifest(
        metadata_dir=config.paths.metadata_dir, collection=collection
    )
    coll = open_existing_collection(config, collection)
    progress_filter = progress_filter_from_limit(progress, manifest=manifest)
    doc = fetch_stored_chunk(coll, chunk_id)
    if doc is None:
        return FetchChunkResult(
            progress=progress_filter,
            status="not_found",
            evidence=None,
        )

    fields = QueryChunkFields.model_validate(doc.fields)
    if not progress_filter.allows(fields):
        return FetchChunkResult(
            progress=progress_filter,
            status="outside_progress",
            evidence=None,
        )
    return FetchChunkResult(
        progress=progress_filter,
        status="found",
        evidence=_evidence_from_doc(doc, retrieval_mode="fetch"),
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
            num_predict=extraction_settings.num_predict,
            num_ctx=config.ollama.num_ctx,
        )

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
        extraction = generate_with_retry(
            generator,
            build_extraction_prompt(chunk),
            lambda raw: parse_graph_extraction(raw, chunk),
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
            num_predict=settings.num_predict,
            num_ctx=config.ollama.num_ctx,
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

    graph_result = query_knowledge_graph(graph, query, progress=progress_filter)
    chunks = [chunk for chunk in all_chunks if progress_filter.allows(chunk)]
    return GraphSearchResult(
        progress=progress_filter,
        evidence=_graph_evidence(graph_result, chunks, query=query),
    )


def hybrid_search(
    config: ReadFellowConfig,
    query: str,
    collection: str,
    *,
    top_k: int | None = None,
    progress: ProgressLimit | None = None,
) -> HybridSearchResult:
    limit = top_k or config.search.top_k
    fan_out = limit * FAN_OUT_MULTIPLIER

    vector_result = semantic_search(
        config, query, collection, top_k=fan_out, progress=progress
    )
    fts_result = fts_search(config, query, collection, top_k=fan_out, progress=progress)

    graph_evidence: list[Evidence] = []
    skipped_reason: str | None = None
    try:
        graph_result = query_graph(config, query, collection, progress=progress)
    except (FileNotFoundError, RuntimeError) as exc:
        skipped_reason = str(exc)
    else:
        graph_evidence = _rank_graph_evidence(graph_result.evidence)[:fan_out]

    channels: list[tuple[ChannelMode, list[Evidence]]] = [
        ("vector", vector_result.evidence),
        ("fts", fts_result.evidence),
        ("graph", graph_evidence),
    ]
    return HybridSearchResult(
        progress=vector_result.progress,
        channels=[
            ChannelStatus(
                mode=mode,
                candidates=len(items),
                skipped_reason=skipped_reason if mode == "graph" else None,
            )
            for mode, items in channels
        ],
        evidence=_fuse_channels(channels)[:limit],
    )


def open_existing_collection(config: ReadFellowConfig, collection: str):
    path = collection_path(config.paths.index_dir, collection)
    if not path.exists():
        raise FileNotFoundError(
            f"collection does not exist: {path}; run the index command first"
        )
    import zvec

    return zvec.open(str(path), zvec.CollectionOption(read_only=True, enable_mmap=True))


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
        temperature=0.0,
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


def _evidence_from_doc(
    doc: Any,
    *,
    retrieval_mode: Literal["vector", "fts", "fetch", "graph"],
) -> Evidence:
    fields = QueryChunkFields.model_validate(doc.fields)
    return Evidence(
        chunk_id=doc.id,
        source_path=fields.source_path,
        chunk_index=fields.chunk_index,
        line_start=fields.line_start,
        line_end=fields.line_end,
        byte_start=fields.byte_start,
        byte_end=fields.byte_end,
        chapter=fields.chapter,
        text_hash=fields.text_hash,
        text=fields.text,
        retrieval_mode=retrieval_mode,
        score=doc.score,
    )


def _graph_evidence(
    result: GraphQueryResult,
    chunks: list[Chunk],
    *,
    query: str,
) -> list[Evidence]:
    context_by_chunk: dict[str, tuple[set[str], set[str]]] = {}

    def context_for(chunk_id: str) -> tuple[set[str], set[str]]:
        return context_by_chunk.setdefault(chunk_id, (set(), set()))

    for relation in result.relations:
        entities, relations = context_for(relation.chunk_id)
        entities.update(
            value
            for value in (
                relation.subject_entity or relation.subject,
                relation.object_entity or relation.object,
            )
            if value
        )
        relations.add(f"{relation.subject} --{relation.relation}--> {relation.object}")

    needle = query.strip().casefold()
    for entity in result.entities:
        identity_values = [entity.name, *entity.aliases, *entity.types]
        if any(needle in value.casefold() for value in identity_values):
            matching_contexts = [*entity.mentions, *entity.evidence]
        else:
            matching_contexts = [
                item for item in entity.evidence if needle in item.text.casefold()
            ]
        for item in matching_contexts:
            if item.chunk_id:
                entities, _ = context_for(item.chunk_id)
                entities.add(entity.name)

    evidence: list[Evidence] = []
    for chunk in chunks:
        context = context_by_chunk.get(chunk.id)
        if context is None:
            continue
        entities, relations = context
        evidence.append(
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
                graph_context=EvidenceGraphContext(
                    entities=sorted(entities),
                    relations=sorted(relations),
                ),
            )
        )
    return evidence


def _rank_graph_evidence(evidence: list[Evidence]) -> list[Evidence]:
    def rank_key(item: Evidence) -> tuple[int, int, int]:
        context = item.graph_context
        relations = len(context.relations) if context is not None else 0
        entities = len(context.entities) if context is not None else 0
        return (-relations, -entities, item.chunk_index)

    return sorted(evidence, key=rank_key)


def _fuse_channels(
    channels: list[tuple[ChannelMode, list[Evidence]]],
) -> list[Evidence]:
    scores: defaultdict[str, float] = defaultdict(float)
    matches: defaultdict[str, list[EvidenceMatch]] = defaultdict(list)
    stored_docs: dict[str, Evidence] = {}
    graph_docs: dict[str, Evidence] = {}

    for mode, items in channels:
        for rank, item in enumerate(items, start=1):
            scores[item.chunk_id] += 1.0 / (RRF_K + rank)
            matches[item.chunk_id].append(EvidenceMatch(mode=mode, rank=rank))
            target = graph_docs if mode == "graph" else stored_docs
            target.setdefault(item.chunk_id, item)

    fused: list[Evidence] = []
    for chunk_id, score in scores.items():
        graph_hit = graph_docs.get(chunk_id)
        base = stored_docs.get(chunk_id) or graph_docs[chunk_id]
        fused.append(
            base.model_copy(
                update={
                    "retrieval_mode": "hybrid",
                    "score": score,
                    "matches": matches[chunk_id],
                    "graph_context": graph_hit.graph_context
                    if graph_hit is not None
                    else None,
                }
            )
        )
    fused.sort(key=lambda item: (-(item.score or 0.0), item.chunk_index))
    return fused


def _emit(
    callback: Callable[[Any], None] | None,
    event: Any,
) -> None:
    if callback is not None:
        callback(event)
