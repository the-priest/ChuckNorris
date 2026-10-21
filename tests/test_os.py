import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""Chuck OS (docs/OS.md, roadmap phases 16-21): the agent kernel on its own,
then wired into the real window on the stub toolkit."""
import re
import threading
import time
from shutil import which as shutil_which

from chucknorris_ext import kernel as K

fails = 0


def check(cond, label, detail=""):
    global fails
    if cond:
        print(f"  [OK]   {label}")
    else:
        fails += 1
        print(f"  [FAIL] {label}  {str(detail)[:300]}")


# ═══════════════════════════════════════════════════════════════════════════
print("\n--- the kernel: processes, quotas, accounting ---")
k = K.Kernel(slots=2)
root = k.spawn("chuck", kind="chat", quota={"tokens": 10000})
child = k.spawn("survey", parent=root.pid, quota={"tokens": 50000})
check(child.quota["tokens"] == 10000, "a child's quota is clipped to what its parent has left",
      child.quota)
grand = k.spawn("deeper", parent=child.pid)
check(grand.quota["tokens"] == 10000, "and a grandchild inherits the limit")
k.charge(grand.pid, prompt=1000, completion=200, usd=0.01)
check(grand.tokens() == 1200 and child.tokens() == 1200 and root.tokens() == 1200
      and abs(root.total["usd"] - 0.01) < 1e-9 and root.own["calls"] == 0,
      "a call is charged to the process and every ancestor")
child2 = k.spawn("later", parent=root.pid)
check(child2.quota["tokens"] == 8800, "a later child gets only what's left")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- the scheduler ---")
k = K.Kernel(slots=1)
holder = k.spawn("holder")
k.acquire(holder.pid)
order = []
procs = [k.spawn(n, nice=nv) for n, nv in (("low", 5), ("normal", 0), ("high", -5))]


def ask(p):
    k.acquire(p.pid)
    order.append(p.name)
    time.sleep(0.05)
    k.release(p.pid)


ths = []
for p in procs:
    t = threading.Thread(target=ask, args=(p,), name="srv-ask")
    t.start()
    ths.append(t)
    time.sleep(0.05)
k.release(holder.pid)
for t in ths:
    t.join(5)
check(order == ["high", "normal", "low"], "slots go to the highest priority first", order)

k = K.Kernel(slots=1)
a, b = k.spawn("a"), k.spawn("b")
k.signal(b.pid, K.STOP)
got = []
t = threading.Thread(target=lambda: (k.acquire(b.pid), got.append("b")), name="srv-b")
t.start()
time.sleep(0.4)
check(not got and b.state == K.STOPPED, "a stopped process never gets a slot", b.state)
k.signal(b.pid, K.CONT)
t.join(3)
check(got == ["b"], "CONT lets it run again")
k.release(b.pid)

k = K.Kernel(slots=1)
p = k.spawn("busy")
k.acquire(p.pid)
k.signal(p.pid, K.STOP)
check(p.state == K.RUNNING, "STOP lets a call in flight finish")
k.release(p.pid)
check(p.state == K.STOPPED, "and parks the process when it does")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- quotas: TERM, one last call, then KILL ---")
k = K.Kernel(slots=2)
p = k.spawn("spender", quota={"tokens": 1000})
k.acquire(p.pid); k.charge(p.pid, 900, 200); k.release(p.pid)
check(k.over_quota(p.pid) == "token quota reached", "over its quota", k.over_quota(p.pid))
ok = k.acquire(p.pid)
check(ok and k.terminating(p.pid), "it gets one more call, flagged TERM, to write its report")
k.release(p.pid)
try:
    k.acquire(p.pid)
    killed = False
except K.ProcessKilled:
    killed = True
check(killed and p.state == K.KILLED, "and is killed if it asks for another")

k = K.Kernel(slots=1)
h = k.spawn("h")
k.acquire(h.pid)
w = k.spawn("waiter")
res = []


def wait_slot():
    try:
        k.acquire(w.pid)
        res.append("granted")
    except K.ProcessKilled:
        res.append("killed")
t = threading.Thread(target=wait_slot, name="srv-w")
t.start()
time.sleep(0.3)
k.signal(w.pid, K.KILL)
t.join(3)
check(res == ["killed"], "KILL wakes a process waiting for a slot and stops it", res)

k = K.Kernel()
par = k.spawn("parent")
kids = [k.spawn(f"kid{i}", parent=par.pid) for i in range(3)]
k.signal(par.pid, K.KILL)
check(all(c.state == K.KILLED for c in kids), "KILL takes the children with it")
info = k.signal(kids[0].pid, K.INFO)
check(info["state"] == "killed" and info["name"] == "kid0", "INFO answers without a model call")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- IPC, wait, the journal ---")
k = K.Kernel()
a, b = k.spawn("a"), k.spawn("b")
k.send(b.pid, "found the bug in auth.py:41", sender=a.pid)
m = k.recv(b.pid)
check(m and m[0] == a.pid and "auth.py:41" in m[1] and k.recv(b.pid) is None,
      "mailboxes deliver once, in order")
k.post("api-base", "https://api.example.com", by=a.pid)
check(k.read("api-base") == "https://api.example.com", "the blackboard is shared")
threading.Timer(0.2, lambda: k.exit(b.pid, 0, "REPORT")).start()
check(k.wait(b.pid, timeout=3) == (0, "REPORT"), "wait() returns the exit status and result")
check(any(e[2] == "exit" for e in k.events(b.pid)), "and the journal recorded it")
check([r["pid"] for r in k.ps()] == [a.pid], "ps lists the live processes")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- in the app: the chat is PID 1, sub-agents are processes ---")
import tempfile
from pathlib import Path
from sim import cn, new_win, type_and_send, settle, ScriptedBackend, FAILS

tmp = Path(tempfile.mkdtemp(prefix="chuck_os_"))
ws = tmp / "proj"
ws.mkdir()
(ws / "app.py").write_text("print('hello')\n")


class SubBackend(ScriptedBackend):
    """Main replies come from the script; sub-agents answer by their brief:
    'loop' never stops calling tools (a runaway), others report at once."""
    def stream(self, messages, on_delta, on_done, on_error, vision=False,
               should_stop=None, attempts=3, on_open=None):
        sysmsg = messages[0]["content"] if messages else ""
        if sysmsg.startswith("You are a sub-agent"):
            brief = messages[1]["content"]
            if "out of budget" in str(messages[-1].get("content")):
                on_delta("REPORT (partial): stopped at the budget")
            elif brief.startswith("loop"):
                on_delta("```grep x\n```\n" + "padding " * 400)
            elif brief.startswith("slow"):
                time.sleep(1.5)
                on_delta("REPORT: slow one done")
            else:
                on_delta(f"REPORT: {brief} found in app.py")
            self.last_usage = {"prompt": 1000, "completion": 200, "cached": 0, "reasoning": 0}
            on_done()
            return
        super().stream(messages, on_delta, on_done, on_error, vision, should_stop,
                       attempts, on_open)


w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.set_workspace(ws, announce=False)
check(w.kernel is not None and w.kernel.procs[w._pid1].name == "chuck", "the chat is PID 1")
w.backend = SubBackend(["```task\nfind hello\n```\n```task\nfind print\n```", "Both reported."])
type_and_send(w, "survey in parallel")
subs = [r for r in w.kernel.ps(all=True) if r["kind"] == "subagent"]
check(len(subs) == 2 and all(r["state"] == "done" for r in subs) and all(r["tokens"] >= 1200 for r in subs),
      "each sub-agent ran as its own process, charged for its calls", subs)
root = w.kernel.procs[w._pid1]
check(root.tokens() >= sum(r["tokens"] for r in subs) and root.own["calls"] >= 1,
      "PID 1's total includes its children's tokens and its own calls")
w.settings["subagent_tokens"] = 5000
w.backend = SubBackend(["```task\nloop forever\n```", "ok"])
type_and_send(w, "runaway")
r = [x for x in w.kernel.ps(all=True) if x["name"].startswith("loop")][-1]
h = " ".join(str(m.get("content")) for m in w.history)
check(r["state"] in ("done", "failed", "killed") and "REPORT (partial)" in h,
      "a runaway sub-agent hits its quota, is asked to wrap up, and reports what it has", r)
w.settings["subagent_tokens"] = 250000
w.entry.get_buffer().set_text("/top")
w.on_send()
texts = [x.get_text() for x in w.msgbox.walk() if isinstance(x.get_text(), str)]
check("TOP" in texts and "PID" in texts and "chuck" in texts and any("model slots" in t for t in texts),
      "/top shows the process table", [t for t in texts if "slots" in t])
w.backend = SubBackend(["```task\nslow one\n```\n```task\nslow two\n```", "ok"])
w.entry.get_buffer().set_text("two slow ones")
w.on_send()
time.sleep(0.5)
live = [x for x in w.kernel.ps() if x["kind"] == "subagent"]
check(len(live) == 2, "while they run, both are live processes", live)
check(w.process_command("kill", str(live[0]["pid"])) and w.kernel.procs[live[0]["pid"]].state == "killed",
      "/kill ends one sub-agent")
w.stop_run()
time.sleep(0.3)
check(all(x["state"] != "running" for x in w.kernel.ps() if x["kind"] == "subagent"),
      "and Stop ends the rest of the turn's sub-agents")
settle()
check(w.process_command("kill", str(w._pid1)) is False, "PID 1 can't be killed from /kill (Esc stops a turn)")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- background agents: copy-on-write workspaces, fork and join ---")
from chucknorris_ext import cow as C
src = tmp / "cowproj"
src.mkdir()
(src / "a.py").write_text("one\ntwo\nthree\nfour\nfive\n")
(src / "c.py").write_text("c1\nc2\nc3\n")
(src / "del.py").write_text("bye\n")
(src / "node_modules").mkdir()
(src / "node_modules" / "big.js").write_text("x")
ws_ = C.Workspace.create(src, tmp / "wt", "t1")
check(not (ws_.work / "node_modules").exists() and (ws_.work / "a.py").exists(),
      "a workspace copies the project, not its dependencies")
(ws_.work / "a.py").write_text("ONE\ntwo\nthree\nfour\nfive\n")
(ws_.work / "c.py").write_text("c1\nAGENT\nc3\n")
(ws_.work / "new.py").write_text("new\n")
(ws_.work / "del.py").unlink()
(src / "a.py").write_text("one\ntwo\nthree\nfour\nFIVE\n")      # you, meanwhile
(src / "c.py").write_text("c1\nYOURS\nc3\n")
check(ws_.changes() == {"a.py": "modified", "c.py": "modified", "del.py": "deleted", "new.py": "added"},
      "it knows exactly what the agent changed", ws_.changes())
r_ = ws_.merge()
check((src / "a.py").read_text() == "ONE\ntwo\nthree\nfour\nFIVE\n" and r_["merged"] == ["a.py"],
      "your edit and the agent's to different lines of one file are merged three-way")
check((src / "c.py").read_text() == "c1\nYOURS\nc3\n" and r_["conflicts"] == ["c.py"]
      and (src / ".chuck-conflicts" / "c.py").read_text() == "c1\nAGENT\nc3\n",
      "overlapping edits are never forced: yours stays, the agent's is set aside")
check((src / "new.py").exists() and not (src / "del.py").exists(), "new and deleted files carry over")


class BgBackend(SubBackend):
    """Background agents act by their brief."""
    def stream(self, messages, on_delta, on_done, on_error, vision=False,
               should_stop=None, attempts=3, on_open=None):
        sysmsg = messages[0]["content"] if messages else ""
        if sysmsg.startswith("You are a background agent"):
            brief = messages[1]["content"]
            n = sum(1 for m in messages if m["role"] == "assistant")
            tag = brief.split()[0].rstrip(":")
            if n == 0:
                on_delta(f"Writing it.\n```write {tag}.txt\nmade by {tag}\n```\n"
                         f"```bash\ncat {tag}.txt && pwd && touch /tmp/escape-{tag}\n```")
            elif n == 1 and "slow" in brief:
                time.sleep(1.0)
                on_delta(f"```bash\necho still working\n```")
            else:
                on_delta(f"REPORT: wrote {tag}.txt and checked it with cat.")
            self.last_usage = {"prompt": 800, "completion": 100, "cached": 0, "reasoning": 0}
            on_done()
            return
        super().stream(messages, on_delta, on_done, on_error, vision, should_stop,
                       attempts, on_open)


proj = tmp / "bgproj"
proj.mkdir()
(proj / "app.py").write_text("print('hi')\n")
w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.set_workspace(proj, announce=False)
w.backend = BgBackend(["```spawn\nalpha: add a file\n```\nStarted it.", "ok"])
type_and_send(w, "do it in the background")
bgs = [r for r in w.kernel.ps(all=True) if r["kind"] == "background"]
check(len(bgs) == 1, "```spawn``` starts a background agent process", bgs)
pid = bgs[0]["pid"]
check(w.kernel.wait(pid, timeout=20) is not None, "it runs to the end on its own")
settle()
e = w._bg[pid]
check(not (proj / "alpha.txt").exists() and e["changes"] == {"alpha.txt": "added"},
      "its work stays in its own copy until merged", e["changes"])
cat_out = " ".join(str(m.get("content")) for m in w.history)
check("made by alpha" in str(e["report"]) or "wrote alpha.txt" in str(e["report"]),
      "and it reports back", e["report"])
check(any("background agent" in n for n in w._user_notes) or "background agent" in cat_out,
      "the main agent is told it finished")
texts = [x.get_text() for x in w.msgbox.walk() if isinstance(x.get_text(), str)]
check(any(t.startswith("BACKGROUND AGENT") for t in texts) and "Merge into folder" in
      [x.get_label() for x in w.msgbox.walk() if isinstance(x, cn.Gtk.Button)],
      "its card offers the diff and a merge")
check(w.show_bg_diff(pid) and any("+made by alpha" in str(x.get_text()) for x in w.msgbox.walk()
                                   if isinstance(x.get_text(), str)),
      "the diff shows exactly what it would change")
msg = w.join_background(pid)
check((proj / "alpha.txt").read_text() == "made by alpha\n" and "merged" in msg,
      "merging brings its work into the folder", msg)
check(w._bg[pid]["status"] == "merged" and not w._bg[pid]["ws"].dir.exists(),
      "and its copy is cleaned up")
check(w.join_background(pid) == "nothing to merge", "a second merge does nothing")
if shutil_which("bwrap"):
    check(not Path("/tmp/escape-alpha").exists(), "its commands are jailed to its copy (no writes outside)")
p1 = w.spawn_background("beta: one file\nslow")
p2 = w.spawn_background("gamma: another file")
check(p1 and p2 and len([r for r in w.kernel.ps() if r["kind"] == "background"]) >= 1,
      "two background agents can run at once")
w.kernel.wait(p1, 20); w.kernel.wait(p2, 20)
settle()
w.join_background(p1); w.join_background(p2)
check((proj / "beta.txt").exists() and (proj / "gamma.txt").exists(),
      "and both merge: two writers, no trampling")
p3 = w.spawn_background("delta: will be stopped\nslow")
time.sleep(0.3)
w.process_command("kill", str(p3))
w.kernel.wait(p3, 10)
settle()
check(w._bg[p3]["status"] == "finished" and not (proj / "delta.txt").exists(),
      "a killed background agent leaves the folder alone")
check(w.discard_background(p3) and w._bg[p3]["status"] == "discarded", "and its work can be discarded")
w.set_access("readonly")
check(w.spawn_background("eps: nope") is None, "no background agent in a read-only folder")
w.set_access("full")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- virtual memory: tool output paged out, paged back in exactly ---")
w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
big = lambda i: (f"TOOL RESULTS (continue):\n\n[read src/mod{i}.py]\n" + "\n".join(
    f"line {n} of module {i}" for n in range(400)) + (f"\nERROR: test_{i} failed at line 41" if i == 3 else ""))
w.history = [w.history[0], {"role": "user", "content": "fix the build"}]
for i in range(8):
    w.history += [{"role": "assistant", "content": f"```read src/mod{i}.py```"},
                  {"role": "user", "content": big(i)}]
before = [m["content"] for m in w.history]
sent = w._trim_for_send(list(w.history))
paged = [m["content"] for m in sent if str(m.get("content")).startswith("[PAGED OUT")]
inline = [m["content"] for m in sent if str(m.get("content")).startswith("TOOL RESULTS")]
check(len(paged) == 5 and len(inline) == 3, "older tool output is paged out; the newest three stay inline",
      (len(paged), len(inline)))
check([m["content"] for m in w.history] == before, "history keeps every byte (only what's sent changes)")
p3 = next(x for x in paged if "mod3" in x)
check("ERROR: test_3 failed" in p3 and "read src/mod3.py" in p3,
      "a page-table entry carries the gist: the tool and the error lines", p3[:300])
pid3 = int(re.search(r"p(\d+)", p3).group(1))
got = w._t_page(f"p{pid3}")
check(got.split("\n", 1)[1] == w.history[pid3]["content"], "```page``` brings it back exactly")
got = w._t_page(f"p{pid3} 4-6")
check("4: line 0 of module 3" in got and "6: line 2 of module 3" in got and "line 3 of" not in got,
      "a line range, numbered", got[:200])
got = w._t_page(f"p{pid3} /ERROR/")
check("1 matching line" in got and "test_3 failed" in got, "or a search inside it")
check("no such page" in w._t_page("p9999"), "a missing page says so")
w2 = new_win([], tmp)
w2.history = [w2.history[0], {"role": "user", "content": "hi"},
              {"role": "assistant", "content": "```read a```"}, {"role": "user", "content": big(0)},
              {"role": "assistant", "content": "ok"}]
check(not any(str(m.get("content")).startswith("[PAGED") for m in w2._trim_for_send(list(w2.history))),
      "a small conversation isn't paged at all")
w._compact = {"upto": 12, "text": "Summary: read modules 0-4.", "anchor": cn._msg_key(w.history[11])}
sent = w._trim_for_send(list(w.history))
summ = next(m["content"] for m in sent if str(m.get("content")).startswith("CONVERSATION SUMMARY"))
check("PAGE TABLE" in summ and f"p{pid3}" in summ,
      "after compaction the summary carries a page table: old output is still one call away")
w._compact = None
w.backend = ScriptedBackend([f"```page p{pid3} /ERROR/```", "Found it: test_3 fails at line 41."])
type_and_send(w, "what failed earlier?")
check("test_3 failed at line 41" in " ".join(str(m.get("content")) for m in w.history[-3:]),
      "the agent pages it in with a tool call")

print(f"\nTOTAL OS FAILURES: {fails}")
assert fails == 0 and not FAILS, "OS regressions present"
print("ALL OS TESTS PASSED")
