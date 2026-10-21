"""A misbehaving language server for test_v17: `flood` writes a burst of
requests to the client before reading on (the deadlock case), `bad-init`
refuses to initialize and then lingers."""
import json
import sys
import time

mode = sys.argv[1]
inp, out = sys.stdin.buffer, sys.stdout.buffer


def read():
    n = None
    while True:
        line = inp.readline()
        if not line:
            sys.exit(0)
        line = line.strip()
        if not line:
            break
        if line.lower().startswith(b"content-length:"):
            n = int(line.split(b":", 1)[1])
    return json.loads(inp.read(n))


def send(msg):
    data = json.dumps(msg).encode()
    out.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
    out.flush()


while True:
    msg = read()
    m = msg.get("method")
    if m == "initialize":
        if mode == "bad-init":
            send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -1, "message": "no"}})
            time.sleep(300)
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"capabilities": {}}})
    elif m == "textDocument/didOpen" and mode == "flood":
        # ~400 KB of questions, written before reading anything else: a
        # client that stops reading while it writes deadlocks right here
        for i in range(400):
            send({"jsonrpc": "2.0", "id": f"s{i}", "method": "workspace/configuration",
                  "params": {"items": [{"section": "x" * 1000}]}})
    elif "id" in msg and m:
        send({"jsonrpc": "2.0", "id": msg["id"], "result": []})
