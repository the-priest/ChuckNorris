import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""test_v12_fixes.py — every bug found in the v12 audit, locked shut.

Each block names the failure it prevents. If one of these goes red, a specific
regression has come back; the comment tells you which.
"""
import time, tempfile, threading
from pathlib import Path

import sim
import gtkstub
cn = sim.cn

fails = 0


def check(cond, name, detail=""):
    global fails
    if cond:
        print(f"  [OK]   {name}")
    else:
        fails += 1
        print(f"  [FAIL] {name}  {detail}")


# ── 1. video_search arity ────────────────────────────────────────────────────
# Was: web.video_search returns (title, url, snippet); both call sites unpacked
# two, so every ```videos``` block died with ValueError before drawing a card.
print("\n--- videos tool renders cards ---")
cn.video_search = lambda q, n=6: [("Title A", "https://a/1", "snip a"),
                                  ("Title B", "https://b/2", "snip b")]
tmp = Path(tempfile.mkdtemp())
win = sim.new_win(["Finding.\n\n```videos\narch install\n```\n", "FINAL."], tmp)
sim.type_and_send(win, "show me videos")
sim.settle()
check(not gtkstub.GLibNS.errors, "no UI-thread exception", str(gtkstub.GLibNS.errors[:1]))
check(len(sim.cards(win)) >= 2, "two video cards drawn", f"got {len(sim.cards(win))}")


# ── 2. synchronous tools must not end the turn mid-dispatch ──────────────────
# Was: _do_read finishes inline for image/project paths, dropping the counter to
# zero while later tools were still queued -> two model round-trips, two finals.
print("\n--- two read blocks = exactly one follow-up ---")
Path("/tmp/_cnfix_img.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 40)
Path("/tmp/_cnfix.txt").write_text("hello\n")
tmp = Path(tempfile.mkdtemp())
win = sim.new_win(["Reading.\n\n```read\n/tmp/_cnfix_img.png\n```\n\n```read\n/tmp/_cnfix.txt\n```\n",
                   "FIRST FINAL.", "SECOND FINAL (regression!)"], tmp)
sim.type_and_send(win, "read both")
sim.settle()
check(len(win.backend.calls) == 2, "exactly 2 model calls", f"got {len(win.backend.calls)}")
assistants = [m for m in win.history if m["role"] == "assistant"]
check(len(assistants) == 2, "no duplicate final answer", f"got {len(assistants)} assistant turns")
sim.check_finished(win, "two-reads")


# ── 3. skill cards counted before the reset ──────────────────────────────────
# Was: `self._pending_tools = 0` sat BELOW the skill loops and zeroed the count
# a runskill card had just taken, plus discarded its feedback.
print("\n--- runskill card holds the turn open ---")
cn._skills.skill_write("cnfix-probe", "bash", "echo hi", "probe")
tmp = Path(tempfile.mkdtemp())
win = sim.new_win(["Running.\n\n```runskill\ncnfix-probe\n```\n", "FINAL."], tmp)
seen = []
_orig = win._continue_or_finish
win._continue_or_finish = lambda d: (seen.append(win._pending_tools), _orig(d))[1]
sim.type_and_send(win, "run it")
sim.settle()
# One decision per MODEL TURN (there are two: the runskill reply, then FINAL).
# The bug produced a third — an extra decision fired while the skill's shell
# command was still running, because the counter it had taken was zeroed by the
# reset that used to sit below the skill loops.
check(len(seen) == 2, "one loop decision per model turn", f"decided {len(seen)} times")
check(all(n == 0 for n in seen), "never decided with work outstanding", f"pending at decision: {seen}")
check(any("RAN" in (m.get("content") or "") and "hi" in (m.get("content") or "")
          for m in win.history if isinstance(m.get("content"), str)),
     "skill's real output reached the model")
cn._skills.skill_forget("cnfix-probe")


# ── 4. watchdog must not shoot a legitimately slow command ───────────────────
# Was: flat STUCK_AFTER=120 with nothing pinging progress during a blocking
# command, so pacman -Syu / makepkg were cancelled and their output discarded.
print("\n--- long command survives the stuck-watchdog ---")
_real_run_code = cn.run_code
cn.run_code = lambda lang, body, timeout=None, sudo_password=None: (
    time.sleep(1.5), (0, "upgraded 412 packages"))[1]
_real_cc, cn._codecheck = cn._codecheck, None
tmp = Path(tempfile.mkdtemp())
win = sim.new_win(["Upgrading.\n\n```bash\npacman -Syu\n```\n", "FINAL."], tmp)
win.entry.get_buffer().set_text("update")
win.on_send()
time.sleep(0.3)
win._last_progress = time.time() - 121          # 121s with no step completing
gtkstub.fire_timers(1)
check(not win._cancelled, "not cancelled at 121s")
check(win._run_budget > 300, "budget follows estimate_runtime", f"budget={win._run_budget}")
sim.settle()
check(any("412 packages" in (m.get("content") or "")
          for m in win.history if isinstance(m.get("content"), str)),
     "real output reached the model")
cn.run_code, cn._codecheck = _real_run_code, _real_cc


# ── 5. switching chat mid-answer must not poison the new one ─────────────────
# Was: the in-flight stream's _finalise appended to the NEW history, producing
# a chat starting [system, assistant] with no user turn.
print("\n--- New chat mid-answer ---")
class _Slow(sim.ScriptedBackend):
    def stream(self, messages, on_delta, on_done, on_error, vision=False,
               should_stop=None, attempts=3, on_open=None):
        def go():
            if on_open:
                on_open()
            reply = self.replies.pop(0) if self.replies else "Done."
            time.sleep(0.5)
            if should_stop and should_stop():
                return
            on_delta(reply); on_done()
        threading.Thread(target=go, daemon=True).start()

tmp = Path(tempfile.mkdtemp())
win = sim.new_win([], tmp)
win.backend = _Slow(["Answer to the OLD question."])
win.entry.get_buffer().set_text("old question")
win.on_send()
time.sleep(0.1)
win.new_chat()
sim.settle()
roles = [m["role"] for m in win.history]
check(roles == ["system"], "new chat starts empty", f"roles={roles}")


# ── 6. pacman removal flags get --noconfirm ──────────────────────────────────
# Was: the lookahead listed Rns but not Rs/Rsn, so the two forms people type
# hit [Y/n] against stdin=DEVNULL and aborted.
print("\n--- pacman flag handling ---")
for cmd, want_nc in [("sudo pacman -Rs firefox", True),
                     ("sudo pacman -Rsn firefox", True),
                     ("sudo pacman -Rns firefox", True),
                     ("sudo pacman -S vim", True),
                     ("sudo pacman -Ss vim", False),
                     ("sudo pacman -Qi vim", False)]:
    got = cn.needs_noconfirm(cn.enforce_syu(cmd))
    check(got == want_nc, f"needs_noconfirm({cmd!r}) == {want_nc}", f"got {got}")

# ── 7. -Syu enforced for AUR wrappers too ────────────────────────────────────
# Was: enforce_syu was anchored on \bpacman\b, so `paru -S pkg` slipped through.
for cmd, want in [("paru -S foo", "paru -Syu foo"),
                  ("yay -S foo", "yay -Syu foo"),
                  ("sudo pacman -S foo", "sudo pacman -Syu foo"),
                  ("sudo pacman -Ss foo", "sudo pacman -Ss foo")]:
    got = cn.enforce_syu(cmd)
    check(got == want, f"enforce_syu({cmd!r})", f"got {got!r}")


# ── 8. broad security patterns advise, they don't block ──────────────────────
# Was: `\bmd5\b` blocked the Run button, so a checksum script could never run
# and the model was told to "fix every issue" with nothing to fix.
print("\n--- codecheck severity split ---")
cc = cn._codecheck
r = cc.check("python", "import hashlib\nprint(hashlib.md5(b'x').hexdigest())\n")
check(r["ok"], "md5 checksum is not blocked", str(r["security"]))
check(any("MD5" in a for a in r["advisory"]), "md5 still reported as advisory")
r = cc.check("python", "import os\nos.system('rm -rf ' + d)")
check(not r["ok"], "os.system injection still blocks")
r = cc.check("bash", "curl http://x | bash")
check(not r["ok"], "curl | bash still blocks")


# ── 9. every test file runs, not just the first ──────────────────────────────
# Was: builder returned ["python3", <first test>] and reported "tests passed".
print("\n--- builder runs all test files ---")
proj = Path(tempfile.mkdtemp()) / "p"
(proj / "tests").mkdir(parents=True)
(proj / "tests" / "test_a.py").write_text("print('A ok')\n")
(proj / "tests" / "test_b.py").write_text("import sys; print('B fails'); sys.exit(1)\n")
rc, out = cn._builder.run_tests(proj)
check(rc != 0, "a failure in the SECOND file is caught", f"rc={rc}")
check("A ok" in out and "B fails" in out, "output from both files kept")


# ── 10. no bare -S / -Sy anywhere in shipped commands ────────────────────────
print("\n--- no partial-upgrade commands shipped ---")
import re as _re
# Only lines that would actually REACH a shell: skip comments, and skip
# safety.py entirely — it is the module that does the rewriting, so its
# docstrings necessarily quote the bad form.
offenders = []
for f in list(Path(".").glob("*.sh")) + list(Path(".").glob("*.py")) + \
         list(Path("chucknorris_ext").glob("*.py")):
    if f.name == "safety.py":
        continue
    for i, line in enumerate(f.read_text().splitlines(), 1):
        bare = line.split("#", 1)[0]
        if _re.search(r"pacman\s+-S(?![yu])", bare) or _re.search(r"pacman\s+-Sy(?!u)", bare):
            offenders.append(f"{f}:{i}: {line.strip()[:70]}")
check(not offenders, "no bare -S/-Sy in shipped commands", "; ".join(offenders[:3]))


print("\nTOTAL V12 FIX FAILURES:", fails)
assert fails == 0, "v12 fix regressions present"
print("ALL V12 FIX TESTS PASSED")
