import json
from types import SimpleNamespace

import pytest

from readfellow import app, cli
from readfellow.openai_compat import GenerationRejected


@pytest.fixture
def summaries(readable_book, monkeypatch):
    config, source = readable_book
    config.analysis.backend = "openai"
    config.analysis.retries = 1
    config.analysis.concurrency = 2
    calls = []
    fake = SimpleNamespace(answer=lambda prompt: ("修理机器。", ["尤基"]))

    def generate(prompt, schema):
        if "moments" in schema["properties"]:
            return json.dumps({"moments": [], "identities": []})
        calls.append(prompt)
        summary, characters = fake.answer(prompt)
        return json.dumps(
            {"summary": summary, "characters": characters, "tags": ["成长"]}
        )

    monkeypatch.setattr(
        app,
        "OpenAICompatGenerator",
        lambda **kwargs: SimpleNamespace(generate_json=generate),
    )
    monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: config)
    return config, source, fake, calls


def run(*args):
    return cli.main([*args, "--collection", "books"])


def test_analyze_does_not_retry_generation_rejected(summaries, monkeypatch, capsys):
    calls = []

    def reject(prompt, schema):
        calls.append("summary" if "summary" in schema["properties"] else "moments")
        raise GenerationRejected("terminal response")

    monkeypatch.setattr(
        app,
        "OpenAICompatGenerator",
        lambda **kwargs: SimpleNamespace(generate_json=reject),
    )
    assert run("analyze", "--chapters", "1") == 1
    lines = capsys.readouterr().out.splitlines()
    assert "summary\t1\tGenerationRejected" in lines
    assert "moments\t1\tGenerationRejected" in lines
    assert calls == ["summary", "moments"]


def test_analyze_resumes_from_committed_notes(summaries, capsys):
    _, _, _, calls = summaries
    assert run("analyze") == 0
    first = capsys.readouterr()
    assert first.out.splitlines() == [
        "summary  chapters 2  cached 0  written 2  failed 0  spoiler 0",
        "moments  chapters 2  cached 0  written 2  failed 0  hidden 0",
        "links  candidates 0  cached 0  judged 0  failed 0  accepted 0",
    ]
    assert {line.split()[1] for line in first.err.splitlines()} == {"1", "2"}
    assert run("analyze") == 0
    assert capsys.readouterr().out.splitlines() == [
        "summary  chapters 2  cached 2  written 0  failed 0  spoiler 0",
        "moments  chapters 2  cached 2  written 0  failed 0  hidden 0",
        "links  candidates 0  cached 0  judged 0  failed 0  accepted 0",
    ]
    assert len(calls) == 2


def test_toc_range_and_partial_summary(summaries, capsys):
    config, _, _, _ = summaries
    app.analyze(config, "books")
    app.set_progress(config, "books", line=1)
    assert run("toc", "--chapters", "1", "--json") == 0
    (entry,) = json.loads(capsys.readouterr().out)["chapters"]
    assert entry["partial"] and entry["summary"] is None and entry["tags"] == []
    app.set_progress(config, "books")
    assert run("toc", "--chapters", "1") == 0
    (line,) = capsys.readouterr().out.splitlines()
    assert line.startswith("1\t") and line.endswith("\t修理机器。\t成长")


def test_find_chapter_rows_include_summary(summaries, capsys):
    config, _, _, _ = summaries
    app.analyze(config, "books")
    assert run("find", "尤基", "--limit", "0") == 0
    assert capsys.readouterr().out.splitlines()[1:] == [
        "1\t第一章 开始\t1\t修理机器。",
        "2\t第二章 之后\t1\t修理机器。",
    ]


def test_q15_retries_character_and_reports_exhaustion(summaries, capsys):
    config, _, fake, calls = summaries
    names = iter(["秘密", "尤基"])
    fake.answer = lambda prompt: ("修理机器。", [next(names)])
    assert run("analyze", "--chapters", "1") == 0
    assert len(calls) == 2
    config.openai.generation_model = "second-config"
    fake.answer = lambda prompt: ("修理机器。", ["秘密"])
    assert run("analyze", "--chapters", "1") == 1
    assert len(calls) == 4
    out = capsys.readouterr().out
    assert (
        "written 0  failed 1" in out and "1\tcharacter appears after chapter end" in out
    )
    assert app.collection_status(config, "books").summaries == (0, 0)


def test_q15_scan_hides_earlier_summary(summaries):
    config, _, fake, _ = summaries
    fake.answer = lambda prompt: (
        "尤基发现秘密。",
        ["秘密"] if "第二章" in prompt else ["尤基"],
    )
    app.analyze(config, "books", chapters=(1, 1))
    result = app.analyze(config, "books", chapters=(2, 2))
    assert result.spoiler == 1
    earlier, later = app.table_of_contents(config, "books").chapters
    assert earlier.summary is None and earlier.tags == []
    assert later.summary == "尤基发现秘密。"


def test_summary_cache_tracks_chapter_body_across_rebuild(summaries, capsys):
    config, source, _, _ = summaries
    app.analyze(config, "books")
    options = app.IndexDocumentOptions(chunk_chars=1200, rebuild=True)
    app.index_document(config, source, "books", options=options)
    assert run("status") == 0
    assert "summaries: 2/2, pending 0, spoiler 0" in capsys.readouterr().out
    source.write_text(source.read_text().replace("机器", "零件"))
    app.index_document(config, source, "books", options=options)
    assert run("status") == 0
    assert "summaries: 1/2, pending 1, spoiler 0" in capsys.readouterr().out
