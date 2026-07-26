# AGENTS.md

## Project Purpose

This is an experimental repository for making large or newly acquired documents easier for agents to read. The intended workflow is to vectorize documents that are inconvenient to search with plain `grep` or are unlikely to be covered by model training data, then let agents retrieve relevant passages efficiently and discuss them with the user against the original text.

Treat source documents as the ground truth. Vector indexes, summaries, and chunk metadata are derived artifacts and must not replace verification against the original file.

## Zvec Reference

Use `gh` to inspect upstream zvec documentation when needed:

```sh
gh repo view alibaba/zvec --json nameWithOwner,description,url,latestRelease,repositoryTopics
gh api repos/alibaba/zvec/contents/README_CN.md --jq '.content' | base64 -d
```

As checked during initialization on 2026-07-05, `alibaba/zvec` describes itself as a lightweight, fast, in-process vector database. Its README documents Python, Node.js, Go, Rust, and Dart/Flutter SDKs. For Python, upstream documents the package as `zvec` for Python 3.10-3.14; in this repository, install and run it through `uv`. The upstream repository reports latest release `v0.5.1` published on 2026-06-24.

Relevant zvec capabilities for this project:

- Local in-process vector database; no separate database server is required.
- Dense and sparse vector retrieval.
- Full-text search and hybrid retrieval.
- Durable local storage through WAL.
- Multiple readers can open a collection concurrently; writes are single-process exclusive.

## Repository Conventions

- Keep raw source documents immutable unless the user explicitly asks to edit or normalize them.
- Store generated indexes, caches, embeddings, and temporary build products outside the raw text files. Prefer clearly named ignored directories such as `.zvec/`, `.cache/`, or `indexes/` once a `.gitignore` exists.
- Do not commit API keys, model credentials, or local endpoint secrets. Use environment variables such as `OPENAI_API_KEY` or `DASHSCOPE_API_KEY` when embedding providers require them.
- Preserve enough metadata per chunk to recover the original passage: source path, byte offsets when practical, line range, chapter/title if known, chunk index, text hash, and the original chunk text or a direct way to fetch it.
- For Chinese long-form text, prefer chapter-aware chunking first, then bounded chunks with modest overlap. Avoid transformations that would make offsets or quotes drift from the raw file.

## Python Workflow

Use `uv` for Python dependency management and Python command execution.

- Add dependencies with `uv add ...`.
- Run Python modules with `uv run python -m ...`.
- Run scripts with `uv run python scripts/<name>.py ...`.
- Run tests with `uv run pytest`.
- Do not use bare `pip`, `python`, or `python3` for project workflows when a `uv` equivalent is available.
- If the repository does not have a Python project scaffold yet, create one with `uv init` before adding Python dependencies.

Expected first dependency for indexing work:

```sh
uv add zvec
```

## Suggested Directory Layout

Use this layout as the repository grows:

```text
.
├── AGENTS.md
├── corpus/
│   ├── raw/          # Source documents used as ground truth.
│   └── samples/      # Small test/sample documents managed by the user.
├── indexes/          # Local zvec collections and generated retrieval indexes.
├── metadata/         # Chunk manifests, source maps, hashes, and statistics.
├── mcp/              # MCP server and tool definitions, if added.
├── scripts/          # Reproducible indexing, inspection, and maintenance scripts.
├── src/              # Shared library code.
└── tests/            # Automated tests for chunking, indexing, and retrieval.
```

Generated directories such as `indexes/` and `.cache/` should be ignored by git unless the user explicitly wants to version a small deterministic fixture.

## Agent Reading Rules

- Retrieval is a navigation aid. Before making claims about plot, wording, characters, or chronology, reread the corresponding original passage or exact stored chunk text.
- When answering the user about document contents, cite the source filename and the most precise available location, such as chapter name, line range, or chunk id.
- Distinguish clearly between facts present in the source text and interpretations or inferences.
- Use exact search tools such as `rg` for literal names or phrases when that is more reliable than semantic retrieval.
- For broad thematic or fuzzy questions, use vector or hybrid retrieval first, then verify against the raw text.

## Future MCP Tool Shape

If this repository grows MCP tooling around zvec, keep the tools narrow and source-grounded:

- `index_document`: chunk a raw document and build/update the zvec collection.
- `semantic_search`: return candidate chunks with scores and source metadata.
- `hybrid_search`: combine vector search, full-text search, and structured filters.
- `fetch_chunk`: return original text for a chunk id.
- `fetch_range`: return original text by file and byte or line range.

Tool responses should include enough provenance for an agent to inspect the original content before answering.
