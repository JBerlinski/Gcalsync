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
from gcalsync.state import is_state_event
from gcalsync.storage import ConfigError

REPO_CONFIG = Path(__file__).resolve().parent.parent / "gcalsync.config.json"
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
FILES = {"WIG23IX2S1": NEW_GROUP.read_bytes(), "WIG23IX1S1": DEFAULT_GROUP.read_bytes()}


def classes(api, calendar_id) -> list[dict]:
    """Zdarzenia zajęć w kalendarzu (bez technicznego zdarzenia ze stanem)."""
    return [e for e in api.events[calendar_id].values() if not is_state_event(e)]


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
    assert len(classes(api, config.calendar.id)) == 75

    allowed = run(config, api, apply=True, files=only_new, allow_mass_delete=True)
    assert allowed.exit_code == EXIT_OK
    assert len(classes(api, config.calendar.id)) < 75


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
    assert api.writes == 76  # 75 zajęć + zdarzenie ze stanem (lista usuniętych ręcznie)


def test_cli_errors_are_reported_with_exit_3(monkeypatch, cli_env, tmp_path, capsys):
    monkeypatch.setattr(cli, "EwigClient", EwigClient)  # prawdziwy klient, brak sekretów
    monkeypatch.delenv("EWIG_LOGIN", raising=False)
    summary = tmp_path / "s.md"
    assert cli.main(["auto", "--config", str(cli_env), "--summary", str(summary)]) == 3
    assert "EWIG_LOGIN" in summary.read_text("utf-8")
    assert "## ❌" in summary.read_text("utf-8")


def test_cli_missing_config_is_exit_2(tmp_path, capsys):
    assert cli.main(["auto", "--config", str(tmp_path / "brak.json")]) == 2


# --- opis: prowadzący zamiast tematu ------------------------------------------------------


def test_description_has_teacher_and_no_raw_subject(config, api):
    run(config, api, apply=True)
    event = next(
        e for e in classes(api, config.calendar.id) if e["summary"] == "Analizy teledetekcyjne (w)"
    )
    assert "Prowadzący: dr inż. Analizy Wykład" in event["description"]
    assert "Temat w planie" not in event["description"]


# --- ręczne zmiany w Kalendarzu Google ----------------------------------------------------


def find(api, config, summary, start):
    """Zdarzenie zajęć po tytule i początku (czas lokalny, np. „2026-10-01T09:50”)."""
    return next(
        e
        for e in classes(api, config.calendar.id)
        if e["summary"] == summary and e["start"]["dateTime"].startswith(start)
    )


def move(event, start, end):
    """Przeniesienie zdarzenia tak, jak zrobiłby to użytkownik w Kalendarzu Google."""
    event["start"] = {"dateTime": f"{start}:00+02:00", "timeZone": "Europe/Warsaw"}
    event["end"] = {"dateTime": f"{end}:00+02:00", "timeZone": "Europe/Warsaw"}


SEMINAR = ("Seminarium dyplomowe (S)", "2026-10-01T09:50")


def test_manually_moved_class_stays_where_it_was_moved(config, api):
    run(config, api, apply=True)
    move(find(api, config, *SEMINAR), "2026-10-02T12:00", "2026-10-02T13:35")

    again = run(config, api, apply=True)
    assert again.plan.operation_count == 0
    moved = find(api, config, "Seminarium dyplomowe (S)", "2026-10-02T12:00")
    assert moved["end"]["dateTime"].startswith("2026-10-02T13:35")
    [keep] = again.plan.manual_keeps
    assert keep.manual == ["czas"]
    assert "✋" in markdown_summary(again)


def test_manual_note_in_description_is_kept_but_room_still_follows_plan(config, api, tmp_path):
    run(config, api, apply=True)
    event = find(api, config, *SEMINAR)
    event["description"] = "Przynieść laptopa"
    # Dziekanat zmienia salę tych zajęć.
    new_default = DEFAULT_GROUP.read_bytes().replace(
        b"Seminarium dyplomowe (S) [1],18 58", b"Seminarium dyplomowe (S) [1],99 99"
    )
    files = {**FILES, "WIG23IX1S1": new_default}
    result = run(config, api, apply=True, files=files)
    assert result.exit_code == EXIT_OK
    stored = find(api, config, *SEMINAR)
    assert stored["description"] == "Przynieść laptopa"
    assert stored["location"] == "99 99"


def test_value_set_back_to_plan_ends_protection(config, api):
    run(config, api, apply=True)
    original = find(api, config, *SEMINAR)
    start, end = original["start"], original["end"]
    move(original, "2026-10-02T12:00", "2026-10-02T13:35")
    run(config, api, apply=True)
    moved = find(api, config, "Seminarium dyplomowe (S)", "2026-10-02T12:00")
    moved["start"], moved["end"] = start, end  # użytkownik cofa przeniesienie
    result = run(config, api, apply=True)
    assert result.plan.manual_keeps == []


def test_dean_moving_class_to_manual_slot_adopts_event(config, api):
    run(config, api, apply=True)
    move(find(api, config, *SEMINAR), "2026-10-01T08:00", "2026-10-01T09:35")
    run(config, api, apply=True)
    # Dziekanat wpisuje ten sam nowy termin (plik nadal ułożony chronologicznie).
    new_default = DEFAULT_GROUP.read_bytes().replace(
        b"Seminarium dyplomowe (S) [1],18 58,2026-10-01,09:50,2026-10-01,11:25",
        b"Seminarium dyplomowe (S) [1],18 58,2026-10-01,08:00,2026-10-01,09:35",
    )
    files = {**FILES, "WIG23IX1S1": new_default}
    result = run(config, api, apply=True, files=files)
    assert result.plan.adds == [] and result.plan.deletes == []
    [update] = result.plan.updates
    assert update.adopted
    seminars = [
        e
        for e in classes(api, config.calendar.id)
        if e["start"]["dateTime"].startswith("2026-10-01T08:00")
    ]
    assert len(seminars) == 1
    # Od teraz to zwykłe zajęcia z planu: brak ochrony, brak zmian.
    again = run(config, api, apply=True, files=files)
    assert again.plan.operation_count == 0 and again.plan.manual_keeps == []


def test_manually_moved_class_removed_from_plan_is_kept(config, api):
    run(config, api, apply=True)
    move(find(api, config, *SEMINAR), "2026-10-02T12:00", "2026-10-02T13:35")
    run(config, api, apply=True)
    without = b"".join(
        line
        for line in DEFAULT_GROUP.read_bytes().splitlines(keepends=True)
        if not line.startswith(b"Seminarium dyplomowe (S) [1],")
    )
    result = run(config, api, apply=True, files={**FILES, "WIG23IX1S1": without})
    assert result.plan.deletes == []
    [keep] = result.plan.manual_keeps
    assert keep.reason == "zmienione ręcznie, brak w planie"
    assert find(api, config, "Seminarium dyplomowe (S)", "2026-10-02T12:00")


def test_manually_deleted_class_is_not_added_again(config, api):
    run(config, api, apply=True)
    event = find(api, config, *SEMINAR)
    del api.events[config.calendar.id][event["id"]]

    dry = run(config, api, apply=False)  # dry-run wykrywa, ale niczego nie zapisuje
    assert dry.plan.adds == [] and len(dry.plan.newly_deleted) == 1
    assert dry.state_saved is False

    first = run(config, api, apply=True)
    assert first.plan.adds == [] and first.state_saved
    assert "Usunięte ręcznie" in markdown_summary(first)
    for _ in range(2):  # pamiętane także w kolejnych uruchomieniach
        later = run(config, api, apply=True)
        assert later.plan.operation_count == 0
        assert len(later.plan.deleted_by_user) == 1 and later.plan.newly_deleted == []


def test_restore_deleted_brings_classes_back(config, api):
    run(config, api, apply=True)
    event = find(api, config, *SEMINAR)
    del api.events[config.calendar.id][event["id"]]
    run(config, api, apply=True)

    restored = run(config, api, apply=True, restore_deleted=True)
    assert len(restored.plan.adds) == 1
    assert find(api, config, *SEMINAR)
    assert run(config, api, apply=True).plan.deleted_by_user == []


def test_events_from_before_tracking_are_overwritten_and_migrated(config, api):
    run(config, api, apply=True)
    for e in classes(api, config.calendar.id):
        del e["extendedProperties"]["private"]["gcalsync_h"]
    event = find(api, config, *SEMINAR)
    event["location"] = "stara sala"
    result = run(config, api, apply=True)
    assert len(result.plan.updates) == 75  # znacznik dopisany wszystkim
    assert find(api, config, *SEMINAR)["location"] == "18 58"
    assert run(config, api, apply=True).plan.operation_count == 0


def test_state_event_is_not_counted_as_foreign(config, api):
    run(config, api, apply=True)
    result = run(config, api, apply=True)
    assert result.plan.unmanaged == []
