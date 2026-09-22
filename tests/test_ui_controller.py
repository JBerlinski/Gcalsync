"""Logika GUI (Controller) — bez przeglądarki, na atrapie kalendarza."""

import pytest
from conftest import DEFAULT_GROUP, NEW_GROUP
from fake_calendar import FakeCalendarApi

from gcalsync.app import PlanChangedError
from gcalsync.gcal.mapping import event_body
from gcalsync.storage import ConfigError, Paths, load_config
from gcalsync.ui.controller import Controller

BIM = "Modelowanie danych do BIM"


@pytest.fixture
def api():
    return FakeCalendarApi()


@pytest.fixture
def ctrl(tmp_path, api):
    return Controller(Paths(tmp_path / "home"), api_factory=lambda _p: api, sleep=lambda _s: None)


@pytest.fixture
def loaded(ctrl):
    ctrl.add_source(NEW_GROUP.read_bytes(), NEW_GROUP.name, "Grupa kierunkowa")
    ctrl.add_source(DEFAULT_GROUP.read_bytes(), DEFAULT_GROUP.name, "Grupa domyślna")
    ctrl.add_rule("course", "equals", BIM)
    return ctrl


def test_empty_start(ctrl):
    assert ctrl.preview is None
    assert ctrl.preview_error is None
    assert ctrl.suggest_source_name() == "Źródło 1"
    assert ctrl.courses() == []


def test_sources_and_preview(loaded):
    assert len(loaded.preview.events) == 75
    assert loaded.rule_hits() == [30]
    assert BIM in loaded.courses()
    assert loaded.suggest_source_name() == "Źródło 3"
    # zmiany są od razu zapisane w config.json
    saved = load_config(loaded.paths)
    assert [s.name for s in saved.sources] == ["Grupa kierunkowa", "Grupa domyślna"]


def test_move_rename_encoding_remove(loaded):
    loaded.move_source("Grupa domyślna", -1)
    assert [s.name for s in loaded.config.sources] == ["Grupa domyślna", "Grupa kierunkowa"]
    loaded.move_source("Grupa domyślna", -1)  # już na górze — bez zmian
    loaded.rename_source("Grupa domyślna", "  Domyślna  ")
    assert loaded.config.sources[0].name == "Domyślna"
    with pytest.raises(ConfigError):
        loaded.rename_source("Domyślna", "Grupa kierunkowa")
    loaded.set_source_encoding("Domyślna", "cp1250")
    assert loaded.config.sources[0].encoding == "cp1250"
    with pytest.raises(ConfigError):
        loaded.set_source_encoding("Domyślna", "koi8-r")
    loaded.remove_source("Domyślna")
    assert [s.name for s in loaded.config.sources] == ["Grupa kierunkowa"]
    assert len(loaded.preview.events) == 30


def test_replace_keeps_name_and_encoding(loaded):
    loaded.set_source_encoding("Grupa kierunkowa", "cp1250")
    loaded.replace_source("Grupa kierunkowa", DEFAULT_GROUP.read_bytes(), "nowy.txt")
    source = loaded.config.sources[0]
    assert (source.name, source.encoding, source.original_filename) == (
        "Grupa kierunkowa",
        "cp1250",
        "nowy.txt",
    )


def test_rules(loaded):
    with pytest.raises(ConfigError, match="już istnieje"):
        loaded.add_rule("course", "equals", BIM)
    loaded.set_rule_enabled(0, False)
    assert len(loaded.preview.conflicts) == 7
    loaded.set_rule_enabled(0, True)
    loaded.add_rule("subject", "contains", "Seminarium", source_names=["Grupa domyślna"])
    assert loaded.rule_hits() == [30, 15]
    assert "tylko źródła: Grupa domyślna" in loaded.config.rules[1].describe(loaded.source_names())
    loaded.remove_rule(1)
    assert len(loaded.preview.events) == 75


def test_settings(loaded):
    assert loaded.title_example() == "Seminarium dyplomowe (S)"
    assert loaded.title_example("{course} [{seq}]") == "Seminarium dyplomowe [1]"
    with pytest.raises(ConfigError):
        loaded.title_example("{zle}")
    loaded.set_title_template("{course}")
    loaded.set_policy("keep-all")
    saved = load_config(loaded.paths)
    assert (saved.title_template, saved.policy.value) == ("{course}", "keep-all")


def test_google_status_without_files(ctrl):
    status = ctrl.google_status()
    assert not status.logged_in
    assert "client_secret.json" in status.message


def test_dry_run_apply_and_verify(loaded, api):
    loaded.create_calendar("Plan WAT")
    [cal_id] = api.calendars
    sync = loaded.dry_run()
    assert len(sync.plan.adds) == 75
    progress = []
    outcome = loaded.apply(lambda done, total, op, err: progress.append((done, err)))
    assert outcome.result.done == 75
    assert outcome.remaining == []
    assert progress[-1] == (75, None)
    assert len(api.events[cal_id]) == 75
    assert loaded.sync is None  # po zapisie trzeba ponownie sprawdzić
    assert loaded.dry_run().plan.operation_count == 0
    assert loaded.run_warning() is None


def test_apply_requires_dry_run(loaded, api):
    loaded.create_calendar()
    with pytest.raises(ConfigError, match="dry-run"):
        loaded.apply()


def test_config_change_invalidates_dry_run(loaded):
    loaded.create_calendar()
    loaded.dry_run()
    loaded.set_rule_enabled(0, False)
    assert loaded.sync is None


def test_apply_refuses_when_calendar_changed_since_dry_run(loaded, api):
    loaded.create_calendar()
    [cal_id] = api.calendars
    loaded.dry_run()
    # Między podglądem a zapisem ktoś (np. inna karta) dodał jedno z docelowych zdarzeń.
    event = loaded.preview.events[0]
    api.put_event(cal_id, event_body(event, loaded.config.title_template, "x"))
    writes = api.writes
    with pytest.raises(PlanChangedError):
        loaded.apply()
    assert api.writes == writes


def test_ui_module_builds_page_function(loaded):
    from gcalsync.ui.app import build_root

    assert callable(build_root(loaded))
