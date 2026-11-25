"""Jednorazowy zwiad: jak w ewig otworzyć plan prowadzącego (nie do scalenia z main)."""
import os, re, sys
from gcalsync.sources import ewig as E

TEACHER = os.environ.get("TEACHER", "Jenerowicz")
c = E.EwigClient(os.environ["EWIG_LOGIN"], os.environ["EWIG_PASSWORD"])
sid = c.login()
secrets = [sid, os.environ["EWIG_LOGIN"], os.environ["EWIG_PASSWORD"]]

def scrub(s):
    for x in secrets:
        if x:
            s = s.replace(x, "<X>")
    return s

def get(url, step):
    return c._text(c._get(url, step))

home = get(c._referer, "home")
print("HOMEURL", scrub(c._referer))
print("=== HOME len", len(home))
# Pozycje menu: wywołania JS z etykietami
for m in re.finditer(r"""(executeCmm\w*|exeCmm\w*|callFunction|showPlan|ownData)\(([^)]*)\)[^>]*>(.*?)</""", home, re.S | re.I):
    label = re.sub(r"<[^>]+>|\s+", " ", m.group(3)).strip()
    print("MENU", m.group(1), m.group(2), "|", label[:80])
for m in re.finditer(r"var\s+(_\w+)\s*=\s*(\d+)", home):
    print("VAR", m.group(1), m.group(2))

plan = get(E.group_plan_url(sid, 20261, "WIG23IX2S1"), "plan")
print("=== PLAN len", len(plan))
for m in re.finditer(TEACHER, plan):
    a = max(0, m.start() - 400); b = m.end() + 200
    print("CTX", scrub(plan[a:b]).replace("\n", " "))
    break
# Wszystkie onclick / href z logged.php i funkcje JS z „teach”/„prow”
for m in set(re.findall(r"""on\w+\s*=\s*["']([^"']{0,200})["']""", plan)):
    print("ONCLICK", scrub(m)[:200])
for m in set(re.findall(r"""<script[^>]*src\s*=\s*["']([^"']+)""", plan + home)):
    print("SCRIPT", m)
for m in re.finditer(r"<select\b.*?</select>", plan, re.S | re.I):
    opts = re.findall(r"<option[^>]*value\s*=\s*[\"']?([^\"' >]*)[^>]*>([^<]*)", m.group(0))
    print("SELECT", len(opts), [o for o in opts if re.search(TEACHER, o[1], re.I)][:5], opts[:5])
c.logout()
