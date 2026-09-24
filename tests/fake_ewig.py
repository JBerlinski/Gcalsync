"""Atrapa serwera ewig dla requests (bez sieci).

Strony są uproszczone, ale mają te same elementy, na których opiera się klient (formularz
logowania z `index.php?sid=…`, `var sid = new String('…')`, „Zalogowany student”, kod grupy
w planie), odtworzone z zapisanych stron ewig — bez danych osobowych i prawdziwych sesji.
"""

from __future__ import annotations

from html import escape
from urllib.parse import parse_qs, urlparse

import requests
from requests.adapters import BaseAdapter

SID = "0123456789abcdef0123456789abcdef"

LOGIN_PAGE = f"""<html><head><meta charset="iso-8859-2"></head><body>
<form name=aaa border="0" class="formChaLog" method=post action=index.php?sid={SID}>
<input type="hidden" name="formname" value="login">
<input type="radio" checked name="default_fun" VALUE="1">Aktualności
<input type="radio" name="default_fun" VALUE="2">Rozkład zajęć
<input TABINDEX=1 name="userid" class="inputChaLog">
<input type="password" TABINDEX=2 name="password" class="inputChaLog">
<input type="submit" value="Zaloguj się">
<input type="hidden" name="view_height" VALUE="1">
<input type="hidden" name="view_width" VALUE="2">
</form></body></html>"""

LOGGED_PAGE = f"""<html><head><script language="JavaScript">
        var sid = new String('{SID}');
        var used = 0;
</script></head><body><i>Zalogowany student &nbsp;: </i> <b>Student Testowy</b>
<b>WIG23IX1S1 - I3X1S1</b></body></html>"""


MENU_PAGE = f"""<html><body><script>var sid = new String('{SID}');</script>
<select name="Data1"><option value="WIG23IX1S1">WIG23IX1S1</option></select></body></html>"""


KIND_NAMES = {"w": "Wykład", "L": "Laboratorium", "P": "Projekt", "S": "Seminarium"}


def teacher_for(course: str, kind: str) -> str:
    """Prowadzący w atrapie: deterministyczne, zmyślone nazwisko."""
    return f"dr inż. {course.split()[0]} {KIND_NAMES.get(kind, kind)}"


def plan_cell(course: str, kind: str, seq: int, room: str, teacher: str) -> str:
    """Komórka planu o takiej budowie jak na prawdziwej stronie ewig (zmyślone dane)."""
    title = escape(f"{course} - {teacher} ({KIND_NAMES.get(kind, kind)})")
    return (
        f'<td class="tdFormList1DSheTeaGrpHTM3" valign="top" title="{title}"><center>'
        f'<table border="0" align="center" cellspacing="0" cellpadding="0" title="{title}">'
        f'<tbody><tr border="0"><td class="tdFormList1DSheTeaGrpHTM4" title="{title}"><nobr>'
        f'<b style="font-size:7.0pt;">ABC</b><br>(<b style="font-size:7.0pt;">{kind}</b>)'
        f"<br>{room}</nobr></td></tr>"
        f'<tr border="0"><td class="tdFormList1DSheTeaGrpHTM4" title="{title}"><nobr>'
        f'<a class="GlubN" title="{title}" href="javascript:cpg(&#39;n&#39;, &#39;1&#39;);">TeT'
        f"</a></nobr></td></tr>"
        f'<tr border="0"><td class="tdFormList1DSheTeaGrpHTM4" title="{title}">'
        f"<nobr>[{seq}]</nobr></td></tr></tbody></table></center></td>"
    )


def plan_page(group: str, data: bytes | None = None) -> str:
    from gcalsync.core.normalize import parse_subject
    from gcalsync.sources.outlook_csv import parse_outlook_csv

    cells = []
    for raw in parse_outlook_csv(data).events if data else []:
        course, kind, seq = parse_subject(raw.subject)
        if kind and seq:
            cells.append(plan_cell(course, kind, seq, raw.location, teacher_for(course, kind)))
    return (
        f"<html><body><script>var sid = new String('{SID}');</script>"
        f"<a href=\"javascript:downloadCSV('TXT');\">eksport</a>"
        f"<div>{group} (2026-09-23)</div><table><tr>{''.join(cells)}</tr></table></body></html>"
    )


class FakeEwig(BaseAdapter):
    """Adapter podpinany pod requests.Session: session.mount('https://', FakeEwig(...))."""

    def __init__(self, files: dict[str, bytes], password: str = "sekret"):
        super().__init__()
        self.files = files
        self.password = password
        self.logged_in = False
        self.log: list[tuple[str, str, dict]] = []
        self.overrides: dict[str, tuple[int, bytes]] = {}  # opr/ścieżka -> odpowiedź
        # Błąd prawdziwego ewig: eksport w pliku tymczasowym sesji; kolejny eksport w tej samej
        # sesji ma na początku bajty poprzedniego (cały poprzedni plik, dalej reszta nowego).
        self.session_export: bytes = b""
        self.logins = 0

    def _response(self, request, status: int, body: bytes) -> requests.Response:
        response = requests.Response()
        response.status_code = status
        response._content = body
        response.url = request.url
        response.request = request
        response.headers["Content-Type"] = "text/html; charset=iso-8859-2"
        return response

    @staticmethod
    def _checksum_ok(query: dict) -> bool:
        """Serwer odrzuca (403) nawigację, której vrf=!<suma> nie zgadza się z checkurl()."""
        total = sum(
            j + int(ch)
            for name in ("mid", "iid", "exv")
            for value in query.get(name, [])
            for j, ch in enumerate(value)
            if ch.isdigit()
        )
        return f"!{total + 0x45}" in query.get("vrf", [])

    def send(self, request, **kwargs):
        url = urlparse(request.url)
        query = parse_qs(url.query)
        body = request.body or ""
        form = parse_qs(body if isinstance(body, str) else body.decode("latin-1"))
        self.log.append((request.method, url.path, query | form))
        key = query.get("opr", [url.path])[0]
        if key in self.overrides:
            return self._response(request, *self.overrides[key])

        page: str | bytes
        if url.path == "/ed2/" and request.method == "GET":
            page = LOGIN_PAGE
        elif url.path == "/ed2/index.php" and request.method == "POST":
            ok = form.get("password") == [self.password] and form.get("formname") == ["login"]
            self.logged_in = ok
            if ok:
                self.logins += 1
                self.session_export = b""
            page = LOGGED_PAGE if ok else LOGIN_PAGE
        elif url.path == "/ed2/index.php" and query.get("lou") == ["1"]:
            self.logged_in = False
            page = LOGIN_PAGE
        elif url.path == "/ed2/logged.php":
            if not self.logged_in or query.get("sid") != [SID]:
                page = LOGIN_PAGE
            elif "opr" not in query and not self._checksum_ok(query):
                return self._response(request, 403, b"<html>Forbidden</html>")
            elif "exv" not in query:
                page = MENU_PAGE
            elif query.get("opr") == ["DTXT"]:
                group = query["exv"][0]
                if group not in self.files:
                    return self._response(request, 200, b"<html>brak grupy</html>")
                new = self.files[group]
                glued = self.session_export + new[len(self.session_export) :]
                self.session_export = new
                return self._response(request, 200, glued)
            else:
                page = plan_page(query["exv"][0], self.files.get(query["exv"][0]))
        else:
            return self._response(request, 404, b"not found")
        data = page.encode("iso-8859-2") if isinstance(page, str) else page
        return self._response(request, 200, data)

    def close(self):
        pass
