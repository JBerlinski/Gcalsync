"""Atrapa serwera ewig dla requests (bez sieci).

Strony są uproszczone, ale mają te same elementy, na których opiera się klient (formularz
logowania z `index.php?sid=…`, `var sid = new String('…')`, „Zalogowany student”, kod grupy
w planie), odtworzone z zapisanych stron ewig — bez danych osobowych i prawdziwych sesji.
"""

from __future__ import annotations

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


def plan_page(group: str) -> str:
    return (
        f"<html><body><script>var sid = new String('{SID}');</script>"
        f"<a href=\"javascript:downloadCSV('TXT');\">eksport</a>"
        f"<div>{group} (2026-09-23)</div></body></html>"
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

    def _response(self, request, status: int, body: bytes) -> requests.Response:
        response = requests.Response()
        response.status_code = status
        response._content = body
        response.url = request.url
        response.request = request
        response.headers["Content-Type"] = "text/html; charset=iso-8859-2"
        return response

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
            page = LOGGED_PAGE if ok else LOGIN_PAGE
        elif url.path == "/ed2/index.php" and query.get("lou") == ["1"]:
            self.logged_in = False
            page = LOGIN_PAGE
        elif url.path == "/ed2/logged.php":
            if not self.logged_in or query.get("sid") != [SID]:
                page = LOGIN_PAGE
            elif query.get("opr") == ["DTXT"]:
                group = query["exv"][0]
                if group not in self.files:
                    return self._response(request, 200, b"<html>brak grupy</html>")
                return self._response(request, 200, self.files[group])
            else:
                page = plan_page(query["exv"][0])
        else:
            return self._response(request, 404, b"not found")
        data = page.encode("iso-8859-2") if isinstance(page, str) else page
        return self._response(request, 200, data)

    def close(self):
        pass
