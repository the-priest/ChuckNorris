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

print("--- a SLOW first token is not mistaken for a hang ---")
# This is the bug in the screenshots: "Thinking..." then
# "no progress for 45s - looks stuck, stopping." on a healthy request.
import time as _t
import tempfile as _tf
cn.CHATS_DIR = Path(_tf.mkdtemp()); cn.SETTINGS = Path(_tf.mkdtemp()) / "s.json"
_app = cn.ChuckApp()
w = cn.ChuckWindow(_app)
w._start_run()
w._awaiting_first = True
_wait = w.STUCK_AFTER + 30         # well past the between-steps budget…
w._run_started = _t.time() - _wait
w._last_progress = _t.time() - _wait       # …but still waiting on the first token
w._heartbeat()
if not w._running:
    fail(f"watchdog still kills a slow first response at {_wait}s")
else:
    print(f"  {_wait}s waiting for the model -> still running; ticker: {w.live.get_text()!r}")
if "waiting for the model" not in w.live.get_text():
    fail("ticker doesn't say what it's waiting on")

print("--- but a truly dead request is still stopped ---")
w._last_progress = _t.time() - (w.AWAIT_FIRST + 5)
w._heartbeat()
if w._running:
    fail("a request with no response at all was never stopped")
print(f"  stopped after {w.AWAIT_FIRST}s of total silence")

print("--- stalls BETWEEN steps still use the tight budget ---")
w2 = cn.ChuckWindow(_app); w2._start_run()
w2._awaiting_first = False                  # model already spoke; a tool stalled
_stall = w2.STUCK_AFTER + 5        # relative, so raising the constant can't stale this
w2._run_started = _t.time() - _stall
w2._last_progress = _t.time() - _stall
w2._heartbeat()
if w2._running:
    fail("tool-phase watchdog got loosened by mistake")
print(f"  tool stalled {w2.STUCK_AFTER}s -> stopped, as it should be")

print("--- the connection opening counts as proof of life ---")
w3 = cn.ChuckWindow(_app); w3._start_run()
w3._last_progress = _t.time() - 100
w3._note_progress()
if _t.time() - w3._last_progress > 2:
    fail("on_open did not reset the stall clock")
print("  on_open resets the clock")

print("--- voice is on by default ---")
if not w3.settings.get("tts", True):
    fail("tts default is off")
print("  tts defaults to on")

print("--- header icons all resolve (no missing-image glyph) ---")
for names in (("sidebar-show-symbolic", "view-list-bullet-symbolic", "document-open-recent-symbolic"),
              ("go-up-symbolic", "pan-up-symbolic"),
              ("media-playback-start-symbolic", "audio-volume-high-symbolic")):
    got = cn._pick_icon(*names)
    if not got:
        fail(f"no icon resolved for {names}")
print("  _pick_icon always returns a usable name")

print("--- font scaling produces valid CSS across the range ---")
for px in (9, 14, 22, 28, 999, 1):
    css = cn.font_css(px)
    if "font-size" not in css:
        fail(f"font_css({px}) produced nothing usable")
print("  font_css clamps and renders for every input")

print("--- settings reach every module (one shared dict) ---")
# Splitting the app into modules makes it easy to end up with TWO settings
# dicts, where changing the proxy or the voice in Settings silently does
# nothing. Everything must reference the same object.
import chucknorris_ext.web as _w
import chucknorris_ext.voice as _v
import chucknorris_ext.config as _c
_wS = cn.ChuckWindow(_app)
_wS.settings["searx_url"] = "https://propagation-probe"
_wS.settings["proxy"] = "http://probe:8080"
_wS.settings["voice_speed"] = 1.75
for _name, _ok in (
        ("searx -> web", _w._SETTINGS.get("searx_url") == "https://propagation-probe"),
        ("proxy -> config", _c.SETTINGS_DATA.get("proxy") == "http://probe:8080"),
        ("voice_speed -> voice", _v._SETTINGS.get("voice_speed") == 1.75),
        ("backend shares the dict", _wS.backend.s is _wS.settings)):
    if not _ok:
        fail(f"settings do not propagate: {_name}")
print("  proxy, SearXNG and voice settings all reach their modules")

print("--- constants are defined in exactly one place ---")
import re as _re
_app = dict(_re.findall(r"^([A-Z][A-Z0-9_]+) = (.+?)(?:\s+#.*)?$",
                        open("chucknorris.py").read(), _re.M))
_cfg = dict(_re.findall(r"^([A-Z][A-Z0-9_]+) = (.+?)(?:\s+#.*)?$",
                        open("chucknorris_ext/config.py").read(), _re.M))
_dupes = sorted(set(_app) & set(_cfg))
if _dupes:
    fail(f"constants defined twice (they will drift): {_dupes}")
else:
    print("  no constant is defined in both the app and config")
if cn.DEFAULT_MODEL != "deepseek-ai/DeepSeek-V4-Flash":
    fail(f"model invariant broken: {cn.DEFAULT_MODEL}")
print(f"  model invariant holds: {cn.DEFAULT_MODEL}")

print()
print("TOTAL STARTUP FAILURES:", len(FAILS))
for f in FAILS:
    print("  ", f)
assert not FAILS, "startup reliability regressions"
print("ALL STARTUP TESTS PASSED")
