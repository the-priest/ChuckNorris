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
lt = lambda t: time.struct_time((2026, 1, 2, int(t // 3600) % 24, int(t // 60) % 60, int(t) % 60, 4, 2, 0))
pr.localtime = staticmethod(lt)
day = {"when": {"daily": "08:30"}, "last_run": 0}
check(D.check(day, 8 * 3600 + 29 * 60, pr)[0] is False and D.check(day, 8 * 3600 + 31 * 60, pr)[0] is True,
      "a daily daemon fires once the time has come")
day["last_run"] = 8 * 3600 + 31 * 60
check(D.check(day, 20 * 3600, pr)[0] is False, "and not again the same day")
check(D.check({"when": {"daily": "08:30"}, "last_run": 0}, 15 * 3600, pr)[0] is True,
      "a run missed while the computer was off happens when it's back")
pr.localtime = staticmethod(time.localtime)
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
check(rop("curl x", strict=True) and rop("echo x > f", strict=True) and rop("echo $(id)", strict=True)
      and not rop("curl x"), "without a sandbox only read-only-by-nature commands run")
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
w.backend = DaemonBackend(["```daemon\ndaily 08:30: summarise the journal's new errors\n```\nProposed.", "ok"])
w.backend.seen, w.backend.results = [], []
n0 = len(w._daemons.items)
type_and_send(w, "watch my journal every morning")
check("DAEMON · PROPOSED" in notes() and len(w._daemons.items) == n0,
      "the agent can only PROPOSE a daemon: nothing exists until the user allows it")
w._pending_daemon[0](True)
check(len(w._daemons.items) == n0 + 1 and w._daemons.items[-1]["when"] == {"daily": "08:30"},
      "Allow creates it")
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

print(f"\nTOTAL OS FAILURES: {fails}")
assert fails == 0 and not FAILS, "OS regressions present"
print("ALL OS TESTS PASSED")
