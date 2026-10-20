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
print("\n--- retry and edit ---")
import gtkstub
w = agent(["```bash\necho step-one\n```", "Answer A1.", "Answer A2."])
type_and_send(w, "question one")
w.retry_last()
settle()
h = hist_text(w)
check("Answer A2." in h and "Answer A1." not in h and "step-one\n" not in h.split("question one")[-1].split("Answer A2.")[0],
      "retry re-asks from the question: the old answer and its tool results are gone")
users = [m for m in w.history if w._is_real_user(m)]
check(len(users) == 1, "the question itself is kept once", len(users))
bots = [e["data"]["text"] for e in w._log if e.get("kind") == "bot"]
check(bots and bots[-1] == "Answer A2." and not any("A1" in b for b in bots),
      "the transcript shows only the new answer", bots)
check_finished(w, "retry")

w = agent(["First answer.", "Second answer.", "Edited answer."])
type_and_send(w, "first question")
type_and_send(w, "second question with a typo")
check(w.edit_last() and w._get_entry() == "second question with a typo",
      "edit brings the last message back into the composer")
w.entry.get_buffer().set_text("second question, fixed")
w.on_send()
settle()
contents = [m["content"] for m in w.history[1:]]
check(contents == ["first question", "First answer.", "second question, fixed", "Edited answer."],
      "sending the edit replaces the original and everything after it", contents)
w._on_key(None, gtkstub.GdkNS.KEY_Up, 0, 0)
check(w._get_entry() == "second question, fixed", "↑ in an empty composer starts editing")
w._on_key(None, gtkstub.GdkNS.KEY_Escape, 0, 0)
check(w._get_entry() == "" and w._edit_at is None, "Esc cancels the edit")
w.backend.replies = ["Another."]
type_and_send(w, "third")
check(w.history[-2]["content"] == "third" and len(w.history) == 7, "a normal send after that is unaffected")

w = agent(["Hi."])
type_and_send(w, "msg with a note")
w._compact = {"upto": 2, "text": "S" * 50}
w.retry_last()
settle()
check(w._compact is None, "rewinding past a summary drops it (it described turns that are gone)")
w2 = agent([])
check(w2.retry_last() is False and w2.edit_last() is False, "nothing to retry or edit in an empty chat")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- git: /diff, /commit, /review ---")
import subprocess
ws = folder("gitrepo")
g = lambda *a: subprocess.run(["git", *a], cwd=ws, capture_output=True, text=True)
g("init", "-q"); g("config", "user.email", "t@example.com"); g("config", "user.name", "Test")
(ws / "app.py").write_text("print('v1')\n")
g("add", "-A"); g("commit", "-qm", "init")
(ws / "app.py").write_text("print('v2')\n")
(ws / "new.txt").write_text("fresh\n")
w = agent([], ws)
calls = len(w.backend.calls)
w.entry.get_buffer().set_text("/diff")
w.on_send()
card = [x for x in w.msgbox.walk() if x.get_text() == "WORKING TREE"]
check(card, "/diff shows a WORKING TREE card")
texts = [x.get_text() for x in w.msgbox.walk() if isinstance(x.get_text(), str)]
check(any(t == "2 changed" for t in texts), "with the number of changed files", [t for t in texts if "changed" in t])
check(any(" M app.py" in t for t in texts) and any("?? new.txt" in t for t in texts),
      "listing modified and untracked files")
check(len(w.backend.calls) == calls, "without calling the model")
w.backend.replies = ["```bash\ngit add app.py && git commit -qm 'Bump the greeting to v2' && git log -1 --stat\n```",
                     "Committed app.py; left new.txt out."]
w.entry.get_buffer().set_text("/commit")
w.on_send()
settle()
first_user = [m["content"] for m in w.history if w._is_real_user(m)][-1]
check(first_user.startswith("Commit the work:"), "/commit asks the agent to commit properly")
check("Bump the greeting to v2" in g("log", "-1", "--format=%s").stdout, "and the commit is made")
check("?? new.txt" in g("status", "--short").stdout, "an unrelated file was left out")
w.backend.replies = ["No bugs found in the diff."]
w.entry.get_buffer().set_text("/review")
w.on_send()
settle()
check([m["content"] for m in w.history if w._is_real_user(m)][-1].startswith("Review the uncommitted"),
      "/review asks for a review that changes nothing")
w2 = agent([], folder("notgit"))
w2.entry.get_buffer().set_text("/diff")
w2.on_send()
notes = [x.get_text() for x in w2.msgbox.walk() if isinstance(x.get_text(), str)]
check(any("isn't a git repository" in t for t in notes), "outside a repo, /diff says so")
check_finished(w, "git")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- hooks: your commands around the agent's work ---")
ws = folder("hooks")
upper = sys.executable + " -c \"import sys;p=sys.argv[1];t=open(p).read();open(p,'w').write(t.upper())\" {path}"
w = agent(["```write a.py\nprint('hi')\n```\n```write notes.md\nlower case notes\n```",
           "```changes```", "Done."], ws)
w.settings["hook_after_write"] = upper
w.settings["hook_after_write_glob"] = "*.py"
type_and_send(w, "write files")
h = hist_text(w)
check((ws / "a.py").read_text() == "PRINT('HI')\n", "the after-write hook ran on the matching file")
check((ws / "notes.md").read_text() == "lower case notes\n", "and not on files outside its pattern")
check("it changed the file; re-read it" in h, "the model is told the hook changed the file")
check("+PRINT('HI')" in h, "and the checkpoint follows disk (changes shows the formatted file)")
check_finished(w, "after-write hook")

w = agent(["```write b.py\nprint(1)\n```", "Done."], ws)
w.settings["hook_after_write"] = "false {path}"
type_and_send(w, "write b")
check("after-write hook on b.py: FAILED (exit 1)" in hist_text(w), "a failing after-write hook is reported")

ws2 = folder("hooks2")
w = agent(["```write a.py\nprint(1)\n```", "All done.",            # verify nudge: run it
           "```bash\npython3 a.py\n```", "All done.",              # hook fails: no flag
           "```bash\ntouch done.flag\n```", "Now it's done."], ws2)
w.settings["hook_after_write"] = ""
w.settings["hook_before_done"] = "test -f done.flag || { echo 'missing done.flag'; exit 1; }"
type_and_send(w, "make it so")
h = hist_text(w)
check("before-finish hook `test -f done.flag" in h and "missing done.flag" in h,
      "the before-finish hook catches an unfinished task and tells the model")
check((ws2 / "done.flag").exists() and len(w.backend.calls) == 6,
      "the agent fixes it; the hook passes and the turn ends", len(w.backend.calls))
check_finished(w, "before-done hook")

w = agent(["Just answering, no files."], ws2)
w.settings["hook_before_done"] = "false"
type_and_send(w, "a question")
check(len(w.backend.calls) == 1 and "before-finish" not in hist_text(w),
      "no file changes: the before-finish hook doesn't run")

w = agent(["```write c.py\nprint(1)\n```"] + ["Done."] * 10, ws2)
w.settings["hook_before_done"] = "false"
type_and_send(w, "stubborn")
check(hist_text(w).count("before-finish hook `false` FAILED") == 3 and not w._running,
      "a hook that never passes stops after three rounds", hist_text(w).count("FAILED"))
w.settings["hook_before_done"] = ""
w.settings["hook_after_write"] = ""

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- reopening a chat keeps what the machine did ---")
w = agent(["```bash\necho reopened-output\n```", "It printed."])
type_and_send(w, "print something")
w._save_chat()
path = cn.CHATS_DIR / f"{w.chat_id}.json"
w2 = new_win([], tmp)
w2._load_chat(path)
folds = [e for e in w2._log if e.get("kind") == "fold"]
check(folds and "reopened-output" in folds[0]["data"]["text"],
      "a reopened chat shows its tool results, folded", [e.get("kind") for e in w2._log])
check([e.get("kind") for e in w2._log if e.get("kind") != "note"][:3] == ["user", "fold", "bot"],
      "in order: your message, what ran, the answer", [e.get("kind") for e in w2._log])
w.backend.replies = ["Again."]
w.retry_last()
settle()
check(not any(e.get("kind") == "fold" and "reopened-output" in e["data"]["text"] for e in w._log),
      "a retry rewinds that turn's results too (they belonged to the old answer)")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- /doctor and the crash log ---")
os.environ["XDG_STATE_HOME"] = str(tmp / "state")
log = cn._crashlog
check(log.log_path() == tmp / "state" / "chucknorris" / "chuck.log", "the log lives under XDG_STATE_HOME")
check(log.recent() == [], "no log yet: nothing recent")
log.record("tool bash", "Traceback (most recent call last):\n  File x\nValueError: boom one")
log.record("thread worker", "Traceback\nKeyError: 'two'")
r = log.recent()
check([k for _w, k, _l in r] == ["thread worker", "tool bash"] and r[1][2] == "ValueError: boom one",
      "recent() reads entries back newest first, with the last traceback line", r)
old = log.log_path().read_text().replace(time.strftime("%Y-%m-%d"), "2001-01-01")
log.log_path().write_text(old)
check(log.recent() == [], "entries older than a week are not 'recent'")
log.log_path().write_text("x" * (log.MAX_BYTES + 10))
log.record("big", "after rotate")
check(log.log_path().with_suffix(".log.1").exists() and log.log_path().stat().st_size < 200,
      "the log rotates past 1 MB, keeping one old copy")
log.log_path().unlink()
ws = folder("doctor")
w = agent(["```changes```", "Done."], ws)


def explode(*a, **k):
    raise ValueError("tool exploded on purpose")


real = cn._TOOL_REG["changes"]["fn"]
try:
    cn._TOOL_REG["changes"]["fn"] = explode
    type_and_send(w, "what changed")
finally:
    cn._TOOL_REG["changes"]["fn"] = real
logged = log.log_path().read_text() if log.log_path().exists() else ""
check("tool exploded on purpose" in logged and f"v{cn._config.VERSION}" in logged,
        "a tool that raises is written to the crash log with its traceback", logged[:300])
calls = len(w.backend.calls)
w.entry.get_buffer().set_text("/doctor")
w.on_send()
check([x for x in w.msgbox.walk() if x.get_text() == "DOCTOR"], "/doctor shows a DOCTOR card")
texts = [x.get_text() for x in w.msgbox.walk() if isinstance(x.get_text(), str)]
for name in ("API key", "Model", "Working folder", "Python linter", "Disk", "Errors (7 days)", "API"):
    check(name in texts, f"with a {name} row")
check(any(t.endswith("to look at") or t == "all good" for t in texts), "and a verdict pill")
rows = w.doctor_checks()
check(all(st in ("ok", "warn", "fail") for st, _n, _d in rows), "every row has a state")
check(dict((n, st) for st, n, _d in rows)["API key"] == "ok", "a set key reads ok")
errs = [d for st, n, d in rows if n == "Errors (7 days)"]
check(errs and "recent" in errs[0] and "tool exploded" in errs[0], "logged errors show up in the doctor", errs)
check(len(w.backend.calls) == calls, "without calling the model")
settle()
check_finished(w, "doctor")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- update check and /update ---")
import io
import shutil
import tarfile
up = cn._update
check(up.parse_version('X = 1\nVERSION = "15.2.0"\n') == "15.2.0", "reads VERSION from a config file")
check(up.is_newer("15.10.0", "15.9.9") and not up.is_newer("15.1.0", "15.1.0")
      and not up.is_newer("15.0.9", "15.1.0") and not up.is_newer(None, "1.0"),
      "compares versions as numbers, not strings")
st = tmp / "upstate" / "update.json"
check(up.due(st), "never checked: a check is due")
up.mark_checked(st, "15.2.0", now=1000)
check(not up.due(st, now=1000 + 3600) and up.due(st, now=1000 + 25 * 3600), "then once a day")
check(up.verify_tree(cn.HERE) == cn._config.VERSION, "this source tree verifies as complete")


def src_copy(name, version, breaker=None):
    """A copy of this repo's app files as GitHub's archive lays them out."""
    root = tmp / name / "ChuckNorris-main"
    (root / "chucknorris_ext").mkdir(parents=True)
    shutil.copy(cn.HERE / "chucknorris.py", root / "chucknorris.py")
    for f in (cn.HERE / "chucknorris_ext").glob("*.py"):
        shutil.copy(f, root / "chucknorris_ext" / f.name)
    cfg = root / "chucknorris_ext" / "config.py"
    cfg.write_text(cfg.read_text().replace(f'VERSION = "{cn._config.VERSION}"', f'VERSION = "{version}"'))
    (root / "assets").mkdir()
    (root / "assets" / "new-art.png").write_bytes(b"png")
    if breaker:
        breaker(root)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(root, arcname="ChuckNorris-main")
    return buf.getvalue()


class Opener:
    def __init__(self, archive=b"", config=""):
        self.archive, self.config, self.urls = archive, config, []

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        if req.full_url == up.ARCHIVE_URL:
            return io.BytesIO(self.archive)
        if req.full_url == up.CONFIG_URL:
            return io.BytesIO(self.config.encode())
        raise OSError("unexpected url " + req.full_url)


def old_install(name):
    app = tmp / name / "app"
    (app / "chucknorris_ext").mkdir(parents=True)
    (app / "chucknorris.py").write_text("OLD = 1\n")
    (app / "chucknorris_ext" / "config.py").write_text('VERSION = "1.0.0"\n')
    return app


check(up.remote_version(Opener(config='VERSION = "99.0.0"\n')) == (True, "99.0.0"),
      "the remote version is read from GitHub's config.py")
check(up.remote_version(Opener(config="nothing"))[0] is False, "a page with no version is not a version")
app = old_install("up_ok")
ok, msg, v = up.apply(app, tmp / "up_ok" / "assets", opener=Opener(src_copy("src_ok", "99.0.0")))
check(ok and v == "99.0.0", "/update installs the new version", msg)
check((app / "chucknorris.py").read_text() == (cn.HERE / "chucknorris.py").read_text()
      and 'VERSION = "99.0.0"' in (app / "chucknorris_ext" / "config.py").read_text(),
      "the app and its package are replaced")
check(not (app / "chucknorris_ext.old").exists() and not (app / "chucknorris_ext.new").exists()
      and not (app / "chucknorris.py.new").exists(), "and no staging copies are left behind")
check((tmp / "up_ok" / "assets" / "new-art.png").exists(), "its art is installed too")
for label, brk, why in [
        ("a module missing", lambda r: (r / "chucknorris_ext" / "net.py").unlink(), "missing modules"),
        ("a file that doesn't compile", lambda r: (r / "chucknorris_ext" / "web.py").write_text("def (:\n"),
         "doesn't compile")]:
    app = old_install("up_bad" + str(len(label)))
    ok, msg, v = up.apply(app, None, opener=Opener(src_copy("src_bad" + str(len(label)), "99.0.0", brk)))
    check(not ok and why in msg and "nothing was changed" in msg, f"a download with {label} is refused", msg)
    check((app / "chucknorris.py").read_text() == "OLD = 1\n"
          and (app / "chucknorris_ext" / "config.py").read_text() == 'VERSION = "1.0.0"\n',
          "and the installed copy is untouched")
app = old_install("up_dl")
ok, msg, v = up.apply(app, None, opener=Opener(b"not a tarball"))
check(not ok and (app / "chucknorris.py").read_text() == "OLD = 1\n", "a corrupt archive changes nothing", msg)

# a git clone is pulled, never overwritten
origin = tmp / "origin"
origin.mkdir()
gg = lambda d, *a: subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True)
src = tmp / "src_git"
shutil.copytree(cn.HERE / "chucknorris_ext", origin / "chucknorris_ext",
                ignore=shutil.ignore_patterns("__pycache__"))
shutil.copy(cn.HERE / "chucknorris.py", origin / "chucknorris.py")
gg(origin, "init", "-q", "-b", "main"); gg(origin, "config", "user.email", "t@e.com")
gg(origin, "config", "user.name", "T"); gg(origin, "add", "-A"); gg(origin, "commit", "-qm", "v1")
clone = tmp / "clone"
subprocess.run(["git", "clone", "-q", str(origin), str(clone)], capture_output=True)
cfg = origin / "chucknorris_ext" / "config.py"
cfg.write_text(cfg.read_text().replace(f'VERSION = "{cn._config.VERSION}"', 'VERSION = "99.1.0"'))
gg(origin, "commit", "-qam", "bump")
ok, msg, v = up.apply(clone, None)
check(ok and v == "99.1.0", "a git clone is updated with a fast-forward pull", msg)
cfg.write_text(cfg.read_text().replace('VERSION = "99.1.0"', 'VERSION = "99.2.0"'))
gg(origin, "commit", "-qam", "bump2")
(clone / "chucknorris_ext" / "config.py").write_text("# my local edit\n" + cfg.read_text())
gg(clone, "commit", "-qam", "local")
ok, msg, v = up.apply(clone, None)
check(not ok and "nothing was changed" in msg and "# my local edit" in
      (clone / "chucknorris_ext" / "config.py").read_text(), "local commits are never overwritten", msg)

# the window side
w = agent([])
real_rv, real_apply = up.remote_version, up.apply
calls = []
try:
    up.remote_version = lambda *a, **k: (True, cn._config.VERSION)
    w.UPDATE_STATE = tmp / "upstate" / "w.json"
    w.run_update()
    settle()
    texts = [x.get_text() for x in w.msgbox.walk() if isinstance(x.get_text(), str)]
    check("UPDATE" in texts and "up to date" in texts, "/update on the newest version says so", texts[-6:])
    up.remote_version = lambda *a, **k: (True, "99.0.0")
    up.apply = lambda *a, **k: (calls.append(a), (True, "installed v99.0.0", "99.0.0"))[1]
    w.entry.get_buffer().set_text("/update")
    w.on_send()
    settle()
    texts = [x.get_text() for x in w.msgbox.walk() if isinstance(x.get_text(), str)]
    check(calls and calls[0][0] == cn.HERE, "/update installs into the folder Chuck runs from")
    check("v99.0.0 ready" in texts and any("Restart Chuck to use it" in t for t in texts),
          "and says a restart picks it up", texts[-6:])
    check(any(isinstance(x, cn.Gtk.Button) and x.get_label() == "Restart now" for x in w.msgbox.walk()),
          "with a Restart now button")
    n = len(calls)
    w.run_update()
    settle()
    check(len(calls) == n, "a second /update before restarting doesn't download again")
    w._update_ready = None
    up.apply = lambda *a, **k: (False, "download failed (URLError) \u2014 nothing was changed", None)
    w.run_update()
    settle()
    texts = [x.get_text() for x in w.msgbox.walk() if isinstance(x.get_text(), str)]
    check("failed" in texts and any("nothing was changed" in t for t in texts), "a failed update says so")
    toasts = []
    w._update_toast = lambda v: toasts.append(v)
    os.environ.pop("CHUCK_NO_UPDATE_CHECK", None)
    w.UPDATE_STATE = tmp / "upstate" / "daily.json"          # never checked
    w.settings["check_updates"] = False
    w._update_check()
    settle()
    check(not toasts, "the daily check can be turned off")
    w.settings["check_updates"] = True
    w._update_check()
    settle()
    check(toasts == ["99.0.0"], "the daily check finds a newer version", toasts)
    toasts.clear()
    w._update_check()
    settle()
    check(not toasts, "and asks GitHub at most once a day")
finally:
    os.environ["CHUCK_NO_UPDATE_CHECK"] = "1"
    up.remote_version, up.apply = real_rv, real_apply
check_finished(w, "update")
w = agent(["Remember me."])
type_and_send(w, "a chat worth keeping")
execd = []
real_execv = os.execv
os.execv = lambda exe, argv: execd.append(argv)
try:
    w.restart_app()
finally:
    os.execv = real_execv
check(execd and execd[0][-1].endswith("chucknorris.py") and os.environ.get("CHUCK_REOPEN") == w.chat_id
      and (cn.CHATS_DIR / f"{w.chat_id}.json").exists(),
      "Restart now saves the chat and starts a fresh Chuck", execd)
shown = []
real_present = cn.ChuckWindow.present
cn.ChuckWindow.present = lambda self: shown.append(self)
try:
    cn.ChuckApp().do_activate()
finally:
    cn.ChuckWindow.present = real_present
check(shown and shown[0].chat_id == w.chat_id
      and any(e.get("kind") == "user" for e in shown[0]._log) and "CHUCK_REOPEN" not in os.environ,
      "and the restarted Chuck reopens that chat", shown and shown[0].chat_id)

# ═══════════════════════════════════════════════════════════════════════════
print(f"\nTOTAL V16 FAILURES: {fails + len(FAILS)}")
assert fails == 0 and not FAILS, "v16 regressions present"
print("ALL V16 TESTS PASSED")
