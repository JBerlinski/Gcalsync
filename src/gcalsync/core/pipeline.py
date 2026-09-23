"""Cały potok: źródła -> parsowanie -> wykluczenia -> deduplikacja -> konflikty -> podgląd."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from gcalsync.core.merge import (
    Conflict,
    ConflictPolicy,
    DuplicateGroup,
    Overlap,
    deduplicate,
    resolve_conflicts,
)
from gcalsync.core.normalize import WARSAW, to_events
from gcalsync.core.rules import Excluded, ExclusionRule, apply_rules
from gcalsync.model import Event, Issue
from gcalsync.sources.base import Source


@dataclass
class SourceSummary:
    id: str
    name: str
    priority: int  # 0 = najważniejsze
    filename: str | None
    encoding: str | None
    delimiter: str | None
    event_count: int
    first_start: datetime | None
    last_end: datetime | None
    courses: Counter[str]  # „Przedmiot (typ)” -> liczba zdarzeń
    issues: list[Issue]


@dataclass
class PreviewResult:
    sources: list[SourceSummary]
    rules: list[ExclusionRule]
    policy: ConflictPolicy
    parsed: list[Event]  # wszystkie poprawnie sparsowane zdarzenia
    events: list[Event]  # stan docelowy, posortowany po czasie
    excluded: list[Excluded]
    duplicates: list[DuplicateGroup]
    conflicts: list[Conflict]
    overlaps: list[Overlap]
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    @property
    def coverage(self) -> tuple[datetime, datetime] | None:
        """Okno pokrycia wgranych plików: od najwcześniejszego początku do najpóźniejszego końca
        wśród wszystkich sparsowanych zdarzeń (także wykluczonych). Synchronizacja nie będzie
        zmieniać zdarzeń poza tym oknem."""
        if not self.parsed:
            return None
        return min(e.start for e in self.parsed), max(e.end for e in self.parsed)

    def rule_hits(self) -> list[int]:
        """Liczba zdarzeń wykluczonych przez każdą regułę (w kolejności `rules`)."""
        counts = Counter(id(x.rule) for x in self.excluded)
        return [counts[id(rule)] for rule in self.rules]

    @property
    def source_names(self) -> dict[str, str]:
        return {s.id: s.name for s in self.sources}

    def source_name(self, source_id: str) -> str:
        return self.source_names.get(source_id, source_id)


def course_label(event: Event) -> str:
    return f"{event.course} ({event.kind})" if event.kind else event.course


def build_preview(
    sources: Sequence[Source],
    rules: Iterable[ExclusionRule] = (),
    policy: ConflictPolicy = ConflictPolicy.PRIORITY,
    tz: ZoneInfo = WARSAW,
) -> PreviewResult:
    """Przetwarza źródła w kolejności priorytetu (pierwsze = najważniejsze)."""
    ids = [s.id for s in sources]
    if len(set(ids)) != len(ids):
        raise ValueError(f"Identyfikatory źródeł muszą być unikalne: {ids}")
    priority = {s.id: i for i, s in enumerate(sources)}
    rules = list(rules)

    summaries: list[SourceSummary] = []
    parsed_events: list[Event] = []
    issues: list[Issue] = []
    seen_content: dict[str, str] = {}

    for rank, source in enumerate(sources):
        parsed = source.read()
        events, event_issues = to_events(parsed.events, source.id, tz)
        source_issues = parsed.issues + event_issues

        digest = getattr(source, "sha256", None)
        if digest is not None:
            if digest in seen_content:
                source_issues.append(
                    Issue(
                        "warning",
                        f"Ten sam plik co w źródle „{seen_content[digest]}”.",
                        source_id=source.id,
                    )
                )
            else:
                seen_content[digest] = source.name

        summaries.append(
            SourceSummary(
                id=source.id,
                name=source.name,
                priority=rank,
                filename=getattr(source, "filename", None) or None,
                encoding=parsed.encoding,
                delimiter=parsed.delimiter,
                event_count=len(events),
                first_start=min((e.start for e in events), default=None),
                last_end=max((e.end for e in events), default=None),
                courses=Counter(course_label(e) for e in events),
                issues=source_issues,
            )
        )
        parsed_events += events
        issues += source_issues

    kept, excluded = apply_rules(parsed_events, rules)
    unique, duplicates = deduplicate(kept, priority)
    final, conflicts, overlaps = resolve_conflicts(unique, priority, policy)

    return PreviewResult(
        sources=summaries,
        rules=rules,
        policy=policy,
        parsed=parsed_events,
        events=final,
        excluded=excluded,
        duplicates=duplicates,
        conflicts=conflicts,
        overlaps=overlaps,
        issues=issues,
    )
