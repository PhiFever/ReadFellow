import json
from types import SimpleNamespace

import pytest

from readfellow import app, cli


def moment(evidence, **changes):
    return {
        "characters": ["尤基"],
        "kind": "成长",
        "description": "他学会了修理。",
        "trait": "",
        "importance": 3,
        "evidence": evidence,
        **changes,
    }


@pytest.fixture
def notebook(book, monkeypatch):
    config, source = book
    source.write_bytes(
        "第一章 开始\r\n尤基修机器。\r\n第二章 重逢\r\n老罗就是罗明。\r\n尤基点头。\r\n第三章 告别\r\n众人离开。".encode()
    )
    app.index_document(config, source, "books")
    app.set_progress(config, "books")
    config.analysis.backend = "openai"
    first = {"moments": [moment("尤基修机器")], "identities": []}
    second = {
        "moments": [moment("老罗就是罗明。\n尤基点头")],
        "identities": [{"alias": "老罗", "name": "罗明", "evidence": "老罗就是罗明"}],
    }
    calls = []

    def generate(prompt, schema):
        if "summary" in schema["properties"]:
            return json.dumps({"summary": "本章结束。", "characters": [], "tags": []})
        calls.append(prompt)
        if "第一章 开始" in prompt:
            return json.dumps(first)
        if "第二章 重逢" in prompt:
            return json.dumps(second)
        return json.dumps({"moments": [], "identities": []})

    monkeypatch.setattr(
        app,
        "OpenAICompatGenerator",
        lambda **kw: SimpleNamespace(generate_json=generate),
    )
    monkeypatch.setattr(cli, "load_config", lambda *a, **kw: config)
    return config, source, first, second, calls


def notes(capsys, chapters="1-3"):
    assert (
        cli.main(["notes", "--chapters", chapters, "--collection", "books", "--json"])
        == 0
    )
    return json.loads(capsys.readouterr().out)["chapters"]


def test_notes_cite_absolute_source_lines_and_resume(notebook, capsys):
    config, source, _, _, calls = notebook
    app.analyze(config, "books", chapters=(2, 2))
    (chapter,) = notes(capsys, "2")
    (event,) = chapter["moments"]
    (identity,) = chapter["identities"]
    assert (event["evidence"], event["line_start"], event["line_end"]) == (
        "老罗就是罗明。\r\n尤基点头",
        4,
        5,
    )
    assert (
        identity["alias"],
        identity["name"],
        identity["line_start"],
        identity["line_end"],
    ) == ("老罗", "罗明", 4, 4)
    assert event["source_path"] == identity["source_path"] == str(source)
    app.analyze(config, "books", chapters=(2, 2))
    assert len(calls) == 1


def test_late_character_rejects_only_that_moment(notebook, capsys):
    config, _, first, _, _ = notebook
    first["moments"].append(moment("尤基修机器", characters=["老罗"]))
    app.analyze(config, "books", chapters=(1, 1))
    (chapter,) = notes(capsys, "1")
    assert [m["characters"] for m in chapter["moments"]] == [["尤基"]]
    assert app.collection_status(config, "books").moments.name_rejected == 1


def test_identity_needs_both_names_in_evidence(notebook, capsys):
    config, _, _, second, _ = notebook
    second["identities"][0]["evidence"] = "老罗就是"
    app.analyze(config, "books", chapters=(2, 2))
    (chapter,) = notes(capsys, "2")
    assert chapter["identities"] == []
    assert app.collection_status(config, "books").moments.unanchored == 1


def test_scan_hides_moments_when_later_names_become_known(notebook, capsys):
    config, _, first, _, _ = notebook
    first["moments"] += [
        moment("尤基修机器", description="罗明来了。"),
        moment("尤基修机器", trait="信任老罗"),
    ]
    app.analyze(config, "books", chapters=(1, 1))
    assert len(notes(capsys, "1")[0]["moments"]) == 3
    app.analyze(config, "books", chapters=(2, 2))
    assert [m["description"] for m in notes(capsys, "1")[0]["moments"]] == [
        "他学会了修理。"
    ]
    assert app.collection_status(config, "books").moments.hidden == 2


def test_notes_emit_only_complete_chapters_within_saved_progress(notebook, capsys):
    config, *_ = notebook
    app.analyze(config, "books")
    app.set_progress(config, "books", line=4)
    assert [c["ordinal"] for c in notes(capsys)] == [1]
