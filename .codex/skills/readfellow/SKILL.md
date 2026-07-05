---
name: readfellow
description: Use this skill when working in the ReadFellow repository to index, search, fetch, or discuss local long-form text corpora through the project's uv-managed readfellow CLI, zvec indexes, and Ollama embeddings. Trigger for requests about reading the indexed corpus, semantic search, Chinese FTS, chunk provenance, rebuilding indexes, non-spoiler reading progress limits, or answering questions from original document passages.
---

# ReadFellow

## Purpose

Use this skill to work with ReadFellow's local document index. The core rule is: retrieval is navigation, original chunks are evidence. Always fetch or inspect the returned chunk text before making content claims.

## Commands

Run project Python commands through `uv`.

```sh
uv run readfellow search "query text" --collection sample --top-k 5
uv run readfellow fts "keyword" --collection sample --top-k 5
uv run readfellow fetch <chunk-id> --collection sample
uv run readfellow search "query text" --collection sample --max-chapter 50
uv run readfellow index corpus/samples/<document>.txt --collection sample --rebuild
uv run pytest
```

Defaults:

- Ollama endpoint: `http://127.0.0.1:11434`
- Embedding model: `qwen3-embedding:8b`
- Index output: `indexes/`
- Metadata output: `metadata/`

## Workflow

1. Check whether the user has declared reading progress before searching.
2. For broad or semantic questions, run `uv run readfellow search ...`.
3. For exact names, terms, or phrases, run `uv run readfellow fts ...` or `rg` on the source file.
4. Fetch the best chunk ids with `uv run readfellow fetch ...`.
5. Answer from fetched text, citing `source_path:line_start-line_end` and chapter when available.
6. Clearly label inference or interpretation separately from text facts.

## Spoiler Control

If the user says they have read only up to a point, apply the same progress limit to every retrieval and fetch command.

- Use `--max-chapter N` when the user says they read through chapter N.
- Use `--max-line N` when the user gives a line boundary.
- Use `--max-chunk-index N` only for low-level debugging or exact tool handoff.
- Do not run an unbounded search, FTS query, or fetch after a progress limit is known.
- Do not mention that later matches exist outside the limit.
- If a chunk is rejected as outside progress, ask for permission before searching beyond the declared point.

Example:

```sh
uv run readfellow search "尤基的新爸爸是谁" --collection sample --max-chapter 50
uv run readfellow fts "新爸爸" --collection sample --max-chapter 50
uv run readfellow fetch bdd935754e17_000000 --collection sample --max-chapter 50
```

`--max-chapter N` means the Nth detected chapter heading in source order. The CLI excludes chunks whose ending line crosses into chapter N+1, so boundary-crossing chunks are not used.

## Indexing Notes

- Use `--limit 8` for a smoke test before a full rebuild.
- Full indexing calls `zvec.optimize()` by default. Keep this unless intentionally testing raw write speed; persisted Chinese FTS may not work correctly after reopen without optimize.
- If the embedding model changes, rebuild the index.
- Generated `indexes/` and `metadata/` are local artifacts and normally ignored by git.

## Answering Rules

- Do not rely only on vector scores for factual answers.
- Prefer exact wording from fetched chunks for plot events, character actions, chronology, and terminology.
- Keep quotes short and cite the chunk location.
- If retrieval is weak or contradictory, say so and run a narrower search.
