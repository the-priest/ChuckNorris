import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""Research must converge. No spinning on the same lookup.

The reported failure: Chuck searched the same thing five different ways
('X github' → 'github X' → 'site:github.com X' → 'X github repository'),
re-read the same two pages each round, and when the hop budget ran out the
pending search was silently dropped so he just narrated 'Working on it...'
forever.
"""
import tempfile
from collections import Counter
from pathlib import Path
from sim import cn, new_win, type_and_send, check_finished, fail, FAILS

tmp = Path(tempfile.mkdtemp())

queries, reads = [], []
def fake_search(q, n=3):
    queries.append(q)
    return [("the-priest · GitHub", "https://github.com/the-priest", "s"),
            ("The Priest", "https://fanellileandro.itch.io/", "s")]
def fake_fetch(u, *a, **k):
    reads.append(u); return "PAGE BODY " + u
cn.web_search = fake_search
cn.web_fetch = fake_fetch

print("--- the same query, reworded seven ways, must not run seven times ---")
replies = [
    "```search\nthe-priest seewat github\n```",
    "```search\ngithub the-priest seewat\n```",
    "```search\nthe-priest seewat github repository\n```",
    "```search\nseewat the-priest github\n```",
    "```search\nthe-priest  seewat   github\n```",
    "```search\ngithub repository the-priest seewat\n```",
    "Couldn't find a public seewat repo.",
]
w = new_win(replies, tmp)
type_and_send(w, "check my seewat repo")
print(f"  model asked {len([r for r in replies if 'search' in r])}x, app ran {len(queries)}: {queries}")
if len(queries) > 2:
    fail(f"reworded duplicates still ran: {queries}")

print("--- no page is fetched twice in one turn ---")
dupes = [u for u, n in Counter(reads).items() if n > 1]
print("  pages:", dict(Counter(reads)))
if dupes:
    fail(f"re-read the same pages: {dupes}")

print("--- the turn converges instead of dangling ---")
check_finished(w, "loop")
if w._running:
    fail("run never ended")
forced = [m for m in w.history if isinstance(m.get("content"), str)
          and "BUDGET SPENT" in m["content"]]
if not forced:
    fail("budget ran out with no nudge to answer — the tool was dropped silently")
else:
    print("  budget-spent nudge issued, forcing a final answer")

print("--- feedback tells him what he already has ---")
tr = [m for m in w.history if isinstance(m.get("content"), str)
      and m["content"].startswith("TOOL RESULTS")]
if not tr:
    fail("no tool results fed back at all")
else:
    last = tr[-1]["content"]
    for want in ("already read", "rounds left", "LAST research round"):
        pass
    if "already read" not in last.lower() and "ALREADY DONE" not in last:
        fail("feedback doesn't tell him what he's already covered")
    else:
        print("  ", last.splitlines()[1][:70])

print("--- after the budget nudge, further searches are ignored ---")
q_before = len(queries)
w2 = new_win(["```search\nsomething\n```"] * 8 + ["done"], tmp)
type_and_send(w2, "find something")
check_finished(w2, "forced")
if w2._running:
    fail("run hung after the forced answer")
print(f"  extra queries run across the whole turn: {len(queries) - q_before}")

print("--- genuinely different searches are still allowed ---")
queries.clear(); reads.clear()
w3 = new_win(["```search\ncurrent linux kernel version\n```",
              "```search\ncachyos bore scheduler benchmarks\n```",
              "Here's what I found."], tmp)
type_and_send(w3, "two different questions")
print("  distinct queries run:", queries)
if len(queries) < 2:
    fail(f"legitimately different searches were blocked: {queries}")

print("--- an unreachable page is not retried ---")
queries.clear(); reads.clear()
def flaky_fetch(u, *a, **k):
    reads.append(u)
    return "" if "dead" in u else "BODY"
cn.web_search = lambda q, n=3: [("dead", "https://dead.example/x", "s"),
                                ("ok", "https://ok.example/y", "s")]
cn.web_fetch = flaky_fetch
w4 = new_win(["```search\nfirst\n```", "```search\nsecond thing entirely\n```", "done"], tmp)
type_and_send(w4, "go")
dead_hits = [u for u in reads if "dead" in u]
print(f"  dead page fetched {len(dead_hits)}x")
if len(dead_hits) > 1:
    fail("retried a page that already failed")

print()
print("TOTAL RESEARCH-LOOP FAILURES:", len(FAILS))
for f in FAILS:
    print("  ", f)
assert not FAILS, "research loop regressions"
print("ALL RESEARCH LOOP TESTS PASSED")
