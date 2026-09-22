"""Test prawdziwej klasy GoogleCalendarApi na atrapie HTTP (bez sieci), z dokumentem API
dołączonym do google-api-python-client."""

import json
from urllib.parse import parse_qs, urlparse

import pytest
from googleapiclient.http import HttpMockSequence

from gcalsync.gcal.client import GoogleApiError, GoogleCalendarApi


def api(*responses):
    seq = [({"status": str(status)}, body) for status, body in responses]
    return GoogleCalendarApi(None, num_retries=0, http=HttpMockSequence(seq))


def test_list_events_follows_pages():
    client = api(
        (200, json.dumps({"items": [{"id": "a"}], "nextPageToken": "p2"})),
        (200, json.dumps({"items": [{"id": "b"}]})),
    )
    assert [e["id"] for e in client.list_events("cal")] == ["a", "b"]


def test_list_events_request_parameters():
    # Sprawdzamy zapytanie zbudowane przez bibliotekę z tymi samymi parametrami co list_events.
    client = api()
    request = client._service.events().list(
        calendarId="cal@group.calendar.google.com",
        singleEvents=True,
        showDeleted=False,
        maxResults=2500,
    )
    query = parse_qs(urlparse(request.uri).query)
    assert query["singleEvents"] == ["true"]
    assert query["showDeleted"] == ["false"]
    assert query["maxResults"] == ["2500"]


def test_get_calendar_404_is_none():
    client = api((404, json.dumps({"error": {"code": 404, "message": "Not Found"}})))
    assert client.get_calendar("nie-ma") is None


def test_get_calendar_ok():
    client = api((200, json.dumps({"id": "cal", "summary": "Plan WAT"})))
    assert client.get_calendar("cal")["summary"] == "Plan WAT"


def test_http_errors_are_explained():
    client = api((403, json.dumps({"error": {"code": 403, "message": "Forbidden"}})))
    with pytest.raises(GoogleApiError) as exc:
        client.list_events("cal")
    assert exc.value.status == 403
    assert "HTTP 403" in str(exc.value)


def test_create_calendar_sends_time_zone():
    client = api((200, "echo_request_body"))
    body = client.create_calendar("Plan WAT", "Europe/Warsaw", "opis")
    assert body == {"summary": "Plan WAT", "timeZone": "Europe/Warsaw", "description": "opis"}
