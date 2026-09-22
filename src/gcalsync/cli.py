"""Interfejs wiersza poleceń gcalsync."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from gcalsync import __version__
from gcalsync.app import preview_from_config
from gcalsync.core.merge import ConflictPolicy
from gcalsync.core.pipeline import PreviewResult, build_preview
from gcalsync.core.rules import FIELD_LABELS, OPERATOR_LABELS, ExclusionRule, RuleError
from gcalsync.report import preview_to_dict, render_text
from gcalsync.sources.outlook_csv import CsvFileSource
from gcalsync.storage import (
    Config,
    ConfigError,
    Paths,
    add_source,
    load_config,
    move_source,
    remove_source,
    replace_source_file,
    rule_at,
    save_config,
    set_rule_enabled,
    validate_title_template,
)

EXIT_OK = 0
EXIT_FILE_ERRORS = 1
EXIT_USAGE = 2


class CliError(Exception):
    """Błąd do pokazania użytkownikowi bez śladu stosu (kod wyjścia 2)."""


# --- parser -------------------------------------------------------------------------------


def _add_rule_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--exclude-course",
        action="append",
        default=[],
        metavar="PRZEDMIOT",
        help="wyklucz przedmiot o dokładnie tej nazwie (bez typu i numeru); można powtarzać",
    )
    parser.add_argument(
        "--exclude-contains",
        action="append",
        default=[],
        metavar="TEKST",
        help="wyklucz zdarzenia, których temat zawiera tekst; można powtarzać",
    )
    parser.add_argument(
        "--exclude-regex",
        action="append",
        default=[],
        metavar="WZORZEC",
        help="wyklucz zdarzenia, których temat pasuje do wyrażenia regularnego",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gcalsync",
        description="Synchronizacja planu zajęć WAT (CSV z ewig) z Google Calendar.",
    )
    parser.add_argument("--version", action="version", version=f"gcalsync {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="POLECENIE")

    # preview
    preview = sub.add_parser(
        "preview",
        help="podgląd planu po filtrowaniu, deduplikacji i konfliktach (nic nie zapisuje)",
        description=(
            "Bez plików: podgląd zapisanej konfiguracji (źródła, priorytety, reguły). "
            "Z plikami: podgląd jednorazowy — kolejność plików to priorytet (pierwszy "
            "najważniejszy), reguły tylko z opcji --exclude-*."
        ),
    )
    preview.add_argument("files", nargs="*", type=Path, metavar="PLIK", help="pliki CSV z ewig")
    preview.add_argument(
        "--names",
        nargs="+",
        metavar="NAZWA",
        help="nazwy źródeł w kolejności plików (domyślnie nazwy plików)",
    )
    _add_rule_filters(preview)
    preview.add_argument(
        "--encoding",
        help="wymuś kodowanie wszystkich plików (np. cp1250, iso-8859-2, utf-8)",
    )
    preview.add_argument(
        "--policy",
        choices=[p.value for p in ConflictPolicy],
        help="priority: ważniejsze źródło wygrywa (domyślnie); keep-all: tylko ostrzegaj",
    )
    preview.add_argument(
        "--json", type=Path, metavar="PLIK", help="zapisz pełny podgląd jako JSON (UTF-8)"
    )

    # paths
    sub.add_parser("paths", help="pokaż katalog danych aplikacji (client_secret.json, token…)")

    # sources
    sources = sub.add_parser("sources", help="zapisane źródła (pliki CSV) i ich priorytet")
    sources_sub = sources.add_subparsers(dest="action", metavar="AKCJA")
    sources_sub.add_parser("list", help="pokaż źródła (domyślnie)")
    s_add = sources_sub.add_parser("add", help="dodaj plik jako nowe źródło (na koniec listy)")
    s_add.add_argument("file", type=Path, metavar="PLIK")
    s_add.add_argument("--name", required=True, metavar="NAZWA")
    s_add.add_argument("--encoding", help="wymuś kodowanie (domyślnie wykrywane)")
    s_rep = sources_sub.add_parser("replace", help="podmień plik istniejącego źródła")
    s_rep.add_argument("name", metavar="NAZWA")
    s_rep.add_argument("file", type=Path, metavar="PLIK")
    s_rep.add_argument("--encoding", help="wymuś kodowanie (domyślnie wykrywane)")
    s_rm = sources_sub.add_parser("remove", help="usuń źródło (i jego kopię pliku)")
    s_rm.add_argument("name", metavar="NAZWA")
    s_mv = sources_sub.add_parser("move", help="zmień priorytet źródła")
    s_mv.add_argument("name", metavar="NAZWA")
    s_mv.add_argument("position", type=int, metavar="POZYCJA", help="1 = najwyższy priorytet")

    # rules
    rules = sub.add_parser("rules", help="zapisane reguły wykluczeń")
    rules_sub = rules.add_subparsers(dest="action", metavar="AKCJA")
    rules_sub.add_parser("list", help="pokaż reguły (domyślnie)")
    r_add = rules_sub.add_parser(
        "add",
        help="dodaj regułę",
        description=(
            "Skróty: --course (przedmiot równa się), --contains (temat zawiera), --regex "
            "(temat pasuje do wzorca). Albo pełna postać: --field POLE --op OPERATOR --value X."
        ),
    )
    shortcut = r_add.add_mutually_exclusive_group()
    shortcut.add_argument("--course", metavar="PRZEDMIOT")
    shortcut.add_argument("--contains", metavar="TEKST")
    shortcut.add_argument("--regex", metavar="WZORZEC")
    r_add.add_argument("--field", choices=list(FIELD_LABELS))
    r_add.add_argument("--op", choices=list(OPERATOR_LABELS))
    r_add.add_argument("--value")
    r_add.add_argument("--case-sensitive", action="store_true", help="rozróżniaj wielkość liter")
    r_add.add_argument(
        "--source", action="append", metavar="NAZWA", help="tylko dla tego źródła; można powtarzać"
    )
    r_add.add_argument("--from", dest="date_from", type=date.fromisoformat, metavar="RRRR-MM-DD")
    r_add.add_argument("--to", dest="date_to", type=date.fromisoformat, metavar="RRRR-MM-DD")
    r_add.add_argument("--disabled", action="store_true", help="dodaj jako wyłączoną")
    for action, help_text in (
        ("remove", "usuń regułę"),
        ("enable", "włącz regułę"),
        ("disable", "wyłącz regułę"),
    ):
        p = rules_sub.add_parser(action, help=help_text)
        p.add_argument("number", type=int, metavar="NR", help="numer z: gcalsync rules list")

    # settings
    settings = sub.add_parser("settings", help="pokaż lub zmień ustawienia")
    settings.add_argument(
        "--title-template",
        metavar="SZABLON",
        help="szablon tytułu, pola: {course} {kind} {seq} {subject} {location}",
    )
    settings.add_argument("--policy", choices=[p.value for p in ConflictPolicy])
    return parser


# --- pomocnicze ---------------------------------------------------------------------------


def _unique_names(names: list[str]) -> list[str]:
    unique, seen = [], Counter()
    for name in names:
        seen[name] += 1
        unique.append(name if seen[name] == 1 else f"{name} #{seen[name]}")
    return unique


def _flag_rules(args: argparse.Namespace) -> list[ExclusionRule]:
    try:
        return (
            [ExclusionRule("course", "equals", v) for v in args.exclude_course]
            + [ExclusionRule("subject", "contains", v) for v in args.exclude_contains]
            + [ExclusionRule("subject", "regex", v) for v in args.exclude_regex]
        )
    except RuleError as exc:
        raise CliError(str(exc)) from exc


def _require_file(path: Path) -> None:
    if not path.is_file():
        raise CliError(f"Nie ma pliku: {path}")


def _print_preview(result: PreviewResult, json_path: Path | None) -> int:
    sys.stdout.write(render_text(result))
    if json_path:
        json_path.write_text(
            json.dumps(preview_to_dict(result), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nZapisano JSON: {json_path}")
    return EXIT_FILE_ERRORS if result.has_errors else EXIT_OK


def _local_time(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")


# --- polecenia ----------------------------------------------------------------------------


def cmd_preview(args: argparse.Namespace, paths: Paths) -> int:
    if not args.files:
        if args.names or args.encoding:
            raise CliError("--names i --encoding działają tylko z podanymi plikami.")
        config = load_config(paths)
        if args.policy:
            config.policy = ConflictPolicy(args.policy)
        config.rules += _flag_rules(args)
        return _print_preview(preview_from_config(paths, config), args.json)

    if args.names is not None and len(args.names) != len(args.files):
        raise CliError(
            f"Liczba nazw ({len(args.names)}) musi być równa liczbie plików ({len(args.files)})."
        )
    names = _unique_names(args.names or [f.name for f in args.files])
    sources = []
    for path, name in zip(args.files, names, strict=True):
        _require_file(path)
        sources.append(CsvFileSource.from_path(path, id=name, name=name, encoding=args.encoding))
    policy = ConflictPolicy(args.policy or ConflictPolicy.PRIORITY.value)
    return _print_preview(build_preview(sources, _flag_rules(args), policy), args.json)


def cmd_paths(args: argparse.Namespace, paths: Paths) -> int:
    paths.ensure()

    def state(path: Path, present: str, missing: str) -> str:
        return present if path.exists() else missing

    print(f"Katalog danych aplikacji: {paths.root}")
    print(f"  config.json         {paths.config}  ({state(paths.config, 'jest', 'jeszcze brak')})")
    print(
        f"  client_secret.json  {paths.client_secret}  "
        f"({state(paths.client_secret, 'jest', 'BRAK — skopiuj tu plik z Google Cloud Console')})"
    )
    print(
        f"  token.json          {paths.token}  "
        f"({state(paths.token, 'zalogowano', 'niezalogowano')})"
    )
    print(f"  kopie plików CSV    {paths.files}")
    print(f"  dziennik synchron.  {paths.runs}")
    return EXIT_OK


def _print_sources(config: Config) -> None:
    if not config.sources:
        print("Brak zapisanych źródeł. Dodaj: gcalsync sources add PLIK --name NAZWA")
        return
    print("Źródła (od najwyższego priorytetu):")
    for i, s in enumerate(config.sources, 1):
        encoding = s.encoding or "wykrywane automatycznie"
        print(
            f"  {i}. {s.name} — plik: {s.original_filename}, "
            f"wgrany: {_local_time(s.added)}, kodowanie: {encoding}"
        )


def cmd_sources(args: argparse.Namespace, paths: Paths) -> int:
    config = load_config(paths)
    action = args.action or "list"
    if action == "list":
        _print_sources(config)
        return EXIT_OK

    if action == "add":
        _require_file(args.file)
        source = add_source(paths, config, args.file, args.name, args.encoding)
        print(f"Dodano źródło „{source.name}” (priorytet {len(config.sources)}).")
    elif action == "replace":
        _require_file(args.file)
        source = replace_source_file(paths, config, args.name, args.file, args.encoding)
        print(f"Podmieniono plik źródła „{source.name}”.")
    elif action == "remove":
        source, dropped = remove_source(paths, config, args.name)
        print(f"Usunięto źródło „{source.name}”.")
        for rule in dropped:
            print(f"Usunięto regułę dotyczącą tylko tego źródła: {rule.describe()}")
    elif action == "move":
        move_source(config, args.name, args.position)
        print(f"Przeniesiono „{args.name}” na pozycję {args.position}.")
    save_config(paths, config)
    _print_sources(config)
    return EXIT_OK


def _print_rules(config: Config) -> None:
    if not config.rules:
        print('Brak reguł wykluczeń. Dodaj np.: gcalsync rules add --course "NAZWA PRZEDMIOTU"')
        return
    names = {s.id: s.name for s in config.sources}
    print("Reguły wykluczeń:")
    for i, rule in enumerate(config.rules, 1):
        print(f"  {i}. {rule.describe(names)}")


def _rule_from_args(args: argparse.Namespace, config: Config) -> ExclusionRule:
    if args.course is not None:
        field, op, value = "course", "equals", args.course
    elif args.contains is not None:
        field, op, value = "subject", "contains", args.contains
    elif args.regex is not None:
        field, op, value = "subject", "regex", args.regex
    else:
        if not (args.field and args.op and args.value is not None):
            raise CliError(
                "Podaj --course, --contains lub --regex, albo komplet --field --op --value."
            )
        field, op, value = args.field, args.op, args.value
    if (args.course or args.contains or args.regex) and (args.field or args.op or args.value):
        raise CliError("Nie łącz skrótów (--course/--contains/--regex) z --field/--op/--value.")
    source_ids = tuple(config.source_by_name(n).id for n in args.source) if args.source else None
    try:
        return ExclusionRule(
            field=field,
            op=op,
            value=value,
            case_sensitive=args.case_sensitive,
            enabled=not args.disabled,
            sources=source_ids,
            date_from=args.date_from,
            date_to=args.date_to,
        )
    except RuleError as exc:
        raise CliError(str(exc)) from exc


def cmd_rules(args: argparse.Namespace, paths: Paths) -> int:
    config = load_config(paths)
    action = args.action or "list"
    if action == "list":
        _print_rules(config)
        return EXIT_OK

    if action == "add":
        config.rules.append(_rule_from_args(args, config))
        print(f"Dodano regułę nr {len(config.rules)}.")
    elif action == "remove":
        rule = rule_at(config, args.number)
        config.rules.remove(rule)
        print(f"Usunięto regułę: {rule.describe({x.id: x.name for x in config.sources})}")
    else:
        set_rule_enabled(config, args.number, action == "enable")
        print(f"Reguła nr {args.number} {'włączona' if action == 'enable' else 'wyłączona'}.")
    save_config(paths, config)
    _print_rules(config)
    return EXIT_OK


def cmd_settings(args: argparse.Namespace, paths: Paths) -> int:
    config = load_config(paths)
    changed = False
    if args.title_template is not None:
        validate_title_template(args.title_template)
        config.title_template = args.title_template
        changed = True
    if args.policy is not None:
        config.policy = ConflictPolicy(args.policy)
        changed = True
    if changed:
        save_config(paths, config)
        print("Zapisano ustawienia.")
    print(f"Szablon tytułu:     {config.title_template}")
    print(f"Polityka konfliktów: {config.policy.value}")
    if config.calendar:
        print(f"Kalendarz:           {config.calendar.summary} ({config.calendar.id})")
    else:
        print("Kalendarz:           nie ustawiony")
    return EXIT_OK


COMMANDS = {
    "preview": cmd_preview,
    "paths": cmd_paths,
    "sources": cmd_sources,
    "rules": cmd_rules,
    "settings": cmd_settings,
}


def main(argv: list[str] | None = None, paths: Paths | None = None) -> int:
    # Konsola Windows lub przekierowanie do pliku mogą nie obsługiwać wszystkich znaków.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)
    handler = COMMANDS.get(args.command)
    if handler is None:
        parser.print_help()
        return EXIT_OK
    try:
        return handler(args, paths or Paths.default())
    except (CliError, ConfigError) as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
