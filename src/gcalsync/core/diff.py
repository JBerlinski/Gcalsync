"""Plan synchronizacji: stan docelowy z plików vs zdarzenia w kalendarzu Google.

Zasady bezpieczeństwa (docs/PLAN.md, sekcja 5):
- modyfikowane są tylko zdarzenia ze znacznikiem gcalsync (extendedProperties.private);
- zdarzenia zakończone (koniec ≤ teraz) są poza synchronizacją: nie są dodawane, zmieniane
  ani usuwane;
- usuwać wolno tylko zarządzane zdarzenia w całości mieszczące się w oknie pokrycia plików.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from gcalsync.gcal.mapping import (
    COMPARED_FIELDS,
    comparable,
    event_times,
    is_managed,
    managed_key,
)
from gcalsync.model import Event

# Bezpiecznik: tyle usunięć (i jednocześnie taki odsetek zarządzanych zdarzeń w oknie)
# wymaga dodatkowego potwierdzenia przy zapisie.
MASS_DELETE_MIN = 5
MASS_DELETE_RATIO = 0.3

GoogleEvent = dict[str, Any]


@dataclass(frozen=True)
class FieldChange:
    field: str
    old: str
    new: str


@dataclass
class PlannedAdd:
    event: Event
    body: dict[str, Any]


@dataclass
class PlannedUpdate:
    event: Event
    body: dict[str, Any]
    existing: GoogleEvent
    changes: list[FieldChange]


@dataclass
class PlannedDelete:
    existing: GoogleEvent
    reason: str  # "nieaktualne" | "duplikat"


@dataclass
class SyncPlan:
    now: datetime
    window: tuple[datetime, datetime] | None
    adds: list[PlannedAdd] = field(default_factory=list)
    updates: list[PlannedUpdate] = field(default_factory=list)
    deletes: list[PlannedDelete] = field(default_factory=list)
    unchanged: int = 0
    skipped_past: list[Event] = field(default_factory=list)  # zakończone zajęcia z plików
    kept_outside: list[GoogleEvent] = field(default_factory=list)  # zarządzane, poza zakresem
    unmanaged: list[GoogleEvent] = field(default_factory=list)
    broken: list[GoogleEvent] = field(default_factory=list)  # znacznik bez klucza
    managed_total: int = 0  # wszystkie zdarzenia ze znacznikiem gcalsync
    managed_in_scope: int = 0  # zarządzane zdarzenia, które synchronizacja może zmieniać

    @property
    def operation_count(self) -> int:
        return len(self.adds) + len(self.updates) + len(self.deletes)

    @property
    def mass_delete(self) -> bool:
        obsolete = sum(d.reason == "nieaktualne" for d in self.deletes)
        return obsolete >= MASS_DELETE_MIN and obsolete > MASS_DELETE_RATIO * max(
            self.managed_in_scope, 1
        )


def _in_scope(times: tuple[datetime, datetime], window, now: datetime) -> bool:
    start, end = times
    if end <= now or window is None:
        return False
    return start >= window[0] and end <= window[1]


def _changes(desired: dict[str, Any], existing: GoogleEvent) -> list[FieldChange]:
    new, old = comparable(desired), comparable(existing)
    return [FieldChange(f, old[f], new[f]) for f in COMPARED_FIELDS if old[f] != new[f]]


def plan_sync(
    desired: Sequence[tuple[Event, dict[str, Any]]],
    existing: Sequence[GoogleEvent],
    window: tuple[datetime, datetime] | None,
    now: datetime,
) -> SyncPlan:
    plan = SyncPlan(now=now, window=window)

    by_key: dict[str, list[GoogleEvent]] = {}
    for g in existing:
        if not is_managed(g):
            plan.unmanaged.append(g)
            continue
        plan.managed_total += 1
        key = managed_key(g)
        times = event_times(g)
        if key is None or times is None:
            plan.broken.append(g)
            continue
        by_key.setdefault(key, []).append(g)
        if _in_scope(times, window, now):
            plan.managed_in_scope += 1

    desired_by_key = {event.key: (event, body) for event, body in desired}

    for event, body in desired:
        if event.end <= now:
            plan.skipped_past.append(event)
            continue
        candidates = by_key.get(event.key, [])
        if not candidates:
            plan.adds.append(PlannedAdd(event, body))
            continue
        # Preferuj egzemplarz, który już ma docelową treść; pozostałe to duplikaty.
        keeper = next((g for g in candidates if not _changes(body, g)), candidates[0])
        changes = _changes(body, keeper)
        if changes:
            plan.updates.append(PlannedUpdate(event, body, keeper, changes))
        else:
            plan.unchanged += 1
        for extra in candidates:
            if extra is keeper:
                continue
            times = event_times(extra)
            if times is not None and _in_scope(times, window, now):
                plan.deletes.append(PlannedDelete(extra, "duplikat"))
            else:
                plan.kept_outside.append(extra)

    for key, group in by_key.items():
        if key in desired_by_key:
            continue
        for g in group:
            times = event_times(g)
            if times is not None and _in_scope(times, window, now):
                plan.deletes.append(PlannedDelete(g, "nieaktualne"))
            else:
                plan.kept_outside.append(g)

    plan.adds.sort(key=lambda a: a.event.start)
    plan.updates.sort(key=lambda u: u.event.start)
    plan.deletes.sort(key=lambda d: event_times(d.existing)[0])
    return plan
