"""Mapowanie Event <-> zasób zdarzenia Google Calendar i porównywanie treści."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from gcalsync.core.normalize import WARSAW
from gcalsync.model import Event

TIME_ZONE = "Europe/Warsaw"

# Znacznik w extendedProperties.private (klucze ≤ 44 znaki, wartości ≤ 1024 znaki).
PROP_MANAGED = "gcalsync_managed"
PROP_KEY = "gcalsync_key"
PROP_VERSION = "gcalsync_v"
MARKER_VERSION = "1"
# Skróty pól w postaci, w jakiej gcalsync je ostatnio zapisał — pozwalają odróżnić zmianę
# wprowadzoną ręcznie w Kalendarzu Google od zmiany w planie (patrz core.diff).
PROP_HASHES = "gcalsync_h"
# Skrót przedmiotu i typu zajęć — do rozpoznania ręcznie przeniesionych zajęć, gdy dziekanat
# przeniesie je w to samo miejsce.
PROP_COURSE = "gcalsync_ck"
MARKER_PROPS = (PROP_MANAGED, PROP_KEY, PROP_VERSION, PROP_HASHES, PROP_COURSE)

# Pola porównywane przy wykrywaniu zmian (przypomnienia celowo pomijamy — to Twoje ustawienie).
COMPARED_FIELDS = ("summary", "location", "description", "start", "end")
FIELD_LABELS = {
    "summary": "tytuł",
    "location": "sala",
    "description": "opis",
    "start": "początek",
    "end": "koniec",
    "marker": "znacznik gcalsync",
}

# Grupy pól, które można zmienić ręcznie; zmiana któregoś pola grupy chroni całą grupę.
MANUAL_GROUPS = {
    "czas": ("start", "end"),
    "sala": ("location",),
    "tytuł": ("summary",),
    "opis": ("description",),
}


def render_title(event: Event, template: str) -> str:
    """Tytuł z szablonu; temat bez typu zajęć (np. „Konsultacje”) daje sam przedmiot."""
    if event.kind is None:
        return event.course
    fields = {
        "course": event.course,
        "kind": event.kind,
        "seq": "" if event.seq is None else str(event.seq),
        "subject": event.subject_raw,
        "location": event.location,
    }
    return " ".join(template.format_map(fields).split())


def render_description(event: Event, source_name: str, teacher: str | None = None) -> str:
    lines = [f"Przedmiot: {event.course}"]
    if event.kind:
        lines.append(f"Typ zajęć: {event.kind}")
    if teacher:
        lines.append(f"Prowadzący: {teacher}")
    if event.seq is not None:
        lines.append(f"Zajęcia nr: {event.seq}")
    lines += [
        f"Źródło: {source_name}",
        "",
        "Plan z e-Dziekanatu (gcalsync). Zmiany wprowadzone tu ręcznie zostają zachowane,",
        "a usuniętych zajęć gcalsync nie dodaje ponownie.",
    ]
    return "\n".join(lines)


def course_signature(course: str, kind: str | None) -> str:
    text = f"{course.casefold()}|{(kind or '').casefold()}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _digest(values: tuple[str, ...]) -> str:
    return hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()[:12]


def group_values(resource: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """Wartości grup pól (znormalizowane jak w `comparable`)."""
    values = comparable(resource)
    return {g: tuple(values[f] for f in fields) for g, fields in MANUAL_GROUPS.items()}


def group_hashes(resource: dict[str, Any]) -> dict[str, str]:
    return {g: _digest(v) for g, v in group_values(resource).items()}


def encode_hashes(hashes: dict[str, str]) -> str:
    return ";".join(f"{g}={h}" for g, h in hashes.items())


def decode_hashes(value: str | None) -> dict[str, str] | None:
    """None = zdarzenie sprzed śledzenia ręcznych zmian (albo uszkodzony znacznik)."""
    if not value:
        return None
    pairs = [part.split("=", 1) for part in value.split(";")]
    if any(len(p) != 2 for p in pairs):
        return None
    return dict(pairs)


def _local_iso(moment: datetime) -> str:
    return moment.astimezone(WARSAW).replace(tzinfo=None).isoformat(timespec="seconds")


def event_body(
    event: Event, title_template: str, source_name: str, teacher: str | None = None
) -> dict[str, Any]:
    """Zasób zdarzenia do wysłania: czas lokalny bez offsetu + timeZone (Google wylicza offset)."""
    body: dict[str, Any] = {
        "summary": render_title(event, title_template),
        "description": render_description(event, source_name, teacher),
        "start": {"dateTime": _local_iso(event.start), "timeZone": TIME_ZONE},
        "end": {"dateTime": _local_iso(event.end), "timeZone": TIME_ZONE},
        "reminders": {"useDefault": True},
        "extendedProperties": {
            "private": {
                PROP_MANAGED: "1",
                PROP_KEY: event.key,
                PROP_VERSION: MARKER_VERSION,
                PROP_COURSE: course_signature(event.course, event.kind),
            }
        },
    }
    if event.location:
        body["location"] = event.location
    body["extendedProperties"]["private"][PROP_HASHES] = encode_hashes(group_hashes(body))
    return body


# --- odczyt zdarzeń z Google --------------------------------------------------------------


def private_props(google_event: dict[str, Any]) -> dict[str, str]:
    return google_event.get("extendedProperties", {}).get("private", {}) or {}


def is_managed(google_event: dict[str, Any]) -> bool:
    return private_props(google_event).get(PROP_MANAGED) == "1"


def managed_key(google_event: dict[str, Any]) -> str | None:
    return private_props(google_event).get(PROP_KEY) or None


def parse_time(value: dict[str, Any]) -> datetime | None:
    """Początek/koniec zdarzenia jako datetime ze strefą (całodniowe: północ w Warszawie)."""
    if not value:
        return None
    if "dateTime" in value:
        moment = datetime.fromisoformat(value["dateTime"])
        if moment.tzinfo is None:  # czas bez offsetu jest w strefie z pola timeZone
            moment = moment.replace(tzinfo=ZoneInfo(value.get("timeZone") or TIME_ZONE))
        return moment
    if "date" in value:
        return datetime.combine(date.fromisoformat(value["date"]), time(), tzinfo=WARSAW)
    return None


def event_times(google_event: dict[str, Any]) -> tuple[datetime, datetime] | None:
    start = parse_time(google_event.get("start", {}))
    end = parse_time(google_event.get("end", {}))
    if start is None or end is None:
        return None
    return start, end


def _time_value(value: dict[str, Any]) -> str:
    if "date" in value and "dateTime" not in value:
        return f"cały dzień {value['date']}"
    moment = parse_time(value)
    return moment.astimezone(UTC).isoformat() if moment else ""


def comparable(resource: dict[str, Any]) -> dict[str, str]:
    """Pola do porównania, znormalizowane tak samo dla naszego zasobu i zdarzenia z Google."""
    return {
        "summary": resource.get("summary", ""),
        "location": resource.get("location", ""),
        "description": resource.get("description", ""),
        "start": _time_value(resource.get("start", {})),
        "end": _time_value(resource.get("end", {})),
    }


def display_time(value: str) -> str:
    """Wartość pola start/end z `comparable` w czytelnej postaci lokalnej."""
    if not value or value.startswith("cały dzień"):
        return value
    return datetime.fromisoformat(value).astimezone(WARSAW).strftime("%Y-%m-%d %H:%M %z")
