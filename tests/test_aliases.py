import json
from types import SimpleNamespace

import pytest

from readfellow import app, cli


@pytest.fixture
def alias_book(book, monkeypatch):
    config, source = book
    source.write_text(
        "第一章 启程\n向山提灯。\n阿杰·周留在屋里。\n周关上门。\n"
        "第二章 重逢\n铁拳本名向山。\n“向山”的山。\n众人散去。\n"
        "第三章 来信\n山·赵署了名。\n来信很短。",
        encoding="utf-8",
        newline="\r\n",
    )
    app.index_document(config, source, "books")
    app.set_progress(config, "books")
    config.analysis.backend = "openai"
    judged = []
    rejected = set()

    def generate(prompt, schema):
        fields = schema["properties"]
        if "same" in fields:
            judged.append(prompt)
            return json.dumps(
                {
                    "same": not any(name in prompt for name in rejected),
                    "reason": "按引文判定。",
                }
            )
        if "summary" in fields:
            return json.dumps({"summary": "本章结束。", "characters": [], "tags": []})
        identities = []
        if "第一章 启程" in prompt:
            names = ["向山", "阿杰·周", "周"]
            evidence = "向山提灯。"
        elif "第二章 重逢" in prompt:
            names = ["山"]
            evidence = "“向山”的山。"
            identities = [
                {"alias": "铁拳", "name": "向山", "evidence": "铁拳本名向山。"}
            ]
        else:
            names = ["山·赵"]
            evidence = "山·赵署了名。"
        moment = {
            "characters": names,
            "kind": "其他",
            "description": "众人在场。",
            "trait": "",
            "importance": 1,
            "evidence": evidence,
        }
        return json.dumps({"moments": [moment], "identities": identities})

    monkeypatch.setattr(
        app,
        "OpenAICompatGenerator",
        lambda **kw: SimpleNamespace(generate_json=generate),
    )
    monkeypatch.setattr(cli, "load_config", lambda *a, **kw: config)
    return config, source, judged, rejected


def query(capsys, name):
    assert cli.main(["aliases", name, "--collection", "books", "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_aliases_cite_all_three_sources_and_resume(alias_book, capsys):
    config, source, judged, _ = alias_book
    result = app.analyze(config, "books")
    assert (result.links.candidates, result.links.judged, result.links.accepted) == (
        3,
        3,
        3,
    )
    assert len(judged) == 3
    assert all("向山提灯。" not in p and "阿杰·周留在屋里。" not in p for p in judged)
    links = query(capsys, " 铁拳 ")["links"]
    expected = [
        ("铁拳", "向山", 2, "第二章 重逢", 6, "铁拳本名向山。"),
        ("山", "向山", 2, "第二章 重逢", 7, "“向山”的山。"),
        ("山", "山·赵", 3, "第三章 来信", 10, "山·赵署了名。"),
    ]
    assert links == [
        {
            "alias": a,
            "name": b,
            "ordinal": n,
            "title": title,
            "source_path": str(source),
            "line_start": line,
            "line_end": line,
            "evidence": quote,
        }
        for a, b, n, title, line, quote in expected
    ]
    judged.clear()
    again = app.analyze(config, "books", chapters=(1, 1))
    assert (again.links.cached, again.links.judged) == (3, 0)
    assert judged == []


def test_aliases_hide_future_and_partial_chapters(alias_book, capsys):
    config, *_ = alias_book
    app.analyze(config, "books")
    for limit in (8, 10):
        app.set_progress(config, "books", line=limit)
        hidden = query(capsys, "山·赵")
        missing = query(capsys, "不存在的人")
        assert hidden.pop("name") == "山·赵"
        assert missing.pop("name") == "不存在的人"
        assert hidden == missing
        assert hidden["links"] == []
        assert len(query(capsys, "铁拳")["links"]) == 2


def test_rejected_alias_does_not_connect(alias_book, capsys):
    config, _, _, rejected = alias_book
    rejected.add("山·赵")
    result = app.analyze(config, "books")
    assert (result.links.judged, result.links.accepted) == (3, 2)
    assert query(capsys, "山·赵")["links"] == []
    assert len(query(capsys, "山")["links"]) == 2
