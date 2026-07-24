import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""Cold start must work on the FIRST message.

The reported bug: the first message of a session almost always errored, then
everything worked. Cause: the very first request pays for DNS + TLS while the
user waits, and a single transient failure had no retry behind it — so one
hiccup burned the turn. Separately, any exception on the send path escaped into
the GTK handler and left the button stuck on Stop.
"""
import tempfile, socket, types, urllib.error
from pathlib import Path
import gtkstub
gtkstub.install()
import importlib.util
_s = importlib.util.spec_from_file_location("cn", "chucknorris.py")
cn = importlib.util.module_from_spec(_s); _s.loader.exec_module(cn)

FAILS = []
def fail(m):
    FAILS.append(m); print("   *** FAIL:", m)


class Resp:
    lines = [b'data: {"choices":[{"delta":{"content":"hello"}}]}\n', b'data: [DONE]\n']
    def __iter__(self): return iter(self.lines)
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass


def run(fail_times, exc, attempts=3):
    state = {"n": 0}
    def urlopen(req, timeout=None):
        state["n"] += 1
        if state["n"] <= fail_times:
            raise exc
        return Resp()
    cn.urllib.request.urlopen = urlopen
    out = {"delta": "", "done": False, "err": None}
    cn.Backend({"siliconflow_api_key": "k"}).stream(
        [{"role": "user", "content": "hi"}],
        lambda d: out.__setitem__("delta", out["delta"] + d),
        lambda: out.__setitem__("done", True),
        lambda m: out.__setitem__("err", m), attempts=attempts)
    return state["n"], out


DNS = urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))

print("--- a cold-connection hiccup on the first message recovers silently ---")
n, out = run(1, DNS)
print(f"  attempts={n} done={out['done']} err={out['err']}")
if not out["done"] or out["err"]:
    fail("a single transient failure still surfaces as an error")

print("--- two failures then success ---")
n, out = run(2, DNS)
if not out["done"] or out["err"]:
    fail(f"gave up too early (attempts={n})")
print(f"  attempts={n}, recovered")

print("--- a genuinely dead network reports clearly and stops ---")
n, out = run(9, DNS)
print(f"  attempts={n} err={out['err']!r}")
if n != 3:
    fail(f"expected 3 attempts, got {n}")
if not out["err"] or "reach the API" not in out["err"]:
    fail("error message is not actionable")

print("--- a rejected key is never retried ---")
n, out = run(9, urllib.error.HTTPError("u", 401, "Unauthorized", {}, None))
print(f"  attempts={n} err={out['err']!r}")
if n != 1:
    fail(f"retried a permanent 401 {n} times")
if not out["err"] or "key rejected" not in out["err"]:
    fail("401 message not actionable")

print("--- transient HTTP codes ARE retried ---")
for code in (429, 500, 502, 503, 504):
    n, out = run(1, urllib.error.HTTPError("u", code, "x", {}, None))
    if not out["done"]:
        fail(f"HTTP {code} was not retried")
print("  429/500/502/503/504 all retried")

print("--- a 400 is NOT retried (permanent) ---")
n, out = run(9, urllib.error.HTTPError("u", 400, "Bad Request", {}, None))
if n != 1:
    fail(f"retried a 400 {n} times")
print(f"  attempts={n}")

print("--- a reply cut off mid-stream keeps what arrived ---")
class Partial:
    def __iter__(self):
        yield b'data: {"choices":[{"delta":{"content":"half a sen"}}]}\n'
        raise ConnectionResetError("dropped")
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass
cn.urllib.request.urlopen = lambda r, timeout=None: Partial()
out = {"delta": "", "done": False, "err": None}
cn.Backend({"siliconflow_api_key": "k"}).stream(
    [{"role": "user", "content": "x"}],
    lambda d: out.__setitem__("delta", out["delta"] + d),
    lambda: out.__setitem__("done", True),
    lambda m: out.__setitem__("err", m))
print(f"  kept={out['delta']!r} done={out['done']} err={out['err']}")
if out["delta"] != "half a sen" or not out["done"] or out["err"]:
    fail("partial reply was discarded or reported as an error")

print("--- missing key is reported immediately, not retried ---")
out = {"err": None}
cn.Backend({}).stream([{"role": "user", "content": "x"}],
                      lambda d: None, lambda: None,
                      lambda m: out.__setitem__("err", m))
if not out["err"] or "API key" not in out["err"]:
    fail("missing key not reported clearly")
print(" ", out["err"])

print("--- the backend warms the connection at startup ---")
if not hasattr(cn.Backend({}), "warm_up"):
    fail("no warm_up on the backend")
else:
    cn.socket.create_connection = lambda *a, **k: type("S", (), {"close": lambda s: None})()
    cn.Backend({"siliconflow_api_key": "k"}).warm_up()
    print("  warm_up ran without raising")

print("--- an exception on the send path cannot wedge the button ---")
home = Path(tempfile.mkdtemp())
app = cn.ChuckApp()
win = cn.ChuckWindow(app)
win._stream_into_bubble_inner = types.MethodType(
    lambda self, m, v=False: (_ for _ in ()).throw(RuntimeError("boom")), win)
win.entry.get_buffer().set_text("first message")
win.on_send()
print(f"  running={win._running} button={win.send_btn.get_icon_name()}")
if win._running:
    fail("run left hanging after a send-path exception")
if win.send_btn.get_icon_name() != "go-up-symbolic":
    fail("send button stuck on Stop — the user would have to click twice")

print("--- a cold app with no config still builds and augments ---")
try:
    w2 = cn.ChuckWindow(app)
    msgs = w2._augment([{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}])
    if not msgs or msgs[0]["role"] != "system":
        fail("cold _augment produced a malformed payload")
    print(f"  cold augment produced {len(msgs)} messages")
except Exception as e:
    fail(f"cold start raised: {e}")

print()
print("TOTAL STARTUP FAILURES:", len(FAILS))
for f in FAILS:
    print("  ", f)
assert not FAILS, "startup reliability regressions"
print("ALL STARTUP TESTS PASSED")
