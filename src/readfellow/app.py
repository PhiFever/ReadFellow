from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from .chunking import CHUNKER_VERSION, chunk_document, read_chapters, sha256_file
from .config import ReadFellowConfig
from .library import SqliteChunkStore
from .models import (
    ChapterBoundary,
    ChapterHits,
    Evidence,
    EvidenceMatch,
    FindResult,
    IndexManifest,
    LineHit,
    ProgressFilter,
    ReaderState,
    ReadLine,
    ReadResult,
    TocEntry,
    TocResult,
)
from .ollama import OllamaEmbedder
from .progress import AmbiguousChapter, build_progress_filter, line_limit_for_chapter

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
class _Book:
    store: SqliteChunkStore
    manifest: IndexManifest
    chapters: list[ChapterBoundary]
    source_error: str | None


@contextmanager
def _open_book(
    config: ReadFellowConfig,
    collection: str,
    *,
    report_errors: bool = False,
    write: bool = False,
) -> Iterator[_Book]:
    path = _library_path(config, collection)
    if write and not path.is_file():
        raise FileNotFoundError(
            f"collection does not exist: {path}; run the index command first"
        )
    with SqliteChunkStore.open(path, write=write) as store:
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
        yield _Book(store, manifest, chapters, error)


def set_progress(
    config: ReadFellowConfig,
    collection: str,
    *,
    chapter: str | None = None,
    line: int | None = None,
) -> ReaderState:
    if chapter is not None and line is not None:
        raise ValueError("choose one progress limit")
    with _open_book(config, collection, write=True) as book:
        limit = build_progress_filter(
            chapters=book.chapters,
            source=book.manifest.source_path,
            max_chapter=chapter,
            max_line=line,
        )
        kind = (
            "chapter" if chapter is not None else "line" if line is not None else "all"
        )
        value = str(line) if line is not None else chapter
        return book.store.save_progress(
            kind, value, limit.max_line_end, book.manifest.source_hash
        )


def _current_progress(book: _Book) -> ReaderState:
    state = book.store.reader_state()
    if state is None:
        raise ValueError("reading progress is unset; run progress set")
    if state.source_hash != book.manifest.source_hash:
        raise ValueError("reading progress is stale; run progress set again")
    return state


def show_progress(config: ReadFellowConfig, collection: str) -> ReaderState:
    with _open_book(config, collection) as book:
        return _current_progress(book)


@dataclass(frozen=True)
class _IndexedBook(_Book):
    progress: ProgressFilter

    def chapter_line_limit(self) -> int | None:
        end = self.progress.max_line_end
        if self.progress.max_chunk_index is not None:
            chunk_end = self.store.chunk_line_limit(self.progress.max_chunk_index)
            return chunk_end if end is None else min(end, chunk_end)
        return end


@contextmanager
def _read_collection(
    config: ReadFellowConfig, collection: str, progress: ProgressLimit | None = None
) -> Iterator[_IndexedBook]:
    with _open_book(config, collection) as book:
        state = _current_progress(book)
        request = progress or ProgressLimit()
        chapters = book.chapters
        if request.max_line is None and request.max_chapter is None:
            request = replace(
                request,
                max_chapter=state.value if state.kind == "chapter" else None,
                max_line=state.line_limit if state.kind == "line" else None,
            )
        elif state.line_limit is not None:
            message = "requested limit exceeds saved progress; use progress set to read further"
            if request.max_line is not None and request.max_line > state.line_limit:
                raise ValueError(message)
            if request.max_chapter is not None:
                # Resolve against the whole book, but keep later titles out of diagnostics.
                try:
                    chapter_limit, _ = line_limit_for_chapter(
                        chapters, request.max_chapter, source=book.manifest.source_path
                    )
                except AmbiguousChapter as exc:
                    if any(c.line_end > state.line_limit for c in exc.candidates):
                        raise ValueError(
                            "chapter is ambiguous; use --max-line within saved progress"
                        ) from None
                    raise
                if chapter_limit > state.line_limit:
                    raise ValueError(message)
        limit = build_progress_filter(
            chapters=chapters, source=book.manifest.source_path, **asdict(request)
        )
        yield _IndexedBook(
            book.store, book.manifest, chapters, book.source_error, limit
        )


def _visible_chapters(
    book: _IndexedBook,
    end: int | None,
    ordinals: tuple[int, int] | None = None,
    start: int = 1,
) -> Iterator[tuple[ChapterBoundary, list[str]]]:
    for chapter in book.store.chapter_texts(end, ordinals, start):
        assert chapter.text is not None
        lines = chapter.text.splitlines()
        if end is not None:
            lines = lines[: end - chapter.line_start + 1]
        yield chapter, lines


def table_of_contents(
    config: ReadFellowConfig, collection: str, *, progress: ProgressLimit | None = None
) -> TocResult:
    with _read_collection(config, collection, progress) as book:
        entries = []
        for chapter, lines in _visible_chapters(book, book.chapter_line_limit()):
            end = chapter.line_start + len(lines) - 1
            ref = str(chapter.volume)
            if chapter.number is not None:
                ref += f":{chapter.number}"
            entries.append(
                TocEntry(
                    ordinal=chapter.ordinal,
                    reference=ref,
                    title=chapter.title,
                    line_start=chapter.line_start,
                    line_end=end,
                    characters=sum(len("".join(line.split())) for line in lines),
                    partial=end < chapter.line_end,
                )
            )
        return TocResult(progress=book.progress.description, chapters=entries)


def read_passage(
    config: ReadFellowConfig,
    collection: str,
    *,
    chapters: tuple[int, int] | None = None,
    lines: tuple[int, int] | None = None,
    progress: ProgressLimit | None = None,
) -> ReadResult:
    if (chapters is None) == (lines is None):
        raise ValueError("choose --chapters or --lines")
    with _read_collection(config, collection, progress) as book:
        cap = book.chapter_line_limit()
        if lines:
            first, last = lines
        else:
            assert chapters is not None
            selected = [
                c for c in book.chapters if chapters[0] <= c.ordinal <= chapters[1]
            ]
            if not selected:
                raise ValueError("no chapters in requested range")
            first, last = selected[0].line_start, selected[-1].line_end
        end = min(last, cap) if cap is not None else last
        if first > end:
            raise ValueError("requested range is outside reading progress")
        result = ReadResult(
            progress=book.progress.description,
            lines=[],
            clipped_at=end if end < last else None,
            truncated=False,
        )
        remaining = 60000
        for chapter, texts in _visible_chapters(book, end, chapters, first):
            for number, text in enumerate(texts, chapter.line_start):
                if number < first:
                    continue
                row = ReadLine(number=number, text=text)
                size = len(row.numbered()) + 1
                if size > remaining:
                    room = remaining - (size - len(text))
                    if room >= 0:
                        result.lines.append(ReadLine(number=number, text=text[:room]))
                    result.truncated = True
                    return result
                result.lines.append(row)
                remaining -= size
        if not result.lines:
            raise ValueError("no chapter text in requested range")
        return result


def find_text(
    config: ReadFellowConfig,
    query: str,
    collection: str,
    *,
    chapters: tuple[int, int] | None = None,
    limit: int = 50,
    progress: ProgressLimit | None = None,
) -> FindResult:
    if not query or limit < 0:
        raise ValueError("find requires a nonempty term and a nonnegative limit")
    with _read_collection(config, collection, progress) as book:
        counts = []
        hits = []
        for chapter, texts in _visible_chapters(
            book, book.chapter_line_limit(), chapters
        ):
            count = 0
            for number, text in enumerate(texts, chapter.line_start):
                offset = text.find(query)
                if offset < 0:
                    continue
                count += 1
                if len(hits) < limit:
                    hits.append(
                        LineHit(
                            number=number,
                            ordinal=chapter.ordinal,
                            title=chapter.title,
                            snippet=text[
                                max(0, offset - 60) : offset + len(query) + 60
                            ],
                        )
                    )
            if count:
                counts.append(
                    ChapterHits(
                        ordinal=chapter.ordinal, title=chapter.title, count=count
                    )
                )
        return FindResult(
            progress=book.progress.description,
            total_lines=sum(c.count for c in counts),
            total_chapters=len(counts),
            chapters=counts,
            hits=hits,
        )


def collection_status(config: ReadFellowConfig, collection: str) -> CollectionStatus:
    with _open_book(config, collection, report_errors=True) as book:
        return CollectionStatus(
            collection,
            book.manifest,
            len(book.chapters),
            book.source_error,
            IndexReport(book.manifest.chunk_count, book.store.chunk_count()),
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
