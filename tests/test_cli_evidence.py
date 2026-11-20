import re

from readfellow.cli import main


def test_cli_fts_then_fetch_prints_persisted_source_evidence(book, capsys):
    config, source = book
    args = ["--library-dir", str(config.paths.library_dir)]
    assert main([*args, "index", str(source), "--collection", "books"]) == 0
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
