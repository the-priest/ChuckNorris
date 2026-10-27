#!/usr/bin/env python3
"""A small language server for the tests: Content-Length framed JSON-RPC,
answering workspace/symbol, references and rename for Python files by plain
whole-word search over what the client opened — enough to check the client
does the protocol right (positions, versions, edits, refusals)."""
import json, os, re, sys

docs = {}          # uri -> text, as the client sent it
root = None
inp, out = sys.stdin.buffer, sys.stdout.buffer

def read():
    n = None
    while True:
        line = inp.readline()
        if not line:
            sys.exit(0)
        line = line.strip()
        if not line:
            if n is not None:
                break
            continue
        if line.lower().startswith(b"content-length:"):
            n = int(line.split(b":")[1])
    return json.loads(inp.read(n))

def send(msg):
    b = json.dumps(msg).encode()
    out.write(b"Content-Length: %d\r\n\r\n" % len(b) + b); out.flush()

def utf16(s):
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)

def hits(word):
    rx = re.compile(r"(?<![\w$])" + re.escape(word) + r"(?![\w$])")
    for uri, text in sorted(docs.items()):
        for i, ln in enumerate(text.split("\n")):
            for m in rx.finditer(ln):
                c = utf16(ln[:m.start()])
                yield uri, i, c, c + utf16(word), ln

def word_at(uri, line, ch):
    ln = docs[uri].split("\n")[line]
    for m in re.finditer(r"[\w$]+", ln):
        if utf16(ln[:m.start()]) <= ch <= utf16(ln[:m.end()]):
            return m.group()

def rng(i, a, b):
    return {"start": {"line": i, "character": a}, "end": {"line": i, "character": b}}

while True:
    m = read()
    mid, meth, p = m.get("id"), m.get("method"), m.get("params") or {}
    if meth == "initialize":
        root = p["rootUri"]
        # ask the client something first, as real servers do
        send({"jsonrpc": "2.0", "id": "cfg", "method": "workspace/configuration", "params": {"items": [{}, {}]}})
        r = read()
        assert r.get("id") == "cfg" and r.get("result") == [None, None], r
        send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {}}})
    elif meth in ("textDocument/didOpen",):
        docs[p["textDocument"]["uri"]] = p["textDocument"]["text"]
    elif meth == "textDocument/didChange":
        docs[p["textDocument"]["uri"]] = p["contentChanges"][-1]["text"]
    elif meth == "workspace/symbol":
        q = p["query"]
        res = []
        for uri, i, a, b, ln in hits(q):
            dm = re.match(r"\s*(def|class)\s+" + re.escape(q) + r"\b", ln)
            if dm:
                cls = None
                lines = docs[uri].split("\n")
                if ln.startswith((" ", "\t")):
                    for prev in reversed(lines[:i]):
                        cm = re.match(r"class\s+(\w+)", prev)
                        if cm:
                            cls = cm.group(1); break
                res.append({"name": q, "kind": 5 if dm.group(1) == "class" else (6 if cls else 12),
                            "containerName": cls or "", "location": {"uri": uri, "range": rng(i, 0, len(ln))}})
        # a library's symbol, outside the project: the client must drop it
        res.append({"name": q, "kind": 12, "location": {"uri": "file:///usr/lib/python3/site.py", "range": rng(0, 0, 1)}})
        send({"jsonrpc": "2.0", "id": mid, "result": res})
    elif meth == "textDocument/references":
        w = word_at(p["textDocument"]["uri"], p["position"]["line"], p["position"]["character"])
        send({"jsonrpc": "2.0", "id": mid, "result": [{"uri": u, "range": rng(i, a, b)} for u, i, a, b, _ in hits(w)]})
    elif meth == "textDocument/rename":
        w = word_at(p["textDocument"]["uri"], p["position"]["line"], p["position"]["character"])
        changes = {}
        for u, i, a, b, _ in hits(w):
            changes.setdefault(u, []).append({"range": rng(i, a, b), "newText": p["newName"]})
        if os.environ.get("FAKE_LSP_STALE"):
            # an edit that doesn't cover the name: what a server with an old copy would send
            u = next(iter(changes))
            changes[u][0]["range"]["end"]["character"] += 1
        send({"jsonrpc": "2.0", "id": mid, "result": {"changes": changes}})
    elif meth == "shutdown":
        send({"jsonrpc": "2.0", "id": mid, "result": None})
    elif meth == "exit":
        sys.exit(0)
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}})
