"""Interfejs wiersza poleceń gcalsync."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from gcalsync import __version__
from gcalsync.core.merge import ConflictPolicy
from gcalsync.core.pipeline import build_preview
from gcalsync.core.rules import ExclusionRule, RuleError
from gcalsync.report import preview_to_dict, render_text
from gcalsync.sources.outlook_csv import CsvFileSource

EXIT_OK = 0
EXIT_FILE_ERRORS = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gcalsync",
        description="Synchronizacja planu zajęć WAT (CSV z ewig) z Google Calendar.",
    )
    parser.add_argument("--version", action="version", version=f"gcalsync {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="POLECENIE")

    preview = sub.add_parser(
        "preview",
        help="podgląd planu po filtrowaniu, deduplikacji i konfliktach (nic nie zapisuje)",
        description=(
            "Przetwarza pliki CSV i pokazuje, co trafiłoby do kalendarza. Kolejność plików "
            "to priorytet: pierwszy plik jest najważniejszy i wygrywa w konfliktach."
        ),
    )
    preview.add_argument("files", nargs="+", type=Path, metavar="PLIK", help="pliki CSV z ewig")
    preview.add_argument(
        "--names",
        nargs="+",
        metavar="NAZWA",
        help="nazwy źródeł w kolejności plików (domyślnie nazwy plików)",
    )
    preview.add_argument(
        "--exclude-course",
        action="append",
        default=[],
        metavar="PRZEDMIOT",
        help="wyklucz przedmiot o dokładnie tej nazwie (bez typu i numeru); można powtarzać",
    )
    preview.add_argument(
        "--exclude-contains",
        action="append",
        default=[],
        metavar="TEKST",
        help="wyklucz zdarzenia, których temat zawiera tekst; można powtarzać",
    )
    preview.add_argument(
        "--exclude-regex",
        action="append",
        default=[],
        metavar="WZORZEC",
        help="wyklucz zdarzenia, których temat pasuje do wyrażenia regularnego",
    )
    preview.add_argument(
        "--encoding",
        help="wymuś kodowanie wszystkich plików (np. cp1250, iso-8859-2, utf-8)",
    )
    preview.add_argument(
        "--policy",
        choices=[p.value for p in ConflictPolicy],
        default=ConflictPolicy.PRIORITY.value,
        help="priority: ważniejsze źródło wygrywa (domyślnie); keep-all: tylko ostrzegaj",
    )
    preview.add_argument(
        "--json", type=Path, metavar="PLIK", help="zapisz pełny podgląd jako JSON (UTF-8)"
    )
    return parser


def _source_names(files: list[Path], names: list[str] | None) -> list[str]:
    if names is None:
        names = [f.name for f in files]
    unique, seen = [], Counter()
    for name in names:
        seen[name] += 1
        unique.append(name if seen[name] == 1 else f"{name} #{seen[name]}")
    return unique


def _rules(args: argparse.Namespace) -> list[ExclusionRule]:
    return (
        [ExclusionRule("course", "equals", v) for v in args.exclude_course]
        + [ExclusionRule("subject", "contains", v) for v in args.exclude_contains]
        + [ExclusionRule("subject", "regex", v) for v in args.exclude_regex]
    )


def cmd_preview(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.names is not None and len(args.names) != len(args.files):
        parser.error(
            f"liczba nazw ({len(args.names)}) musi być równa liczbie plików ({len(args.files)})"
        )
    try:
        rules = _rules(args)
    except RuleError as exc:
        parser.error(str(exc))

    sources = []
    for path, name in zip(args.files, _source_names(args.files, args.names), strict=True):
        try:
            sources.append(
                CsvFileSource.from_path(path, id=name, name=name, encoding=args.encoding)
            )
        except OSError as exc:
            parser.error(f"nie można odczytać pliku {path}: {exc.strerror or exc}")

    result = build_preview(sources, rules, ConflictPolicy(args.policy))
    sys.stdout.write(render_text(result))
    if args.json:
        args.json.write_text(
            json.dumps(preview_to_dict(result), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nZapisano JSON: {args.json}")
    return EXIT_FILE_ERRORS if result.has_errors else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    # Konsola Windows lub przekierowanie do pliku mogą nie obsługiwać wszystkich znaków.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "preview":
        return cmd_preview(args, parser)
    parser.print_help()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
