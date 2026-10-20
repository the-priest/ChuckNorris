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

from sim import cn, new_win, type_and_send, settle, check_finished, FAILS

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
print(f"\nTOTAL V17 FAILURES: {fails + len(FAILS)}")
shutil.rmtree(tmp, ignore_errors=True)
assert fails == 0 and not FAILS, "v17 regressions present"
print("ALL V17 TESTS PASSED")
