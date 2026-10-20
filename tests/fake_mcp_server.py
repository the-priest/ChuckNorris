#!/usr/bin/env python3
"""A real stdio MCP server for tests: newline-delimited JSON-RPC 2.0.

Deliberately awkward: it prints a non-JSON line to stdout at start (servers
that log to stdout exist), pages its tool list, asks the CLIENT for its roots
mid-call, and has tools that error, return images, or take their time.
"""
import json
import sys
import time

PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
TOOLS = [
    {"name": "echo", "description": "Echo the text back.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "add", "description": "Add two numbers.",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "number"},
                                                      "b": {"type": "number"}},
                     "required": ["a", "b"]}},
    {"name": "pixel", "description": "Return a 1x1 image.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "fail", "description": "Always reports an error.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "slow", "description": "Sleep, then answer.",
     "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}}},
    {"name": "where", "description": "Ask the client for its roots and report them.",
     "inputSchema": {"type": "object", "properties": {}}},
]
pending = {}


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


BACKLOG = []          # requests that arrived while we waited on the client


def ask_client(method):
    """A server -> client request; waits for the answer on stdin, keeping any
    other request that arrives meanwhile for the main loop (a real server
    multiplexes; dropping them would leave the client waiting forever)."""
    mid = f"srv-{time.time()}"
    send({"jsonrpc": "2.0", "id": mid, "method": method, "params": {}})
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        msg = json.loads(line)
        if msg.get("id") == mid and "method" not in msg:
            return msg.get("result") or msg.get("error")
        BACKLOG.append(line)


def lines():
    while True:
        if BACKLOG:
            yield BACKLOG.pop(0)
            continue
        line = sys.stdin.readline()
        if not line:
            return
        yield line


def main():
    print("fake-mcp starting up (this line is not JSON)", flush=True)
    sys.stderr.write("fake-mcp: stderr is for logs\n"); sys.stderr.flush()
    for line in lines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid, m, p = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            continue                                     # a notification
        if m == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": p.get("protocolVersion"), "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1.2.3"}}})
        elif m == "tools/list":
            if not p.get("cursor"):                      # two pages
                send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS[:3],
                                                              "nextCursor": "page2"}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS[3:]}})
        elif m == "tools/call":
            name, a = p.get("name"), p.get("arguments") or {}
            if name == "echo":
                res = {"content": [{"type": "text", "text": "ECHO: " + a.get("text", "")}]}
            elif name == "add":
                res = {"content": [{"type": "text", "text": str(a["a"] + a["b"])}]}
            elif name == "pixel":
                res = {"content": [{"type": "text", "text": "one pixel"},
                                   {"type": "image", "data": PNG, "mimeType": "image/png"}]}
            elif name == "fail":
                res = {"content": [{"type": "text", "text": "it broke on purpose"}], "isError": True}
            elif name == "slow":
                time.sleep(float(a.get("seconds", 1)))
                res = {"content": [{"type": "text", "text": "finally"}]}
            elif name == "where":
                roots = ask_client("roots/list")
                res = {"content": [{"type": "text", "text": json.dumps(roots)}]}
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602,
                                                             "message": f"unknown tool {name}"}})
                continue
            send({"jsonrpc": "2.0", "id": mid, "result": res})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}})


if __name__ == "__main__":
    main()
