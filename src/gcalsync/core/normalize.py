"""Normalizacja surowych wierszy do wspólnego modelu Event."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from gcalsync.model import Event, Issue, RawEvent

WARSAW = ZoneInfo("Europe/Warsaw")

# „Geowizualizacja (L) [3]” -> przedmiot, typ, numer. Numer jest opcjonalny.
SUBJECT_RE = re.compile(r"^(?P<course>.+?)\s*\((?P<kind>[^()]+)\)\s*(?:\[(?P<seq>\d+)\])?$")

LONG_EVENT = timedelta(hours=4)


def normalize_text(value: str) -> str:
    """NFC + zwinięcie wszystkich białych znaków do pojedynczych spacji."""
    return " ".join(unicodedata.normalize("NFC", value).split())


def parse_subject(subject: str) -> tuple[str, str | None, int | None]:
    """Rozbija temat na (przedmiot, typ, numer). Temat bez wzorca -> (temat, None, None)."""
    subject = normalize_text(subject)
    m = SUBJECT_RE.match(subject)
    if not m:
        return subject, None, None
    seq = m.group("seq")
    return m.group("course"), m.group("kind").strip(), int(seq) if seq else None


def localize(d: date, t: time, tz: ZoneInfo = WARSAW) -> tuple[datetime, str | None]:
    """Łączy datę i godzinę lokalną w datetime ze strefą. Zwraca (czas, ostrzeżenie lub None).

    Godziny nieistniejące (przestawienie zegarów do przodu) są przesuwane zgodnie z regułami
    zoneinfo; dla niejednoznacznych (cofnięcie zegarów) przyjmowane jest pierwsze wystąpienie.
    """
    naive = datetime.combine(d, t)
    aware = naive.replace(tzinfo=tz)
    roundtrip = aware.astimezone(UTC).astimezone(tz)
    if roundtrip.replace(tzinfo=None) != naive:
        return roundtrip, (
            f"Godzina {naive:%Y-%m-%d %H:%M} nie istnieje w strefie {tz.key} (zmiana czasu); "
            f"przyjęto {roundtrip:%H:%M}."
        )
    if aware.replace(fold=1).utcoffset() != aware.utcoffset():
        return aware, (
            f"Godzina {naive:%Y-%m-%d %H:%M} jest niejednoznaczna w strefie {tz.key} "
            "(zmiana czasu); przyjęto pierwsze wystąpienie (czas letni)."
        )
    return aware, None


def event_key(course: str, kind: str | None, start: datetime, end: datetime) -> str:
    """Klucz tożsamości zdarzenia: przedmiot, typ i czas (UTC).

    Nie obejmuje numeru [n], lokalizacji ani źródła — patrz docs/PLAN.md, sekcja 3.
    """
    parts = [
        normalize_text(course).casefold(),
        normalize_text(kind or "").casefold(),
        start.astimezone(UTC).isoformat(),
        end.astimezone(UTC).isoformat(),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def to_event(
    raw: RawEvent, source_id: str, tz: ZoneInfo = WARSAW
) -> tuple[Event | None, list[Issue]]:
    issues: list[Issue] = []

    def add(level, message):
        issues.append(Issue(level, message, source_id=source_id, row=raw.row))

    course, kind, seq = parse_subject(raw.subject)
    start, warn = localize(raw.start_date, raw.start_time, tz)
    if warn:
        add("warning", warn)
    end, warn = localize(raw.end_date, raw.end_time, tz)
    if warn:
        add("warning", warn)

    if end <= start:
        add(
            "error",
            f"Koniec ({end:%Y-%m-%d %H:%M}) nie jest późniejszy niż początek "
            f"({start:%Y-%m-%d %H:%M}).",
        )
        return None, issues
    if raw.start_date != raw.end_date:
        add("warning", "Zdarzenie trwa więcej niż jeden dzień.")
    elif end - start > LONG_EVENT:
        add("warning", f"Nietypowo długie zdarzenie ({end - start}).")

    event = Event(
        source_id=source_id,
        row=raw.row,
        subject_raw=raw.subject,
        course=course,
        kind=kind,
        seq=seq,
        location_raw=raw.location,
        location=normalize_text(raw.location),
        start=start,
        end=end,
        key=event_key(course, kind, start, end),
    )
    return event, issues


def to_events(
    raws: Iterable[RawEvent], source_id: str, tz: ZoneInfo = WARSAW
) -> tuple[list[Event], list[Issue]]:
    events, issues = [], []
    for raw in raws:
        event, event_issues = to_event(raw, source_id, tz)
        issues += event_issues
        if event is not None:
            events.append(event)
    return events, issues
