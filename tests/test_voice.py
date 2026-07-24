import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""Voice robustness — the reply must be spoken IN FULL and never wedge.

The original bug: text was truncated to 800 chars, so any longer reply stopped
mid-sentence and never resumed. These tests keep that shut, and cover the
failure modes a chunked speech pipeline introduces.
"""
import tempfile, time, threading, pathlib
import gtkstub
gtkstub.install()
import importlib.util
_s = importlib.util.spec_from_file_location("cn", "chucknorris.py")
cn = importlib.util.module_from_spec(_s); _s.loader.exec_module(cn)

FAILS = []
def fail(m):
    FAILS.append(m); print("   *** FAIL:", m)


class R:
    returncode = 0


def _wav_out(cmd):
    return [a for i, a in enumerate(cmd) if cmd[i - 1] in ("-f", "-w")]


def settle(limit=120):
    for _ in range(limit):
        if not [t for t in threading.enumerate()
                if t is not threading.current_thread() and t.is_alive()]:
            return
        time.sleep(0.05)


cn.voice.CONFIG_DIR = pathlib.Path(tempfile.mkdtemp())
cn.CONFIG_DIR = cn.voice.CONFIG_DIR
cn.voice.shutil.which = lambda n: "/usr/bin/" + n if n in ("piper", "espeak-ng", "paplay") else None
cn.voice._find_piper_model = lambda: "/fake/model.onnx"

print("--- long replies are spoken in full (the 800-char truncation bug) ---")
long_reply = ("Right, here is the situation you are dealing with. " * 80).strip()
clean = cn.voice.tts_clean(long_reply)
chunks = cn.voice.tts_chunks(clean)
queued = sum(len(c) for c in chunks)
print(f"  reply={len(long_reply)} cleaned={len(clean)} chunks={len(chunks)} queued={queued}")
if queued < len(clean) - len(chunks):
    fail(f"text lost: queued {queued} of {len(clean)}")
if len(clean) <= 800:
    fail("test text too short to prove the truncation fix")

print("--- no chunk exceeds the cap; edge inputs are safe ---")
for name, txt in [("empty", ""), ("spaces", "   "), ("one word", "hi"),
                  ("no punctuation", "word " * 400),
                  ("giant sentence", "a" * 4000),
                  ("unicode", "Grüße — naïve café… 日本語 test."),
                  ("newlines", "\n\n\n"), ("none", None)]:
    try:
        ch = cn.voice.tts_chunks(cn.voice.tts_clean(txt))
    except Exception as e:
        fail(f"chunking crashed on {name}: {e}"); continue
    if any(len(c) > cn.voice._TTS_CHUNK for c in ch):
        fail(f"{name}: chunk over the cap")
print("  all edge inputs chunked safely")

print("--- cleaning: code, URLs and markdown are not read aloud ---")
msg = ("## Heading\nUse `pacman -Syu`. See [the wiki](https://wiki.archlinux.org/x) "
       "or https://example.com/a/b?q=1\n- **one**\n- _two_\n"
       "```bash\nrm -rf /tmp/x\n```\nDone.")
c = cn.voice.tts_clean(msg)
for bad in ("```", "http", "**", "##", "`", "rm -rf"):
    if bad in c:
        fail(f"{bad!r} survived cleaning: {c!r}")
if "the wiki" not in c:
    fail("link label lost in cleaning")
print("  ", repr(c[:70]))

print("--- every chunk reaches the synthesiser ---")
synth = []
def fake_run(cmd, **kw):
    synth.append(kw.get("input", ""))
    o = _wav_out(cmd)
    if o:
        open(o[0], "wb").write(b"RIFF" + b"\0" * 200)
    return R()
class P:
    def __init__(s, cmd, **k): s.n = 0
    def poll(s):
        s.n += 1; return 0 if s.n > 1 else None
    def terminate(s): pass
    def wait(s, **k): pass
    def kill(s): pass
cn.voice.subprocess.run = fake_run
cn.voice.subprocess.Popen = P
cn.voice.speak(long_reply, {})
settle()
print(f"  chunks={len(chunks)} synthesised={len(synth)}")
if len(synth) != len(chunks):
    fail(f"only {len(synth)} of {len(chunks)} chunks synthesised")

print("--- the worker exits promptly (no 120s park on the sentinel) ---")
synth.clear()
t0 = time.time()
cn.voice.speak("One. Two. Three. Four. Five.", {})
settle()
el = time.time() - t0
print(f"  finished in {el:.2f}s")
if el > 10:
    fail(f"speech worker hung for {el:.1f}s")

print("--- one failed chunk does not kill the rest ---")
played = []
calls = {"n": 0}
def flaky(cmd, **kw):
    calls["n"] += 1
    o = _wav_out(cmd)
    if calls["n"] == 2:
        raise RuntimeError("engine exploded")
    if o:
        open(o[0], "wb").write(b"RIFF" + b"\0" * 200)
    return R()
class P2:
    def __init__(s, cmd, **k): played.append(cmd[-1]); s.n = 0
    def poll(s):
        s.n += 1; return 0 if s.n > 1 else None
    def terminate(s): pass
    def wait(s, **k): pass
    def kill(s): pass
cn.voice.subprocess.run = flaky
cn.voice.subprocess.Popen = P2
many = ". ".join(f"Sentence number {i} of this reply" for i in range(30))
nchunks = len(cn.voice.tts_chunks(cn.voice.tts_clean(many)))
cn.voice.speak(many, {})
settle()
print(f"  chunks={nchunks} played={len(played)} (one engine failure injected)")
if len(played) < nchunks - 1:
    fail(f"a single failed chunk aborted playback ({len(played)}/{nchunks})")

print("--- stop silences immediately ---")
cn.voice.subprocess.run = fake_run
class SlowP:
    terms = []
    def __init__(s, cmd, **k): s.t = time.time(); s.k = False
    def poll(s): return None if (time.time() - s.t) < 5 and not s.k else 0
    def terminate(s): s.k = True; SlowP.terms.append(1)
    def wait(s, **k): pass
    def kill(s): s.k = True
cn.voice.subprocess.Popen = SlowP
cn.voice.speak("One. Two. Three. Four. Five. Six. Seven.", {})
time.sleep(0.3)
g = cn.voice._TTS_GEN[0]
cn.voice.stop_speaking()
time.sleep(0.4)
if cn.voice._TTS_GEN[0] <= g:
    fail("stop_speaking did not bump the generation")
if not SlowP.terms:
    fail("playback was not terminated on stop")
print("  player terminated on stop:", bool(SlowP.terms))
settle()

print("--- a new reply cancels the previous one (no overlap) ---")
SlowP.terms.clear()
cn.voice.speak("First reply speaking.", {})
time.sleep(0.2)
g1 = cn.voice._TTS_GEN[0]
cn.voice.speak("Second reply interrupts.", {})
time.sleep(0.3)
if cn.voice._TTS_GEN[0] <= g1:
    fail("new speech did not supersede the old")
print("  superseded:", cn.voice._TTS_GEN[0] > g1, "| old playback stopped:", bool(SlowP.terms))
settle()

print("--- voice settings are applied ---")
seen = []
def capture(cmd, **kw):
    seen.append(cmd)
    o = _wav_out(cmd)
    if o:
        open(o[0], "wb").write(b"RIFF" + b"\0" * 200)
    return R()
cn.voice.subprocess.run = capture
cn.voice.subprocess.Popen = P
cn.voice.speak("Testing speed.", {"voice_engine": "espeak", "voice_speed": 1.5, "voice_pitch": 55})
settle()
esp = [c for c in seen if c and c[0] == "espeak-ng"]
if not esp:
    fail("voice_engine=espeak was ignored")
else:
    print("  espeak args:", " ".join(esp[0][:9]))
    if "-s" not in esp[0] or "225" not in esp[0]:
        fail(f"voice_speed not applied: {esp[0]}")
    if "55" not in esp[0]:
        fail(f"voice_pitch not applied: {esp[0]}")

print("--- no temp wavs left behind ---")
stray = list(pathlib.Path(cn.CONFIG_DIR).glob(".say-*.wav"))
print("  stray wavs:", len(stray))
if stray:
    fail(f"{len(stray)} temp wav files leaked")

print()
print("TOTAL VOICE FAILURES:", len(FAILS))
for f in FAILS:
    print("  ", f)
assert not FAILS, "voice robustness regressions"
print("ALL VOICE TESTS PASSED")
