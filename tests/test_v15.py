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
w2 = agent(["```bash\necho hi\n```", "Ok."])
w2.set_workspace(None, announce=False)
type_and_send(w2, "no folder, no cd")
check("The shell is now in" not in hist_text(w2), "no cd, no 'shell moved' note (even with no folder)")
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
print("\n--- browse: a real headless browser the agent drives ---")
from chucknorris_ext import browser as br
check(br.to_url("localhost:8000/x") == "http://localhost:8000/x", "host:port becomes http://")
check(br.to_url("https://a.b/c") == "https://a.b/c", "a URL passes through")
try:
    br.to_url("nope.html", str(tmp))
    check(False, "a missing file is an error")
except FileNotFoundError:
    check(True, "a missing file is an error")
if br.find_chrome():
    ws = folder("browse")
    (ws / "index.html").write_text(
        "<!doctype html><title>Game</title><p id=s>0</p><button id=b>Go</button>"
        "<script src='gone.js'></script><script>let n=0;"
        "b.onclick=()=>{n+=10;s.textContent=n};"
        "addEventListener('keydown',e=>{if(e.key==='ArrowLeft'){n+=1;s.textContent=n}});"
        "setTimeout(()=>{brokenCall()},50);</script>")
    w = agent(["```browse index.html 800x600\nclick #b\npress ArrowLeft ArrowLeft\n"
               "eval document.getElementById('s').textContent\n```", "Fixing the error."], ws)
    type_and_send(w, "check the game")
    h = hist_text(w)
    check("BROWSE file://" in h and "(800x600" in h, "the page loads at the requested size")
    check("Uncaught ReferenceError: brokenCall is not defined (index.html:1)" in h,
          "an uncaught exception is reported with file and line", h[-900:])
    check("gone.js: net::ERR_FILE_NOT_FOUND" in h, "a missing script is a failed request")
    check(h.count("ERR_FILE_NOT_FOUND") == 1, "reported once, not twice")
    check('→ "12"' in h, "clicks and key presses really reach the page (score 12)")
    check("IMAGE of index.html in the browser is attached" in h, "the screenshot reaches the model")
    check_finished(w, "browse")
    w = agent(["```bash\necho building\n```\n```browse index.html```", "Ok."], ws)
    type_and_send(w, "build and browse")
    check("browse index.html held back" in hist_text(w),
          "browse beside a command waits until the command has run")
    w = agent(["```browse missing.html```", "Ok."], ws)
    type_and_send(w, "browse nothing")
    check("[browse: no such file: missing.html]" in hist_text(w), "a missing page is a clear result")
    check_finished(w, "browse missing")
else:
    print("  [SKIP] no Chromium here — browser behaviour untested")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- browser choice: Brave first, Camoufox on request ---")
import types
from contextlib import contextmanager
bindir = folder("fakebin")
for name in ("brave", "chromium"):
    f = bindir / name
    f.write_text("#!/bin/sh\nexit 0\n"); f.chmod(0o755)
old_path, old_env = os.environ.get("PATH", ""), os.environ.pop("CHROME_BIN", None)
os.environ["PATH"] = str(bindir) + os.pathsep + old_path
try:
    check(br.resolve_engine("auto") == ("cdp", str(bindir / "brave")),
          "Automatic prefers Brave when it's installed", br.resolve_engine("auto"))
    check(br.resolve_engine("chromium") == ("cdp", str(bindir / "chromium")),
          "an explicit choice is honoured")
    (bindir / "brave").unlink()
    try:
        br.resolve_engine("brave")
        check(False, "asking for Brave when it isn't installed is a clear error")
    except RuntimeError as ex:
        check("Brave isn't installed" in str(ex), "asking for Brave when it isn't installed is a clear error")
    check(br.resolve_engine("auto")[0] == "cdp", "Automatic falls back to another Chromium-family browser")
    check(br.resolve_engine(str(bindir / "chromium")) == ("cdp", str(bindir / "chromium")),
          "a path to a browser binary works")
finally:
    os.environ["PATH"] = old_path
    if old_env:
        os.environ["CHROME_BIN"] = old_env

# a stand-in Camoufox (its real binary can't be downloaded in CI); the page
# speaks the Playwright API and reports errors the way FIREFOX does
camo_calls = []


class FakePage:
    def __init__(self, vp):
        self.vp, self.handlers, self.keys = vp, {}, []

    def on(self, ev, fn):
        self.handlers[ev] = fn

    def goto(self, url, **kw):
        L = types.SimpleNamespace
        self.handlers["console"](L(type="error", text="boom from console",
                                   location={"url": url, "lineNumber": 11}))
        self.handlers["console"](L(type="error", text="Failed to load resource: x",
                                   location={"url": url, "lineNumber": 1}))
        self.handlers["console"](L(type="warning", text="careful", location={}))
        self.handlers["pageerror"](L(name="ReferenceError", message="scroe is not defined",
                                     stack="draw@" + url + ":25:7\n@" + url + ":30:1"))
        self.handlers["requestfailed"](L(url=url.replace("index.html", "gone.js"),
                                         failure="NS_ERROR_FILE_NOT_FOUND"))
        self.handlers["response"](L(url="http://x/api", status=404))

    def wait_for_timeout(self, ms): pass
    def query_selector(self, sel): return None if sel == "#nope" else object()
    def click(self, sel, **kw): camo_calls.append(("click", sel))
    def evaluate(self, js): return "SCORE 12" if "score" in js else "page text"
    def title(self): return "Snake"
    def set_viewport_size(self, vp): self.vp = vp
    def screenshot(self, path): Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")

    @property
    def keyboard(self):
        page = self
        return types.SimpleNamespace(press=lambda k: page.keys.append(k), type=lambda t: None)

    @property
    def mouse(self):
        return types.SimpleNamespace(click=lambda x, y: None, wheel=lambda a, b: None)


class FakeCamoufox:
    def __init__(self, **kw):
        camo_calls.append(("launch", kw))

    def __enter__(self):
        return types.SimpleNamespace(new_page=lambda viewport: FakePage(viewport))

    def __exit__(self, *a):
        camo_calls.append(("closed",))


mod = types.ModuleType("camoufox"); sub = types.ModuleType("camoufox.sync_api")
sub.Camoufox = FakeCamoufox; mod.sync_api = sub
saved_mods = {k: sys.modules.get(k) for k in ("camoufox", "camoufox.sync_api")}
sys.modules["camoufox"], sys.modules["camoufox.sync_api"] = mod, sub
try:
    ws = folder("camo")
    (ws / "index.html").write_text("<p>hi</p>")
    r = br.browse("index.html", ["press ArrowLeft right space", "eval document.getElementById('score').textContent",
                                 "click #nope", "click #go"], root=str(ws),
                  shot_path=str(tmp / "camo.png"), engine="camoufox", settle=0)
    rep = br.report(r)
    check(("launch", {"headless": True}) in camo_calls and ("closed",) in camo_calls,
          "Camoufox is launched headless and always closed", camo_calls)
    check("Camoufox)" in rep, "the report says which browser ran", rep[:120])
    check("Uncaught ReferenceError: scroe is not defined (index.html:25)" in rep,
          "a Firefox-style stack still yields file and line", rep)
    check("console.error: boom from console (index.html:12)" in rep, "console errors keep their line")
    check("Failed to load resource" not in rep and "gone.js: NS_ERROR_FILE_NOT_FOUND" in rep
          and "http://x/api: HTTP 404" in rep, "failed requests and HTTP errors are reported once")
    check('→ "SCORE 12"' in rep and "click #nope: FAILED: no element matches" in rep
          and "click #go: ok" in rep, "actions run through Playwright, failures are results")
    check(r["shot"] and Path(r["shot"]).exists(), "the screenshot is written")
    camo_calls.clear()
    br.browse("index.html", [], root=str(ws), engine="/opt/camoufox/camoufox-bin", settle=0)
    check(("launch", {"headless": True, "executable_path": "/opt/camoufox/camoufox-bin"}) in camo_calls,
          "a path to a Camoufox binary is passed through")
    w = agent(["```browse index.html```", "Seen."], ws)
    w.settings["browser"] = "camoufox"
    type_and_send(w, "check it with camoufox")
    check("Camoufox)" in hist_text(w) and "scroe is not defined" in hist_text(w),
          "the browse tool uses the browser chosen in Settings")
    check_finished(w, "camoufox browse")
finally:
    for k, v in saved_mods.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None
if sync_playwright and br.find_chrome():
    @contextmanager
    def real_pw():
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=br.find_chrome(), headless=True)
            try:
                yield b
            finally:
                b.close()
    ws = folder("realpw")
    (ws / "index.html").write_text("<title>T</title><p id=s>0</p><script>let n=0;"
                                   "addEventListener('keydown',e=>{n++;s.textContent=n});"
                                   "setTimeout(()=>{nope()},30)</script>")
    r = br.browse_playwright("index.html", ["press ArrowLeft ArrowRight", "eval s.textContent"],
                             (800, 600), str(ws), None, 0.4, 30, real_pw)
    check(any("nope is not defined" in e for e in r["errors"]) and '→ "2"' in " ".join(r["steps"]),
          "the Playwright path works against a real browser", r["errors"])
else:
    print("  [SKIP] Playwright not installed — real Playwright path untested here")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- diagnose: real problems from real tools ---")
ws = folder("diag")
(ws / "pkg").mkdir()
(ws / "pkg" / "a.py").write_text("def f():\n    return undefined_thing\n")
(ws / "pkg" / "b.py").write_text("def broken(:\n    pass\n")
(ws / "ok.py").write_text("print('fine')\n")
(ws / "conf.json").write_text('{"a": 1,}\n')
(ws / "node_modules").mkdir()
(ws / "node_modules" / "junk.py").write_text("this is not python(\n")
w = agent(["```diagnose```", "```diagnose ok.py```", "Found them."], ws)
type_and_send(w, "check the project")
h = hist_text(w)
check("pkg/a.py:2" in h and "undefined_thing" in h, "an undefined name is found with file:line", h[-800:])
check("pkg/b.py:1" in h, "a syntax error is found")
check("conf.json:1:" in h and "trailing comma" in h.lower(), "broken JSON is found with its position")
check("node_modules" not in h.split("DIAGNOSE")[1], "dependency folders are skipped")
check("DIAGNOSE ok.py: checked 1 python\nNo problems found." in h, "a clean file is reported clean")
check_finished(w, "diagnose")
w = agent(["```diagnose```", "Ok."])
w.set_workspace(None, announce=False)
type_and_send(w, "no folder")
check("no working folder" in hist_text(w), "with no folder it says so")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- patch: multi-file unified diffs, all-or-nothing ---")
ws = folder("patch")
(ws / "app.py").write_text("def greet(name):\n    return 'hi ' + name\n\n\nprint(greet('x'))\n")
(ws / "README.md").write_text("# App\n\n```bash\npython3 app.py\n```\n")
(ws / "old.txt").write_text("remove me\n")
diff = """```patch
--- a/app.py
+++ b/app.py
@@ -90,2 +90,2 @@
 def greet(name):
-    return 'hi ' + name
+    return f'Hello, {name}!'
--- a/README.md
+++ b/README.md
@@ -3,3 +3,4 @@
 ```bash
 python3 app.py
+touch PWNED_BY_PATCH
 ```
--- /dev/null
+++ b/lib/util.py
@@ -0,0 +1,2 @@
+def double(x):
+    return 2 * x
--- a/old.txt
+++ /dev/null
@@ -1 +0,0 @@
-remove me
```"""
w = agent([diff, "```bash\npython3 app.py\n```", "Patched and ran."], ws)
type_and_send(w, "refactor")
h = hist_text(w)
check("Hello, {name}!" in (ws / "app.py").read_text(), "a hunk with wrong line numbers lands by its context")
check("touch PWNED_BY_PATCH" in (ws / "README.md").read_text() and not (ws / "PWNED_BY_PATCH").exists(),
      "a diff touching a README's ``` lines applies, and its content never runs")
check((ws / "lib" / "util.py").read_text() == "def double(x):\n    return 2 * x\n", "a new file is created")
check(not (ws / "old.txt").exists(), "a deleted file is removed")
check("patch applied to 4 file(s)" in h, "the model hears what was applied")
check("Hello, x!" in h, "and the patched code runs")
check(len([x for x in w.msgbox.walk() if x.get_label() == "Revert"]) == 4, "each file got a Revert card")

before = {f: (ws / f).read_text() for f in ("app.py", "README.md")}
bad = """```patch
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def greet(name):
-    return f'Hello, {name}!'
+    return 'changed'
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-# A heading that is not there
+# New
```"""
w.backend.replies = [bad, "It didn't apply."]
type_and_send(w, "bad patch")
check({f: (ws / f).read_text() for f in before} == before,
      "one hunk that doesn't match changes NOTHING, in any file")
check("patch NOT applied, nothing was changed" in hist_text(w) and "README.md: hunk 1" in hist_text(w),
      "and the model is told which hunk failed")
(ws / "twice.py").write_text("one\ntwo\nthree\n")
w.backend.replies = ["```patch\n--- a/twice.py\n+++ b/twice.py\n@@ -1 +1 @@\n-one\n+ONE\n"
                     "--- a/twice.py\n+++ b/twice.py\n@@ -3 +3 @@\n-three\n+THREE\n```", "Ok."]
type_and_send(w, "same file twice")
check((ws / "twice.py").read_text() == "ONE\ntwo\nTHREE\n",
      "a diff naming the same file twice applies both sections")
check(len([x for x in w.msgbox.walk() if x.get_label() == "Revert"]) == 5,
      "and checkpoints it once (Revert restores the original)")
w.backend.replies = ["```changes all```", "Ok."]
type_and_send(w, "review all")
check("old.txt (deleted" in hist_text(w), "changes reports a deleted file as deleted")
check_finished(w, "patch")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- live output: long commands stream into their card ---")
import gtkstub
got, t0 = [], time.time()
cn._LIVE_OUT.fn = lambda c: got.append((time.time() - t0, c))
rc, out = cn.run_command("for i in 1 2 3; do echo tick$i; sleep 0.25; done; printf 'caf\\xc3'; printf '\\xa9\\n'")
cn._LIVE_OUT.fn = None
check(rc == 0 and out.startswith("tick1\ntick2\ntick3"), "the full output is still returned", out)
check(len(got) >= 3 and got[0][0] < 0.2 and got[-1][0] > 0.5,
      "chunks arrive while the command runs, not at the end", [(round(a, 2), b) for a, b in got])
check("café" in "".join(c for _, c in got), "a UTF-8 character split across writes decodes whole")
rc, out = cn.run_command("echo quiet")
check(out.strip() == "quiet", "without a live sink, commands run as before")
w = agent([])
sink = gtkstub.Widget()
feed = cn._LiveFeed(w, sink)
feed.push("line one\n")
feed.push("line two\n")
check(not sink.kids, "nothing is drawn until the first refresh tick")
gtkstub.fire_timers(1)
check(len(sink.kids) == 1 and "term-live" in sink.kids[0].classes, "then one LIVE panel appears in the card")
texts = [x.get_text() for x in sink.walk() if isinstance(x.get_text(), str)]
check(any("line one\nline two" == t for t in texts), "showing the output so far", texts)
feed.push("x" * 20000)
gtkstub.fire_timers(1)
texts = [x.get_text() for x in sink.walk() if isinstance(x.get_text(), str)]
check(any(t.endswith("x" * 100) for t in texts) and all(len(t) <= cn._LiveFeed.TAIL for t in texts),
      "only the newest 12k characters are kept", [len(t) for t in texts])
feed.close()
check(not sink.kids, "close() removes it (the full output panel replaces it)")
feed.push("late\n")
gtkstub.fire_timers(1)
check(not sink.kids, "output after close never resurrects it")
ws = folder("live")
w = agent(["```bash\nfor i in $(seq 1 5); do echo n$i; done\n```", "Done."], ws)
type_and_send(w, "count")
check(not [x for x in w.msgbox.walk() if "term-live" in x.classes], "no LIVE panel is left behind")
check("n5" in hist_text(w), "the model still gets the full output")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- MCP: every tool the ecosystem ships ---")
fake = Path(__file__).with_name("fake_mcp_server.py")
cn.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
(cn.CONFIG_DIR / "mcp.json").write_text(json.dumps({"mcpServers": {
    "fake": {"command": sys.executable, "args": [str(fake)]},
    "ghost": {"command": "no-such-binary-xyz"}}}))
ws = folder("mcpws")
w = agent([], ws)
for _ in range(100):
    if not w._mcp.loading and any(st == "running" for _, st, _ in w._mcp.status()):
        break
    time.sleep(0.1)
w._refresh_system()
sysmsg = w.history[0]["content"]
check("MCP TOOLS" in sysmsg and "- fake.add(a, b): Add two numbers." in sysmsg,
      "a configured server's tools are listed in the system prompt", sysmsg[-600:])
check("fake.slow(seconds?)" in sysmsg, "optional arguments are marked")
w.backend.replies = ['```mcp fake.add\n{"a": 20, "b": 22}\n```\n```mcp fake.pixel```\n'
                     '```mcp fake.where```', "```mcp fake.fail```\n```mcp fake.add\n{\"a\": 1}\n```\n"
                     '```mcp fake.echo {"text": "same line"}```', "Done."]
type_and_send(w, "use the tools")
h = hist_text(w)
check("[mcp fake.add OK]\n42" in h, "a tool call returns its result")
check("one pixel" in h and "IMAGE of fake.pixel image 1 is attached" in h, "an image result is shown and seen")
check(str(ws.resolve()) in h.split("fake.where OK]")[1][:300] if "fake.where OK]" in h else False,
      "a server asking for roots gets the working folder")
check("[mcp fake.fail FAILED]\nit broke on purpose" in h, "a tool error is reported as a failure")
check("is missing: b" in h, "missing required arguments are caught before the call")
check("ECHO: same line" in h, "arguments on the tool's own line work too")
check_finished(w, "mcp")
w.backend.replies = ["```mcp ghost.x```", "```mcp fake.add\nnot json\n```", "Ok."]
type_and_send(w, "bad calls")
h = hist_text(w)
check("failed to start: command not found: no-such-binary-xyz" in h, "a server that failed to start says why")
check("arguments aren't valid JSON" in h, "bad JSON is a clear result")
w.entry.get_buffer().set_text("/mcp")
w.on_send()
notes = [x.get_text() for x in w.msgbox.walk() if isinstance(x.get_text(), str) and "MCP servers" in x.get_text()]
check(notes and "fake" in notes[-1] and "running" in notes[-1] and "ghost" in notes[-1],
      "/mcp shows each server's state", notes[-1:] if notes else None)
w.set_agent_mode(False, announce=False)
check("MCP TOOLS" not in w.history[0]["content"], "Chat mode lists no MCP tools")
w.backend.replies = ['```mcp fake.add\n{"a": 1, "b": 1}\n```', "Ok."]
calls_before = hist_text(w).count("[mcp fake.add OK]")
type_and_send(w, "chat call")
check(hist_text(w).count("[mcp fake.add OK]") == calls_before, "and can't call them")
procs = [s.proc for s in w._mcp.servers.values()]
w._on_close()
time.sleep(0.5)
check(procs and all(p.poll() is not None for p in procs), "closing the window stops the servers")
(cn.CONFIG_DIR / "mcp.json").unlink()

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- model fallback: one model down never stops the task ---")
import http.server
import socketserver
seen_models = []


class API(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        m = body["model"]
        seen_models.append(m)
        if m == "busy/model":
            self.send_response(503); self.end_headers(); self.wfile.write(b'{"error":"overloaded"}')
            return
        if m == "gone/model":
            self.send_response(400); self.end_headers()
            self.wfile.write(b'{"code":20012,"message":"Model does not exist. Please check it."}')
            return
        if m == "bad/request":
            self.send_response(400); self.end_headers()
            self.wfile.write(b'{"message":"messages must not be empty"}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        for chunk in ({"choices": [{"delta": {"content": f"answer from {m}"}}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5}}):
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")


srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), API)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}/v1"


def run_backend(settings):
    b = cn.Backend(dict({"siliconflow_api_key": "k", "siliconflow_base_url": base}, **settings))
    out, err, sw = [], [], []
    b.on_fallback = lambda bad, fb, why: sw.append((bad, fb, why))
    b.stream([{"role": "user", "content": "hi"}], out.append, lambda: None, err.append,
             attempts=2)
    return b, "".join(out), err, sw


seen_models.clear()
b, out, err, sw = run_backend({"model": "busy/model", "fallback_model": "good/model"})
check(out == "answer from good/model" and not err, "an overloaded model falls back after its retries", (out, err))
check(seen_models == ["busy/model", "busy/model", "good/model"], "retries first, then falls back once", seen_models)
check(sw and sw[0][0] == "busy/model" and "trouble" in sw[0][2], "the switch is reported with the reason", sw)
check(b.last_model == "good/model", "the answering model is recorded (for billing)")
seen_models.clear()
b, out, err, sw = run_backend({"model": "gone/model", "fallback_model": "good/model"})
check(out == "answer from good/model" and seen_models == ["gone/model", "good/model"],
      "a model that no longer exists falls back at once, no pointless retries", seen_models)
seen_models.clear()
b, out, err, sw = run_backend({"model": "bad/request", "fallback_model": "good/model"})
check(err and not out and seen_models == ["bad/request"], "a bad REQUEST is not blamed on the model", seen_models)
seen_models.clear()
b, out, err, sw = run_backend({"model": "busy/model", "fallback_model": "none"})
check(err and seen_models == ["busy/model", "busy/model"], "'none' turns fallback off", seen_models)
seen_models.clear()
b, out, err, sw = run_backend({"model": "busy/model", "fallback_model": "busy/model"})
check(sw and sw[0][1] == cn._pricing.SUGGESTED[0] and out.startswith("answer from"),
      "a configured fallback that IS the failing model is skipped for the automatic one", sw)
bdead = cn.Backend({"siliconflow_api_key": "k", "siliconflow_base_url": "http://127.0.0.1:9/v1",
                    "model": "busy/model", "fallback_model": "good/model"})
fsw, ferr = [], []
bdead.on_fallback = lambda *a: fsw.append(a)
bdead.stream([{"role": "user", "content": "hi"}], lambda c: None, lambda: None, ferr.append, attempts=2)
check(ferr and not fsw, "a dead network doesn't trigger a fallback (no model can help)")
b = cn.Backend({"model": cn._pricing.SUGGESTED[0]})
check(b.fallback_for(cn._pricing.SUGGESTED[0]) == cn._pricing.SUGGESTED[1],
      "automatic fallback: the next suggested model")
srv.shutdown()

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- spending cap: pause and ask before spending more ---")
w = agent(["```bash\necho step\n```", "Done after continuing."])
w.settings["cost_cap"] = 0.10
w._usage["cost"] = 0.25                      # already past it
type_and_send(w, "keep working")
check(len(w.backend.calls) == 0, "no model call is made past the cap")
cards = [x for x in w.msgbox.walk() if x.get_text() == "SPENDING CAP"]
check(cards, "a SPENDING CAP card asks first")
check_finished(w, "cap paused")
go = [x for x in w.msgbox.walk() if (x.get_label() or "").startswith("Continue (another")]
check(go and "$0.10" in go[0].get_label(), "Continue offers another cap's worth", go[0].get_label() if go else None)
if go:
    go[0].click()
    settle()
check(len(w.backend.calls) >= 1 and "Done after continuing." in hist_text(w),
      "Continue resumes the task where it paused")
check(w._cap_extra >= 0.25, "and the allowance counts from what was already spent", w._cap_extra)
w._usage["cost"] = w._cap_extra + 0.10 + 0.01
w.backend.replies = ["never sent"]
type_and_send(w, "spend more")
check("never sent" not in hist_text(w), "spending past the new allowance pauses again")
stop = [x for x in w.msgbox.walk() if x.get_label() == "Stop here"]
stop[-1].click()
check("never sent" not in hist_text(w), "Stop here stays stopped")
ws = folder("capsub")
w3 = agent([], ws)
w3.settings["cost_cap"] = 0.10
w3._usage["cost"] = 0.0
sub_call = w3._sub_call()
w3._usage["cost"] = 0.50
try:
    sub_call([{"role": "system", "content": "x"}, {"role": "user", "content": "y"}])
    check(False, "a sub-agent can't spend past the cap")
except RuntimeError as ex:
    check("spending cap" in str(ex), "a sub-agent can't spend past the cap")
w2 = agent(["Free."])
w2.settings["cost_cap"] = 0
w2._usage["cost"] = 99
type_and_send(w2, "no cap")
check("Free." in hist_text(w2), "a cap of 0 means no cap")
w.new_chat()
check(w._cap_extra == 0, "a new chat starts with no approved overspend")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- compaction: long sessions keep their memory ---")
w = agent([])
for i in range(30):
    w.history += [{"role": "user", "content": f"request {i}: GOAL-{i} " + "x" * 300},
                  {"role": "assistant", "content": f"did {i}"}]
full = [dict(m) for m in w.history]
w.entry.get_buffer().set_text("/compact")
w.on_send()
settle()
check(w._compact and w._compact["upto"] == len(w.history) - cn.ChuckWindow.COMPACT_KEEP,
      "/compact summarises all but the newest messages", w._compact and w._compact["upto"])
check(w.history == full, "the transcript itself is untouched")
sent = w._trim_for_send(list(w.history))
check(sent[1]["content"].startswith("CONVERSATION SUMMARY") and "SUMMARY:" in sent[1]["content"],
      "what is sent opens with the summary")
check(len(sent) == 2 + cn.ChuckWindow.COMPACT_KEEP and sent[-1] == w.history[-1],
      "followed by the newest messages as they are", len(sent))
check(not any("GOAL-3 " in str(m.get("content")) for m in sent[2:]), "the covered turns are not re-sent")
w._save_chat()
w2 = new_win([], tmp)
w2._load_chat(cn.CHATS_DIR / f"{w.chat_id}.json")
check(w2._compact == w._compact, "the summary survives saving and reloading the chat")
w.entry.get_buffer().set_text("/compact")
w.on_send()
settle()
notes = [x.get_text() for x in w.msgbox.walk() if isinstance(x.get_text(), str)]
check(any("Nothing to compact yet." in t for t in notes), "nothing new: it says so instead of paying again")
w.new_chat()
check(w._compact is None, "a new chat has no summary")

w = agent([])
w.settings["context_chars"] = 40000
for i in range(40):
    w.history += [{"role": "user", "content": f"step {i} " + "y" * 1500},
                  {"role": "assistant", "content": f"ok {i}"}]
w.backend.replies = ["Carrying on."]
type_and_send(w, "keep going")
settle()
check(w._compact is not None, "past 80% of the context budget it compacts by itself")
check(getattr(w.backend, "compactions", 0) == 1, "once", getattr(w.backend, "compactions", 0))
w.backend.replies = ["Still here."]
type_and_send(w, "next")
check(json.loads(w.backend.payloads[-1])["messages"][1]["content"].startswith("CONVERSATION SUMMARY"),
      "and the next request carries the summary")
w.settings["auto_compact"] = False
w.settings["context_chars"] = 40000

w = agent([])
for i in range(20):
    w.history += [{"role": "user", "content": f"u{i}"}, {"role": "assistant", "content": f"a{i}"}]
w._cont_prefix = "half a reply"
check(w.compact_now() is False and w._compact is None,
      "no compaction starts in the middle of a reply that is being continued")
w._cont_prefix = None
w._compact = {"upto": 30, "text": "S" * 50}
w._cont_hist_at = 12
w._collapse_continuation("stitched")
check(len(w.history) == 12 and w._compact["upto"] == 12,
      "collapsing a continuation clamps the summary so it never hides newer messages",
      (len(w.history), w._compact))

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- export: a chat as Markdown ---")
w = agent(["Running it.\n```bash\necho exported-output\n```", "It printed the line."])
type_and_send(w, "print something")
w.entry.get_buffer().set_text("/export")
w.on_send()
outs = list((Path.home() / "Downloads").glob("chuck-*.md")) + list((cn.DATA_DIR / "exports").glob("chuck-*.md"))
check(outs, "/export writes a Markdown file")
md = outs[-1].read_text() if outs else ""
check(md.startswith("# print something"), "titled by the first request", md[:60])
check("## You\n\nprint something" in md and "## Chuck\n\nIt printed the line." in md,
      "user and Chuck turns are headed sections")
check("<details><summary>Tool results</summary>" in md and "exported-output" in md,
      "tool results are folded away, not lost")
check("You are Chuck" not in md, "the system prompt is never exported")

# ═══════════════════════════════════════════════════════════════════════════
print(f"\nTOTAL V15 FAILURES: {fails + len(FAILS)}")
assert fails == 0 and not FAILS, "v15 regressions present"
print("ALL V15 TESTS PASSED")
