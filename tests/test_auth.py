import json
from datetime import UTC, datetime, timedelta

import pytest

from gcalsync.gcal.auth import SCOPES, AuthError, load_credentials, login
from gcalsync.storage import Paths


@pytest.fixture
def paths(tmp_path):
    p = Paths(tmp_path / "home")
    p.ensure()
    return p


def write_token(paths, scopes, expiry):
    paths.token.write_text(
        json.dumps(
            {
                "token": "access",
                "refresh_token": "refresh",
                "client_id": "id.apps.googleusercontent.com",
                "client_secret": "secret",
                "token_uri": "https://oauth2.googleapis.com/token",
                "scopes": scopes,
                "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            }
        )
    )


def test_scope_is_the_narrow_app_created_one():
    assert SCOPES == ["https://www.googleapis.com/auth/calendar.app.created"]


def test_missing_client_secret(paths):
    with pytest.raises(AuthError, match="Brak pliku"):
        login(paths, open_browser=False)


def test_web_client_is_rejected(paths):
    paths.client_secret.write_text(json.dumps({"web": {"client_id": "x"}}))
    with pytest.raises(AuthError, match="Desktop app"):
        login(paths, open_browser=False)


def test_corrupted_client_secret(paths):
    paths.client_secret.write_text("{zły json")
    with pytest.raises(AuthError, match="Nie można odczytać"):
        login(paths, open_browser=False)


def test_not_logged_in(paths):
    with pytest.raises(AuthError, match="Nie zalogowano"):
        load_credentials(paths)


def test_corrupted_token(paths):
    paths.token.write_text("[]")
    with pytest.raises(AuthError, match="Uszkodzony"):
        load_credentials(paths)


def test_token_with_other_scope_is_rejected(paths):
    future = datetime.now(UTC) + timedelta(hours=1)
    write_token(paths, ["https://www.googleapis.com/auth/calendar.readonly"], future)
    with pytest.raises(AuthError, match="uprawnienia"):
        load_credentials(paths)


def test_valid_token_is_used_without_network(paths):
    future = datetime.now(UTC) + timedelta(hours=1)
    write_token(paths, SCOPES, future)
    credentials = load_credentials(paths)
    assert credentials.valid
    assert credentials.token == "access"
