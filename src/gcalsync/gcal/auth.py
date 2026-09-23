"""Logowanie OAuth (aplikacja typu Desktop) i przechowywanie tokenu poza repozytorium."""

from __future__ import annotations

import json

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from gcalsync.storage import Paths, write_private

# Jedyny zakres: tworzenie kalendarzy dodatkowych i zarządzanie zdarzeniami wyłącznie w nich.
# Aplikacja nie widzi Twojego głównego kalendarza ani innych kalendarzy.
SCOPES = ["https://www.googleapis.com/auth/calendar.app.created"]

LOGIN_TIMEOUT_SECONDS = 300


class AuthError(Exception):
    pass


def _check_client_secret(paths: Paths) -> None:
    path = paths.client_secret
    if not path.exists():
        raise AuthError(
            f"Brak pliku {path}. Pobierz JSON klienta OAuth (typ „Desktop app”) z Google Cloud "
            "Console i zapisz go pod tą nazwą. Katalog pokazuje: gcalsync paths"
        )
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise AuthError(f"Nie można odczytać {path}: {exc}") from exc
    if "installed" not in data:
        kind = "„Web application”" if "web" in data else "nieznanego typu"
        raise AuthError(
            f"{path.name} to klient {kind}. Utwórz klienta OAuth typu „Desktop app” "
            "i pobierz jego plik JSON."
        )


def login(paths: Paths, open_browser: bool = True) -> Credentials:
    """Otwiera przeglądarkę z ekranem zgody Google i zapisuje token w katalogu aplikacji."""
    _check_client_secret(paths)
    flow = InstalledAppFlow.from_client_secrets_file(str(paths.client_secret), SCOPES)
    try:
        credentials = flow.run_local_server(
            host="localhost",
            port=0,
            open_browser=open_browser,
            authorization_prompt_message=(
                "Otwieram przeglądarkę z ekranem zgody Google. Jeśli się nie otworzyła, "
                "wejdź na adres:\n{url}\n"
            ),
            success_message="Zalogowano do gcalsync. Możesz zamknąć tę kartę.",
            timeout_seconds=LOGIN_TIMEOUT_SECONDS,
        )
    except Warning as exc:  # oauthlib: przyznany zakres inny niż żądany
        raise AuthError(
            "Nie przyznano wymaganego uprawnienia do kalendarzy tworzonych przez aplikację. "
            "Zaloguj się ponownie i zaznacz je na ekranie zgody."
        ) from exc
    except Exception as exc:
        raise AuthError(f"Logowanie nie powiodło się: {exc}") from exc
    if not credentials.refresh_token:
        raise AuthError("Google nie zwrócił refresh tokenu. Spróbuj zalogować się ponownie.")
    paths.ensure()
    write_private(paths.token, credentials.to_json().encode("utf-8"))
    return credentials


def _ready(credentials: Credentials) -> Credentials:
    """Sprawdza zakres i w razie potrzeby odświeża token (bez przeglądarki)."""
    if not credentials.has_scopes(SCOPES):
        raise AuthError("Token nie ma wymaganego uprawnienia. Zaloguj się ponownie: gcalsync login")
    if credentials.valid:
        return credentials
    if not credentials.refresh_token:
        raise AuthError("Token wygasł i nie da się go odświeżyć. Uruchom: gcalsync login")
    try:
        credentials.refresh(Request())
    except RefreshError as exc:
        raise AuthError(
            "Sesja Google wygasła albo dostęp został odwołany "
            f"({exc}). W trybie Testing token jest ważny 7 dni. Uruchom: gcalsync login"
        ) from exc
    except TransportError as exc:
        raise AuthError(f"Brak połączenia z Google podczas odświeżania tokenu: {exc}") from exc
    return credentials


def load_credentials(paths: Paths) -> Credentials:
    """Wczytuje zapisany token i w razie potrzeby odświeża go (bez przeglądarki)."""
    if not paths.token.exists():
        raise AuthError("Nie zalogowano. Uruchom: gcalsync login")
    try:
        # Bez argumentu scopes: biblioteka nadpisałaby nim zakresy zapisane w tokenie,
        # a chcemy sprawdzić, co Google faktycznie przyznał.
        credentials = Credentials.from_authorized_user_file(str(paths.token))
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
        raise AuthError(
            f"Uszkodzony plik tokenu {paths.token} ({exc}). Zaloguj się ponownie: gcalsync login"
        ) from exc
    was_valid = credentials.valid
    credentials = _ready(credentials)
    if not was_valid:
        write_private(paths.token, credentials.to_json().encode("utf-8"))
    return credentials


def credentials_from_json(token_json: str) -> Credentials:
    """Token z sekretu (np. GOOGLE_TOKEN_JSON w GitHub Actions) — zawartość pliku token.json."""
    if not token_json.strip():
        raise AuthError("Brak tokenu Google (GOOGLE_TOKEN_JSON).")
    try:
        credentials = Credentials.from_authorized_user_info(json.loads(token_json))
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        raise AuthError(
            f"Niepoprawny token Google w GOOGLE_TOKEN_JSON ({type(exc).__name__})."
        ) from exc
    return _ready(credentials)


def logout(paths: Paths) -> bool:
    """Usuwa lokalny token. Zwraca True, jeśli był zapisany."""
    if paths.token.exists():
        paths.token.unlink()
        return True
    return False
