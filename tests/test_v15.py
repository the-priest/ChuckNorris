import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""v15 — the roadmap in docs/ROADMAP.md. One section per item, each driven
through the REAL window on the stub toolkit (or the module that owns it)."""
import json
import tempfile
import threading
import time
from pathlib import Path

from sim import cn, new_win, type_and_send, settle, check_finished, check_history, FAILS

fails = 0


def check(cond, label, detail=""):
    global fails
    if cond:
        print(f"  [OK]   {label}")
    else:
        fails += 1
        print(f"  [FAIL] {label}  {str(detail)[:300]}")


tmp = Path(tempfile.mkdtemp(prefix="chuck_v15_"))
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


# ═══════════════════════════════════════════════════════════════════════════
print("\n--- registry: one place to add a tool ---")
calls = []


@cn.tool("probe_ro", agent_only=False, cap=2)
def _probe_ro(self, body):
    calls.append(("ro", body))
    return f"[probe_ro saw {body}]"


@cn.tool("probe_async", acts=True)
def _probe_async(self, body):
    calls.append(("async", body))
    return lambda: (time.sleep(0.2), f"[probe_async done {body}]")[1]


@cn.tool("probe_see", after_actions=True)
def _probe_see(self, body):
    calls.append(("see", body))
    return "[probe_see looked]"


@cn.tool("probe_boom")
def _probe_boom(self, body):
    raise RuntimeError("kaboom")


w = agent(["```probe_ro one``` ```probe_ro two``` ```probe_ro three```", "Done."])
type_and_send(w, "probe")
check([c for c in calls if c[0] == "ro"] == [("ro", "one"), ("ro", "two")],
      "a registered tool is parsed and capped", calls)
check("[probe_ro saw one]" in hist_text(w), "its result reaches the model")
check_finished(w, "registry sync")

calls.clear()
w = agent(["```probe_async x```\n```probe_see y```", "Done."])
type_and_send(w, "async and see")
check(("async", "x") in calls and "[probe_async done x]" in hist_text(w),
      "an async tool runs on a worker and its result comes back")
check(("see", "y") not in calls and "probe_see y held back" in hist_text(w),
      "an observing tool beside an action is held back, and the model told")
check_finished(w, "registry async")

calls.clear()
w = new_win(["```probe_ro chat``` ```probe_async nope```", "Done."], tmp)
w.set_agent_mode(False, announce=False)
type_and_send(w, "chat mode")
check(("ro", "chat") in calls and ("async", "nope") not in calls,
      "Chat mode runs only tools that aren't agent-only")
check_finished(w, "registry chat")

w = agent(["```probe_boom```\n```probe_boom x```", "It failed."])
type_and_send(w, "boom")
check("[probe_boom failed: kaboom]" in hist_text(w), "a handler that raises is a result, not a hang")
check_finished(w, "registry raise")
for t in ("probe_ro", "probe_async", "probe_see", "probe_boom"):
    cn._TOOL_REG.pop(t, None)

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- changes: the agent reviews its own work ---")
ws = folder("changes")
(ws / "a.py").write_text("x = 1\n")
w = agent(["```write b.py\nprint('new')\n```\n```edit a.py\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n"
           ">>>>>>> REPLACE\n```", "```edit a.py\n<<<<<<< SEARCH\nx = 2\n=======\nx = 3\n"
           ">>>>>>> REPLACE\n```", "```changes```", "Reviewed."], ws)
type_and_send(w, "change things")
h = hist_text(w)
check("CHANGES (2 file(s))" in h, "both changed files are listed once", h[-600:])
check("-x = 1" in h and "+x = 3" in h and "+x = 2" not in h.split("CHANGES")[-1],
      "edits to one file are shown net: first before, last after")
check("b.py (new, +1 -0)" in h, "a new file is marked new")
w.backend.replies = ["```changes```", "Nothing yet."]
type_and_send(w, "next task")
check("no file changes in this task yet" in hist_text(w), "a new task starts with no changes")
w.backend.replies = ["```changes all```", "All."]
type_and_send(w, "whole chat")
check(hist_text(w).count("CHANGES (2 file(s))") == 2, "```changes all``` covers the whole chat")
check_finished(w, "changes")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- project instructions: CHUCK.md / AGENTS.md / CLAUDE.md ---")
ws = folder("rules")
(ws / "AGENTS.md").write_text("Always use tabs. Run `make check` before finishing.\n")
(ws / "CLAUDE.md").write_text("I am second in line and must not load.\n")
w = agent(["Ok."], ws)
type_and_send(w, "hi")
sysmsg = w.history[0]["content"]
check("Run `make check` before finishing." in sysmsg and "AGENTS.md" in sysmsg,
      "the folder's AGENTS.md is in the system prompt")
check("second in line" not in sysmsg, "only the first instruction file found is used")
(ws / "CHUCK.md").write_text("CHUCK rules win.\n")
w.backend.replies = ["Ok."]
type_and_send(w, "again")
check("CHUCK rules win." in w.history[0]["content"], "an edited/added file is picked up on the next message")
first_payload = json.loads(w.backend.payloads[-1])["messages"][0]["content"]
check("CHUCK rules win." in first_payload, "and it rides in the cached system prefix, not the per-hop note")
cn.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
(cn.CONFIG_DIR / "CHUCK.md").write_text("Global: answer in British English.\n")
w.set_workspace(None, announce=False)
check("British English" in w.history[0]["content"] and "CHUCK rules win" not in w.history[0]["content"],
      "global rules apply everywhere; folder rules leave with the folder")
(cn.CONFIG_DIR / "CHUCK.md").unlink()
(ws / "CHUCK.md").write_text("x" * 50000)
w.set_workspace(ws, announce=False)
check(len(w.history[0]["content"]) < len(cn.SYSTEM_PROMPT) + 16000,
      "a huge instruction file is capped")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- @file mentions ---")
ws = folder("mentions")
(ws / "src").mkdir()
(ws / "src" / "app.py").write_text("SECRET_APP_LINE = 7\n")
(ws / "notes.txt").write_text("NOTE_LINE\n")
w = agent(["Got them."], ws)
type_and_send(w, "look at @src/app.py and @notes.txt, then mail me@example.com about @missing.py.")
u = next(m for m in w.history if m["role"] == "user")
c = json.dumps(u["content"])
check("SECRET_APP_LINE" in c and "NOTE_LINE" in c, "mentioned files are attached with their contents")
check("example.com" not in [a.get("name") for a in w._attachments], "an e-mail address is not a mention")
check(not w._attachments, "the tray is cleared after sending")
w2 = agent(["Ok."])
w2.set_workspace(None, announce=False)
type_and_send(w2, "what is @src/app.py")
check("SECRET_APP_LINE" not in json.dumps(w2.history), "with no folder, relative mentions resolve to nothing")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- the shell remembers cd ---")
ws = folder("shellcd")
(ws / "sub" / "deeper").mkdir(parents=True)
w = agent(["```bash\ncd sub/deeper && pwd\n```", "```bash\npwd\n```", "Done."], ws)
type_and_send(w, "move around")
h = hist_text(w)
check(h.count(str((ws / "sub" / "deeper").resolve())) >= 2,
      "the second command runs where the first one cd'd to", h[-500:])
check("The shell is now in" in h, "the model is told the shell left the working folder")
w.backend.replies = ["```bash\nfalse_cmd_xyz; cd /\n```", "```bash\npwd\n```", "Ok."]
type_and_send(w, "fail then cd")
check("\n/\n" in hist_text(w) or "output:\n/" in hist_text(w).replace("Real output:\n", "output:\n"),
      "a cd at the end of a failing script still carries")
w.set_workspace(ws, announce=False)
w.backend.replies = ["```bash\npwd\n```", "Ok."]
type_and_send(w, "reset")
check(hist_text(w).rstrip().split("pwd")[-1].count(str(ws.resolve())) >= 1,
      "setting the working folder resets the shell to it")
w.backend.replies = ["```bash\necho one\nbogus_command_qq\n```", "Ok."]
type_and_send(w, "where is the error")
check("line 2: bogus_command_qq" in hist_text(w),
      "bash error line numbers are unchanged by the cd tracking", hist_text(w)[-300:])

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- slash commands ---")
ws = folder("slash")
w = agent([], ws)
calls_before = len(w.backend.calls)
for cmd in ("/help", "/chat", "/cost", "/model deepseek-v4.1", "/folder " + str(ws)):
    w.entry.get_buffer().set_text(cmd)
    w.on_send()
check(len(w.backend.calls) == calls_before, "slash commands never call the model")
check(not w.agent_mode, "/chat switches mode")
check("DeepSeek-V4.1-Flash" in (w.settings.get("model") or ""), "/model matches part of a name",
      w.settings.get("model"))
w.entry.get_buffer().set_text("/agent")
w.on_send()
check(w.agent_mode, "/agent switches back")
w.backend.replies = ["```write u.txt\nhello\n```", "Wrote it."]
type_and_send(w, "write u")
w.entry.get_buffer().set_text("/undo")
w.on_send()
check(not (ws / "u.txt").exists(), "/undo reverts the last file change")
w.backend.replies = ["It is a path."]
type_and_send(w, "/etc/fstab looks broken")
check(len(w.backend.calls) > calls_before, "a message that just starts with a path is sent normally")
w.backend.replies = ["```write CHUCK.md\n# Conventions\n```", "Wrote CHUCK.md."]
w.entry.get_buffer().set_text("/init")
w.on_send()
settle()
check((ws / "CHUCK.md").exists(), "/init has the agent write CHUCK.md")
check("# Conventions" in w.history[0]["content"], "which is then loaded as the folder's rules")
w.entry.get_buffer().set_text("/clear")
w.on_send()
check(len(w.history) == 1, "/clear starts a new chat")
check_finished(w, "slash")

# ═══════════════════════════════════════════════════════════════════════════
print(f"\nTOTAL V15 FAILURES: {fails + len(FAILS)}")
assert fails == 0 and not FAILS, "v15 regressions present"
print("ALL V15 TESTS PASSED")
