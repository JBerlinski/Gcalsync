import json

import pytest
from conftest import DEFAULT_GROUP, NEW_GROUP, make_csv, row

from gcalsync import __version__
from gcalsync.cli import main

SAMPLE_ARGS = [
    "preview",
    str(NEW_GROUP),
    str(DEFAULT_GROUP),
    "--names",
    "Grupa kierunkowa",
    "Grupa domyślna",
]


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    assert "preview" in capsys.readouterr().out


def test_preview_samples_with_bim_rule(capsys, tmp_path):
    out_json = tmp_path / "podglad.json"
    code = main(
        [*SAMPLE_ARGS, "--exclude-course", "Modelowanie danych do BIM", "--json", str(out_json)]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "=== Do kalendarza (75) ===" in out
    assert "=== Wykluczone przez reguły (30) ===" in out
    assert "1. Grupa kierunkowa" in out
    assert "nic nie zostało zapisane" in out
    data = json.loads(out_json.read_text("utf-8"))
    assert data["summary"]["events"] == 75
    assert data["sources"][0]["name"] == "Grupa kierunkowa"


def test_preview_samples_without_rule_shows_conflicts(capsys):
    assert main(SAMPLE_ARGS) == 0
    out = capsys.readouterr().out
    assert "=== Konflikty rozwiązane priorytetem (7) ===" in out


def test_preview_default_names_are_made_unique(capsys, tmp_path):
    a = tmp_path / "a" / "plan.txt"
    b = tmp_path / "b" / "plan.txt"
    for path in (a, b):
        path.parent.mkdir()
    a.write_bytes(make_csv(row("A (w) [1]")))
    b.write_bytes(make_csv(row("B (w) [1]", "2026-10-02 08:00", "2026-10-02 09:35")))
    assert main(["preview", str(a), str(b)]) == 0
    out = capsys.readouterr().out
    assert "1. plan.txt" in out
    assert "2. plan.txt #2" in out


def test_preview_file_errors_give_exit_code_1(capsys, tmp_path):
    bad = tmp_path / "zly.csv"
    bad.write_bytes(make_csv(row("X (w) [1]", "2026-10-01 10:00", "2026-10-01 09:00")))
    assert main(["preview", str(bad)]) == 1
    assert "BŁĄD" in capsys.readouterr().out


def test_names_count_mismatch_is_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["preview", str(NEW_GROUP), "--names", "a", "b"])
    assert exc.value.code == 2


def test_invalid_regex_is_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["preview", str(NEW_GROUP), "--exclude-regex", "(zły"])
    assert exc.value.code == 2
    assert "wyrażenie regularne" in capsys.readouterr().err


def test_missing_file_is_usage_error(capsys, tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["preview", str(tmp_path / "brak.csv")])
    assert exc.value.code == 2
