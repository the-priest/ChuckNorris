#!/usr/bin/env python3
"""A small MCP server for the tests: speaks JSON-RPC on stdio like a real one,
and misbehaves on cue."""
import json, sys, time

out = sys.stdout
print("starting up (a log line on stdout that isn't JSON)", flush=True)
print("fake server ready", file=sys.stderr, flush=True)
pending = {}

def send(msg):
    out.write(json.dumps(msg) + "\n"); out.flush()

def ask_client(method, mid):
    send({"jsonrpc": "2.0", "id": mid, "method": method})
    for line in sys.stdin:
        m = json.loads(line)
        if m.get("id") == mid and "method" not in m:
            return m

TOOLS1 = [
    {"name": "echo", "description": "Echo the text back", "inputSchema": {"properties": {"text": {}}, "required": ["text"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "write_note", "description": "Write a note file", "inputSchema": {"properties": {"path": {}, "text": {}},
     "required": ["path", "text"]}},
]
TOOLS2 = [
    {"name": "roots", "annotations": {"readOnlyHint": True}},
    {"name": "image", "annotations": {"readOnlyHint": True}},
    {"name": "fails", "annotations": {"readOnlyHint": True}},
    {"name": "slow", "annotations": {"readOnlyHint": True}},
    {"name": "crash"},
    {"name": "bad name!"},
]

for line in sys.stdin:
    try:
        m = json.loads(line)
    except ValueError:
        continue
    mid, method, p = m.get("id"), m.get("method"), m.get("params") or {}
    if mid is None:
        continue
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": p.get("protocolVersion"),
              "serverInfo": {"name": "fake", "version": "1.2"}, "capabilities": {"tools": {}}}})
    elif method == "tools/list":
        if p.get("cursor") == "page2":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS2}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS1, "nextCursor": "page2"}})
    elif method == "tools/call":
        name, a = p.get("name"), p.get("arguments") or {}
        if name == "echo":
            # the server pings the client mid-call, as servers may
            ask_client("ping", "srv-1")
            r = {"content": [{"type": "text", "text": "echo: " + a["text"]}]}
        elif name == "write_note":
            open(a["path"], "w").write(a["text"])
            r = {"content": [{"type": "text", "text": "wrote " + a["path"]}]}
        elif name == "roots":
            got = ask_client("roots/list", "srv-2")
            other = ask_client("sampling/createMessage", "srv-3")
            r = {"content": [{"type": "text", "text": json.dumps([x["uri"] for x in got["result"]["roots"]])
                              + " / " + str(other.get("error", {}).get("code"))}]}
        elif name == "image":
            r = {"content": [{"type": "image", "mimeType": "image/png", "data": "iVBORw0KGgo="},
                             {"type": "resource", "resource": {"uri": "file:///x", "text": "resource text"}}]}
        elif name == "fails":
            r = {"content": [{"type": "text", "text": "no such repo"}], "isError": True}
        elif name == "slow":
            time.sleep(1.5)
            r = {"content": [{"type": "text", "text": "late"}]}
        elif name == "crash":
            print("dying now", file=sys.stderr, flush=True)
            sys.exit(3)
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}})
            continue
        send({"jsonrpc": "2.0", "id": mid, "result": r})
    else:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}})
