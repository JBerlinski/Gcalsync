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
        if str(ev.start)[:10] in DATES or str(getattr(ev, "date", ""))[:10] in DATES:
            print("  EV", ev)
    if not grid:
        titles = re.findall(r'title="([^"]*\([A-Za-zżźćńółęąś]+\))"', page)
        print("titles sample:", titles[:10])

for mid, name in ((327, "nauczyciel"), (294, "sala")):
    page = get(menu(mid), f"menu {mid}")
    print(f"=== MENU {mid} ({name}) len {len(page)}")
    needle = TEACHER if mid == 327 else "17"
    hits = [m.start() for m in re.finditer(needle, page)]
    print("hits", len(hits))
    for h in hits[:3]:
        print("CTX", scrub(page[max(0, h - 500):h + 150]).replace("\n", " "))
    # funkcje JS wywoływane z listy
    print("FUNCS", sorted(set(re.findall(r"javascript:(\w+)\(", page)))[:30])
    if mid == 327 and hits:
        h = hits[0]
        window = page[max(0, h - 500):h + 150]
        args = re.findall(r"\w+\(\s*'?([^'),]+)'?\s*[,)]", window)
        print("ARGS", args[-5:])
        for exv in reversed(args[-3:]):
            try:
                p = get(plan_url(mid, exv.strip()), f"plan {exv}")
            except Exception as exc:
                print("ERR", exv, exc); continue
            if TEACHER in p:
                show(p, f"PLAN nauczyciela exv={exv}")
                break
c.logout()
