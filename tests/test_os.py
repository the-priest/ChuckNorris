import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""Chuck OS (docs/OS.md, roadmap phases 16-21): the agent kernel on its own,
then wired into the real window on the stub toolkit."""
import re
import shutil
import sys
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
                on_delta("```bash\necho still working\n```")
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

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- daemons: triggers checked for free, an agent woken only when one fires ---")
from chucknorris_ext import daemons as D
pw = D.parse_when
check(pw("every 30m") == {"every": 1800} and pw("every 2 hours") == {"every": 7200}
      and pw("every day") == {"every": 86400}, "intervals", pw("every 2 hours"))
check(pw("daily 08:30") == {"daily": "08:30"} and pw("every day at 7pm") == {"daily": "19:00"}
      and pw("daily at 12am") == {"daily": "00:00"}, "a time of day, 24h or am/pm")
check(pw("when a service fails") == {"unit_failed": True}
      and pw("when disk / is above 90%") == {"disk": "/", "above": 90}
      and pw("when `checkupdates` changes") == {"command": "checkupdates", "changes": True}
      and pw("when /etc/fstab changes") == {"watch": "/etc/fstab"}, "events")
for bad in ("every 10s", "daily 25:00", "sometime", ""):
    try:
        pw(bad)
        check(False, f"{bad!r} is refused")
    except D.DaemonError as ex:
        check("say when" in str(ex) or "minute" in str(ex) or "exist" in str(ex),
              f"{bad!r} is refused with a hint", ex)


class FakeProbe:
    def __init__(self):
        self.sigs, self.units, self.disk, self.out, self.ran = {}, [], 50.0, "", []

    def mtime(self, p):
        return self.sigs.get(p, "s0")

    def failed_units(self):
        return list(self.units)

    def disk_percent(self, p):
        return self.disk

    def run(self, cmd):
        self.ran.append(cmd)
        return self.out

    localtime = staticmethod(time.localtime)


pr = FakeProbe()
spec = {"when": {"every": 3600}, "last_run": 1000}
check(D.check(spec, 1000 + 3599, pr)[0] is False and D.check(spec, 1000 + 3600, pr)[0] is True,
      "an interval fires when it has passed")
midnight = time.mktime((2026, 1, 2, 0, 0, 0, 0, 0, -1))
day = {"when": {"daily": "08:30"}, "last_run": midnight - 86400}
check(D.check(day, midnight + 8 * 3600 + 29 * 60, pr)[0] is False
      and D.check(day, midnight + 8 * 3600 + 31 * 60, pr)[0] is True,
      "a daily daemon fires once the time has come")
day["last_run"] = midnight + 8 * 3600 + 31 * 60
check(D.check(day, midnight + 20 * 3600, pr)[0] is False, "and not again the same day")
check(D.check({"when": {"daily": "08:30"}, "last_run": midnight - 86400}, midnight + 15 * 3600, pr)[0] is True,
      "a run missed while the computer was off happens when it's back")
w_ = {"when": {"watch": "/x"}, "state": {}}
due, _, st = D.check(w_, 0, pr)
check(due is False and st["sig"] == "s0", "a watch records the first state without firing")
w_["state"] = st
check(D.check(w_, 0, pr)[0] is False, "nothing changed: nothing fires")
pr.sigs["/x"] = "s1"
check(D.check(w_, 0, pr)[0] is True, "a change fires it")
u = {"when": {"unit_failed": True}, "state": {}}
_, _, u["state"] = D.check(u, 0, pr)
pr.units = ["nginx.service"]
due, why, u["state"] = D.check(u, 0, pr)
check(due and "nginx.service" in why, "a newly failed unit fires, naming it", why)
check(D.check(u, 0, pr)[0] is False, "the same failure doesn't fire twice")
dk = {"when": {"disk": "/", "above": 90}, "state": {}, "last_run": 0}
pr.disk = 95.0
due, why, dk["state"] = D.check(dk, 100, pr)
check(due and "95%" in why, "the disk passing its threshold fires", why)
dk["last_run"] = 100
check(D.check(dk, 200, pr)[0] is False and D.check(dk, 100 + 6 * 3600, pr)[0] is True,
      "and repeats only every 6 hours while it stays full")
cm = {"when": {"command": "checkupdates", "changes": True}, "state": {}}
pr.out = "linux 6.1 -> 6.2"
_, _, cm["state"] = D.check(cm, 1000, pr)
pr.out = "linux 6.1 -> 6.3"
pr.ran.clear()
check(D.check(cm, 1100, pr)[0] is False and not pr.ran,
      "a trigger's command isn't run at every tick (checkupdates asks the mirrors)")
due, _, cm["state"] = D.check(cm, 1300, pr)
check(due is True, "a command's output changing fires it")
check("6.3" in D.trigger_detail(cm), "and the woken agent is told what it printed")
rop = D.read_only_problem
check(not rop("journalctl -u nginx -n 50 --no-pager") and not rop("systemctl status nginx")
      and not rop("df -h; du -sh ~/x | sort -h") and not rop("git log -5") and not rop("pacman -Qu"),
      "looking is allowed")
check(all(rop(c) for c in ("systemctl restart nginx", "sudo journalctl", "git push", "pacman -Syu",
                           "find . -delete", "busctl call org.freedesktop.login1 / x PowerOff",
                           "rm x", "xargs rm", "loginctl poweroff", "dbus-send --system x")),
      "changing anything is not")
check(rop("ip -s link", strict=True) == "" and rop("echo x > f", strict=True) and rop("echo $(id)", strict=True)
      and rop("curl x") and rop("vmtouch x", strict=True) and not rop("vmtouch x"),
      "without a sandbox only read-only-by-nature commands run (and never curl: there's ```fetch```)")
reg = D.Registry(tmp / "dm.json")
d1 = reg.add("", "every 1h", "look at things")
check(D.Registry(tmp / "dm.json").get(d1["id"])["brief"] == "look at things", "daemons persist")
ib = D.Inbox(tmp / "ib.json", keep=3)
for i in range(5):
    ib.add("d", "why", f"r{i}")
check([i["report"] for i in D.Inbox(tmp / "ib.json").items] == ["r2", "r3", "r4"],
      "the inbox keeps the newest reports")


class DaemonBackend(BgBackend):
    def stream(self, messages, on_delta, on_done, on_error, vision=False,
               should_stop=None, attempts=3, on_open=None):
        sysmsg = messages[0]["content"] if messages else ""
        if sysmsg.startswith("You are a daemon"):
            self.seen.append(messages[1]["content"])
            n = sum(1 for m in messages if m["role"] == "assistant")
            brief = messages[1]["content"]
            if brief.startswith("quiet"):
                on_delta("Nothing to do: all services are fine.")
            elif n == 0:
                on_delta("Looking.\n```bash\nsystemctl restart nginx\n```\n```bash\necho looked-$((1+1))\n```"
                         "\n```write evil.txt\nx\n```")
            else:
                self.results.append(messages[-1]["content"])
                on_delta("nginx failed: port 80 in use.\nPropose: sudo systemctl restart nginx")
            self.last_usage = {"prompt": 500, "completion": 50, "cached": 0, "reasoning": 0}
            on_done()
            return
        super().stream(messages, on_delta, on_done, on_error, vision, should_stop,
                       attempts, on_open)


cn.ChuckWindow.DAEMONS_FILE = tmp / "daemons.json"
cn.ChuckWindow.INBOX_FILE = tmp / "inbox.json"
w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.set_workspace(proj, announce=False)
w.backend = DaemonBackend([])
w.backend.seen, w.backend.results = [], []
probe = FakeProbe()
w._daemon_probe = probe


def slash(t):
    w.entry.get_buffer().set_text(t)
    w.on_send()


def notes():
    return [x.get_text() for x in w.msgbox.walk() if isinstance(x.get_text(), str)]


slash("/daemon when a service fails: find out why and propose a fix")
check(len(w._daemons.items) == 1 and w._daemons.items[0]["when"] == {"unit_failed": True},
      "/daemon <when>: <what> creates one", w._daemons.items)
slash("/daemon whenever: x")
check(len(w._daemons.items) == 1 and any("say when" in t for t in notes()),
      "a trigger it can't read is refused with examples")
slash("/daemon when `rm -rf ~/x` changes: y")
check(len(w._daemons.items) == 1, "a trigger command that changes things is refused")
w._daemon_tick(); settle()
check(not w._daemon_live and w._daemons.items[0]["state"].get("units") == [] and not w.backend.seen,
      "the first check only records the state: no agent, no tokens")
w._daemon_tick(); settle()
check(not w.backend.seen, "nothing failing: still no tokens spent")
probe.units = ["nginx.service"]
w._daemon_tick(); settle()
dps = [r for r in w.kernel.ps(all=True) if r["kind"] == "daemon"]
check(len(dps) == 1 and dps[0]["state"] == "done" and dps[0]["tokens"] > 0,
      "a failed service wakes the daemon as a metered process", dps)
check(w.backend.seen and "nginx.service" in w.backend.seen[0] and "find out why" in w.backend.seen[0],
      "it is told what fired and what it saw", w.backend.seen[:1])
res = w.backend.results[0] if w.backend.results else ""
check("NOT run: systemctl restart changes the system" in res and "looked-2" in res,
      "its commands only look: restarting a service is refused, looking runs", res[:400])
check("not available" in res and not (proj / "evil.txt").exists(), "and it can't write files")
check(len(w._inbox.unread()) == 1 and "port 80" in w._inbox.unread()[0]["report"],
      "its report lands in the inbox")
check(any(t.startswith("\U0001F4EC daemon") and "port 80" in t for t in notes()),
      "and the chat shows its headline")
w._daemon_tick(); settle()
check(len([r for r in w.kernel.ps(all=True) if r["kind"] == "daemon"]) == 1,
      "the same failure doesn't wake it again")
slash("/daemon every 1h: quiet check")
dq = w._daemons.items[-1]
check(dq["last_run"] > 0, "a schedule starts from now, not at once")
dq["last_run"] = 0
w._daemon_tick(); settle()
check(len(w._inbox.items) == 2 and not w._inbox.items[-1]["unread"],
      "a daemon that finds nothing is filed quietly (no notification, no unread)")
slash("/daemons")
t = notes()
check("DAEMONS" in t and any("when a service fails" in x for x in t), "/daemons lists them")
btn = [x for x in w.msgbox.walk() if isinstance(x, cn.Gtk.Button) and x.get_label() == "Pause"]
btn[0].emit("clicked") if btn and hasattr(btn[0], "emit") else w._daemon_action(w._daemons.items[0], "off")
check(not w._daemons.items[0]["enabled"] and btn and btn[0].get_label() == "Resume",
      "Pause turns one off, and the button says Resume", w._daemons.items[0])
n_d = len([r for r in w.kernel.ps(all=True) if r["kind"] == "daemon"])
probe.units = ["nginx.service", "cups.service"]
w._daemon_tick(); settle()
check(len([r for r in w.kernel.ps(all=True) if r["kind"] == "daemon"]) == n_d, "a paused daemon sleeps")
w._daemon_action(w._daemons.items[0], "on")
check(w._daemon_action(w._daemons.items[0], "run"), "Run now wakes it by hand")
settle()
check(len([r for r in w.kernel.ps(all=True) if r["kind"] == "daemon"]) == n_d + 1, "(and it ran)")
slash("/inbox")
check("INBOX" in notes() and not w._inbox.unread(), "/inbox shows the reports and marks them read")
w.act_on_report(w._inbox.items[0])
check("port 80" in w.entry.get_buffer().get_text() and w.agent_mode,
      "Act on it hands the report to the agent, for the user to send")
w.entry.get_buffer().set_text("")
slash("/daemon rm 1")
check(w._daemons.get(1) is None, "/daemon rm deletes one")
w.backend = DaemonBackend(["```daemon\nact daily 08:30: update the dependencies\n```\nProposed.", "ok"])
w.backend.seen, w.backend.results = [], []
n0 = len(w._daemons.items)
type_and_send(w, "update my dependencies every morning")
check("DAEMON · PROPOSED" in notes() and len(w._daemons.items) == n0,
      "a daemon that would CHANGE code is only proposed: nothing exists until the user allows it")
w._pending_daemon[0](True)
check(len(w._daemons.items) == n0 + 1 and w._daemons.items[-1]["when"] == {"daily": "08:30"}
      and w._daemons.items[-1]["kind"] == "act", "Allow creates it")
slash("/daemon act every 1h: zeta: add a file")
da = w._daemons.items[-1]
check(da["kind"] == "act" and da["folder"] == str(proj), "an acting daemon belongs to a folder")
da["last_run"] = 0
w._daemon_tick()
settle()
bg = [r for r in w.kernel.ps(all=True) if r["kind"] == "background"]
check(bg, "it wakes as a background agent in a copy of the folder", bg)
if bg:
    w.kernel.wait(bg[-1]["pid"], 20)
settle()
it = w._inbox.items[-1]
check(it.get("bg_pid") == bg[-1]["pid"] and "/join" in it["report"] and not (proj / "zeta.txt").exists(),
      "its report goes to the inbox with the merge, and the folder is untouched until you merge",
      it)
check(not w._daemon_live, "and the daemon is free to fire again")
w2 = new_win([], tmp)
check(len(w2._daemons.items) == len(w._daemons.items) and w2._inbox.items,
      "daemons and the inbox survive a restart")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- capabilities, programs and the governor ---")
k = K.Kernel()
a = k.spawn("root")
b = k.spawn("reader", parent=a.pid, caps={"read", "net"})
c = k.spawn("wants-more", parent=b.pid, caps={"read", "write", "exec"})
check(a.caps is None and b.caps == {"read", "net"} and c.caps == {"read"},
      "a child's capabilities are clipped to its parent's", (b.caps, c.caps))
check(k.can(c.pid, "read") and not k.can(c.pid, "write") and not k.can(999, "read"),
      "can() answers per process; an unknown pid may do nothing")
k.syscall(c.pid, "read", "src/app.py", 2.5, 1200)
k.syscall(c.pid, "write", ("x.py", "body"), 0, 0, denied=True)
tr = k.strace(c.pid)
check(len(tr) == 2 and tr[0][1:5] == ("read", "src/app.py", 2.5, 1200) and tr[1][2] == "x.py" and tr[1][5],
      "every syscall is logged with its latency and size, denials marked", tr)

from chucknorris_ext import governor as G, programs as PG, pricing as PR
chat = "zai-org/GLM-5.3-Flash"
S = {"governor": "performance"}
check(all(G.pick(S, r, chat, PR.PRICES, PR.SUGGESTED) == chat for r in G.ROLE_NAMES),
      "performance: every call on the chat model")
S = {"governor": "balanced"}
fast = G.fast_model(S, chat, PR.PRICES, PR.SUGGESTED)
check(fast == "deepseek-ai/DeepSeek-V4-Flash", "the fast model is the cheapest suggested one", fast)
check(G.pick(S, "main", chat, PR.PRICES, PR.SUGGESTED) == chat
      and G.pick(S, "background", chat, PR.PRICES, PR.SUGGESTED) == chat
      and G.pick(S, "sub", chat, PR.PRICES, PR.SUGGESTED) == fast
      and G.pick(S, "daemon", chat, PR.PRICES, PR.SUGGESTED) == fast,
      "balanced: writing on the chat model, reading on the fast one")
check(G.pick({"governor": "powersave"}, "main", chat, PR.PRICES, PR.SUGGESTED) == fast,
      "powersave: everything on the fast model")
check(G.fast_model({"fast_model": "Qwen/Qwen3-8B"}, chat, PR.PRICES, PR.SUGGESTED) == "Qwen/Qwen3-8B",
      "the fast model can be chosen")
check(G.fast_model({}, "deepseek-ai/DeepSeek-V4-Flash", PR.PRICES, PR.SUGGESTED)
      == "deepseek-ai/DeepSeek-V4-Flash", "when the chat model is already the cheapest, it stays")
check(0.2 < G.saving(S, chat, PR.PRICES, PR.SUGGESTED) < 0.9, "and it knows what it saves",
      G.saving(S, chat, PR.PRICES, PR.SUGGESTED))
check(G.mode_of({"governor": "turbo"}) == "performance", "an unknown mode is performance")

progs, probs = PG.load([])
check(set(progs) >= {"reviewer", "tester", "auditor", "checkup", "researcher", "explainer"} and not probs,
      "the built-in programs all parse", probs)
check(PG.caps_of(progs["reviewer"]) == {"read", "exec"} and PG.caps_of(progs["tester"]) == {"read", "write", "exec"}
      and PG.caps_of(progs["researcher"]) == {"read", "net"}, "a program's capabilities follow from its tools")
for bad, why in (("---\nmode: report\ntools: read write\n---\nx", "can't use write"),
                 ("---\nmode: sideways\n---\nx", "mode must be"), ("---\nmode: report\n---\n", "no prompt"),
                 ("---\ntokens: lots\n---\nx", "must be a number")):
    try:
        PG.parse(bad, "bad")
        check(False, f"refused: {why}")
    except PG.ProgramError as ex:
        check(why in str(ex), f"a broken program is refused ({why})", ex)
pdir = tmp / "progs"
pdir.mkdir()
(pdir / "reviewer.md").write_text("---\ndescription: mine\nmode: report\ntools: read\n---\nMy review.")
(pdir / "Bad Name.md").write_text("x")
progs, probs = PG.load([pdir])
check(progs["reviewer"]["description"] == "mine" and progs["reviewer"]["source"].endswith("reviewer.md")
      and probs, "your program replaces a built-in of the same name; a bad file is reported", probs)


class ProgBackend(BgBackend):
    def stream(self, messages, on_delta, on_done, on_error, vision=False,
               should_stop=None, attempts=3, on_open=None):
        sysmsg = messages[0]["content"] if messages else ""
        if sysmsg.startswith("You are an agent program"):
            self.seen.append((sysmsg, messages[1]["content"]))
            n = sum(1 for m in messages if m["role"] == "assistant")
            if n == 0:
                on_delta("```bash\necho reviewing-$((2+2))\n```\n```fetch https://example.com```\n"
                         "```write hack.txt\nx\n```")
            else:
                self.results.append(messages[-1]["content"])
                on_delta("2 bugs found\nparser.py:3 crashes on empty input.")
            self.last_usage = {"prompt": 400, "completion": 40, "cached": 0, "reasoning": 0}
            on_done()
            return
        super().stream(messages, on_delta, on_done, on_error, vision, should_stop,
                       attempts, on_open)


w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.set_workspace(proj, announce=False)
w.backend = ProgBackend([])
w.backend.seen, w.backend.results = [], []
check("AGENT OS:" in w.history[0]["content"] and "reviewer" in w.history[0]["content"],
      "the agent knows which programs it can run")
pid = w.run_program("reviewer the parser")
w.kernel.wait(pid, 20); settle()
p = w.kernel.procs[pid]
check(p.kind == "program" and p.program == "reviewer" and p.caps == {"read", "exec"},
      "/run starts a program as a process with the program's capabilities", (p.kind, p.caps))
check(w.backend.seen and "YOUR PROGRAM (reviewer)" in w.backend.seen[0][0]
      and w.backend.seen[0][1] == "the parser", "it runs the program's prompt on your input")
res = w.backend.results[0] if w.backend.results else ""
check("reviewing-4" in res and "fetch is not available" in res and "write is not available" in res
      and not (proj / "hack.txt").exists(), "only the program's own tools exist for it", res[:500])
tr = w.kernel.strace(pid)
check([t[1] for t in tr] == ["bash"] and tr[0][3] >= 0, "and its syscalls are traced", tr)
texts = notes()
check(any(t.startswith("PROGRAM REVIEWER") for t in texts) and any("2 bugs found" in t for t in texts),
      "its card and its headline show in the chat")
check(w.show_strace(str(pid)) and any(t.startswith("STRACE") for t in notes())
      and any("capabilities: read exec" in t for t in notes()), "/strace shows them")
w.set_access("sandbox")
w.backend.results.clear()
(CONFIG := cn.CONFIG_DIR / "programs").mkdir(parents=True, exist_ok=True)
(CONFIG / "webby.md").write_text("---\ndescription: looks things up\nmode: report\ntools: bash fetch\n---\nLook it up.")
pid2 = w.run_program("webby")
w.kernel.wait(pid2, 20); settle()
res = w.backend.results[-1] if w.backend.results else ""
check("no 'net' capability" in res and not w.kernel.can(pid2, "net"),
      "in an offline-sandboxed folder a program loses the network, and the kernel refuses it", res[:300])
check(any(t[5] for t in w.kernel.strace(pid2)), "the refusal is in its trace")
w.set_access("full")
(CONFIG / "maker.md").write_text("---\ndescription: makes a file\nmode: act\ntools: read write bash\n---\nMake it.")
w.backend = ProgBackend(["```spawn\n@maker kappa: make a file\n```\nStarted.", "ok"])
w.backend.seen, w.backend.results = [], []
type_and_send(w, "have maker do it")
bgs = [r for r in w.kernel.ps(all=True) if r["kind"] == "background" and r["program"] == "maker"]
check(bgs and bgs[0]["caps"] == "read write exec", "```spawn @program``` runs an act program in a copy", bgs)
if bgs:
    w.kernel.wait(bgs[0]["pid"], 20); settle()
    e = w._bg[bgs[0]["pid"]]
    check(e["changes"] == {"kappa.txt": "added"} and not (proj / "kappa.txt").exists(),
          "its work waits in its copy for a merge", e["changes"])
d = [r for r in w.kernel.ps(all=True) if r["kind"] == "subagent"]
w.backend = SubBackend(["```task\nfind hello\n```", "done"])
type_and_send(w, "look")
sub = [r for r in w.kernel.ps(all=True) if r["kind"] == "subagent"][-1]
check(sub["caps"] == "read net", "sub-agents run with read and net only", sub["caps"])
sv = w._guard(sub["pid"], {"write": lambda a: "WROTE", "read": lambda a: "ok"})
check(sv["write"](("x", "y")).startswith("[refused") and sv["read"]("x") == "ok",
      "the guard refuses a syscall the process has no capability for")
w.entry.get_buffer().set_text("/governor balanced")
w.on_send()
check(w.settings.get("governor") == "balanced" and any(t.startswith("GOVERNOR") for t in notes()),
      "/governor switches mode and shows who runs on what")
bk = cn.Backend(w.settings)
bk.role = "sub"
m_sub = bk.model()
bk.role = "main"
check(m_sub != bk.model() and m_sub == cn._governor.fast_model(w.settings, bk.model(), PR.PRICES, PR.SUGGESTED),
      "a sub-agent's call goes to the fast model under balanced", (m_sub, bk.model()))
bk.pin = "zai-org/GLM-5.3"
check(bk.model() == "zai-org/GLM-5.3", "a program's pinned model wins")
w.settings["governor"] = "performance"
w.entry.get_buffer().set_text("/programs")
w.on_send()
check("PROGRAMS" in notes() and any(t.startswith("webby") for t in notes()), "/programs lists them")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- hibernate, replay, fork: time travel for agents ---")
from chucknorris_ext import hibernate as H, worker as WK
st = H.Store(tmp / "procs-unit")
st.save({"key": "1-2", "msgs": [], "time": 5})
st.save({"key": "1-1", "msgs": [], "time": 1})
check([r["key"] for r in st.load()] == ["1-1", "1-2"], "records persist, oldest first")
st.remove("1-1")
check([r["key"] for r in st.load()] == ["1-2"] and not st.save({"key": "../evil", "msgs": []}),
      "a record is removed; a key that isn't a key is refused")
conv = [{"role": "system", "content": "S"}, {"role": "user", "content": "B"},
        {"role": "assistant", "content": "```read a.py```"}, {"role": "user", "content": "R1"},
        {"role": "assistant", "content": "```bash\nmake\n```"},
        {"role": "user", "content": "R2\n\nThis was your last step: write the REPORT now."}]
check([s[0] for s in H.steps_of(conv)] == [1, 2] and H.steps_of(conv)[1][2].startswith("R2"),
      "a run reads back as steps")
fm = H.fork_messages(conv, 2, "try make -j")
check(len(fm) == 6 and "last step" not in fm[-1]["content"] and "try make -j" in fm[-1]["content"]
      and conv[-1]["content"].endswith("now."), "a fork keeps the run up to its step, adds the "
      "instruction, and leaves the original alone")
check(len(H.fork_messages(conv, 1)) == 4 and H.tool_labels(conv[4]["content"]) == "bash make",
      "fork at an earlier step; a step's tools as a label")
seen_steps = []
replies = iter(["```read x```", "REPORT: done"])
rep_, n_ = WK.run_worker("", lambda m: next(replies), lambda r: [("read", "x")] if "```" in r else [],
                         {"read": lambda a: "X"}, resume=conv[:4], on_step=lambda m, n: seen_steps.append(n))
check(rep_ == "REPORT: done" and seen_steps == [2] and n_ == 3,
      "a worker resumes from a saved conversation and goes on numbering steps", (rep_, seen_steps, n_))


class TimeBackend(BgBackend):
    """tt: a three-step writer; a fork's instruction changes what it writes.
    hib: a run that can be cut off between steps."""
    hold = threading.Event()

    def stream(self, messages, on_delta, on_done, on_error, vision=False,
               should_stop=None, attempts=3, on_open=None):
        sysmsg = messages[0]["content"] if messages else ""
        brief = messages[1]["content"] if len(messages) > 1 else ""
        if sysmsg.startswith("You are a background agent") and brief.startswith(("tt", "hib")):
            n = sum(1 for m in messages if m["role"] == "assistant")
            forked = re.search(r"Their instruction: write (\w+)", messages[-1]["content"])
            if brief.startswith("hib"):
                if n == 0:
                    on_delta("```write h.txt\nfirst\n```")
                elif n == 1 and not TimeBackend.hold.is_set():
                    TimeBackend.hold.wait(5)
                    on_delta("```bash\necho never\n```")
                elif n == 1:
                    on_delta("```write h2.txt\nafter the restart\n```")
                else:
                    on_delta(f"REPORT: finished after {n} steps.")
            elif forked:
                on_delta(f"```write a.txt\n{forked.group(1)}\n```")
            elif n == 0:
                on_delta("```write a.txt\nv1\n```")
            elif n == 1:
                on_delta("```write a.txt\nv2\n```\n```write b.txt\nb\n```")
            else:
                on_delta("REPORT: a.txt and b.txt written.")
            self.last_usage = {"prompt": 300, "completion": 30, "cached": 0, "reasoning": 0}
            on_done()
            return
        super().stream(messages, on_delta, on_done, on_error, vision, should_stop,
                       attempts, on_open)


tproj = tmp / "timeproj"
tproj.mkdir()
(tproj / "keep.py").write_text("x = 1\n")
(tproj / ".gitignore").write_text(".env\n")
(tproj / ".env").write_text("SECRET=1\n")
w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.set_workspace(tproj, announce=False)
w.backend = TimeBackend([])
pid = w.spawn_background("tt: write two files")
w.kernel.wait(pid, 20); settle()
e = w._bg[pid]
check([s["step"] for s in e["steps"]] == [0, 1, 2] and all(s["sha"] for s in e["steps"]),
      "every step of a writer is snapshotted", e["steps"])
check(e["changes"] == {"a.txt": "added", "b.txt": "added"} and any(r["key"] == e["key"] for r in w._hib.load()),
      "its finished, unmerged work is kept on disk")
check(w.show_replay(str(pid)) and any(t.startswith("REPLAY") for t in notes())
      and any("step 2 · write a.txt, write b.txt" in t for t in notes()),
      "/replay shows the run step by step", [t for t in notes() if t.startswith("step")][:4])
fpid = w.fork_process(f"{pid} 1 write v9")
w.kernel.wait(fpid, 20); settle()
f = w._bg[fpid]
check((f["ws"].work / "a.txt").read_text() == "v9\n" and not (f["ws"].work / "b.txt").exists()
      and (f["ws"].work / ".env").read_text() == "SECRET=1\n",
      "/fork restarts it from step 1: the files as they were then (ignored ones kept), "
      "the new instruction followed", sorted(os.listdir(f["ws"].work)))
check((e["ws"].work / "a.txt").read_text() == "v2\n", "and the original run is untouched")
check(f["msgs"][3]["content"].count("FORKED") == 1 and len(f["msgs"]) >= 6,
      "the fork has the original conversation up to its step")
ffpid = w.fork_process(f"{fpid} 2 write v77")
w.kernel.wait(ffpid, 20); settle()
check(ffpid and (w._bg[ffpid]["ws"].work / "a.txt").read_text() == "v77\n"
      and not (w._bg[ffpid]["ws"].work / "b.txt").exists(),
      "a fork of a fork replays from the original base")
check(w.fork_process(f"{fpid} 0 x") is None, "a fork can't be forked before its own fork point")
check("merged" in w.join_background(fpid) and (tproj / "a.txt").read_text() == "v9\n",
      "a fork's work merges like any background agent's")

TimeBackend.hold.clear()
hpid = w.spawn_background("hib: two steps then a restart")
for _ in range(100):
    if len(w._bg[hpid].get("steps", [])) >= 2:
        break
    time.sleep(0.05)
w._closing = True                       # the window closes (or Chuck crashes) mid-run
w.kernel.signal(hpid, "KILL")
TimeBackend.hold.set()
w.kernel.wait(hpid, 10); settle()
recs = {r["name"]: r for r in w._hib.load()}
hrec = next((r for r in recs.values() if r["name"].startswith("hib")), None)
check(hrec and hrec["status"] == "running" and len(H.steps_of(hrec["msgs"])) == 1,
      "a run cut off mid-way stays hibernated at its last step", hrec and hrec["status"])
w2 = new_win([], tmp)
w2.set_agent_mode(True, announce=False)
w2.set_workspace(tproj, announce=False)
w2.backend = TimeBackend([])
check(w2.offer_hibernated() and "INTERRUPTED AGENTS" in [x.get_text() for x in w2.msgbox.walk()
                                                          if isinstance(x.get_text(), str)],
      "the next start offers to resume it")
restored = [x for x in w2._bg.values() if x["name"].startswith("tt") and x["status"] == "finished"]
check(restored and restored[0]["changes"] == {"a.txt": "added", "b.txt": "added"},
      "finished work that was never merged comes back after a restart, ready to merge",
      [(x["name"], x["status"]) for x in w2._bg.values()])
npid = w2.resume_hibernated(hrec)
w2.kernel.wait(npid, 20); settle()
he = w2._bg[npid]
check(he["report"] == "REPORT: finished after 2 steps." and (he["ws"].work / "h.txt").exists()
      and (he["ws"].work / "h2.txt").exists(),
      "Resume goes on from the step it was cut off at, in the same copy", he["report"])
check(restored and "merged" in w2.join_background(restored[0]["pid"]) and (tproj / "b.txt").exists(),
      "and the restored work merges")
check(not [r for r in w2._hib.load() if r["key"] == restored[0]["key"]],
      "a merged run's record is gone")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- what agents need: talk, ask, share, pace, wait ---")
k = K.Kernel()
a = k.spawn("a")
k.block(a.pid, "asking you: which port?")
check(a.state == "waiting" and "which port" in a.activity, "a process waiting on the user is WAITING")
k.unblock(a.pid)
check(a.state == "ready", "and ready again once answered")
k.send(a.pid, "hello", sender=0); k.send(a.pid, "again", sender=1)
check(k.drain(a.pid) == [(0, "hello"), (1, "again")] and k.drain(a.pid) == [], "mail is drained in order")
k.post("auth", "pid 3 is rewriting auth.py", by=3)
check(k.board_items()[0][:3] == ("auth", "pid 3 is rewriting auth.py", 3), "the board lists who posted what")
box = {"mail": ["the user: also add a README"], "n": 0}


def fake_model(m):
    box["n"] += 1
    if box["n"] == 1:
        return "REPORT: done"                      # about to finish: mail arrives
    return "REPORT: done, and the README too" if "also add a README" in m[-1]["content"] else "REPORT: ?"


rep_, _ = WK.run_worker("x", fake_model, lambda r: [], {}, inbox=lambda: box.pop("mail", []))
check(rep_ == "REPORT: done, and the README too",
      "a message that arrives as an agent is finishing is heard before it reports")
seen = {}


def fm2(m):
    seen.setdefault("msgs", m)
    return "```read x```" if len(m) < 4 else "REPORT"


WK.run_worker("x", fm2, lambda r: [("read", "x")] if "```" in r else [], {"read": lambda a: "X"},
              status_line=lambda: "tokens 1.2k of 50k · 9 min left")
check("[kernel] step 1 of 60 · tokens 1.2k of 50k" in seen["msgs"][-1]["content"],
      "every result carries the kernel's line: what's spent, what's left")


class TalkBackend(TimeBackend):
    def stream(self, messages, on_delta, on_done, on_error, vision=False,
               should_stop=None, attempts=3, on_open=None):
        sysmsg = messages[0]["content"] if messages else ""
        brief = messages[1]["content"] if len(messages) > 1 else ""
        if sysmsg.startswith("You are a background agent") and brief.startswith("talk"):
            n = sum(1 for m in messages if m["role"] == "assistant")
            last = messages[-1]["content"]
            if n == 0:
                on_delta("```post auth: talk agent is changing server.py\n```\n```board```\n"
                         "```ask Which port, 8080 or 9090?```")
            elif n == 1:
                self.saw = last
                port = re.search(r"The user answered: (\d+)", last)
                on_delta(f"```write port.txt\n{port.group(1) if port else 'none'}\n```")
            else:
                on_delta("REPORT: port set.")
            self.last_usage = {"prompt": 300, "completion": 30, "cached": 0, "reasoning": 0}
            on_done()
            return
        super().stream(messages, on_delta, on_done, on_error, vision, should_stop,
                       attempts, on_open)


w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.set_workspace(tproj, announce=False)
w.backend = TalkBackend([])
tpid = w.spawn_background("talk: set the port")
for _ in range(100):
    if tpid in w._asks:
        break
    time.sleep(0.05)
check(tpid in w._asks and w.kernel.procs[tpid].state == "waiting",
      "a background agent's ```ask``` waits for you (WAITING in /top)", w.kernel.procs[tpid].state)
check(any(t.startswith(f"PID {tpid} ASKS") for t in notes()), "and shows you the question")
check(w.kernel.board_items() and w.kernel.board_items()[0][0] == "auth", "its ```post``` is on the board")
w.entry.get_buffer().set_text(f"/send {tpid} also keep the old port as a fallback")
w.on_send()
w.entry.get_buffer().set_text(f"/answer {tpid} 9090")
w.on_send()
w.kernel.wait(tpid, 20); settle()
saw = getattr(w.backend, "saw", "")
check("The user answered: 9090" in saw and "the user: also keep the old port" in saw
      and "[kernel] step 1" in saw and "talk agent is changing server.py" in saw,
      "the answer, your message, its budget and the board all reach it", saw[:600])
check((w._bg[tpid]["ws"].work / "port.txt").read_text() == "9090\n", "and it acts on the answer")
w.entry.get_buffer().set_text("/board")
w.on_send()
check("BOARD" in notes() and any("talk agent is changing" in t for t in notes()), "/board shows it")
p2 = w.spawn_background("tt: for waiting")
w.backend = TimeBackend([f"```wait {p2}```", "Got it."])
type_and_send(w, "do it and wait")
h = " ".join(str(m.get("content")) for m in w.history[-4:])
check(f"pid {p2}" in h and "a.txt and b.txt written" in h and f"```join {p2}```" in h,
      "the main agent can ```wait``` for a background agent and gets its report", h[-500:])
check(not any("a.txt and b.txt written" in n for n in w._user_notes),
      "a waited-for report isn't delivered twice")
w.backend = TimeBackend(["```post plan: refactor the parser first\n```", "ok"])
type_and_send(w, "share the plan")
check(w.kernel.board_items()[0][:2] == ("plan", "refactor the parser first"), "the main agent posts too")
check("no running process" in w.send_to(9999, "hi"), "sending to a process that isn't running says so")
w3 = new_win([], tmp)
w3.set_agent_mode(True, announce=False)
w3.set_workspace(tproj, announce=False)
w3.backend = TalkBackend([])
TimeBackend.hold.clear()
young = w3.spawn_background("hib: started in the first seconds")
for _ in range(100):
    if len(w3._bg[young].get("steps", [])) >= 2:
        break
    time.sleep(0.05)
w3.offer_hibernated()
check(not any(t == "INTERRUPTED AGENTS" for t in [x.get_text() for x in w3.msgbox.walk()
                                                  if isinstance(x.get_text(), str)]),
      "an agent started in this session is never offered as interrupted")
w3.process_command("kill", str(young))
TimeBackend.hold.set()
w3.kernel.wait(young, 10); settle()

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- review fixes: forks, folders, budgets, daemons ---")
import subprocess as _sp
from chucknorris_ext import checkpoint as CP
rt = tmp / "replayt"
(rt / "w").mkdir(parents=True)
outside = tmp / "outside.yaml"
outside.write_text("user's real config\n")
(rt / "w" / "config").symlink_to(outside)
(rt / "w" / "run.sh").write_text("echo hi\n")
(rt / "w" / "foo").mkdir()
(rt / "w" / "foo" / "bar").write_text("x\n")
shw = CP.Shadow(rt / "w", rt / "store")
s0 = shw.snapshot("0")
(rt / "w" / "config").unlink()
(rt / "w" / "config").write_text("agent version\n")          # symlink -> regular file
os.chmod(rt / "w" / "run.sh", 0o755)
shutil.rmtree(rt / "w" / "foo")
(rt / "w" / "foo").write_text("now a file\n")                 # folder -> file
(rt / "w" / "link2").symlink_to("run.sh")
(rt / "w" / "sub").mkdir()
_sp.run(["git", "init", "-q", str(rt / "w" / "sub")], check=True)
(rt / "w" / "sub" / "f").write_text("nested\n")
_sp.run(["git", "-C", str(rt / "w" / "sub"), "-c", "user.email=a@b", "-c", "user.name=a", "add", "."], check=True)
_sp.run(["git", "-C", str(rt / "w" / "sub"), "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "x"], check=True)
s1 = shw.snapshot("1")
dest = rt / "dest"
dest.mkdir()
(dest / "config").symlink_to(outside)
(dest / "run.sh").write_text("echo hi\n")
(dest / "foo").mkdir()
(dest / "foo" / "bar").write_text("x\n")
err = None
try:
    shw.replay_onto(s0, s1, dest)
except Exception as ex:
    err = ex
check(err is None and outside.read_text() == "user's real config\n"
      and not (dest / "config").is_symlink() and (dest / "config").read_text() == "agent version\n",
      "a fork never writes through a symlink to a file outside its copy", err)
check(os.access(dest / "run.sh", os.X_OK) and (dest / "foo").read_text() == "now a file\n"
      and os.readlink(dest / "link2") == "run.sh",
      "a fork keeps executable bits, symlinks, and a folder that became a file")
check(not (dest / "sub" / "f").exists(), "a nested repository is left alone, not a crash")

w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.set_workspace(tproj, announce=False)
w.backend = TimeBackend([])
pa = w.spawn_background("tt: first window's run")
w.kernel.wait(pa, 20); settle()
w2 = new_win([], tmp)
w2.set_agent_mode(True, announce=False)
w2.set_workspace(tproj, announce=False)
w2.backend = TimeBackend([])
pb = w2.spawn_background("tt: first window's run")
w2.kernel.wait(pb, 20); settle()
check(pa == pb and w._bg[pa]["ws"].dir != w2._bg[pb]["ws"].dir and w._bg[pa]["ws"].work.is_dir(),
      "the same pid in a later session never takes over an earlier run's copy",
      (w._bg[pa]["ws"].dir.name, w2._bg[pb]["ws"].dir.name))
rec = {"key": "1-99", "kind": "background", "name": "tt: x", "brief": "tt: x", "root": str(tproj),
       "program": "", "msgs": [], "steps": [{"step": 1, "tokens": 1000000}]}
w2.set_access("sandbox")
e_, _p = w2._new_entry_proc(rec, spent=1000000)
q = w2.kernel.procs[e_["pid"]].quota
check(q["tokens"] == 500000 and not w2.kernel.can(e_["pid"], "net"),
      "a resumed run gets what was left of its budget, and an offline folder keeps it offline", q)
w2.set_access("full")
fake = dict(w2._bg[pb])
fake["program"] = "no-such-program"
w2._bg[999] = fake
check(w2.fork_process("999 1 x") is None, "forking a run whose program is gone is refused")
del w2._bg[999]
old = w.WORKTREES / "1-1-kept"
old.mkdir(parents=True, exist_ok=True)
os.utime(old, (1, 1))
w._hib.save({"key": "1-77", "msgs": [], "ws_dir": str(old), "status": "finished", "time": time.time()})
gone = w.WORKTREES / "1-2-stale"
gone.mkdir(exist_ok=True)
os.utime(gone, (1, 1))
w._prune_worktrees()
check(old.is_dir() and not gone.exists(), "old copies are pruned, but never one a saved run still needs")
w._hib.remove("1-77")
check(G.fast_model({}, "my/unknown-model", PR.PRICES, PR.SUGGESTED) == "my/unknown-model"
      and G.fast_model({}, chat, PR.PRICES, PR.SUGGESTED, lambda m: (0.01, None, 0.01) if m == chat
                       else PR.PRICES.get(m.lower())) == chat,
      "the governor never moves work to a model that costs more than yours (unknown or custom prices)")

reg_p = tmp / "dm-bad.json"
for bad in ("null", "5", '{"items": "x"}', '[{"when": "every 2h", "brief": "x"}, {"id": "3"}]'):
    reg_p.write_text(bad)
    r_ = D.Registry(reg_p)
    check(r_.items == [], f"a damaged daemons file ({bad[:20]}) doesn't stop Chuck starting")
ib_p = tmp / "ib-bad.json"
ib_p.write_text('{"a": 1}')
check(D.Inbox(ib_p).unread() == [], "nor does a damaged inbox")
reg_p.unlink()
r_ = D.Registry(reg_p)
a1 = r_.add("", "every 1h", "one")
a2 = r_.add("", "every 1h", "two")
r_.remove(a2["id"])
a3 = r_.add("", "every 1h", "three")
check(a3["id"] == a2["id"] + 1 and D.Registry(reg_p).add("", "every 1h", "four")["id"] == a3["id"] + 1,
      "a deleted daemon's id is never given to a new one")
check(D.parse_when("when disk is above 90%") == {"disk": "/", "above": 90}
      and D.parse_when("when disk /home is above 80%") == {"disk": "/home", "above": 80},
      "'when disk is above 90%' watches /, not a path called 'is'")
dst = _sp.run([sys.executable, "-c", """
import os, time, sys
os.environ['TZ'] = 'America/New_York'; time.tzset()
sys.path.insert(0, '.')
from chucknorris_ext import daemons as D
class P:
    localtime = staticmethod(time.localtime)
spec = {'when': {'daily': '00:30'}, 'last_run': 0}
t = time.mktime((2026, 11, 1, 0, 0, 0, 0, 0, -1))
fired = []
for i in range(0, 4 * 3600, 60):
    due, _w, _s = D.check(spec, t + i, P())
    if due:
        fired.append(time.strftime('%H:%M %Z', time.localtime(t + i)))
        spec['last_run'] = t + i
print(fired)"""], capture_output=True, text=True)
check(dst.stdout.strip() == "['00:30 EDT']", "a daily daemon fires once on the day the clocks go back",
      dst.stdout + dst.stderr)
rop = D.read_only_problem
for c in ("echo suspend | xargs systemctl", "systemctl -p status suspend", "nice systemctl suspend",
          "sh -c 'systemctl suspend'", '"systemctl" suspend', "s\\ystemctl suspend",
          "(systemctl suspend)", "$(printf systemctl) suspend", "git log --output=/tmp/x",
          "sort -o ~/.bashrc /dev/null", "git stash", "git reflog expire --all", "loginctl list-users",
          "awk 'BEGIN{system(\"id\")}'"):
    check(rop(c) and rop(c, strict=True), f"a daemon may not run: {c}")
check(not any(rop(c, strict=True) for c in ("systemctl status nginx", "systemctl list-units --state=failed",
                                             "journalctl -p 3 -b --no-pager 2>&1 | tail -50",
                                             "git log -5 --oneline", "du -sh ~/x | sort -h")),
      "and every ordinary look still runs")
check("/run/dbus/system_bus_socket" not in cn._LOOK_RUN,
      "a daemon's sandbox has no system bus at all: nothing it runs can reach services or power")
mail = ["the user: hello"]
WK.run_worker("x", lambda m: "REPORT", lambda r: [], {}, max_steps=1, inbox=lambda: [mail.pop()] if mail else [])
check(mail == ["the user: hello"], "mail isn't taken from the mailbox on a step where it can't be heard")
w3 = new_win([], tmp)
w3.set_agent_mode(True, announce=False)
w3.set_workspace(tproj, announce=False)
w3.backend = TimeBackend([])
TimeBackend.hold.clear()
hp = w3.spawn_background("hib: waited for")
res = w3._t_wait(f"{hp} 0")
out_ = res() if callable(res) else res
check("still running" in out_ and not w3._bg[hp].get("waited"),
      "a wait that times out still lets the report reach the agent later")
TimeBackend.hold.set()
w3.kernel.wait(hp, 10); settle()
check(any("hib: waited for" in n for n in w3._user_notes), "(and it does)")
d_ = w3.daemon_add("every 1h: x")[0]
d_["enabled"] = False
d_["last_run"] = 1
w3._daemon_action(d_, "on")
check(d_["last_run"] > time.time() - 5, "resuming a paused daemon doesn't fire it at once")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- missions: a goal, planned as a graph and run by agents ---")
from chucknorris_ext import missions as MS
plan_txt = ('Here is the plan:\n```json\n{"tasks": [{"id": "t1", "title": "mka: make a", "kind": "code", "deps": [], '
            '"brief": "write mka.txt"}, {"id": "t2", "title": "look: research", "kind": "research", "deps": [], '
            '"brief": "find out"}, {"id": "t3", "title": "mkb: make b", "kind": "code", "deps": ["t1"], '
            '"brief": "write mkb.txt next to mka.txt"}, {"id": "t4", "title": "mkbad: fix", "kind": "code", '
            '"deps": ["t3"], "brief": "write bad.txt"}], "notes": "t3 needs t1"}\n```')
tasks, pnotes = MS.parse_plan(plan_txt)
check([t["id"] for t in tasks] == ["t1", "t2", "t3", "t4"] and pnotes == "t3 needs t1",
      "a plan is read out of a reply, fences and prose around it")
for bad, why in (('{"tasks": [{"id": "a", "deps": ["b"], "brief": "x"}, {"id": "b", "deps": ["a"], "brief": "y"}]}', "cycle"),
                 ('{"tasks": [{"id": "a", "deps": ["zz"], "brief": "x"}]}', "isn't a task"),
                 ('{"tasks": []}', "no tasks"), ("no json here", "isn't JSON")):
    try:
        MS.parse_plan(bad)
        check(False, f"a broken plan is refused: {why}")
    except MS.MissionError as ex:
        check(why in str(ex), f"a broken plan is refused ({why})", ex)
mm = MS.new_mission("g", "/x", tasks)
mm["state"] = MS.ACTIVE
check([t["id"] for t in MS.ready(mm)] == ["t1", "t2"], "tasks with no dependency start together")
MS.task(mm, "t1")["state"] = MS.MERGED
check([t["id"] for t in MS.ready(mm, 5)] == ["t2", "t3"], "a task starts when what it depends on is merged")


class MissionBackend(ProgBackend):
    def stream(self, messages, on_delta, on_done, on_error, vision=False,
               should_stop=None, attempts=3, on_open=None):
        sysmsg = messages[0]["content"] if messages else ""
        brief = messages[1]["content"] if len(messages) > 1 else ""
        self.last_usage = {"prompt": 300, "completion": 30, "cached": 0, "reasoning": 0}
        if sysmsg.startswith("You plan missions"):
            self.planned = messages
            on_delta(plan_txt); on_done(); return
        if sysmsg.startswith("You are a background agent") and brief.split(":")[0] in ("mka", "mkb", "mkbad"):
            tag = brief.split(":")[0]
            n = sum(1 for m in messages if m["role"] == "assistant")
            last = messages[-1]["content"]
            if "tests FAIL" in last:
                on_delta("```write bad.txt\nfixed\n```")
            elif n == 0:
                fname, body = ("bad", "broken") if tag == "mkbad" else (tag, f"made by {tag}")
                on_delta(f"```write {fname}.txt\n{body}\n```\n```bash\nls\n```")
            else:
                self.seen_ls = getattr(self, "seen_ls", {})
                self.seen_ls[tag] = last
                on_delta(f"REPORT: {tag}.txt written.")
            on_done(); return
        super().stream(messages, on_delta, on_done, on_error, vision, should_stop, attempts, on_open)


mproj = tmp / "missionproj"
(mproj / ".chuck").mkdir(parents=True)
(mproj / "app.py").write_text("x = 1\n")
(mproj / ".chuck" / "test").write_text("test ! -f bad.txt || grep -q fixed bad.txt\n")
w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.set_workspace(mproj, announce=False)
w.backend = MissionBackend([])
w.backend.seen, w.backend.results = [], []
m = w.start_mission("build a and b")
settle()
check(m["state"] == MS.AWAITING and len(m["tasks"]) == 4 and "Files and what they define" in w.backend.planned[0]["content"],
      "/mission plans with the project's map, then waits for Start", m["state"])
check(any(t.startswith("MISSION") for t in notes()) and "Start" in
      [x.get_label() for x in w.msgbox.walk() if isinstance(x, cn.Gtk.Button)], "its card shows the plan and Start")
check(w.kernel.procs[m["pid"]].kind == "mission", "the mission is a process (a group for its agents)")
w.mission_action(m["id"], "start")
for _ in range(200):
    if m["state"] in (MS.COMPLETE, MS.PAUSED, MS.CANCELLED):
        break
    time.sleep(0.1)
    settle()
check(m["state"] == MS.COMPLETE, "the mission runs to the end on its own", [(t["id"], t["state"], t["note"]) for t in m["tasks"]])
check((mproj / "mka.txt").exists() and (mproj / "mkb.txt").exists() and (mproj / "bad.txt").exists() and (mproj / "bad.txt").read_text() == "fixed\n",
      "every code task's work is merged into the folder", [(t["id"], t["state"], t["note"], t["attempts"]) for t in m["tasks"]])
check("mka.txt" in w.backend.seen_ls.get("mkb", ""), "a task starts from a folder that already has its dependencies' work")
t4 = MS.task(m, "t4")
check(t4["attempts"] == 2 and t4["state"] == MS.MERGED and "tests pass" in t4["note"],
      "a task whose tests fail on its copy is sent back to fix them, then merged", t4)
kids = [p for p in w.kernel.procs.values() if p.ppid == m["pid"]]
check(len(kids) >= 5 and sum(p.tokens() for p in kids) > 0 and m["spent_tokens"] > 0
      and w.kernel.procs[m["pid"]].tokens() >= sum(p.tokens() for p in kids),
      "every agent of the mission is a child of its process, charged to it",
      (m["spent_tokens"], [(p.pid, p.kind, p.tokens()) for p in kids]))
check(any("Mission complete" in n for n in w._user_notes) and any("mission complete" in t for t in notes()),
      "the chat and the agent hear that it's complete")
check(MS.Store(w.MISSIONS_DIR).load()[0]["state"] == MS.COMPLETE, "missions are saved")
m2 = w.start_mission("stop me")
settle()
w.mission_action(m2["id"], "cancel")
check(m2["state"] == MS.CANCELLED and not w.kernel.procs[m2["pid"]].alive(), "Cancel ends a mission and its agents")



class MissionHibBackend(TimeBackend):
    def stream(self, messages, on_delta, on_done, on_error, vision=False,
               should_stop=None, attempts=3, on_open=None):
        if messages and messages[0]["content"].startswith("You plan missions"):
            self.last_usage = {"prompt": 300, "completion": 30, "cached": 0, "reasoning": 0}
            on_delta('{"tasks": [{"id": "t1", "title": "hib: survive a restart", "kind": "code", '
                     '"deps": [], "brief": "two steps"}]}')
            on_done(); return
        super().stream(messages, on_delta, on_done, on_error, vision, should_stop, attempts, on_open)


hproj = tmp / "mhibproj"
hproj.mkdir()
(hproj / "x.py").write_text("x = 1\n")
w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.set_workspace(hproj, announce=False)
w.backend = MissionHibBackend([])
TimeBackend.hold.clear()
mh = w.start_mission("survive a restart", autostart=True)
for _ in range(100):
    t1 = MS.task(mh, "t1") if mh["tasks"] else None
    if t1 and t1.get("pid") and len(w._bg.get(t1["pid"], {}).get("steps", [])) >= 2:
        break
    time.sleep(0.05)
w._closing = True                                  # Chuck closes mid-mission
w.kernel.signal(mh["pid"], "KILL")
TimeBackend.hold.set()
w.kernel.wait(t1["pid"], 10); settle()
w2 = new_win([], tmp)
w2.set_agent_mode(True, announce=False)
w2.set_workspace(hproj, announce=False)
w2.backend = MissionHibBackend([])
w2.offer_hibernated()
mh2 = w2._missions.get(mh["id"])
check(mh2 is not None and MS.task(mh2, "t1")["state"] == MS.RUNNING,
      "after a restart a running mission goes on, its cut-off task resumed from its checkpoint",
      mh2 and MS.task(mh2, "t1"))
for _ in range(100):
    if mh2 and mh2["state"] == MS.COMPLETE:
        break
    time.sleep(0.1)
    settle()
check(mh2 and mh2["state"] == MS.COMPLETE and (hproj / "h.txt").exists() and (hproj / "h2.txt").exists(),
      "and it completes, with the work from before and after the restart merged",
      mh2 and [(t["state"], t["note"]) for t in mh2["tasks"]])
check(not any(t == "INTERRUPTED AGENTS" for t in [x.get_text() for x in w2.msgbox.walk()
                                                  if isinstance(x.get_text(), str)]),
      "a mission's agents are resumed by the mission, not offered one by one")

w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.set_workspace(mproj, announce=False)
w.backend = MissionBackend([])
(mproj / "bad.txt").unlink()
gp = w.spawn_background("mkbad: plain background run")
w.kernel.wait(gp, 20); settle()
for _ in range(50):
    if w._bg[gp].get("verify"):
        break
    time.sleep(0.1)
check(w._bg[gp].get("verify") == "fail" and "tests FAIL on its copy" in w._bg[gp]["ui"]["act"].get_text()
      and any("FAILS the project's tests" in n for n in w._user_notes),
      "any background agent's copy is tested when it finishes: the card says it fails before you merge",
      w._bg[gp].get("verify"))

w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.set_workspace(mproj, announce=False)
w.backend = MissionBackend([])
m5 = w.start_mission("started in the first seconds")
settle()
pid5 = m5["pid"]
w.offer_hibernated()
check(w._missions[m5["id"]] is m5 and m5["pid"] == pid5,
      "a mission started in this session isn't taken over by the startup resume")
w.mission_action(m5["id"], "cancel")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- the budget: one limit for everything the OS runs ---")
from chucknorris_ext import budget as BU
lp = tmp / "spend.json"
L_ = BU.Ledger(lp)
t0_ = time.mktime((2026, 3, 10, 12, 0, 0, 0, 0, -1))
L_.add(0.40, t0_); L_.add(0.70, t0_ + 3600); L_.add(0.25, t0_ - 86400)
check(abs(L_.today(t0_) - 1.10) < 1e-9 and abs(L_.month(t0_) - 1.35) < 1e-9, "spend is booked by day and month")
check(L_.over(1.0, 0, t0_)[0] == "day" and L_.over(2.0, 0, t0_) is None and L_.over(0, 1.2, t0_)[0] == "month",
      "a daily or monthly limit is reached; 0 means none")
L_.allow_more("day", 1.0, t0_)
check(L_.over(1.0, 0, t0_) is None, "'allow more' lifts today's limit by that much")
L_.save()
L2_ = BU.Ledger(lp)
check(abs(L2_.today(t0_) - 1.10) < 1e-9 and L2_.over(1.0, 0, t0_) is None, "the ledger survives a restart")
lp.write_text('{"days": {"2026-03-10": "lots"}, "extra": 5}')
check(BU.Ledger(lp).today(t0_) == 0, "a damaged ledger is read as empty, not a crash")
check([d for d, _v in L2_.history(3, t0_)] == ["2026-03-08", "2026-03-09", "2026-03-10"], "and the last days, oldest first")
w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.set_workspace(tproj, announce=False)
w.settings["budget_day_usd"] = 0.000001
w._ledger.add(0.01)
w.backend = TimeBackend(["should not run"])
type_and_send(w, "anything")
texts = [x.get_text() for x in w.msgbox.walk() if isinstance(x.get_text(), str)]
check("BUDGET" in texts and not any("should not run" in str(m.get("content")) for m in w.history),
      "over the daily limit the chat pauses before the next model call and asks")
bp = w.spawn_background("tt: over budget")
w.kernel.wait(bp, 10); settle()
check("budget" in str(w._bg[bp]["report"]), "a background agent is refused its call, and says why",
      w._bg[bp]["report"])
w._daemons.add("", "every 1h", "x")["last_run"] = 0
w._daemon_probe = FakeProbe()
w._daemon_tick(); settle()
check(not any(r["kind"] == "daemon" for r in w.kernel.ps(all=True)), "daemons don't wake while over budget")
w.entry.get_buffer().set_text("/budget")
w.on_send()
check(any(t.startswith("Today $") or t.startswith("Today") for t in [x.get_text() for x in w.msgbox.walk()
                                                                     if isinstance(x.get_text(), str)]),
      "/budget shows today, the month and the last two weeks")
w.entry.get_buffer().set_text("/budget day 5")
w.on_send()
check(w.settings["budget_day_usd"] == 5.0 and w._over_budget() is None, "/budget day 5 sets the limit")
w.settings["budget_day_usd"] = 0.0

print("\n--- the code runs on every Python Chuck supports ---")
import glob as _glob
if shutil_which("python3.11"):
    bad = []
    for f in ["chucknorris.py"] + sorted(_glob.glob("chucknorris_ext/*.py")):
        r = _sp.run(["python3.11", "-c", f"import ast; ast.parse(open({f!r}).read())"], capture_output=True, text=True)
        if r.returncode:
            bad.append((f, r.stderr.strip().splitlines()[-1:]))
    check(not bad, "every module parses under Python 3.11 (no 3.12-only f-strings)", bad)

print("\n--- the tray: the OS at a glance ---")
w = new_win([], tmp)
w._inbox.mark_read()                          # earlier tests' reports
for m_ in w._missions.values():
    m_["state"] = "complete"
w._tray_tick()
check(not w.tray_btn.get_visible(), "the tray is hidden while nothing is going on", w._tray_lbl.get_label())
sp_ = w.kernel.spawn("survey", kind="subagent", parent=w._pid1)
w._asks[sp_.pid] = (threading.Event(), {}, "which port?")
w._inbox.add("d", "why", "report")
w._tray_tick()
check(w.tray_btn.get_visible() and w._tray_lbl.get_label() == "? 1  \u25b6 1  \u2709 1"
      and w.tray_btn.has_css_class("tray-ask"),
      "it shows questions waiting, agents working, unread reports; a question lights it up",
      w._tray_lbl.get_label())
w.entry.get_buffer().set_text("/activity")
w.on_send()
tx = [x.get_text() for x in w.msgbox.walk() if isinstance(x.get_text(), str)]
check("ACTIVITY" in tx and "WAITING FOR YOUR ANSWER" in tx and any("which port?" in t for t in tx),
      "/activity (or a click) lists everything waiting for you and everything working")
w._asks.clear()
w.kernel.exit(sp_.pid, 0, "")
w._inbox.mark_read()
w._tray_tick()
check(not w.tray_btn.get_visible(), "and it hides again when all is quiet")

print("\n--- review fixes: missions and the budget ---")
k = K.Kernel()
mq = k.spawn("mission", quota={"tokens": 100})
c1, c2 = k.spawn("a", parent=mq.pid), k.spawn("b", parent=mq.pid)
k.charge(c1.pid, 60, 0); k.charge(c2.pid, 60, 0)
check("quota of pid" in k.over_quota(c1.pid), "a parent's quota caps its whole subtree, not each child alone")
tk, _n = MS.parse_plan(r'{"tasks": [{"id": "t1", "brief": "echo \"}\" now"}, {"id": "a.b", "brief": "x"},'
                       r' {"id": "c", "deps": ["a.b"], "brief": "y"}]}')
check(tk[0]["brief"] == 'echo "}" now' and tk[2]["deps"] == ["ab"], "escaped quotes and odd ids in a plan")
w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.set_workspace(mproj, announce=False)


class _WS:
    source, work = mproj, tmp / "fakework"


(tmp / "fakework" / ".chuck").mkdir(parents=True, exist_ok=True)
(tmp / "fakework" / ".chuck" / "test").write_text("true\n")          # the agent's "exam"
(mproj / ".chuck" / "test").write_text("# a comment first\nfalse\ntrue\n")
st_, out_ = w._verify_copy(_WS)
check(st_ == "fail", "the test gate reads .chuck/test from YOUR folder, runs every line, and an agent "
      "can't rewrite it in its copy", out_[:200])
(mproj / ".chuck" / "test").write_bytes(b"\xff\xfe broken\n")
real_inner = w._verify_copy_inner
w._verify_copy_inner = lambda ws: (_ for _ in ()).throw(PermissionError("denied"))
check(w._verify_copy(_WS)[0] == "fail", "a test command that can't even be read is a fail, not a hang")
w._verify_copy_inner = real_inner
(mproj / ".chuck" / "test").write_text("test ! -f bad.txt || grep -q fixed bad.txt\n")
mf = MS.new_mission("x", mproj, MS.parse_plan('{"tasks": [{"id": "t1", "kind": "check", "brief": "x"}]}')[0])
mf["state"], mf["pid"] = MS.ACTIVE, w.kernel.spawn("mission: x", kind="mission", parent=w._pid1).pid
w._missions[mf["id"]] = mf
real_rp = w.run_program
w.run_program = lambda *a, **kw: None
w._mission_step(mf)
w.run_program = real_rp
check(mf["state"] == MS.PAUSED and MS.task(mf, "t1")["state"] == MS.FAILED,
      "a task that can't start pauses the mission (it doesn't sit 'running' with nothing running)", mf["state"])


class LoopBackend(MissionBackend):
    def stream(self, messages, on_delta, on_done, on_error, vision=False,
               should_stop=None, attempts=3, on_open=None):
        if messages and messages[0]["content"].startswith("You are a background agent"):
            self.last_usage = {"prompt": 100, "completion": 10, "cached": 0, "reasoning": 0}
            on_delta("```write loop.txt\\nstill going\\n```\\n```bash\\nls\\n```"); on_done(); return
        super().stream(messages, on_delta, on_done, on_error, vision, should_stop, attempts, on_open)


w.backend = LoopBackend([])
w.settings["background_steps"] = 5
ml = MS.new_mission("loop", mproj, MS.parse_plan('{"tasks": [{"id": "t1", "title": "loop: never ends", "brief": "x"}]}')[0])
ml["pid"] = w.kernel.spawn("mission: loop", kind="mission", parent=w._pid1, quota={"tokens": 10 ** 7}).pid
ml["state"] = MS.ACTIVE
w._missions[ml["id"]] = ml
w._mission_step(ml)
for _ in range(100):
    if ml["state"] != MS.ACTIVE:
        break
    time.sleep(0.1); settle()
check(MS.task(ml, "t1")["state"] == MS.FAILED and not (mproj / "loop.txt").exists(),
      "a task cut off at its step limit is not merged as if it were finished", MS.task(ml, "t1"))
w.settings["background_steps"] = 60
check(ml.get("spent_tokens", 0) > 0, "a mission's spend is saved as it goes (a restart keeps counting)")
bp_ = w.spawn_background("plain run, not a task")
w.kernel.wait(bp_, 10); settle()
fake_task = dict(w._bg[bp_], mission=("m1", "t1"), status="finished")
w._bg[9999] = fake_task
check("nothing to merge" == w.join_background(9999) or w._bg[9999]["status"] != "merged",
      "a mission's task can't be merged by hand past its test gate")
del w._bg[9999]
w.settings["model"] = "some/unpriced-model"
before = w._ledger.today()
w._account(4000, 400, usage={"prompt": 1000, "completion": 100, "cached": 0, "reasoning": 0},
           model="some/unpriced-model")
check(w._ledger.today() > before, "a model with no known price still counts against the budget")
w.settings["model"] = cn.DEFAULT_MODEL

print("\n--- the command palette (Ctrl+P) ---")
from chucknorris_ext import palette as PA
its = [{"title": "New mission…", "group": "Agent OS", "keywords": "plan big job"},
       {"title": "Missions", "group": "Agent OS"}, {"title": "Settings", "group": "Settings"},
       {"title": "Use DeepSeek-V4-Flash", "group": "Models", "keywords": "deepseek-ai/DeepSeek-V4-Flash"},
       {"title": "Kill 4 · survey the auth module", "group": "Processes"}]
check(PA.rank(its, "miss")[0]["title"] == "Missions" and PA.rank(its, "mission")[0]["title"] == "Missions",
      "a title that starts with what you typed comes first")
check(PA.rank(its, "nmis")[0]["title"].startswith("New mission"), "letters in order find it ('nmis')")
check(PA.rank(its, "big job")[0]["title"].startswith("New mission"), "keywords find it too")
check(PA.rank(its, "auth")[0]["title"].startswith("Kill 4"), "and words inside a title")
check(PA.rank(its, "zzzq") == [], "nothing that doesn't match")
w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.set_workspace(mproj, announce=False)
sp2 = w.kernel.spawn("survey the auth module", kind="subagent", parent=w._pid1)
items = w.palette_items()
titles = [i["title"] for i in items]
groups = {i["group"] for i in items}
check({"Agent OS", "Processes", "Programs", "Chat", "Code", "Folder", "Models", "Voice", "Settings"} <= groups,
      "the palette organises everything into groups", groups)
check(any(t.startswith(f"Kill {sp2.pid}") for t in titles) and any(t == "Run reviewer…" for t in titles)
      and any(t.startswith("Use ") for t in titles), "live processes, programs and models are in it")
it = next(i for i in items if i["title"] == "Activity: everything waiting and working")
w.palette_run(it)
check("ACTIVITY" in notes(), "running an item does it")
it = next(i for i in items if i["title"] == "New mission…")
w.palette_run(it)
check(w.entry.get_buffer().get_text() == "/mission ", "an item that needs more puts it in the composer to finish")
w.entry.get_buffer().set_text("")
kill = next(i for i in items if i["title"].startswith(f"Kill {sp2.pid}"))
w.palette_run(kill)
check(w.kernel.procs[sp2.pid].state == "killed", "killing an agent from the palette works")
check(w.open_palette() and w._palette_state["rows"]["rows"], "Ctrl+P opens it with rows to choose from")
ps = w._palette_state
ps["search"].set_text("diff")
ps["fill"]()
first = ps["rows"]["rows"][0]._item
check(first["title"] == "Show the diff", "typing filters it, best match first", first["title"])
ps["run"](ps["rows"]["rows"][0])
settle()
check(w._palette_win is None, "running a row closes it")

print("\n--- /watch: AI! comments from your own editor ---")
wproj = tmp / "watchproj"
wproj.mkdir()
(wproj / "calc.py").write_text("def total(items):\n    return sum(items)\n")
(wproj / "old.py").write_text("x = 1  # this was here before AI!\n")
w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.set_workspace(wproj, announce=False)
w.backend = ScriptedBackend(["Done: total() now skips None.", "Because sum() is O(n); fine here."])
check(w.watch_command("on") and w._watch is not None, "/watch starts watching the folder")
settle()
time.sleep(0.05)
(wproj / "calc.py").write_text("def total(items):   # make this skip None values AI!\n    return sum(items)\n")
os.utime(wproj / "calc.py", None)
w._watch_tick(w._watch); settle()
sent = [m["content"] for m in w.history if m.get("role") == "user"]
check(sent and "calc.py:1: make this skip None values" in sent[-1] and "REMOVE the AI! markers" in sent[-1]
      and "old.py" not in sent[-1],
      "saving an AI! comment sends it to the agent with its code around it (old markers don't fire)",
      sent[-1:] if sent else None)
n_before = len(w.history)
os.utime(wproj / "calc.py", None)
w._watch_tick(w._watch); settle()
check(len(w.history) == n_before, "the same comment isn't acted on twice")
(wproj / "calc.py").write_text("def total(items):\n    return sum(items)   # AI? why not a loop\n")
w._watch_tick(w._watch); settle()
sent = [m["content"] for m in w.history if m.get("role") == "user"]
check("answer the question(s)" in sent[-1] and "why not a loop" in sent[-1] and "don't change files" in sent[-1],
      "an AI? comment is a question, answered in the chat")
w._tray_tick()
check("\U0001F441" in w._tray_lbl.get_label(), "the tray shows it's watching")
w.set_workspace(mproj, announce=False)
check(w._watch is None, "switching folders stops watching the old one")

print("\n--- choose which files to merge ---")
sproj = tmp / "selproj"
sproj.mkdir()
(sproj / "keep.txt").write_text("k\n")
w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.set_workspace(sproj, announce=False)
w.backend = TimeBackend([])
sp3 = w.spawn_background("tt: two files")
w.kernel.wait(sp3, 20); settle()
check(w.show_bg_diff(sp3) and set(w._bg[sp3]["_merge_checks"]) == {"a.txt", "b.txt"},
      "the diff card has a check per changed file")
w._bg[sp3]["_merge_checks"]["b.txt"].set_active(False)
w.join_background(sp3, only=[r for r, c in w._bg[sp3]["_merge_checks"].items() if c.get_active()])
check((sproj / "a.txt").exists() and not (sproj / "b.txt").exists()
      and "left out as you chose" in " ".join(notes()),
      "merging only the checked files brings those in and leaves the rest out")

print("\n--- mission estimates that learn; /pr ---")
w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.set_workspace(mproj, announce=False)
w.settings.pop("mission_task_tokens", None)
w.backend = MissionBackend([])
w.backend.seen, w.backend.results = [], []
me = w.start_mission("estimate me")
settle()
info = me["_ui"]["info"].get_text()
tok0, _usd0 = w._mission_estimate(me)
check(me["state"] == MS.AWAITING and "Estimated: about" in info and tok0 == 180000 * 3 + 60000,
      "before Start, the card estimates what the plan will cost", info)
for t_ in me["tasks"]:
    t_["pid"] = w.kernel.spawn("x", parent=me["pid"]).pid
    w.kernel.charge(t_["pid"], 9000, 1000)
    t_["state"] = MS.MERGED if t_["kind"] == "code" else MS.DONE
w._mission_learn(me)
w._mission_learn(me)
tok1, _u = w._mission_estimate(me)
check(tok1 == 10000 * 4, "after missions run, estimates use what this machine's tasks really spent", tok1)
w.mission_action(me["id"], "cancel")
w.backend = ScriptedBackend(["On it."])
w.entry.get_buffer().set_text("/pr adds the parser tests")
w.on_send()
sent = [m["content"] for m in w.history if m.get("role") == "user"]
check(sent and "gh pr create" in sent[-1] and "adds the parser tests" in sent[-1],
      "/pr asks the agent for a branch, commit, push and a pull request")

print("\n--- /context: the context window's memory map ---")
w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.history += [{"role": "user", "content": "fix the parser"},
              {"role": "assistant", "content": "```read parser.py```"},
              {"role": "user", "content": "TOOL RESULTS (continue):\n\n" + "line\n" * 3000},
              {"role": "assistant", "content": "Fixed."}]
rows_, total_, budget_, _paged = w.context_map()
lab = dict(rows_)
check(lab.get("system prompt", 0) > 3000 and lab.get("tool results", 0) > 10000 and lab.get("your messages", 0) > 0
      and lab.get("replies", 0) > 0 and total_ == sum(lab.values()),
      "it accounts for every character the next call sends, by kind", rows_)
w.entry.get_buffer().set_text("/context")
w.on_send()
tx = [x.get_text() for x in w.msgbox.walk() if isinstance(x.get_text(), str)]
check("CONTEXT" in tx and any("The next model call sends" in t for t in tx), "/context shows it")

print("\n--- reminders ---")
now_ = time.mktime((2026, 3, 10, 10, 0, 0, 0, 0, -1))
check(D.describe(D.parse_when("at 9am", now_)) == "at Wed 11 Mar 09:00"
      and D.parse_when("in 20m", now_) == {"at": now_ + 1200}
      and D.describe(D.parse_when("tomorrow at 9:15", now_)) == "at Wed 11 Mar 09:15",
      "'in 20m', 'at 9am' (tomorrow, if 9am has passed) and 'tomorrow at 9:15' are understood")
w = new_win([], tmp)
w.set_agent_mode(False, announce=False)
w.backend = ScriptedBackend(["```remind in 20m: take the pizza out\n```\nWill do."])
type_and_send(w, "remind me in 20 minutes to take the pizza out")
rem = [d for d in w._daemons.items if d.get("kind") == "remind"]
check(rem and rem[-1]["brief"] == "take the pizza out" and "at" in rem[-1]["when"],
      "the agent sets a reminder, even in Chat mode", rem[-1:] if rem else None)
spoken = []
real_speak = cn.speak
cn.speak = lambda t, s=None: spoken.append(t)
w.settings["budget_day_usd"] = 0.000001
w._ledger.add(0.01)
rem[-1]["when"]["at"] = time.time() - 1
w._daemon_probe = FakeProbe()
w._daemon_tick(); settle()
cn.speak = real_speak
w.settings["budget_day_usd"] = 0.0
check(any("Reminder: take the pizza out" in t for t in notes()) and spoken
      and not any(d.get("kind") == "remind" and d["brief"] == "take the pizza out" for d in w._daemons.items)
      and any(i["report"] == "take the pizza out" for i in w._inbox.items),
      "when it's due: a note, the inbox, aloud, and gone (even over budget: it costs nothing)")
r2 = w.remind_command("every day at 8:30: stand-up")
check(r2 and r2["when"] == {"daily": "08:30"}, "a reminder can repeat")
check(w.remind_command("whenever: x") is None, "a time it can't read is refused with examples")

print("\n--- the shell: the running OS from a terminal ---")
w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.backend = ScriptedBackend(["Looking at it."])
said = []
check(w.remote_command(["--help"], None, said.append) == 0 and "chucknorris --mission" in said[-1],
      "chucknorris --help prints what it can do")
check(w.remote_command(["--here", "fix", "the", "tests"], str(sproj), said.append) == 0
      and w.workspace == sproj.resolve() and said[-1] == "sent", "--here sets the folder, then the message is sent")
settle()
check(any(m.get("content") == "fix the tests" for m in w.history), "the message arrives as if typed")
check(w.remote_command(["--remind", "in", "20m:", "tea"], None, said.append) == 0 and said[-1] == "done"
      and any(d["brief"] == "tea" for d in w._daemons.items), "--remind sets a reminder")
w.remote_command(["--top"], None, said.append)
check("chuck" in said[-1], "--top prints the process table in the terminal", said[-1][:100])
check(w.remote_command(["--bogus"], None, said.append) == 2, "an unknown option is an error with a hint")

print("\n--- review fixes: palette keys, /watch, partial merge, /context ---")
w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.set_workspace(sproj, announce=False)
w.open_palette()
ps = w._palette_state
ps["search"].set_text("diff")
ps["fill"]()
ps["search"].emit("activate")                 # Enter, as the search entry reports it
settle()
check(w._palette_win is None and any("git repository" in t or t.startswith("GIT DIFF") for t in notes()),
      "Enter in the palette's search box runs the selected row (the entry no longer swallows it)")
w.open_palette()
w._palette_state["search"].emit("stop-search")  # Esc
check(w._palette_win is None, "and Esc closes it")
from chucknorris_ext import aiwatch as AW
t0w = time.time()
check(AW.match("a;" * 40000 + "EMAIL AI!") is None and AW.match("a;" * 700 + " # EMAIL AI!", ".py")
      and time.time() - t0w < 0.5, "a huge minified line can't freeze /watch")
check(AW.match("See http://example.com/AI!", ".py") is None and AW.match("* Powered by AI!", ".md") is None
      and AW.match("# Powered by AI!", ".md") is None and AW.match("<!-- fix this AI! -->", ".md"),
      "URLs, Markdown bullets and headings aren't instructions; an HTML comment in Markdown is")
(wproj / "calc.py").write_text("def total(items):\n    return sum(items)\n")
w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.set_workspace(wproj, announce=False)
w.backend = ScriptedBackend(["ok", "ok", "ok"])
w.watch_command("on"); settle()
w.entry.get_buffer().set_text("my half-written message")
(wproj / "calc.py").write_text("x = 1  # AI!\ny = 2\n")
w._watch_tick(w._watch); settle()
check(w.entry.get_buffer().get_text() == "my half-written message" and w._watch.get("pending"),
      "an AI comment never overwrites what you're typing: it waits")
w.entry.get_buffer().set_text("")
w._watch_tick(w._watch); settle()
sent = [m["content"] for m in w.history if m.get("role") == "user"]
check(sent and "calc.py:1" in sent[-1], "and is sent once the draft is gone")
(wproj / "calc.py").write_text("x = 1\ny = 2\n")
w._watch_tick(w._watch); settle()
(wproj / "calc.py").write_text("x = 1  # AI!\ny = 2\n")
n0 = len(w.history)
w._watch_tick(w._watch); settle()
check(len(w.history) > n0, "a comment removed and written again is acted on again")
w.watch_command("off")
w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.set_workspace(sproj, announce=False)
(sproj / "a.txt").unlink(missing_ok=True); (sproj / "b.txt").unlink(missing_ok=True)
w.backend = TimeBackend([])
sp4 = w.spawn_background("tt: two files again")
w.kernel.wait(sp4, 20); settle()
w.join_background(sp4, only=["a.txt"])
check((sproj / "a.txt").exists() and not (sproj / "b.txt").exists() and w._bg[sp4]["ws"].work.is_dir()
      and w._bg[sp4]["changes"] == {"b.txt": "added"}, "after a partial merge, what you left out stays in its copy")
w.join_background(sp4)
check((sproj / "b.txt").exists(), "and can still be merged later")
w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.backend = ScriptedBackend([])
w.settings["auto_compact"] = True
w.history += [{"role": "user", "content": "go"}] + [
    m_ for i in range(30) for m_ in ({"role": "assistant", "content": f"```read f{i}.py```"},
                                    {"role": "user", "content": "TOOL RESULTS (continue):\n\n" + "x" * 20000})]
w.context_map()
check(not w._compacting and not any("trimmed" in t for t in notes()),
      "/context only looks: it never starts a (paid) compaction or posts notes")

print("\n--- no commands needed: Chuck runs the OS for you ---")
w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
w.set_workspace(mproj, announce=False)
check("never ask them to type a command" in w.history[0]["content"] and "```chuck <command>```" in w.history[0]["content"],
      "the agent is told it runs the OS and how to choose between its tools")
sp5 = w.kernel.spawn("survey the auth module", kind="subagent", parent=w._pid1)
w.backend = ScriptedBackend(["```chuck top```", f"```chuck kill {sp5.pid}```", "Stopped it."])
type_and_send(w, "what are my agents doing? stop the auth survey")
h = " ".join(str(m.get("content")) for m in w.history)
check("survey the auth" in h and w.kernel.procs[sp5.pid].state == "killed",
      "'what are my agents doing / stop that one' works in plain words (the agent uses ```chuck```)")
w.settings["budget_day_usd"] = 2.0
w.backend = ScriptedBackend(["```chuck budget day 50```", "Asked you."])
type_and_send(w, "raise my budget to 50 a day")
check(w.settings["budget_day_usd"] == 2.0 and "BUDGET" in notes(),
      "raising a spending limit is never the agent's call: it asks the user with one click")
w._pending_confirm[0](True)
check(w.settings["budget_day_usd"] == 50.0, "(and Allow applies it)")
w.backend = ScriptedBackend(["```chuck budget day 1```", "Done."])
type_and_send(w, "lower my budget to 1 a day")
check(w.settings["budget_day_usd"] == 1.0, "lowering a limit is applied at once")
w.settings["budget_day_usd"] = 0.0
w.settings.pop("mission_task_tokens", None)
w.settings["mission_auto_usd"] = 50.0
w.backend = MissionBackend(["```mission build a and b```\nOn it.", "ok"])
w.backend.seen, w.backend.results = [], []
type_and_send(w, "build a and b with tests")
settle()
mm_ = sorted(w._missions.values(), key=lambda x: -x["created"])[0]
check(mm_["state"] in (MS.ACTIVE, MS.COMPLETE, MS.PAUSED) and "Started by itself" in mm_["notes"],
      "a mission the agent starts runs by itself when its estimate is under the auto-start limit",
      (mm_["state"], mm_["notes"][-120:]))
w.mission_action(mm_["id"], "cancel")
w.settings["mission_auto_usd"] = 0.0
w.backend = MissionBackend(["```mission build a and b again```\nOn it.", "ok"])
w.backend.seen, w.backend.results = [], []
type_and_send(w, "again")
settle()
mm2 = sorted(w._missions.values(), key=lambda x: -x["created"])[0]
check(mm2["state"] == MS.AWAITING and "Waiting for Start" in mm2["notes"],
      "and waits for one click when it isn't", mm2["state"])
w.mission_action(mm2["id"], "cancel")
w.backend = ScriptedBackend(["```daemon daily 08:30: summarise new journal errors```", "Set."])
n_d = len(w._daemons.items)
type_and_send(w, "every morning tell me what went wrong in the journal")
check(len(w._daemons.items) == n_d + 1 and "DAEMON" in notes(),
      "a daemon that only looks is created straight away")
w._daemon_undo()
check(len(w._daemons.items) == n_d, "and Undo takes it back")
# the chuck tool is no back door around the approvals
n_d = len(w._daemons.items)
w.backend = ScriptedBackend(["```chuck daemon act every day at 3am: update the dependencies```", "Proposed."])
type_and_send(w, "update my deps every night")
check(len(w._daemons.items) == n_d and "DAEMON \u00b7 PROPOSED" in notes(),
      "a daemon that acts, asked for through ```chuck```, is only proposed (same as the daemon tool)")
w.backend = MissionBackend(["```mission build a and b once more```\nOn it.", "ok"])
w.backend.seen, w.backend.results = [], []
type_and_send(w, "once more")
settle()
mm3 = sorted(w._missions.values(), key=lambda x: -x["created"])[0]
w.backend = ScriptedBackend([f"```chuck mission start {mm3['id'][-5:]}```", "Asked."])
type_and_send(w, "go")
check(mm3["state"] == MS.AWAITING and "MISSION" in notes(),
      "a mission over the auto-start limit isn't started through ```chuck``` either: the user gets a Start card",
      mm3["state"])
w.mission_action(mm3["id"], "cancel")
w.set_agent_mode(False, announce=False)
w.backend = ScriptedBackend(["That needs to run on your machine.\n```agent update my system safely```", "Updating."])
type_and_send(w, "update my system")
check("AGENT MODE" in notes() and not w.agent_mode, "Chat mode offers a one-click handoff instead of 'press Tab'")
w._pending_confirm[0](True)
settle()
check(w.agent_mode and any(m.get("content") == "update my system safely" for m in w.history),
      "one click and it's done in Agent mode")

print(f"\nTOTAL OS FAILURES: {fails}")
assert fails == 0 and not FAILS, "OS regressions present"
print("ALL OS TESTS PASSED")
