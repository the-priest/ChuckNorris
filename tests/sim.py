import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""sim.py — end-to-end simulation of complete turns through the REAL ChuckWindow.

A scripted backend replays realistic model replies (including tool blocks); the
network is mocked; everything else is Chuck's own code. Each scenario asserts
the WHOLE chain: history stays well-formed, the API payload stays serialisable,
tools dispatch and feed back, and the run always terminates with the button
returned to Send.
"""
import sys, json, time, threading, tempfile, io
from pathlib import Path
sys.path.insert(0, '.')
import gtkstub
gtkstub.install()
import importlib.util
_spec = importlib.util.spec_from_file_location("cn", "chucknorris.py")
cn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cn)

FAILS = []


def fail(msg):
    FAILS.append(msg)
    print("   *** FAIL:", msg)


class ScriptedBackend:
    """Replays a list of assistant replies, one per _ask_model call."""
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.payloads = []

    def key(self): return "test-key"
    def base(self): return "https://api.example.com/v1"

    def stream(self, messages, on_delta, on_done, on_error, vision=False, should_stop=None):
        self.calls.append(len(messages))
        # the payload the real Backend would build — must be serialisable
        try:
            self.payloads.append(json.dumps({"model": "m", "messages": messages}))
        except Exception as e:
            fail(f"history is not JSON-serialisable: {e}")
        reply = self.replies.pop(0) if self.replies else "Done."
        for i in range(0, len(reply), 40):
            if should_stop and should_stop():
                return
            on_delta(reply[i:i + 40])
        if should_stop and should_stop():
            return
        on_done()


def settle(limit=60):
    """Let every worker thread finish (the app is heavily threaded)."""
    for _ in range(limit):
        alive = [t for t in threading.enumerate()
                 if t is not threading.current_thread() and t.is_alive()]
        if not alive:
            return True
        for t in alive:
            t.join(timeout=1.0)
    return False


def new_win(replies, tmp):
    gtkstub.GLibNS.errors.clear()
    cn.CHATS_DIR = tmp / "chats"; cn.CHATS_DIR.mkdir(exist_ok=True)
    cn.SETTINGS = tmp / "settings.json"
    cn.BASILISK_SETTINGS = tmp / "none.json"
    app = cn.ChuckApp()
    win = cn.ChuckWindow(app)
    win.backend = ScriptedBackend(replies)
    return win


def type_and_send(win, text):
    win.entry.get_buffer().set_text(text)
    win.on_send()
    settle()


def check_history(win, label):
    """Invariants that must hold after every turn."""
    h = win.history
    if not h or h[0]["role"] != "system":
        fail(f"{label}: system message missing/not first")
    for i, m in enumerate(h):
        if m.get("role") not in ("system", "user", "assistant"):
            fail(f"{label}: bad role at {i}: {m.get('role')}")
        c = m.get("content")
        if c is None:
            fail(f"{label}: None content at {i}")
        if not isinstance(c, (str, list)):
            fail(f"{label}: content type {type(c).__name__} at {i}")
    if len(h) > 1 and h.count(h[0]) > 1:
        fail(f"{label}: system prompt duplicated in history")
    try:
        json.dumps(h)
    except Exception as e:
        fail(f"{label}: history not serialisable: {e}")


def check_finished(win, label):
    if win._running:
        fail(f"{label}: run never ended (_running still True)")
    if win.send_btn.get_icon_name() != "go-up-symbolic":
        fail(f"{label}: send button stuck as Stop ({win.send_btn.get_icon_name()})")
    if "stop-fab" in win.send_btn.classes:
        fail(f"{label}: stop-fab class not removed")
    if win._pending_tools != 0:
        fail(f"{label}: pending tools stuck at {win._pending_tools}")
    if gtkstub.GLibNS.errors:
        fail(f"{label}: exceptions on the UI thread: {gtkstub.GLibNS.errors[:2]}")


def bubbles(win):
    return [w.get_text() for w in win.msgbox.walk() if isinstance(w.get_text(), str) and w.get_text()]


def cards(win):
    return [w for w in win.msgbox.walk() if "cmd-card" in getattr(w, "classes", set())]
