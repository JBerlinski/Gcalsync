from datetime import UTC, date, datetime, time, timedelta

import pytest
from conftest import DEFAULT_GROUP, make_csv, row

from gcalsync.core.normalize import (
    WARSAW,
    event_key,
    localize,
    normalize_text,
    parse_subject,
    to_events,
)
from gcalsync.sources.outlook_csv import CsvFileSource, parse_outlook_csv


def utc(*args):
    return datetime(*args, tzinfo=UTC)


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Geowizualizacja (L) [3]", ("Geowizualizacja", "L", 3)),
        ("Modelowanie danych do BIM (w) [12]", ("Modelowanie danych do BIM", "w", 12)),
        ("Seminarium dyplomowe (S)", ("Seminarium dyplomowe", "S", None)),
        ("Matematyka  (ć)  [1]", ("Matematyka", "ć", 1)),
        ("Konsultacje", ("Konsultacje", None, None)),
        ("Coś [1]", ("Coś [1]", None, None)),
        ("  Geo\u00a0(w) [1] ", ("Geo", "w", 1)),
    ],
)
def test_parse_subject(subject, expected):
    assert parse_subject(subject) == expected


def test_normalize_text():
    assert normalize_text("A  59") == "A 59"
    assert normalize_text(" 17\t58 ") == "17 58"
    assert normalize_text("e\u0328") == "ę"  # NFD -> NFC


def test_dst_offsets_on_samples():
    source = CsvFileSource.from_path(DEFAULT_GROUP)
    events, issues = to_events(source.read().events, source.id)
    assert issues == []
    by_subject = {e.subject_raw: e for e in events}
    # 23.10.2026 — jeszcze czas letni (+02:00)
    seminar = by_subject["Seminarium dyplomowe (S) [3]"]
    assert seminar.start == datetime(2026, 10, 23, 13, 30, tzinfo=WARSAW)
    assert seminar.start.utcoffset() == timedelta(hours=2)
    assert seminar.start.astimezone(UTC) == utc(2026, 10, 23, 11, 30)
    # 30.10.2026 — już czas zimowy (+01:00)
    bim = by_subject["Modelowanie danych do BIM (L) [1]"]
    assert bim.start.utcoffset() == timedelta(hours=1)
    assert bim.start.astimezone(UTC) == utc(2026, 10, 30, 7, 0)


def test_spring_dst_2027():
    before, _ = localize(date(2027, 3, 26), time(8, 0))
    after, _ = localize(date(2027, 3, 29), time(8, 0))
    assert before.astimezone(UTC) == utc(2027, 3, 26, 7, 0)
    assert after.astimezone(UTC) == utc(2027, 3, 29, 6, 0)


def test_nonexistent_local_time_warns():
    moment, warning = localize(date(2027, 3, 28), time(2, 30))
    assert warning and "nie istnieje" in warning
    assert moment.astimezone(UTC) == utc(2027, 3, 28, 1, 30)
    assert (moment.hour, moment.minute) == (3, 30)


def test_ambiguous_local_time_warns_and_takes_first():
    moment, warning = localize(date(2026, 10, 25), time(2, 30))
    assert warning and "niejednoznaczna" in warning
    assert moment.astimezone(UTC) == utc(2026, 10, 25, 0, 30)


def test_regular_time_has_no_warning():
    assert localize(date(2026, 10, 25), time(8, 0))[1] is None


def test_event_fields_and_location_normalization():
    parsed = parse_outlook_csv(make_csv(row("Analizy (w) [1]", location="A  59")))
    [event], issues = to_events(parsed.events, "B")
    assert issues == []
    assert (event.course, event.kind, event.seq) == ("Analizy", "w", 1)
    assert event.location_raw == "A  59"
    assert event.location == "A 59"
    assert event.source_id == "B"
    assert event.row == 2


def test_key_ignores_seq_location_source_and_case():
    start = datetime(2026, 10, 1, 8, 0, tzinfo=WARSAW)
    end = start + timedelta(minutes=95)
    base = event_key("Geowizualizacja", "L", start, end)
    assert base == event_key("GEOWIZUALIZACJA ", "l", start, end)
    parsed = parse_outlook_csv(
        make_csv(
            row("Geowizualizacja (L) [1]", location="14 58"),
            row("Geowizualizacja (L) [7]", location="13 58"),
        )
    )
    a, b = to_events(parsed.events, "A")[0]
    assert a.key == b.key == base
    assert event_key("Geowizualizacja", "P", start, end) != base
    assert event_key("Geowizualizacja", "L", start, end + timedelta(minutes=5)) != base


def test_key_is_timezone_independent():
    start = datetime(2026, 10, 1, 8, 0, tzinfo=WARSAW)
    end = start + timedelta(minutes=95)
    assert event_key("X", None, start, end) == event_key(
        "X", None, start.astimezone(UTC), end.astimezone(UTC)
    )


def test_end_before_start_is_error():
    parsed = parse_outlook_csv(make_csv(row("X (w) [1]", "2026-10-01 10:00", "2026-10-01 09:00")))
    events, issues = to_events(parsed.events, "A")
    assert events == []
    assert issues[0].level == "error"
    assert issues[0].row == 2


def test_long_and_multiday_events_warn():
    parsed = parse_outlook_csv(
        make_csv(
            row("Długie (w) [1]", "2026-10-01 08:00", "2026-10-01 17:00"),
            row("Wielodniowe (w) [1]", "2026-10-01 08:00", "2026-10-02 09:00"),
        )
    )
    events, issues = to_events(parsed.events, "A")
    assert len(events) == 2
    assert [i.level for i in issues] == ["warning", "warning"]
