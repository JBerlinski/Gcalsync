"""Tryb automatyczny (GitHub Actions): pobierz plan z ewig i zsynchronizuj kalendarz.

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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gcalsync.app import SyncPreview
from gcalsync.core.diff import SyncPlan, plan_sync
from gcalsync.core.merge import ConflictPolicy
from gcalsync.core.pipeline import PreviewResult, build_preview
from gcalsync.core.rules import ExclusionRule, RuleError
from gcalsync.gcal.client import CalendarApi
from gcalsync.gcal.executor import ExecutionResult, Journal, execute_plan, verify
from gcalsync.gcal.mapping import event_body, event_times
from gcalsync.sources.ewig import EwigClient, EwigGroup, fetch_sources
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
class AutoConfig:
    semester_iid: int
    groups: list[EwigGroup]  # kolejność = priorytet
    rules: list[ExclusionRule]
    policy: ConflictPolicy
    title_template: str
    calendar: CalendarConfig
    auto_apply: bool  # czy zaplanowane uruchomienia mogą zapisywać


def load_auto_config(path: Path) -> AutoConfig:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Nie można odczytać {path}: {exc}") from exc
    try:
        if data.get("version") != 1:
            raise ConfigError(f"Nieobsługiwana wersja {path.name}: {data.get('version')!r}")
        ewig = data["ewig"]
        groups = [EwigGroup(code=g["code"], name=g["name"]) for g in ewig["groups"]]
        if not groups:
            raise ConfigError("Lista grup w konfiguracji jest pusta.")
        if len({g.name for g in groups}) != len(groups):
            raise ConfigError("Nazwy grup w konfiguracji muszą być unikalne.")
        config = AutoConfig(
            semester_iid=int(ewig["semester_iid"]),
            groups=groups,
            rules=[rule_from_dict(r) for r in data.get("rules", [])],
            policy=ConflictPolicy(data.get("policy", ConflictPolicy.PRIORITY.value)),
            title_template=data.get("title_template", DEFAULT_TITLE_TEMPLATE),
            calendar=CalendarConfig(**data["calendar"]),
            auto_apply=bool(data.get("auto_apply", False)),
        )
    except (KeyError, TypeError, ValueError, RuleError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"Błąd w {path.name}: {exc!r}") from exc
    validate_title_template(config.title_template)
    return config


@dataclass
class AutoResult:
    exit_code: int
    headline: str
    preview: PreviewResult | None = None
    plan: SyncPlan | None = None
    execution: ExecutionResult | None = None
    remaining: list[str] | None = None
    applied: bool = False


def _plan(config: AutoConfig, preview: PreviewResult, api: CalendarApi, now: datetime) -> SyncPlan:
    if api.get_calendar(config.calendar.id) is None:
        raise ConfigError(
            f"Kalendarz „{config.calendar.summary}” ({config.calendar.id}) nie istnieje albo "
            "nie został utworzony przez gcalsync."
        )
    desired = [
        (e, event_body(e, config.title_template, preview.source_name(e.source_id)))
        for e in preview.events
    ]
    existing = api.list_events(config.calendar.id)
    return plan_sync(desired, existing, preview.coverage, now)


def run_auto(
    config: AutoConfig,
    ewig: EwigClient,
    api_factory: Callable[[], CalendarApi],
    *,
    apply: bool,
    allow_mass_delete: bool = False,
    save_dir: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> AutoResult:
    """Wykonuje jedno uruchomienie. Wyjątki ewig/Google/konfiguracji propagują do wywołującego."""
    log(f"Pobieram plan z ewig: {', '.join(g.code for g in config.groups)}")
    sources = fetch_sources(ewig, config.semester_iid, config.groups)
    if save_dir is not None:
        files = save_dir / "files"
        files.mkdir(parents=True, exist_ok=True)
        for group, source in zip(config.groups, sources, strict=True):
            (files / f"{group.code}.csv").write_bytes(source.data)

    preview = build_preview(sources, config.rules, config.policy)
    if preview.has_errors:
        return AutoResult(
            EXIT_FILE_ERRORS, "Błędy w pobranych plikach — nic nie zapisano.", preview=preview
        )

    api = api_factory()
    plan = _plan(config, preview, api, now())
    log(render_sync_text(SyncPreview(preview, plan, config.calendar), apply=apply))

    result = AutoResult(EXIT_OK, "", preview=preview, plan=plan)
    if plan.operation_count == 0:
        result.headline = "Kalendarz jest zgodny z planem — brak zmian."
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
    execution = execute_plan(api, config.calendar.id, plan, Journal.create(runs), sleep=sleep)
    after = _plan(config, preview, api, now())
    remaining = verify(after)
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


# --- podsumowanie dla GitHub Actions (Markdown) -------------------------------------------


def _when(resource: dict[str, Any]) -> str:
    times = event_times(resource)
    return f"{times[0]:%Y-%m-%d %H:%M}" if times else "?"


def markdown_summary(result: AutoResult, limit: int = 60) -> str:
    icon = {EXIT_OK: "✅", EXIT_SAFETY_STOP: "⛔"}.get(result.exit_code, "❌")
    lines = [f"## {icon} gcalsync", "", result.headline, ""]
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
    if result.remaining:
        lines += ["", "Niezgodności po zapisie:"] + [f"- {r}" for r in result.remaining[:20]]
    return "\n".join(lines) + "\n"
