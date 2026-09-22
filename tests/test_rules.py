from datetime import date

import pytest
from conftest import DEFAULT_GROUP, make_csv, row

from gcalsync.core.normalize import to_events
from gcalsync.core.rules import ExclusionRule, RuleError, apply_rules
from gcalsync.sources.outlook_csv import CsvFileSource, parse_outlook_csv

BIM = "Modelowanie danych do BIM"


def events_from(*rows, source_id="A"):
    events, issues = to_events(parse_outlook_csv(make_csv(*rows)).events, source_id)
    assert issues == []
    return events


@pytest.fixture
def default_events():
    source = CsvFileSource.from_path(DEFAULT_GROUP, id="domyślna")
    return to_events(source.read().events, source.id)[0]


def test_course_equals_excludes_all_bim_on_sample(default_events):
    rule = ExclusionRule("course", "equals", BIM)
    kept, excluded = apply_rules(default_events, [rule])
    assert len(excluded) == 30
    assert len(kept) == 45
    assert {x.event.course for x in excluded} == {BIM}
    assert all(x.rule is rule for x in excluded)


def test_course_equals_is_case_and_whitespace_insensitive(default_events):
    _, excluded = apply_rules(
        default_events, [ExclusionRule("course", "equals", "  modelowanie  DANYCH do bim ")]
    )
    assert len(excluded) == 30


def test_course_equals_does_not_match_partial_name():
    events = events_from(row("Modelowanie danych do BIM II (w) [1]"))
    assert apply_rules(events, [ExclusionRule("course", "equals", BIM)])[1] == []


def test_subject_contains():
    events = events_from(row("Modelowanie danych do BIM (w) [1]"), row("Geowizualizacja (w) [1]"))
    kept, excluded = apply_rules(events, [ExclusionRule("subject", "contains", "bim")])
    assert [e.course for e in kept] == ["Geowizualizacja"]
    assert len(excluded) == 1


def test_case_sensitive_rule():
    events = events_from(row("Modelowanie danych do BIM (w) [1]"))
    rule = ExclusionRule("subject", "contains", "bim", case_sensitive=True)
    assert apply_rules(events, [rule])[1] == []


def test_regex_rule():
    events = events_from(row("Geowizualizacja (L) [1]"), row("Geowizualizacja (w) [1]"))
    kept, _ = apply_rules(events, [ExclusionRule("subject", "regex", r"\(l\)")])
    assert [e.kind for e in kept] == ["w"]


def test_kind_and_location_fields():
    events = events_from(
        row("Geo (L) [1]", location="A  59"),
        row("Geo (w) [1]", location="17 58"),
    )
    assert len(apply_rules(events, [ExclusionRule("kind", "equals", "l")])[1]) == 1
    assert len(apply_rules(events, [ExclusionRule("location", "equals", "A 59")])[1]) == 1


def test_disabled_rule_matches_nothing(default_events):
    rule = ExclusionRule("course", "equals", BIM, enabled=False)
    assert apply_rules(default_events, [rule])[1] == []


def test_source_scope():
    a = events_from(row(f"{BIM} (w) [1]"), source_id="A")
    b = events_from(row(f"{BIM} (w) [1]"), source_id="B")
    rule = ExclusionRule("course", "equals", BIM, sources=("A",))
    kept, excluded = apply_rules(a + b, [rule])
    assert [e.source_id for e in kept] == ["B"]
    assert [x.event.source_id for x in excluded] == ["A"]


def test_date_range(default_events):
    rule = ExclusionRule(
        "course", "equals", BIM, date_from=date(2026, 11, 1), date_to=date(2026, 11, 30)
    )
    _, excluded = apply_rules(default_events, [rule])
    assert excluded
    assert all(x.event.start.month == 11 for x in excluded)


def test_first_matching_rule_is_recorded():
    events = events_from(row(f"{BIM} (w) [1]"))
    first = ExclusionRule("subject", "contains", "BIM")
    second = ExclusionRule("course", "equals", BIM)
    assert apply_rules(events, [first, second])[1][0].rule is first


@pytest.mark.parametrize(
    "kwargs",
    [
        {"field": "title", "op": "equals", "value": "x"},
        {"field": "course", "op": "startswith", "value": "x"},
        {"field": "course", "op": "equals", "value": "   "},
        {"field": "subject", "op": "regex", "value": "(unclosed"},
        {
            "field": "course",
            "op": "equals",
            "value": "x",
            "date_from": date(2026, 12, 1),
            "date_to": date(2026, 11, 1),
        },
    ],
)
def test_invalid_rules_are_rejected(kwargs):
    with pytest.raises(RuleError):
        ExclusionRule(**kwargs)


def test_describe():
    rule = ExclusionRule("course", "equals", BIM)
    assert rule.describe() == "przedmiot równa się „Modelowanie danych do BIM”"
    scoped = ExclusionRule("subject", "contains", "BIM", sources=("A",), enabled=False)
    assert "tylko źródła: A" in scoped.describe()
    assert "WYŁĄCZONA" in scoped.describe()
