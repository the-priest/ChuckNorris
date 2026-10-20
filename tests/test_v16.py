import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""v16 — the gap-filling roadmap (docs/ROADMAP.md, phases 5-9). One section
per item, through the REAL window on the stub toolkit."""
import json
import tempfile
import time
from pathlib import Path

from sim import cn, new_win, type_and_send, settle, check_finished, check_history, FAILS, ScriptedBackend

fails = 0


def check(cond, label, detail=""):
    global fails
    if cond:
        print(f"  [OK]   {label}")
    else:
        fails += 1
        print(f"  [FAIL] {label}  {str(detail)[:300]}")


tmp = Path(tempfile.mkdtemp(prefix="chuck_v16_"))
cn._builder.PROJECTS = tmp / "projects"
cn._builder.PROJECTS.mkdir()
cn._builder._STATE = cn._builder.PROJECTS / ".current"
cn.ChuckWindow.JOB_DIR = tmp / "jobs"


def hist_text(w):
    return " ".join(str(m.get("content", "")) for m in w.history)


def agent(replies, folder=None):
    w = new_win(replies, tmp)
    w.set_agent_mode(True, announce=False)
    if folder:
        w.set_workspace(folder, announce=False)
    return w


def folder(name):
    d = tmp / name
    d.mkdir(parents=True)
    return d


class Slow(ScriptedBackend):
    """Each reply takes a moment, so the user can type mid-run."""
    def stream(self, *a, **k):
        time.sleep(0.4)
        return super().stream(*a, **k)


def user_bubbles(w):
    return [e for e in w._log if e.get("kind") == "user"]


# ═══════════════════════════════════════════════════════════════════════════
print("\n--- steer mid-task ---")
ws = folder("steer")
w = agent([], ws)
w.backend = Slow(["```bash\nsleep 0.6; echo first-step\n```", "Switched to port 9000."])
w.entry.get_buffer().set_text("start the server")
w.on_send()
time.sleep(0.2)
w.entry.get_buffer().set_text("use port 9000 instead")
w.on_send()                                      # while it works
check(w._steer == ["use port 9000 instead"] and w._get_entry() == "",
      "typing while it works queues the message and clears the composer")
settle()
tool_msgs = [m["content"] for m in w.history if m["role"] == "user" and "TOOL RESULTS" in str(m["content"])]
check(tool_msgs and "MESSAGE FROM THE USER" in tool_msgs[0] and "port 9000" in tool_msgs[0],
      "it reaches the agent with the next step's results", tool_msgs[:1])
check(not w._steer, "and leaves the queue")
check(len(w.backend.calls) == 2, "no extra turn was started for it", len(w.backend.calls))
check_finished(w, "steer delivered")
check_history(w, "steer delivered")

w = agent([], ws)
w.backend = Slow(["Done quickly.", "Okay, port 9000."])
w.entry.get_buffer().set_text("quick question")
w.on_send()
time.sleep(0.1)
w.entry.get_buffer().set_text("and use port 9000")
w.on_send()
settle()
users = [m["content"] for m in w.history if m["role"] == "user"]
check(users[-1].endswith("and use port 9000") and "Okay, port 9000." in hist_text(w),
      "a message queued after the last step becomes the next turn", users)
check(len(w.backend.calls) == 2, "exactly one more turn", len(w.backend.calls))
check(len(user_bubbles(w)) == 2, "and the queued message is shown once, not twice",
      [e["data"].get("text") for e in user_bubbles(w)])
check_finished(w, "steer as next turn")

w = agent([], ws)
w.backend = Slow(["```bash\nsleep 2\n```", "never"])
w.entry.get_buffer().set_text("slow thing")
w.on_send()
time.sleep(0.1)
w.entry.get_buffer().set_text("actually stop")
w.on_send()
w.stop_run()
settle()
check(w._get_entry() == "actually stop" and not w._steer,
      "Stop puts an undelivered message back in the composer")
check_finished(w, "steer stop")

w = agent(["Hi."], ws)
type_and_send(w, "normal message")
check(len([m for m in w.history if m["role"] == "user"]) == 1, "an idle send is unchanged")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- plan mode: propose, approve, then act ---")
ws = folder("planmode")
(ws / "a.py").write_text("print('a')\n")
w = agent(["Looking first.\n```read a.py```",
           "```write b.py\nprint(1)\n```\n```bash\necho SHOULD_NOT_RUN\n```",
           "```plan\n- [ ] write b.py\n- [ ] run it\n```\nApproach: a one-line script, then run it."],
          ws)
w.set_plan_mode(True)
type_and_send(w, "add b.py and run it")
h = hist_text(w)
check("print('a')" in h, "read-only tools still work in plan mode")
check(not (ws / "b.py").exists() and "SHOULD_NOT_RUN\n" not in h.split("echo SHOULD_NOT_RUN")[-1],
      "nothing is written or run before approval")
check("PLAN MODE: not run" in h, "the model is told why")
pe = w._plan_entry
check(pe and pe["data"].get("approval") == "pending", "the proposed plan waits for approval")
check(len(w.backend.calls) == 3 and "unfinished step(s)" not in h,
      "the turn ends on the plan, with no nudge to act", len(w.backend.calls))
check_finished(w, "plan proposed")
w.backend.replies = ["```write b.py\nprint(1)\n```\n```bash\npython3 b.py\n```",
                     "```plan\n- [x] write b.py\n- [x] run it\n```\nDone."]
approve = [x for x in w.msgbox.walk() if x.get_label() == "Approve & run"]
check(approve, "the plan card has Approve & run")
if approve:
    approve[0].click()
    settle()
check((ws / "b.py").exists() and "RAN that bash" in hist_text(w), "approving runs the plan")
check(not w.plan_mode, "and turns plan mode off")
check(w._plan_entry is pe and all(st == "x" for st, _ in pe["data"]["items"]),
      "the run ticks off the SAME plan card", pe["data"]["items"])
check(pe["data"]["approval"] == "approved", "which is marked approved")
check_finished(w, "plan approved")

w = agent(["```plan\n- [ ] something risky\n```\nThat's the plan."], ws)
w.set_plan_mode(True)
type_and_send(w, "plan something")
rev = [x for x in w.msgbox.walk() if x.get_label() == "Revise"]
rev[0].click()
check(w._plan_entry["data"]["approval"] == "revise" and w.plan_mode,
      "Revise keeps plan mode on and drops the buttons")
w.entry.get_buffer().set_text("/plan")
w.on_send()
check(not w.plan_mode, "/plan toggles it")
w.set_agent_mode(False, announce=False)
w.entry.get_buffer().set_text("/plan")
w.on_send()
check(w.agent_mode and w.plan_mode, "/plan in Chat mode switches to Agent with planning on")

# ═══════════════════════════════════════════════════════════════════════════
print(f"\nTOTAL V16 FAILURES: {fails + len(FAILS)}")
assert fails == 0 and not FAILS, "v16 regressions present"
print("ALL V16 TESTS PASSED")
