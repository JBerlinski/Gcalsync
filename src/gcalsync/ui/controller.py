"""Logika ekranów GUI — bez zależności od NiceGUI, więc da się ją testować w pytest.

Kontroler trzyma bieżącą konfigurację, zapisuje ją po każdej zmianie i przelicza podgląd.
Operacje sieciowe (logowanie, odczyt i zapis kalendarza) są blokujące — GUI wywołuje je
w osobnym wątku (nicegui.run.io_bound).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date

from gcalsync.app import (
    ApplyOutcome,
    SyncPreview,
    apply_sync,
    build_sync_preview,
    create_calendar,
    google_api,
    preview_from_config,
    require_calendar,
)
from gcalsync.core.merge import ConflictPolicy
from gcalsync.core.pipeline import PreviewResult
from gcalsync.core.rules import ExclusionRule, RuleError
from gcalsync.gcal.auth import AuthError, load_credentials, login, logout
from gcalsync.gcal.client import CalendarApi, GoogleApiError
from gcalsync.gcal.executor import Operation, last_run, run_warning
from gcalsync.gcal.mapping import render_title
from gcalsync.storage import (
    DEFAULT_CALENDAR_NAME,
    Config,
    ConfigError,
    Paths,
    SourceConfig,
    add_source_data,
    load_config,
    move_source,
    remove_source,
    rename_source,
    replace_source_data,
    save_config,
    validate_title_template,
)

ENCODINGS = {
    None: "wykrywaj automatycznie",
    "cp1250": "cp1250",
    "iso-8859-2": "ISO-8859-2",
    "utf-8": "UTF-8",
}

# Błędy, które GUI pokazuje jako komunikat (bez śladu stosu).
USER_ERRORS = (ConfigError, RuleError, AuthError, GoogleApiError)


@dataclass
class GoogleStatus:
    has_client_secret: bool
    logged_in: bool
    message: str


class Controller:
    def __init__(
        self,
        paths: Paths,
        api_factory: Callable[[Paths], CalendarApi] = google_api,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.paths = paths
        self.paths.ensure()
        self.api_factory = api_factory
        self.sleep = sleep
        self.config: Config = load_config(paths)
        self.preview: PreviewResult | None = None
        self.preview_error: str | None = None
        self.sync: SyncPreview | None = None  # ostatni dry-run
        self.refresh_preview()

    # --- stan i zapis konfiguracji ---

    def _changed(self) -> None:
        save_config(self.paths, self.config)
        self.sync = None  # wynik dry-runu jest nieaktualny po każdej zmianie
        self.refresh_preview()

    def refresh_preview(self) -> None:
        self.preview, self.preview_error = None, None
        if not self.config.sources:
            return
        try:
            self.preview = preview_from_config(self.paths, self.config)
        except ConfigError as exc:
            self.preview_error = str(exc)

    def run_warning(self) -> str | None:
        return run_warning(last_run(self.paths.runs))

    # --- źródła ---

    def suggest_source_name(self) -> str:
        n = len(self.config.sources) + 1
        names = {s.name for s in self.config.sources}
        while f"Źródło {n}" in names:
            n += 1
        return f"Źródło {n}"

    def add_source(self, data: bytes, filename: str, name: str) -> SourceConfig:
        source = add_source_data(self.paths, self.config, data, filename, name)
        self._changed()
        return source

    def replace_source(self, name: str, data: bytes, filename: str) -> SourceConfig:
        current = self.config.source_by_name(name)
        source = replace_source_data(
            self.paths, self.config, name, data, filename, encoding=current.encoding
        )
        self._changed()
        return source

    def remove_source(self, name: str) -> list[ExclusionRule]:
        _, dropped = remove_source(self.paths, self.config, name)
        self._changed()
        return dropped

    def move_source(self, name: str, delta: int) -> None:
        index = self.config.sources.index(self.config.source_by_name(name))
        target = min(max(index + delta, 0), len(self.config.sources) - 1)
        if target != index:
            move_source(self.config, name, target + 1)
            self._changed()

    def rename_source(self, name: str, new_name: str) -> None:
        rename_source(self.config, name, new_name)
        self._changed()

    def set_source_encoding(self, name: str, encoding: str | None) -> None:
        if encoding not in ENCODINGS:
            raise ConfigError(f"Nieobsługiwane kodowanie: {encoding}")
        self.config.source_by_name(name).encoding = encoding
        self._changed()

    # --- reguły ---

    def courses(self) -> list[str]:
        """Przedmioty występujące w plikach — do szybkiego dodania reguły."""
        if self.preview is None:
            return []
        return sorted({e.course for e in self.preview.parsed})

    def add_rule(
        self,
        field: str,
        op: str,
        value: str,
        case_sensitive: bool = False,
        source_names: list[str] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> ExclusionRule:
        sources = (
            tuple(self.config.source_by_name(n).id for n in source_names) if source_names else None
        )
        rule = ExclusionRule(
            field=field,
            op=op,
            value=value,
            case_sensitive=case_sensitive,
            sources=sources,
            date_from=date_from,
            date_to=date_to,
        )
        if rule in self.config.rules:
            raise ConfigError("Taka reguła już istnieje.")
        self.config.rules.append(rule)
        self._changed()
        return rule

    def set_rule_enabled(self, index: int, enabled: bool) -> None:
        self.config.rules[index] = replace(self.config.rules[index], enabled=enabled)
        self._changed()

    def remove_rule(self, index: int) -> None:
        del self.config.rules[index]
        self._changed()

    def rule_hits(self) -> list[int]:
        if self.preview is None:
            return [0] * len(self.config.rules)
        return self.preview.rule_hits()

    def source_names(self) -> dict[str, str]:
        return {s.id: s.name for s in self.config.sources}

    # --- ustawienia ---

    def set_title_template(self, template: str) -> None:
        validate_title_template(template)
        self.config.title_template = template
        self._changed()

    def set_policy(self, policy: str) -> None:
        self.config.policy = ConflictPolicy(policy)
        self._changed()

    def title_example(self, template: str | None = None) -> str:
        template = template if template is not None else self.config.title_template
        validate_title_template(template)
        if self.preview is None or not self.preview.events:
            return "(brak zdarzeń do pokazania)"
        return render_title(self.preview.events[0], template)

    # --- Google ---

    def google_status(self) -> GoogleStatus:
        """Sprawdza token (może odświeżyć go przez sieć — wywoływać w tle)."""
        has_secret = self.paths.client_secret.exists()
        if not self.paths.token.exists():
            message = (
                "Nie zalogowano."
                if has_secret
                else f"Brak pliku client_secret.json — skopiuj go do: {self.paths.client_secret}"
            )
            return GoogleStatus(has_secret, False, message)
        try:
            load_credentials(self.paths)
        except AuthError as exc:
            return GoogleStatus(has_secret, False, str(exc))
        return GoogleStatus(has_secret, True, "Zalogowano.")

    def login(self) -> None:
        login(self.paths)

    def logout(self) -> bool:
        self.sync = None
        return logout(self.paths)

    def calendar_status(self) -> str:
        if self.config.calendar is None:
            return "Kalendarz docelowy nie jest ustawiony."
        calendar = require_calendar(self.config, self.api_factory(self.paths))
        return f"„{calendar.summary}” — dostępny ({calendar.id})"

    def create_calendar(self, name: str = DEFAULT_CALENDAR_NAME) -> None:
        create_calendar(self.paths, self.config, self.api_factory(self.paths), name)
        self.sync = None

    def forget_calendar(self) -> None:
        self.config.calendar = None
        self._changed()

    # --- synchronizacja ---

    def dry_run(self) -> SyncPreview:
        api = self.api_factory(self.paths) if self.config.calendar is not None else None
        self.sync = build_sync_preview(self.paths, self.config, api)
        self.preview = self.sync.preview
        return self.sync

    def apply(
        self, progress: Callable[[int, int, Operation, str | None], None] | None = None
    ) -> ApplyOutcome:
        if self.sync is None:
            raise ConfigError("Najpierw sprawdź zmiany (dry-run).")
        confirmed, self.sync = self.sync, None
        outcome = apply_sync(
            self.paths,
            self.config,
            self.api_factory(self.paths),
            confirmed,
            progress=progress,
            sleep=self.sleep,
        )
        return outcome
