---
name: readfellow
description: Answer questions about a ReadFellow-indexed corpus from original passages, with citations and within the user's reading progress. Use when discussing a book's content, reading chapters, searching or fetching passages, setting spoiler limits, or rebuilding an index.
---

# ReadFellow

## Purpose

Use this skill to discuss a book stored in a ReadFellow library. The core rule is: the stored original text is evidence; search results are only navigation. Read the actual lines before making content claims.

Each book lives in one SQLite file, `library/<collection>.db`. It holds the chapter table, the full chapter text, the chunks, the vectors, and the saved reading progress. Every reading command reads from this file. Do not read the source files under `corpus/` directly: they bypass the progress limit.

## Commands

Run project Python commands through `uv`. Every command takes `--collection <name>` (default from `config.yaml`).

```sh
uv run readfellow progress show --collection sample        # what the user has read
uv run readfellow progress set 1:50 --collection sample    # through volume 1, chapter 50
uv run readfellow progress set --line 88000 --collection sample
uv run readfellow progress set --all --collection sample   # finished the book
uv run readfellow toc --collection sample                  # chapters within progress
uv run readfellow read --chapters 12-14 --collection sample   # toc ordinals, numbered lines
uv run readfellow read --lines 9000-9400 --collection sample
uv run readfellow find "尤基" --collection sample --limit 20  # exact substring, per-chapter counts
uv run readfellow find "尤基" --chapters 2-40 --collection sample
uv run readfellow search "query text" --collection sample --top-k 5
uv run readfellow hybrid "query text" --collection sample --top-k 5
uv run readfellow fetch <chunk-id> --collection sample
uv run readfellow status --collection sample
```

`toc`, `read`, and `find` accept `--json`.

## Workflow

1. Run `progress show`. If progress is unset or stale, ask the user how far they have read and run `progress set` with that point. Do not set `--all` unless the user says they finished the book.
2. Run `toc` to see the chapters within progress. Chapter ranges in `read` and `find` use the ordinal in the first `toc` column, not the in-book chapter number, because in-book numbers repeat across volumes.
3. For names, items, and terms, run `find`. It is an exact substring match, so two-character names work, and its per-chapter counts show where to read.
4. Read the relevant chapters or lines with `read`. Output above 60000 characters is truncated; narrow the range when that happens.
5. For vague or thematic questions where you do not know where to look, use `search` or `hybrid`, then `fetch` or `read` the hits before relying on them.
6. Answer from the text you read. Cite `source_path:line` or a line range, and the chapter title. Label inference and interpretation separately from what the text says.

## Spoiler Control

The saved progress applies to every reading command (`toc`, `read`, `find`, `search`, `fts`, `hybrid`, `fetch`). Text beyond it is never returned.

- `progress set V:N` means the chapter titled 第N章 in volume V. Volumes are inferred from chapter numbers restarting at 1, and plain `N` works only when that number is unique. If `progress set` reports the number as ambiguous, confirm the chapter title with the user and use the `--line` value listed for that candidate. If the chapter is not found, ask the user for the title instead of guessing a nearby number.
- `--max-chapter`, `--max-line`, and `--max-chunk-index` on a single command can only narrow the saved progress, never widen it. Asking for more is an error.
- To read further, ask the user first. Run `progress set` only after they agree.
- Do not mention that matches exist beyond the user's progress.
- After the source file changes and the book is re-indexed, saved progress becomes stale and reading commands refuse to run. Confirm the point with the user again and run `progress set`.

## Indexing Notes

- `index <source> --collection <name>` builds or resumes a library. If it is interrupted, run the same command again: it embeds only the missing chunks. `status` shows `stored/expected` chunk counts.
- Use `--limit 8` for a smoke test before a full build.
- Changing the source file, the chunk settings, or the embedding model requires `index --rebuild`. A rebuild keeps the saved progress, though a changed source makes it stale.
- `index`, `search`, and `hybrid` need the local Ollama embedding model. `progress`, `toc`, `read`, `find`, and `fetch` do not.
- `library/` is a local artifact and is ignored by git.

## Answering Rules

- Do not rely on vector scores or search snippets for factual answers. Read the lines.
- Prefer exact wording from the text for plot events, character actions, chronology, and terminology.
- Keep quotes short and cite their location.
- If the evidence is weak or contradictory, say so and read more of the surrounding text.
