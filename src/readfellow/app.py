from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from .chunking import chunk_document, sha256_file
from .config import ReadFellowConfig
from .graph import (
    build_extraction_prompt,
    empty_graph,
    graph_path,
    graph_staleness_reason,
    merge_extraction,
    parse_graph_extraction,
    processed_chunk_ids,
    query_graph as query_knowledge_graph,
    read_chunks,
    read_graph,
    update_graph_metadata,
    write_graph,
)
from .models import (
    Chunk,
    Evidence,
    EvidenceGraphContext,
    GraphExtractionSettings,
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
    fetch_chunk as fetch_stored_chunk,
    open_or_create_collection,
    query_fts,
    query_vector,
    read_manifest,
    write_manifest,
)


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


class GraphGenerator(Protocol):
    def generate_json(self, prompt: str) -> str: ...


@dataclass(frozen=True)
class GraphSearchResult:
    progress: ProgressFilter
    evidence: list[Evidence]


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
    generator: GraphGenerator | None = None,
) -> GraphBuildResult:
    options = options or GraphBuildOptions()
    manifest = read_manifest(
        metadata_dir=config.paths.metadata_dir, collection=collection
    )
    all_chunks = read_chunks(
        metadata_dir=config.paths.metadata_dir,
        collection=collection,
    )
    _validate_chunk_metadata_source(manifest, all_chunks)
    progress_filter = progress_filter_from_limit(progress, manifest=manifest)
    chunks = [chunk for chunk in all_chunks if progress_filter.allows(chunk)]
    if options.limit:
        chunks = chunks[: options.limit]

    path = graph_path(config.paths.metadata_dir, collection)
    llm_model = options.llm_model or config.ollama.generation_model
    num_predict = (
        config.graph.num_predict if options.num_predict is None else options.num_predict
    )
    retries = config.graph.retries if options.retries is None else options.retries
    extraction_settings = GraphExtractionSettings(
        temperature=0.0,
        num_predict=num_predict,
        retries=retries,
    )
    path_existed = path.exists()
    rebuilt = path_existed and options.rebuild
    if path_existed and not options.rebuild:
        graph = read_graph(path)
        stale_reason = graph_staleness_reason(
            graph,
            all_chunks,
            collection=collection,
            source_path=manifest.source_path,
            llm_model=llm_model,
            extraction_settings=extraction_settings,
        )
        if stale_reason is not None:
            graph = empty_graph(
                collection=collection,
                manifest=manifest,
                llm_model=llm_model,
                extraction_settings=extraction_settings,
            )
            rebuilt = True
    else:
        graph = empty_graph(
            collection=collection,
            manifest=manifest,
            llm_model=llm_model,
            extraction_settings=extraction_settings,
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

    if not chunks:
        write_graph(path, graph)
        return GraphBuildResult(
            collection=collection,
            graph_path=path,
            status="empty",
            selected_chunk_count=0,
            entity_count=graph.entity_count,
            relation_count=graph.relation_count,
        )

    if not pending:
        write_graph(path, graph)
        return GraphBuildResult(
            collection=collection,
            graph_path=path,
            status="up_to_date",
            selected_chunk_count=len(chunks),
            entity_count=graph.entity_count,
            relation_count=graph.relation_count,
        )

    if generator is None:
        generator = OllamaGenerator(
            base_url=config.ollama.base_url,
            model=llm_model,
            keep_alive=config.ollama.keep_alive,
            num_predict=num_predict,
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
        prompt = build_extraction_prompt(chunk)
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                raw = generator.generate_json(prompt)
                extraction = parse_graph_extraction(raw, chunk)
                break
            except Exception as exc:
                last_error = exc
                if attempt >= retries:
                    raise RuntimeError(
                        f"failed to extract graph for chunk {chunk_id}: {exc}"
                    ) from exc
                _emit(
                    on_progress,
                    GraphBuildEvent(
                        stage="retry",
                        index=index,
                        total=len(pending),
                        chunk_id=chunk_id,
                        attempt=attempt + 1,
                        retries=retries,
                        error=str(exc),
                    ),
                )
        else:
            raise RuntimeError(
                f"failed to extract graph for chunk {chunk_id}: {last_error}"
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
        status="rebuilt" if rebuilt else "built",
        selected_chunk_count=len(chunks),
        entity_count=graph.entity_count,
        relation_count=graph.relation_count,
    )


def query_graph(
    config: ReadFellowConfig,
    query: str,
    collection: str,
    *,
    progress: ProgressLimit | None = None,
) -> GraphSearchResult:
    manifest = read_manifest(
        metadata_dir=config.paths.metadata_dir, collection=collection
    )
    path = graph_path(config.paths.metadata_dir, collection)
    if not path.is_file():
        raise FileNotFoundError(f"graph index not found: {path}; run graph-index first")

    all_chunks = read_chunks(
        metadata_dir=config.paths.metadata_dir,
        collection=collection,
    )
    _validate_chunk_metadata_source(manifest, all_chunks)
    progress_filter = progress_filter_from_limit(progress, manifest=manifest)
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


def _validate_chunk_metadata_source(
    manifest: IndexManifest,
    chunks: list[Chunk],
) -> None:
    if not chunks:
        return
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


def _emit(
    callback: Callable[[Any], None] | None,
    event: Any,
) -> None:
    if callback is not None:
        callback(event)
