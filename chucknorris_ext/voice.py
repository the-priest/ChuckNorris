"""voice.py — text to speech: cleaning, chunking, synthesis, playback.

Part of Chuck Norris — split out of the original single file.
"""
import os
import re
import shutil
import itertools
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


ON_PIPER_FAIL = [None]          # the app's "say it once" callback


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


def tts_clean(text):
    """Turn a chat reply into something worth listening to."""
    if not isinstance(text, str):
        return ""
    s = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)     # code blocks
    s = re.sub(r"`[^`]*`", " ", s)                            # inline code
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)               # images
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)            # links -> label
    s = _URL_RE.sub(" link ", s)                              # bare URLs
    s = re.sub(r"^\s*[-*+]\s+", "", s, flags=re.M)            # bullets
    s = re.sub(r"^\s*#{1,6}\s*", "", s, flags=re.M)           # headings
    s = re.sub(r"[*_~>#|]", "", s)                            # leftover marks
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def tts_chunks(text, size=_TTS_CHUNK):
    """Split into speakable chunks on sentence boundaries where possible."""
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?:;])\s+|\n+", text)
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
    return [c for c in chunks if c]


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

