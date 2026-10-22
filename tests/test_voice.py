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
print("--- the natural voice: never downloaded twice, never deleted for a runtime error ---")
import hashlib as _hl
V = cn.voice
_kd = pathlib.Path(tempfile.mkdtemp())
_saved = (V.KOKORO_MODEL, V.KOKORO_VOICES, dict(V.KOKORO_SHA256), V.ensure_venv, V.test_kokoro, V.KOKORO_URLS)
V.KOKORO_MODEL, V.KOKORO_VOICES = _kd / "kokoro-v1.0.onnx", _kd / "voices-v1.0.bin"
_body = {V.KOKORO_MODEL.name: b"M" * 3000, V.KOKORO_VOICES.name: b"V" * 2000}
V.KOKORO_SHA256.clear()
V.KOKORO_SHA256.update({n: (len(b), _hl.sha256(b).hexdigest()) for n, b in _body.items()})
V.KOKORO_URLS = {n: "https://x/" + n for n in _body}
downloads = []


class _Resp:
    def __init__(self, data): self.data, self.headers = data, {"Content-Length": str(len(data))}
    def read(self, n=-1): d, self.data = self.data[:n], self.data[n:]; return d
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Opener:
    def __init__(self, corrupt=False): self.corrupt = corrupt
    def open(self, req, timeout=0):
        name = req.full_url.rsplit("/", 1)[1]
        downloads.append(name)
        return _Resp(b"X" * len(_body[name]) if self.corrupt else _body[name])


V.ensure_venv = lambda *a, **k: (True, "already installed")
V.test_kokoro = lambda voice=None: (False, "Kokoro failed: InvalidArgument: [ONNXRuntimeError] : 2 : "
                                           "INVALID_ARGUMENT : Unexpected input data type")
ok_, msg_ = V.install_kokoro(opener=_Opener())
if sorted(downloads) != sorted(_body):
    fail(f"first install should download both files, got {downloads}")
if not (V.KOKORO_MODEL.exists() and V.KOKORO_VOICES.exists()):
    fail("a runtime error (InvalidArgument) deleted the intact model files")
downloads.clear()
V.install_kokoro(opener=_Opener())
if downloads:
    fail(f"intact files were downloaded again: {downloads}")
V.KOKORO_MODEL.write_bytes(b"M" * 2999 + b"!")              # damaged, right size
V.install_kokoro(opener=_Opener())
if V.KOKORO_MODEL.exists():
    fail("a damaged model (checksum mismatch) was kept after the test failed")
V.KOKORO_MODEL.unlink(missing_ok=True); downloads.clear()
ok_, msg_ = V.install_kokoro(opener=_Opener(corrupt=True))
if ok_ or V.KOKORO_MODEL.exists() or "damaged" not in msg_:
    fail(f"a corrupt download must be refused, not kept: {ok_} {msg_}")
V.KOKORO_MODEL, V.KOKORO_VOICES, _sha, V.ensure_venv, V.test_kokoro, V.KOKORO_URLS = _saved
V.KOKORO_SHA256.clear(); V.KOKORO_SHA256.update(_sha)
print("  intact files kept and reused; damaged ones replaced")

print("--- the voice server feeds the model the types it declares ---")
_ks = importlib.util.spec_from_file_location("ks", "chucknorris_ext/kokoro_server.py")
ks = importlib.util.module_from_spec(_ks); _ks.loader.exec_module(ks)
try:
    import numpy as _np
except ImportError:
    _np = None
if _np is not None:
    class _In:
        def __init__(self, n, t): self.name, self.type = n, t

    class _Sess:
        def __init__(self, types): self.types, self.fed = types, None
        def get_inputs(self): return [_In(n, t) for n, t in self.types.items()]
        def run(self, out, feed): self.fed = feed; return [None]

    class _Eng:
        def __init__(self, sess): self.sess = sess
    for types, want in (({"input_ids": "tensor(int64)", "style": "tensor(float)", "speed": "tensor(float)"},
                         ("float32", 0.97)),
                        ({"tokens": "tensor(int64)", "style": "tensor(float)", "speed": "tensor(int32)"},
                         ("int32", 1))):
        sess, sp = _Sess(types), [0.97]
        ks.fit_inputs(_Eng(sess), _np, sp)
        sess.run(None, {"input_ids" if "input_ids" in types else "tokens": _np.array([[1, 2]], dtype=_np.int64),
                        "style": _np.zeros((1, 256), dtype=_np.float64),
                        "speed": _np.array([0], dtype=_np.int32)})    # what kokoro-onnx 0.5 sends
        got = sess.fed["speed"]
        if str(got.dtype) != want[0] or abs(float(got[0]) - want[1]) > 1e-6 or sess.fed["style"].dtype != _np.float32:
            fail(f"inputs not fitted to {types}: speed {got.dtype} {got}, style {sess.fed['style'].dtype}")
    print("  speed and style match the model; the real speed is passed, not 0")

print("TOTAL VOICE FAILURES:", len(FAILS))
for f in FAILS:
    print("  ", f)
assert not FAILS, "voice robustness regressions"
print("ALL VOICE TESTS PASSED")
