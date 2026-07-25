import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""Regression tests — lock in the bugs fixed in v12.

Each maps to a specific defect the rest of the suite did not catch:

  1. `_run_card` was wired to the CRITICAL command card's Run button but never
     defined, so approving a critical command raised AttributeError inside the
     GTK handler and silently did nothing.
  2. Approving a gated card AFTER its turn had ended resumed the model outside
     the run lifecycle — no Stop button, no stuck-watchdog, no "working" ticker.
  3. "One runnable step per reply" collected code blocks in language-priority
     order instead of the order written, so a shell step written first could be
     dropped in favour of a python step written later.
  4. The TTS producer used an unbounded queue put and deadlocked (leaking a
     synth thread) when speech was cancelled mid-playback.

(Bug 5 — the sidebar retention countdown ignoring the configured TTL — is
already covered by test_chats.)
"""
import time, threading, tempfile
from pathlib import Path
from sim import cn, new_win, type_and_send, settle, check_finished, fail, FAILS

tmp = Path(tempfile.mkdtemp())


def hist(w):
    return " ".join(str(m.get("content", "")) for m in w.history)


# ── 1 + 2 ── _run_card exists, executes, disarms its own button, and re-arms
#             the Stop/watchdog lifecycle when a gated card is approved after
#             its turn has already ended ─────────────────────────────────────
def test_run_card_runs_and_rearms():
    if "_run_card" not in vars(cn.ChuckWindow):
        fail("R1: ChuckWindow._run_card is undefined — the critical Run button is dead")
        return
    w = new_win(["Understood."], tmp)          # one reply for the post-run continuation
    run_btn = cn.Gtk.Button(label="Run")
    status = cn.Gtk.Label(label="")

    # spy on the Send/Stop button icon: _start_run is the ONLY thing that shows
    # the stop glyph, so seeing it proves the lifecycle was re-armed for the
    # deferred run (fix 2).
    seen = []
    orig_set_icon = w.send_btn.set_icon_name
    def spy(n):
        seen.append(n)
        return orig_set_icon(n)
    w.send_btn.set_icon_name = spy

    # the window is idle here — this is the "approved after the turn ended" case
    w._run_card("echo REG_ALPHA_$((3*4))", run_btn, status)
    settle()

    # the computed value 12 can only appear if the command actually executed —
    # the raw command text contains "$((3*4))", not "12".
    if "REG_ALPHA_12" not in hist(w):
        fail("R1: _run_card did not execute the command / feed the real result back")
    if run_btn.get_sensitive():
        fail("R1: Run button was not disarmed after being clicked")
    if "media-playback-stop-symbolic" not in seen:
        fail("R2: the deferred run never armed the Stop button (no watchdog while working)")
    check_finished(w, "run_card")


# ── 3 ── the first runnable block in DOCUMENT order is the one that runs ──────
def test_first_code_block_in_document_order():
    # bash is written FIRST, python SECOND. Only the block that actually runs
    # can reveal its *computed* sentinel — the raw reply only carries the
    # un-evaluated source — so the output tells us unambiguously which ran.
    reply = ("Install, then run the helper:\n"
             "```bash\necho B4SH_$((6*7))\n```\n"
             "```python\nprint('PY' + str(6*7))\n```")
    w = new_win([reply, "All set."], tmp)
    type_and_send(w, "set it up")
    h = hist(w)
    if "B4SH_42" not in h:
        fail("R3: the first (bash) block was not the one executed")
    if "PY42" in h:
        fail("R3: a later python block ran instead of the earlier bash block")
    check_finished(w, "doc_order")


# ── 4 ── cancelling speech mid-playback must not leak the synthesis thread ────
def test_voice_cancel_does_not_hang():
    import chucknorris_ext.voice as V
    made = []

    def fake_synth(chunk, gen, s):
        p = str(Path(tempfile.gettempdir()) /
                f".regvoice-{abs(hash((chunk, gen))) % 10 ** 8}.wav")
        with open(p, "wb") as fh:
            fh.write(b"\x00" * 128)
        made.append(p)
        return p

    started = threading.Event()

    def fake_play(path, gen=None):
        started.set()
        while gen is not None and gen == V._TTS_GEN[0]:   # "play" until superseded
            time.sleep(0.02)
        return False

    orig_synth, orig_play = V._synth, V._play
    V._synth, V._play = fake_synth, fake_play
    try:
        base = set(threading.enumerate())
        # A long passage → many 260-char chunks → with instant synth and a
        # blocked player the producer is guaranteed to be sitting on a full
        # queue when we cancel. A short line collapses to one chunk and never
        # exercises the deadlock.
        V.speak("word " * 500, {"tts": True})
        if not started.wait(3):
            fail("R4: playback never started — could not exercise the cancel path")
            return
        time.sleep(0.4)                 # let the producer block on the full queue
        V.stop_speaking()               # cancel mid-playback

        # bounded wait: the fixed producer bails from its put loop at once; its
        # final _TTS_END put has a 5s timeout, so everything drains inside ~5s.
        # The old unbounded put would hang this thread forever.
        deadline = time.time() + 10
        leaked = None
        while time.time() < deadline:
            leaked = [t for t in threading.enumerate()
                      if t not in base
                      and t is not threading.current_thread() and t.is_alive()]
            if not leaked:
                break
            time.sleep(0.1)
        if leaked:
            fail(f"R4: {len(leaked)} voice thread(s) still alive after cancel — "
                 "producer deadlocked")
    finally:
        V._synth, V._play = orig_synth, orig_play
        for p in made:
            try:
                os.unlink(p)
            except Exception:
                pass


for _t in (test_run_card_runs_and_rearms,
           test_first_code_block_in_document_order,
           test_voice_cancel_does_not_hang):
    try:
        _t()
    except Exception as _e:
        import traceback
        traceback.print_exc()
        fail(f"{_t.__name__} raised: {_e}")

print()
print("TOTAL REGRESSION FAILURES:", len(FAILS))
for _f in FAILS:
    print("  ", _f)
assert not FAILS, "regression failures"
print("ALL REGRESSION TESTS PASSED")
