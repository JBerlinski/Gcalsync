"""Plan synchronizacji: stan docelowy z plików vs zdarzenia w kalendarzu Google.

Zasady bezpieczeństwa (docs/PLAN.md, sekcja 5):
- modyfikowane są tylko zdarzenia ze znacznikiem gcalsync (extendedProperties.private);
- zdarzenia zakończone (koniec ≤ teraz) są poza synchronizacją: nie są dodawane, zmieniane
  ani usuwane;
- usuwać wolno tylko zarządzane zdarzenia w całości mieszczące się w oknie pokrycia plików.

Ręczne zmiany w Kalendarzu Google (docs/PLAN.md, sekcja 10):
- każde zdarzenie niesie skróty pól w postaci, w jakiej gcalsync je zapisał; pole, które
  różni się i od planu, i od tego skrótu, zmienił człowiek — zostaje, jak jest;
- zajęcia usunięte ręcznie (były w kalendarzu przy poprzednim zapisie, a teraz ich nie ma)
  nie są dodawane ponownie; lista usuniętych jest przechowywana poza zdarzeniami (gcalsync.state);
- zdarzenie z ręcznymi zmianami, którego nie ma już w planie, zostaje; gdy dziekanat przeniesie
  zajęcia dokładnie tam, gdzie przeniesiono je ręcznie, zdarzenie jest przypisywane do nowego
  terminu z planu.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from gcalsync.gcal.mapping import (
    COMPARED_FIELDS,
    MANUAL_GROUPS,
    MARKER_PROPS,
    PROP_COURSE,
    PROP_HASHES,
    comparable,
    decode_hashes,
    encode_hashes,
    event_times,
    group_hashes,
    group_values,
    is_managed,
    managed_key,
    private_props,
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
    manual: list[str] = field(default_factory=list)  # grupy pól zachowane po ręcznej zmianie
    adopted: bool = False  # ręcznie przeniesione zajęcia przypisane do nowego terminu z planu


@dataclass
class ManualKeep:
    """Zdarzenie z ręcznymi zmianami, które zostaje bez zmian wbrew planowi."""

    existing: GoogleEvent
    manual: list[str]
    reason: str  # "zmienione ręcznie" | "zmienione ręcznie, brak w planie"


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
    manual_keeps: list[ManualKeep] = field(default_factory=list)
    deleted_by_user: list[Event] = field(default_factory=list)  # nie są dodawane ponownie
    newly_deleted: list[Event] = field(default_factory=list)  # wykryte w tym uruchomieniu

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
    changes = [FieldChange(f, old[f], new[f]) for f in COMPARED_FIELDS if old[f] != new[f]]
    old_props, new_props = private_props(existing), private_props(desired)
    marker = [p for p in MARKER_PROPS if old_props.get(p) != new_props.get(p)]
    if marker == [PROP_HASHES] and changes:
        marker = []  # skróty wynikają ze zmienionych pól — nie ma czego osobno pokazywać
    if marker:
        changes.append(FieldChange("marker", ", ".join(marker), ""))
    return changes


def _with_manual(body: dict[str, Any], existing: GoogleEvent) -> tuple[dict[str, Any], list[str]]:
    """Treść docelowa z zachowaniem pól zmienionych ręcznie; zwraca też listę takich grup.

    Pole zmienił człowiek, gdy obecna wartość różni się od docelowej i od skrótu tego,
    co gcalsync zapisał ostatnio. Dla zachowanej grupy zostaje stary skrót, więc ochrona
    trwa, dopóki wartość w kalendarzu nie zrówna się z planem.
    """
    stored = decode_hashes(private_props(existing).get(PROP_HASHES))
    if stored is None:
        return body, []
    current, wanted = group_values(existing), group_values(body)
    current_hashes = group_hashes(existing)
    result = copy.deepcopy(body)
    hashes = group_hashes(body)
    manual = []
    for group, fields in MANUAL_GROUPS.items():
        if current[group] == wanted[group] or stored.get(group) in (None, current_hashes[group]):
            continue
        manual.append(group)
        hashes[group] = stored[group]
        for f in fields:
            if f in existing:
                result[f] = copy.deepcopy(existing[f])
            else:
                result.pop(f, None)
    result["extendedProperties"]["private"][PROP_HASHES] = encode_hashes(hashes)
    return result, manual


def _manual_groups(existing: GoogleEvent) -> list[str]:
    """Grupy pól zmienione ręcznie względem tego, co gcalsync zapisał ostatnio."""
    stored = decode_hashes(private_props(existing).get(PROP_HASHES))
    if stored is None:
        return []
    current = group_hashes(existing)
    return [g for g in MANUAL_GROUPS if stored.get(g) not in (None, current[g])]


def plan_sync(
    desired: Sequence[tuple[Event, dict[str, Any]]],
    existing: Sequence[GoogleEvent],
    window: tuple[datetime, datetime] | None,
    now: datetime,
    *,
    deleted_keys: Collection[str] = (),
    seen_keys: Collection[str] = (),
    key_id: Callable[[str], str] = lambda key: key,
) -> SyncPlan:
    """Plan zmian. `deleted_keys` — zajęcia usunięte ręcznie wcześniej, `seen_keys` — zajęcia
    obecne w kalendarzu po poprzednim zapisie (brak takiego teraz = usunięte ręcznie);
    oba w postaci `key_id(klucz)`."""
    plan = SyncPlan(now=now, window=window)
    deleted, seen = set(deleted_keys), set(seen_keys)

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

    # Zdarzenia spoza planu z ręcznie zmienionym czasem — kandydaci do przypisania nowego
    # terminu, gdy dziekanat przeniesie zajęcia tam, gdzie przeniesiono je ręcznie.
    movable: dict[tuple, list[GoogleEvent]] = {}
    for key, group in by_key.items():
        if key in desired_by_key:
            continue
        for g in group:
            if "czas" in _manual_groups(g):
                sig = (private_props(g).get(PROP_COURSE), event_times(g))
                movable.setdefault(sig, []).append(g)
    adopted_ids: set[str] = set()

    for event, body in desired:
        if event.end <= now:
            plan.skipped_past.append(event)
            continue
        candidates = by_key.get(event.key, [])
        if not candidates:
            if key_id(event.key) in deleted:
                plan.deleted_by_user.append(event)
                continue
            if key_id(event.key) in seen:
                plan.deleted_by_user.append(event)
                plan.newly_deleted.append(event)
                continue
            sig = (body["extendedProperties"]["private"].get(PROP_COURSE), (event.start, event.end))
            match = next((g for g in movable.get(sig, []) if g["id"] not in adopted_ids), None)
            if match is None:
                plan.adds.append(PlannedAdd(event, body))
                continue
            adopted_ids.add(match["id"])
            merged, manual = _with_manual(body, match)
            plan.updates.append(
                PlannedUpdate(event, merged, match, _changes(merged, match), manual, adopted=True)
            )
            continue
        # Preferuj egzemplarz, który już ma docelową treść (bez ręcznych zmian, potem z nimi);
        # pozostałe to duplikaty.
        merged_all = [(g, *_with_manual(body, g)) for g in candidates]
        keeper, merged, manual = next(
            (m for m in merged_all if not _changes(body, m[0])),
            next((m for m in merged_all if not _changes(m[1], m[0])), merged_all[0]),
        )
        changes = _changes(merged, keeper)
        if changes:
            plan.updates.append(PlannedUpdate(event, merged, keeper, changes, manual))
        else:
            plan.unchanged += 1
            if manual:
                plan.manual_keeps.append(ManualKeep(keeper, manual, "zmienione ręcznie"))
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
            if g.get("id") in adopted_ids:
                continue
            times = event_times(g)
            if times is None or not _in_scope(times, window, now):
                plan.kept_outside.append(g)
            elif manual := _manual_groups(g):
                plan.manual_keeps.append(ManualKeep(g, manual, "zmienione ręcznie, brak w planie"))
            else:
                plan.deletes.append(PlannedDelete(g, "nieaktualne"))

    plan.adds.sort(key=lambda a: a.event.start)
    plan.updates.sort(key=lambda u: u.event.start)
    plan.deletes.sort(key=lambda d: event_times(d.existing)[0])
    return plan
