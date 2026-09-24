"""Cienka warstwa nad Google Calendar API v3.

Odczyty korzystają z wbudowanego w google-api-python-client ponawiania (`num_retries`):
5xx, 429, 403 z powodem rateLimitExceeded/userRateLimitExceeded oraz błędy połączenia,
z wykładniczym opóźnieniem. Tak samo zapisy (patrz niżej).
"""

from __future__ import annotations

from typing import Any, Protocol

import httplib2
from google.auth.credentials import Credentials
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

NUM_RETRIES = 5
PAGE_SIZE = 2500  # maksimum dla events.list


class GoogleApiError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class CalendarApi(Protocol):
    def get_calendar(self, calendar_id: str) -> dict[str, Any] | None: ...

    def create_calendar(self, summary: str, time_zone: str, description: str) -> dict[str, Any]: ...

    def list_events(self, calendar_id: str) -> list[dict[str, Any]]: ...

    def insert_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]: ...

    def patch_event(
        self, calendar_id: str, event_id: str, body: dict[str, Any]
    ) -> dict[str, Any]: ...

    def update_event(
        self, calendar_id: str, event_id: str, body: dict[str, Any]
    ) -> dict[str, Any]: ...
    def delete_event(self, calendar_id: str, event_id: str) -> None: ...


# Błędy sieci/odświeżania tokenu, które zostają po wyczerpaniu ponowień biblioteki.
NETWORK_ERRORS = (OSError, httplib2.HttpLib2Error, TransportError, RefreshError)


def _network(exc: Exception, action: str) -> GoogleApiError:
    if isinstance(exc, RefreshError):
        return GoogleApiError(
            f"{action}: sesja Google wygasła ({exc}) — uruchom: gcalsync login", 401
        )
    return GoogleApiError(f"{action}: brak połączenia z Google ({exc})")


def _explain(exc: HttpError, action: str) -> GoogleApiError:
    status = exc.resp.status if exc.resp is not None else None
    detail = getattr(exc, "reason", "") or str(exc)
    hints = {
        401: "sesja wygasła — uruchom: gcalsync login",
        403: "brak uprawnień lub przekroczony limit zapytań",
        404: "nie znaleziono (albo kalendarz nie został utworzony przez gcalsync)",
    }
    hint = hints.get(status, "")
    return GoogleApiError(
        f"Google Calendar API: {action} nie powiodło się (HTTP {status}: {detail})"
        + (f" — {hint}" if hint else ""),
        status,
    )


class GoogleCalendarApi:
    def __init__(
        self,
        credentials: Credentials | None,
        num_retries: int = NUM_RETRIES,
        http: Any = None,  # tylko dla testów (googleapiclient.http.HttpMockSequence)
    ):
        # Dokument API jest dołączony do biblioteki (static_discovery) — bez pobierania z sieci.
        self._service = build(
            "calendar", "v3", credentials=credentials, http=http, cache_discovery=False
        )
        self._retries = num_retries

    def get_calendar(self, calendar_id: str) -> dict[str, Any] | None:
        try:
            return (
                self._service.calendars()
                .get(calendarId=calendar_id)
                .execute(num_retries=self._retries)
            )
        except HttpError as exc:
            if exc.resp is not None and exc.resp.status == 404:
                return None
            raise _explain(exc, "odczyt kalendarza") from exc
        except NETWORK_ERRORS as exc:
            raise _network(exc, "odczyt kalendarza") from exc

    def create_calendar(self, summary: str, time_zone: str, description: str) -> dict[str, Any]:
        body = {"summary": summary, "timeZone": time_zone, "description": description}
        try:
            # Bez ponawiania: powtórzony insert mógłby utworzyć drugi kalendarz.
            return self._service.calendars().insert(body=body).execute()
        except HttpError as exc:
            raise _explain(exc, "utworzenie kalendarza") from exc
        except NETWORK_ERRORS as exc:
            raise _network(exc, "utworzenie kalendarza") from exc

    def list_events(self, calendar_id: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        page_token = None
        while True:
            try:
                response = (
                    self._service.events()
                    .list(
                        calendarId=calendar_id,
                        singleEvents=True,
                        showDeleted=False,
                        maxResults=PAGE_SIZE,
                        pageToken=page_token,
                    )
                    .execute(num_retries=self._retries)
                )
            except HttpError as exc:
                raise _explain(exc, "odczyt zdarzeń") from exc
            except NETWORK_ERRORS as exc:
                raise _network(exc, "odczyt zdarzeń") from exc
            events += response.get("items", [])
            page_token = response.get("nextPageToken")
            if not page_token:
                return events

    # --- zapisy -------------------------------------------------------------------------
    # Ponawianie przez num_retries także dla zapisów. Ponowiony insert, którego pierwsza
    # próba jednak dotarła do Google, może dać duplikat — następna synchronizacja wykrywa go
    # po kluczu w znaczniku i usuwa.

    def _execute(self, request, action: str) -> Any:
        try:
            return request.execute(num_retries=self._retries)
        except HttpError as exc:
            raise _explain(exc, action) from exc
        except NETWORK_ERRORS as exc:
            raise _network(exc, action) from exc

    def insert_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        request = self._service.events().insert(calendarId=calendar_id, body=body)
        return self._execute(request, "dodanie zdarzenia")

    def patch_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        request = self._service.events().patch(calendarId=calendar_id, eventId=event_id, body=body)
        return self._execute(request, "zmiana zdarzenia")

    def update_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Pełna podmiana zdarzenia (PUT) — w odróżnieniu od patch usuwa pominięte pola."""
        request = self._service.events().update(calendarId=calendar_id, eventId=event_id, body=body)
        return self._execute(request, "zapis zdarzenia")

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        request = self._service.events().delete(calendarId=calendar_id, eventId=event_id)
        try:
            self._execute(request, "usunięcie zdarzenia")
        except GoogleApiError as exc:
            # 404/410: zdarzenia już nie ma (np. ponowienie po udanym usunięciu) — cel osiągnięty.
            if exc.status not in (404, 410):
                raise
