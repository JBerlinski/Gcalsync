import json
from datetime import date

import pytest
from conftest import DEFAULT_GROUP, NEW_GROUP

from gcalsync.app import preview_from_config
from gcalsync.core.merge import ConflictPolicy
from gcalsync.core.rules import ExclusionRule
from gcalsync.storage import (
    CalendarConfig,
    Config,
    ConfigError,
    Paths,
    add_source,
    config_from_dict,
    config_to_dict,
    load_config,
    move_source,
    remove_source,
    replace_source_file,
    save_config,
    set_rule_enabled,
    validate_title_template,
)

BIM = "Modelowanie danych do BIM"


@pytest.fixture
def paths(tmp_path):
    return Paths(tmp_path / "gcalsync")


def test_gcalsync_home_override(monkeypatch, tmp_path):
    monkeypatch.setenv("GCALSYNC_HOME", str(tmp_path / "x"))
    assert Paths.default().root == tmp_path / "x"


def test_default_path_has_no_author_level(monkeypatch):
    monkeypatch.delenv("GCALSYNC_HOME", raising=False)
    root = Paths.default().root
    assert root.name == "gcalsync"
    assert root.parent.name != "gcalsync"


def test_missing_config_gives_defaults(paths):
    config = load_config(paths)
    assert config.sources == []
    assert config.rules == []
    assert config.policy is ConflictPolicy.PRIORITY
    assert config.title_template == "{course} ({kind})"
    assert config.calendar is None


def test_roundtrip_full_config(paths):
    config = Config()
    add_source(paths, config, NEW_GROUP, "Grupa kierunkowa")
    add_source(paths, config, DEFAULT_GROUP, "Grupa domyślna", encoding="cp1250")
    config.rules.append(ExclusionRule("course", "equals", BIM))
    config.rules.append(
        ExclusionRule(
            "subject",
            "regex",
            r"\(L\)",
            case_sensitive=True,
            enabled=False,
            sources=(config.sources[1].id,),
            date_from=date(2026, 10, 1),
            date_to=date(2026, 12, 31),
        )
    )
    config.policy = ConflictPolicy.KEEP_ALL
    config.title_template = "{course} – {kind}"
    config.calendar = CalendarConfig(id="abc@group.calendar.google.com", summary="Plan WAT")
    save_config(paths, config)

    loaded = load_config(paths)
    assert config_to_dict(loaded) == config_to_dict(config)
    assert loaded.rules == config.rules
    raw = json.loads(paths.config.read_text("utf-8"))
    assert raw["version"] == 1
    assert "Grupa domyślna" in paths.config.read_text("utf-8")  # UTF-8, bez \\u escapes


def test_source_copy_is_stored_in_app_dir(paths):
    config = Config()
    source = add_source(paths, config, DEFAULT_GROUP, "Grupa domyślna")
    copy = paths.root / source.file
    assert copy.parent == paths.files
    assert copy.read_bytes() == DEFAULT_GROUP.read_bytes()
    assert source.original_filename == DEFAULT_GROUP.name
    assert len(source.sha256) == 64


def test_duplicate_source_name_rejected(paths):
    config = Config()
    add_source(paths, config, NEW_GROUP, "A")
    with pytest.raises(ConfigError):
        add_source(paths, config, DEFAULT_GROUP, "A")


def test_replace_source_keeps_id_and_position(paths, tmp_path):
    config = Config()
    add_source(paths, config, NEW_GROUP, "A")
    add_source(paths, config, DEFAULT_GROUP, "B")
    old_id = config.sources[0].id
    replace_source_file(paths, config, "A", DEFAULT_GROUP)
    assert config.sources[0].id == old_id
    assert config.sources[0].name == "A"
    assert (paths.root / config.sources[0].file).read_bytes() == DEFAULT_GROUP.read_bytes()


def test_move_source(paths):
    config = Config()
    for name in ("A", "B", "C"):
        add_source(paths, config, NEW_GROUP, name)
    move_source(config, "C", 1)
    assert [s.name for s in config.sources] == ["C", "A", "B"]
    with pytest.raises(ConfigError):
        move_source(config, "A", 4)
    with pytest.raises(ConfigError):
        move_source(config, "nie ma", 1)


def test_remove_source_deletes_copy_and_cleans_rule_scope(paths):
    config = Config()
    a = add_source(paths, config, NEW_GROUP, "A")
    b = add_source(paths, config, DEFAULT_GROUP, "B")
    config.rules.append(ExclusionRule("course", "equals", BIM, sources=(a.id,)))
    config.rules.append(ExclusionRule("course", "equals", "X", sources=(a.id, b.id)))
    _, dropped = remove_source(paths, config, "A")
    assert not (paths.root / a.file).exists()
    # Reguła tylko dla A znika (nie może zacząć działać globalnie), druga traci zakres A.
    assert [r.value for r in dropped] == [BIM]
    assert [(r.value, r.sources) for r in config.rules] == [("X", (b.id,))]


def test_set_rule_enabled(paths):
    config = Config(rules=[ExclusionRule("course", "equals", BIM)])
    set_rule_enabled(config, 1, False)
    assert config.rules[0].enabled is False
    with pytest.raises(ConfigError):
        set_rule_enabled(config, 2, True)


def test_preview_from_config_matches_samples(paths):
    config = Config()
    add_source(paths, config, NEW_GROUP, "Grupa kierunkowa")
    add_source(paths, config, DEFAULT_GROUP, "Grupa domyślna")
    config.rules.append(ExclusionRule("course", "equals", BIM))
    save_config(paths, config)

    result = preview_from_config(paths, load_config(paths))
    assert len(result.events) == 75
    assert len(result.excluded) == 30
    assert [s.name for s in result.sources] == ["Grupa kierunkowa", "Grupa domyślna"]


def test_preview_from_config_without_sources(paths):
    with pytest.raises(ConfigError, match="Brak zapisanych źródeł"):
        preview_from_config(paths, Config())


def test_missing_source_copy_is_clear_error(paths):
    config = Config()
    source = add_source(paths, config, NEW_GROUP, "A")
    (paths.root / source.file).unlink()
    with pytest.raises(ConfigError, match="Brak kopii pliku"):
        preview_from_config(paths, config)


@pytest.mark.parametrize(
    "data",
    [
        {"version": 99},
        {"version": 1, "sources": [{"id": "x"}]},
        {"version": 1, "rules": [{"field": "nope", "op": "equals", "value": "x"}]},
        {"version": 1, "policy": "random"},
        {"version": 1, "title_template": "{nieznane}"},
    ],
)
def test_invalid_config_rejected(data):
    with pytest.raises(ConfigError):
        config_from_dict(data)


def test_corrupted_config_file(paths):
    paths.root.mkdir(parents=True)
    paths.config.write_text("{nie json", "utf-8")
    with pytest.raises(ConfigError):
        load_config(paths)


@pytest.mark.parametrize(
    "template", ["{course} ({kind})", "{course}", "{subject}", "[{kind}] {course} {location}"]
)
def test_valid_title_templates(template):
    validate_title_template(template)


@pytest.mark.parametrize("template", ["{title}", "{}", "{course", "   "])
def test_invalid_title_templates(template):
    with pytest.raises(ConfigError):
        validate_title_template(template)


def test_save_is_atomic_no_temp_files_left(paths):
    save_config(paths, Config())
    save_config(paths, Config())
    assert sorted(p.name for p in paths.root.iterdir()) == ["config.json"]
