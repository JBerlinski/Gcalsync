"""Dane lokalne: katalog aplikacji, config.json i kopie wgranych plików CSV.

Wszystko leży poza repozytorium, w katalogu użytkownika:
- Windows: %LOCALAPPDATA%\\gcalsync (np. C:\\Users\\<nazwa>\\AppData\\Local\\gcalsync),
- Linux: ~/.config/gcalsync, macOS: ~/Library/Application Support/gcalsync.
Zmienna środowiskowa GCALSYNC_HOME nadpisuje to położenie.
"""

from __future__ import annotations

import json
import os
import secrets
import string
import tempfile
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import platformdirs

from gcalsync.core.merge import ConflictPolicy
from gcalsync.core.rules import ExclusionRule, RuleError
from gcalsync.sources.outlook_csv import CsvFileSource

APP_NAME = "gcalsync"
CONFIG_VERSION = 1
DEFAULT_TITLE_TEMPLATE = "{course} ({kind})"
DEFAULT_CALENDAR_NAME = "Plan WAT"
TITLE_FIELDS = ("course", "kind", "seq", "subject", "location")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Paths:
    root: Path

    @classmethod
    def default(cls) -> Paths:
        override = os.environ.get("GCALSYNC_HOME")
        if override:
            return cls(Path(override).expanduser())
        # appauthor=False: bez dodatkowego poziomu katalogu „autora” na Windows.
        return cls(Path(platformdirs.user_config_dir(APP_NAME, appauthor=False, roaming=False)))

    @property
    def config(self) -> Path:
        return self.root / "config.json"

    @property
    def client_secret(self) -> Path:
        return self.root / "client_secret.json"

    @property
    def token(self) -> Path:
        return self.root / "token.json"

    @property
    def files(self) -> Path:
        return self.root / "files"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    def ensure(self) -> None:
        for d in (self.root, self.files, self.runs):
            d.mkdir(parents=True, exist_ok=True)


# --- model konfiguracji -------------------------------------------------------------------


@dataclass
class SourceConfig:
    id: str
    name: str
    file: str  # ścieżka względem katalogu aplikacji, np. files/src-ab12cd.csv
    original_filename: str
    sha256: str
    added: str  # ISO 8601
    encoding: str | None = None

    def to_source(self, paths: Paths) -> CsvFileSource:
        path = paths.root / self.file
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ConfigError(
                f"Brak kopii pliku źródła „{self.name}” ({path}): {exc.strerror or exc}. "
                "Podmień plik poleceniem: gcalsync sources replace."
            ) from exc
        return CsvFileSource(
            id=self.id,
            name=self.name,
            data=data,
            filename=self.original_filename,
            encoding=self.encoding,
        )


@dataclass
class CalendarConfig:
    id: str
    summary: str


@dataclass
class Config:
    sources: list[SourceConfig] = field(default_factory=list)  # kolejność = priorytet
    rules: list[ExclusionRule] = field(default_factory=list)
    policy: ConflictPolicy = ConflictPolicy.PRIORITY
    title_template: str = DEFAULT_TITLE_TEMPLATE
    calendar: CalendarConfig | None = None

    # --- źródła ---

    def source_by_name(self, name: str) -> SourceConfig:
        for s in self.sources:
            if s.name == name:
                return s
        known = ", ".join(f"„{s.name}”" for s in self.sources) or "(brak)"
        raise ConfigError(f"Nie ma źródła „{name}”. Zapisane źródła: {known}.")

    def source_name(self, source_id: str) -> str:
        return next((s.name for s in self.sources if s.id == source_id), source_id)


def validate_title_template(template: str) -> None:
    formatter = string.Formatter()
    try:
        names = [name for _, name, _, _ in formatter.parse(template) if name is not None]
    except ValueError as exc:
        raise ConfigError(f"Niepoprawny szablon tytułu „{template}”: {exc}") from exc
    unknown = [n for n in names if n not in TITLE_FIELDS]
    if unknown or "" in names:
        raise ConfigError(
            f"Nieznane pola w szablonie tytułu: {', '.join(unknown) or '{}'}. "
            f"Dozwolone: {', '.join('{' + f + '}' for f in TITLE_FIELDS)}."
        )
    if not template.strip():
        raise ConfigError("Szablon tytułu nie może być pusty.")


# --- serializacja -------------------------------------------------------------------------


def _rule_to_dict(rule: ExclusionRule) -> dict[str, Any]:
    return {
        "field": rule.field,
        "op": rule.op,
        "value": rule.value,
        "case_sensitive": rule.case_sensitive,
        "enabled": rule.enabled,
        "sources": list(rule.sources) if rule.sources is not None else None,
        "date_from": rule.date_from.isoformat() if rule.date_from else None,
        "date_to": rule.date_to.isoformat() if rule.date_to else None,
    }


def _rule_from_dict(data: dict[str, Any]) -> ExclusionRule:
    return ExclusionRule(
        field=data["field"],
        op=data["op"],
        value=data["value"],
        case_sensitive=bool(data.get("case_sensitive", False)),
        enabled=bool(data.get("enabled", True)),
        sources=tuple(data["sources"]) if data.get("sources") is not None else None,
        date_from=date.fromisoformat(data["date_from"]) if data.get("date_from") else None,
        date_to=date.fromisoformat(data["date_to"]) if data.get("date_to") else None,
    )


def config_to_dict(config: Config) -> dict[str, Any]:
    return {
        "version": CONFIG_VERSION,
        "sources": [vars(s).copy() for s in config.sources],
        "rules": [_rule_to_dict(r) for r in config.rules],
        "policy": config.policy.value,
        "title_template": config.title_template,
        "calendar": vars(config.calendar).copy() if config.calendar else None,
    }


def config_from_dict(data: dict[str, Any]) -> Config:
    version = data.get("version")
    if version != CONFIG_VERSION:
        raise ConfigError(f"Nieobsługiwana wersja config.json: {version!r}")
    try:
        config = Config(
            sources=[SourceConfig(**s) for s in data.get("sources", [])],
            rules=[_rule_from_dict(r) for r in data.get("rules", [])],
            policy=ConflictPolicy(data.get("policy", ConflictPolicy.PRIORITY.value)),
            title_template=data.get("title_template", DEFAULT_TITLE_TEMPLATE),
            calendar=CalendarConfig(**data["calendar"]) if data.get("calendar") else None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, (ConfigError, RuleError)):
            raise ConfigError(f"Błąd w config.json: {exc}") from exc
        raise ConfigError(f"Uszkodzony config.json: {exc!r}") from exc
    validate_title_template(config.title_template)
    return config


# --- odczyt i zapis -----------------------------------------------------------------------


def _atomic_write(path: Path, data: bytes) -> None:
    """Zapis przez plik tymczasowy + os.replace, żeby przerwanie nie zostawiło połowy pliku."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_private(path: Path, data: bytes) -> None:
    """Zapis pliku z danymi wrażliwymi (token); na Linux/macOS z prawami 0600."""
    _atomic_write(path, data)
    if os.name != "nt":
        path.chmod(0o600)


def load_config(paths: Paths) -> Config:
    if not paths.config.exists():
        return Config()
    try:
        data = json.loads(paths.config.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Nie można odczytać {paths.config}: {exc}") from exc
    return config_from_dict(data)


def save_config(paths: Paths, config: Config) -> None:
    text = json.dumps(config_to_dict(config), ensure_ascii=False, indent=2) + "\n"
    _atomic_write(paths.config, text.encode("utf-8"))


# --- operacje na źródłach -----------------------------------------------------------------


def _new_source_id(config: Config) -> str:
    existing = {s.id for s in config.sources}
    while True:
        candidate = "src-" + secrets.token_hex(3)
        if candidate not in existing:
            return candidate


def _store_copy(paths: Paths, source_id: str, data: bytes) -> str:
    rel = f"files/{source_id}.csv"
    _atomic_write(paths.root / rel, data)
    return rel


def add_source(
    paths: Paths, config: Config, file: Path, name: str, encoding: str | None = None
) -> SourceConfig:
    if any(s.name == name for s in config.sources):
        raise ConfigError(f"Źródło „{name}” już istnieje. Użyj: gcalsync sources replace.")
    data = file.read_bytes()
    source_id = _new_source_id(config)
    csv = CsvFileSource(id=source_id, name=name, data=data)
    source = SourceConfig(
        id=source_id,
        name=name,
        file=_store_copy(paths, source_id, data),
        original_filename=file.name,
        sha256=csv.sha256,
        added=datetime.now(UTC).isoformat(timespec="seconds"),
        encoding=encoding,
    )
    config.sources.append(source)
    return source


def replace_source_file(
    paths: Paths, config: Config, name: str, file: Path, encoding: str | None = None
) -> SourceConfig:
    source = config.source_by_name(name)
    data = file.read_bytes()
    source.file = _store_copy(paths, source.id, data)
    source.original_filename = file.name
    source.sha256 = CsvFileSource(id=source.id, name=name, data=data).sha256
    source.added = datetime.now(UTC).isoformat(timespec="seconds")
    source.encoding = encoding
    return source


def remove_source(
    paths: Paths, config: Config, name: str
) -> tuple[SourceConfig, list[ExclusionRule]]:
    """Usuwa źródło i kopię jego pliku. Reguły ograniczone wyłącznie do tego źródła są usuwane
    (inaczej zaczęłyby działać na wszystkie źródła); zwraca je, żeby pokazać użytkownikowi."""
    source = config.source_by_name(name)
    config.sources.remove(source)
    (paths.root / source.file).unlink(missing_ok=True)
    kept, dropped = [], []
    for rule in config.rules:
        if rule.sources and source.id in rule.sources:
            remaining = tuple(s for s in rule.sources if s != source.id)
            if not remaining:
                dropped.append(rule)
                continue
            rule = replace(rule, sources=remaining)
        kept.append(rule)
    config.rules = kept
    return source, dropped


def move_source(config: Config, name: str, position: int) -> None:
    """Ustawia źródło na pozycji `position` (1 = najwyższy priorytet)."""
    source = config.source_by_name(name)
    if not 1 <= position <= len(config.sources):
        raise ConfigError(f"Pozycja musi być z zakresu 1–{len(config.sources)}.")
    config.sources.remove(source)
    config.sources.insert(position - 1, source)


def set_rule_enabled(config: Config, index: int, enabled: bool) -> ExclusionRule:
    rule = rule_at(config, index)
    config.rules[index - 1] = replace(rule, enabled=enabled)
    return config.rules[index - 1]


def rule_at(config: Config, index: int) -> ExclusionRule:
    if not 1 <= index <= len(config.rules):
        raise ConfigError(f"Nie ma reguły nr {index} (zapisanych reguł: {len(config.rules)}).")
    return config.rules[index - 1]
