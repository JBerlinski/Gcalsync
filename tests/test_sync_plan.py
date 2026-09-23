from datetime import UTC, datetime, timedelta

import pytest
from conftest import DEFAULT_GROUP, NEW_GROUP, make_csv, row
from fake_calendar import FakeCalendarApi, to_google_time

from gcalsync.core.diff import plan_sync
from gcalsync.core.normalize import WARSAW, to_events
from gcalsync.core.pipeline import build_preview
from gcalsync.core.rules import ExclusionRule
from gcalsync.gcal.mapping import (
    PROP_KEY,
    PROP_MANAGED,
    comparable,
    event_body,
    render_description,
    render_title,
)
from gcalsync.sources.outlook_csv import CsvFileSource, parse_outlook_csv

BIM = ExclusionRule("course", "equals", "Modelowanie danych do BIM")
BEFORE_SEMESTER = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
TEMPLATE = "{course} ({kind})"


def one_event(
    subject="Geowizualizacja (L) [3]",
    start="2026-10-30 08:00",
    end="2026-10-30 09:35",
    location="14 58",
):
    [event], issues = to_events(
        parse_outlook_csv(make_csv(row(subject, start, end, location))).events, "A"
    )
    assert issues == []
    return event


def sample_preview(rules=(BIM,)):
    sources = [
        CsvFileSource.from_path(NEW_GROUP, id="kier", name="Grupa kierunkowa"),
        CsvFileSource.from_path(DEFAULT_GROUP, id="dom", name="Grupa domyślna"),
    ]
    return build_preview(sources, list(rules))


def desired_of(preview):
    return [(e, event_body(e, TEMPLATE, preview.source_name(e.source_id))) for e in preview.events]


def google_copy(body, **changes):
    g = dict(body, start=to_google_time(body["start"]), end=to_google_time(body["end"]))
    g.update(changes)
    return g


# --- mapowanie ----------------------------------------------------------------------------


def test_title_from_template():
    event = one_event()
    assert render_title(event, TEMPLATE) == "Geowizualizacja (L)"
    assert render_title(event, "{course} {kind} [{seq}]") == "Geowizualizacja L [3]"
    assert render_title(event, "{subject}") == "Geowizualizacja (L) [3]"


def test_title_without_kind_is_course():
    assert render_title(one_event("Konsultacje"), TEMPLATE) == "Konsultacje"


def test_description_holds_number_and_source():
    text = render_description(one_event(), "Grupa domyślna")
    assert "Zajęcia nr: 3" in text
    assert "Źródło: Grupa domyślna" in text
    assert "gcalsync" in text


def test_body_uses_local_time_with_time_zone_and_marker():
    event = one_event()
    body = event_body(event, TEMPLATE, "A")
    assert body["start"] == {"dateTime": "2026-10-30T08:00:00", "timeZone": "Europe/Warsaw"}
    assert body["end"] == {"dateTime": "2026-10-30T09:35:00", "timeZone": "Europe/Warsaw"}
    assert body["location"] == "14 58"
    assert body["reminders"] == {"useDefault": True}
    props = body["extendedProperties"]["private"]
    assert props[PROP_MANAGED] == "1"
    assert props[PROP_KEY] == event.key
    assert all(len(k) <= 44 and len(v) <= 1024 for k, v in props.items())
    assert "attendees" not in body


def test_body_omits_empty_location():
    assert "location" not in event_body(one_event(location=""), TEMPLATE, "A")


def test_comparable_matches_google_representation_across_dst():
    for start, end in (
        ("2026-10-23 13:30", "2026-10-23 15:05"),
        ("2026-10-30 08:00", "2026-10-30 09:35"),
    ):
        body = event_body(one_event(start=start, end=end), TEMPLATE, "A")
        google = google_copy(body)
        assert google["start"]["dateTime"].endswith(("+02:00", "+01:00"))
        assert comparable(body) == comparable(google)


# --- plan synchronizacji ------------------------------------------------------------------


def test_empty_calendar_adds_all_75():
    preview = sample_preview()
    plan = plan_sync(desired_of(preview), [], preview.coverage, BEFORE_SEMESTER)
    assert len(plan.adds) == 75
    assert (plan.updates, plan.deletes, plan.unchanged) == ([], [], 0)
    assert plan.adds[0].event.start == datetime(2026, 10, 1, 9, 50, tzinfo=WARSAW)


def test_second_run_is_idempotent():
    preview = sample_preview()
    desired = desired_of(preview)
    existing = [google_copy(body, id=f"e{i}") for i, (_, body) in enumerate(desired)]
    plan = plan_sync(desired, existing, preview.coverage, BEFORE_SEMESTER)
    assert plan.operation_count == 0
    assert plan.unchanged == 75
    assert plan.managed_total == plan.managed_in_scope == 75


def test_location_change_is_update_not_delete_add():
    event = one_event(location="14 58")
    old = google_copy(event_body(event, TEMPLATE, "A"), id="e1")
    moved = one_event(location="13 58")
    assert moved.key == event.key
    plan = plan_sync(
        [(moved, event_body(moved, TEMPLATE, "A"))],
        [old],
        (moved.start, moved.end),
        BEFORE_SEMESTER,
    )
    [update] = plan.updates
    assert [(c.field, c.old, c.new) for c in update.changes] == [("location", "14 58", "13 58")]
    assert update.existing["id"] == "e1"
    assert plan.adds == plan.deletes == []


def test_manual_edit_in_google_is_reverted():
    event = one_event()
    body = event_body(event, TEMPLATE, "A")
    edited = google_copy(body, id="e1", summary="Moja notatka")
    plan = plan_sync([(event, body)], [edited], (event.start, event.end), BEFORE_SEMESTER)
    assert [c.field for c in plan.updates[0].changes] == ["summary"]


def test_time_change_is_delete_and_add():
    old_event = one_event(start="2026-10-30 08:00", end="2026-10-30 09:35")
    new_event = one_event(start="2026-10-30 09:50", end="2026-10-30 11:25")
    old = google_copy(event_body(old_event, TEMPLATE, "A"), id="e1")
    plan = plan_sync(
        [(new_event, event_body(new_event, TEMPLATE, "A"))],
        [old],
        (old_event.start, new_event.end),
        BEFORE_SEMESTER,
    )
    assert len(plan.adds) == 1
    assert [d.reason for d in plan.deletes] == ["nieaktualne"]


def test_events_removed_from_files_are_deleted_within_window():
    with_bim = sample_preview(rules=())  # 98 zdarzeń, w tym 23 BIM
    existing = [google_copy(b, id=f"e{i}") for i, (_, b) in enumerate(desired_of(with_bim))]
    preview = sample_preview()
    plan = plan_sync(desired_of(preview), existing, preview.coverage, BEFORE_SEMESTER)
    assert len(plan.deletes) == 23
    assert all("BIM" in d.existing["summary"] for d in plan.deletes)
    assert plan.unchanged == 75
    assert not plan.mass_delete  # 23 z 98 < 30%


def test_unmanaged_events_are_never_touched():
    preview = sample_preview()
    mine = {
        "id": "private",
        "summary": "Dentysta",
        "start": {"dateTime": "2026-10-01T10:00:00+02:00"},
        "end": {"dateTime": "2026-10-01T11:00:00+02:00"},
    }
    plan = plan_sync(desired_of(preview), [mine], preview.coverage, BEFORE_SEMESTER)
    assert plan.unmanaged == [mine]
    assert all(d.existing["id"] != "private" for d in plan.deletes)


def test_past_events_are_neither_added_nor_deleted():
    preview = sample_preview()
    desired = desired_of(preview)
    now = datetime(2026, 11, 10, 12, 0, tzinfo=WARSAW)
    # W kalendarzu jest zarządzane zdarzenie z przeszłości, którego już nie ma w plikach.
    gone = one_event("Stary przedmiot (w) [1]", "2026-10-02 08:00", "2026-10-02 09:35")
    existing = [google_copy(event_body(gone, TEMPLATE, "A"), id="old")]
    plan = plan_sync(desired, existing, preview.coverage, now)
    past = [e for e, _ in desired if e.end <= now]
    assert len(plan.skipped_past) == len(past) > 0
    assert len(plan.adds) == 75 - len(past)
    assert plan.deletes == []
    assert [g["id"] for g in plan.kept_outside] == ["old"]


def test_managed_events_outside_window_are_kept():
    preview = sample_preview()
    later = one_event("Letni semestr (w) [1]", "2027-03-03 08:00", "2027-03-03 09:35")
    existing = [google_copy(event_body(later, TEMPLATE, "A"), id="summer")]
    plan = plan_sync(desired_of(preview), existing, preview.coverage, BEFORE_SEMESTER)
    assert plan.deletes == []
    assert [g["id"] for g in plan.kept_outside] == ["summer"]


def test_duplicates_are_removed_keeping_matching_copy():
    event = one_event()
    body = event_body(event, TEMPLATE, "A")
    stale = google_copy(body, id="stale", location="99 99")
    good = google_copy(body, id="good")
    plan = plan_sync([(event, body)], [stale, good], (event.start, event.end), BEFORE_SEMESTER)
    assert plan.unchanged == 1
    assert plan.updates == []
    assert [(d.existing["id"], d.reason) for d in plan.deletes] == [("stale", "duplikat")]


def test_broken_marker_is_reported_not_touched():
    event = one_event()
    body = event_body(event, TEMPLATE, "A")
    broken = google_copy(body, id="b")
    broken["extendedProperties"] = {"private": {PROP_MANAGED: "1"}}
    plan = plan_sync([], [broken], (event.start, event.end), BEFORE_SEMESTER)
    assert plan.broken == [broken]
    assert plan.deletes == []


def test_mass_delete_flag():
    preview = sample_preview()
    existing = [google_copy(b, id=f"e{i}") for i, (_, b) in enumerate(desired_of(preview))]
    only_one_file = build_preview(
        [CsvFileSource.from_path(NEW_GROUP, id="kier", name="Grupa kierunkowa")]
    )
    # Okno z pełnego zestawu, żeby usunięcia mieściły się w zakresie.
    plan = plan_sync(desired_of(only_one_file), existing, preview.coverage, BEFORE_SEMESTER)
    assert len(plan.deletes) == 45
    assert plan.mass_delete


def test_no_window_means_nothing_deletable():
    event = one_event()
    existing = [google_copy(event_body(event, TEMPLATE, "A"), id="e1")]
    plan = plan_sync([], existing, None, BEFORE_SEMESTER)
    assert plan.deletes == []
    assert len(plan.kept_outside) == 1


@pytest.mark.parametrize("minutes", [0, 1])
def test_event_ending_exactly_now_counts_as_past(minutes):
    event = one_event()
    now = event.end + timedelta(minutes=minutes)
    plan = plan_sync([(event, event_body(event, TEMPLATE, "A"))], [], (event.start, event.end), now)
    assert plan.adds == []
    assert plan.skipped_past == [event]


def test_fake_calendar_roundtrip():
    api = FakeCalendarApi()
    cal = api.create_calendar("Plan WAT", "Europe/Warsaw", "opis")
    body = event_body(one_event(), TEMPLATE, "A")
    api.put_event(cal["id"], body)
    [stored] = api.list_events(cal["id"])
    assert stored["start"]["dateTime"] == "2026-10-30T08:00:00+01:00"
    assert comparable(stored) == comparable(body)
