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
    assert main(["preview", str(NEW_GROUP), "--names", "a", "b"]) == 2
    assert "Liczba nazw" in capsys.readouterr().err


def test_invalid_regex_is_usage_error(capsys):
    assert main(["preview", str(NEW_GROUP), "--exclude-regex", "(zły"]) == 2
    assert "wyrażenie regularne" in capsys.readouterr().err


def test_missing_file_is_usage_error(capsys, tmp_path):
    assert main(["preview", str(tmp_path / "brak.csv")]) == 2
    assert "Nie ma pliku" in capsys.readouterr().err


# --- zapisana konfiguracja (etap 4) -------------------------------------------------------


@pytest.fixture
def paths(tmp_path):
    from gcalsync.storage import Paths

    return Paths(tmp_path / "home")


def run(paths, *argv):
    return main(list(argv), paths=paths)


def test_paths_command_creates_dir_and_shows_client_secret_location(capsys, paths):
    assert run(paths, "paths") == 0
    out = capsys.readouterr().out
    assert str(paths.client_secret) in out
    assert "BRAK" in out
    assert paths.root.is_dir()


def test_configured_workflow_end_to_end(capsys, paths):
    assert run(paths, "sources", "add", str(DEFAULT_GROUP), "--name", "Grupa domyślna") == 0
    assert run(paths, "sources", "add", str(NEW_GROUP), "--name", "Grupa kierunkowa") == 0
    assert run(paths, "sources", "move", "Grupa kierunkowa", "1") == 0
    assert run(paths, "rules", "add", "--course", "Modelowanie danych do BIM") == 0
    capsys.readouterr()

    assert run(paths, "sources") == 0
    out = capsys.readouterr().out
    assert out.index("1. Grupa kierunkowa") < out.index("2. Grupa domyślna")

    assert run(paths, "preview") == 0
    out = capsys.readouterr().out
    assert "=== Do kalendarza (75) ===" in out
    assert "przedmiot równa się „Modelowanie danych do BIM” — wykluczono: 30" in out

    assert run(paths, "rules", "disable", "1") == 0
    capsys.readouterr()
    assert run(paths, "preview") == 0
    assert "=== Konflikty rozwiązane priorytetem (7) ===" in capsys.readouterr().out


def test_rule_scoped_to_source_shows_source_name(capsys, paths):
    run(paths, "sources", "add", str(DEFAULT_GROUP), "--name", "Grupa domyślna")
    assert run(paths, "rules", "add", "--contains", "BIM", "--source", "Grupa domyślna") == 0
    assert "tylko źródła: Grupa domyślna" in capsys.readouterr().out


def test_rule_add_errors(capsys, paths):
    assert run(paths, "rules", "add") == 2
    assert run(paths, "rules", "add", "--regex", "(") == 2
    assert run(paths, "rules", "add", "--course", "X", "--source", "nie ma") == 2
    assert run(paths, "rules", "add", "--course", "X", "--field", "kind") == 2
    assert run(paths, "rules", "remove", "5") == 2
    err = capsys.readouterr().err
    assert "Nie ma źródła „nie ma”" in err
    assert "Nie ma reguły nr 5" in err


def test_rule_add_full_form(capsys, paths):
    argv = ["rules", "add", "--field", "location", "--op", "equals", "--value", "A 59"]
    assert run(paths, *argv) == 0
    assert "lokalizacja równa się „A 59”" in capsys.readouterr().out


def test_preview_without_sources_is_clear_error(capsys, paths):
    assert run(paths, "preview") == 2
    assert "Brak zapisanych źródeł" in capsys.readouterr().err


def test_sources_replace_and_remove(capsys, paths):
    run(paths, "sources", "add", str(NEW_GROUP), "--name", "A")
    assert run(paths, "sources", "replace", "A", str(DEFAULT_GROUP)) == 0
    assert run(paths, "sources", "remove", "A") == 0
    assert run(paths, "sources", "remove", "A") == 2
    assert list(paths.files.iterdir()) == []


def test_settings(capsys, paths):
    assert run(paths, "settings", "--title-template", "{course} [{kind}]") == 0
    assert "{course} [{kind}]" in capsys.readouterr().out
    assert run(paths, "settings", "--title-template", "{zle}") == 2
    assert run(paths, "settings") == 0
    out = capsys.readouterr().out
    assert "{course} [{kind}]" in out
    assert "nie ustawiony" in out


# --- Google (etap 5): kalendarz i dry-run -------------------------------------------------


@pytest.fixture
def fake_api():
    from fake_calendar import FakeCalendarApi

    return FakeCalendarApi()


def run_google(paths, api, *argv):
    return main(list(argv), paths=paths, api_factory=lambda _paths: api)


def configure_samples(paths):
    run(paths, "sources", "add", str(NEW_GROUP), "--name", "Grupa kierunkowa")
    run(paths, "sources", "add", str(DEFAULT_GROUP), "--name", "Grupa domyślna")
    run(paths, "rules", "add", "--course", "Modelowanie danych do BIM")


def test_sync_without_calendar_needs_no_google(capsys, paths):
    configure_samples(paths)
    capsys.readouterr()

    def no_google(_paths):
        raise AssertionError("dry-run bez kalendarza nie może łączyć się z Google")

    assert main(["sync"], paths=paths, api_factory=no_google) == 0
    out = capsys.readouterr().out
    assert "Nie utworzono jeszcze kalendarza" in out
    assert "=== Do dodania (75) ===" in out
    assert "Zapis byłby zablokowany: kalendarz docelowy nie jest utworzony" in out
    assert "DRY-RUN" in out


def test_calendar_create_and_dry_run_against_it(capsys, paths, fake_api):
    configure_samples(paths)
    assert run_google(paths, fake_api, "calendar", "create") == 0
    out = capsys.readouterr().out
    assert "Utworzono pusty kalendarz „Plan WAT”" in out
    [cal_id] = fake_api.calendars
    assert fake_api.calendars[cal_id]["timeZone"] == "Europe/Warsaw"
    assert fake_api.events[cal_id] == {}

    assert run_google(paths, fake_api, "sync") == 0
    out = capsys.readouterr().out
    assert cal_id in out
    assert "=== Do dodania (75) ===" in out
    assert "+ 2026-10-01 (cz) 09:50–11:25 +02:00  Seminarium dyplomowe (S)  sala: 18 58" in out
    assert "+ 2026-10-30 (pt) 08:00–09:35 +01:00  Analizy teledetekcyjne (w)  sala: A 59" in out
    assert "Zapis byłby zablokowany" not in out
    # Dry-run tylko czyta.
    assert fake_api.events[cal_id] == {}
    assert not any(c.startswith(("insert", "patch", "delete")) for c in fake_api.calls)


def test_dry_run_shows_updates_deletes_and_ignores_foreign_events(capsys, paths, fake_api):
    from gcalsync.app import build_sync_preview
    from gcalsync.storage import load_config

    configure_samples(paths)
    run_google(paths, fake_api, "calendar", "create")
    [cal_id] = fake_api.calendars
    # Stan „po synchronizacji”: wszystkie docelowe zdarzenia są już w kalendarzu.
    config = load_config(paths)
    for planned in build_sync_preview(paths, config, fake_api).plan.adds:
        fake_api.put_event(cal_id, planned.body)
    # Ręczna zmiana sali w jednym zdarzeniu i własne zdarzenie użytkownika.
    first = next(iter(fake_api.events[cal_id].values()))
    first["location"] = "999 99"
    fake_api.events[cal_id]["own"] = {
        "id": "own",
        "summary": "Prywatne",
        "start": {"dateTime": "2026-10-05T10:00:00+02:00"},
        "end": {"dateTime": "2026-10-05T11:00:00+02:00"},
    }
    capsys.readouterr()
    assert run_google(paths, fake_api, "sync") == 0
    out = capsys.readouterr().out
    assert "=== Do dodania (0) ===" in out
    assert "=== Do zmiany (1) ===" in out
    assert "sala: „999 99” -> „18 58”" in out
    assert "=== Bez zmian: 74 ===" in out
    assert "inne 1 (nigdy nie są ruszane)" in out

    # Bez pliku grupy kierunkowej okno pokrycia kończy się 18.12: usuwane są tylko zajęcia
    # z Analiz mieszczące się w oknie, późniejsze zostają nietknięte.
    run(paths, "sources", "remove", "Grupa kierunkowa")
    capsys.readouterr()
    assert run_google(paths, fake_api, "sync") == 0
    out = capsys.readouterr().out
    assert "=== Do usunięcia (16) ===" in out
    assert "poza oknem (nie są ruszane): 14" in out
    assert "Okno synchronizacji (pokrycie plików): 2026-10-01 09:50 – 2026-12-18 13:15" in out


def test_calendar_status_forget_and_use(capsys, paths, fake_api):
    assert run_google(paths, fake_api, "calendar") == 0
    assert "nie jest ustawiony" in capsys.readouterr().out
    run_google(paths, fake_api, "calendar", "create", "--name", "Plan testowy")
    [cal_id] = fake_api.calendars
    assert run_google(paths, fake_api, "calendar", "status") == 0
    assert "„Plan testowy”" in capsys.readouterr().out
    assert run_google(paths, fake_api, "calendar", "create") == 2
    assert "już ustawiony" in capsys.readouterr().err
    assert run_google(paths, fake_api, "calendar", "forget") == 0
    assert run_google(paths, fake_api, "calendar", "use", "nie-ma") == 2
    assert run_google(paths, fake_api, "calendar", "use", cal_id) == 0
    assert "Ustawiono kalendarz docelowy" in capsys.readouterr().out


def test_sync_with_deleted_calendar_is_clear_error(capsys, paths, fake_api):
    configure_samples(paths)
    run_google(paths, fake_api, "calendar", "create")
    fake_api.calendars.clear()
    assert run_google(paths, fake_api, "sync") == 2
    assert "nie istnieje" in capsys.readouterr().err


def test_sync_with_calendar_but_not_logged_in(capsys, paths, fake_api):
    configure_samples(paths)
    run_google(paths, fake_api, "calendar", "create")
    capsys.readouterr()
    assert run(paths, "sync") == 3  # prawdziwa fabryka API, brak token.json
    assert "Nie zalogowano. Uruchom: gcalsync login" in capsys.readouterr().err


def test_login_without_client_secret(capsys, paths):
    assert run(paths, "login") == 3
    err = capsys.readouterr().err
    assert "client_secret.json" in err
    assert "Desktop app" in err


def test_logout(capsys, paths):
    paths.root.mkdir(parents=True)
    paths.token.write_text("{}")
    assert run(paths, "logout") == 0
    assert not paths.token.exists()
    assert run(paths, "logout") == 0
    assert "Nie było zapisanego tokenu" in capsys.readouterr().out
