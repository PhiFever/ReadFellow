from readfellow.artifacts import StoredRun
from readfellow.artifacts import open_artifacts


def replace_run(config, kind, document):
    store = open_artifacts(config)
    latest = store.latest("books", kind)
    source_id, _, _ = store.read_source("books")
    if latest is None:
        latest, _ = store.prepare(
            source_id, document, stale=lambda _: None, rebuild=False
        )
    store.save(StoredRun(latest.id, source_id, document))
