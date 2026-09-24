"""Raport tekstowy planu synchronizacji (dry-run)."""

from __future__ import annotations

from typing import Any

from gcalsync.app import SyncPreview
from gcalsync.core.diff import MASS_DELETE_MIN, MASS_DELETE_RATIO
from gcalsync.core.normalize import WARSAW
from gcalsync.gcal.mapping import FIELD_LABELS, display_time, event_times
from gcalsync.report import format_issue

WEEKDAYS = ("pn", "wt", "śr", "cz", "pt", "so", "nd")


def _format_when(resource: dict[str, Any]) -> str:
    times = event_times(resource)
    if times is None:
        return "(brak czasu)"
    start, end = (t.astimezone(WARSAW) for t in times)
    offset = start.strftime("%z")
    return (
        f"{start:%Y-%m-%d} ({WEEKDAYS[start.weekday()]}) {start:%H:%M}–{end:%H:%M} "
        f"{offset[:3]}:{offset[3:]}"
    )


def _line(resource: dict[str, Any]) -> str:
    parts = [_format_when(resource), resource.get("summary", "(bez tytułu)")]
    if resource.get("location"):
        parts.append(f"sala: {resource['location']}")
    return "  ".join(parts)


def _value(field: str, value: str) -> str:
    if field in ("start", "end"):
        return display_time(value)
    if field == "description":
        return value.replace("\n", " / ")
    return value


def render_sync_text(sync: SyncPreview, apply: bool = False) -> str:
    plan, preview = sync.plan, sync.preview
    out: list[str] = []
    add = out.append

    add("=== Kalendarz docelowy ===")
    if sync.calendar is None:
        add("  Nie utworzono jeszcze kalendarza — plan zakłada pusty kalendarz.")
        add("  Utworzenie (pusty kalendarz „Plan WAT”): gcalsync calendar create")
    else:
        add(f"  {sync.calendar.summary} ({sync.calendar.id})")
        add(
            f"  Zdarzenia w kalendarzu: zarządzane przez gcalsync {plan.managed_total}, "
            f"inne {len(plan.unmanaged)} (nigdy nie są ruszane)"
        )
    if plan.window:
        start, end = plan.window
        add(
            f"  Okno synchronizacji (pokrycie plików): {start:%Y-%m-%d %H:%M} – "
            f"{end:%Y-%m-%d %H:%M}"
        )
    add(f"  Teraz: {plan.now.astimezone():%Y-%m-%d %H:%M} — zakończone zajęcia są pomijane")

    add(
        f"\n=== Z plików: {len(preview.parsed)} zdarzeń, wykluczone {len(preview.excluded)}, "
        f"odrzucone w konfliktach {len(preview.conflicts)}, docelowo {len(preview.events)} ==="
    )
    add("  (szczegóły: gcalsync preview)")

    add(f"\n=== Do dodania ({len(plan.adds)}) ===")
    for a in plan.adds:
        add(f"  + {_line(a.body)}")

    add(f"\n=== Do zmiany ({len(plan.updates)}) ===")
    for u in plan.updates:
        add(f"  ~ {_line(u.body)}")
        if u.adopted:
            add("      zajęcia przeniesione ręcznie — dziekanat wpisał ten sam termin")
        if u.manual:
            add(f"      zachowane ręczne zmiany: {', '.join(u.manual)}")
        for c in u.changes:
            if c.field == "marker":
                add(f"      {FIELD_LABELS[c.field]}: {c.old}")
                continue
            add(
                f"      {FIELD_LABELS[c.field]}: „{_value(c.field, c.old)}” -> "
                f"„{_value(c.field, c.new)}”"
            )

    add(f"\n=== Do usunięcia ({len(plan.deletes)}) ===")
    for d in plan.deletes:
        add(f"  - {_line(d.existing)}  [{d.reason}]")

    add(f"\n=== Bez zmian: {plan.unchanged} ===")

    add("\n=== Pominięte ===")
    add(f"  Zakończone zajęcia z plików (nie są dodawane ani zmieniane): {len(plan.skipped_past)}")
    add(
        "  Zarządzane zdarzenia zakończone lub poza oknem (nie są ruszane): "
        f"{len(plan.kept_outside)}"
    )
    add(
        "  Zmienione ręcznie w Kalendarzu Google (zmiany zachowane): "
        f"{len(plan.manual_keeps) + sum(1 for u in plan.updates if u.manual)}"
    )
    for k in plan.manual_keeps:
        add(f"    ✋ {_line(k.existing)}  [{k.reason}: {', '.join(k.manual)}]")
    add(f"  Usunięte ręcznie (nie są dodawane ponownie): {len(plan.deleted_by_user)}")
    for e in plan.newly_deleted:
        add(f"    ✖ {e.start.astimezone(WARSAW):%Y-%m-%d %H:%M} {e.subject_raw}  [nowe]")
    if plan.broken:
        add(f"  Zdarzenia z uszkodzonym znacznikiem gcalsync (nie są ruszane): {len(plan.broken)}")
        for g in plan.broken:
            add(f"    ? {_line(g)}")

    if preview.warnings:
        add(f"\n=== Ostrzeżenia z plików ({len(preview.warnings)}) ===")
        out.extend(f"  {format_issue(i, preview)}" for i in preview.warnings)
    if preview.errors:
        add(f"\n=== Błędy w plikach ({len(preview.errors)}) ===")
        out.extend(f"  {format_issue(i, preview)}" for i in preview.errors)

    add("\n=== Podsumowanie ===")
    add(
        f"  Dodanie: {len(plan.adds)} | zmiana: {len(plan.updates)} | "
        f"usunięcie: {len(plan.deletes)} | bez zmian: {plan.unchanged}"
    )
    if plan.mass_delete:
        add(
            f"  UWAGA: plan usuwa dużo zdarzeń (co najmniej {MASS_DELETE_MIN} i ponad "
            f"{MASS_DELETE_RATIO:.0%} zarządzanych w oknie). Sprawdź, czy wgrałeś wszystkie "
            "pliki — zapis będzie wymagał dodatkowego potwierdzenia."
        )
    if sync.blocked_reason:
        add(f"  Zapis byłby zablokowany: {sync.blocked_reason}.")
    if apply:
        add("  Powyższe zmiany zostaną zapisane dopiero po potwierdzeniu.")
    else:
        add("  DRY-RUN — nic nie zostało zapisane w kalendarzu. Zapis: gcalsync sync --apply")
    return "\n".join(out) + "\n"
