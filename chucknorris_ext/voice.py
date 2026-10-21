"""voice.py — text to speech: cleaning, chunking, synthesis, playback.

Part of Chuck Norris — split out of the original single file.
"""
import os
import re
import shutil
import itertools
import json
import subprocess
import threading
import time
from pathlib import Path

from . import config

CONFIG_DIR = config.CONFIG_DIR
VOICE_DIR = config.VOICE_DIR
_SETTINGS = config.SETTINGS_DATA
# Chuck's own Piper, in a private virtualenv: no PATH trouble (~/.local/bin is
# often missing from a desktop session's PATH), no PEP 668 fights with the
# system Python, and no clash with Arch's /usr/bin/piper, which is a gaming
# mouse configuration tool and not text to speech at all.
PIPER_VENV = config.DATA_DIR / "piper"
VOICE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/high"
VOICE_NAME = "en_US-ryan-high"
LAST_ERROR = {"piper": "", "told": False}   # why Piper last failed, for /doctor and a note

# ── Kokoro: the natural voice ───────────────────────────────────────────────
# Kokoro-82M (Apache 2.0) through kokoro-onnx, in its own venv. It is the most
# natural of the open local voices, and with the full-precision model it runs
# well ahead of real time on a 4-core CPU, so speech streams sentence by
# sentence. The model stays loaded in kokoro_server.py: loading costs seconds.
VOICE_VENV = config.DATA_DIR / "voice"         # shared by the voice and voice input
KOKORO_DIR = VOICE_DIR / "kokoro"
KOKORO_MODEL = KOKORO_DIR / "kokoro-v1.0.onnx"
KOKORO_VOICES = KOKORO_DIR / "voices-v1.0.bin"
KOKORO_URLS = {
    KOKORO_MODEL.name: "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
                       "model-files-v1.1/kokoro-v1.0.onnx",
    KOKORO_VOICES.name: "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
                        "model-files-v1.1/voices-v1.0.bin",
}
KOKORO_RATE = 24000
# Chuck's voice: Kokoro's two clearest American male voices, blended. Kokoro's
# own grades put af_heart (A) and af_bella (A-) highest of all.
DEFAULT_KOKORO_VOICE = "am_michael:0.6,am_fenrir:0.4"
KOKORO_CHOICES = [
    (DEFAULT_KOKORO_VOICE, "Chuck \u2014 deep, steady (Michael + Fenrir)"),
    ("af_heart", "Heart \u2014 warm, the most natural"),
    ("af_bella", "Bella \u2014 bright, expressive"),
    ("am_michael", "Michael \u2014 calm American"),
    ("am_fenrir", "Fenrir \u2014 deeper American"),
    ("am_puck", "Puck \u2014 lively American"),
    ("am_onyx", "Onyx \u2014 low American"),
    ("af_nicole", "Nicole \u2014 soft, close"),
    ("af_sarah", "Sarah \u2014 clear American"),
    ("bm_george", "George \u2014 British"),
    ("bm_fable", "Fable \u2014 British storyteller"),
    ("bf_emma", "Emma \u2014 British"),
]
_KOKORO_SERVER = Path(__file__).with_name("kokoro_server.py")


def voice_python():
    p = VOICE_VENV / "bin" / "python"
    return str(p) if p.exists() else None


kokoro_python = voice_python


def kokoro_ready():
    return bool(voice_python() and KOKORO_MODEL.exists() and KOKORO_VOICES.exists())


def ensure_venv(packages, module, progress=lambda s: None):
    """The voice venv with `packages` installed (checked by importing
    `module`). -> (ok, message)."""
    import sys
    py = voice_python()
    if py and subprocess.run([py, "-c", f"import {module}"], capture_output=True,
                             timeout=120).returncode == 0:
        return True, "already installed"
    if not py or not (VOICE_VENV / "bin" / "pip").exists():
        # (a venv created without pip, e.g. Debian before python3-venv was
        # installed, is rebuilt rather than reused forever)
        progress("creating the voice's own Python environment")
        base = sys.executable if os.path.basename(sys.executable or "").startswith("python") else "python3"
        r = subprocess.run([base, "-m", "venv", "--clear", str(VOICE_VENV)], capture_output=True,
                           text=True, timeout=180)
        if r.returncode != 0:
            return False, ("couldn't create a virtualenv (on Debian/Ubuntu: apt install "
                           "python3-venv): " + (r.stderr or r.stdout).strip()[-200:])
    progress(f"installing {', '.join(packages)} (pip)")
    r = subprocess.run([str(VOICE_VENV / "bin" / "pip"), "install", "-q", "-U", *packages],
                       capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        return False, f"pip install {' '.join(packages)} failed: " + (r.stderr or r.stdout).strip()[-300:]
    return True, "installed"


class _KokoroEngine:
    """The warm Kokoro process: started on first use, restarted if it dies."""

    def __init__(self):
        self.proc = None
        self.lines = None
        self.lock = threading.Lock()
        self.error = ""
        self.seq = 0
        self.epoch = 0                  # bumped by every stop(): a synth cut by one isn't a crash

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _start(self, timeout=90):
        import queue
        py = kokoro_python()
        if not py:
            raise RuntimeError("Kokoro isn't installed")
        self.proc = subprocess.Popen([py, "-u", str(_KOKORO_SERVER), str(KOKORO_MODEL),
                                      str(KOKORO_VOICES)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, start_new_session=True)
        q = self.lines = queue.Queue()
        out = self.proc.stdout

        def read():
            for ln in out:
                q.put(ln)
            q.put(None)
        threading.Thread(target=read, daemon=True, name="kokoro-reader").start()
        msg = self._reply(timeout)
        if not msg.get("ready"):
            self.stop()
            raise RuntimeError(msg.get("error") or "Kokoro didn't start")

    def _reply(self, timeout):
        import queue
        try:
            ln = self.lines.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError(f"Kokoro didn't answer within {timeout}s")
        if ln is None:
            raise RuntimeError("Kokoro stopped")
        return json.loads(ln)

    def warm(self):
        with self.lock:
            if not self.alive():
                self._start()

    def synth(self, text, voice, speed, out, timeout=120):
        """Render text to raw 24 kHz 16-bit PCM at `out`."""
        with self.lock:
            if not self.alive():
                self._start()
            self.seq += 1
            rid = str(self.seq)
            self.proc.stdin.write(json.dumps({"id": rid, "text": text, "voice": voice,
                                              "speed": speed, "out": out}) + "\n")
            self.proc.stdin.flush()
            while True:
                msg = self._reply(timeout)
                if msg.get("id") == rid:
                    break
            if not msg.get("ok"):
                raise RuntimeError(msg.get("error") or "synthesis failed")
            return msg.get("samples", 0)

    def stop(self):
        self.epoch += 1
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=3)
            except Exception:
                pass
        self.proc = None


KOKORO = _KokoroEngine()
_KOKORO_DOWN = [0.0]          # until when Kokoro is skipped after it broke mid-reply


def engine_for(s):
    """Which engine will speak: kokoro, piper or espeak (None if none can)."""
    want = (s.get("voice_engine") or "auto").lower()
    if want in ("auto", "kokoro") and kokoro_ready() and time.time() >= _KOKORO_DOWN[0]:
        return "kokoro"
    if want in ("auto", "piper") and piper_bin() and _find_piper_model():
        return "piper"
    if want in ("auto", "espeak") and _have("espeak-ng"):
        return "espeak"
    if want == "kokoro" and piper_bin() and _find_piper_model():
        return "piper"                      # asked for Kokoro but it isn't available
    return "espeak" if _have("espeak-ng") else None


def warm_up(s):
    """Start Kokoro in the background so the first reply speaks at once."""
    if engine_for(s) == "kokoro":
        threading.Thread(target=lambda: _safe(KOKORO.warm), daemon=True,
                         name="kokoro-warm").start()


def _safe(fn):
    try:
        fn()
    except Exception as ex:
        LAST_ERROR["kokoro"] = str(ex)


def _download(url, dest, progress, opener=None, label=""):
    import urllib.request
    op = opener.open if opener else urllib.request.urlopen
    tmp = Path(str(dest) + ".part")
    with op(urllib.request.Request(url, headers={"User-Agent": "chuck"}), timeout=60) as r, \
            open(tmp, "wb") as fh:
        total = int(getattr(r, "headers", {}).get("Content-Length") or 0) if hasattr(r, "headers") else 0
        got, last = 0, 0.0
        while True:
            buf = r.read(1 << 20)
            if not buf:
                break
            fh.write(buf)
            got += len(buf)
            if time.time() - last > 0.5:
                last = time.time()
                size = (lambda n: f"{n >> 20} MB" if n >= 1 << 20 else f"{n >> 10} KB")
                progress(f"downloading {label} \u2014 {size(got)}"
                         + (f" of {size(total)}" if total else ""))
    if total and got != total:                 # the connection dropped: not a finished file
        try:
            tmp.unlink()
        except OSError:
            pass
        raise RuntimeError(f"download of {label} was cut off ({got >> 20} of {total >> 20} MB) "
                           "\u2014 try again")
    os.replace(tmp, dest)


INSTALL_LOCK = threading.Lock()            # one voice install at a time (shared venv)


def install_kokoro(progress=lambda s: None, opener=None):
    """Install the natural voice: a venv with kokoro-onnx plus the model and
    voices (about 350 MB). -> (ok, message). Safe to re-run."""
    with INSTALL_LOCK:
        return _install_kokoro(progress, opener)


def _install_kokoro(progress, opener):
    try:
        ok, msg = ensure_venv(["kokoro-onnx"], "kokoro_onnx", progress)
        if not ok:
            return False, msg
        KOKORO_DIR.mkdir(parents=True, exist_ok=True)
        for dest, label in ((KOKORO_VOICES, "the voices"), (KOKORO_MODEL, "the voice model")):
            if dest.exists() and dest.stat().st_size > 1_000_000:
                continue
            _download(KOKORO_URLS[dest.name], dest, progress, opener, label)
    except Exception as ex:
        return False, f"{ex.__class__.__name__}: {ex}"
    stop_speaking()                         # a reply mid-sentence is cut, not "broken"
    KOKORO.stop()
    _KOKORO_DOWN[0] = 0.0
    progress("testing the voice")
    ok, why = test_kokoro()
    if not ok and re.search(r"protobuf|invalid|corrupt|load model|zip|pickle|npz|"
                            r"unexpected end|truncat", why, re.I):
        # the files themselves are bad: don't keep them, so a retry downloads
        # afresh (a slow start or a missing library keeps the 350 MB download)
        for f in (KOKORO_MODEL, KOKORO_VOICES):
            try:
                f.unlink()
            except OSError:
                pass
    return (True, "natural voice ready (Kokoro)") if ok else (False, why)


def test_kokoro(voice=None):
    if not kokoro_ready():
        return False, "Kokoro isn't installed \u2014 Settings \u203a Voice \u203a Install natural voice"
    out = str(CONFIG_DIR / f".say-test-{os.getpid()}.pcm")
    try:
        n = KOKORO.synth("Chuck Norris approves this voice.", voice or DEFAULT_KOKORO_VOICE,
                         1.0, out, timeout=120)
        LAST_ERROR["kokoro"] = ""
        _KOKORO_DOWN[0] = 0.0                  # it works again: use it again
        LAST_ERROR["kokoro_told"] = False
        return (True, "Kokoro works") if n > KOKORO_RATE // 2 else (False, "Kokoro made no audio")
    except Exception as ex:
        LAST_ERROR["kokoro"] = str(ex)
        return False, f"Kokoro failed: {ex}"
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def voice_status(s=None):
    """(state, detail) of the voice that will actually speak."""
    s = s or _SETTINGS
    if kokoro_ready() and time.time() < _KOKORO_DOWN[0]:
        return "fail", (f"Kokoro failed: {LAST_ERROR.get('kokoro', '')[:140]} "
                        "\u2014 another voice speaks meanwhile")
    eng = engine_for(s)
    if eng == "kokoro":
        if LAST_ERROR.get("kokoro"):
            return "fail", f"Kokoro failed: {LAST_ERROR['kokoro'][:160]}"
        return "ok", "Kokoro (natural) \u00b7 " + dict(KOKORO_CHOICES).get(
            s.get("kokoro_voice") or DEFAULT_KOKORO_VOICE, s.get("kokoro_voice") or "").split(" \u2014")[0]
    if eng == "piper":
        st, det = piper_status()
        return ("warn" if st == "ok" else st), det + " \u2014 Install natural voice for Kokoro"
    if eng == "espeak":
        return "warn", "espeak-ng (robotic) \u2014 Settings \u203a Voice \u203a Install natural voice"
    return "fail", "no voice engine \u2014 Settings \u203a Voice \u203a Install natural voice"

# ── voice: natural Piper; espeak-ng fallback ────────────────────────────────
_WHICH_CACHE = {}


def _have(name):
    """shutil.which(), memoised. Called per synthesis chunk and again per
    playback — a long reply is dozens of chunks, so this was dozens of PATH
    walks per spoken answer for an answer that cannot change at runtime."""
    hit = _WHICH_CACHE.get(name)
    if hit is None:
        hit = shutil.which(name) or ""
        _WHICH_CACHE[name] = hit
    return hit or None


_MODEL_CACHE = {"key": object(), "path": None}


def _find_piper_model():
    """The voice model to speak with. Cached against the configured path, so
    the VOICE_DIR glob happens once rather than once per chunk — and still
    re-resolves the moment the user points at a different model in Settings.
    A model is only usable with its .onnx.json config beside it: Piper can't
    load one without, and every sentence would fail."""
    m = (_SETTINGS.get("piper_model") or "").strip()
    if _MODEL_CACHE["key"] == m:
        return _MODEL_CACHE["path"]
    found = None
    if m and Path(m).exists() and Path(m + ".json").exists():
        found = m
    else:
        for f in sorted(VOICE_DIR.glob("*.onnx")):
            if Path(str(f) + ".json").exists():
                found = str(f)
                break
    _MODEL_CACHE.update({"key": m, "path": found})
    return found


_PIPER_CACHE = {}


def _is_tts_piper(path):
    """True unless the file is clearly the OTHER piper (libratbag's GTK mouse
    configurator, a Python script importing piper.application and Gtk)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8192)
    except OSError:
        return True                       # can't look: let a real run decide
    if head.startswith(b"\x7fELF"):
        return True                       # the native piper release binary
    bad = (b"piper.application", b"gi.require_version", b"ratbag")
    return not any(b in head for b in bad)


def piper_bin():
    """The Piper text-to-speech command, or None: Chuck's own venv first, then
    ~/.local/bin (pip --user), then PATH, skipping a piper that isn't TTS."""
    want = (_SETTINGS.get("piper_bin") or "").strip()
    if want in _PIPER_CACHE:
        return _PIPER_CACHE[want]
    found = None
    for cand in ([want] if want else []) + [str(PIPER_VENV / "bin" / "piper"),
                                            str(Path.home() / ".local" / "bin" / "piper"),
                                            _have("piper") or ""]:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK) and _is_tts_piper(cand):
            found = cand
            break
        if cand and cand == _have("piper") and _is_tts_piper(cand):
            found = cand                  # on PATH (the test stub has no real file)
            break
    _PIPER_CACHE[want] = found
    return found


ON_PIPER_FAIL = [None]          # the app's "say it once" callback (Piper)
ON_VOICE_FAIL = [None]          # the same for any engine: (engine, why)


def _piper_failed(why):
    LAST_ERROR["piper"] = (why or "unknown error").strip()[-400:]
    if not LAST_ERROR["told"] and ON_PIPER_FAIL[0] is not None:
        LAST_ERROR["told"] = True
        try:
            ON_PIPER_FAIL[0](LAST_ERROR["piper"])
        except Exception:
            pass


def piper_status():
    """(state, detail) for /doctor and Settings: ok, warn or fail."""
    b, m = piper_bin(), _find_piper_model()
    if b and m:
        if LAST_ERROR["piper"]:
            return "fail", f"Piper failed: {LAST_ERROR['piper'].splitlines()[-1][:160]}"
        return "ok", f"Piper \u00b7 {Path(m).stem}"
    if not b and not m:
        return "warn", "Piper isn't installed \u2014 Settings \u203a Voice \u203a Install natural voice"
    if not b:
        return "warn", "a voice model is here but Piper isn't \u2014 Settings \u203a Voice \u203a Install"
    return "warn", "Piper is installed but there's no voice model (with its .json) \u2014 Install"


def install_piper(progress=lambda s: None, opener=None, _retry=True):
    """Install Piper into Chuck's venv and download the default voice.
    -> (ok, message). Safe to re-run; nothing outside DATA_DIR is touched.
    A voice already on disk that doesn't work is downloaded again, once."""
    import sys
    import urllib.request
    try:
        if not (PIPER_VENV / "bin" / "piper").exists():
            progress("creating Piper's own Python environment")
            r = subprocess.run([sys.executable if os.path.basename(sys.executable).startswith(
                "python") else "python3", "-m", "venv", str(PIPER_VENV)],
                capture_output=True, text=True, timeout=180)
            if r.returncode != 0:
                return False, ("couldn't create a virtualenv (on Debian/Ubuntu: apt install "
                               "python3-venv): " + (r.stderr or r.stdout).strip()[-200:])
            progress("downloading Piper (pip install piper-tts)")
            r = subprocess.run([str(PIPER_VENV / "bin" / "pip"), "install", "-q", "-U", "piper-tts"],
                               capture_output=True, text=True, timeout=900)
            if r.returncode != 0:
                return False, "pip install piper-tts failed: " + (r.stderr or r.stdout).strip()[-300:]
        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        model = VOICE_DIR / f"{VOICE_NAME}.onnx"
        for name in (f"{VOICE_NAME}.onnx.json", f"{VOICE_NAME}.onnx"):
            dest = VOICE_DIR / name
            if dest.exists() and dest.stat().st_size > 0:
                continue
            progress(f"downloading the voice ({name})")
            tmp = dest.with_suffix(dest.suffix + ".part")
            op = opener.open if opener else urllib.request.urlopen
            with op(urllib.request.Request(f"{VOICE_URL}/{name}",
                                           headers={"User-Agent": "chuck"}), timeout=60) as r, \
                    open(tmp, "wb") as fh:
                shutil.copyfileobj(r, fh)
            os.replace(tmp, dest)
    except Exception as ex:
        return False, f"{ex.__class__.__name__}: {ex}"
    _PIPER_CACHE.clear()
    _MODEL_CACHE["key"] = object()
    LAST_ERROR.update(piper="", told=False)
    ok, why = test_piper()
    if not ok and _retry and "No such file" not in why:
        for name in (f"{VOICE_NAME}.onnx", f"{VOICE_NAME}.onnx.json"):
            try:
                (VOICE_DIR / name).unlink()
            except OSError:
                pass
        _MODEL_CACHE["key"] = object()
        return install_piper(progress, opener, _retry=False)
    return (True, f"natural voice ready ({model.name})") if ok else (False, why)


def test_piper():
    """Synthesise one short sentence with Piper -> (ok, detail)."""
    b, m = piper_bin(), _find_piper_model()
    if not (b and m):
        return False, piper_status()[1]
    out = str(CONFIG_DIR / f".say-test-{os.getpid()}.wav")
    try:
        r = subprocess.run([b, "-m", m, "-f", out], input="Chuck Norris approves this voice.",
                           text=True, capture_output=True, timeout=120)
        if os.path.exists(out) and os.path.getsize(out) > 1000:
            LAST_ERROR["piper"] = ""
            return True, f"Piper works ({Path(m).stem})"
        _piper_failed((r.stderr or r.stdout or f"exit {r.returncode}"))
        return False, "Piper failed: " + LAST_ERROR["piper"].splitlines()[-1][:200]
    except Exception as ex:
        _piper_failed(str(ex))
        return False, f"Piper failed: {ex}"
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def _play(path, gen=None):
    """Play a wav, abortable. Returns True if it played to the end."""
    for player in (["paplay", path], ["aplay", "-q", path],
                   ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path]):
        if not _have(player[0]):
            continue
        try:
            proc = subprocess.Popen(player, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            while proc.poll() is None:
                if gen is not None and gen != _TTS_GEN[0]:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        proc.kill()
                    return False
                time.sleep(0.05)
            return True
        except Exception:
            return False
    return False


_RAW_PLAYERS = (
    ["pw-play", "--rate", str(KOKORO_RATE), "--channels", "1", "--format", "s16", "-"],
    ["paplay", "--raw", f"--rate={KOKORO_RATE}", "--channels=1", "--format=s16le"],
    ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(KOKORO_RATE), "-c", "1"],
    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-f", "s16le", "-ar",
     str(KOKORO_RATE), "-ac", "1", "-i", "-"],
)
_ACTIVE_PLAYER = [None]


def _raw_player_argv():
    want = (_SETTINGS.get("voice_player") or "").strip()
    if want:
        return want.split()
    for argv in _RAW_PLAYERS:
        if _have(argv[0]):
            return argv
    return None


class _Stream:
    """ONE player process for a whole reply: chunks are written into it back
    to back, so there are no gaps or clicks between sentences."""

    def __init__(self, gen):
        self.gen = gen
        self.proc = None

    def write(self, pcm):
        if self.proc is None:
            argv = _raw_player_argv()
            if not argv:
                return False
            self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
            _ACTIVE_PLAYER[0] = self.proc
        step = KOKORO_RATE // 5 * 2               # 0.2 s at a time: Stop is never far off
        try:
            for i in range(0, len(pcm), step):
                if self.gen != _TTS_GEN[0]:
                    self.kill()
                    return False
                self.proc.stdin.write(pcm[i:i + step])
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def finish(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        while self.proc.poll() is None:            # let the tail play out
            if self.gen != _TTS_GEN[0]:
                self.kill()
                return
            time.sleep(0.05)

    def kill(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:
                pass


# ── voice ───────────────────────────────────────────────────────────────────
# Speech used to be one subprocess call on text truncated to 800 chars, which
# meant any longer reply was cut off mid-sentence and never resumed. Now the
# text is cleaned, split into sentence-sized chunks, and synthesised one chunk
# ahead of playback — so nothing is dropped, a single bad chunk can't kill the
# rest, and the whole thing can be stopped instantly.
_TTS_GEN = [0]                     # bump to cancel whatever is speaking
_TTS_START = threading.Lock()
_TTS_CHUNK = 260                   # chars per chunk: short enough to stay responsive
_TTS_END = object()                # distinct end-of-stream marker (None = failed chunk)

_URL_RE = re.compile(r"https?://\S+|www\.\S+")


def stop_speaking():
    """Cancel any in-flight speech immediately."""
    _TTS_GEN[0] += 1
    p = _ACTIVE_PLAYER[0]
    if p is not None and p.poll() is None:
        try:
            p.kill()
        except Exception:
            pass


_ABBR = [(r"\be\.g\.", "for example"), (r"\bi\.e\.", "that is"),
         (r"\betc\.(?=\s+[A-Z]|\s*$)", "et cetera."), (r"\betc\.", "et cetera"),
         (r"\bvs\.?(?=\s)", "versus"), (r"\bapprox\.", "approximately"), (r"\bw/o\b", "without"),
         (r"\bw/(?=\s)", "with"), (r"\bfyi\b", "for your information"), (r"\bimo\b", "in my opinion"),
         (r"\btl;dr\b", "in short")]
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE0F\U0000200D\u2190-\u21FF"
                    "\u2300-\u23FF\u2B00-\u2BFF\u2713\u2714\u2717\u2718]")


def _say_code(m):
    """Short inline code is part of the sentence ("run `make test` first"):
    keep its words, drop the symbols nobody says aloud."""
    code = m.group(1).strip()
    if not code:
        return " "
    if len(code) > 32 or "\n" in code:
        return " the command shown " if re.match(r"(?:sudo |pip|npm|git|apt|pacman|cargo|"
                                                 r"python|make|docker|curl|cd )", code) \
            else " the code shown "
    code = re.sub(r"^.*/", "", code) if "/" in code and " " not in code else code
    return " " + re.sub(r"[-_=<>{}()\[\];$`\\|]+", " ", code).strip() + " "


def tts_clean(text):
    """Turn a chat reply into something worth listening to: no code, tables or
    markup, and the things people write but never say spelled out, so the
    voice reads it like a person would."""
    if not isinstance(text, str):
        return ""
    # code blocks: a fence closes only with one as long (```` holds ```), and
    # one left open (a reply cut short) runs to the end
    s = re.sub(r"(?ms)^[ \t]*(`{3,}|~{3,}).*?(?:^[ \t]*\1[ \t]*$|\Z)", " ", text)
    s = re.sub(r"```.*?```", " ", s, flags=re.DOTALL)        # ...and inline ones
    s = re.sub(r"`([^`]*)`", _say_code, s)                     # inline code (pipes and all)
    s = re.sub(r"(?m)^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$", "", s)   # table rules
    s = re.sub(r"(?m)^\s*\|.*\|\s*$", "", s)                  # table rows: | a | b |
    s = re.sub(r"(?m)^[ \t]*>+[ \t]?", "", s)                 # quote marks (not "greater than")
    s = re.sub(r"<((?:https?|ftp)://[^>\s]+)>", r"\1", s)        # <https://autolinks>
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)               # images
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)            # links -> label
    s = _URL_RE.sub(lambda m: " link" + re.search(r"[.,;:!?)\]]*$", m.group(0)).group(0) + " ",
                    s)                                         # bare URLs (keep the full stop)
    s = re.sub(r"(?<![\w.])(?:~|\.{1,2})?(?:/[\w.+-]+){2,}/?", lambda m: " " + (
        m.group(0).rstrip("/").rsplit("/", 1)[-1]) + " ", s)  # paths: just the name
    # headings and bullets end in a pause, not run into the next line
    s = re.sub(r"(?m)^\s*#{1,6}\s*(.+?)\s*$", lambda m: m.group(1).rstrip(".:") + ".", s)
    s = re.sub(r"(?m)^\s*(?:[-*+]|\d{1,3}[.)])\s+(.+?)\s*$",
               lambda m: m.group(1) + ("" if m.group(1)[-1:] in ".!?:;" else "."), s)
    for pat, rep_ in _ABBR:
        s = re.sub(pat, rep_, s, flags=re.I)
    s = re.sub(r"\s*(?:->|=>|\u2192)\s*", ", then ", s)
    s = re.sub(r"\s+&\s+", " and ", s)
    s = re.sub(r"(?<=\s)~(?=\d)", "about ", s)
    s = re.sub(r"\bv(\d+(?:\.\d+)+)\b", r"version \1", s)
    s = _EMOJI.sub(" ", s)
    s = re.sub(r"(?<=[\w)])[ \t]+>=[ \t]+(?=[\w(])", " at least ", s)
    s = re.sub(r"(?<=[\w)])[ \t]+<=[ \t]+(?=[\w(])", " at most ", s)
    s = re.sub(r"(?<=[\w)])[ \t]+>[ \t]+(?=[\w(])", " greater than ", s)
    s = re.sub(r"(?<=[\w)])[ \t]+<[ \t]+(?=[\w(])", " less than ", s)
    s = re.sub(r"(?<=\d)\s*[*\u00d7]\s*(?=\d)", " times ", s)
    s = re.sub(r"\b([CF])#", r"\1 sharp", s)
    s = re.sub(r"(\*\*|__|~~)(.+?)\1", r"\2", s)               # bold, strike
    s = re.sub(r"(?<!\w)[*_](\S(?:.*?\S)?)[*_](?!\w)", r"\1", s)  # italics
    s = re.sub(r"(?m)^\s*>+\s?", "", s)                        # quote marks
    s = re.sub(r"[|]", " ", s)
    s = re.sub(r"(?<!\w)[*#~](?!\w)", " ", s)                   # stray marks
    s = re.sub(r"\s+([.,!?;:)])", r"\1", s)
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"[ \t]*\n\s*", "\n", s)                       # blank and space-only lines
    return s.strip()


def tts_chunks(text, size=_TTS_CHUNK, first=None):
    """Split into speakable chunks on sentence boundaries where possible.
    `first` caps the first chunk smaller, so speech starts sooner."""
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?:;])\s+|\n+", text)
    # "Dr. Johnson", "the U.S. economy": not sentence ends
    merged = []
    for p in parts:
        if merged and _ABBREV_END.search(merged[-1]):
            merged[-1] = merged[-1] + " " + p
        else:
            merged.append(p)
    parts = merged
    chunks, cur = [], ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        while len(p) > size:                 # a monster sentence: wrap on a space
            cut = p.rfind(" ", 0, size)
            if cut <= 0:
                cut = size
            if cur:
                chunks.append(cur.strip()); cur = ""
            chunks.append(p[:cut].strip())
            p = p[cut:].strip()
        if len(cur) + len(p) + 1 <= size:
            cur = (cur + " " + p).strip()
        else:
            if cur:
                chunks.append(cur.strip())
            cur = p
    if cur:
        chunks.append(cur.strip())
    chunks = [c for c in chunks if c]
    if first and chunks and len(chunks[0]) > first:
        head = tts_chunks(chunks[0], first)
        chunks = head[:1] + ([" ".join(head[1:])] if head[1:] else []) + chunks[1:]
    return chunks


_ABBREV_END = re.compile(r"(?:\b(?:Mr|Mrs|Ms|Dr|Prof|St|Jr|Sr|vs|Inc|Ltd|Co|No|Fig|approx|"
                         r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.|"
                         r"\b(?:[A-Z]\.){1,3})$")
_SYNTH_SEQ = itertools.count()


def _synth(chunk, gen, s):
    """Render one chunk to a wav. Piper preferred, espeak-ng as fallback.
    Returns a path, or None if both engines failed for this chunk."""
    engine = (s.get("voice_engine") or "auto").lower()
    # generous but bounded: scales with length so a long chunk isn't cut short
    tmo = max(20, min(90, 8 + len(chunk) // 8))
    # A monotonic counter, NOT hash(chunk): a reply that repeats a sentence
    # produced two chunks with the same filename, and the consumer's unlink of
    # the first silently deleted the second out from under the producer.
    out = str(CONFIG_DIR / f".say-{gen}-{next(_SYNTH_SEQ)}.wav")
    model = _find_piper_model()
    pbin = piper_bin() if engine in ("auto", "piper") else None
    if pbin and model:
        try:
            cmd = [pbin, "-m", model, "-f", out]
            ls = s.get("voice_speed")
            if ls:
                # piper: length-scale <1 speaks faster. Map 0.5..2.0 speed -> scale
                try:
                    cmd += ["--length-scale", f"{1.0 / float(ls):.3f}"]
                except Exception:
                    pass
            r = subprocess.run(cmd, input=chunk, text=True, timeout=tmo,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if os.path.exists(out) and os.path.getsize(out) > 64:
                return out
            _piper_failed(getattr(r, "stderr", "") or f"exit {getattr(r, 'returncode', '?')}")
        except Exception as ex:
            _piper_failed(str(ex))
    if engine in ("auto", "espeak") and _have("espeak-ng"):
        try:
            rate = int(float(s.get("voice_speed", 1.0)) * 150)
            pitch = int(s.get("voice_pitch", 28))
            subprocess.run(["espeak-ng", "-v", "en-us", "-p", str(max(0, min(99, pitch))),
                            "-s", str(max(80, min(400, rate))), "-g", "3", "-w", out],
                           input=chunk, text=True, timeout=tmo,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(out) and os.path.getsize(out) > 64:
                return out
        except Exception:
            pass
    return None


def speak(text, settings=None):
    """Speak a reply in full. Non-blocking; cancels anything already speaking."""
    s = settings or {}
    clean = tts_clean(text)
    if not clean:
        return
    cap = int(s.get("voice_max_chars", 20000) or 20000)
    clean = clean[:cap]
    # ONE bump, under the lock: it both cancels whatever is speaking and claims
    # this utterance's generation. Bumping outside the lock as well meant two
    # concurrent speak() calls could interleave and each think it was current.
    with _TTS_START:
        _TTS_GEN[0] += 1
        gen = _TTS_GEN[0]

    eng = engine_for(s)
    if eng == "kokoro":
        threading.Thread(target=_speak_kokoro, args=(clean, gen, s), daemon=True,
                         name="speak").start()
        return
    # the engine that CAN speak (asked for Kokoro while it's down -> Piper),
    # or _synth would try only the one asked for and say nothing
    s = dict(s, voice_engine=eng or "auto")

    def worker():
        chunks = tts_chunks(clean)
        if not chunks:
            return
        import queue as _q
        # Two chunks of lookahead, not one. With maxsize=1 the producer is
        # blocked from starting chunk N+2 until chunk N has finished PLAYING,
        # so every synthesis after the first is serialised behind real-time
        # audio — and any chunk slower to render than the previous one is
        # audible as a gap. One more slot costs a few hundred KB of wav and
        # keeps the pipeline ahead of the speaker.
        pipe = _q.Queue(maxsize=2)

        def producer():
            for ch in chunks:
                if gen != _TTS_GEN[0]:
                    break
                wav = _synth(ch, gen, s)
                if gen != _TTS_GEN[0]:
                    if wav:
                        try:
                            os.unlink(wav)
                        except Exception:
                            pass
                    break
                # Bounded, cancellable hand-off. A plain blocking put would hang
                # this thread forever if speech is cancelled while the queue is
                # full (maxsize=1) — the consumer exits and never drains it, so
                # the put never returns and the thread leaks. Retry on a short
                # timeout, re-checking the generation each time, and bail if a
                # newer utterance has superseded this one.  (None = failed chunk,
                # still enqueued so the consumer skips it and keeps going.)
                while gen == _TTS_GEN[0]:
                    try:
                        pipe.put(wav, timeout=0.2)
                        break
                    except _q.Full:
                        continue
                else:
                    if wav:
                        try:
                            os.unlink(wav)
                        except Exception:
                            pass
                    break
            try:
                pipe.put(_TTS_END, timeout=5)
            except Exception:
                pass

        threading.Thread(target=producer, daemon=True).start()
        while gen == _TTS_GEN[0]:
            try:
                wav = pipe.get(timeout=120)
            except Exception:
                break
            if wav is _TTS_END:          # producer finished
                break
            if wav is None:              # this chunk failed to synthesise —
                continue                 # skip it, keep speaking the rest
            try:
                _play(wav, gen=gen)
            finally:
                try:
                    os.unlink(wav)
                except Exception:
                    pass
        # tidy any strays from this generation
        try:
            for f in Path(CONFIG_DIR).glob(f".say-{gen}-*.wav"):
                f.unlink()
        except Exception:
            pass

    threading.Thread(target=worker, daemon=True).start()


def _speak_kokoro(clean, gen, s):
    """Synthesise one chunk ahead and stream every chunk into ONE player:
    the first chunk is a single short sentence so speech starts quickly,
    later ones are longer so the intonation carries across sentences."""
    import queue as _q
    chunks = tts_chunks(clean, 420, first=60)
    voice = s.get("kokoro_voice") or DEFAULT_KOKORO_VOICE
    try:
        speed = max(0.5, min(2.0, float(s.get("voice_speed") or 1.0)))
    except (TypeError, ValueError):
        speed = 1.0
    pipe = _q.Queue(maxsize=2)
    gap = b"\0\0" * int(KOKORO_RATE * 0.16)       # a breath between chunks

    def producer():
        for n, ch in enumerate(chunks):
            if gen != _TTS_GEN[0]:
                break
            out = str(CONFIG_DIR / f".say-{gen}-{n}.pcm")
            epoch = KOKORO.epoch
            try:
                KOKORO.synth(ch, voice, speed, out)
                with open(out, "rb") as fh:
                    pcm = fh.read()
                LAST_ERROR["kokoro"] = ""
            except Exception as ex:
                LAST_ERROR["kokoro"] = str(ex)
                pcm = None
            finally:
                try:
                    os.unlink(out)
                except OSError:
                    pass
            if pcm is None and (epoch != KOKORO.epoch or gen != _TTS_GEN[0]):
                break                   # stopped on purpose (closing, reinstalling): not broken
            if pcm is None:
                # broken: stop here rather than restart it for every chunk, and
                # let the other voices speak for a few minutes
                _KOKORO_DOWN[0] = time.time() + 300
                KOKORO.stop()
                try:
                    pipe.put(None, timeout=5)
                except Exception:
                    pass
                break
            while gen == _TTS_GEN[0]:
                try:
                    pipe.put(pcm, timeout=0.2)
                    break
                except _q.Full:
                    continue
        try:
            pipe.put(_TTS_END, timeout=5)
        except Exception:
            pass

    threading.Thread(target=producer, daemon=True, name="kokoro-synth").start()
    stream = _Stream(gen)
    spoken = 0
    broke = False
    while gen == _TTS_GEN[0]:
        try:
            pcm = pipe.get(timeout=180)
        except Exception:
            break
        if pcm is _TTS_END:
            break
        if not pcm:
            broke = True
            break
        if not stream.write(pcm + gap):
            break
        spoken += 1
    stream.finish()
    try:                                       # a timed-out synth can still write later
        for f in Path(CONFIG_DIR).glob(f".say-{gen}-*.pcm"):
            f.unlink()
    except OSError:
        pass
    if broke and gen == _TTS_GEN[0]:
        # Kokoro broke: say so once, and let another voice read the rest
        if ON_VOICE_FAIL[0] is not None and not LAST_ERROR.get("kokoro_told"):
            LAST_ERROR["kokoro_told"] = True
            try:
                ON_VOICE_FAIL[0]("Kokoro", LAST_ERROR.get("kokoro", "failed"))
            except Exception:
                pass
        rest = " ".join(chunks[spoken:])
        if rest:
            speak(rest, dict(s, voice_engine="piper" if (piper_bin() and _find_piper_model())
                             else "espeak"))

