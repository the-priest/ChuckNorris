"""stt_server.py — Chuck's ears, kept warm.

Run by the voice venv's Python (it needs sherpa-onnx and numpy):

    python stt_server.py MODEL_DIR VAD.onnx

The speech model is loaded once. Then, per command line on stdin:

    {"cmd": "listen", "recorder": [argv...], "max_wait": 8, "max_len": 30}
    {"cmd": "cancel"}

"listen" starts the recorder (raw 16 kHz mono s16 on its stdout), runs the
Silero voice-activity detector over it, and as soon as you stop talking
transcribes what you said. Events go to stdout, one JSON object per line:

    {"event": "ready"}                       model loaded
    {"event": "listening"}                   the recorder is running
    {"event": "level", "rms": 0.08}          about ten a second, for a meter
    {"event": "speech"}                      you started talking
    {"event": "final", "text": "..."}        what you said
    {"event": "nothing"}                     no speech before max_wait
    {"event": "error", "error": "..."}
"""
import json
import os
import select
import subprocess
import sys
import threading
import time

RATE = 16000


def emit(**kw):
    sys.stdout.write(json.dumps(kw) + "\n")
    sys.stdout.flush()


def main():
    model_dir, vad_path = sys.argv[1], sys.argv[2]
    try:
        import numpy as np
        import sherpa_onnx as so
        j = lambda n: os.path.join(model_dir, n)          # noqa: E731
        if os.path.exists(j("encode.int8.onnx")):
            rec = so.OfflineRecognizer.from_moonshine(
                preprocessor=j("preprocess.onnx"), encoder=j("encode.int8.onnx"),
                uncached_decoder=j("uncached_decode.int8.onnx"),
                cached_decoder=j("cached_decode.int8.onnx"), tokens=j("tokens.txt"),
                num_threads=max(1, min(4, os.cpu_count() or 2)))
        else:
            raise RuntimeError(f"no speech model in {model_dir}")
        cfg = so.VadModelConfig()
        cfg.silero_vad.model = vad_path
        cfg.silero_vad.threshold = 0.5
        cfg.silero_vad.min_silence_duration = 0.8      # a pause this long ends the sentence
        cfg.silero_vad.min_speech_duration = 0.25
        cfg.silero_vad.max_speech_duration = 30
        cfg.sample_rate = RATE
        window = cfg.silero_vad.window_size
    except Exception as ex:
        emit(event="error", error=f"{ex.__class__.__name__}: {ex}")
        return 1

    def transcribe(samples):
        st = rec.create_stream()
        st.accept_waveform(RATE, samples)
        rec.decode_stream(st)
        return st.result.text.strip()

    # warm the recogniser so the first answer is quick
    try:
        transcribe(np.zeros(RATE // 2, dtype=np.float32))
    except Exception:
        pass
    emit(event="ready")

    cmds = []
    cmd_ready = threading.Event()

    def read_cmds():
        for line in sys.stdin:
            try:
                cmds.append(json.loads(line))
                cmd_ready.set()
            except ValueError:
                pass
        cmds.append({"cmd": "quit"})
        cmd_ready.set()
    threading.Thread(target=read_cmds, daemon=True).start()

    while True:
        cmd_ready.wait()
        cmd_ready.clear()
        while cmds:
            c = cmds.pop(0)
            if c.get("cmd") == "quit":
                return 0
            if c.get("cmd") != "listen":
                continue
            listen(c, so, np, cfg, window, transcribe, cmds)


def listen(c, so, np, cfg, window, transcribe, cmds):
    try:
        proc = subprocess.Popen(c["recorder"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL)
    except OSError as ex:
        emit(event="error", error=f"couldn't start the microphone: {ex}")
        return
    vad = so.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)
    emit(event="listening")
    t0 = time.time()
    max_wait, max_len = float(c.get("max_wait", 8)), float(c.get("max_len", 30))
    heard = False
    speech_t = None
    buf = np.zeros(0, dtype=np.float32)
    last_level = 0.0
    fd = proc.stdout.fileno()
    result = None
    try:
        while True:
            if any(x.get("cmd") == "cancel" for x in cmds):
                cmds[:] = [x for x in cmds if x.get("cmd") != "cancel"]
                result = "cancel"
                break
            r, _w, _x = select.select([fd], [], [], 0.1)
            if not r:
                if proc.poll() is not None:
                    err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
                    emit(event="error", error="the microphone stopped" + (f": {err[-200:]}" if err else ""))
                    result = "error"
                    break
                if not heard and time.time() - t0 > max_wait:
                    result = "nothing"
                    break
                continue
            data = os.read(fd, 3200)                     # 0.1 s
            if not data:
                # the recorder ended (a file being replayed, or a closed device):
                # whatever was said up to now is the utterance
                vad.flush()
                if not vad.empty():
                    seg = vad.front.samples
                    emit(event="final", text=transcribe(np.array(seg, dtype=np.float32)))
                    result = "final"
                elif heard:
                    emit(event="final", text=transcribe(buf))
                    result = "final"
                else:
                    result = "nothing"
                break
            pcm = np.frombuffer(data[:len(data) // 2 * 2], dtype="<i2").astype(np.float32) / 32768
            if time.time() - last_level > 0.1:
                last_level = time.time()
                emit(event="level", rms=round(float(np.sqrt(np.mean(pcm * pcm))) if pcm.size else 0.0, 4))
            buf = np.concatenate([buf, pcm])
            while buf.size >= window:
                vad.accept_waveform(buf[:window])
                buf = buf[window:]
            if vad.is_speech_detected() and not heard:
                heard, speech_t = True, time.time()
                emit(event="speech")
            if not vad.empty():                           # a whole utterance, ended by a pause
                seg = vad.front.samples
                text = transcribe(np.array(seg, dtype=np.float32))
                emit(event="final", text=text)
                result = "final"
                break
            if not heard and time.time() - t0 > max_wait:
                result = "nothing"
                break
            if heard and time.time() - speech_t > max_len:
                vad.flush()
                if not vad.empty():
                    emit(event="final", text=transcribe(np.array(vad.front.samples, dtype=np.float32)))
                    result = "final"
                    break
    finally:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
    if result == "nothing":
        emit(event="nothing")
    elif result == "cancel":
        emit(event="cancelled")


if __name__ == "__main__":
    sys.exit(main())
