"""Interfejs wiersza poleceń gcalsync."""

from __future__ import annotations

import argparse

from gcalsync import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gcalsync",
        description="Synchronizacja planu zajęć WAT (CSV z ewig) z Google Calendar.",
    )
    parser.add_argument("--version", action="version", version=f"gcalsync {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
