"""Prezentacja podglądu: raport tekstowy (CLI) i słownik do JSON."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from gcalsync.core.pipeline import PreviewResult
from gcalsync.model import Event

WEEKDAYS = ("pn", "wt", "śr", "cz", "pt", "so", "nd")


def _offset(moment: datetime) -> str:
    return moment.strftime("%z")[:3] + ":" + moment.strftime("%z")[3:]


def format_when(event: Event) -> str:
    start, end = event.start, event.end
    day = f"{start:%Y-%m-%d} ({WEEKDAYS[start.weekday()]})"
    if start.date() == end.date():
        return f"{day} {start:%H:%M}–{end:%H:%M} {_offset(start)}"
    return f"{day} {start:%H:%M} {_offset(start)} – {end:%Y-%m-%d %H:%M} {_offset(end)}"


def format_event(event: Event, result: PreviewResult, *, with_source: bool = True) -> str:
    parts = [format_when(event), event.subject_raw]
    if event.location:
        parts.append(f"sala: {event.location}")
    if with_source:
        parts.append(f"[{result.source_name(event.source_id)}]")
    return "  ".join(parts)


def _section(title: str, count: int | None = None) -> str:
    suffix = f" ({count})" if count is not None else ""
    return f"\n=== {title}{suffix} ==="


def _course_breakdown(events: list[Event]) -> list[str]:
    by_course: dict[str, Counter[str]] = {}
    for e in events:
        by_course.setdefault(e.course, Counter())[e.kind or "—"] += 1
    lines = []
    for course in sorted(by_course):
        kinds = by_course[course]
        detail = ", ".join(f"{k} {n}" for k, n in sorted(kinds.items()))
        lines.append(f"  {course}: {sum(kinds.values())} ({detail})")
    return lines


def render_text(result: PreviewResult) -> str:
    out: list[str] = []
    add = out.append

    add(_section("Źródła (od najwyższego priorytetu)"))
    for s in result.sources:
        add(f"{s.priority + 1}. {s.name}" + (f" — plik: {s.filename}" if s.filename else ""))
        info = [f"zdarzeń: {s.event_count}"]
        if s.encoding:
            info.insert(0, f"kodowanie: {s.encoding}")
        if s.delimiter:
            info.insert(1, f"separator: „{'TAB' if s.delimiter == chr(9) else s.delimiter}”")
        if s.first_start and s.last_end:
            info.append(f"zakres: {s.first_start:%Y-%m-%d} – {s.last_end:%Y-%m-%d}")
        add("   " + ", ".join(info))
        for label, n in sorted(s.courses.items()):
            add(f"   - {label}: {n}")

    add(_section("Reguły wykluczeń", len(result.rules)))
    if not result.rules:
        add("  (brak)")
    for rule, hits in zip(result.rules, result.rule_hits(), strict=True):
        add(f"  - {rule.describe()} — wykluczono: {hits}")

    add(_section("Wykluczone przez reguły", len(result.excluded)))
    for x in sorted(result.excluded, key=lambda x: x.event.start):
        add(f"  {format_event(x.event, result)}")

    add(_section("Duplikaty scalone", len(result.duplicates)))
    for d in result.duplicates:
        others = ", ".join(f"{result.source_name(e.source_id)} (wiersz {e.row})" for e in d.dropped)
        add(f"  {format_event(d.kept, result)}")
        add(f"      występuje też w: {others}")
        for diff in d.differences:
            add(f"      rozbieżność — {diff}; użyto wartości z ważniejszego źródła")

    add(_section("Konflikty rozwiązane priorytetem", len(result.conflicts)))
    for c in result.conflicts:
        add(f"  ODRZUCONE: {format_event(c.loser, result)}")
        for w in c.winners:
            add(f"     wygrywa: {format_event(w, result)}")

    add(_section("Kolizje pozostawione (ostrzeżenia)", len(result.overlaps)))
    for o in result.overlaps:
        add(f"  {format_event(o.first, result)}")
        add(f"    nakłada się na: {format_event(o.second, result)}")

    add(_section("Błędy", len(result.errors)))
    out += [f"  {i}" for i in result.errors]
    add(_section("Ostrzeżenia", len(result.warnings)))
    out += [f"  {i}" for i in result.warnings]

    add(_section("Do kalendarza", len(result.events)))
    out += _course_breakdown(result.events)
    add("")
    previous_week = None
    for e in result.events:
        week = e.start.isocalendar()[:2]
        if previous_week is not None and week != previous_week:
            add("")
        previous_week = week
        add(f"  {format_event(e, result)}")

    add(_section("Podsumowanie"))
    dropped_dups = sum(len(d.dropped) for d in result.duplicates)
    add(
        f"  Wejście: {len(result.parsed)} | wykluczone: {len(result.excluded)} | "
        f"duplikaty: {dropped_dups} | odrzucone w konfliktach: {len(result.conflicts)} | "
        f"do kalendarza: {len(result.events)}"
    )
    if result.coverage:
        start, end = result.coverage
        add(f"  Okno pokrycia plików: {start:%Y-%m-%d %H:%M} – {end:%Y-%m-%d %H:%M}")
    if result.has_errors:
        add(
            f"  UWAGA: {len(result.errors)} błędów w plikach — synchronizacja byłaby "
            "zablokowana, dopóki ich nie poprawisz."
        )
    add("  Tryb: podgląd — nic nie zostało zapisane w kalendarzu.")
    return "\n".join(out).lstrip("\n") + "\n"


# --- JSON ---------------------------------------------------------------------------------


def event_to_dict(event: Event, result: PreviewResult | None = None) -> dict[str, Any]:
    return {
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "subject": event.subject_raw,
        "course": event.course,
        "kind": event.kind,
        "seq": event.seq,
        "location": event.location,
        "source": result.source_name(event.source_id) if result else event.source_id,
        "row": event.row,
        "key": event.key,
    }


def preview_to_dict(result: PreviewResult) -> dict[str, Any]:
    coverage = result.coverage
    return {
        "sources": [
            {
                "name": s.name,
                "priority": s.priority + 1,
                "filename": s.filename,
                "encoding": s.encoding,
                "delimiter": s.delimiter,
                "events": s.event_count,
                "courses": dict(sorted(s.courses.items())),
            }
            for s in result.sources
        ],
        "rules": [
            {"rule": r.describe(), "excluded": hits}
            for r, hits in zip(result.rules, result.rule_hits(), strict=True)
        ],
        "policy": str(result.policy),
        "coverage": (
            {"start": coverage[0].isoformat(), "end": coverage[1].isoformat()} if coverage else None
        ),
        "events": [event_to_dict(e, result) for e in result.events],
        "excluded": [
            {"event": event_to_dict(x.event, result), "rule": x.rule.describe()}
            for x in result.excluded
        ],
        "duplicates": [
            {
                "kept": event_to_dict(d.kept, result),
                "dropped": [event_to_dict(e, result) for e in d.dropped],
                "differences": list(d.differences),
            }
            for d in result.duplicates
        ],
        "conflicts": [
            {
                "dropped": event_to_dict(c.loser, result),
                "winners": [event_to_dict(w, result) for w in c.winners],
            }
            for c in result.conflicts
        ],
        "overlaps": [
            [event_to_dict(o.first, result), event_to_dict(o.second, result)]
            for o in result.overlaps
        ],
        "issues": [
            {"level": i.level, "source": i.source_id, "row": i.row, "message": i.message}
            for i in result.issues
        ],
        "summary": {
            "parsed": len(result.parsed),
            "excluded": len(result.excluded),
            "duplicates": sum(len(d.dropped) for d in result.duplicates),
            "conflicts": len(result.conflicts),
            "events": len(result.events),
            "errors": len(result.errors),
            "warnings": len(result.warnings),
        },
    }
