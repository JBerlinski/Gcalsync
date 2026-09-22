"""Testy całego potoku, w tym test „złoty” na przykładowych plikach.

Wzorzec: tests/golden/samples_bim_excluded.json. Po świadomej zmianie wyniku odśwież go:
    UPDATE_GOLDEN=1 uv run pytest tests/test_pipeline.py
"""

import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from conftest import DEFAULT_GROUP, NEW_GROUP, make_csv, row

from gcalsync.core.merge import ConflictPolicy
from gcalsync.core.normalize import WARSAW
from gcalsync.core.pipeline import build_preview
from gcalsync.core.rules import ExclusionRule
from gcalsync.report import event_to_dict, preview_to_dict, render_text
from gcalsync.sources.outlook_csv import CsvFileSource

GOLDEN = Path(__file__).parent / "golden" / "samples_bim_excluded.json"
BIM_RULE = ExclusionRule("course", "equals", "Modelowanie danych do BIM")


def sample_sources():
    return [
        CsvFileSource.from_path(NEW_GROUP, id="Grupa kierunkowa", name="Grupa kierunkowa"),
        CsvFileSource.from_path(DEFAULT_GROUP, id="Grupa domyślna", name="Grupa domyślna"),
    ]


def csv_source(source_id, *rows):
    return CsvFileSource(id=source_id, name=source_id, data=make_csv(*rows))


def test_golden_samples_with_bim_rule():
    result = build_preview(sample_sources(), [BIM_RULE])
    actual = [event_to_dict(e, result) for e in result.events]

    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(actual, ensure_ascii=False, indent=2) + "\n", "utf-8")
    expected = json.loads(GOLDEN.read_text("utf-8"))

    assert len(actual) == 75
    assert actual == expected


def test_samples_summary_with_bim_rule():
    result = build_preview(sample_sources(), [BIM_RULE])
    assert len(result.parsed) == 105
    assert len(result.excluded) == 30
    assert result.rule_hits() == [30]
    assert result.duplicates == []
    assert result.conflicts == []
    assert result.overlaps == []
    assert result.issues == []
    assert len(result.events) == 75
    assert result.coverage == (
        datetime(2026, 10, 1, 9, 50, tzinfo=WARSAW),
        datetime(2027, 1, 29, 17, 35, tzinfo=WARSAW),
    )
    counts = {s.name: s.event_count for s in result.sources}
    assert counts == {"Grupa kierunkowa": 30, "Grupa domyślna": 75}


def test_samples_without_rule_report_seven_conflicts():
    result = build_preview(sample_sources())
    assert len(result.conflicts) == 7
    assert len(result.events) == 98


def test_samples_keep_all_policy():
    result = build_preview(sample_sources(), policy=ConflictPolicy.KEEP_ALL)
    assert result.conflicts == []
    assert len(result.overlaps) == 7
    assert len(result.events) == 105


def test_coverage_includes_excluded_events():
    src = csv_source(
        "A",
        row("Geo (w) [1]", "2026-10-05 08:00", "2026-10-05 09:35"),
        row("BIM (w) [1]", "2026-10-01 08:00", "2026-10-01 09:35"),
    )
    result = build_preview([src], [ExclusionRule("course", "equals", "BIM")])
    assert result.coverage[0] == datetime(2026, 10, 1, 8, 0, tzinfo=WARSAW)
    assert len(result.events) == 1


def test_dedup_runs_before_conflicts():
    # To samo zdarzenie w dwóch plikach nie może być raportowane jako konflikt.
    a = csv_source("A", row("Seminarium (S) [1]"))
    b = csv_source("B", row("Seminarium (S) [1]"))
    result = build_preview([a, b])
    assert len(result.events) == 1
    assert len(result.duplicates) == 1
    assert result.conflicts == []


def test_errors_are_collected_and_flagged():
    good = csv_source("A", row("Geo (w) [1]"))
    bad = csv_source("B", row("Geo (w) [2]", "2026-10-01 10:00", "2026-10-01 09:00"))
    result = build_preview([good, bad])
    assert result.has_errors
    assert [(i.source_id, i.row) for i in result.errors] == [("B", 2)]
    assert len(result.events) == 1


def test_same_file_twice_is_warned():
    data = make_csv(row("Geo (w) [1]"))
    a = CsvFileSource(id="A", name="A", data=data)
    b = CsvFileSource(id="B", name="B", data=data)
    result = build_preview([a, b])
    assert any("Ten sam plik" in w.message for w in result.warnings)


def test_duplicate_source_ids_rejected():
    with pytest.raises(ValueError):
        build_preview([csv_source("A", row("X (w) [1]")), csv_source("A", row("Y (w) [1]"))])


def test_empty_input():
    result = build_preview([])
    assert result.events == []
    assert result.coverage is None


def test_render_and_json_do_not_crash_on_everything():
    a = csv_source(
        "A",
        row("Seminarium (S) [1]", location="1"),
        row("Geo (w) [1]", "2026-10-02 08:00", "2026-10-02 09:35"),
        row("Kolizja (L) [1]", "2026-10-02 09:00", "2026-10-02 10:00"),
    )
    b = csv_source(
        "B",
        row("Seminarium (S) [1]", location="2"),
        row("Inne (w) [1]", "2026-10-02 08:30", "2026-10-02 09:00"),
        row("BIM (w) [1]"),
        row("Zły (w) [1]", "2026-10-03 10:00", "2026-10-03 09:00"),
    )
    result = build_preview([a, b], [ExclusionRule("course", "equals", "BIM")])
    text = render_text(result)
    for heading in ("Wykluczone", "Duplikaty", "Konflikty", "Kolizje", "Błędy", "Do kalendarza"):
        assert heading in text
    assert "rozbieżność" in text
    assert "ODRZUCONE" in text
    assert "zablokowana" in text
    data = preview_to_dict(result)
    json.dumps(data, ensure_ascii=False)
    assert data["summary"] == {
        "parsed": 6,
        "excluded": 1,
        "duplicates": 1,
        "conflicts": 1,
        "events": 3,
        "errors": 1,
        "warnings": 0,
    }


def test_report_shows_dst_offsets():
    text = render_text(build_preview(sample_sources(), [BIM_RULE]))
    assert "2026-10-23 (pt) 13:30–15:05 +02:00" in text
    assert "2026-10-30 (pt) 08:00–09:35 +01:00" in text
