from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from .app import (
    AnalysisBuildEvent,
    AnalysisBuildOptions,
    ChannelStatus,
    GraphBuildEvent,
    GraphBuildOptions,
    IndexDocumentOptions,
    IndexProgressEvent,
    ProgressLimit,
    fts_search,
    hybrid_search,
    index_document,
    semantic_search,
)
from .app import (
    build_analysis as build_analysis_workflow,
)
from .app import (
    build_graph as build_graph_workflow,
)
from .app import (
    fetch_chunk as fetch_chunk_workflow,
)
from .app import (
    query_graph as query_graph_workflow,
)
from .config import CONFIG_FILE, ReadFellowConfig, load_config
from .models import ChapterAnalysis, ChunkContext, Evidence, ProgressFilter


def valid_collection_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]", "_", value.strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if len(normalized) < 2:
        raise argparse.ArgumentTypeError(
            "collection name must contain at least two letters, numbers, or underscores"
        )
    return normalized


def build_parser(config: ReadFellowConfig) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="readfellow")
    parser.add_argument("--config", type=Path, default=CONFIG_FILE)
    parser.add_argument("--index-dir", type=Path, default=config.paths.index_dir)
    parser.add_argument("--metadata-dir", type=Path, default=config.paths.metadata_dir)
    parser.add_argument("--ollama-url", default=config.ollama.base_url)
    parser.add_argument("--model", default=config.ollama.embedding_model)
    parser.add_argument("--keep-alive", default=config.ollama.keep_alive)

    subparsers = parser.add_subparsers(dest="command", required=True)

    index = subparsers.add_parser("index", help="chunk and index a UTF-8 text document")
    index.add_argument("source", type=Path)
    index.add_argument(
        "--collection",
        type=valid_collection_name,
        default=config.indexing.default_collection,
    )
    index.add_argument("--chunk-chars", type=int, default=config.indexing.chunk_chars)
    index.add_argument(
        "--overlap-chars", type=int, default=config.indexing.overlap_chars
    )
    index.add_argument("--batch-size", type=int, default=config.indexing.batch_size)
    index.add_argument(
        "--limit", type=int, default=0, help="index only the first N chunks"
    )
    index.add_argument("--rebuild", action="store_true")
    index.add_argument(
        "--no-optimize",
        action="store_true",
        help="skip final zvec optimize; faster indexing, but persisted FTS may not be queryable",
    )

    search = subparsers.add_parser("search", help="semantic vector search")
    search.add_argument("query")
    search.add_argument(
        "--collection",
        type=valid_collection_name,
        default=config.indexing.default_collection,
    )
    search.add_argument("--top-k", type=int, default=config.search.top_k)
    add_progress_args(search)

    fts = subparsers.add_parser(
        "fts", help="Chinese full-text search using zvec jieba FTS"
    )
    fts.add_argument("query")
    fts.add_argument(
        "--collection",
        type=valid_collection_name,
        default=config.indexing.default_collection,
    )
    fts.add_argument("--top-k", type=int, default=config.search.top_k)
    add_progress_args(fts)

    hybrid = subparsers.add_parser(
        "hybrid",
        help="fuse vector, FTS, and graph retrieval into one ranked evidence list",
    )
    hybrid.add_argument("query")
    hybrid.add_argument(
        "--collection",
        type=valid_collection_name,
        default=config.indexing.default_collection,
    )
    hybrid.add_argument("--top-k", type=int, default=config.search.top_k)
    add_progress_args(hybrid)

    fetch = subparsers.add_parser("fetch", help="fetch one stored chunk by id")
    fetch.add_argument("chunk_id")
    fetch.add_argument(
        "--collection",
        type=valid_collection_name,
        default=config.indexing.default_collection,
    )
    add_progress_args(fetch)

    graph_index = subparsers.add_parser(
        "graph-index",
        help="extract a local JSON knowledge graph from stored chunks",
    )
    graph_index.add_argument(
        "--collection",
        type=valid_collection_name,
        default=config.indexing.default_collection,
    )
    graph_index.add_argument(
        "--limit",
        type=int,
        default=0,
        help="extract only the first N eligible chunks",
    )
    graph_index.add_argument("--llm-model", default=config.ollama.generation_model)
    graph_index.add_argument(
        "--num-predict",
        type=int,
        default=config.graph.num_predict,
        help="maximum generated tokens per chunk for Ollama graph extraction",
    )
    graph_index.add_argument(
        "--retries",
        type=int,
        default=config.graph.retries,
        help="retry failed graph extraction this many times per chunk",
    )
    graph_index.add_argument("--rebuild", action="store_true")
    add_progress_args(graph_index)

    graph_query = subparsers.add_parser(
        "graph-query",
        help="query the local JSON knowledge graph",
    )
    graph_query.add_argument("query")
    graph_query.add_argument(
        "--collection",
        type=valid_collection_name,
        default=config.indexing.default_collection,
    )
    add_progress_args(graph_query)

    analyze = subparsers.add_parser(
        "analyze",
        help="analyze complete chapters and print the stored analysis",
    )
    analyze.add_argument(
        "--collection",
        type=valid_collection_name,
        default=config.indexing.default_collection,
    )
    analyze.add_argument("--llm-model", default=config.ollama.generation_model)
    analyze.add_argument(
        "--num-predict",
        type=int,
        default=config.analysis.num_predict,
        help="maximum generated tokens per chapter for Ollama chapter analysis",
    )
    analyze.add_argument(
        "--retries",
        type=int,
        default=config.analysis.retries,
        help="retry failed chapter analysis this many times per chapter",
    )
    analyze.add_argument("--rebuild", action="store_true")
    add_progress_args(analyze)

    return parser


def add_progress_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-chapter",
        type=int,
        help="only use chunks fully contained at or before the Nth detected chapter",
    )
    parser.add_argument(
        "--max-line",
        type=int,
        help="only use chunks whose ending line is at or before this line",
    )
    parser.add_argument(
        "--max-chunk-index",
        type=int,
        help="only use chunks whose chunk_index is at or below this value",
    )


def command_index(args: argparse.Namespace, config: ReadFellowConfig) -> int:
    if not args.source.is_file():
        print(f"source file not found: {args.source}", file=sys.stderr)
        return 2

    result = index_document(
        config,
        args.source,
        args.collection,
        options=IndexDocumentOptions(
            chunk_chars=args.chunk_chars,
            overlap_chars=args.overlap_chars,
            batch_size=args.batch_size,
            limit=args.limit,
            rebuild=args.rebuild,
            optimize=not args.no_optimize,
        ),
        on_progress=print_index_progress,
    )
    print(
        f"done: collection={result.collection}, chunks={result.chunk_count}, "
        f"inserted={result.inserted}, skipped={result.skipped}"
    )
    return 0


def command_search(args: argparse.Namespace, config: ReadFellowConfig) -> int:
    result = semantic_search(
        config,
        args.query,
        args.collection,
        top_k=args.top_k,
        progress=progress_limit_from_args(args),
    )
    print_progress(result.progress)
    print_evidence(result.evidence)
    return 0


def command_fts(args: argparse.Namespace, config: ReadFellowConfig) -> int:
    result = fts_search(
        config,
        args.query,
        args.collection,
        top_k=args.top_k,
        progress=progress_limit_from_args(args),
    )
    print_progress(result.progress)
    print_evidence(result.evidence)
    return 0


def command_hybrid(args: argparse.Namespace, config: ReadFellowConfig) -> int:
    result = hybrid_search(
        config,
        args.query,
        args.collection,
        top_k=args.top_k,
        progress=progress_limit_from_args(args),
    )
    print_progress(result.progress)
    print_channels(result.channels)
    print_evidence(result.evidence)
    return 0


def command_fetch(args: argparse.Namespace, config: ReadFellowConfig) -> int:
    result = fetch_chunk_workflow(
        config,
        args.chunk_id,
        args.collection,
        progress=progress_limit_from_args(args),
    )
    if result.status == "not_found":
        print(f"chunk not found: {args.chunk_id}", file=sys.stderr)
        return 1
    if result.status == "outside_progress":
        print_progress(result.progress)
        print(
            f"chunk {args.chunk_id} is outside the configured reading progress",
            file=sys.stderr,
        )
        return 1

    if result.evidence is None:
        raise RuntimeError(f"fetch returned no evidence for {args.chunk_id}")
    print_progress(result.progress)
    print_evidence([result.evidence], full_text=True)
    return 0


def command_graph_index(args: argparse.Namespace, config: ReadFellowConfig) -> int:
    result = build_graph_workflow(
        config,
        args.collection,
        progress=progress_limit_from_args(args),
        options=GraphBuildOptions(
            limit=args.limit,
            llm_model=args.llm_model,
            num_predict=args.num_predict,
            retries=args.retries,
            rebuild=args.rebuild,
        ),
        on_progress=print_graph_progress,
    )
    if result.status == "empty":
        print(f"done: no chunks selected for collection={result.collection}")
        return 0
    if result.status == "up_to_date":
        print(
            f"done: graph is already up to date for collection={result.collection}, "
            f"selected_chunks={result.selected_chunk_count}"
        )
        return 0

    print(
        f"done: collection={result.collection}, graph={result.graph_path}, "
        f"entities={result.entity_count}, relations={result.relation_count}"
    )
    return 0


def command_graph_query(args: argparse.Namespace, config: ReadFellowConfig) -> int:
    result = query_graph_workflow(
        config,
        args.query,
        args.collection,
        progress=progress_limit_from_args(args),
    )
    print_progress(result.progress)
    print_evidence(result.evidence)
    return 0


def command_analyze(args: argparse.Namespace, config: ReadFellowConfig) -> int:
    result = build_analysis_workflow(
        config,
        args.collection,
        progress=progress_limit_from_args(args),
        options=AnalysisBuildOptions(
            llm_model=args.llm_model,
            num_predict=args.num_predict,
            retries=args.retries,
            rebuild=args.rebuild,
        ),
        on_progress=print_analysis_progress,
    )
    print_chapter_analyses(result.chapters)
    print(
        f"\ndone: collection={result.collection}, analysis={result.analysis_path}, "
        f"status={result.status}"
    )
    return 0


def print_index_progress(event: IndexProgressEvent) -> None:
    if event.stage == "probe":
        print(f"probing embedding dimension with {event.model} ...", flush=True)
        return
    if event.stage == "optimize":
        print("optimizing collection ...", flush=True)
        return
    if event.stage == "batch" and event.skipped_existing:
        print(
            f"[{event.processed:>5}/{event.total}] skipped existing chunks",
            flush=True,
        )
        return
    if event.stage == "batch":
        print(
            f"[{event.processed:>5}/{event.total}] indexed {event.inserted}, "
            f"skipped {event.skipped}, {event.rate:.2f} chunks/s",
            flush=True,
        )


def print_graph_progress(event: GraphBuildEvent) -> None:
    if event.stage == "selected" and event.progress is not None:
        print_progress(event.progress)
        return
    if event.stage == "extracting":
        print(
            f"[{event.index:>5}/{event.total}] extracting graph from {event.chunk_id}",
            flush=True,
        )
        return
    if event.stage == "retry":
        print(
            f"        retry {event.attempt}/{event.retries}: {event.error}",
            flush=True,
        )
        return
    if event.stage == "extracted":
        print(
            f"        entities={event.entity_count},"
            f" relations={event.relation_count}"
            f"{_rejected_suffix(event.rejected_count)}",
            flush=True,
        )


def print_analysis_progress(event: AnalysisBuildEvent) -> None:
    if event.stage == "selected" and event.progress is not None:
        print_progress(event.progress)
        return
    if event.stage == "skipped":
        print(f"skipped {event.chapter_title}: {event.reason}", flush=True)
        return
    if event.stage == "analyzing":
        print(
            f"[{event.index:>5}/{event.total}] analyzing {event.chapter_title}",
            flush=True,
        )
        return
    if event.stage == "retry":
        print(
            f"        retry {event.attempt}/{event.retries}: {event.error}",
            flush=True,
        )
        return
    if event.stage == "analyzed":
        print(
            f"        characters={event.character_count},"
            f" events={event.event_count}"
            f"{_rejected_suffix(event.rejected_count)}",
            flush=True,
        )


def _rejected_suffix(rejected_count: int) -> str:
    """How many items were dropped for quoting nothing, shown only when some were."""
    return f", rejected={rejected_count}" if rejected_count else ""


def print_chapter_analyses(chapters: list[ChapterAnalysis]) -> None:
    if not chapters:
        print("no chapter analysis available")
        return
    for chapter in chapters:
        title = chapter.chapter_title or "(no chapter)"
        print(
            f"\n[{chapter.chapter_index}] {title} "
            f"{chapter.source_path}:{chapter.line_start}-{chapter.line_end} "
            f"chunks={len(chapter.chunk_ids)}"
        )
        summary = chapter.summary or "(withheld: chapter extends past the progress)"
        print(f"summary: {summary}")
        for mention in chapter.characters:
            role = f" — {mention.role_in_chapter}" if mention.role_in_chapter else ""
            print(f"  character: {mention.name}{role}")
            print(f"    {chunk_provenance(mention)} {one_line(mention.evidence)}")
        for event in chapter.events:
            print(f"  event {event.order}: {event.description}")
            print(f"    {chunk_provenance(event)} {one_line(event.evidence)}")


def one_line(text: str) -> str:
    """Evidence is stored as the source's own span, line breaks and indent included."""
    return re.sub(r"\s+", " ", text).strip()


def chunk_provenance(context: ChunkContext) -> str:
    return f"{context.source_path}:{context.line_start}-{context.line_end}"


def print_evidence(items: list[Evidence], *, full_text: bool = False) -> None:
    if not items:
        print("no results")
        return
    for index, evidence in enumerate(items, start=1):
        chapter = evidence.chapter or "(no chapter)"
        score = f" score={evidence.score:.6f}" if evidence.score is not None else ""
        print(
            f"\n[{index}] id={evidence.chunk_id}{score} "
            f"{evidence.source_path}:{evidence.line_start}-{evidence.line_end}"
        )
        if evidence.matches:
            print(
                "matched: "
                + ", ".join(f"{match.mode}#{match.rank}" for match in evidence.matches)
            )
        print(f"chapter: {chapter}")
        if evidence.graph_context is not None:
            if evidence.graph_context.entities:
                print("graph entities: " + ", ".join(evidence.graph_context.entities))
            for relation in evidence.graph_context.relations:
                print(f"graph relation: {relation}")
        text = evidence.text.rstrip()
        if not full_text:
            text = re.sub(r"\s+", " ", text)[:260]
        print(text)


def progress_limit_from_args(args: argparse.Namespace) -> ProgressLimit:
    return ProgressLimit(
        max_chapter=getattr(args, "max_chapter", None),
        max_line=getattr(args, "max_line", None),
        max_chunk_index=getattr(args, "max_chunk_index", None),
    )


def print_progress(progress: ProgressFilter) -> None:
    if progress.description:
        print(f"progress limit: {progress.description}", file=sys.stderr)


def print_channels(channels: list[ChannelStatus]) -> None:
    parts = [
        f"{channel.mode}=skipped ({channel.skipped_reason})"
        if channel.skipped_reason is not None
        else f"{channel.mode}={channel.candidates}"
        for channel in channels
    ]
    print("channels: " + ", ".join(parts), file=sys.stderr)


def config_path_from_argv(argv: list[str] | None) -> Path:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", type=Path, default=CONFIG_FILE)
    args, _ = parser.parse_known_args(argv)
    return args.config


def apply_global_overrides(
    config: ReadFellowConfig,
    args: argparse.Namespace,
) -> ReadFellowConfig:
    effective = config.model_copy(deep=True)
    effective.paths.index_dir = args.index_dir
    effective.paths.metadata_dir = args.metadata_dir
    effective.ollama.base_url = args.ollama_url
    effective.ollama.embedding_model = args.model
    effective.ollama.keep_alive = args.keep_alive
    return effective


def main(argv: list[str] | None = None) -> int:
    config_path = config_path_from_argv(argv)
    try:
        config = load_config(config_path, missing_ok=config_path == CONFIG_FILE)
    except Exception as exc:  # noqa: BLE001
        print(f"error loading config: {exc}", file=sys.stderr)
        return 1

    parser = build_parser(config)
    args = parser.parse_args(argv)
    config = apply_global_overrides(config, args)
    try:
        if args.command == "index":
            return command_index(args, config)
        if args.command == "search":
            return command_search(args, config)
        if args.command == "fts":
            return command_fts(args, config)
        if args.command == "hybrid":
            return command_hybrid(args, config)
        if args.command == "fetch":
            return command_fetch(args, config)
        if args.command == "graph-index":
            return command_graph_index(args, config)
        if args.command == "graph-query":
            return command_graph_query(args, config)
        if args.command == "analyze":
            return command_analyze(args, config)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
