"""Atrapa Google Calendar API w pamięci — zachowuje się jak odpowiedzi prawdziwego API
w zakresie używanym przez gcalsync (format czasu z offsetem, znaczniki, 404)."""

from __future__ import annotations

import copy
import itertools
from collections.abc import Callable
from typing import Any

from gcalsync.gcal.client import GoogleApiError
from gcalsync.gcal.mapping import TIME_ZONE, parse_time


def to_google_time(value: dict[str, Any]) -> dict[str, Any]:
    """Google zwraca dateTime z offsetem (RFC 3339) i powtarza timeZone."""
    if "date" in value:
        return dict(value)
    moment = parse_time(value)
    return {"dateTime": moment.isoformat(), "timeZone": value.get("timeZone", TIME_ZONE)}


class FakeCalendarApi:
    def __init__(self) -> None:
        self.calendars: dict[str, dict[str, Any]] = {}
        self.events: dict[str, dict[str, dict[str, Any]]] = {}
        self.calls: list[str] = []
        self._ids = itertools.count(1)
        self.writes = 0
        # Wstrzykiwanie awarii: fail(kind, nr_zapisu) -> (wyjątek, czy_zapis_mimo_to_się_udał)
        self.fail: Callable[[str, int], tuple[BaseException, bool] | None] | None = None

    def _maybe_fail(self, kind: str) -> bool:
        """Zwraca True, jeśli po wykonaniu operacji trzeba rzucić wyjątek (odpowiedź „zgubiona”)."""
        self.writes += 1
        if self.fail is None:
            return False
        outcome = self.fail(kind, self.writes)
        if outcome is None:
            return False
        exc, committed = outcome
        if not committed:
            raise exc
        self._pending = exc
        return True

    # --- API używane przez aplikację ---

    def get_calendar(self, calendar_id: str) -> dict[str, Any] | None:
        self.calls.append(f"get_calendar {calendar_id}")
        cal = self.calendars.get(calendar_id)
        return copy.deepcopy(cal) if cal else None

    def create_calendar(self, summary: str, time_zone: str, description: str) -> dict[str, Any]:
        self.calls.append(f"create_calendar {summary}")
        cal_id = f"fake{next(self._ids)}@group.calendar.google.com"
        self.calendars[cal_id] = {
            "id": cal_id,
            "summary": summary,
            "timeZone": time_zone,
            "description": description,
        }
        self.events[cal_id] = {}
        return copy.deepcopy(self.calendars[cal_id])

    def list_events(self, calendar_id: str) -> list[dict[str, Any]]:
        self.calls.append(f"list_events {calendar_id}")
        if calendar_id not in self.events:
            raise GoogleApiError("not found", 404)
        return [copy.deepcopy(e) for e in self.events[calendar_id].values()]

    def insert_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(f"insert {body.get('summary')}")
        lost = self._maybe_fail("insert")
        created = self.put_event(calendar_id, body)
        if lost:
            raise self._pending
        return created

    def patch_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(f"patch {event_id}")
        lost = self._maybe_fail("patch")
        event = self.events[calendar_id].get(event_id)
        if event is None:
            raise GoogleApiError("not found", 404)
        for key, value in body.items():
            event[key] = to_google_time(value) if key in ("start", "end") else copy.deepcopy(value)
        if event.get("location") == "":
            del event["location"]
        if lost:
            raise self._pending
        return copy.deepcopy(event)

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        self.calls.append(f"delete {event_id}")
        lost = self._maybe_fail("delete")
        # Prawdziwy klient zamienia 404/410 na sukces — atrapa robi to samo.
        self.events[calendar_id].pop(event_id, None)
        if lost:
            raise self._pending

    # --- pomocnicze dla testów ---

    def put_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Wstawia zdarzenie tak, jak zapisałby je Google (dla przygotowania stanu testu)."""
        event_id = f"evt{next(self._ids)}"
        stored = copy.deepcopy(body)
        stored.update(
            {
                "id": event_id,
                "status": "confirmed",
                "start": to_google_time(body["start"]),
                "end": to_google_time(body["end"]),
            }
        )
        self.events[calendar_id][event_id] = stored
        return copy.deepcopy(stored)
