"""listen.py — talk to Chuck: local speech to text, nothing leaves the machine.

Moonshine (base, English) through sherpa-onnx, with the Silero voice-activity
detector deciding when you started and stopped talking. Both run in the same
private venv as the voice (shared numpy/onnxruntime), inside stt_server.py,
which stays loaded so a question is transcribed the moment you pause.

The microphone is recorded with PipeWire's pw-record (else parecord or
arecord) as raw 16 kHz mono, which is what the models want.
"""
import json
import os
import queue
import shutil
import subprocess
import tarfile
import threading
import time
from pathlib import Path

from . import config
from . import voice as _voice

STT_DIR = config.VOICE_DIR / "stt"
MODEL_DIR = STT_DIR / "moonshine-base-en"
VAD_PATH = STT_DIR / "silero_vad.onnx"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
             "sherpa-onnx-moonshine-base-en-int8.tar.bz2")
VAD_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"
MODEL_FILES = ("preprocess.onnx", "encode.int8.onnx", "uncached_decode.int8.onnx",
               "cached_decode.int8.onnx", "tokens.txt")
_SERVER = Path(__file__).with_name("stt_server.py")
_SETTINGS = config.SETTINGS_DATA
RECORDERS = (
    ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", "-"],
    ["parecord", "--raw", "--rate=16000", "--channels=1", "--format=s16le"],
    ["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-r", "16000", "-c", "1"],
)


def ready():
    return bool(_voice.voice_python() and all((MODEL_DIR / f).exists() for f in MODEL_FILES)
                and VAD_PATH.exists())


def recorder_argv():
    want = (_SETTINGS.get("mic_command") or "").strip()
    if want:
        return want.split() if not want.startswith("sh -c ") else ["sh", "-c", want[6:]]
    for argv in RECORDERS:
        if shutil.which(argv[0]):
            return argv
    return None


def status():
    """(state, detail) for /doctor and Settings."""
    if not ready():
        return "warn", "not installed — Settings › Voice › Install voice input"
    if recorder_argv() is None:
        return "fail", "no recorder: install pipewire (pw-record), pulseaudio-utils or alsa-utils"
    return "ok", f"Moonshine, local · mic via {recorder_argv()[0]}"


class Ears:
    """The warm speech-to-text process. listen() calls on_event(dict) from a
    background thread for every event (see stt_server.py)."""

    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.events = None
        self.on_event = None
        self.busy = False
        self.cancel_pending = False

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, timeout=90):
        with self.lock:
            if self.alive():
                return
            py = _voice.voice_python()
            if not py:
                raise RuntimeError("voice input isn't installed")
            self.proc = subprocess.Popen([py, "-u", str(_SERVER), str(MODEL_DIR), str(VAD_PATH)],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True,
                                         start_new_session=True)
            first = queue.Queue()
            out = self.proc.stdout
            me = self.proc

            def read():
                got_ready = False
                for ln in out:
                    try:
                        ev = json.loads(ln)
                    except ValueError:
                        continue
                    if not got_ready:
                        got_ready = True
                        first.put(ev)
                        continue
                    if ev.get("event") in ("final", "nothing", "error", "cancelled"):
                        self.busy = False
                    cb = self.on_event
                    if cb is not None:
                        try:
                            cb(ev)
                        except Exception:
                            pass
                if self.proc is not me:
                    return                          # replaced (reinstall, stop): not an error
                was_busy, self.busy = self.busy, False
                if not got_ready:
                    first.put({"event": "error", "error": "the speech model didn't load"})
                elif was_busy and self.on_event is not None:
                    try:
                        self.on_event({"event": "error", "error": "the listener stopped"})
                    except Exception:
                        pass
            threading.Thread(target=read, daemon=True, name="stt-reader").start()
            try:
                ev = first.get(timeout=timeout)
            except queue.Empty:
                self._kill()
                raise RuntimeError(f"the speech model didn't load within {timeout}s")
            if ev.get("event") != "ready":
                self._kill()
                raise RuntimeError(ev.get("error") or "the speech model didn't load")

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def listen(self, on_event, max_wait=8, max_len=30):
        """Start one utterance. Returns False if it can't start."""
        argv = recorder_argv()
        if argv is None:
            on_event({"event": "error", "error": status()[1]})
            return False
        self.cancel_pending = False
        self.on_event = on_event
        self.busy = True                        # from here a cancel counts, even mid-load
        try:
            self.start()
        except Exception:
            self.busy = False
            raise
        if self.cancel_pending:                 # cancelled while the model was loading
            self.busy = False
            self.cancel_pending = False
            on_event({"event": "cancelled"})
            return False
        self._send({"cmd": "listen", "recorder": argv, "max_wait": max_wait, "max_len": max_len})
        return True

    def cancel(self):
        if self.busy and not self.alive():
            self.cancel_pending = True
            return
        if self.alive() and self.busy:
            try:
                self._send({"cmd": "cancel"})
            except Exception:
                pass

    def _kill(self):
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=3)
            except Exception:
                pass
        self.proc = None

    def stop(self):
        self._kill()


EARS = Ears()


def warm_up():
    if ready():
        threading.Thread(target=lambda: _quiet(EARS.start), daemon=True, name="stt-warm").start()


def _quiet(fn):
    try:
        fn()
    except Exception:
        pass


def install(progress=lambda s: None, opener=None):
    """Install voice input: sherpa-onnx in the voice venv, the Moonshine model
    (about 250 MB download) and the voice-activity detector. -> (ok, msg)."""
    with _voice.INSTALL_LOCK:
        return _install(progress, opener)


def _install(progress, opener):
    arc = STT_DIR / "moonshine.tar.bz2"
    try:
        ok, msg = _voice.ensure_venv(["sherpa-onnx"], "sherpa_onnx", progress)
        if not ok:
            return False, msg
        STT_DIR.mkdir(parents=True, exist_ok=True)
        if not VAD_PATH.exists():
            _voice._download(VAD_URL, VAD_PATH, progress, opener, "the speech detector")
        if not all((MODEL_DIR / f).exists() for f in MODEL_FILES):
            if not arc.exists():
                _voice._download(MODEL_URL, arc, progress, opener, "the speech model")
            progress("unpacking the speech model")
            tmp = STT_DIR / "unpack"
            shutil.rmtree(tmp, ignore_errors=True)
            with tarfile.open(arc) as tf:
                members = [m for m in tf.getmembers()
                           if m.isfile() and Path(m.name).name in MODEL_FILES]
                try:
                    tf.extractall(tmp, members=members, filter="data")
                except TypeError:
                    tf.extractall(tmp, members=members)
            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            for f in tmp.rglob("*"):
                if f.is_file() and f.name in MODEL_FILES:
                    os.replace(f, MODEL_DIR / f.name)
            shutil.rmtree(tmp, ignore_errors=True)
            missing = [f for f in MODEL_FILES if not (MODEL_DIR / f).exists()]
            if missing:
                arc.unlink()
                return False, "the speech model download was incomplete: " + ", ".join(missing)
            arc.unlink()                                  # only the model files are kept
    except Exception as ex:
        try:
            arc.unlink()                        # a bad download must not be reused forever
        except OSError:
            pass
        return False, f"{ex.__class__.__name__}: {ex}"
    EARS.stop()
    progress("loading the speech model")
    try:
        EARS.start()
    except Exception as ex:
        return False, f"installed, but it didn't load: {ex}"
    return True, "voice input ready (Moonshine, local)"


def wait_idle(timeout=5):
    end = time.time() + timeout
    while EARS.busy and time.time() < end:
        time.sleep(0.05)
