from datetime import datetime

import pytest
from conftest import DEFAULT_GROUP, NEW_GROUP, make_csv, row

from gcalsync.core.merge import (
    ConflictPolicy,
    deduplicate,
    overlapping_pairs,
    resolve_conflicts,
)
from gcalsync.core.normalize import WARSAW, to_events
from gcalsync.sources.outlook_csv import CsvFileSource, parse_outlook_csv

BIM = "Modelowanie danych do BIM"
ANALIZY = "Analizy teledetekcyjne"


def events_from(*rows, source_id):
    events, issues = to_events(parse_outlook_csv(make_csv(*rows)).events, source_id)
    assert issues == []
    return events


def load(path, source_id):
    return to_events(CsvFileSource.from_path(path).read().events, source_id)[0]


@pytest.fixture
def samples():
    return load(NEW_GROUP, "kierunkowa"), load(DEFAULT_GROUP, "domyślna")


# --- konflikty na próbkach ----------------------------------------------------------------


def test_samples_without_rules_new_group_wins(samples):
    new, default = samples
    priority = {"kierunkowa": 0, "domyślna": 1}
    accepted, conflicts, overlaps = resolve_conflicts(new + default, priority)

    assert len(conflicts) == 7
    assert {c.loser.course for c in conflicts} == {BIM}
    assert {w.course for c in conflicts for w in c.winners} == {ANALIZY}
    assert [c.loser.start for c in conflicts] == [
        datetime(2026, 10, 30, 8, 0, tzinfo=WARSAW),
        datetime(2026, 10, 30, 9, 50, tzinfo=WARSAW),
        datetime(2026, 11, 6, 13, 30, tzinfo=WARSAW),
        datetime(2026, 11, 27, 8, 0, tzinfo=WARSAW),
        datetime(2026, 11, 27, 9, 50, tzinfo=WARSAW),
        datetime(2026, 12, 9, 11, 40, tzinfo=WARSAW),
        datetime(2026, 12, 9, 13, 30, tzinfo=WARSAW),
    ]
    assert len(accepted) == 105 - 7
    # Pozostałe 23 zajęcia BIM nie kolidują z niczym — dlatego potrzebna jest reguła wykluczenia.
    assert sum(e.course == BIM for e in accepted) == 23
    assert overlaps == []


def test_samples_reversed_priority_drops_new_group(samples):
    new, default = samples
    priority = {"domyślna": 0, "kierunkowa": 1}
    _, conflicts, _ = resolve_conflicts(new + default, priority)
    assert len(conflicts) == 7
    assert {c.loser.course for c in conflicts} == {ANALIZY}


def test_samples_after_excluding_bim_have_no_conflicts(samples):
    new, default = samples
    without_bim = [e for e in new + default if e.course != BIM]
    accepted, conflicts, overlaps = resolve_conflicts(without_bim, {"kierunkowa": 0, "domyślna": 1})
    assert (len(accepted), conflicts, overlaps) == (75, [], [])


# --- konflikty na danych syntetycznych ----------------------------------------------------


def test_chain_dropped_event_does_not_block_others():
    x = events_from(row("X (w) [1]", "2026-10-01 08:00", "2026-10-01 09:35"), source_id="X")
    y = events_from(row("Y (w) [1]", "2026-10-01 09:00", "2026-10-01 10:35"), source_id="Y")
    z = events_from(row("Z (w) [1]", "2026-10-01 10:00", "2026-10-01 11:35"), source_id="Z")
    accepted, conflicts, _ = resolve_conflicts(x + y + z, {"X": 0, "Y": 1, "Z": 2})
    assert [e.course for e in accepted] == ["X", "Z"]
    assert [c.loser.course for c in conflicts] == ["Y"]


def test_touching_events_do_not_conflict():
    a = events_from(row("A (w) [1]", "2026-10-01 08:00", "2026-10-01 09:35"), source_id="A")
    b = events_from(row("B (w) [1]", "2026-10-01 09:35", "2026-10-01 11:00"), source_id="B")
    accepted, conflicts, overlaps = resolve_conflicts(a + b, {"A": 0, "B": 1})
    assert (len(accepted), conflicts, overlaps) == (2, [], [])


def test_partial_overlap_is_a_conflict():
    a = events_from(row("A (w) [1]", "2026-10-01 08:00", "2026-10-01 09:35"), source_id="A")
    b = events_from(row("B (w) [1]", "2026-10-01 09:30", "2026-10-01 11:00"), source_id="B")
    _, conflicts, _ = resolve_conflicts(a + b, {"A": 0, "B": 1})
    assert [c.loser.course for c in conflicts] == ["B"]


def test_loser_lists_all_winners():
    a = events_from(
        row("A1 (w) [1]", "2026-10-01 08:00", "2026-10-01 09:00"),
        row("A2 (w) [1]", "2026-10-01 09:00", "2026-10-01 10:00"),
        source_id="A",
    )
    b = events_from(row("B (w) [1]", "2026-10-01 08:30", "2026-10-01 09:30"), source_id="B")
    _, [conflict], _ = resolve_conflicts(a + b, {"A": 0, "B": 1})
    assert [w.course for w in conflict.winners] == ["A1", "A2"]


def test_same_source_overlap_keeps_both_with_warning():
    a = events_from(
        row("A1 (w) [1]", "2026-10-01 08:00", "2026-10-01 09:35"),
        row("A2 (L) [1]", "2026-10-01 09:00", "2026-10-01 10:35"),
        source_id="A",
    )
    accepted, conflicts, overlaps = resolve_conflicts(a, {"A": 0})
    assert len(accepted) == 2
    assert conflicts == []
    assert [(o.first.course, o.second.course) for o in overlaps] == [("A1", "A2")]


def test_keep_all_policy_only_warns():
    a = events_from(row("A (w) [1]", "2026-10-01 08:00", "2026-10-01 09:35"), source_id="A")
    b = events_from(row("B (w) [1]", "2026-10-01 09:00", "2026-10-01 10:35"), source_id="B")
    accepted, conflicts, overlaps = resolve_conflicts(
        a + b, {"A": 0, "B": 1}, ConflictPolicy.KEEP_ALL
    )
    assert len(accepted) == 2
    assert conflicts == []
    assert len(overlaps) == 1


def test_overlapping_pairs_finds_nested_intervals():
    events = events_from(
        row("Long (w) [1]", "2026-10-01 08:00", "2026-10-01 12:00"),
        row("Short1 (w) [1]", "2026-10-01 08:30", "2026-10-01 09:00"),
        row("Short2 (w) [1]", "2026-10-01 11:00", "2026-10-01 11:30"),
        source_id="A",
    )
    names = {(a.course, b.course) for a, b in overlapping_pairs(events)}
    assert names == {("Long", "Short1"), ("Long", "Short2")}


# --- deduplikacja -------------------------------------------------------------------------


def test_duplicate_across_files_keeps_higher_priority_and_reports_difference():
    a = events_from(row("Seminarium dyplomowe (S) [1]", location="18 58"), source_id="A")
    b = events_from(row("Seminarium dyplomowe (S) [1]", location="13 58"), source_id="B")
    unique, [group] = deduplicate(b + a, {"A": 0, "B": 1})
    assert len(unique) == 1
    assert unique[0].source_id == "A"
    assert group.kept.location == "18 58"
    assert [e.source_id for e in group.dropped] == ["B"]
    assert len(group.differences) == 1
    assert "13 58" in group.differences[0].describe()


def test_duplicate_with_different_number_is_still_duplicate():
    a = events_from(row("Geo (L) [3]"), source_id="A")
    b = events_from(row("Geo (L) [4]"), source_id="B")
    unique, [group] = deduplicate(a + b, {"A": 0, "B": 1})
    assert len(unique) == 1
    assert "numer zajęć" in group.differences[0].describe()


def test_identical_duplicate_has_no_differences():
    a = events_from(row("Geo (L) [3]"), source_id="A")
    b = events_from(row("Geo (L) [3]"), source_id="B")
    _, [group] = deduplicate(a + b, {"A": 0, "B": 1})
    assert group.differences == ()


def test_duplicate_within_one_file_keeps_first_row():
    a = events_from(
        row("Geo (L) [1]", location="1"), row("Geo (L) [1]", location="2"), source_id="A"
    )
    unique, [group] = deduplicate(a, {"A": 0})
    assert unique[0].location == "1"
    assert group.dropped[0].row == 3


def test_different_kind_same_time_is_not_duplicate():
    a = events_from(row("Geo (L) [1]"), row("Geo (P) [1]"), source_id="A")
    unique, groups = deduplicate(a, {"A": 0})
    assert len(unique) == 2
    assert groups == []
