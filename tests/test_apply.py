"""Etap 6: zapis do kalendarza — na atrapie Google (bez sieci)."""

import json

import pytest
from conftest import DEFAULT_GROUP, NEW_GROUP
from fake_calendar import FakeCalendarApi

from gcalsync.app import build_sync_preview
from gcalsync.cli import main
from gcalsync.gcal.client import GoogleApiError
from gcalsync.gcal.executor import (
    MAX_CONSECUTIVE_FAILURES,
    Journal,
    execute_plan,
    last_run,
    operations_for,
    run_warning,
)
from gcalsync.storage import Paths, load_config


@pytest.fixture
def paths(tmp_path):
    return Paths(tmp_path / "home")


@pytest.fixture
def api():
    return FakeCalendarApi()


def cli(paths, api, *argv, answers=()):
    replies = iter(answers)

    def ask(prompt):
        print(prompt, end="")
        return next(replies)

    return main(list(argv), paths=paths, api_factory=lambda _p: api, ask=ask, sleep=lambda _s: None)


@pytest.fixture
def configured(paths, api):
    for argv in (
        ["sources", "add", str(NEW_GROUP), "--name", "Grupa kierunkowa"],
        ["sources", "add", str(DEFAULT_GROUP), "--name", "Grupa domyślna"],
        ["rules", "add", "--course", "Modelowanie danych do BIM"],
        ["calendar", "create"],
    ):
        assert cli(paths, api, *argv) == 0
    [cal_id] = api.calendars
    return cal_id


def events(api, cal_id):
    return list(api.events[cal_id].values())


# --- pełny przepływ przez CLI -------------------------------------------------------------


def test_apply_adds_75_then_second_run_is_noop(capsys, paths, api, configured):
    capsys.readouterr()
    assert cli(paths, api, "sync", "--apply", answers=["tak"]) == 0
    out = capsys.readouterr().out
    assert "Powyższe zmiany zostaną zapisane dopiero po potwierdzeniu" in out
    assert "dodanie 75, zmiana 0, usunięcie 0? Wpisz „tak”" in out
    assert "[75/75] +" in out
    assert "Wykonano 75 z 75 operacji." in out
    assert "Weryfikacja: kalendarz jest zgodny z planem." in out
    stored = events(api, configured)
    assert len(stored) == 75
    assert {e["summary"] for e in stored} >= {"Geowizualizacja (L)", "Analizy teledetekcyjne (w)"}
    assert all(e["reminders"] == {"useDefault": True} for e in stored)

    assert cli(paths, api, "sync", "--apply") == 0  # bez pytania — nic do zrobienia
    assert "Kalendarz jest zgodny z planem — nic do zapisania." in capsys.readouterr().out
    assert run_warning(last_run(paths.runs)) is None


def test_answer_other_than_tak_cancels(capsys, paths, api, configured):
    assert cli(paths, api, "sync", "--apply", answers=["t"]) == 0
    assert "Anulowano — nic nie zapisano." in capsys.readouterr().out
    assert events(api, configured) == []
    assert not paths.runs.exists() or list(paths.runs.iterdir()) == []


def test_no_input_available_cancels(capsys, paths, api, configured):
    def eof(_prompt):
        raise EOFError

    code = main(["sync", "--apply"], paths=paths, api_factory=lambda _p: api, ask=eof)
    assert code == 0
    assert events(api, configured) == []


def test_apply_blocked_without_calendar(capsys, paths, api):
    cli(paths, api, "sources", "add", str(NEW_GROUP), "--name", "A")
    assert cli(paths, api, "sync", "--apply") == 2
    assert "Zapis zablokowany: kalendarz docelowy nie jest utworzony" in capsys.readouterr().err
    assert api.writes == 0


def test_apply_blocked_by_file_errors(capsys, paths, api, configured, tmp_path):
    bad = tmp_path / "zly.csv"
    bad.write_bytes(
        "Temat,Data rozpoczęcia,Czas rozpoczęcia,Data zakończenia,Czas zakończenia\r\n"
        "X (w) [1],2026-11-02,10:00,2026-11-02,09:00\r\n".encode("cp1250")
    )
    cli(paths, api, "sources", "add", str(bad), "--name", "Zły")
    assert cli(paths, api, "sync", "--apply") == 1
    assert "Zapis zablokowany: 1 błędów w plikach" in capsys.readouterr().err
    assert api.writes == 0


def test_update_and_delete_are_applied(capsys, paths, api, configured):
    cli(paths, api, "sync", "--apply", answers=["tak"])
    first = next(e for e in events(api, configured) if e["summary"] == "Seminarium dyplomowe (S)")
    first["location"] = "999 99"
    first["reminders"] = {"useDefault": False, "overrides": [{"method": "popup", "minutes": 5}]}
    own = {
        "id": "own",
        "summary": "Prywatne",
        "start": {"dateTime": "2026-10-05T10:00:00+02:00"},
        "end": {"dateTime": "2026-10-05T11:00:00+02:00"},
    }
    api.events[configured]["own"] = own
    # Wyłączenie źródła kierunkowego: 16 zajęć z Analiz w oknie do usunięcia (bez bezpiecznika).
    cli(paths, api, "sources", "remove", "Grupa kierunkowa")
    capsys.readouterr()

    assert cli(paths, api, "sync", "--apply", answers=["tak"]) == 0
    out = capsys.readouterr().out
    assert "dodanie 0, zmiana 0, usunięcie 16" in out
    assert "Weryfikacja: kalendarz jest zgodny z planem." in out
    stored = {e["id"]: e for e in events(api, configured)}
    # Sala zmieniona ręcznie w Kalendarzu Google zostaje, przypomnienia też.
    assert stored[first["id"]]["location"] == "999 99"
    assert stored[first["id"]]["reminders"]["overrides"][0]["minutes"] == 5
    assert "own" in stored
    analizy = [e for e in stored.values() if e["summary"].startswith("Analizy")]
    assert len(analizy) == 14  # po 18.12, poza oknem — nietknięte


def test_mass_delete_needs_typed_count(capsys, paths, api, configured):
    cli(paths, api, "sync", "--apply", answers=["tak"])
    cli(paths, api, "sources", "remove", "Grupa domyślna")
    capsys.readouterr()

    assert cli(paths, api, "sync", "--apply", answers=["tak", "10"]) == 0
    out = capsys.readouterr().out
    assert "Aby potwierdzić, wpisz liczbę usuwanych zdarzeń" in out
    assert "Anulowano" in out
    assert len(events(api, configured)) == 75

    plan = build_sync_preview(paths, load_config(paths), api).plan
    count = str(len(plan.deletes))
    assert plan.mass_delete
    assert cli(paths, api, "sync", "--apply", answers=["tak", count]) == 0
    assert len(events(api, configured)) == 75 - int(count)


# --- awarie -------------------------------------------------------------------------------


def test_interrupted_run_is_reported_and_resumed(capsys, paths, api, configured):
    api.fail = lambda kind, n: (KeyboardInterrupt(), False) if n == 31 else None
    assert cli(paths, api, "sync", "--apply", answers=["tak"]) == 130
    assert "Przerwano" in capsys.readouterr().err
    assert len(events(api, configured)) == 30

    summary = last_run(paths.runs)
    assert (summary.finished, summary.done, summary.planned) == (False, 30, 75)
    assert cli(paths, api, "calendar", "status") == 0
    assert "została przerwana: wykonano 30 z 75" in capsys.readouterr().out

    api.fail = None
    assert cli(paths, api, "sync", "--apply", answers=["tak"]) == 0
    out = capsys.readouterr().out
    assert "UWAGA: Poprzednia synchronizacja" in out
    assert "dodanie 45, zmiana 0, usunięcie 0" in out
    assert len(events(api, configured)) == 75
    assert run_warning(last_run(paths.runs)) is None


def test_lost_insert_response_does_not_duplicate(capsys, paths, api, configured):
    # Google zapisał zdarzenie, ale odpowiedź nie dotarła.
    api.fail = lambda kind, n: (GoogleApiError("timeout"), True) if n == 5 else None
    assert cli(paths, api, "sync", "--apply", answers=["tak"]) == 3
    out = capsys.readouterr().out
    assert "nieudanych: 1" in out
    assert "Weryfikacja: kalendarz jest zgodny z planem." in out  # zdarzenie jednak jest
    assert len(events(api, configured)) == 75
    assert "zakończyła się z błędami: 1 nieudanych" in run_warning(last_run(paths.runs))


def test_retry_duplicate_is_cleaned_on_next_run(capsys, paths, api, configured):
    cli(paths, api, "sync", "--apply", answers=["tak"])
    # Symulacja ponowienia insertu, który za pierwszym razem też się zapisał.
    first = events(api, configured)[0]
    api.put_event(configured, {k: v for k, v in first.items() if k != "id"})
    capsys.readouterr()
    assert cli(paths, api, "sync", "--apply", answers=["tak"]) == 0
    out = capsys.readouterr().out
    assert "[duplikat]" in out
    assert len(events(api, configured)) == 75


def test_single_failure_continues_with_other_operations(capsys, paths, api, configured):
    api.fail = lambda kind, n: (GoogleApiError("HTTP 500", 500), False) if n == 3 else None
    assert cli(paths, api, "sync", "--apply", answers=["tak"]) == 3
    out = capsys.readouterr().out
    assert "Wykonano 74 z 75 operacji, nieudanych: 1." in out
    assert "Weryfikacja: kalendarz nie jest jeszcze zgodny (1)" in out
    api.fail = None
    assert cli(paths, api, "sync", "--apply", answers=["tak"]) == 0
    assert len(events(api, configured)) == 75


def test_consecutive_failures_abort(capsys, paths, api, configured):
    api.fail = lambda kind, n: (GoogleApiError("HTTP 503", 503), False)
    assert cli(paths, api, "sync", "--apply", answers=["tak"]) == 3
    captured = capsys.readouterr()
    assert f"{MAX_CONSECUTIVE_FAILURES} kolejne operacje nieudane" in captured.err
    assert api.writes == MAX_CONSECUTIVE_FAILURES
    assert f"pominiętych: {75 - MAX_CONSECUTIVE_FAILURES}" in captured.out


def test_expired_session_aborts_immediately(capsys, paths, api, configured):
    api.fail = lambda kind, n: (GoogleApiError("sesja", 401), False)
    assert cli(paths, api, "sync", "--apply", answers=["tak"]) == 3
    assert "sesja Google wygasła" in capsys.readouterr().err
    assert api.writes == 1


# --- dziennik i kolejność -----------------------------------------------------------------


def test_journal_records_every_operation(paths, api, configured):
    plan = build_sync_preview(paths, load_config(paths), api).plan
    journal = Journal.create(paths.runs)
    result = execute_plan(api, configured, plan, journal, sleep=lambda _s: None)
    records = [json.loads(line) for line in journal.path.read_text("utf-8").splitlines()]
    assert records[0]["type"] == "start"
    assert len(records[0]["operations"]) == 75
    assert [r["type"] for r in records[1:-1]] == ["op"] * 75
    assert all(r["status"] == "ok" and r["event_id"] for r in records[1:-1])
    assert records[-1] == {**records[-1], "type": "end", "done": 75, "failed": 0}
    assert result.done == 75


def test_operations_order_adds_updates_deletes(paths, api, configured):
    cli(paths, api, "sync", "--apply", answers=["tak"])
    stored = events(api, configured)
    # Zdarzenie sprzed śledzenia ręcznych zmian (bez skrótów) — zmiana jest nadpisywana.
    stored[0]["summary"] = "zmienione"
    del stored[0]["extendedProperties"]["private"]["gcalsync_h"]
    cli(paths, api, "sources", "remove", "Grupa kierunkowa")
    cli(paths, api, "rules", "disable", "1")
    plan = build_sync_preview(paths, load_config(paths), api).plan
    kinds = [op.kind for op in operations_for(plan)]
    assert kinds == sorted(kinds, key=["add", "update", "delete"].index)
    assert set(kinds) == {"add", "update", "delete"}


def test_throttling_between_operations(paths, api, configured):
    plan = build_sync_preview(paths, load_config(paths), api).plan
    pauses = []
    execute_plan(api, configured, plan, Journal.create(paths.runs), sleep=pauses.append)
    assert len(pauses) == 74
    assert all(p > 0 for p in pauses)


def test_truncated_journal_line_is_tolerated(paths):
    paths.runs.mkdir(parents=True)
    path = paths.runs / "20260101T000000000000Z.jsonl"
    path.write_text(
        json.dumps({"type": "start", "time": "2026-01-01T00:00:00+00:00", "operations": [{}]})
        + '\n{"type": "op", "sta',
        "utf-8",
    )
    summary = last_run(paths.runs)
    assert summary.finished is False
    assert "przerwana" in run_warning(summary)
