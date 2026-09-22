"""Operacje aplikacji na zapisanej konfiguracji — wspólne dla CLI i (później) UI."""

from __future__ import annotations

from gcalsync.core.pipeline import PreviewResult, build_preview
from gcalsync.storage import Config, ConfigError, Paths


def preview_from_config(paths: Paths, config: Config) -> PreviewResult:
    if not config.sources:
        raise ConfigError(
            "Brak zapisanych źródeł. Dodaj plik: gcalsync sources add PLIK --name NAZWA"
        )
    sources = [s.to_source(paths) for s in config.sources]
    return build_preview(sources, config.rules, config.policy)
