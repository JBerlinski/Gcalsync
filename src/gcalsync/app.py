"""Operacje aplikacji na zapisanej konfiguracji — wspólne dla CLI i (później) UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from gcalsync.core.diff import SyncPlan, plan_sync
from gcalsync.core.pipeline import PreviewResult, build_preview
from gcalsync.gcal.client import CalendarApi
from gcalsync.gcal.mapping import TIME_ZONE, event_body
from gcalsync.storage import (
    DEFAULT_CALENDAR_NAME,
    CalendarConfig,
    Config,
    ConfigError,
    Paths,
    save_config,
)

CALENDAR_DESCRIPTION = "Plan zajęć WAT zarządzany przez gcalsync."


def preview_from_config(paths: Paths, config: Config) -> PreviewResult:
    if not config.sources:
        raise ConfigError(
            "Brak zapisanych źródeł. Dodaj plik: gcalsync sources add PLIK --name NAZWA"
        )
    sources = [s.to_source(paths) for s in config.sources]
    return build_preview(sources, config.rules, config.policy)


@dataclass
class SyncPreview:
    preview: PreviewResult
    plan: SyncPlan
    calendar: CalendarConfig | None

    @property
    def blocked_reason(self) -> str | None:
        """Dlaczego zapis byłby niedozwolony (None = można zapisać po potwierdzeniu)."""
        if self.preview.has_errors:
            return f"{len(self.preview.errors)} błędów w plikach"
        if self.calendar is None:
            return "kalendarz docelowy nie jest utworzony"
        return None


def require_calendar(config: Config, api: CalendarApi) -> CalendarConfig:
    """Sprawdza, że zapisany kalendarz istnieje i jest dostępny dla aplikacji."""
    if config.calendar is None:
        raise ConfigError("Kalendarz docelowy nie jest ustawiony. Utwórz: gcalsync calendar create")
    if api.get_calendar(config.calendar.id) is None:
        raise ConfigError(
            f"Kalendarz „{config.calendar.summary}” ({config.calendar.id}) nie istnieje albo nie "
            "został utworzony przez gcalsync. Utwórz nowy: gcalsync calendar forget, "
            "a potem gcalsync calendar create"
        )
    return config.calendar


def build_sync_preview(
    paths: Paths, config: Config, api: CalendarApi | None, now: datetime | None = None
) -> SyncPreview:
    """Dry-run: porównuje stan docelowy z plików z kalendarzem. Niczego nie zapisuje.

    Bez ustawionego kalendarza (api może być None) plan zakłada pusty kalendarz.
    """
    preview = preview_from_config(paths, config)
    desired = [
        (e, event_body(e, config.title_template, preview.source_name(e.source_id)))
        for e in preview.events
    ]
    existing = []
    calendar = None
    if config.calendar is not None:
        if api is None:
            raise ConfigError("Brak połączenia z Google (wymagane logowanie).")
        calendar = require_calendar(config, api)
        existing = api.list_events(calendar.id)
    plan = plan_sync(desired, existing, preview.coverage, now or datetime.now(UTC))
    return SyncPreview(preview=preview, plan=plan, calendar=calendar)


def create_calendar(
    paths: Paths, config: Config, api: CalendarApi, summary: str = DEFAULT_CALENDAR_NAME
) -> CalendarConfig:
    if config.calendar is not None:
        raise ConfigError(
            f"Kalendarz jest już ustawiony: „{config.calendar.summary}” ({config.calendar.id}). "
            "Aby utworzyć nowy, najpierw: gcalsync calendar forget"
        )
    created = api.create_calendar(summary, TIME_ZONE, CALENDAR_DESCRIPTION)
    config.calendar = CalendarConfig(id=created["id"], summary=created.get("summary", summary))
    save_config(paths, config)
    return config.calendar


def use_calendar(
    paths: Paths, config: Config, api: CalendarApi, calendar_id: str
) -> CalendarConfig:
    found = api.get_calendar(calendar_id)
    if found is None:
        raise ConfigError(
            f"Kalendarz {calendar_id} nie istnieje albo nie został utworzony przez gcalsync "
            "(zakres calendar.app.created obejmuje tylko takie kalendarze)."
        )
    config.calendar = CalendarConfig(id=calendar_id, summary=found.get("summary", calendar_id))
    save_config(paths, config)
    return config.calendar
