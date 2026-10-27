#!/usr/bin/env python3
"""Stands in for the voice environment's Python: run as kokoro_server.py it
speaks (writes a PCM chunk whose every sample is the request number), run as
stt_server.py it hears "hello chuck"."""
import json, os, sys, time

args = [a for a in sys.argv[1:] if a != "-u"]
script = os.path.basename(args[0])
if script == "kokoro_server.py":
    with open(os.path.join(os.environ["HOME"], "kokoro.starts"), "a") as f:
        f.write("start\n")
    print(json.dumps({"ready": True, "voices": ["am_michael"]}), flush=True)
    for line in sys.stdin:
        req = json.loads(line)
        n = int(req["id"])
        if os.environ.get("FAKE_SLOW"):
            time.sleep(0.4)
        if req["text"].startswith("BREAK"):
            print(json.dumps({"id": req["id"], "ok": False, "error": "model exploded"}), flush=True)
            continue
        with open(req["out"], "wb") as f:
            f.write(bytes([n % 256, 0]) * 2400)          # 0.1 s of sample value n
        with open(os.path.join(os.environ["HOME"], "kokoro.log"), "a") as f:
            f.write(req["text"] + "\n")
        print(json.dumps({"id": req["id"], "ok": True, "samples": 2400}), flush=True)
elif script == "stt_server.py":
    print(json.dumps({"event": "ready"}), flush=True)
    for line in sys.stdin:
        cmd = json.loads(line)
        if cmd["cmd"] == "listen":
            assert cmd["recorders"] and cmd["max_wait"] == 8, cmd
            for ev in ({"event": "listening"}, {"event": "speech"}, {"event": "level", "rms": 0.25},
                       {"event": "final", "text": " hello chuck "}):
                print(json.dumps(ev), flush=True)
