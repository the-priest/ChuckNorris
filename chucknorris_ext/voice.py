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
    re-resolves the moment the user points at a different model in Settings."""
    m = (_SETTINGS.get("piper_model") or "").strip()
    if _MODEL_CACHE["key"] == m:
        return _MODEL_CACHE["path"]
    found = None
    if m and Path(m).exists():
        found = m
    else:
        for f in sorted(VOICE_DIR.glob("*.onnx")):
            found = str(f)
            break
    _MODEL_CACHE.update({"key": m, "path": found})
    return found


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
    if engine in ("auto", "piper") and _have("piper") and model:
        try:
            cmd = ["piper", "-m", model, "-f", out]
            ls = s.get("voice_speed")
            if ls:
                # piper: length-scale <1 speaks faster. Map 0.5..2.0 speed -> scale
                try:
                    cmd += ["--length-scale", f"{1.0 / float(ls):.3f}"]
                except Exception:
                    pass
            subprocess.run(cmd, input=chunk, text=True, timeout=tmo,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(out) and os.path.getsize(out) > 64:
                return out
        except Exception:
            pass
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

