"""Tryb automatyczny: ewig (atrapa) -> potok -> kalendarz (atrapa)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests
from conftest import DEFAULT_GROUP, NEW_GROUP, make_csv, row
from fake_calendar import FakeCalendarApi
from fake_ewig import FakeEwig

from gcalsync import cli
from gcalsync.auto import (
    EXIT_OK,
    EXIT_SAFETY_STOP,
    load_auto_config,
    markdown_summary,
    run_auto,
)
from gcalsync.sources.ewig import EwigClient
from gcalsync.storage import ConfigError

REPO_CONFIG = Path(__file__).resolve().parent.parent / "gcalsync.config.json"
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
FILES = {"WIG23IX2S1": NEW_GROUP.read_bytes(), "WIG23IX1S1": DEFAULT_GROUP.read_bytes()}


def ewig_client(files=FILES) -> EwigClient:
    session = requests.Session()
    session.mount("https://", FakeEwig(files))
    return EwigClient("login", "sekret", session=session, sleep=lambda _s: None)


@pytest.fixture
def api():
    return FakeCalendarApi()


@pytest.fixture
def config(api, tmp_path):
    cal = api.create_calendar("Plan WAT", "Europe/Warsaw", "x")
    data = json.loads(REPO_CONFIG.read_text("utf-8"))
    data["calendar"]["id"] = cal["id"]
    data["auto_apply"] = False  # testy włączają zapis cykliczny same, gdy go potrzebują
    path = tmp_path / "gcalsync.config.json"
    path.write_text(json.dumps(data), "utf-8")
    return load_auto_config(path)


def run(config, api, *, apply, files=FILES, **kwargs):
    return run_auto(
        config,
        ewig_client(files),
        lambda: api,
        apply=apply,
        now=lambda: NOW,
        sleep=lambda _s: None,
        log=lambda _m: None,
        **kwargs,
    )


def test_repo_config_is_valid():
    config = load_auto_config(REPO_CONFIG)
    assert [g.code for g in config.groups] == ["WIG23IX2S1", "WIG23IX1S1"]
    assert config.semester_iid == 20261
    assert config.rules[0].value == "Modelowanie danych do BIM"
    assert config.auto_apply is True  # włączone po udanym dry-runie i ręcznym zapisie


def test_dry_run_writes_nothing(config, api):
    result = run(config, api, apply=False)
    assert result.exit_code == EXIT_OK
    assert len(result.plan.adds) == 75
    assert "Dry-run" in result.headline
    assert api.writes == 0


def test_apply_then_next_run_has_no_changes(config, api, tmp_path):
    result = run(config, api, apply=True, save_dir=tmp_path / "out")
    assert result.exit_code == EXIT_OK
    assert result.execution.done == 75
    assert result.remaining == []
    assert (tmp_path / "out" / "files" / "WIG23IX2S1.csv").read_bytes() == FILES["WIG23IX2S1"]
    assert list((tmp_path / "out" / "runs").glob("*.jsonl"))
    summaries = {e["summary"] for e in api.events[config.calendar.id].values()}
    assert not any("BIM" in s for s in summaries)

    again = run(config, api, apply=True)
    assert again.plan.operation_count == 0
    assert "brak zmian" in again.headline


def test_existing_events_from_local_sync_are_recognized(config, api):
    # Kalendarz wypełniony wcześniej lokalnym `gcalsync sync --apply` (te same nazwy źródeł).
    run(config, api, apply=True)
    assert run(config, api, apply=False).plan.unchanged == 75


def test_mass_delete_stops_unless_allowed(config, api):
    run(config, api, apply=True)
    only_new = {
        "WIG23IX2S1": FILES["WIG23IX2S1"],
        # Plik grupy domyślnej z jednymi zajęciami, żeby przeszedł walidację, ale „zgubił” resztę.
        "WIG23IX1S1": make_csv(
            row("Seminarium dyplomowe (S) [1]", "2026-10-01 09:50", "2026-10-01 11:25", "18 58")
        ),
    }
    stopped = run(config, api, apply=True, files=only_new)
    assert stopped.exit_code == EXIT_SAFETY_STOP
    assert "bezpiecznik" in stopped.headline
    assert len(api.events[config.calendar.id]) == 75

    allowed = run(config, api, apply=True, files=only_new, allow_mass_delete=True)
    assert allowed.exit_code == EXIT_OK
    assert len(api.events[config.calendar.id]) < 75


def test_missing_calendar_is_error(config, api):
    api.calendars.clear()
    with pytest.raises(ConfigError, match="nie istnieje"):
        run(config, api, apply=False)


def test_markdown_summary(config, api):
    text = markdown_summary(run(config, api, apply=False))
    assert text.startswith("## ✅ gcalsync")
    assert "| 75 | 0 | 0 |" in text
    assert "… i 15 więcej" in text  # 75 zmian, limit 60


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"version": 2}, "wersja"),
        ({"ewig": {"semester_iid": 20261, "groups": []}}, "pusta"),
        ({"rules": [{"field": "x", "op": "equals", "value": "y"}]}, "Błąd"),
        ({"calendar": None}, "Błąd"),
    ],
)
def test_invalid_config(tmp_path, patch, message):
    data = json.loads(REPO_CONFIG.read_text("utf-8")) | patch
    path = tmp_path / "c.json"
    path.write_text(json.dumps(data), "utf-8")
    with pytest.raises(ConfigError, match=message):
        load_auto_config(path)


# --- CLI ----------------------------------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch, api, config, tmp_path):
    """CLI `auto` z atrapami ewig i Google w miejsce prawdziwych klientów."""
    path = tmp_path / "gcalsync.config.json"
    monkeypatch.setattr(cli, "EwigClient", lambda login, password: ewig_client())
    monkeypatch.setattr(cli, "GoogleCalendarApi", lambda credentials: api)
    monkeypatch.setattr(cli, "credentials_from_json", lambda token: None)
    return path


def test_cli_apply_if_enabled_respects_config(cli_env, api, capsys, tmp_path):
    summary = tmp_path / "summary.md"
    code = cli.main(
        ["auto", "--config", str(cli_env), "--apply-if-enabled", "--summary", str(summary)],
        sleep=lambda _s: None,
    )
    assert code == 0
    assert "auto_apply = false" in capsys.readouterr().out
    assert api.writes == 0
    assert "Dry-run" in summary.read_text("utf-8")

    data = json.loads(cli_env.read_text("utf-8"))
    data["auto_apply"] = True
    cli_env.write_text(json.dumps(data), "utf-8")
    assert (
        cli.main(["auto", "--config", str(cli_env), "--apply-if-enabled"], sleep=lambda _s: None)
        == 0
    )
    assert api.writes == 75


def test_cli_errors_are_reported_with_exit_3(monkeypatch, cli_env, tmp_path, capsys):
    monkeypatch.setattr(cli, "EwigClient", EwigClient)  # prawdziwy klient, brak sekretów
    monkeypatch.delenv("EWIG_LOGIN", raising=False)
    summary = tmp_path / "s.md"
    assert cli.main(["auto", "--config", str(cli_env), "--summary", str(summary)]) == 3
    assert "EWIG_LOGIN" in summary.read_text("utf-8")
    assert "## ❌" in summary.read_text("utf-8")


def test_cli_missing_config_is_exit_2(tmp_path, capsys):
    assert cli.main(["auto", "--config", str(tmp_path / "brak.json")]) == 2
