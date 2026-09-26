"""sample 集合：按当前源文件算出的章节边界，和索引时 chunk 的行号是否仍在同一坐标系。"""

from readfellow.artifacts import open_artifacts
from readfellow.config import load_config
from readfellow.progress import chapter_boundaries, source_from_manifest

cfg = load_config()
_, manifest, chunks = open_artifacts(cfg).read_source("sample")
src = source_from_manifest(manifest)
lines = src.read_text(encoding="utf-8").splitlines()
bounds = chapter_boundaries(src)
print("current lines", len(lines), "chapters", len(bounds))
print("indexed max line_end", max(c.line_end for c in chunks))
# 每个 chunk 的首行文本，是否与当前文件同一行号的文本一致
mismatch = []
for c in chunks:
    first = c.text.splitlines()[0] if c.text else ""
    cur = lines[c.line_start - 1] if c.line_start <= len(lines) else None
    if cur is None or cur.strip() != first.strip():
        mismatch.append((c.chunk_index, c.line_start, c.line_end))
print("chunks whose first line no longer matches:", len(mismatch))
print("first few:", mismatch[:5])
beyond = [c for c in chunks if c.line_start > len(lines)]
print("chunks entirely beyond current EOF:", len(beyond))
