from urllib.parse import parse_qsl, urlparse

import pytest
import requests
from conftest import DEFAULT_GROUP, NEW_GROUP, make_csv
from fake_ewig import SID, FakeEwig

from gcalsync.sources.ewig import (
    EwigClient,
    EwigError,
    EwigGroup,
    EwigLoginError,
    checksum,
    export_url,
    fetch_sources,
    group_plan_url,
    menu_url,
)

GROUPS = [
    EwigGroup("WIG23IX2S1", "Grupa kierunkowa"),
    EwigGroup("WIG23IX1S1", "Grupa domyślna"),
]
FILES = {"WIG23IX2S1": NEW_GROUP.read_bytes(), "WIG23IX1S1": DEFAULT_GROUP.read_bytes()}


def client(fake: FakeEwig, password: str = "sekret") -> EwigClient:
    session = requests.Session()
    session.mount("https://", fake)
    return EwigClient("login", password, session=session, sleep=lambda _s: None)


# --- adresy odtworzone z JavaScriptu ewig -------------------------------------------------


def test_checksum_matches_checkurl_algorithm():
    # mid "328": 0+3, 1+2, 2+8 = 16; iid "20261": 2+1+4+9+5 = 21;
    # exv "WIG23IX2S1": cyfry na pozycjach 3,4,7,9 -> 5+7+9+10 = 31
    # plus used = 0x45 (69) ustawione przez prolongTimeOut() na początku checkurl()
    assert checksum("328", "20261", "WIG23IX2S1") == 68 + 69
    assert checksum("328", "20261", "WIG23IX2S1", used=0) == 68


def test_menu_url():
    params = parse_qsl(urlparse(menu_url("abc", 20261)).query)
    assert params == [
        ("sid", "abc"),
        ("mid", "328"),
        ("iid", "20261"),
        ("vrf", "!106"),  # 16 + 21 + 69
        ("rdo", "1"),
        ("pos", "0"),
    ]


def test_group_plan_url():
    params = parse_qsl(urlparse(group_plan_url("abc", 20261, "WIG23IX2S1")).query)
    assert params == [
        ("sid", "abc"),
        ("mid", "328"),
        ("iid", "20261"),
        ("vrf", "32820261"),
        ("rdo", "1"),
        ("pos", "0"),
        ("exv", "WIG23IX2S1"),
        ("vrf", "!137"),
        ("rdo", "1"),
        ("pos", "0"),
    ]
    assert "vrf=!137" in group_plan_url("abc", 20261, "WIG23IX2S1")


def test_export_url_is_download_csv_txt():
    url = export_url("abc", 20261, "WIG23IX1S1")
    assert url.startswith("https://ewig.wcy.wat.edu.pl/ed2/logged.php?")
    assert parse_qsl(urlparse(url).query) == [
        ("sid", "abc"),
        ("mid", "328"),
        ("iid", "20261"),
        ("vrf", "32820261"),
        ("rdo", "1"),
        ("pos", "0"),
        ("exv", "WIG23IX1S1"),
        ("opr", "DTXT"),
    ]


# --- przepływ na atrapie serwera ----------------------------------------------------------


def test_fetch_sources_logs_in_downloads_and_logs_out():
    fake = FakeEwig(FILES)
    sources = fetch_sources(client(fake), 20261, GROUPS)
    assert [s.name for s in sources] == ["Grupa kierunkowa", "Grupa domyślna"]
    assert sources[0].data == NEW_GROUP.read_bytes()
    assert sources[1].filename == "WIG23IX1S1.csv"

    methods = [(m, p) for m, p, _ in fake.log]
    assert methods[0] == ("GET", "/ed2/")
    assert methods[1] == ("POST", "/ed2/index.php")
    login_form = fake.log[1][2]
    assert login_form["formname"] == ["login"]
    assert login_form["default_fun"] == ["1"]  # „Aktualności”
    assert login_form["view_height"] == ["1"]  # pola ukryte formularza są przekazywane
    # Dla każdej grupy: menu, plan grupy, eksport.
    steps = [
        q.get("opr", q.get("exv", ["menu"]))[0] for _, p, q in fake.log if p == "/ed2/logged.php"
    ]
    assert steps == ["menu", "WIG23IX2S1", "DTXT", "menu", "WIG23IX1S1", "DTXT"]
    assert fake.log[-1][2]["lou"] == ["1"]  # wylogowanie
    assert not fake.logged_in


def test_wrong_password():
    fake = FakeEwig(FILES)
    with pytest.raises(EwigLoginError, match="login i hasło"):
        fetch_sources(client(fake, password="zle"), 20261, GROUPS)


def test_password_never_in_error_messages():
    fake = FakeEwig(FILES)
    with pytest.raises(EwigError) as exc:
        fetch_sources(client(fake, password="TajneHaslo123"), 20261, GROUPS)
    assert "TajneHaslo123" not in str(exc.value)


def test_missing_credentials():
    with pytest.raises(EwigLoginError, match="EWIG_LOGIN"):
        EwigClient("", "")


def test_html_instead_of_csv_is_error_and_still_logs_out():
    fake = FakeEwig({"WIG23IX2S1": NEW_GROUP.read_bytes()})
    with pytest.raises(EwigError, match="HTML zamiast pliku CSV"):
        fetch_sources(client(fake), 20261, GROUPS)
    assert fake.log[-1][2].get("lou") == ["1"]


def test_empty_or_broken_csv_is_error():
    fake = FakeEwig({**FILES, "WIG23IX1S1": make_csv()})
    with pytest.raises(EwigError, match="nie zawiera żadnych zdarzeń"):
        fetch_sources(client(fake), 20261, GROUPS)
    fake = FakeEwig({**FILES, "WIG23IX1S1": b"Temat,Cos\r\nX,Y\r\n"})
    with pytest.raises(EwigError, match="ma błędy"):
        fetch_sources(client(fake), 20261, GROUPS)


def test_changed_login_page_is_reported():
    fake = FakeEwig(FILES)
    fake.overrides["/ed2/"] = (200, b"<html>przerwa techniczna</html>")
    with pytest.raises(EwigError, match="formularza logowania"):
        client(fake).login()


def test_server_error_is_retried_once_then_fails():
    fake = FakeEwig(FILES)
    fake.overrides["/ed2/"] = (503, b"")
    with pytest.raises(EwigError, match="HTTP 503"):
        client(fake).login()
    assert [p for _, p, _ in fake.log] == ["/ed2/", "/ed2/"]


def test_login_returns_session_id():
    assert client(FakeEwig(FILES)).login() == SID


def test_bad_checksum_is_403_with_step_name():
    fake = FakeEwig(FILES)
    c = client(fake)
    c.login()
    with pytest.raises(EwigError, match=r"HTTP 403 — krok: plan X \(/ed2/logged.php\)"):
        c._get(group_plan_url(c.sid, 20261, "X").replace("!", "!1"), "plan X")
