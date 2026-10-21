import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)
# Language servers installed for the real user keep their caches under the
# real HOME; the suite's throwaway HOME must not hide them.
os.environ.setdefault("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))

"""v17 — the next gaps (docs/ROADMAP.md, phases 10-15). One section per
item, through the REAL window on the stub toolkit, against real language
servers where they're installed."""
import shutil
import tempfile
import time
from pathlib import Path

from sim import cn, new_win, type_and_send, settle, check_finished, FAILS, ScriptedBackend

fails = 0


def check(cond, label, detail=""):
    global fails
    if cond:
        print(f"  [OK]   {label}")
    else:
        fails += 1
        print(f"  [FAIL] {label}  {str(detail)[:300]}")


tmp = Path(tempfile.mkdtemp(prefix="chuck_v17_"))
cn._builder.PROJECTS = tmp / "projects"
cn._builder.PROJECTS.mkdir()
cn._builder._STATE = cn._builder.PROJECTS / ".current"
cn.ChuckWindow.JOB_DIR = tmp / "jobs"
lsp = cn._lsp


def hist_text(w):
    return " ".join(str(m.get("content", "")) for m in w.history)


def agent(replies, folder=None):
    w = new_win(replies, tmp)
    w.set_agent_mode(True, announce=False)
    if folder:
        w.set_workspace(folder, announce=False)
    return w


def project(name):
    d = tmp / name
    (d / "pkg").mkdir(parents=True)
    (d / "web").mkdir()
    (d / "pkg" / "__init__.py").write_text("")
    (d / "pkg" / "store.py").write_text(
        "class Store:\n    def save(self, item):\n        return item\n\n\n"
        "class Cache:\n    def save(self, item):\n        return None\n\n\n"
        "def make_store():\n    \"\"\"Build the default store.\"\"\"\n    return Store()\n")
    (d / "pkg" / "app.py").write_text(
        "from pkg.store import Store, make_store\n\ns = make_store()\ns.save(1)\nStore().save(2)\n"
        "# make_store in a comment is not a use\nLABEL = 'make_store'\n")
    (d / "pkg" / "net.py").write_text(
        "import time\n\n\ndef fetch_with_backoff(url, attempts=5):\n"
        "    \"\"\"Retry a failed request, waiting longer after each failure.\"\"\"\n"
        "    for n in range(attempts):\n        time.sleep(2 ** n)\n    return url\n\n\n"
        "def parse_header(line):\n    return line.split(':', 1)\n")
    (d / "web" / "util.ts").write_text(
        "export function formatPrice(n: number): string {\n  return \"$\" + n.toFixed(2);\n}\n")
    (d / "web" / "main.ts").write_text(
        "import { formatPrice } from \"./util\";\nconsole.log(formatPrice(3));\n"
        "const formatPriceLabel = \"formatPrice\";\n")
    return d


has_py = lsp.server_for("python") is not None
has_ts = lsp.server_for("typescript", None) is not None
print(f"(language servers here: python={'yes' if has_py else 'no'}, "
      f"typescript={'yes' if has_ts else 'no'})")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- def / refs ---")
ws = project("sym")
w = agent(["```def make_store```\n```refs Store.save```", "Found them."], ws)
type_and_send(w, "where is make_store and who calls Store.save?")
h = hist_text(w)
check("DEFINITIONS of make_store (1" in h and "pkg/store.py:11" in h, "def finds the definition", h[-800:])
check("REFERENCES to Store.save (2" in h and "pkg/app.py:4" in h and "pkg/app.py:5" in h,
      "refs finds both calls")
if has_py:
    check("via pyright" in h or "via basedpyright" in h or "via pylsp" in h or "via jedi" in h,
          "through the language server")
    check("Cache" not in h.split("REFERENCES to Store.save")[1][:400],
          "and by meaning: Cache.save's lines are not Store.save's uses")
check_finished(w, "def/refs")

w = agent(["```def save```", "Two of them."], ws)
type_and_send(w, "where is save")
h = hist_text(w)
check("DEFINITIONS of save (2" in h and "Store" in h and "Cache" in h,
      "a bare name lists every definition, with its class")

real_server_for = lsp.server_for
lsp.server_for = lambda *a, **k: None
try:
    w = agent(["```def make_store```\n```refs make_store```", "ok"], ws)
    type_and_send(w, "no server")
    h = hist_text(w)
    check("pkg/store.py:11" in h and "via syntax (no language server)" in h,
          "without a server, def still answers from the syntax and says so")
    check("text search (no language server" in h and "pkg/app.py:3" in h,
          "refs falls back to a whole-word search that says it is one")
    w = agent(["```rename make_store build_store```", "Couldn't."], ws)
    type_and_send(w, "rename without a server")
    check("rename NOT done" in hist_text(w) and "language server" in hist_text(w)
          and "make_store" in (ws / "pkg" / "app.py").read_text(),
          "rename refuses without a server, and changes nothing")
finally:
    lsp.server_for = real_server_for

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- rename ---")
if has_py:
    ws = project("ren")
    w = agent(["```rename make_store build_store```", "Renamed."], ws)
    type_and_send(w, "rename make_store")
    app, store = (ws / "pkg" / "app.py").read_text(), (ws / "pkg" / "store.py").read_text()
    check("def build_store():" in store and "import Store, build_store" in app
          and "s = build_store()" in app, "the definition and every use are renamed", app)
    check("# make_store in a comment" in app and "LABEL = 'make_store'" in app,
          "comments and strings are left alone (they're not uses)")
    h = hist_text(w)
    check("renamed make_store → build_store" in h and "2 file(s)" in h, "the agent is told what changed", h[-400:])
    edits = [c for c in w._changes if c["rel"] in ("pkg/app.py", "pkg/store.py")]
    check(len(edits) == 2, "each file is a checkpoint (revertible)", [c["rel"] for c in w._changes])
    w.backend.replies = ["```undo```", "```undo```", "Reverted."]
    type_and_send(w, "undo it")
    check("make_store" in (ws / "pkg" / "app.py").read_text()
          and "def make_store" in (ws / "pkg" / "store.py").read_text(), "undo puts it back, file by file")

    w = agent(["```rename save store```", "Ambiguous."], ws)
    before = (ws / "pkg" / "store.py").read_text()
    type_and_send(w, "rename save")
    check("defined 2 times" in hist_text(w) and (ws / "pkg" / "store.py").read_text() == before,
          "an ambiguous name is refused with the candidates, nothing changed")
    w = agent(["```rename Cache.save stash```", "Done."], ws)
    type_and_send(w, "rename Cache.save")
    st = (ws / "pkg" / "store.py").read_text()
    check("def stash(self, item):\n        return None" in st and "def save(self, item):\n        return item" in st,
          "Class.method picks one: Cache.save renamed, Store.save untouched", st)
    w = agent(["```rename make_store 9bad```", "No."], ws)
    type_and_send(w, "bad name")
    check("isn't a valid name" in hist_text(w), "an invalid new name is refused")
else:
    print("  [SKIP] no Python language server: rename untested here")

if has_ts:
    ws = project("ts")
    w = agent(["```refs formatPrice```\n", "```rename formatPrice fmtPrice```", "ok"], ws)
    type_and_send(w, "ts")
    main = (ws / "web" / "main.ts").read_text()
    check("import { fmtPrice }" in main and "console.log(fmtPrice(3))" in main
          and 'const formatPriceLabel = "formatPrice"' in main,
          "TypeScript: renamed by meaning, the look-alike name and string untouched", main)
else:
    print("  [SKIP] no TypeScript language server")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- find: code by what it does ---")
ws = project("find")
w = agent(["```find retry a request that failed```", "It's fetch_with_backoff."], ws)
type_and_send(w, "where do we retry?")
h = hist_text(w)
first = h.split("best matches by meaning")[1].strip().splitlines()[1] if "best matches by meaning" in h else ""
check("pkg/net.py:4" in first and "fetch_with_backoff" in first,
      "the function that retries ranks first, though no line says 'retry a request that failed'", first)
cs = cn._codesearch
check(cs.words("retryFailedRequests parse_json") == ["retry", "fail", "request", "parse", "json"],
      "camelCase and snake_case split into words, stemmed")
(ws / "pkg" / "late.py").write_text("def resend_on_timeout():\n    \"\"\"Retry when the request timed out.\"\"\"\n")
r = cs.search(ws, "retry request timed out", k=3)
check(r and r[0][1]["rel"] == "pkg/late.py", "a file added since the last search is found (incremental index)")
w = agent(["```find```", "ok"], ws)
type_and_send(w, "empty")
check("say what you're looking for" in hist_text(w), "an empty find asks what to look for")
w = agent(["```def```", "ok"], ws)
type_and_send(w, "empty def")
check("give a name" in hist_text(w), "an empty def says how to use it")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- plan mode and chat mode ---")
ws = project("plan")
w = agent(["```def make_store```\n```find build the store```", "```plan\n- [ ] rename it\n```\nPlan."], ws)
w.set_plan_mode(True)
type_and_send(w, "plan a rename")
h = hist_text(w)
check("DEFINITIONS of make_store" in h and "best matches by meaning" in h,
      "plan mode allows def and find (read-only)")
w = agent(["```rename make_store build_store```", "ok"], ws)
w.set_plan_mode(True)
type_and_send(w, "just do it")
check("def make_store" in (ws / "pkg" / "store.py").read_text(), "plan mode blocks rename (it changes files)")
w.set_plan_mode(False)

if getattr(w, "_lsp_m", None) is not None:
    w._on_close()
    time.sleep(1.5)
    check(all(c.dead for c in w._lsp_m.clients.values()) or not w._lsp_m.clients,
          "closing Chuck stops the language servers")
settle()

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- checkpoints and /rewind ---")
import subprocess
cn.ChuckWindow.CHECKPOINT_STORE = tmp / "cpstore"
ws = tmp / "cp"
ws.mkdir()
(ws / "a.txt").write_text("v1\n")
(ws / "keep.txt").write_text("keep\n")
g = lambda *a: subprocess.run(["git", "-C", str(ws), *a], capture_output=True, text=True)
g("init", "-q"); g("add", "-A"); g("-c", "user.email=t@e", "-c", "user.name=T", "commit", "-qm", "mine")
w = agent(["```bash\necho v2 > a.txt && echo made > gen.txt\n```",
           "```bash\nrm keep.txt && mkdir -p out/deep && echo x > out/deep/f.txt\n```",
           "Did it."], ws)
type_and_send(w, "change things with commands")
check(len(w._checkpoints) == 2 and w._checkpoints[0]["ask"] == "change things with commands",
      "a snapshot before each acting step", [c["label"] for c in w._checkpoints])
check(not (ws / "keep.txt").exists() and (ws / "gen.txt").exists(), "(the commands ran)")
w.entry.get_buffer().set_text("/rewind")
w.on_send()
check([x for x in w.msgbox.walk() if x.get_text() == "REWIND"], "/rewind shows the checkpoints")
texts = [x.get_text() for x in w.msgbox.walk() if isinstance(x.get_text(), str)]
check(any("2 files differ now" in t for t in texts) and any("4 files differ now" in t for t in texts),
      "each says how many files differ from it now", [t for t in texts if "differ" in t])
btns = [x for x in w.msgbox.walk() if isinstance(x, cn.Gtk.Button) and x.get_label() == "Rewind"]
btns[-1].emit("clicked")                  # oldest: before the first step
check((ws / "a.txt").read_text() == "v1\n" and (ws / "keep.txt").exists()
      and not (ws / "gen.txt").exists() and not (ws / "out").exists(),
      "rewinding restores changed and deleted files, removes created ones and their folders")
check(not any(m["role"] == "assistant" for m in w.history[1:]) and
      w._get_entry() == "change things with commands",
      "and the chat goes back too: your message waits in the composer", w._get_entry())
check(g("status", "--short").stdout == "" and "mine" in g("log", "--oneline").stdout
      and len(g("log", "--oneline").stdout.splitlines()) == 1, "your own git repo is untouched")
und = [c for c in w._checkpoints if c["label"] == "undo the rewind"]
check(len(und) == 1, "the rewind itself can be undone")
w.entry.get_buffer().set_text("")
w.rewind_to(und[0])
check((ws / "gen.txt").exists() and not (ws / "keep.txt").exists()
      and (ws / "out" / "deep" / "f.txt").exists() and "Did it." in hist_text(w),
      "undoing it brings back the files and the conversation")
cp2 = [c for c in w._checkpoints if c["label"].startswith("bash: rm keep.txt")]
check(len(cp2) == 1, "the later step's checkpoint is still there", [c["label"] for c in w._checkpoints])
w.rewind_to(cp2[0])
check((ws / "keep.txt").exists() and (ws / "gen.txt").exists() and (ws / "a.txt").read_text() == "v2\n",
      "rewinding to a later step keeps what earlier steps did")
check(w.history[-1]["role"] == "user" and "TOOL RESULTS" in w.history[-1]["content"]
      and w._get_entry() == "", "the chat ends at that step's input, ready to continue")
w._save_chat()
w2 = new_win([], tmp)
w2._load_chat(cn.CHATS_DIR / f"{w.chat_id}.json")
check(len(w2._checkpoints) == len(w._checkpoints), "checkpoints are saved with the chat")
w3 = agent(["```bash\necho busy\n```", "ok"], ws)
w3._running = True
check(w3.rewind_to(w._checkpoints[0]) is False, "no rewinding in the middle of a run")
w3._running = False
w4 = agent(["```bash\necho off > off.txt\n```", "ok"], ws)
w4.settings["checkpoints"] = False
type_and_send(w4, "no snapshots")
check(w4._checkpoints == [], "snapshots can be turned off in Settings")
w4.settings["checkpoints"] = True
w5 = agent(["```bash\necho hi\n```", "ok"], Path.home())
check(w5._checkpoint("x") is None, "your home folder is never snapshotted")
settle()

ck = cn._checkpoint
ws = tmp / "cp2"
(ws / "data").mkdir(parents=True)
(ws / "logs").mkdir()                           # empty when the snapshot is taken
(ws / ".gitignore").write_text("data/\n.env\n")
(ws / ".env").write_text("SECRET=1\n")
(ws / "data" / "big.bin").write_text("x" * 100)
(ws / "app.py").write_text("v1\n")
odd = "we\tird \u00e9 name.txt"
(ws / odd).write_text("odd\n")
sh = ck.Shadow(ws, tmp / "cpstore")
s0 = sh.snapshot("start")
(ws / ".gitignore").write_text("")              # a step un-ignores them...
(ws / "app.py").write_text("v2\n")
(ws / "gen.py").write_text("generated\n")
(ws / "logs" / "run.log").write_text("log\n")
(ws / odd).unlink()
sh.snapshot("step")                             # ...so this snapshot holds .env and data/
(sh.git_dir / "index.lock").write_text("")       # a git killed mid-way left its lock
before, changed = sh.restore(s0)
check((ws / ".env").read_text() == "SECRET=1\n" and (ws / "data" / "big.bin").exists(),
      "a rewind never deletes files that were ignored at the snapshot (.env, data/)")
check((ws / "app.py").read_text() == "v1\n" and not (ws / "gen.py").exists()
      and (ws / ".gitignore").read_text() == "data/\n.env\n",
      "changed files come back and created ones go, despite a stale git lock")
check((ws / odd).read_text() == "odd\n", "a file name with a tab and an accent is restored")
check((ws / "logs").is_dir() and not (ws / "logs" / "run.log").exists(),
      "a folder that was empty at the snapshot is kept")
lib = ws / "lib"
lib.mkdir()
subprocess.run(["git", "-C", str(lib), "init", "-q"])
(lib / "x.py").write_text("x\n")
subprocess.run(["git", "-C", str(lib), "add", "-A"])
subprocess.run(["git", "-C", str(lib), "-c", "user.email=t@e", "-c", "user.name=T", "commit", "-qm", "l"])
sh.snapshot("with lib")
check(sh.nested() == ["lib"], "a folder that's its own git repo is detected", sh.nested())
w6 = agent(["```bash\necho hi > hi.txt\n```", "ok"], ws)
type_and_send(w6, "touch something")
w6.entry.get_buffer().set_text("/rewind")
w6.on_send()
texts = [x.get_text() for x in w6.msgbox.walk() if isinstance(x.get_text(), str)]
check(any("lib is its own git repository" in t for t in texts),
      "/rewind warns that a nested repo isn't snapshotted", [t for t in texts if "repo" in t])
real_restore = ck.Shadow.restore


def half_restore(self, sha):
    raise ck.CheckpointError("disk full", before="f" * 40)


ck.Shadow.restore = half_restore
n = len(w6._checkpoints)
check(w6.rewind_to(w6._checkpoints[0]) is False and len(w6._checkpoints) == n + 1
      and w6._checkpoints[-1]["sha"] == "f" * 40 and w6._checkpoints[-1]["label"] == "undo the rewind",
      "a rewind that fails part-way still leaves a way back", w6._checkpoints[-1:])
ck.Shadow.restore = real_restore
w7 = agent(["```runtests\n```", "ok"], ws)
type_and_send(w7, "run the tests")
check(len(w7._checkpoints) == 1, "running the tests is snapshotted too (they can write files)")
store = tmp / "prune"
for name, folder, age in (("gone", tmp / "nope", 8), ("unplugged", tmp / "nope", 2), ("old", ws, 40),
                          ("fresh", ws, 1)):
    (store / name).mkdir(parents=True)
    (store / name / "chuck-folder").write_text(str(folder) + "\n")
    t = time.time() - age * 86400
    os.utime(store / name / "chuck-folder", (t, t))
(store / "stranger").mkdir()
check(ck.prune(store) == 2 and sorted(p.name for p in store.iterdir()) == ["fresh", "stranger", "unplugged"],
      "snapshots of folders gone a week and ones unused for 30 days are pruned",
      sorted(p.name for p in store.iterdir()))
settle()

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- folder access: sandboxed and read-only ---")
import socketserver, threading, subprocess
ws = tmp / "acc"
ws.mkdir()
# not under /tmp: the sandbox gives commands a private /tmp, which would hide
# it rather than show it read-only
outside = Path(tempfile.mkdtemp(prefix="chuck_v17_out_", dir="/var/tmp"))
(ws / "f.txt").write_text("orig\n")
srv = socketserver.TCPServer(("127.0.0.1", 0), socketserver.BaseRequestHandler)
threading.Thread(target=srv.serve_forever, daemon=True, name="srv-local").start()
port = srv.server_address[1]
probe = f"python3 -c \"import socket; socket.create_connection(('127.0.0.1', {port}), 2); print('NET-OK')\""
if shutil.which("bwrap"):
    w = agent([f"```bash\necho in > in.txt && echo out > {outside}/o.txt; echo rc=$?\n```",
               f"```bash\n{probe}\n```",
               "```bash\nsudo pacman -Syu\n```",
               "```bash\nmkdir -p sub && cd sub\n```", "```bash\npwd\n```",
               "Done."], ws)
    check(w.set_access("sandbox"), "a folder can be sandboxed")
    check(w._ws_lbl.get_label() == "acc \u00b7 offline", "the folder chip says so", w._ws_lbl.get_label())
    check("FOLDER ACCESS: sandboxed, offline" in w.history[0]["content"], "and so does the model's prompt")
    type_and_send(w, "try everything")
    h = hist_text(w)
    check((ws / "in.txt").exists() and not (outside / "o.txt").exists()
          and ("Read-only file system" in h or "No such file or directory" in h),
          "inside the folder writes work; outside it they fail", h[-1500:])
    check("output:\nNET-OK" not in h and ("unreachable" in h.lower() or "refused" in h.lower()),
          "offline: no network, not even to a local port")
    check("this folder is Sandboxed, so nothing can use sudo" in h, "sudo is refused up front")
    check(h.rstrip().count(str(ws / "sub")) >= 1 and str(ws / "sub") in h.split("pwd")[-1],
          "cd still carries between commands")
    w.set_access("sandbox-net")
    w.backend.replies = [f"```bash\n{probe}\n```", "ok"]
    type_and_send(w, "network now")
    check("output:\nNET-OK" in hist_text(w).split("network now")[-1], "sandboxed with network: it connects")
    w.backend.replies = [f"```py\nopen('{outside}/py.txt', 'w').write('x')\n```",
                         f"```py\nopen('{ws}/mine.txt', 'w').write('y')\n```", "ok"]
    type_and_send(w, "python too")
    tail = hist_text(w).split("python too")[-1]
    check(("Read-only file system" in tail or "No such file or directory" in tail)
          and not (outside / "py.txt").exists()
          and (ws / "mine.txt").exists(), "the py session is sandboxed too", tail[-600:])
    w.backend.replies = [f"```bg writer\necho bg > {outside}/bg.txt; echo bg > {ws}/bg.txt\n```", "ok"]
    type_and_send(w, "background")
    time.sleep(1.0)
    check((ws / "bg.txt").exists() and not (outside / "bg.txt").exists(), "and so are background jobs")
    # escapes the reviewers found: host sockets, credentials, leftover processes
    sockdir = Path(tempfile.mkdtemp(dir="/var/tmp"))
    import socket as _socket
    host = _socket.socket(_socket.AF_UNIX); host.bind(str(sockdir / "s")); host.listen(1)
    (Path.home() / ".ssh").mkdir(exist_ok=True)
    (Path.home() / ".ssh" / "id_test").write_text("SECRET-KEY")
    cn.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (cn.CONFIG_DIR / "settings.json").write_text('{"siliconflow_api_key": "sk-SECRET"}')
    w.set_access("sandbox")
    probe2 = ("python3 -c \"import socket,os\n"
              f"s=socket.socket(socket.AF_UNIX)\ntry:\n s.connect('{sockdir}/s'); print('SOCK-REACHED')\n"
              "except OSError as e: print('sock blocked', e.errno)\n"
              "print('DBUS', os.environ.get('DBUS_SESSION_BUS_ADDRESS'))\n"
              "print('NPMCACHE', os.environ.get('npm_config_cache', ''))\"")
    w.backend.replies = [f"```bash\n{probe2}\n"
                         f"cat ~/.ssh/id_test 2>&1; cat {cn.CONFIG_DIR}/settings.json 2>&1; echo end\n```", "ok"]
    type_and_send(w, "escape attempts")
    tail = hist_text(w).split("escape attempts")[-1]
    check("\nSOCK-REACHED" not in tail and "sock blocked" in tail, "host unix sockets can't be reached", tail[-500:])
    check("DBUS None" in tail, "the session bus address isn't passed in")
    check("SECRET-KEY" not in tail and "sk-SECRET" not in tail, "credentials and the API key are hidden")
    check("NPMCACHE " + str(cn.DATA_DIR / "sandbox-cache" / "npm") in tail,
          "package managers get a writable cache")
    host.close(); shutil.rmtree(sockdir, ignore_errors=True)
    w.backend.replies = ["```python\nprint('script ran', 6 * 7)\n```", "ok"]
    type_and_send(w, "a python block")
    check("script ran 42" in hist_text(w).split("a python block")[-1],
          "code blocks still run (their script is let in)")
    mark = f"chuck-orphan-{os.getpid()}"
    w.backend.replies = [f"```bash\nbash -c 'exec -a {mark} sleep 60' & sleep 30\n```", "stopped"]
    w.entry.get_buffer().set_text("spawn and stop")
    w.on_send()
    time.sleep(2.0)
    w.stop_run()
    time.sleep(1.0)
    left = subprocess.run(["pgrep", "-f", mark], capture_output=True, text=True).stdout.strip()
    check(not left, "Stop kills everything the sandboxed command started", left)
    settle()
    (ws / "tests").mkdir(exist_ok=True)
    (ws / "tests" / "test_esc.py").write_text(
        f"def test_x():\n    open('{outside}/from_tests.txt', 'w').write('x')\n")
    rc, out = cn._builder.run_tests(ws, timeout=60)
    check(not (outside / "from_tests.txt").exists(), "runtests runs inside the sandbox too", out[-300:])
    w.backend.replies = ["```py\nkeep = 41\n```", "```py\nimport time\nwhile True: time.sleep(0.2)\n```", "x"]
    w.entry.get_buffer().set_text("py in sandbox")
    w.on_send()
    time.sleep(4.0)
    w.stop_run()
    settle()
    w.backend.replies = ["```py\nkeep + 1\n```", "42"]
    type_and_send(w, "still there")
    check("\n42\n" in hist_text(w).split("still there")[-1],
          "a sandboxed Python session survives Stop with its variables",
          hist_text(w).split("py in sandbox")[-1][-1500:])
    w.set_access("full")
    check(cn._SANDBOX["on"] is False and w._ws_lbl.get_label() == "acc", "Full access lifts it")
    w.backend.replies = [f"```bash\necho out > {outside}/o.txt\n```", "ok"]
    type_and_send(w, "now outside")
    check((outside / "o.txt").exists(), "and commands write anywhere again")
    w.set_access("sandbox")
    w2 = agent([], outside)
    check(cn._SANDBOX["on"] is False and w2._folder_access() == "full",
          "access is per folder: another folder is unaffected")
    w2.set_workspace(ws, announce=False)
    check(cn._SANDBOX["on"] is True, "and it comes back with the folder")
    w2.set_access("full")
else:
    print("  [SKIP] bubblewrap not installed: sandbox untested here")
w = agent(["```bash\necho changed > f.txt\n```", "```write new.txt\nx\n```", "I'd change f.txt."], ws)
w.set_access("readonly")
type_and_send(w, "change it")
h = hist_text(w)
check((ws / "f.txt").read_text() == "orig\n" and not (ws / "new.txt").exists() and "READ-ONLY FOLDER" in h,
      "read-only: nothing runs or changes, and the agent is told why")
w.backend.replies = ["```video https://example.com/v.mp4```", "No."]
type_and_send(w, "download it")
check("video download" in hist_text(w).split("download it")[-1], "read-only also blocks video downloads")
w.entry.get_buffer().set_text("/access")
w.on_send()
texts = [x.get_text() for x in w.msgbox.walk() if isinstance(x.get_text(), str)]
check("ACCESS" in texts and "Read-only" in texts and "Sandboxed, offline" in texts and "current" in texts,
      "/access lists the choices and marks the current one")
use = [x for x in w.msgbox.walk() if isinstance(x, cn.Gtk.Button) and x.get_label() == "Use"]
use[0].emit("clicked")                         # Full access
check(w._folder_access() == "full", "one click changes it")
srv.shutdown()
shutil.rmtree(outside, ignore_errors=True)
settle()

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- custom commands ---")
ws = tmp / "cmds"
(ws / ".chuck" / "commands").mkdir(parents=True)
(ws / ".chuck" / "commands" / "fixlint.md").write_text(
    "---\ndescription: fix every lint error in a path\n---\nRun the linter on $ARGUMENTS and fix "
    "every error it reports. Re-run until clean.\n")
(ws / ".chuck" / "commands" / "standup.md").write_text("# Summarise today's commits\nList what changed today.\n")
(ws / ".chuck" / "commands" / "diff.md").write_text("I should not replace a built-in.\n")
(ws / ".chuck" / "commands" / "loop.md").write_text("/loop\n")
gl = cn.CONFIG_DIR / "commands"
gl.mkdir(parents=True, exist_ok=True)
(gl / "standup.md").write_text("Global version.\n")
(gl / "everywhere.md").write_text("Say hello from everywhere.\n")
(ws / ".chuck" / "commands" / "plain.md").write_text("description: no fences\nDo the plain thing to $ARGUMENTS.\n")
(ws / ".chuck" / "commands" / "fix-tests2.md").write_text("Fix the failing tests.\n")
w = agent(["Linted.", "Standup.", "Hello."], ws)
cc = w.custom_commands()
check(set(cc) == {"fixlint", "standup", "everywhere", "loop", "plain", "fix-tests2"}, "project and global commands are found; built-ins can't be replaced", sorted(cc))
check(cc["fixlint"][1] == "fix every lint error in a path" and cc["standup"][1] == "Summarise today's commits",
      "the description comes from front matter, else the first line")
check("Summarise" in cc["standup"][1], "the project's version wins over the global one")
type_and_send(w, "/fixlint src/app.py")
users = [m["content"] for m in w.history if w._is_real_user(m)]
check(users and users[-1].startswith("Run the linter on src/app.py and fix") and "description:" not in users[-1],
      "/fixlint src/app.py sends the prompt with $ARGUMENTS filled in", users[-1:])
type_and_send(w, "/standup for the team")
users = [m["content"] for m in w.history if w._is_real_user(m)]
check(users[-1].endswith("for the team") and "List what changed today." in users[-1],
      "without $ARGUMENTS, the arguments are appended")
type_and_send(w, "/everywhere")
check("Say hello from everywhere." in [m["content"] for m in w.history if w._is_real_user(m)][-1],
      "a command from the config folder works in any project")
n = len(w.history)
w.entry.get_buffer().set_text("/loop")
w.on_send()
check(len(w.history) == n, "a command that expands to another command is refused")
w.entry.get_buffer().set_text("/help")
w.on_send()
texts = [x.get_text() for x in w.msgbox.walk() if isinstance(x.get_text(), str)]
check("/fixlint" in texts and "fix every lint error in a path" in texts, "/help lists them")
check(cc["plain"][1] == "no fences", "a description: first line works without --- fences too")
type_and_send(w, "/plain x")
check([m["content"] for m in w.history if w._is_real_user(m)][-1] == "Do the plain thing to x.",
      "and isn't sent as part of the prompt")
type_and_send(w, "/fix-tests2")
check([m["content"] for m in w.history if w._is_real_user(m)][-1] == "Fix the failing tests.",
      "names with digits and dashes run too (/fix-tests2)")
settle()

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- Piper: found, installed, and never silently broken ---")
import io
v = cn.voice
real = (v.PIPER_VENV, v.VOICE_DIR, v._have)
vt = tmp / "voice"
(vt / "bin").mkdir(parents=True)
rat = vt / "bin" / "piper"
rat.write_text("#!/usr/bin/python3\nimport gi\ngi.require_version('Gtk', '3.0')\n"
               "from piper.application import Application\n")
rat.chmod(0o755)
v.PIPER_VENV, v.VOICE_DIR = vt / "venv", vt / "voices"
v._PIPER_CACHE.clear(); v._MODEL_CACHE["key"] = object()
v._have = lambda n: str(rat) if n == "piper" else real[2](n)
try:
    check(v.piper_bin() is None, "Arch's piper (the gaming-mouse tool) is not mistaken for TTS")
    check(v.piper_status()[0] == "warn" and "Install" in v.piper_status()[1],
          "/doctor says Piper isn't installed and how to fix it", v.piper_status())
    # a stand-in for the venv's piper: writes a wav, or fails like a broken model
    (v.PIPER_VENV / "bin").mkdir(parents=True)
    fake = v.PIPER_VENV / "bin" / "piper"
    fake.write_text("#!/bin/sh\nm=$2; out=$4; cat >/dev/null\n"
                    "if grep -q good \"$m\"; then head -c 4000 /dev/zero > \"$out\"; "
                    "else echo \"KeyError: 'num_symbols'\" >&2; exit 1; fi\n")
    fake.chmod(0o755)
    v._PIPER_CACHE.clear()
    check(v.piper_bin() == str(fake), "Chuck's own Piper (in its venv) is found first")
    served = []

    class Op:
        def __init__(self, model):
            self.model = model

        def open(self, req, timeout=None):
            served.append(req.full_url.rsplit("/", 1)[-1])
            return io.BytesIO(b"{}" if req.full_url.endswith(".json") else self.model)
    v.VOICE_DIR.mkdir(parents=True)
    (v.VOICE_DIR / "en_US-ryan-high.onnx").write_bytes(b"corrupt")
    (v.VOICE_DIR / "en_US-ryan-high.onnx.json").write_text("{}")
    steps = []
    ok, msg = v.install_piper(steps.append, opener=Op(b"good model"))
    check(ok and "ready" in msg, "Install finishes with a working voice", msg)
    check(served == ["en_US-ryan-high.onnx.json", "en_US-ryan-high.onnx"],
          "a corrupt voice already on disk is downloaded again", served)
    check(v.test_piper()[0] and v.piper_status() == ("ok", "Piper \u00b7 en_US-ryan-high"),
          "and the test and /doctor agree it works")
    (v.VOICE_DIR / "en_US-ryan-high.onnx").write_bytes(b"broken again")
    told = []
    v.ON_PIPER_FAIL[0] = told.append
    v.LAST_ERROR.update(piper="", told=False)
    for _ in range(3):
        v._synth("Hello there.", 0, {"voice_engine": "piper"})
    check(len(told) == 1 and "num_symbols" in told[0],
          "when Piper fails mid-use, the real reason is reported, once", told)
    check(v.piper_status()[0] == "fail" and "num_symbols" in v.piper_status()[1],
          "and /doctor shows it")
    (v.VOICE_DIR / "solo.onnx").write_bytes(b"good")
    (v.VOICE_DIR / "en_US-ryan-high.onnx").unlink()
    v._MODEL_CACHE["key"] = object()
    check(v._find_piper_model() is None, "a model without its .json config is not used")
finally:
    v.PIPER_VENV, v.VOICE_DIR, v._have = real
    v._PIPER_CACHE.clear(); v._MODEL_CACHE["key"] = object()
    v.ON_PIPER_FAIL[0] = None
    v.LAST_ERROR.update(piper="", told=False)

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- resume after a crash ---")
import json as _json
ws = tmp / "resume"
ws.mkdir()


class Slow(ScriptedBackend):
    def stream(self, *a, **k):
        time.sleep(0.5)
        return super().stream(*a, **k)


w = agent([], ws)
w.backend = Slow(["```bash\nsleep 1; echo step-one\n```", "Finished."])
w.entry.get_buffer().set_text("do a long task")
w.on_send()
time.sleep(0.2)
path = cn.CHATS_DIR / f"{w.chat_id}.json"
check(path.exists() and _json.loads(path.read_text()).get("running") is True,
      "while a task runs, the saved chat says so")
settle()
check(_json.loads(path.read_text()).get("running") is False, "when it finishes, the flag is cleared")
# a Chuck that died mid-task: its last word was a tool call, its process is gone
meta = _json.loads(path.read_text())
meta["history"].append({"role": "user", "content": "now the next part"})
meta["history"].append({"role": "assistant", "content": "```bash\necho half-done\n```"})
meta.update(running=True, pid=999999, title="do the next part")
path.write_text(_json.dumps(meta))
w2 = agent(["I checked: the next part still needs doing. Done now."], ws)
check(w2._interrupted and w2._interrupted[0] == path, "at startup the interrupted task is found")
texts = [x.get_text() for x in w2._empty_holder.walk() if isinstance(x.get_text(), str)]
check(any("Chuck closed in the middle of" in t for t in texts) and "Resume" in texts,
      "and offered on the welcome screen", [t for t in texts if "closed" in t])
[b for b in w2._empty_holder.walk() if isinstance(b, cn.Gtk.Button) and b.get_label() == "Resume"][0].emit("clicked")
settle()
h = [m["content"] for m in w2.history]
check(any(str(c).startswith("CONTINUE: Chuck closed unexpectedly") for c in h)
      and "Done now." in str(h[-1]), "Resume reopens it and the agent carries on, told to check first", h[-3:])
check(_json.loads(path.read_text()).get("running") is False and w2.chat_id == meta["ts"],
      "and it's no longer marked interrupted")
w3 = agent([], ws)
check(not w3._interrupted, "a finished chat isn't offered again")
meta = _json.loads(path.read_text()); meta.update(running=True, pid=999999); path.write_text(_json.dumps(meta))
w4 = agent([], ws)
w4.dismiss_interrupted()
check(not w4._interrupted and _json.loads(path.read_text()).get("running") is False, "Dismiss forgets it")
meta.update(running=True, pid=os.getpid()); path.write_text(_json.dumps(meta))
check(not agent([], ws)._interrupted, "a chat a living Chuck is working on is not 'interrupted'")
meta.update(running=False); path.write_text(_json.dumps(meta))
w5 = agent([], ws)
w5.backend = Slow(["```bash\nsleep 3\n```", "x"])
w5.entry.get_buffer().set_text("close me mid-task")
w5.on_send()
time.sleep(0.3)
w5._on_close()
check(_json.loads((cn.CHATS_DIR / f"{w5.chat_id}.json").read_text()).get("running") is False,
      "closing the window mid-task is a choice, not a crash")
w5.stop_run()
settle()

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- a natural voice: what it reads, how it streams ---")
v = cn.voice
c = v.tts_clean("## Disk cleanup\nYour disk was ~92% full (e.g. caches in /var/cache/pacman/pkg). "
                "Ran `paccache -rk2` -> freed 4 GB \U0001F389.\n- Removed orphans\n- Trimmed logs\n"
                "| a | b |\n|---|---|\n| 1 | 2 |\nUpgrade to v16.0.0 & restart, i.e. now.")
check("Disk cleanup." in c and "about 92%" in c and "for example" in c and "that is" in c,
      "abbreviations and symbols are said the way a person says them", c)
check("pkg" in c and "/var/cache" not in c and "paccache rk2" in c and ", then freed" in c,
      "paths become their name, short inline code is kept as words, arrows become 'then'", c)
check("Removed orphans." in c and "Trimmed logs." in c, "bullets end in a pause instead of running on", c)
check("| " not in c and "---" not in c and "\U0001F389" not in c and "version 16.0.0" in c and " and restart" in c,
      "tables and emoji aren't read out; versions are", c)
ch = v.tts_chunks("Done. Your disk was full of caches and old logs. I cleared them. " * 6, 420, first=60)
check(len(ch[0]) <= 60 and all(len(x) <= 420 for x in ch), "the first chunk is short, so speech starts quickly",
      [len(x) for x in ch])
real = (v.kokoro_ready, v.KOKORO.synth, v._SETTINGS.get("voice_player"))
cap = tmp / "spoken.pcm"
try:
    v.kokoro_ready = lambda: True
    check(v.engine_for({}) == "kokoro" and v.engine_for({"voice_engine": "espeak"}) in ("espeak", None),
          "Kokoro is chosen whenever it's installed")
    calls = []

    def fake_synth(text, voice_, speed, out, timeout=120):
        calls.append((text, voice_, speed))
        open(out, "wb").write(b"\x10\x00" * 2400)        # 0.1 s of audio per chunk
        return 2400
    v.KOKORO.synth = fake_synth
    v._SETTINGS["voice_player"] = f"tee {cap}"
    pops = []
    real_popen = v.subprocess.Popen

    def counting_popen(argv, *a, **k):
        pops.append(argv[0])
        return real_popen(argv, *a, **k)
    v.subprocess.Popen = counting_popen
    v.speak("First sentence here. " + "Then a longer stretch of the reply follows on. " * 12,
            {"kokoro_voice": "af_heart", "voice_speed": 1.2})
    time.sleep(1.5)
    v.subprocess.Popen = real_popen
    data = cap.read_bytes() if cap.exists() else b""
    gap = int(v.KOKORO_RATE * 0.16) * 2
    check(len(calls) >= 3 and pops.count("tee") == 1, "every chunk streams into ONE player: no gaps between files",
          (len(calls), pops))
    check(len(data) == len(calls) * (4800 + gap), "all the audio arrives, with a breath between chunks",
          (len(data), len(calls)))
    check(all(x[1] == "af_heart" and abs(x[2] - 1.2) < 1e-9 for x in calls), "the chosen voice and speed are used")
    calls.clear()

    def slow_synth(text, voice_, speed, out, timeout=120):
        calls.append(text)
        open(out, "wb").write(b"\x10\x00" * 240000)       # 10 s per chunk
        return 240000
    v.KOKORO.synth = slow_synth
    v._SETTINGS["voice_player"] = "sleep 30"
    v.speak("One. Two. Three. Four. Five.", {})
    time.sleep(0.6)
    t0 = time.time()
    v.stop_speaking()
    time.sleep(0.5)
    p = v._ACTIVE_PLAYER[0]
    check(p is not None and p.poll() is not None and len(calls) <= 3,
          "Stop silences it at once and stops synthesising", (p and p.poll(), len(calls)))

    tries = []

    def broken(*a, **k):
        tries.append(1)
        raise RuntimeError("onnxruntime: bad model")
    v.KOKORO.synth = broken
    told = []
    v.ON_VOICE_FAIL[0] = lambda eng, why: told.append((eng, why))
    v.LAST_ERROR["kokoro_told"] = False
    fell = []
    real_speak = v.speak
    v.speak = lambda text, s=None: fell.append(((s or {}).get("voice_engine"), text))
    v._speak_kokoro("Hello there. How are you? " * 6, v._TTS_GEN[0], {})
    v.speak = real_speak
    check(told and told[0][0] == "Kokoro" and "bad model" in told[0][1] and fell
          and fell[0][0] in ("piper", "espeak") and "How are you" in fell[0][1],
          "if Kokoro breaks, the reason is shown once and another voice reads the reply", (told, fell))
    check(len(tries) == 1, "it stops at the first failure instead of retrying every chunk", len(tries))
    check(v.engine_for({}) != "kokoro" and v.voice_status({})[0] == "fail",
          "and is set aside for a while, with /doctor saying why", v.voice_status({}))
    v.KOKORO.synth = lambda text, voice_, speed, out, timeout=120: 24000   # a second of audio
    check(v.test_kokoro()[0] and v.engine_for({}) == "kokoro", "a passing test brings it straight back")
finally:
    v.kokoro_ready, v.KOKORO.synth = real[0], real[1]
    v._SETTINGS["voice_player"] = real[2] or ""
    v.ON_PIPER_FAIL[0] = None
    v.ON_VOICE_FAIL[0] = None
    v._KOKORO_DOWN[0] = 0.0
c = v.tts_clean("Run `ls -la | grep x | wc` now. Dr. Smith said x > 5 and 5 * 3 = 15 in C#. "
                "See https://example.com. Then pack shoes, socks, etc. Then go.")
check("ls la grep x wc" in c and "greater than 5" in c and "5 times 3" in c and "C sharp" in c
      and "link. Then" in c and "et cetera. Then" in c, "pipelines, comparisons and sentence ends survive", c)
check(v.tts_chunks("Dr. Smith agrees. The U.S. team too.", 420, first=10)[0].startswith("Dr. Smith"),
      "no pause after 'Dr.' or 'U.S.'")
import http.server as _hs


class _Short(_hs.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "5000000")
        self.end_headers()
        self.wfile.write(b"x" * 2000000)              # then the connection drops
srv2 = socketserver.TCPServer(("127.0.0.1", 0), _Short)
threading.Thread(target=srv2.handle_request, daemon=True, name="srv-short").start()
dest = tmp / "cut.bin"
try:
    v._download(f"http://127.0.0.1:{srv2.server_address[1]}/m", dest, lambda m: None, label="model")
    ok_cut = False
except RuntimeError as ex:
    ok_cut = "cut off" in str(ex)
check(ok_cut and not dest.exists() and not Path(str(dest) + ".part").exists(),
      "a download that's cut off is refused, not kept as finished")
srv2.server_close()

print("\n--- talking to Chuck: local speech to text ---")
L = cn._listen
real = (L.ready, L.EARS.listen, L.EARS.cancel)
try:
    L.ready = lambda: True
    script = []

    def fake_listen(on_event, max_wait=8, max_len=30):
        for ev in script:
            on_event(ev)
        return True
    L.EARS.listen = fake_listen
    cancelled = []
    L.EARS.cancel = lambda: cancelled.append(1)
    w = agent(["Sure, cleaning it."], tmp / "resume")
    script[:] = [{"event": "listening"}, {"event": "speech"}, {"event": "level", "rms": 0.1},
                 {"event": "final", "text": "clean up my disk please"}]
    w.mic_btn.emit("clicked")
    settle()
    check(w._get_entry() == "clean up my disk please" and not w._listening and not w.backend.calls,
          "what you said lands in the composer, ready to send", w._get_entry())
    check(w.settings.get("mic_used") is True, "and the ears warm up at the next start")
    w.entry.get_buffer().set_text("")
    w.settings["voice_autosend"] = True
    script[:] = [{"event": "listening"}, {"event": "final", "text": "what time is it"}]
    w.toggle_listen()
    settle()
    users = [m["content"] for m in w.history if w._is_real_user(m)]
    check(users and users[-1] == "what time is it", "hands-free: it's sent as you finish speaking", users[-1:])
    w.settings["voice_autosend"] = False
    script[:] = [{"event": "listening"}]
    w.toggle_listen()
    check(w._listening and "mic-on" in w.mic_btn.classes, "the mic button shows it's listening",
          w.mic_btn.classes)
    w.toggle_listen()
    check(cancelled, "pressing it again stops listening")
    w._on_hear({"event": "cancelled"})
    script[:] = [{"event": "nothing"}]
    w.toggle_listen()
    settle()
    check(not w._listening and "mic-on" not in w.mic_btn.classes, "silence ends it cleanly")
    script[:] = [{"event": "error", "error": "the microphone stopped: no such device"}]
    w.toggle_listen()
    settle()
    notes = [x.get_text() for x in w.msgbox.walk() if isinstance(x.get_text(), str)]
    check(any("no such device" in t for t in notes), "a microphone problem is shown, not swallowed")
    L.ready = lambda: False
    w.toggle_listen()
    texts = [x.get_text() for x in w.msgbox.walk() if isinstance(x.get_text(), str)]
    check("VOICE" in texts and any("Moonshine" in t for t in texts),
          "without voice input installed, the mic offers to install it", [t for t in texts if "Moonshine" in t])
    rows = w.doctor_checks()
    check(any(n == "Voice input" for _st, n, _d in rows) and any(n == "Voice" for _st, n, _d in rows),
          "/doctor reports the voice and voice input")
finally:
    L.ready, L.EARS.listen, L.EARS.cancel = real
ears = L.Ears()
real_start, real_argv = ears.start, L.recorder_argv
L.recorder_argv = lambda: ["true"]
got = []


def slow_start(timeout=90):
    ears.cancel()                                  # the user presses again while it loads
    time.sleep(0.2)
ears.start = slow_start
ears.listen(got.append)
check(got == [{"event": "cancelled"}] and not ears.busy, "cancelling while the model loads is honoured", got)
L.recorder_argv = real_argv
settle()

# ═══════════════════════════════════════════════════════════════════════════
print(f"\nTOTAL V17 FAILURES: {fails + len(FAILS)}")
shutil.rmtree(tmp, ignore_errors=True)
assert fails == 0 and not FAILS, "v17 regressions present"
print("ALL V17 TESTS PASSED")
