import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""test_sim.py — end-to-end chain simulation.

Drives complete conversations through the REAL ChuckWindow with a scripted
backend and mocked network. Asserts the whole chain each time: history stays
well-formed and serialisable, tools dispatch and feed back, destructive things
stay gated, and every run terminates with the button restored to Send.
"""
import tempfile, json, time, threading, os
from pathlib import Path
from sim import (cn, new_win, type_and_send, settle, check_history, check_finished,
                 cards, fail, FAILS, ScriptedBackend)
import gtkstub

tmp = Path(tempfile.mkdtemp())
cn.web_search = lambda q, n=3: [("A", "https://a.com/1", "s"), ("B", "https://b.com/2", "s")]
cn.web_fetch = lambda u, *a, **k: f"BODY {u}"


def head(t): print(f"\n--- {t} ---")


head("1 plain answer, no tools")
w = new_win(["Check `dmesg -T | tail -50`."], tmp)
type_and_send(w, "why does it panic")
check_history(w, "S1"); check_finished(w, "S1")
if len(w.backend.calls) != 1: fail("S1: expected 1 model call")
print("  1 call, run ended, history clean")

head("2 research chain: search -> fetch -> feed back -> answer")
w = new_win(["```search\nkernel\n```", "Latest is 6.11."], tmp)
type_and_send(w, "latest kernel?")
tr = [m for m in w.history if isinstance(m.get("content"), str)
      and m["content"].startswith("TOOL RESULTS")]
if len(w.backend.calls) != 2: fail(f"S2: expected 2 calls, got {len(w.backend.calls)}")
if not tr or "BODY" not in tr[0]["content"]: fail("S2: fetched bodies never reached the model")
check_history(w, "S2"); check_finished(w, "S2")
print("  2 calls, real page bodies fed back, final answer produced")

head("3 clean code -> verify -> RUN -> feed the real result back")
w = new_win(["```python\ndef add(a,b):\n    return a+b\nprint(add(2,3))\n```", "It printed 5."], tmp)
type_and_send(w, "adder please")
_h = " ".join(str(m.get("content", "")) for m in w.history)
if "SUCCEEDED" not in _h:
    fail("S3: clean code did not auto-execute")
elif "5" not in _h:
    fail("S3: the real output never came back")
else:
    print("  code ran by itself, produced 5, and the result reached the model")
check_history(w, "S3"); check_finished(w, "S3")

head("4 broken code withheld -> model fixes -> card appears")
w = new_win(["```python\ndef broken(:\n    return 1\n```",
             "```python\ndef fixed(x):\n    return x+1\nprint(fixed(1))\n```", "works"], tmp)
type_and_send(w, "write a function")
vf = [m for m in w.history if isinstance(m.get("content"), str) and "VERIFICATION FAILED" in m["content"]]
if not vf: fail("S4: broken code did not trigger a fix request")
if len(cards(w)) != 1: fail(f"S4: expected 1 card (the fixed one), got {len(cards(w))}")
check_history(w, "S4"); check_finished(w, "S4")
print("  broken withheld, fix requested, only the good code got a button")

head("5 destructive command -> CRITICAL gate")
w = new_win(["```bash\nrm -rf / --no-preserve-root\n```", "ok"], tmp)
type_and_send(w, "wipe it")
cl = cards(w)
if not cl: fail("S5: no card")
else:
    rb = [x for x in cl[0].walk() if x.get_label() == "Run"]
    crit = [x for x in cl[0].walk() if "critical" in getattr(x, "classes", set())]
    if not crit: fail("S5: no CRITICAL banner")
    if rb and rb[0].get_sensitive(): fail("S5: Run left armed on a disk wipe")
    before = len(w.history)
    if rb: rb[0].click(); settle()
    if [m for m in w.history if isinstance(m.get("content"), str) and m["content"].startswith("I ran")]:
        fail("S5: disarmed button still executed")
    print("  CRITICAL banner shown, Run disarmed, click does nothing")
check_finished(w, "S5")

head("6 runaway model: never stops asking to search -> must terminate")
w = new_win(["```search\nq\n```"] * 40, tmp)
type_and_send(w, "go")
if len(w.backend.calls) > cn.MAX_TOOL_HOPS + 2:
    fail(f"S6: ran away — {len(w.backend.calls)} calls")
check_finished(w, "S6")
print(f"  capped at {len(w.backend.calls)} calls (MAX_TOOL_HOPS={cn.MAX_TOOL_HOPS})")

head("7 misbehaving model output")
for name, reply in [("empty", ""), ("whitespace", "   \n "),
                    ("unclosed fence", "```python\nprint(1"),
                    ("unknown tag", "```wibble\nx\n```"),
                    ("huge", "A" * 200000),
                    ("nested fences", "```bash\necho '```'\n```")]:
    w = new_win([reply, "ok"], tmp)
    try:
        type_and_send(w, "t")
        check_history(w, f"S7-{name}"); check_finished(w, f"S7-{name}")
    except Exception as e:
        fail(f"S7 {name}: crashed {e}")
print("  all 6 malformed replies survived without hanging")

head("8 stop mid-run restores the button")
class Slow(ScriptedBackend):
    def stream(self, messages, on_delta, on_done, on_error, vision=False, should_stop=None):
        self.calls.append(len(messages))
        for _ in range(200):
            if should_stop and should_stop(): return
            on_delta("word "); time.sleep(0.004)
        on_done()
w = new_win([], tmp); w.backend = Slow([])
w.entry.get_buffer().set_text("long")
th = threading.Thread(target=w.on_send); th.start(); time.sleep(0.2)
if not w._running: fail("S8: never entered running state")
w.stop_run(); settle(); th.join(timeout=5)
if w._running: fail("S8: still running after stop")
if w.send_btn.get_icon_name() != "go-up-symbolic": fail("S8: button not restored")
print("  stopped cleanly, button back to Send")

head("9 payload stays bounded over a long research conversation")
cn.web_fetch = lambda u, *a, **k: "X" * 8000
reps = []
for i in range(60): reps += [f"```search\nt{i}\n```", f"A{i}."]
w = new_win(reps, tmp)
sizes = []
for i in range(25):
    type_and_send(w, f"q{i}")
    sizes.append(len(w.backend.payloads[-1]))
if sizes[-1] > 200_000: fail(f"S9: payload unbounded ({sizes[-1]} chars)")
last = json.loads(w.backend.payloads[-1])["messages"]
if last[0]["role"] != "system": fail("S9: system prompt lost to trimming")
if not any("q24" in str(m.get("content", "")) for m in last): fail("S9: current question trimmed")
blobs = [m for m in last if isinstance(m.get("content"), str)
         and m["content"].startswith("TOOL RESULTS")]
if not blobs: fail("S9: all tool results trimmed — research loop would break")
print(f"  turn1={sizes[0]} turn25={sizes[-1]} chars (plateaus); {len(blobs)} recent blobs kept")
cn.web_fetch = lambda u, *a, **k: f"BODY {u}"

head("10 augmentation is ephemeral (never bloats saved history)")
import chucknorris_ext.memory as mem
mem.FACTS = tmp / "f.jsonl"
if mem.FACTS.exists(): mem.FACTS.unlink()
mem.remember("user's editor is neovim")
w = new_win(["ok"], tmp)
type_and_send(w, "what editor do I use")
sent = json.loads(w.backend.payloads[-1])["messages"]
if not any("neovim" in m["content"] for m in sent if m["role"] == "system"):
    fail("S10: memory not injected into the live prompt")
if any("neovim" in m["content"] for m in w.history if m["role"] == "system"):
    fail("S10: memory leaked into saved history")
print("  injected for the turn, absent from saved history")

head("11 chat persistence round trip + 24h purge")
w = new_win(["one", "two"], tmp)
type_and_send(w, "first question")
w._save_chat()
files = cn.chat_files()
if not files: fail("S11: chat not saved")
w.new_chat()
if len(w.history) != 1: fail("S11: new_chat did not reset")
w._load_chat(files[0])
if not any("first question" in str(m.get("content", "")) for m in w.history):
    fail("S11: reload lost the conversation")
old = cn.CHATS_DIR / "20260101-000000.json"
old.write_text(json.dumps({"title": "old", "ts": "20260101-000000", "history": []}))
t = time.time() - 30 * 3600; os.utime(old, (t, t))
w._sweep_chats()
if old.exists(): fail("S11: 24h purge did not remove an expired chat")
print("  saved, reloaded, and expired chat purged")

head("12 heartbeat + watchdog")
w = new_win(["x"], tmp); w._start_run()
w._run_started = time.time() - 3; w._last_progress = time.time()
gtkstub.fire_timers(1)
if "working" not in w.live.get_text(): fail("S12: heartbeat not ticking")
w._last_progress = time.time() - (w.STUCK_AFTER + 5)
gtkstub.fire_timers(1); settle()
if w._running: fail("S12: watchdog did not stop a stalled run")
w2 = new_win(["x"], tmp); w2._start_run()
w2._run_started = time.time() - (w2.RUN_HARD_CAP + 5); w2._last_progress = time.time()
gtkstub.fire_timers(1); settle()
if w2._running: fail("S12: hard cap did not fire")
print("  heartbeat ticks, watchdog and hard cap both stop the run")

print()
print("UI-thread exceptions:", gtkstub.GLibNS.errors[:2] or "none")
print("TOTAL SIMULATION FAILURES:", len(FAILS))
for f in FAILS: print("  ", f)
assert not FAILS, "end-to-end simulation failures"
print("ALL END-TO-END SIMULATIONS PASSED")
