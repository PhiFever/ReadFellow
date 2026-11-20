from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from .chunking import CHUNKER_VERSION, chunk_document, read_chapters, sha256_file
from .config import ReadFellowConfig
from .library import SqliteChunkStore
from .models import Evidence, EvidenceMatch, IndexManifest, ProgressFilter
from .ollama import OllamaEmbedder
from .progress import build_progress_filter

RRF_K = 60
FAN_OUT_MULTIPLIER = 10
ChannelMode = Literal["vector", "fts"]


@dataclass(frozen=True)
class ProgressLimit:
    max_chapter: str | None = None
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
class ChannelStatus:
    mode: ChannelMode
    candidates: int


@dataclass(frozen=True)
class HybridSearchResult:
    progress: ProgressFilter
    channels: list[ChannelStatus]
    evidence: list[Evidence]


@dataclass(frozen=True)
class IndexReport:
    expected_doc_count: int
    stored_doc_count: int

    @property
    def is_complete(self) -> bool:
        return self.stored_doc_count == self.expected_doc_count


@dataclass(frozen=True)
class CollectionStatus:
    collection: str
    manifest: IndexManifest
    chapter_count: int
    source_error: str | None
    index: IndexReport


def _library_path(config: ReadFellowConfig, collection: str) -> Path:
    return config.paths.library_dir / f"{collection}.db"


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
    overlap_chars = (
        config.indexing.overlap_chars
        if options.overlap_chars is None
        else options.overlap_chars
    )
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

    chapters = read_chapters(source)
    embedder = OllamaEmbedder(
        base_url=config.ollama.base_url,
        model=config.ollama.embedding_model,
        keep_alive=config.ollama.keep_alive,
    )
    _emit(
        on_progress,
        IndexProgressEvent(stage="probe", model=config.ollama.embedding_model),
    )
    probe_vector = embedder.embed_one("embedding dimension probe")
    dimension = len(probe_vector)

    path = _library_path(config, collection)
    with SqliteChunkStore.open(path, write=True, rebuild=options.rebuild) as store:
        manifest = IndexManifest(
            source_hash=chunks[0].source_hash,
            source_path=relative_source,
            model=config.ollama.embedding_model,
            embedding_dimension=dimension,
            chunk_count=len(chunks),
            chunk_chars=chunk_chars,
            overlap_chars=overlap_chars,
            chunker_version=CHUNKER_VERSION,
        )
        store.prepare(manifest, chapters)

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
            collection_path=path,
        )


@dataclass(frozen=True)
class _IndexedBook:
    store: SqliteChunkStore
    manifest: IndexManifest
    progress: ProgressFilter
    chapter_count: int
    source_error: str | None


@contextmanager
def _read_collection(
    config: ReadFellowConfig,
    collection: str,
    progress: ProgressLimit | None = None,
    *,
    report_errors: bool = False,
) -> Iterator[_IndexedBook]:
    path = _library_path(config, collection)
    with SqliteChunkStore.open(path) as store:
        manifest = store.manifest()
        if manifest is None:
            raise RuntimeError(
                f"collection has no book: {path}; run the index command first"
            )
        chapters = store.chapters()
        error = None
        source = Path(manifest.source_path)
        if manifest.chunker_version != CHUNKER_VERSION:
            error = (
                f"chunk metadata was produced by chunker version {manifest.chunker_version} "
                f"(current {CHUNKER_VERSION}); run index --rebuild"
            )
        else:
            try:
                current_hash = sha256_file(source)
            except FileNotFoundError:
                pass
            except OSError as exc:
                if not report_errors:
                    raise
                error = str(exc)
            else:
                if current_hash != manifest.source_hash:
                    error = "chunk metadata is stale (source file hash changed); run index --rebuild"
        if error and not report_errors:
            raise RuntimeError(error)
        limit = build_progress_filter(
            chapters=chapters,
            source=manifest.source_path,
            **asdict(progress or ProgressLimit()),
        )
        yield _IndexedBook(store, manifest, limit, len(chapters), error)


def collection_status(config: ReadFellowConfig, collection: str) -> CollectionStatus:
    with _read_collection(config, collection, report_errors=True) as book:
        return CollectionStatus(
            collection,
            book.manifest,
            book.chapter_count,
            book.source_error,
            IndexReport(book.manifest.chunk_count, book.store.stats().doc_count),
        )


def _query_vector(
    config: ReadFellowConfig, manifest: IndexManifest, query: str
) -> list[float]:
    embedder = OllamaEmbedder(
        base_url=config.ollama.base_url,
        model=manifest.model,
        keep_alive=config.ollama.keep_alive,
    )
    return embedder.embed_one(query)


def semantic_search(
    config: ReadFellowConfig,
    query: str,
    collection: str,
    *,
    top_k: int | None = None,
    progress: ProgressLimit | None = None,
) -> SearchResult:
    with _read_collection(config, collection, progress) as book:
        store = book.store
        vector = _query_vector(config, book.manifest, query)
        return SearchResult(
            progress=book.progress,
            evidence=store.search_vector(
                vector,
                top_k=top_k or config.search.top_k,
                progress=book.progress,
            ),
        )


def fts_search(
    config: ReadFellowConfig,
    query: str,
    collection: str,
    *,
    top_k: int | None = None,
    progress: ProgressLimit | None = None,
) -> SearchResult:
    with _read_collection(config, collection, progress) as book:
        store = book.store
        return SearchResult(
            progress=book.progress,
            evidence=store.search_fts(
                query,
                top_k=top_k or config.search.top_k,
                progress=book.progress,
            ),
        )


def fetch_chunk(
    config: ReadFellowConfig,
    chunk_id: str,
    collection: str,
    *,
    progress: ProgressLimit | None = None,
) -> FetchChunkResult:
    with _read_collection(config, collection, progress) as book:
        store = book.store
        evidence = store.fetch(chunk_id)
        if evidence is None:
            return FetchChunkResult(
                progress=book.progress,
                status="not_found",
                evidence=None,
            )
        if not book.progress.allows(evidence):
            return FetchChunkResult(
                progress=book.progress,
                status="outside_progress",
                evidence=None,
            )
        return FetchChunkResult(
            progress=book.progress,
            status="found",
            evidence=evidence,
        )


def hybrid_search(
    config: ReadFellowConfig,
    query: str,
    collection: str,
    *,
    top_k: int | None = None,
    progress: ProgressLimit | None = None,
) -> HybridSearchResult:
    with _read_collection(config, collection, progress) as book:
        store = book.store
        limit = top_k or config.search.top_k
        fan_out = limit * FAN_OUT_MULTIPLIER
        vector = _query_vector(config, book.manifest, query)
        channels: list[tuple[ChannelMode, list[Evidence]]] = [
            (
                "vector",
                store.search_vector(vector, top_k=fan_out, progress=book.progress),
            ),
            ("fts", store.search_fts(query, top_k=fan_out, progress=book.progress)),
        ]
        evidence = _fuse_channels(channels)[:limit]
        return HybridSearchResult(
            progress=book.progress,
            channels=[
                ChannelStatus(mode=mode, candidates=len(items))
                for mode, items in channels
            ],
            evidence=evidence,
        )


def _relative_source_path(source: Path) -> str:
    try:
        return str(source.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(source.resolve())


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
