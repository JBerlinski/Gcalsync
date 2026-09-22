"""Deduplikacja i rozwiązywanie konfliktów między źródłami według priorytetu."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from gcalsync.model import Event

# Mapowanie id źródła -> priorytet; mniejsza liczba = ważniejsze źródło.
Priority = Mapping[str, int]


@dataclass(frozen=True)
class FieldDifference:
    """Rozbieżność pola między zachowanym egzemplarzem duplikatu a odrzuconym."""

    label: str
    kept_value: str
    other_value: str
    other: Event

    def describe(self, source_names: Mapping[str, str] | None = None) -> str:
        name = (source_names or {}).get(self.other.source_id, self.other.source_id)
        return (
            f"{self.label}: „{self.kept_value}” vs „{self.other_value}” "
            f"({name}, wiersz {self.other.row})"
        )


@dataclass(frozen=True)
class DuplicateGroup:
    """To samo zdarzenie (ten sam klucz) występujące więcej niż raz."""

    kept: Event
    dropped: tuple[Event, ...]
    differences: tuple[FieldDifference, ...]


@dataclass(frozen=True)
class Conflict:
    """Zdarzenie odrzucone, bo nakłada się na zdarzenia z ważniejszego źródła."""

    loser: Event
    winners: tuple[Event, ...]


@dataclass(frozen=True)
class Overlap:
    """Nakładające się zdarzenia, które mimo to zostają (ostrzeżenie)."""

    first: Event
    second: Event


class ConflictPolicy(StrEnum):
    PRIORITY = "priority"  # zdarzenie z ważniejszego źródła wygrywa
    KEEP_ALL = "keep-all"  # nic nie jest odrzucane, kolizje są tylko raportowane


def _differences(kept: Event, other: Event) -> list[FieldDifference]:
    diffs = []
    if kept.location != other.location:
        diffs.append(FieldDifference("lokalizacja", kept.location, other.location, other))
    if kept.seq != other.seq:
        diffs.append(FieldDifference("numer zajęć", f"[{kept.seq}]", f"[{other.seq}]", other))
    return diffs


def deduplicate(
    events: Iterable[Event], priority: Priority
) -> tuple[list[Event], list[DuplicateGroup]]:
    """Jeden egzemplarz na klucz: z najważniejszego źródła, a w nim z pierwszego wiersza."""
    groups: dict[str, list[Event]] = {}
    for event in events:
        groups.setdefault(event.key, []).append(event)

    unique, duplicates = [], []
    for group in groups.values():
        group.sort(key=lambda e: (priority[e.source_id], e.row))
        kept, *dropped = group
        unique.append(kept)
        if dropped:
            diffs = [d for other in dropped for d in _differences(kept, other)]
            duplicates.append(DuplicateGroup(kept, tuple(dropped), tuple(diffs)))
    return unique, duplicates


def overlapping_pairs(events: Iterable[Event]) -> list[tuple[Event, Event]]:
    ordered = sorted(events, key=lambda e: (e.start, e.end))
    pairs = []
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            if b.start >= a.end:
                break
            pairs.append((a, b))
    return pairs


def resolve_conflicts(
    events: Iterable[Event],
    priority: Priority,
    policy: ConflictPolicy = ConflictPolicy.PRIORITY,
) -> tuple[list[Event], list[Conflict], list[Overlap]]:
    """Rozwiązuje kolizje czasowe między źródłami.

    Źródła są przetwarzane od najważniejszego. Zdarzenie odpada, jeśli nakłada się na już
    przyjęte zdarzenie z ważniejszego źródła; odrzucone zdarzenie niczego nie blokuje.
    Kolizje w obrębie jednego źródła (a przy KEEP_ALL — wszystkie) zostają jako ostrzeżenia.
    """
    ordered = sorted(events, key=lambda e: (priority[e.source_id], e.start, e.row))
    accepted: list[Event] = []
    conflicts: list[Conflict] = []
    for event in ordered:
        if policy is ConflictPolicy.PRIORITY:
            rank = priority[event.source_id]
            winners = tuple(
                a for a in accepted if priority[a.source_id] < rank and a.overlaps(event)
            )
            if winners:
                conflicts.append(Conflict(event, winners))
                continue
        accepted.append(event)

    accepted.sort(key=lambda e: (e.start, e.end, priority[e.source_id], e.row))
    conflicts.sort(key=lambda c: (c.loser.start, c.loser.row))
    overlaps = [Overlap(a, b) for a, b in overlapping_pairs(accepted)]
    return accepted, conflicts, overlaps
