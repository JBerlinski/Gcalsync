"""Tryb automatyczny (GitHub Actions): pobierz plan z ewig i zsynchronizuj kalendarze.

Konfiguracja może zawierać kilka planów (np. jeden kalendarz na grupę): każdy ma własne
grupy (w kolejności priorytetu), reguły wykluczeń i kalendarz. Każda grupa jest pobierana
z ewig raz, nawet jeśli korzysta z niej kilka planów. Kalendarz planu bez ID jest tworzony
przy pierwszym zapisie, a jego ID dopisywane do pliku konfiguracji.

Konfiguracja bez sekretów leży w repozytorium (gcalsync.config.json). Sekrety przychodzą
ze zmiennych środowiskowych: EWIG_LOGIN, EWIG_PASSWORD, GOOGLE_TOKEN_JSON.

Zapis następuje tylko wtedy, gdy wszystko jest bezpieczne: pliki pobrane i bez błędów,
kalendarz dostępny, bezpiecznik masowego usuwania nie zadziałał (chyba że jawnie pozwolono).
Każdy problem kończy się niezerowym kodem wyjścia — GitHub wyśle wtedy e-mail.
"""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from gcalsync import clock
from gcalsync.app import CALENDAR_DESCRIPTION, SyncPreview
from gcalsync.core.diff import SyncPlan, plan_sync
from gcalsync.core.merge import ConflictPolicy
from gcalsync.core.normalize import WARSAW
from gcalsync.core.pipeline import PreviewResult, build_preview
from gcalsync.core.rules import ExclusionRule, RuleError
from gcalsync.gcal.auth import AuthError
from gcalsync.gcal.client import CalendarApi, GoogleApiError
from gcalsync.gcal.executor import ExecutionResult, Journal, execute_plan, verify
from gcalsync.gcal.mapping import TIME_ZONE, event_body, event_times
from gcalsync.sources.ewig import EwigClient, EwigGroup, fetch_sources, teacher_key
from gcalsync.sources.outlook_csv import CsvFileSource
from gcalsync.state import SyncState, next_state, save_state, short_key, split_state
from gcalsync.storage import (
    DEFAULT_TITLE_TEMPLATE,
    CalendarConfig,
    ConfigError,
    rule_from_dict,
    validate_title_template,
)
from gcalsync.sync_report import render_sync_text

DEFAULT_CONFIG_FILE = "gcalsync.config.json"

EXIT_OK = 0
EXIT_FILE_ERRORS = 1
EXIT_EXTERNAL = 3  # ewig lub Google
EXIT_SAFETY_STOP = 4  # plan wymaga ręcznej decyzji


@dataclass
class PlanCalendar:
    id: str | None  # None = kalendarz zostanie utworzony przy pierwszym zapisie
    summary: str


@dataclass
class PlanConfig:
    name: str
    groups: list[EwigGroup]  # kolejność = priorytet
    rules: list[ExclusionRule]
    calendar: PlanCalendar


@dataclass
class AutoConfig:
    semester_iid: int
    plans: list[PlanConfig]
    policy: ConflictPolicy
    title_template: str
    auto_apply: bool  # czy zaplanowane uruchomienia mogą zapisywać
    path: Path | None = None  # plik konfiguracji — tu trafia ID nowo utworzonego kalendarza

    @property
    def groups(self) -> list[EwigGroup]:
        """Wszystkie grupy do pobrania (każdy kod raz, w kolejności pierwszego wystąpienia)."""
        seen: dict[str, EwigGroup] = {}
        for plan in self.plans:
            for group in plan.groups:
                seen.setdefault(group.code, group)
        return list(seen.values())

    # Zgodność z jednym planem (testy, stary format konfiguracji).
    @property
    def calendar(self) -> PlanCalendar:
        return self.plans[0].calendar

    @property
    def rules(self) -> list[ExclusionRule]:
        return self.plans[0].rules


def _groups(items: list[dict]) -> list[EwigGroup]:
    groups = [EwigGroup(code=g["code"], name=g["name"]) for g in items]
    if not groups:
        raise ConfigError("Lista grup w konfiguracji jest pusta.")
    if len({g.name for g in groups}) != len(groups):
        raise ConfigError("Nazwy grup w planie muszą być unikalne.")
    return groups


def _plan_config(data: dict) -> PlanConfig:
    calendar = data["calendar"]
    return PlanConfig(
        name=data["name"],
        groups=_groups(data["groups"]),
        rules=[rule_from_dict(r) for r in data.get("rules", [])],
        calendar=PlanCalendar(id=calendar.get("id") or None, summary=calendar["summary"]),
    )


def load_auto_config(path: Path) -> AutoConfig:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Nie można odczytać {path}: {exc}") from exc
    try:
        version = data.get("version")
        if version == 1:  # jeden plan: grupy i reguły na najwyższym poziomie
            plans = [
                _plan_config(
                    {
                        "name": data["calendar"]["summary"],
                        "groups": data["ewig"]["groups"],
                        "rules": data.get("rules", []),
                        "calendar": data["calendar"],
                    }
                )
            ]
        elif version == 2:
            plans = [_plan_config(plan) for plan in data["plans"]]
        else:
            raise ConfigError(f"Nieobsługiwana wersja {path.name}: {version!r}")
        if not plans:
            raise ConfigError("Lista planów w konfiguracji jest pusta.")
        if len({p.name for p in plans}) != len(plans):
            raise ConfigError("Nazwy planów w konfiguracji muszą być unikalne.")
        ids = [p.calendar.id for p in plans if p.calendar.id]
        if len(set(ids)) != len(ids):
            raise ConfigError("Dwa plany nie mogą używać tego samego kalendarza.")
        config = AutoConfig(
            semester_iid=int(data["ewig"]["semester_iid"]),
            plans=plans,
            policy=ConflictPolicy(data.get("policy", ConflictPolicy.PRIORITY.value)),
            title_template=data.get("title_template", DEFAULT_TITLE_TEMPLATE),
            auto_apply=bool(data.get("auto_apply", False)),
            path=path,
        )
    except (KeyError, TypeError, ValueError, RuleError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"Błąd w {path.name}: {exc!r}") from exc
    validate_title_template(config.title_template)
    return config


def save_calendar_id(path: Path, plan_name: str, calendar_id: str) -> None:
    """Dopisuje ID utworzonego kalendarza do pliku konfiguracji (format wersji 2)."""
    data = json.loads(path.read_text("utf-8"))
    for plan in data["plans"]:
        if plan["name"] == plan_name:
            plan["calendar"]["id"] = calendar_id
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")


@dataclass
class AutoResult:
    exit_code: int
    headline: str
    preview: PreviewResult | None = None
    plan: SyncPlan | None = None
    execution: ExecutionResult | None = None
    remaining: list[str] | None = None
    applied: bool = False
    state_saved: bool = False
    notes: list[str] | None = None  # ostrzeżenia spoza plików (np. brak prowadzących)
    warning: bool = False  # zakończone bez zapisu, ale bez błędu (np. chwilowa awaria ewig)
    name: str = ""  # nazwa planu (wynik jednego planu)
    plans: list[AutoResult] = field(default_factory=list)  # wyniki planów (wynik zbiorczy)
    calendar_created: str | None = None  # ID kalendarza utworzonego w tym uruchomieniu


def _desired(config: AutoConfig, preview: PreviewResult, sources: list[CsvFileSource]):
    teachers = {source.id: source.teachers for source in sources}
    result = []
    for e in preview.events:
        key = teacher_key(e.course, e.kind, e.seq)
        teacher = teachers.get(e.source_id, {}).get(key) if key else None
        body = event_body(e, config.title_template, preview.source_name(e.source_id), teacher)
        result.append((e, body))
    return result


def _plan(
    config: AutoConfig,
    preview: PreviewResult,
    sources: list[CsvFileSource],
    api: CalendarApi | None,
    calendar_id: str | None,
    now: datetime,
    deleted: set[str] | None = None,
) -> tuple[SyncPlan, SyncState, list[dict[str, Any]]]:
    """Plan zmian dla kalendarza (`calendar_id=None` — kalendarz jeszcze nie istnieje)."""
    state, existing = SyncState(), []
    if calendar_id is not None:
        state, existing = split_state(api.list_events(calendar_id))
    # ewig zawsze daje pełny plan grupy, więc zakres synchronizacji zaczyna się od „teraz”, a nie
    # od pierwszych zajęć w pliku — inaczej zajęcia usunięte z początku planu by zostały.
    window = preview.coverage
    if window is not None:
        window = (min(window[0], now), window[1])
    plan = plan_sync(
        _desired(config, preview, sources),
        existing,
        window,
        now,
        deleted_keys=set(state.deleted) if deleted is None else deleted,
        seen_keys=state.seen,
        key_id=short_key,
    )
    return plan, state, existing


def _plan_sources(plan: PlanConfig, fetched: dict[str, CsvFileSource]) -> list[CsvFileSource]:
    """Źródła planu: pobrane pliki grup pod nazwami grup z tego planu."""
    return [
        CsvFileSource(
            id=g.name,
            name=g.name,
            data=fetched[g.code].data,
            filename=f"{g.code}.csv",
            teachers=fetched[g.code].teachers,
        )
        for g in plan.groups
    ]


SEVERITY = {EXIT_OK: 0, EXIT_FILE_ERRORS: 1, EXIT_SAFETY_STOP: 2, EXIT_EXTERNAL: 3}


def run_auto(
    config: AutoConfig,
    ewig: EwigClient,
    api_factory: Callable[[], CalendarApi],
    *,
    apply: bool,
    allow_mass_delete: bool = False,
    restore_deleted: bool = False,
    save_dir: Path | None = None,
    now: Callable[[], datetime] = lambda: clock.now(),
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> AutoResult:
    """Jedno uruchomienie: pobranie grup z ewig i synchronizacja kolejnych planów.

    Wyjątki ewig propagują do wywołującego (ponowienia i tolerancja są w CLI). Błąd Google
    lub konfiguracji jednego planu nie zatrzymuje pozostałych.
    """
    groups = config.groups
    log(f"Pobieram plan z ewig: {', '.join(g.code for g in groups)}")
    try:
        downloaded = fetch_sources(ewig, config.semester_iid, groups)
    finally:
        for note in ewig.notes:
            log(f"ewig: {note}")
    fetched = {g.code: source for g, source in zip(groups, downloaded, strict=True)}
    if save_dir is not None:
        files = save_dir / "files"
        files.mkdir(parents=True, exist_ok=True)
        for code, source in fetched.items():
            (files / f"{code}.csv").write_bytes(source.data)
    log(
        "Prowadzący odczytani ze strony planu: "
        + ", ".join(f"{code} {len(s.teachers)}" for code, s in fetched.items())
    )
    notes = [
        f"Nie udało się odczytać prowadzących z planu grupy {code} — opisy bez nazwisk."
        for code, source in fetched.items()
        if not source.teachers
    ]
    for note in notes:
        log(f"Ostrzeżenie: {note}")

    api: CalendarApi | None = None

    def google() -> CalendarApi:
        nonlocal api
        if api is None:
            api = api_factory()
        return api

    results = []
    for plan_config in config.plans:
        if len(config.plans) > 1:
            log(f"\n##### Plan {plan_config.name} → kalendarz „{plan_config.calendar.summary}”")
        try:
            result = _run_plan(
                config,
                plan_config,
                _plan_sources(plan_config, fetched),
                google,
                apply=apply,
                allow_mass_delete=allow_mass_delete,
                restore_deleted=restore_deleted,
                save_dir=save_dir,
                now=now,
                sleep=sleep,
                log=log,
            )
        except (GoogleApiError, AuthError, ConfigError) as exc:
            if len(config.plans) == 1:
                raise
            result = AutoResult(EXIT_EXTERNAL, f"Błąd: {exc}")
        result.name = plan_config.name
        results.append(result)
        if len(config.plans) > 1:
            log(f"{plan_config.name}: {result.headline}")

    if len(results) == 1:
        single = results[0]
        single.notes = notes
        single.plans = results
        return single
    worst = max(results, key=lambda r: SEVERITY.get(r.exit_code, 3))
    return AutoResult(
        worst.exit_code,
        " | ".join(f"{r.name}: {r.headline}" for r in results),
        notes=notes,
        plans=results,
    )


def _ensure_calendar(
    config: AutoConfig, plan_config: PlanConfig, api: CalendarApi, apply: bool, log
) -> tuple[str | None, str | None]:
    """ID kalendarza planu (tworzy go przy zapisie, jeśli trzeba) i poprawia jego nazwę.

    Zwraca (ID lub None, gdy kalendarz jeszcze nie istnieje w dry-runie; ID nowo utworzonego).
    """
    calendar = plan_config.calendar
    if calendar.id is None:
        if not apply:
            log(f"Kalendarz „{calendar.summary}” zostanie utworzony przy pierwszym zapisie.")
            return None, None
        created = api.create_calendar(calendar.summary, TIME_ZONE, CALENDAR_DESCRIPTION)
        calendar.id = created["id"]
        log(f"Utworzono kalendarz „{calendar.summary}” ({calendar.id}).")
        if config.path is not None:
            save_calendar_id(config.path, plan_config.name, calendar.id)
        return calendar.id, calendar.id
    existing = api.get_calendar(calendar.id)
    if existing is None:
        raise ConfigError(
            f"Kalendarz „{calendar.summary}” ({calendar.id}) nie istnieje albo "
            "nie został utworzony przez gcalsync."
        )
    if existing.get("summary") != calendar.summary:
        if apply:
            api.patch_calendar(calendar.id, {"summary": calendar.summary})
            log(f"Zmieniono nazwę kalendarza „{existing.get('summary')}” → „{calendar.summary}”.")
        else:
            log(
                f"Nazwa kalendarza „{existing.get('summary')}” zostanie zmieniona na "
                f"„{calendar.summary}” przy zapisie."
            )
    return calendar.id, None


def _run_plan(
    config: AutoConfig,
    plan_config: PlanConfig,
    sources: list[CsvFileSource],
    google: Callable[[], CalendarApi],
    *,
    apply: bool,
    allow_mass_delete: bool,
    restore_deleted: bool,
    save_dir: Path | None,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
    log: Callable[[str], None],
) -> AutoResult:
    preview = build_preview(sources, plan_config.rules, config.policy)
    if preview.has_errors:
        return AutoResult(
            EXIT_FILE_ERRORS, "Błędy w pobranych plikach — nic nie zapisano.", preview=preview
        )

    api = google()
    calendar_id, created = _ensure_calendar(config, plan_config, api, apply, log)
    started = now()
    plan, state, existing = _plan(
        config,
        preview,
        sources,
        api,
        calendar_id,
        started,
        deleted=set() if restore_deleted else None,
    )
    shown = (
        CalendarConfig(id=calendar_id, summary=plan_config.calendar.summary)
        if calendar_id
        else None
    )
    log(render_sync_text(SyncPreview(preview, plan, shown), apply=apply))

    result = AutoResult(EXIT_OK, "", preview=preview, plan=plan, calendar_created=created)
    if plan.operation_count == 0:
        result.headline = "Kalendarz jest zgodny z planem — brak zmian."
        if apply:
            result.state_saved = _save_state(
                api, calendar_id, state, plan, existing, started, restore_deleted
            )
        return result
    if not apply:
        result.headline = (
            f"Dry-run: {plan.operation_count} zmian czeka na zapis (nic nie zapisano)."
        )
        return result
    if plan.mass_delete and not allow_mass_delete:
        result.exit_code = EXIT_SAFETY_STOP
        result.headline = (
            f"Zatrzymano: plan usuwa {len(plan.deletes)} zdarzeń (bezpiecznik). Nic nie zapisano. "
            "Sprawdź plan i, jeśli jest poprawny, uruchom ręcznie z opcją zezwolenia na usuwanie."
        )
        return result

    runs = (save_dir or Path(tempfile.mkdtemp(prefix="gcalsync-"))) / "runs"
    execution = execute_plan(api, calendar_id, plan, Journal.create(runs), sleep=sleep)
    deleted_now = (set() if restore_deleted else set(state.deleted)) | {
        short_key(e.key) for e in plan.newly_deleted
    }
    after, _, events_after = _plan(
        config, preview, sources, api, calendar_id, now(), deleted=deleted_now
    )
    remaining = verify(after)
    result.state_saved = _save_state(
        api, calendar_id, state, plan, events_after, started, restore_deleted
    )
    result.execution, result.remaining, result.applied = execution, remaining, True
    summary = f"Zapisano {execution.done} z {execution.total} zmian"
    if execution.failures or execution.aborted or remaining:
        result.exit_code = EXIT_EXTERNAL
        result.headline = (
            f"{summary}; nieudanych: {len(execution.failures)}"
            + (f", przerwano: {execution.aborted}" if execution.aborted else "")
            + f"; niezgodności po zapisie: {len(remaining)}. Następne uruchomienie dokończy."
        )
    else:
        result.headline = f"{summary}. Weryfikacja: kalendarz zgodny z planem."
    return result


def _save_state(
    api: CalendarApi,
    calendar_id: str,
    state: SyncState,
    plan: SyncPlan,
    events_after: list[dict[str, Any]],
    now: datetime,
    restore: bool,
) -> bool:
    """Zapamiętuje obecne zajęcia i usunięte ręcznie (tylko po zapisie, nie w dry-runie)."""
    new = next_state(
        state,
        events_after,
        [(e.key, e.end) for e in plan.newly_deleted],
        now,
        restore=restore,
    )
    return save_state(api, calendar_id, state, new)


def ewig_outage_result(
    exc: Exception,
    config: AutoConfig,
    api_factory: Callable[[], CalendarApi],
    tolerate: timedelta,
    now: datetime,
) -> AutoResult:
    """Wynik uruchomienia, w którym ewig był niedostępny mimo ponowień.

    Chwilowa awaria ewig to nie powód do alarmu: jeśli ostatnia udana synchronizacja była
    niedawno (w granicy `tolerate`), uruchomienie kończy się bez błędu — GitHub nie wysyła
    wtedy e-maila, a kolejne uruchomienie z harmonogramu spróbuje ponownie. Przy kilku planach
    liczy się najdawniejsza z ostatnich udanych synchronizacji.
    """
    detail = f"ewig niedostępny: {exc}"
    try:
        api = api_factory()
        stamps = [
            split_state(api.list_events(p.calendar.id))[0].last_ok
            for p in config.plans
            if p.calendar.id
        ]
    except (GoogleApiError, AuthError) as google_exc:
        return AutoResult(EXIT_EXTERNAL, f"Błąd: {detail} (stan z Google: {google_exc})")
    known = [s for s in stamps if s is not None]
    if not known:
        return AutoResult(EXIT_EXTERNAL, f"Błąd: {detail}")
    last_ok = min(known)
    since = now - last_ok
    last = last_ok.astimezone(WARSAW).strftime("%Y-%m-%d %H:%M")
    hours = int(since.total_seconds() // 3600)
    if since <= tolerate:
        return AutoResult(
            EXIT_OK,
            f"{detail}. Kalendarz bez zmian — ostatnia udana synchronizacja: {last} "
            f"({hours} h temu). Następne uruchomienie spróbuje ponownie.",
            warning=True,
        )
    return AutoResult(
        EXIT_EXTERNAL,
        f"Błąd: {detail}. Brak udanej synchronizacji od {last} (ponad {hours} h).",
    )


# --- podsumowanie dla GitHub Actions (Markdown) -------------------------------------------


def _when(resource: dict[str, Any]) -> str:
    times = event_times(resource)
    return f"{times[0]:%Y-%m-%d %H:%M}" if times else "?"


def _icon(result: AutoResult) -> str:
    if result.warning:
        return "⚠️"
    return {EXIT_OK: "✅", EXIT_SAFETY_STOP: "⛔"}.get(result.exit_code, "❌")


def markdown_summary(result: AutoResult, limit: int = 60) -> str:
    lines = [f"## {_icon(result)} gcalsync", ""]
    if len(result.plans) > 1:
        lines += [f"- ⚠️ {n}" for n in result.notes or []]
        for plan_result in result.plans:
            lines += ["", f"### {_icon(plan_result)} {plan_result.name}", ""]
            lines += _plan_markdown(plan_result, limit)
    else:
        lines += _plan_markdown(result, limit)
        lines += [f"- ⚠️ {n}" for n in result.notes or []]
    return "\n".join(lines) + "\n"


def _plan_markdown(result: AutoResult, limit: int) -> list[str]:
    lines = [result.headline, ""]
    if result.calendar_created:
        lines += [f"Utworzono kalendarz (ID: `{result.calendar_created}`).", ""]
    p, plan = result.preview, result.plan
    if p is not None:
        lines.append(
            f"Z ewig: {len(p.parsed)} zdarzeń, wykluczone {len(p.excluded)}, "
            f"konflikty {len(p.conflicts)}, docelowo {len(p.events)}."
        )
        lines += [f"- ⚠️ {i}" for i in p.warnings[:20]]
        lines += [f"- ❌ {i}" for i in p.errors[:20]]
    if plan is not None:
        lines += [
            "",
            "| dodanie | zmiana | usunięcie | bez zmian | pominięte zakończone |",
            "|---|---|---|---|---|",
            f"| {len(plan.adds)} | {len(plan.updates)} | {len(plan.deletes)} | "
            f"{plan.unchanged} | {len(plan.skipped_past)} |",
            "",
        ]
        changes = (
            [f"- ➕ {_when(a.body)} {a.body.get('summary', '')}" for a in plan.adds]
            + [
                f"- ✏️ {_when(u.body)} {u.body.get('summary', '')} "
                f"({', '.join(c.field for c in u.changes)})"
                for u in plan.updates
            ]
            + [
                f"- ➖ {_when(d.existing)} {d.existing.get('summary', '')} ({d.reason})"
                for d in plan.deletes
            ]
        )
        lines += changes[:limit]
        if len(changes) > limit:
            lines.append(f"- … i {len(changes) - limit} więcej (pełna lista w logu zadania)")
        manual = [
            f"- ✋ {_when(k.existing)} {k.existing.get('summary', '')} "
            f"({k.reason}: {', '.join(k.manual)})"
            for k in plan.manual_keeps
        ] + [
            f"- ✋ {_when(u.body)} {u.body.get('summary', '')} (zachowane: {', '.join(u.manual)})"
            for u in plan.updates
            if u.manual
        ]
        if manual:
            lines += ["", "Zmiany wprowadzone ręcznie w kalendarzu (zachowane):", *manual[:limit]]
        if plan.deleted_by_user:
            lines += [
                "",
                f"Usunięte ręcznie z kalendarza (nie są dodawane ponownie): "
                f"{len(plan.deleted_by_user)}.",
            ]
            lines += [
                f"- ✖ {e.start.astimezone(WARSAW):%Y-%m-%d %H:%M} {e.subject_raw} (nowe)"
                for e in plan.newly_deleted
            ]
    if result.remaining:
        lines += ["", "Niezgodności po zapisie:"] + [f"- {r}" for r in result.remaining[:20]]
    return lines
