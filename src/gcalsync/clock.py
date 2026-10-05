"""Bieżący czas w jednym miejscu — testy podmieniają `now`, żeby nie zależeć od daty."""

from __future__ import annotations

from datetime import UTC, datetime


def now() -> datetime:
    return datetime.now(UTC)
