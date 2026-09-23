"""Pobieranie planu grup z e-Dziekanatu WIG (ewig.wcy.wat.edu.pl/ed2).

Klient robi to samo co przeglądarka (ustalone z zapisanych stron i ich JavaScriptu):
1. GET strony logowania — identyfikator sesji `sid` jest w adresie formularza,
2. POST formularza logowania (`formname=login`, `default_fun=1` = „Aktualności”),
3. GET pozycji menu „Rozkład zajęć grupy”, GET planu grupy (`mid=328`, `iid=<semestr>`,
   `exv=<kod grupy>`), potem GET eksportu „w formacie Outlooka” (`opr=DTXT`, jak ikona eksportu),
4. wylogowanie (`index.php?sid=…&lou=1`) — kroki 1–4 osobno dla każdej grupy.

Hasło nie jest nigdzie logowane ani umieszczane w komunikatach błędów.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlencode, urlparse

import requests

from gcalsync.sources.outlook_csv import CsvFileSource, parse_outlook_csv

BASE_URL = "https://ewig.wcy.wat.edu.pl/ed2/"
PAGE_ENCODING = "iso-8859-2"
MID_GROUP_PLAN = 328
TIMEOUT_SECONDS = 30
# Tylko ASCII (nagłówki HTTP). Jak przeglądarka, z dopiskiem identyfikującym narzędzie.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0 Safari/537.36 gcalsync/0.1"
)

SID_IN_PAGE = re.compile(r"var sid = new String\('([0-9a-fA-F]+)'\)")


class EwigError(Exception):
    pass


class EwigLoginError(EwigError):
    pass


@dataclass(frozen=True)
class EwigGroup:
    code: str  # np. WIG23IX2S1
    name: str  # nazwa źródła w raportach, np. „Grupa kierunkowa”


# --- adresy (odtworzone z menubody.js / form.js / shedule_teacher_group.js) ----------------


# checkurl() najpierw woła prolongTimeOut(), która zmienia zmienną strony `used` (start: 0):
# gdy bit 0x01 jest pusty -> used |= 0x45, inaczej used |= 0x04. Z 0 zawsze wychodzi 0x45 (69),
# a kolejne wywołania już tego nie zmieniają. Dopiero potem suma jest powiększana o `used`.
USED_AFTER_PROLONG = 0x45


def checksum(*values: str, used: int = USED_AFTER_PROLONG) -> int:
    """Suma `vrf` z checkurl(): dla każdej cyfry na pozycji j dodaje j+cyfra, na końcu used."""
    total = 0
    for value in values:
        for j, ch in enumerate(value):
            if ch.isdigit():
                total += j + int(ch)
    return total + used


def _base_query(sid: str, mid: int, iid: int) -> list[tuple[str, str]]:
    # getURL(): sid, mid, iid + getVrf(mid, iid) -> vrf=<mid><iid>, rdo=1 (tylko odczyt), pos=0
    return [
        ("sid", sid),
        ("mid", str(mid)),
        ("iid", str(iid)),
        ("vrf", f"{mid}{iid}"),
        ("rdo", "1"),
        ("pos", "0"),
    ]


def menu_url(sid: str, semester_iid: int) -> str:
    """Pozycja menu executeCmm(328, <semestr>, 1, ''): makeURL() + checkurl()."""
    ss = checksum(str(MID_GROUP_PLAN), str(semester_iid))
    query = [("sid", sid), ("mid", str(MID_GROUP_PLAN)), ("iid", str(semester_iid))]
    query += [("vrf", f"!{ss}"), ("rdo", "1"), ("pos", "0")]
    return BASE_URL + "logged.php?" + urlencode(query, safe="!")


def group_plan_url(sid: str, semester_iid: int, group: str) -> str:
    """showGroupPlan(grupa): getURL() + exv=<grupa>, a potem checkurl() dopisuje vrf=!<suma>."""
    query = [*_base_query(sid, MID_GROUP_PLAN, semester_iid), ("exv", group)]
    ss = checksum(str(MID_GROUP_PLAN), str(semester_iid), group)
    query += [("vrf", f"!{ss}"), ("rdo", "1"), ("pos", "0")]
    return BASE_URL + "logged.php?" + urlencode(query, safe="!")


def export_url(sid: str, semester_iid: int, group: str) -> str:
    """downloadCSV('TXT'): prepareURL() + 'D' + 'TXT' = getURL() + exv=<grupa> + opr=DTXT."""
    query = [*_base_query(sid, MID_GROUP_PLAN, semester_iid), ("exv", group), ("opr", "DTXT")]
    return BASE_URL + "logged.php?" + urlencode(query)


# --- parsowanie formularza logowania ------------------------------------------------------


class _LoginFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_form = False
        self.action: str | None = None
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "form" and "index.php?sid=" in a.get("action", ""):
            self.in_form, self.action = True, a["action"]
        elif tag == "input" and self.in_form and a.get("type", "text").lower() == "hidden":
            self.fields[a.get("name", "")] = a.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_form = False


# --- klient -------------------------------------------------------------------------------


class EwigClient:
    def __init__(
        self,
        login: str,
        password: str,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not login or not password:
            raise EwigLoginError("Brak loginu lub hasła do ewig (EWIG_LOGIN / EWIG_PASSWORD).")
        self._login = login
        self._password = password
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self._sleep = sleep
        self.sid: str | None = None
        self._referer: str | None = None

    def _get(self, url: str, step: str) -> requests.Response:
        return self._request("GET", url, step)

    def _request(self, method: str, url: str, step: str, **kwargs) -> requests.Response:
        """Zapytanie jak z przeglądarki: Referer poprzedniej strony, jedna ponowna próba."""
        headers = {"Referer": self._referer} if self._referer else {}
        if method == "POST":
            headers["Origin"] = BASE_URL.rstrip("/").rsplit("/", 1)[0]
        where = f"{step} ({urlparse(url).path})"  # bez parametrów: zawierają identyfikator sesji
        last: str = ""
        for attempt in range(2):  # jedna ponowna próba przy błędzie sieci lub 5xx
            try:
                response = self.session.request(
                    method, url, headers=headers, timeout=TIMEOUT_SECONDS, **kwargs
                )
            except requests.RequestException as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code < 500:
                    if response.status_code >= 400:
                        raise EwigError(
                            f"ewig zwrócił HTTP {response.status_code} — krok: {where}."
                        )
                    self._referer = response.url or url
                    return response
                last = f"HTTP {response.status_code}"
            if attempt == 0:
                self._sleep(5)
        raise EwigError(f"Brak połączenia z ewig — krok: {where}: {last}")

    @staticmethod
    def _text(response: requests.Response) -> str:
        return response.content.decode(PAGE_ENCODING, errors="replace")

    def login(self) -> str:
        # Czysta sesja: bez ciasteczek i nagłówka Referer z poprzedniego logowania.
        self.session.cookies.clear()
        self._referer = None
        page = self._text(self._get(BASE_URL, "strona logowania"))
        form = _LoginFormParser()
        form.feed(page)
        if not form.action:
            raise EwigError("Nie znaleziono formularza logowania — strona ewig się zmieniła.")
        data = {**form.fields, "formname": "login", "default_fun": "1"}
        data.update(userid=self._login, password=self._password)
        response = self._request(
            "POST",
            BASE_URL + form.action,
            "logowanie",
            data={k: v.encode(PAGE_ENCODING) for k, v in data.items()},
        )
        text = self._text(response)
        match = SID_IN_PAGE.search(text)
        if "Zalogowany" not in text or not match:
            if 'name="userid"' in text or "name=userid" in text:
                raise EwigLoginError("Logowanie do ewig nie powiodło się — sprawdź login i hasło.")
            raise EwigError("Nieoczekiwana odpowiedź po logowaniu — strona ewig się zmieniła.")
        self.sid = match.group(1)
        return self.sid

    def fetch_group_csv(self, semester_iid: int, group: str) -> bytes:
        if self.sid is None:
            raise EwigError("Najpierw zaloguj się (login()).")
        # Jak w przeglądarce: najpierw pozycja menu „Rozkład zajęć grupy”, potem wybór grupy.
        self._get(menu_url(self.sid, semester_iid), "menu planu grup")
        plan = self._text(self._get(group_plan_url(self.sid, semester_iid, group), f"plan {group}"))
        if group not in plan:
            raise EwigError(f"Nie udało się otworzyć planu grupy {group}.")
        data = self._get(export_url(self.sid, semester_iid, group), f"eksport {group}").content
        head = data[:512].lstrip().lower()
        if head.startswith(b"<") or b"<html" in head:
            raise EwigError(f"Eksport planu grupy {group} zwrócił stronę HTML zamiast pliku CSV.")
        return data

    def logout(self) -> None:
        if self.sid is None:
            return
        try:
            self.session.get(f"{BASE_URL}index.php?sid={self.sid}&lou=1", timeout=TIMEOUT_SECONDS)
        except requests.RequestException:
            pass  # wylogowanie jest uprzejmością; sesja i tak wygaśnie
        finally:
            self.sid = None


def _check_export(group: str, data: bytes, events, previous: dict[str, bytes]) -> None:
    """Wykrywa pliki sklejone przez ewig z poprzednim eksportem.

    ewig buduje eksport w pliku tymczasowym sesji. W jednej sesji drugi eksport potrafił
    mieć na początku bajty pierwszego (cały plik poprzedniej grupy, potem reszta nowego,
    urwana w połowie wiersza). Chroni przed tym osobna sesja dla każdej grupy, a to jest
    druga linia obrony: plik zaczynający się od innego eksportu albo z wierszami nie po
    kolei (ewig zawsze sortuje zajęcia chronologicznie) jest odrzucany.
    """
    for other, other_data in previous.items():
        glued = data.startswith(other_data) or other_data.startswith(data)
        if other_data and len(other_data) != len(data) and glued:
            raise EwigError(
                f"Plik grupy {group} jest sklejony z plikiem grupy {other} "
                "(błąd eksportu ewig) — pomijam synchronizację."
            )
    starts = [(e.start_date, e.start_time) for e in events]
    for row_event, prev_start, start in zip(events[1:], starts, starts[1:], strict=False):
        if start < prev_start:
            raise EwigError(
                f"Plik grupy {group} ma zajęcia nie po kolei (wiersz {row_event.row}: "
                f"„{row_event.subject}”) — wygląda na uszkodzony eksport ewig, "
                "pomijam synchronizację."
            )


def fetch_sources(
    client: EwigClient, semester_iid: int, groups: list[EwigGroup]
) -> list[CsvFileSource]:
    """Pobiera CSV wszystkich grup (w kolejności priorytetu), każdą w osobnej sesji ewig.

    Osobne logowanie dla każdej grupy, bo ewig trzyma eksport w pliku tymczasowym sesji
    i w jednej sesji potrafi skleić drugi plik z pierwszym (patrz _check_export).
    Każdy plik musi przejść parser bez błędów, zawierać co najmniej jedno zdarzenie i być
    spójny — inaczej EwigError (lepiej nie synchronizować niż usunąć zajęcia przez zły plik).
    """
    sources = []
    fetched: dict[str, bytes] = {}
    for group in groups:
        client.login()
        try:
            data = client.fetch_group_csv(semester_iid, group.code)
        finally:
            client.logout()
        parsed = parse_outlook_csv(data, source_id=group.name)
        errors = [i for i in parsed.issues if i.level == "error"]
        if errors:
            raise EwigError(
                f"Plik grupy {group.code} ma błędy: " + "; ".join(str(e) for e in errors[:3])
            )
        if not parsed.events:
            raise EwigError(f"Plik grupy {group.code} nie zawiera żadnych zdarzeń.")
        _check_export(group.code, data, parsed.events, fetched)
        fetched[group.code] = data
        sources.append(
            CsvFileSource(id=group.name, name=group.name, data=data, filename=f"{group.code}.csv")
        )
    return sources
