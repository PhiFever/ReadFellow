import json
import re

import pytest

from readfellow import app
from readfellow.cli import main


def test_cli_fts_then_fetch_prints_persisted_source_evidence(book, capsys):
    config, source = book
    args = ["--library-dir", str(config.paths.library_dir)]
    assert main([*args, "index", str(source), "--collection", "books"]) == 0
    assert main([*args, "progress", "set", "--all", "--collection", "books"]) == 0
    capsys.readouterr()
    limit = ["--collection", "books", "--max-chapter", "1:1"]

    for command, query in (("fts", '"尤基"-:'), ("search", "修理"), ("hybrid", "尤基")):
        assert main([*args, command, query, *limit]) == 0
        output = capsys.readouterr().out
        assert f"{source}:1-2" in output
        assert "尤基修好了机器" in output
        assert "秘密" not in output
        assert output.count("id=") == 1
        if command == "fts":
            chunk_id = re.search(r"id=(\S+)", output)[1]

    assert main([*args, "fetch", chunk_id, *limit]) == 0
    output = capsys.readouterr().out
    assert "第一章 开始\n尤基修好了机器。" in output
    assert f"{source}:1-2" in output


def test_cli_fts_refuses_after_source_is_edited(book, capsys):
    config, source = book
    args = ["--library-dir", str(config.paths.library_dir)]
    assert main([*args, "index", str(source)]) == 0
    assert main([*args, "progress", "set", "1:1"]) == 0
    source.write_text(
        "卷首语\n" * 30 + source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    query = [*args, "fts", "尤基", "--max-chapter", "1:1"]
    assert main(query) != 0
    output = capsys.readouterr()
    assert "source file hash changed" in output.err
    assert "秘密" not in output.out
    assert main([*args, "status"]) == 0
    output = capsys.readouterr().out
    assert "index:       2/2 docs" in output
    assert "⚠ chunk metadata" in output

    source.unlink()
    assert main(query) == 0
    output = capsys.readouterr().out
    assert "尤基修好了机器" in output
    assert "秘密" not in output


@pytest.fixture
def indexed_cli(book):
    config, source = book
    app.index_document(config, source, "books")

    def run(*args):
        return main(
            [
                "--library-dir",
                str(config.paths.library_dir),
                *args,
                "--collection",
                "books",
            ]
        )

    return run


def test_read_commands_require_saved_progress(indexed_cli, capsys):
    for args in (
        ["toc"],
        ["read", "--lines", "1"],
        ["find", "尤基"],
        ["fts", "尤基"],
        ["search", "尤基"],
        ["hybrid", "尤基"],
        ["fetch", "id"],
    ):
        assert indexed_cli(*args) != 0
        output = capsys.readouterr()
        assert "progress set" in output.err
        assert output.out == ""


def test_read_clips_at_saved_line(indexed_cli, capsys):
    assert indexed_cli("progress", "set", "--line", "3") == 0
    capsys.readouterr()
    assert indexed_cli("read", "--chapters", "2") == 0
    output = capsys.readouterr()
    assert output.out.splitlines() == ["     3\t第二章 之后"]
    assert "clipped to line 3" in output.err


def test_find_counts_only_visible_matching_lines(indexed_cli, capsys):
    assert indexed_cli("progress", "set", "--line", "3") == 0
    capsys.readouterr()
    assert indexed_cli("find", "尤基", "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert (result["total_lines"], result["total_chapters"]) == (1, 1)
    assert [c["count"] for c in result["chapters"]] == [1]
    assert [h["snippet"] for h in result["hits"]] == ["尤基修好了机器。"]


def test_one_time_limit_cannot_extend_saved_progress(indexed_cli, capsys):
    assert indexed_cli("progress", "set", "1:1") == 0
    for flag, value in (("--max-line", "3"), ("--max-chapter", "1:2")):
        assert indexed_cli("toc", flag, value) != 0
        assert "progress set" in capsys.readouterr().err


def test_rebuild_preserves_and_invalidates_progress(book, indexed_cli, capsys):
    config, source = book
    assert indexed_cli("progress", "set", "--all") == 0
    source.write_text(source.read_text(encoding="utf-8") + "尾声\n", encoding="utf-8")
    app.index_document(
        config, source, "books", options=app.IndexDocumentOptions(rebuild=True)
    )
    assert indexed_cli("toc") != 0
    err = capsys.readouterr().err
    assert "progress is stale" in err and "progress set" in err
