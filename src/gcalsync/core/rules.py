"""Reguły wykluczeń zdarzeń (np. przedmiot, z którego się wypisałeś)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Literal

from gcalsync.core.normalize import normalize_text
from gcalsync.model import Event

Field = Literal["subject", "course", "kind", "location"]
Operator = Literal["contains", "equals", "regex"]

FIELD_LABELS: dict[str, str] = {
    "subject": "temat",
    "course": "przedmiot",
    "kind": "typ",
    "location": "lokalizacja",
}
OPERATOR_LABELS: dict[str, str] = {
    "contains": "zawiera",
    "equals": "równa się",
    "regex": "pasuje do wzorca",
}


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class ExclusionRule:
    field: Field
    op: Operator
    value: str
    case_sensitive: bool = False
    enabled: bool = True
    sources: tuple[str, ...] | None = None  # None = wszystkie źródła
    date_from: date | None = None
    date_to: date | None = None

    def __post_init__(self) -> None:
        if self.field not in FIELD_LABELS:
            raise RuleError(f"Nieznane pole reguły: {self.field!r}")
        if self.op not in OPERATOR_LABELS:
            raise RuleError(f"Nieznany operator reguły: {self.op!r}")
        if not self.value.strip():
            raise RuleError("Wartość reguły nie może być pusta.")
        if self.op == "regex":
            try:
                re.compile(self.value)
            except re.error as exc:
                raise RuleError(f"Niepoprawne wyrażenie regularne „{self.value}”: {exc}") from exc
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise RuleError("Początek zakresu dat reguły jest późniejszy niż koniec.")

    def _event_value(self, event: Event) -> str:
        if self.field == "subject":
            return normalize_text(event.subject_raw)
        if self.field == "course":
            return event.course
        if self.field == "kind":
            return event.kind or ""
        return event.location

    def matches(self, event: Event) -> bool:
        if not self.enabled:
            return False
        if self.sources is not None and event.source_id not in self.sources:
            return False
        day = event.start.date()
        if (self.date_from and day < self.date_from) or (self.date_to and day > self.date_to):
            return False

        text = self._event_value(event)
        if self.op == "regex":
            flags = 0 if self.case_sensitive else re.IGNORECASE
            return re.search(self.value, text, flags) is not None
        needle = normalize_text(self.value)
        if not self.case_sensitive:
            text, needle = text.casefold(), needle.casefold()
        return needle in text if self.op == "contains" else needle == text

    def describe(self, source_names: Mapping[str, str] | None = None) -> str:
        """Opis reguły po polsku; `source_names` mapuje id źródeł na nazwy do wyświetlenia."""
        parts = [f"{FIELD_LABELS[self.field]} {OPERATOR_LABELS[self.op]} „{self.value}”"]
        if self.case_sensitive:
            parts.append("z rozróżnieniem wielkości liter")
        if self.sources is not None:
            names = source_names or {}
            parts.append("tylko źródła: " + ", ".join(names.get(s, s) for s in self.sources))
        if self.date_from or self.date_to:
            parts.append(f"daty: {self.date_from or '…'} – {self.date_to or '…'}")
        if not self.enabled:
            parts.append("WYŁĄCZONA")
        return "; ".join(parts)


@dataclass(frozen=True)
class Excluded:
    event: Event
    rule: ExclusionRule


def apply_rules(
    events: Iterable[Event], rules: Iterable[ExclusionRule]
) -> tuple[list[Event], list[Excluded]]:
    """Dzieli zdarzenia na zachowane i wykluczone. Zapamiętywana jest pierwsza pasująca reguła."""
    rules = list(rules)
    kept, excluded = [], []
    for event in events:
        rule = next((r for r in rules if r.matches(event)), None)
        if rule is None:
            kept.append(event)
        else:
            excluded.append(Excluded(event, rule))
    return kept, excluded
