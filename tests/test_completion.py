import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""Finishing the job: the agent keeps going until the task is done, and stops
only to ask the user a question it genuinely can't answer itself.

Each block is a failure users actually saw, or the over-correction it could
turn into:
  * "I'll write the game now." — and then nothing (announce, then stop);
  * a plan posted, step one done, the rest silently dropped;
  * code written and never run, reported as working;
  * the opposite: an agent that won't stop — nudged after it asked a question,
    nudged forever, nudged in Chat mode, nudged past the step budget.
"""
import tempfile
from pathlib import Path

from sim import cn, new_win, type_and_send, settle, check_finished, check_history, FAILS

fails = 0


def check(cond, label, detail=""):
    global fails
    if cond:
        print(f"  [OK]   {label}")
    else:
        fails += 1
        print(f"  [FAIL] {label}  {detail}")


tmp = Path(tempfile.mkdtemp())
cn._builder.PROJECTS = tmp / "projects"
cn._builder.PROJECTS.mkdir()
cn._builder._STATE = cn._builder.PROJECTS / ".current"


def hist_text(w):
    return " ".join(str(m.get("content", "")) for m in w.history)


def agent(replies, folder=None):
    w = new_win(replies, tmp)
    w.set_agent_mode(True, announce=False)
    if folder:
        w.set_workspace(folder, announce=False)
    return w


def fresh_folder(name):
    d = tmp / name
    d.mkdir()
    return d


def cards(w, title):
    return [x for x in w.msgbox.walk() if x.get_text() == title]


# ═══════════════════════════════════════════════════════════════════════════
print("\n--- announce, then stop: the reported bug ---")
ws = fresh_folder("game")
w = agent(["Sure — I'll write the game now.",
           "```write game.py\nprint('snake')\n```",
           "```bash\npython3 game.py\n```",
           "Done: `game.py` runs and prints `snake`."], ws)
type_and_send(w, "write me a snake game")
check((ws / "game.py").exists(), "the file got written after the nudge")
check("snake" in hist_text(w) and "SUCCEEDED (exit 0)" in hist_text(w),
      "and it was run before the final report")
check(len(w.backend.calls) == 4, "four model calls: promise, write, run, report",
      f"{len(w.backend.calls)}")
check_finished(w, "announce then stop")
check_history(w, "announce then stop")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- an unfinished plan is carried through ---")
w = agent(["```plan\n- [ ] look around\n- [ ] report\n```",
           "Looked around.",
           "```plan\n- [x] look around\n- [x] report\n```",
           "All done."])
type_and_send(w, "survey the box")
check("unfinished step(s): report" in hist_text(w) or "unfinished step(s): look around"
      in hist_text(w), "stopping with open plan steps sends it back to finish them")
check(len(w.backend.calls) >= 3, "the model was asked again after the early stop",
      f"{len(w.backend.calls)}")
check_finished(w, "plan carried through")

w = agent(["```plan\n- [ ] a\n- [ ] b\n```"] + ["Okay."] * 20)
type_and_send(w, "stubborn plan")
nudges = hist_text(w).count("unfinished step(s)")
check(nudges == 3, "plan nudges are bounded at three, never a loop", f"{nudges}")
check(len(w.backend.calls) <= 5, "so a stubborn model still ends the run",
      f"{len(w.backend.calls)}")
check_finished(w, "plan nudge cap")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- changed code is verified before 'done' ---")
ws = fresh_folder("verify")
w = agent(["```write calc.py\nprint(2 + 2)\n```",
           "Wrote calc.py, it works.",
           "```bash\npython3 calc.py\n```",
           "Verified: prints 4."], ws)
type_and_send(w, "write calc")
h = hist_text(w)
check("nothing ran after the last change" in h, "a write followed by 'done' gets a verify nudge")
check("calc.py" in h.split("nothing ran after the last change")[0][-200:],
      "the nudge names the file")
check(len(w.backend.calls) == 4, "and the agent ran it before finishing",
      f"{len(w.backend.calls)}")
check_finished(w, "verify nudge")

ws = fresh_folder("verify_once")
w = agent(["```write a.py\nprint(1)\n```", "Done.", "Still done."] + ["Done."] * 5, ws)
type_and_send(w, "write a")
check(hist_text(w).count("nothing ran after the last change") == 1,
      "the verify nudge fires once, not every reply")
check(len(w.backend.calls) == 3, "then the run ends", f"{len(w.backend.calls)}")
check_finished(w, "verify once")

ws = fresh_folder("verified")
w = agent(["```write b.py\nprint(1)\n```", "```bash\npython3 b.py\n```", "Works."], ws)
type_and_send(w, "write b and run it")
check("nothing ran after the last change" not in hist_text(w),
      "already-run code is not nudged")
check(len(w.backend.calls) == 3, "no extra round trip", f"{len(w.backend.calls)}")

ws = fresh_folder("notes")
w = agent(["```write NOTES.md\n# notes\n```", "Wrote the notes."], ws)
type_and_send(w, "write notes")
check("nothing ran after the last change" not in hist_text(w),
      "a docs-only change needs no run")
check(len(w.backend.calls) == 2, "two calls", f"{len(w.backend.calls)}")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- asking: it stops to ask, and only then ---")
w = agent(["```ask\nWhich engine should the game use?\n- pygame\n- curses\n```",
           "Going with pygame.", "x"])
type_and_send(w, "make a game")
check(len(w.backend.calls) == 1, "an ```ask``` ends the turn with no nudge",
      f"{len(w.backend.calls)}")
check(not w._running, "and the run is over, waiting for the user")
check(cards(w, "QUESTION"), "a question card is shown")
opts = [x for x in w.msgbox.walk() if x.get_label() in ("pygame", "curses")]
check(len(opts) == 2, "with one button per option", f"{[o.get_label() for o in opts]}")
check_finished(w, "ask")
pick = [o for o in opts if o.get_label() == "pygame"]
if pick:
    pick[0].click()
    settle()
check(any(m["role"] == "user" and "pygame" in str(m["content"]) for m in w.history),
      "clicking an option sends it as the answer")
check(len(w.backend.calls) == 2, "which starts the next turn", f"{len(w.backend.calls)}")
check_finished(w, "ask answered")

w = agent(["```plan\n- [ ] pick a db\n- [ ] build\n```",
           "Should I use SQLite or Postgres for this?", "x"])
type_and_send(w, "build a todo app")
check(len(w.backend.calls) == 2, "a reply that ends in a question is never nudged, "
      "even with plan steps open", f"{len(w.backend.calls)}")
check("unfinished step(s)" not in hist_text(w), "no plan nudge after a question")
check_finished(w, "question in prose")

w = agent(["I'll set up the database next, which one do you prefer?", "x"])
type_and_send(w, "set it up")
check(len(w.backend.calls) == 1, "an intent line that is really a question is not nudged",
      f"{len(w.backend.calls)}")

w = agent(["```ask\n\n```", "Fine, done."])
type_and_send(w, "blank ask")
check(not cards(w, "QUESTION"), "an empty ```ask``` shows no card")
check_finished(w, "empty ask")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- it never overrides the user or its own limits ---")
w = new_win(["```plan\n- [ ] a\n```", "Okay.", "Okay."], tmp)
w.set_agent_mode(False, announce=False)
type_and_send(w, "chat about plans")
check(len(w.backend.calls) == 1, "Chat mode is never nudged", f"{len(w.backend.calls)}")
check_finished(w, "chat no gap")

w = agent(["```plan\n- [ ] a\n- [ ] b\n```", "```bash\necho 1\n```",
           "```bash\necho 2\n```"] + ["Stopping."] * 10)
w.settings["agent_steps"] = 4          # the setting's floor
type_and_send(w, "tight budget")
w.settings.pop("agent_steps", None)
# plan, echo 1, echo 2 use three steps; one nudge takes the fourth; then it ends
check(len(w.backend.calls) == 5, "completion nudges respect the step budget",
      f"{len(w.backend.calls)}")
check(hist_text(w).count("unfinished step(s)") == 1, "only one nudge fit in the budget")
check_finished(w, "gap within budget")

w = agent(["```plan\n- [ ] a\n```", "Okay.", "Okay.", "Okay.", "Okay."])
type_and_send(w, "first")
first = len(w.backend.calls)
w.backend.replies = ["Hello."]
type_and_send(w, "unrelated question")
check(len(w.backend.calls) == first + 1,
      "a plan from an earlier turn doesn't hijack the next one",
      f"{len(w.backend.calls) - first}")
check_finished(w, "plan per turn")

# ═══════════════════════════════════════════════════════════════════════════
print(f"\nTOTAL COMPLETION FAILURES: {fails + len(FAILS)}")
assert fails == 0 and not FAILS, "completion regressions present"
print("ALL COMPLETION TESTS PASSED")
