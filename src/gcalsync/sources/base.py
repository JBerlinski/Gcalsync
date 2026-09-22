"""Protokół źródła planu zajęć.

Źródło dostarcza surowe wiersze (RawEvent) i komunikaty. Dziś jedynym źródłem jest plik CSV
wgrany ręcznie; automatyczne pobieranie z ewig będzie kolejną implementacją tego protokołu.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from gcalsync.model import Issue, RawEvent


@dataclass
class ParsedSource:
    events: list[RawEvent]
    issues: list[Issue] = field(default_factory=list)
    encoding: str | None = None
    delimiter: str | None = None


class Source(Protocol):
    id: str
    name: str

    def read(self) -> ParsedSource: ...
