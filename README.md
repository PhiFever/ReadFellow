# ReadFellow

ReadFellow is an experimental local workflow for indexing large text documents
with zvec and Ollama embeddings so agents can retrieve relevant original
passages efficiently.

## Quick Start

ReadFellow loads project defaults from the root `config.yaml`. CLI flags such as
`--index-dir`, `--metadata-dir`, `--ollama-url`, `--model`, `--keep-alive`,
`--collection`, `--chunk-chars`, `--overlap-chars`, `--batch-size`, `--top-k`,
and graph extraction settings override that file for a single run.

Smoke-test indexing with only the first few chunks:

```sh
uv run readfellow index corpus/samples/<document>.txt --collection sample --rebuild --limit 8
```

Index a full sample document:

```sh
uv run readfellow index corpus/samples/<document>.txt --collection sample --rebuild
```

The index command writes zvec data to `indexes/`, writes provenance metadata to
`metadata/`, and runs a final `zvec.optimize()` so persisted Chinese FTS remains
queryable after reopening the collection.

Run semantic search:

```sh
uv run readfellow search "要查询的问题" --collection sample --top-k 5
```

Run Chinese full-text search:

```sh
uv run readfellow fts "关键词" --collection sample --top-k 5
```

Avoid spoilers by constraining all retrieval to the reader's progress. If the
reader has finished only the first 50 detected chapters:

```sh
uv run readfellow search "要查询的问题" --collection sample --max-chapter 50
uv run readfellow fts "关键词" --collection sample --max-chapter 50
uv run readfellow fetch <chunk-id> --collection sample --max-chapter 50
```

`--max-chapter N` excludes chunks that cross into chapter `N + 1`. Lower-level
guards are also available: `--max-line` and `--max-chunk-index`.

Fetch a chunk by id:

```sh
uv run readfellow fetch <chunk-id> --collection sample
```

Build a lightweight local knowledge graph from the stored chunks. This reads
`metadata/<collection>/chunks.jsonl`, asks an Ollama generation model to extract
entities and relations, and writes an auditable JSON graph to
`metadata/<collection>/graph.json`:

```sh
uv run readfellow graph-index --collection sample --llm-model qwen3:8b --limit 20
```

Query the graph by entity name, alias, or relation keyword:

```sh
uv run readfellow graph-query "向山" --collection sample
uv run readfellow graph-query "武神" --collection sample --max-chapter 10
```

`graph-index` also supports `--rebuild`, `--max-chapter`, `--max-line`, and
`--max-chunk-index`, so graph extraction can follow the same spoiler limits as
search, FTS, and fetch. Extracted evidence must occur verbatim in its source
chunk. The graph records its prompt version, extraction settings, and source
chunk hashes. A source/chunk mismatch must be re-indexed; stale graph metadata is
rejected by `graph-query` and rebuilt on the next `graph-index` run.

The default embedding endpoint and models are configured in `config.yaml`.

Generated indexes and manifests are written to `indexes/` and `metadata/`.

## Evidence Flow

The reusable workflows in `readfellow.app` return a shared `Evidence` model for
vector search, FTS, fetch, and graph queries. Each item contains the original
chunk text together with its chunk id, source path, line range, chapter,
chunk index, byte range, text hash, retrieval mode, and an optional score. Graph
queries attach matched entities and relations as context while keeping the
stored original chunk as the evidence.

`fetch_chunk` returns an explicit `found`, `not_found`, or `outside_progress`
status. An out-of-progress result never includes the chunk text.
