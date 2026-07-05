from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
import time

from .chunking import chunk_document
from .graph import (
    build_extraction_prompt,
    empty_graph,
    graph_path,
    merge_extraction,
    parse_graph_extraction,
    processed_chunk_ids,
    query_graph,
    read_chunks,
    read_graph,
    update_graph_metadata,
    write_graph,
)
from .ollama import OllamaEmbedder, OllamaGenerator
from .models import (
    ChunkContext,
    GraphQueryResult,
    GraphRelation,
    IndexManifest,
    QueryChunkFields,
)
from .progress import ProgressFilter, build_progress_filter
from .store import (
    chunk_to_doc,
    collection_path,
    fetch_chunk,
    open_or_create_collection,
    query_fts,
    query_vector,
    read_manifest,
    write_manifest,
)


DEFAULT_COLLECTION = "sample"
DEFAULT_MODEL = "qwen3-embedding:8b"
DEFAULT_LLM_MODEL = "qwen3:8b"


def valid_collection_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]", "_", value.strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if len(normalized) < 2:
        raise argparse.ArgumentTypeError(
            "collection name must contain at least two letters, numbers, or underscores"
        )
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="readfellow")
    parser.add_argument("--index-dir", type=Path, default=Path("indexes"))
    parser.add_argument("--metadata-dir", type=Path, default=Path("metadata"))
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--keep-alive", default="30m")

    subparsers = parser.add_subparsers(dest="command", required=True)

    index = subparsers.add_parser("index", help="chunk and index a UTF-8 text document")
    index.add_argument("source", type=Path)
    index.add_argument("--collection", type=valid_collection_name, default=DEFAULT_COLLECTION)
    index.add_argument("--chunk-chars", type=int, default=2400)
    index.add_argument("--overlap-chars", type=int, default=240)
    index.add_argument("--batch-size", type=int, default=8)
    index.add_argument("--limit", type=int, default=0, help="index only the first N chunks")
    index.add_argument("--rebuild", action="store_true")
    index.add_argument(
        "--no-optimize",
        action="store_true",
        help="skip final zvec optimize; faster indexing, but persisted FTS may not be queryable",
    )

    search = subparsers.add_parser("search", help="semantic vector search")
    search.add_argument("query")
    search.add_argument("--collection", type=valid_collection_name, default=DEFAULT_COLLECTION)
    search.add_argument("--top-k", type=int, default=5)
    add_progress_args(search)

    fts = subparsers.add_parser("fts", help="Chinese full-text search using zvec jieba FTS")
    fts.add_argument("query")
    fts.add_argument("--collection", type=valid_collection_name, default=DEFAULT_COLLECTION)
    fts.add_argument("--top-k", type=int, default=5)
    add_progress_args(fts)

    fetch = subparsers.add_parser("fetch", help="fetch one stored chunk by id")
    fetch.add_argument("chunk_id")
    fetch.add_argument("--collection", type=valid_collection_name, default=DEFAULT_COLLECTION)
    add_progress_args(fetch)

    graph_index = subparsers.add_parser(
        "graph-index",
        help="extract a local JSON knowledge graph from stored chunks",
    )
    graph_index.add_argument(
        "--collection",
        type=valid_collection_name,
        default=DEFAULT_COLLECTION,
    )
    graph_index.add_argument(
        "--limit",
        type=int,
        default=0,
        help="extract only the first N eligible chunks",
    )
    graph_index.add_argument("--llm-model", default=DEFAULT_LLM_MODEL)
    graph_index.add_argument("--rebuild", action="store_true")
    add_progress_args(graph_index)

    graph_query = subparsers.add_parser(
        "graph-query",
        help="query the local JSON knowledge graph",
    )
    graph_query.add_argument("query")
    graph_query.add_argument("--collection", type=valid_collection_name, default=DEFAULT_COLLECTION)
    add_progress_args(graph_query)

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


def command_index(args: argparse.Namespace) -> int:
    source = args.source
    if not source.is_file():
        print(f"source file not found: {source}", file=sys.stderr)
        return 2

    relative_source = str(source)
    try:
        relative_source = str(source.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        relative_source = str(source.resolve())

    chunks = chunk_document(
        source,
        source_path=relative_source,
        target_chars=args.chunk_chars,
        overlap_chars=args.overlap_chars,
    )
    if args.limit:
        chunks = chunks[: args.limit]
    if not chunks:
        print("no chunks produced", file=sys.stderr)
        return 1

    embedder = OllamaEmbedder(
        base_url=args.ollama_url,
        model=args.model,
        keep_alive=args.keep_alive,
    )
    print(f"probing embedding dimension with {args.model} ...", flush=True)
    probe_vector = embedder.embed_one(chunks[0].text)
    dimension = len(probe_vector)

    coll = open_or_create_collection(
        index_dir=args.index_dir,
        metadata_dir=args.metadata_dir,
        collection=args.collection,
        dimension=dimension,
        rebuild=args.rebuild,
    )

    manifest = IndexManifest(
        collection=args.collection,
        collection_path=str(collection_path(args.index_dir, args.collection)),
        source_path=relative_source,
        model=args.model,
        embedding_dimension=dimension,
        chunk_count=len(chunks),
        chunk_chars=args.chunk_chars,
        overlap_chars=args.overlap_chars,
    )
    write_manifest(
        metadata_dir=args.metadata_dir,
        collection=args.collection,
        manifest=manifest,
        chunks=chunks,
    )

    start = time.monotonic()
    inserted = 0
    skipped = 0
    for offset in range(0, len(chunks), args.batch_size):
        batch = chunks[offset : offset + args.batch_size]
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
        if not pending:
            print(
                f"[{offset + len(batch):>5}/{len(chunks)}] skipped existing chunks",
                flush=True,
            )
            continue

        texts = [chunk.text for chunk in pending]
        vectors = embedder.embed(texts)
        docs = [
            chunk_to_doc(chunk, vector, model=args.model)
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
        print(
            f"[{offset + len(batch):>5}/{len(chunks)}] indexed {inserted}, "
            f"skipped {skipped}, {rate:.2f} chunks/s",
            flush=True,
        )

    if not args.no_optimize:
        print("optimizing collection ...", flush=True)
        coll.optimize()
    coll.flush()
    print(
        f"done: collection={args.collection}, chunks={len(chunks)}, "
        f"inserted={inserted}, skipped={skipped}"
    )
    return 0


def print_docs(docs) -> None:
    if not docs:
        print("no results")
        return
    for index, doc in enumerate(docs, start=1):
        fields = QueryChunkFields.model_validate(doc.fields)
        chapter = fields.chapter or "(no chapter)"
        text = fields.text.strip()
        preview = re.sub(r"\s+", " ", text)[:260]
        print(
            f"\n[{index}] id={doc.id} score={doc.score:.6f} "
            f"{fields.source_path}:{fields.line_start}-{fields.line_end}"
        )
        print(f"chapter: {chapter}")
        print(preview)


def command_search(args: argparse.Namespace) -> int:
    manifest = read_manifest(metadata_dir=args.metadata_dir, collection=args.collection)
    coll = open_or_create_existing(args)
    progress = progress_from_args(args, manifest=manifest)
    embedder = OllamaEmbedder(
        base_url=args.ollama_url,
        model=manifest.model or args.model,
        keep_alive=args.keep_alive,
    )
    vector = embedder.embed_one(args.query)
    print_progress(progress)
    print_docs(query_vector(coll, vector, top_k=args.top_k, filter=progress.expression))
    return 0


def command_fts(args: argparse.Namespace) -> int:
    manifest = read_manifest(metadata_dir=args.metadata_dir, collection=args.collection)
    coll = open_or_create_existing(args)
    progress = progress_from_args(args, manifest=manifest)
    print_progress(progress)
    print_docs(query_fts(coll, args.query, top_k=args.top_k, filter=progress.expression))
    return 0


def command_fetch(args: argparse.Namespace) -> int:
    manifest = read_manifest(metadata_dir=args.metadata_dir, collection=args.collection)
    coll = open_or_create_existing(args)
    progress = progress_from_args(args, manifest=manifest)
    doc = fetch_chunk(coll, args.chunk_id)
    if doc is None:
        print(f"chunk not found: {args.chunk_id}", file=sys.stderr)
        return 1
    fields = QueryChunkFields.model_validate(doc.fields)
    if not progress.allows(fields):
        print_progress(progress)
        print(
            f"chunk {args.chunk_id} is outside the configured reading progress",
            file=sys.stderr,
        )
        return 1
    print_progress(progress)
    print(
        f"id={doc.id} {fields.source_path}:{fields.line_start}-"
        f"{fields.line_end}"
    )
    if fields.chapter:
        print(f"chapter: {fields.chapter}")
    print()
    print(fields.text.rstrip())
    return 0


def command_graph_index(args: argparse.Namespace) -> int:
    manifest = read_manifest(metadata_dir=args.metadata_dir, collection=args.collection)
    progress = progress_from_args(args, manifest=manifest)
    print_progress(progress)

    chunks = [
        chunk
        for chunk in read_chunks(metadata_dir=args.metadata_dir, collection=args.collection)
        if progress.allows(chunk)
    ]
    if args.limit:
        chunks = chunks[: args.limit]

    path = graph_path(args.metadata_dir, args.collection)
    if path.exists() and not args.rebuild:
        graph = read_graph(path)
    else:
        graph = empty_graph(
            collection=args.collection,
            manifest=manifest,
            llm_model=args.llm_model,
        )

    processed = set() if args.rebuild else processed_chunk_ids(graph)
    pending = [chunk for chunk in chunks if chunk.id not in processed]
    update_graph_metadata(
        graph,
        collection=args.collection,
        manifest=manifest,
        llm_model=args.llm_model,
        progress=progress,
        selected_chunk_count=len(chunks),
    )

    if not chunks:
        write_graph(path, graph)
        print(f"done: no chunks selected for collection={args.collection}")
        return 0

    if not pending:
        write_graph(path, graph)
        print(
            f"done: graph is already up to date for collection={args.collection}, "
            f"selected_chunks={len(chunks)}"
        )
        return 0

    generator = OllamaGenerator(
        base_url=args.ollama_url,
        model=args.llm_model,
        keep_alive=args.keep_alive,
    )

    for index, chunk in enumerate(pending, start=1):
        chunk_id = chunk.id
        print(f"[{index:>5}/{len(pending)}] extracting graph from {chunk_id}", flush=True)
        prompt = build_extraction_prompt(chunk)
        try:
            raw = generator.generate_json(prompt)
            extraction = parse_graph_extraction(raw, chunk)
        except Exception as exc:
            raise RuntimeError(f"failed to extract graph for chunk {chunk_id}: {exc}") from exc

        merge_extraction(graph, extraction, chunk)
        update_graph_metadata(
            graph,
            collection=args.collection,
            manifest=manifest,
            llm_model=args.llm_model,
            progress=progress,
            selected_chunk_count=len(chunks),
        )
        write_graph(path, graph)
        print(
            f"        entities={len(extraction.entities)}, "
            f"relations={len(extraction.relations)}",
            flush=True,
        )

    print(
        f"done: collection={args.collection}, graph={path}, "
        f"entities={graph.entity_count}, relations={graph.relation_count}"
    )
    return 0


def command_graph_query(args: argparse.Namespace) -> int:
    manifest = read_manifest(metadata_dir=args.metadata_dir, collection=args.collection)
    progress = progress_from_args(args, manifest=manifest)
    path = graph_path(args.metadata_dir, args.collection)
    if not path.is_file():
        raise FileNotFoundError(f"graph index not found: {path}; run graph-index first")

    graph = read_graph(path)
    result = query_graph(graph, args.query, progress=progress)
    print_progress(progress)
    print_graph_results(result)
    return 0


def print_graph_results(result: GraphQueryResult) -> None:
    entities = result.entities
    relations = result.relations
    if not entities and not relations:
        print("no graph results")
        return

    if entities:
        print("entities:")
        for entity in entities:
            suffixes = []
            if entity.types:
                suffixes.append("types=" + ", ".join(entity.types))
            if entity.aliases:
                suffixes.append("aliases=" + ", ".join(entity.aliases))
            suffix = f" ({'; '.join(suffixes)})" if suffixes else ""
            print(f"- {entity.name}{suffix}")
            for mention in entity.mentions[:3]:
                print(f"  mention: {format_graph_location(mention)}")
            for evidence in entity.evidence[:2]:
                preview = re.sub(r"\s+", " ", evidence.text)[:180]
                print(f"  evidence: {preview} ({format_graph_location(evidence)})")

    if relations:
        if entities:
            print()
        print("relations:")
        for index, relation in enumerate(relations, start=1):
            print(
                f"[{index}] {relation.subject} --{relation.relation}--> "
                f"{relation.object}"
            )
            if relation.evidence:
                preview = re.sub(r"\s+", " ", relation.evidence)[:220]
                print(f"    evidence: {preview}")
            print(
                f"    chunk: {relation.chunk_id} "
                f"{format_graph_location(relation)}"
            )


def format_graph_location(item: ChunkContext | GraphRelation) -> str:
    chapter = item.chapter or "(no chapter)"
    return (
        f"{item.source_path}:{item.line_start}-"
        f"{item.line_end} chapter={chapter}"
    )


def open_or_create_existing(args: argparse.Namespace):
    path = collection_path(args.index_dir, args.collection)
    if not path.exists():
        raise FileNotFoundError(
            f"collection does not exist: {path}; run the index command first"
        )
    import zvec

    return zvec.open(str(path), zvec.CollectionOption(read_only=True, enable_mmap=True))


def progress_from_args(
    args: argparse.Namespace,
    *,
    manifest: IndexManifest | None,
) -> ProgressFilter:
    return build_progress_filter(
        manifest=manifest,
        max_chapter=getattr(args, "max_chapter", None),
        max_line=getattr(args, "max_line", None),
        max_chunk_index=getattr(args, "max_chunk_index", None),
    )


def print_progress(progress: ProgressFilter) -> None:
    if progress.description:
        print(f"progress limit: {progress.description}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "index":
            return command_index(args)
        if args.command == "search":
            return command_search(args)
        if args.command == "fts":
            return command_fts(args)
        if args.command == "fetch":
            return command_fetch(args)
        if args.command == "graph-index":
            return command_graph_index(args)
        if args.command == "graph-query":
            return command_graph_query(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
