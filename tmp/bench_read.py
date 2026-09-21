"""问题 3 · 失效指纹对纯运行时旋钮是否敏感（敏感 = 会触发整图重建）。
问题 4 · 一次检索要从库里读回多少东西。

需要先跑过 bench_graph.py 生成图谱。

    uv run python tmp/bench_graph.py && uv run python tmp/bench_read.py
"""

import sys
import time

from bench_graph import OUT, manifest_for

from readfellow.artifacts import ArtifactStore
from readfellow.graph import empty_graph, graph_staleness_reason
from readfellow.models import DerivationSettings

print("=== 问题 3 · 失效指纹对 retries / num_ctx 的敏感性 ===")
manifest = manifest_for(0)
base = DerivationSettings(num_predict=4096, num_ctx=16384, retries=2)
graph = empty_graph(
    collection="bench",
    manifest=manifest,
    llm_model="qwen3:8b",
    extraction_settings=base,
)

for label, settings in [
    ("同一份设置", DerivationSettings(num_predict=4096, num_ctx=16384, retries=2)),
    (
        "只改 --retries 2 -> 3",
        DerivationSettings(num_predict=4096, num_ctx=16384, retries=3),
    ),
    (
        "只改 num_ctx 16384->32768",
        DerivationSettings(num_predict=4096, num_ctx=32768, retries=2),
    ),
    (
        "改 num_predict（应失效）",
        DerivationSettings(num_predict=2048, num_ctx=16384, retries=2),
    ),
]:
    reason = graph_staleness_reason(
        graph,
        [],
        collection="bench",
        source_path="corpus/x.txt",
        llm_model="qwen3:8b",
        llm_endpoint="",
        extraction_settings=settings,
    )
    print(
        f"  {label:<28} -> {'整图重建' if reason else '可续建':<8} ({reason or 'no change'})"
    )

db = OUT / "bench_graph.sqlite"
if not db.exists():
    print(f"\n跳过问题 4：先跑 `uv run python tmp/bench_graph.py` 生成 {db}")
    sys.exit(0)

print(f"\n=== 问题 4 · 检索路径要读回多少（沿用 {db.name}）===")
store = ArtifactStore.open(f"sqlite:///{db}")

t = time.perf_counter()
_, _, chunks = store.read_source("bench")
read_ms = (time.perf_counter() - t) * 1000
print(
    f"  read_source():  {read_ms:8.1f} ms  -> {len(chunks)} chunk 全文 "
    f"({sum(len(c.text) for c in chunks) / 1e6:.1f} M 字符)"
)

t = time.perf_counter()
run_ = store.latest("bench", "graph")
latest_ms = (time.perf_counter() - t) * 1000
doc = run_.document
anchors = sum(len(e.mentions) + len(e.evidence) for e in doc.entities)
print(
    f"  latest('graph'):{latest_ms:8.1f} ms  -> {len(doc.entities)} 实体 / "
    f"{len(doc.relations)} 关系 / {anchors} 锚点"
)
print("  ^ hybrid 每查一次就付一遍这两笔，只为给 top-5 结果挂标注")
store.close()
