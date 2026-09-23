"""Pobieranie planu grup z e-Dziekanatu WIG (ewig.wcy.wat.edu.pl/ed2).

Klient robi to samo co przeglądarka (ustalone z zapisanych stron i ich JavaScriptu):
1. GET strony logowania — identyfikator sesji `sid` jest w adresie formularza,
2. POST formularza logowania (`formname=login`, `default_fun=1` = „Aktualności”),
3. dla każdej grupy: GET planu grupy (`mid=328`, `iid=<semestr>`, `exv=<kod grupy>`),
   potem GET eksportu „w formacie Outlooka” (`opr=DTXT`, to samo co ikona eksportu),
4. wylogowanie (`index.php?sid=…&lou=1`).

Hasło nie jest nigdzie logowane ani umieszczane w komunikatach błędów.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlencode

import requests

from gcalsync.sources.outlook_csv import CsvFileSource, parse_outlook_csv

BASE_URL = "https://ewig.wcy.wat.edu.pl/ed2/"
PAGE_ENCODING = "iso-8859-2"
MID_GROUP_PLAN = 328
TIMEOUT_SECONDS = 30
USER_AGENT = "gcalsync (synchronizacja planu zajęć; https://github.com/JBerlinski/Gcalsync)"

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


def checksum(*values: str, used: int = 0) -> int:
    """Suma kontrolna `vrf` z funkcji checkurl(): dla każdej cyfry na pozycji j dodaje j+cyfra."""
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

    def _get(self, url: str) -> requests.Response:
        return self._request("GET", url)

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        last: Exception | None = None
        for attempt in range(2):  # jedna ponowna próba przy błędzie sieci lub 5xx
            try:
                response = self.session.request(method, url, timeout=TIMEOUT_SECONDS, **kwargs)
            except requests.RequestException as exc:
                last = exc
            else:
                if response.status_code < 500:
                    if response.status_code >= 400:
                        raise EwigError(f"ewig zwrócił HTTP {response.status_code}.")
                    return response
                last = EwigError(f"ewig zwrócił HTTP {response.status_code}.")
            if attempt == 0:
                self._sleep(5)
        raise EwigError(f"Brak połączenia z ewig: {type(last).__name__}: {last}") from None

    @staticmethod
    def _text(response: requests.Response) -> str:
        return response.content.decode(PAGE_ENCODING, errors="replace")

    def login(self) -> str:
        page = self._text(self._get(BASE_URL))
        form = _LoginFormParser()
        form.feed(page)
        if not form.action:
            raise EwigError("Nie znaleziono formularza logowania — strona ewig się zmieniła.")
        data = {**form.fields, "formname": "login", "default_fun": "1"}
        data.update(userid=self._login, password=self._password)
        response = self._request(
            "POST",
            BASE_URL + form.action,
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
        plan = self._text(self._get(group_plan_url(self.sid, semester_iid, group)))
        if group not in plan:
            raise EwigError(f"Nie udało się otworzyć planu grupy {group}.")
        data = self._get(export_url(self.sid, semester_iid, group)).content
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


def fetch_sources(
    client: EwigClient, semester_iid: int, groups: list[EwigGroup]
) -> list[CsvFileSource]:
    """Loguje się, pobiera CSV wszystkich grup (w kolejności priorytetu) i się wylogowuje.

    Każdy plik musi przejść parser bez błędów i zawierać co najmniej jedno zdarzenie —
    inaczej EwigError (lepiej nie synchronizować niż usunąć zajęcia przez zły plik).
    """
    client.login()
    try:
        sources = []
        for group in groups:
            data = client.fetch_group_csv(semester_iid, group.code)
            parsed = parse_outlook_csv(data, source_id=group.name)
            errors = [i for i in parsed.issues if i.level == "error"]
            if errors:
                raise EwigError(
                    f"Plik grupy {group.code} ma błędy: " + "; ".join(str(e) for e in errors[:3])
                )
            if not parsed.events:
                raise EwigError(f"Plik grupy {group.code} nie zawiera żadnych zdarzeń.")
            sources.append(
                CsvFileSource(
                    id=group.name, name=group.name, data=data, filename=f"{group.code}.csv"
                )
            )
        return sources
    finally:
        client.logout()
