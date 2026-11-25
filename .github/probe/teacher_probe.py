"""Jednorazowy zwiad: plan prowadzącego i sali w ewig (nie do scalenia z main)."""
import os, re
from urllib.parse import urlencode
from gcalsync.sources import ewig as E
from gcalsync.sources.ewig_plan import parse_plan_grid

TEACHER = os.environ.get("TEACHER", "Jenerowicz")
DATES = ("2026-12-02", "2026-12-23")
SEM = 20261
c = E.EwigClient(os.environ["EWIG_LOGIN"], os.environ["EWIG_PASSWORD"])
sid = c.login()
secrets = [sid, os.environ["EWIG_LOGIN"], os.environ["EWIG_PASSWORD"]]

def scrub(s):
    for x in secrets:
        if x:
            s = s.replace(x, "<X>")
    return s

def menu(mid):
    ss = E.checksum(str(mid), str(SEM))
    q = [("sid", sid), ("mid", str(mid)), ("iid", str(SEM)), ("vrf", f"!{ss}"), ("rdo", "1"), ("pos", "0")]
    return E.BASE_URL + "logged.php?" + urlencode(q, safe="!")

def plan_url(mid, exv):
    q = [*E._base_query(sid, mid, SEM), ("exv", exv)]
    ss = E.checksum(str(mid), str(SEM), exv)
    q += [("vrf", f"!{ss}"), ("rdo", "1"), ("pos", "0")]
    return E.BASE_URL + "logged.php?" + urlencode(q, safe="!")

def get(url, step):
    return c._text(c._get(url, step))

def show(page, label):
    print(f"=== {label}: len {len(page)}")
    grid = parse_plan_grid(page, SEM)
    print("grid events:", len(grid))
    for ev in grid:
        if str(ev.day) in DATES:
            print("  EV", ev)
    if not grid:
        titles = re.findall(r'title="([^"]*\([A-Za-zżźćńółęąś]+\))"', page)
        print("titles sample:", titles[:10])

for mid, fn in ((327, "showPersonPlan"), (294, "showRoomPlan")):
    page = get(menu(mid), f"menu {mid}")
    for m in re.finditer(fn, page):
        print("USE", fn, scrub(page[max(0, m.start()-200):m.end()+300]).replace("\n", " "))
        break
    for m in re.finditer(r"function\s+" + fn + r"\b.*?\n\s*\}", page, re.S):
        print("DEF", scrub(m.group(0))[:1500])
    for m in re.finditer(r"<script[^>]*src\s*=\s*[\"']([^\"']+)", page):
        print("SRC", m.group(1))
    for m in re.finditer(r"<select\b[^>]*>", page):
        print("SELECT", m.group(0)[:200])
    if mid == 294:
        opts = re.findall(r'<option\s+value="(\d+)"[^>]*>([^<]*)</option>', page)
        print("ROOM OPTS 17*:", [(v, t) for v, t in opts if t.strip().startswith("17")][:20])
        print("ROOM OPTS count", len(opts), opts[:3], opts[-3:])

p = get(plan_url(327, "1863"), "plan 1863")
print("TEACHER IN PAGE", TEACHER in p, "len", len(p))
show(p, "PLAN nauczyciela exv=1863")
grid = parse_plan_grid(p, SEM)
for ev in grid:
    if ev.day.isoformat() >= "2026-11-30" and ev.day.isoformat() <= "2026-12-24" and ev.day.weekday() == 2:
        print("  WED", ev.day, ev.start, ev.end, ev.course, ev.kind, ev.seq, ev.room)
c.logout()
