"""Read and validate legacy artifacts, without changing their version metadata."""

import hashlib
from pathlib import Path

from .analysis import analysis_path, read_analysis
from .artifacts import ArtifactStore, ImportResult, fingerprint
from .models import ChunkContext
from .store import read_chunks, read_manifest


def import_collection(
    artifacts: ArtifactStore, metadata_dir: Path, collection: str
) -> ImportResult:
    manifest = read_manifest(metadata_dir=metadata_dir, collection=collection)
    chunks = read_chunks(metadata_dir=metadata_dir, collection=collection)
    analysis_file = analysis_path(metadata_dir, collection)
    analysis = read_analysis(analysis_file) if (analysis_file).is_file() else None
    if manifest.collection != collection or manifest.chunk_count != len(chunks):
        raise ValueError("legacy manifest does not match collection/chunk count")
    by_id = {c.id: c for c in chunks}
    if len(by_id) != len(chunks):
        raise ValueError("duplicate chunk id in legacy metadata")
    for chunk in chunks:
        if (
            chunk.source_path != manifest.source_path
            or hashlib.sha256(chunk.text.encode()).hexdigest() != chunk.text_hash
        ):
            raise ValueError(f"legacy chunk metadata mismatch: {chunk.id}")

    def check_context(context: ChunkContext, evidence: str = ""):
        chunk = by_id.get(context.chunk_id)
        if chunk is None:
            raise ValueError(
                f"legacy reference has no matching chunk: {context.chunk_id}"
            )
        if (
            context.source_path != chunk.source_path
            or context.chunk_index != chunk.chunk_index
            or context.chapter != chunk.chapter
            or not chunk.line_start
            <= context.line_start
            <= context.line_end
            <= chunk.line_end
            or not chunk.byte_start
            <= context.byte_start
            <= context.byte_end
            <= chunk.byte_end
            or (evidence and evidence not in chunk.text)
        ):
            raise ValueError(f"legacy provenance/evidence mismatch: {context.chunk_id}")

    if analysis and (
        analysis.collection != collection
        or analysis.source_path != manifest.source_path
    ):
        raise ValueError("legacy derivation collection/source mismatch")
    if analysis:
        for chunk_id, text_hash in analysis.chunk_text_hashes.items():
            if chunk_id not in by_id or by_id[chunk_id].text_hash != text_hash:
                raise ValueError(f"legacy analysis fingerprint mismatch: {chunk_id}")
        for chapter in analysis.chapters:
            if not chapter.chunk_ids or any(
                chunk_id not in by_id for chunk_id in chapter.chunk_ids
            ):
                raise ValueError(
                    f"legacy chapter references missing chunks: {chapter.chapter_title}"
                )
            for item in [*chapter.characters, *chapter.events]:
                if item.chunk_id not in chapter.chunk_ids:
                    raise ValueError(
                        f"legacy chapter item belongs to another chapter: {item.chunk_id}"
                    )
                check_context(item, item.evidence)
    digest = fingerprint(
        [
            manifest.model_dump(mode="json"),
            [c.model_dump(mode="json") for c in chunks],
            analysis.model_dump(mode="json") if analysis else None,
        ]
    )
    return artifacts.import_documents(digest, manifest, chunks, analysis)
