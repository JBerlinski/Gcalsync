"""Wspólny model danych: surowe wiersze, zdarzenia i komunikaty."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal

Level = Literal["error", "warning"]


@dataclass(frozen=True)
class Issue:
    """Błąd lub ostrzeżenie powiązane (opcjonalnie) ze źródłem i numerem wiersza."""

    level: Level
    message: str
    source_id: str | None = None
    row: int | None = None

    def __str__(self) -> str:
        where = []
        if self.source_id is not None:
            where.append(self.source_id)
        if self.row is not None:
            where.append(f"wiersz {self.row}")
        prefix = "BŁĄD" if self.level == "error" else "Ostrzeżenie"
        loc = f" [{', '.join(where)}]" if where else ""
        return f"{prefix}{loc}: {self.message}"


@dataclass(frozen=True)
class RawEvent:
    """Wiersz źródła po sparsowaniu pól, jeszcze bez interpretacji tematu i strefy czasowej."""

    row: int  # numer linii w pliku (nagłówek = 1)
    subject: str
    location: str
    start_date: date
    start_time: time
    end_date: date
    end_time: time


@dataclass(frozen=True)
class Event:
    """Znormalizowane zdarzenie z czasem w strefie Europe/Warsaw."""

    source_id: str
    row: int
    subject_raw: str
    course: str
    kind: str | None
    seq: int | None
    location_raw: str
    location: str
    start: datetime
    end: datetime
    key: str  # klucz tożsamości, patrz core.normalize.event_key

    def overlaps(self, other: Event) -> bool:
        """Czy przedziały czasu się nakładają (samo stykanie się nie jest nakładaniem)."""
        return self.start < other.end and other.start < self.end
