from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any

from .chunking import chunk_document
from .config import ReadFellowConfig
from .graph import (
    build_extraction_prompt,
    empty_graph,
    graph_path,
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
    GraphQueryResult,
    IndexManifest,
    ProgressFilter,
    QueryChunkFields,
)
from .ollama import OllamaEmbedder, OllamaGenerator
from .progress import build_progress_filter
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
    docs: list[Any]


@dataclass(frozen=True)
class FetchChunkResult:
    progress: ProgressFilter
    doc: Any | None
    allowed: bool


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
    result: GraphQueryResult


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
            chunk.id: QueryChunkFields.model_validate(existing[chunk.id].fields).text_hash
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
    manifest = read_manifest(metadata_dir=config.paths.metadata_dir, collection=collection)
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
    return SearchResult(progress=progress_filter, docs=docs)


def fts_search(
    config: ReadFellowConfig,
    query: str,
    collection: str,
    *,
    top_k: int | None = None,
    progress: ProgressLimit | None = None,
) -> SearchResult:
    manifest = read_manifest(metadata_dir=config.paths.metadata_dir, collection=collection)
    coll = open_existing_collection(config, collection)
    progress_filter = progress_filter_from_limit(progress, manifest=manifest)
    docs = query_fts(
        coll,
        query,
        top_k=top_k or config.search.top_k,
        filter=progress_filter.expression,
    )
    return SearchResult(progress=progress_filter, docs=docs)


def fetch_chunk(
    config: ReadFellowConfig,
    chunk_id: str,
    collection: str,
    *,
    progress: ProgressLimit | None = None,
) -> FetchChunkResult:
    manifest = read_manifest(metadata_dir=config.paths.metadata_dir, collection=collection)
    coll = open_existing_collection(config, collection)
    progress_filter = progress_filter_from_limit(progress, manifest=manifest)
    doc = fetch_stored_chunk(coll, chunk_id)
    if doc is None:
        return FetchChunkResult(progress=progress_filter, doc=None, allowed=False)

    fields = QueryChunkFields.model_validate(doc.fields)
    return FetchChunkResult(
        progress=progress_filter,
        doc=doc,
        allowed=progress_filter.allows(fields),
    )


def build_graph(
    config: ReadFellowConfig,
    collection: str,
    *,
    progress: ProgressLimit | None = None,
    options: GraphBuildOptions | None = None,
    on_progress: Callable[[GraphBuildEvent], None] | None = None,
) -> GraphBuildResult:
    options = options or GraphBuildOptions()
    manifest = read_manifest(metadata_dir=config.paths.metadata_dir, collection=collection)
    progress_filter = progress_filter_from_limit(progress, manifest=manifest)

    chunks = [
        chunk
        for chunk in read_chunks(metadata_dir=config.paths.metadata_dir, collection=collection)
        if progress_filter.allows(chunk)
    ]
    if options.limit:
        chunks = chunks[: options.limit]

    path = graph_path(config.paths.metadata_dir, collection)
    llm_model = options.llm_model or config.ollama.generation_model
    if path.exists() and not options.rebuild:
        graph = read_graph(path)
    else:
        graph = empty_graph(collection=collection, manifest=manifest, llm_model=llm_model)

    processed = set() if options.rebuild else processed_chunk_ids(graph)
    pending = [chunk for chunk in chunks if chunk.id not in processed]
    update_graph_metadata(
        graph,
        collection=collection,
        manifest=manifest,
        llm_model=llm_model,
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

    generator = OllamaGenerator(
        base_url=config.ollama.base_url,
        model=llm_model,
        keep_alive=config.ollama.keep_alive,
        num_predict=options.num_predict or config.graph.num_predict,
    )
    retries = config.graph.retries if options.retries is None else options.retries

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
        status="built",
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
    manifest = read_manifest(metadata_dir=config.paths.metadata_dir, collection=collection)
    progress_filter = progress_filter_from_limit(progress, manifest=manifest)
    path = graph_path(config.paths.metadata_dir, collection)
    if not path.is_file():
        raise FileNotFoundError(f"graph index not found: {path}; run graph-index first")

    graph = read_graph(path)
    return GraphSearchResult(
        progress=progress_filter,
        result=query_knowledge_graph(graph, query, progress=progress_filter),
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


def _relative_source_path(source: Path) -> str:
    try:
        return str(source.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(source.resolve())


def _emit(
    callback: Callable[[Any], None] | None,
    event: Any,
) -> None:
    if callback is not None:
        callback(event)
