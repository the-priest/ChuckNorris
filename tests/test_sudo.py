import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""Privilege-escalation + runtime-awareness tests.

Chuck used to rewrite `sudo X` to `pkexec sh -c X` and give up when pkexec
wasn't there, and its regex only matched `sudo` at the very start of a line —
so `cd /x && sudo make install` or `... | sudo tee` never elevated, and nothing
that needed a password could ever run. It also used one flat timeout, so a dev
server blocked for the full window instead of being told to background itself.

This suite locks in the ported engine:
  * command_needs_sudo — sudo at ANY command position, not just the start
  * _inject_askpass — every sudo becomes `sudo -A`
  * estimate_runtime — install/build → long cap; server → short cap; bg → short
  * _run_shell — routes a password to the sudo runner, and returns the private
    auth-failed sentinel when root is needed but no password is available
  * the UI flow — a root command collects the password once (or reports a clean
    auth failure on cancel) and the turn always ends cleanly, never wedged.
"""
import tempfile
from pathlib import Path
from sim import cn, new_win, type_and_send, check_finished, fail, FAILS

tmp = Path(tempfile.mkdtemp())

_SUDO_PE = {"tool": "sudo", "bin": "sudo", "askpass": True, "stdin": True, "version": ""}


def hist(w):
    return " ".join(str(m.get("content", "")) for m in w.history)


# ── unit: detection / injection / runtime classification ─────────────────────
def test_units():
    for c, want in [("sudo apt install x", True),
                    ("echo pseudo", False),
                    ("cd /t && sudo pacman -Syu x", True),
                    ("echo x | sudo tee /etc/hosts", True),
                    ("/opt/sudoku", False),
                    ("ls -la", False)]:
        if cn.command_needs_sudo(c) != want:
            fail(f"command_needs_sudo({c!r}) should be {want}")
    if cn._inject_askpass("sudo a && sudo b") != "sudo -A a && sudo -A b":
        fail("inject_askpass must turn every sudo into `sudo -A`")
    for c, (kind, cap) in {"pacman -Syu ripgrep": ("long", 1800),
                           "git clone https://x": ("long", 1800),
                           "npm start": ("server", 25),
                           "ls": ("quick", 30),
                           "nohup node s.js &": ("background", 15)}.items():
        e = cn.estimate_runtime(c)
        if e["kind"] != kind or e["hard_timeout_seconds"] != cap:
            fail(f"estimate_runtime({c!r}) = {e['kind']}/{e['hard_timeout_seconds']}, "
                 f"want {kind}/{cap}")


# ── routing: _run_shell honours sudo state, no real sudo needed ──────────────
def test_run_shell_routing():
    rc, out = cn._run_shell("echo ROUTE_OK")           # plain path, real subprocess
    if rc != 0 or "ROUTE_OK" not in out:
        fail(f"plain _run_shell broke: {rc} {out!r}")

    saved = (cn._sudo_ready, cn.detect_priv_esc, cn._run_sudo)
    seen = {}
    cn._sudo_ready = lambda: False
    cn.detect_priv_esc = lambda: dict(_SUDO_PE)
    cn._run_sudo = lambda script, pw, timeout, env: (
        seen.__setitem__("pw", pw), (0, "AUTHED"))[1]
    try:
        rc, out = cn._run_shell("sudo whoami", sudo_password="hunter2")
        if seen.get("pw") != "hunter2" or rc != 0 or out != "AUTHED":
            fail(f"password not routed to the sudo runner: pw={seen.get('pw')!r} "
                 f"rc={rc} out={out!r}")
        rc2, _ = cn._run_shell("sudo whoami")          # needs root, no password
        if rc2 != cn._SUDO_AUTH_FAILED:
            fail(f"root command with no password should return {cn._SUDO_AUTH_FAILED}, "
                 f"got {rc2}")
    finally:
        cn._sudo_ready, cn.detect_priv_esc, cn._run_sudo = saved


# ── UI flow: password collected once; cancel yields a clean auth failure ─────
def _install_reply(second):
    # `sudo whoami` needs root but classifies as a normal (auto-run) command
    return ["Doing it now.\n```bash\nsudo whoami\n```", second]


def test_ui_sudo_answer():
    saved = (cn._sudo_ready, cn.detect_priv_esc, cn._run_sudo)
    seen = {}
    cn._sudo_ready = lambda: False
    cn.detect_priv_esc = lambda: dict(_SUDO_PE)
    cn._run_sudo = lambda script, pw, timeout, env: (
        seen.__setitem__("pw", pw), (0, "ROOT-OUTPUT-OK"))[1]
    try:
        w = new_win(_install_reply("All done."), tmp)
        w._sudo_prompt = lambda script, cb: cb("s3cret")   # user types the password
        type_and_send(w, "run whoami as root")
        if seen.get("pw") != "s3cret":
            fail("typed password never reached the executor")
        if "ROOT-OUTPUT-OK" not in hist(w):
            fail("authenticated command's output didn't feed back to the model")
        check_finished(w, "sudo-answer")
    finally:
        cn._sudo_ready, cn.detect_priv_esc, cn._run_sudo = saved


def test_ui_sudo_cancel():
    saved = (cn._sudo_ready, cn.detect_priv_esc, cn._run_sudo)
    cn._sudo_ready = lambda: False
    cn.detect_priv_esc = lambda: dict(_SUDO_PE)
    # real _run_sudo won't be reached (no password), but stub it just in case
    cn._run_sudo = lambda *a: (0, "should-not-run")
    try:
        w = new_win(_install_reply("Told the user root is needed."), tmp)
        w._sudo_prompt = lambda script, cb: cb(None)       # user cancels
        type_and_send(w, "run whoami as root")
        if "sudo authentication failed" not in hist(w).lower():
            fail("a cancelled password prompt didn't surface an auth-failed result")
        check_finished(w, "sudo-cancel")                   # crucially: not wedged
    finally:
        cn._sudo_ready, cn.detect_priv_esc, cn._run_sudo = saved


for _t in (test_units, test_run_shell_routing, test_ui_sudo_answer, test_ui_sudo_cancel):
    try:
        _t()
    except Exception as _e:
        import traceback
        traceback.print_exc()
        fail(f"{_t.__name__} raised: {_e}")

print()
print("TOTAL SUDO FAILURES:", len(FAILS))
for _f in FAILS:
    print("  ", _f)
assert not FAILS, "sudo/runtime regressions"
print("ALL SUDO TESTS PASSED")
