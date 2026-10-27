"""kokoro_server.py — Chuck's natural voice, kept warm.

Run by the voice venv's Python (it needs kokoro-onnx, numpy and
onnxruntime, which the system Python doesn't have):

    python kokoro_server.py MODEL.onnx VOICES.bin

Loading the model takes a few seconds, so it is loaded once and the process
stays up. One JSON request per line on stdin:

    {"id": "7", "text": "...", "voice": "am_michael:0.6,am_fenrir:0.4",
     "speed": 1.0, "out": "/path/chunk.pcm"}

and one JSON reply per line on stdout: {"id": "7", "ok": true, "samples": N}
or {"id": "7", "ok": false, "error": "..."}. The audio is written to "out" as
raw 16-bit mono PCM at 24 kHz, ready to stream into a player with no gaps.
"""
import json
import sys

_TYPES = {"tensor(float)": "float32", "tensor(double)": "float64", "tensor(int32)": "int32",
          "tensor(int64)": "int64"}


def fit_inputs(engine, np, want_speed):
    """Feed the model exactly the input types IT declares. kokoro-onnx
    0.4-0.5 send `speed` as int32 to the v1.0 model, which expects float
    (INVALID_ARGUMENT: Unexpected input data type), and truncate 0.97 to 0
    on the way; 0.6 reads the types itself. Wrapping the session's run makes
    every version work with the same model file."""
    sess = next((v for v in vars(engine).values() if hasattr(v, "get_inputs") and hasattr(v, "run")),
                None)
    if sess is None:
        return False
    want = {i.name: _TYPES.get(i.type) for i in sess.get_inputs()}
    run = sess.run

    def fitted(output_names, feed, *a, **k):
        out = {}
        for name, val in feed.items():
            dt = want.get(name)
            if name == "speed":
                sp = float(want_speed[0])
                if dt in ("int32", "int64"):
                    sp = max(1, round(sp))            # a model exported with an integer speed
                val = np.array([sp], dtype=dt or "float32")
            elif dt and isinstance(val, np.ndarray) and val.dtype != np.dtype(dt):
                val = val.astype(dt)
            out[name] = val
        return run(output_names, out, *a, **k)
    sess.run = fitted
    return True


def main():
    model, voices = sys.argv[1], sys.argv[2]
    try:
        import numpy as np
        from kokoro_onnx import Kokoro
        engine = Kokoro(model, voices)
        speed_now = [1.0]
        fit_inputs(engine, np, speed_now)
    except Exception as ex:                      # report, don't just die
        print(json.dumps({"ready": False, "error": f"{ex.__class__.__name__}: {ex}"}), flush=True)
        return 1
    styles = {}

    def style(spec):
        """'am_michael' or a blend 'am_michael:0.6,am_fenrir:0.4'."""
        if spec in styles:
            return styles[spec]
        parts = []
        for item in spec.split(","):
            name, _, w = item.strip().partition(":")
            parts.append((name.strip(), float(w) if w else 1.0))
        total = sum(w for _n, w in parts) or 1.0
        st = sum(engine.get_voice_style(n) * (w / total) for n, w in parts)
        styles[spec] = st
        return st

    try:                                   # the first inference is the slow one: pay it now
        engine.create("Ready.", voice=style("am_michael"))
    except Exception:
        pass
    print(json.dumps({"ready": True, "voices": sorted(engine.get_voices())}), flush=True)
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid = req.get("id", "")
        try:
            speed_now[0] = min(2.0, max(0.5, float(req.get("speed") or 1.0)))
            audio, _sr = engine.create(req["text"], voice=style(req.get("voice") or "am_michael"),
                                       speed=speed_now[0],
                                       lang=req.get("lang") or "en-us")
            pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
            with open(req["out"], "wb") as fh:
                fh.write(pcm.tobytes())
            print(json.dumps({"id": rid, "ok": True, "samples": int(pcm.size)}), flush=True)
        except Exception as ex:
            print(json.dumps({"id": rid, "ok": False, "error": f"{ex.__class__.__name__}: {ex}"}),
                  flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
