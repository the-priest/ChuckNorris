"""lsp.py — "where is this defined, who uses it, rename it everywhere".

A small Language Server Protocol client (JSON-RPC over stdio, stdlib only)
for the servers people already have: pyright / basedpyright / pylsp /
jedi-language-server for Python, typescript-language-server for JS/TS,
rust-analyzer, gopls and clangd. A server answers by meaning, so `refs save`
finds calls to THIS save and not every word "save" in the repo.

Without a server the answers come from the AST (Python) or the same
definition patterns the repo map uses, and references fall back to a
whole-word text search that says it is one. rename needs a real server:
a text-level rename is how unrelated code gets broken.

Servers start on first use, one per (project, language), and stay up for the
session; the app stops them on close.
"""
import ast
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

from . import builder as _builder

# language -> (file extensions, LSP languageId by extension, server argv candidates)
LANGS = {
    "python": ((".py", ".pyi"), {".py": "python", ".pyi": "python"},
               [["pyright-langserver", "--stdio"], ["basedpyright-langserver", "--stdio"],
                ["pylsp"], ["jedi-language-server"]]),
    "typescript": ((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
                   {".ts": "typescript", ".tsx": "typescriptreact", ".js": "javascript",
                    ".jsx": "javascriptreact", ".mjs": "javascript", ".cjs": "javascript"},
                   [["typescript-language-server", "--stdio"], ["tsc", "--lsp", "--stdio"]]),
    "rust": ((".rs",), {".rs": "rust"}, [["rust-analyzer"]]),
    "go": ((".go",), {".go": "go"}, [["gopls"]]),
    "c": ((".c", ".h", ".cc", ".cpp", ".hpp", ".cxx"),
          {".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".cxx": "cpp"},
          [["clangd"]]),
}
# what to install, for the message when there is none
INSTALL = {"python": "pyright (pip install --user pyright, or npm i -g pyright)",
           "typescript": "TypeScript 7 (npm i -g typescript: its tsc is a language server) or "
                         "typescript-language-server with TypeScript 6",
           "rust": "rust-analyzer (rustup component add rust-analyzer)",
           "go": "gopls (go install golang.org/x/tools/gopls@latest)",
           "c": "clangd"}
_KIND = {1: "file", 2: "module", 3: "namespace", 4: "package", 5: "class", 6: "method",
         7: "property", 8: "field", 9: "constructor", 10: "enum", 11: "interface",
         12: "function", 13: "variable", 14: "constant", 15: "string", 16: "number",
         17: "boolean", 18: "array", 19: "object", 20: "key", 21: "null", 22: "enum member",
         23: "struct", 24: "event", 25: "operator", 26: "type parameter"}
_WORKS = {}                     # argv[0] -> bool: does this binary actually run?


class LSPError(Exception):
    pass


def lang_of(path):
    ext = Path(path).suffix.lower()
    for lang, (exts, _ids, _argv) in LANGS.items():
        if ext in exts:
            return lang
    return None


def _runs(argv0):
    """A rustup proxy for an uninstalled rust-analyzer exists on PATH and fails
    at once. Starting it would cost a timeout per question, so check once."""
    if argv0 in _WORKS:
        return _WORKS[argv0]
    path = shutil.which(argv0)
    ok = False
    if path:
        try:
            p = subprocess.run([path, "--version"], capture_output=True, timeout=10,
                               stdin=subprocess.DEVNULL)
            # some servers (pyright-langserver) reject --version but still run
            err = (p.stderr or b"").decode("utf-8", "replace").lower()
            ok = p.returncode == 0 or ("unknown binary" not in err and "not installed" not in err
                                       and "is not installed" not in err)
        except (OSError, subprocess.TimeoutExpired):
            ok = False
    _WORKS[argv0] = ok
    return ok


def _tsc_major():
    try:
        out = subprocess.run([shutil.which("tsc") or "tsc", "--version"], capture_output=True,
                             text=True, timeout=10, stdin=subprocess.DEVNULL).stdout
        return int(re.search(r"(\d+)\.", out).group(1))
    except (OSError, subprocess.TimeoutExpired, AttributeError, ValueError):
        return 0


def server_for(lang, root=None):
    """The argv of the first installed server for lang that can work here, or None."""
    for argv in LANGS.get(lang, ((), {}, []))[2]:
        if not _runs(argv[0]):
            continue
        if argv[0] == "typescript-language-server" and not (
                _init_options(lang) or (root and (Path(root) / "node_modules" / "typescript"
                                                  / "lib" / "tsserver.js").is_file())):
            continue                     # it needs a tsserver.js (TypeScript 6 or older)
        if argv[0] == "tsc" and _tsc_major() < 7:
            continue                     # TypeScript 7's native compiler is its own server
        return [shutil.which(argv[0])] + argv[1:]
    return None


def _init_options(lang):
    """typescript-language-server needs a TypeScript; a project without one in
    node_modules gets the globally installed one."""
    if lang != "typescript":
        return None
    for cand in (shutil.which("tsserver"), shutil.which("tsc")):
        if cand:
            lib = Path(os.path.realpath(cand)).parent.parent / "lib" / "tsserver.js"
            if lib.is_file():
                return {"tsserver": {"path": str(lib)}}
    return None


def uri(path):
    return "file://" + urllib.parse.quote(str(Path(path).resolve()))


def path_of(u):
    return urllib.parse.unquote(urllib.parse.urlparse(u).path)


# Set by the app when the folder is sandboxed or read-only: argv -> argv
# (bubblewrap around it), or None when it can't be run safely.
RUN_WRAP = [None]


def _wrapped(argv):
    w = RUN_WRAP[0]
    return list(argv) if w is None else w(list(argv))


class Client:
    """One language server process."""

    def __init__(self, argv, root, lang):
        self.root, self.lang = str(Path(root).resolve()), lang
        self.name = os.path.basename(argv[0])
        wrapped = _wrapped(argv)
        if wrapped is None:
            raise LSPError("this folder's access needs bubblewrap to run a language server")
        self.proc = subprocess.Popen(wrapped, cwd=self.root, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
        self.lock = threading.Lock()
        self.seq = 0
        self.waiting = {}                # id -> [Event, result]
        self.opened = {}                 # path -> version
        self.dead = False
        threading.Thread(target=self._read, daemon=True, name=f"lsp-{self.name}").start()
        caps = {"textDocument": {
                    "definition": {"linkSupport": False}, "references": {},
                    "rename": {"prepareSupport": False}, "documentSymbol": {
                        "hierarchicalDocumentSymbolSupport": True},
                    "synchronization": {"didSave": False}},
                "workspace": {"symbol": {}, "workspaceEdit": {"documentChanges": True},
                              "workspaceFolders": True, "configuration": True}}
        self.request("initialize", {
            "processId": os.getpid(), "rootUri": uri(self.root), "rootPath": self.root,
            "capabilities": caps, "clientInfo": {"name": "chuck"},
            "initializationOptions": _init_options(lang),
            "workspaceFolders": [{"uri": uri(self.root), "name": Path(self.root).name}]},
            timeout=60)
        self.notify("initialized", {})

    # ── wire ──
    def _send(self, msg):
        data = json.dumps(msg).encode()
        with self.lock:
            try:
                self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
                self.proc.stdin.flush()
            except (OSError, ValueError):
                self.dead = True
                raise LSPError(f"{self.name} stopped")

    def _read(self):
        f = self.proc.stdout
        try:
            while True:
                length = None
                while True:
                    line = f.readline()
                    if not line:
                        raise EOFError
                    line = line.strip()
                    if not line:
                        break
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1])
                if length is None:
                    continue
                msg = json.loads(f.read(length))
                if "id" in msg and "method" in msg:          # the server asks us something
                    self._answer(msg)
                elif "id" in msg:
                    w = self.waiting.pop(msg["id"], None)
                    if w:
                        w[1] = msg
                        w[0].set()
        except (EOFError, OSError, ValueError):
            pass
        self.dead = True
        for w in list(self.waiting.values()):
            w[0].set()

    def _answer(self, msg):
        m, params = msg["method"], msg.get("params") or {}
        if m == "workspace/configuration":
            result = [None] * len(params.get("items") or [])
        elif m == "workspace/workspaceFolders":
            result = [{"uri": uri(self.root), "name": Path(self.root).name}]
        else:                       # registerCapability, workDoneProgress/create, ...
            result = None
        try:
            self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        except LSPError:
            pass

    def request(self, method, params, timeout=30):
        if self.dead:
            raise LSPError(f"{self.name} isn't running")
        self.seq += 1
        rid = self.seq
        w = [threading.Event(), None]
        self.waiting[rid] = w
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not w[0].wait(timeout):
            self.waiting.pop(rid, None)
            raise LSPError(f"{self.name} didn't answer {method} within {timeout}s")
        msg = w[1]
        if msg is None:
            raise LSPError(f"{self.name} stopped")
        if "error" in msg:
            raise LSPError(f"{self.name}: {msg['error'].get('message', msg['error'])}")
        return msg.get("result")

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def open(self, path):
        """didOpen (or re-sync after an edit on disk)."""
        path = str(Path(path).resolve())
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        ext = Path(path).suffix.lower()
        lang_id = LANGS[self.lang][1].get(ext, self.lang)
        if path in self.opened:
            self.opened[path] += 1
            self.notify("textDocument/didChange", {
                "textDocument": {"uri": uri(path), "version": self.opened[path]},
                "contentChanges": [{"text": text}]})
        else:
            self.opened[path] = 1
            self.notify("textDocument/didOpen", {"textDocument": {
                "uri": uri(path), "languageId": lang_id, "version": 1, "text": text}})

    def close(self):
        try:
            if not self.dead:
                self.request("shutdown", None, timeout=3)
                self.notify("exit", None)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=3)
        except Exception:
            try:
                os.killpg(self.proc.pid, 9)
            except OSError:
                self.proc.kill()
        self.dead = True


class Manager:
    """Servers per (root, language), started on demand."""

    def __init__(self):
        self.clients = {}
        self.failed = {}                 # (root, lang) -> why its server wouldn't start
        self.lock = threading.Lock()

    def client(self, root, lang):
        """A running server, or None (none installed, or it failed to start:
        then the syntax/text fallbacks answer and failed[] says why)."""
        key = (str(Path(root).resolve()), lang)
        with self.lock:
            c = self.clients.get(key)
            if c is not None and not c.dead:
                return c
            if key in self.failed:
                return None
            argv = server_for(lang, root)
            if argv is None:
                return None
            try:
                c = Client(argv, root, lang)
            except (LSPError, OSError) as ex:
                self.failed[key] = str(ex)
                return None
            self.clients[key] = c
            return c

    def stop_all(self):
        with self.lock:
            cs, self.clients = list(self.clients.values()), {}
        for c in cs:
            c.close()


# ── the questions ────────────────────────────────────────────────────────────
def _files(root, lang=None):
    out = []
    for rel in _builder._project_files(root, limit=5000):
        if lang is None or lang_of(rel) == lang:
            p = Path(root) / rel
            try:
                if p.is_file() and p.stat().st_size <= _builder.MAX_FILE_BYTES:
                    out.append(rel)
            except OSError:
                pass
    return out


def _line(root, rel, n):
    try:
        with open(Path(root) / rel, encoding="utf-8", errors="replace") as fh:
            for i, ln in enumerate(fh, 1):
                if i == n:
                    return ln.rstrip("\n")
    except OSError:
        pass
    return ""


def _rel(root, path):
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return str(path)


def _split(name):
    """'Store.save' -> ('Store', 'save'); 'save' -> (None, 'save')."""
    if "." in name.strip(".") and not name.startswith("."):
        cont, _, leaf = name.rpartition(".")
        return cont.rpartition(".")[2], leaf
    return None, name


def _ast_defs(root, name):
    cont, leaf = _split(name)
    hits = []
    for rel in _files(root, "python"):
        try:
            tree = ast.parse((Path(root) / rel).read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):
            continue

        def walk(nodes, parent):
            for n in nodes:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if n.name == leaf and (cont is None or cont == parent):
                        kind = ("class" if isinstance(n, ast.ClassDef) else
                                "method" if parent else "function")
                        hits.append({"path": rel, "line": n.lineno, "col": n.col_offset,
                                     "kind": kind, "container": parent or ""})
                    walk(n.body, n.name if isinstance(n, ast.ClassDef) else parent)
                elif isinstance(n, (ast.Assign, ast.AnnAssign)) and parent is None and cont is None:
                    targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                    for t in targets:
                        if isinstance(t, ast.Name) and t.id == leaf:
                            hits.append({"path": rel, "line": n.lineno, "col": t.col_offset,
                                         "kind": "variable", "container": ""})
        walk(tree.body, None)
    return hits


def _pattern_defs(root, name):
    _cont, leaf = _split(name)
    hits = []
    for rel in _files(root):
        ext = Path(rel).suffix.lower()
        rx = next((r for exts, r in _builder._DEF_PATTERNS.items() if ext in exts), None)
        if rx is None:
            continue
        try:
            text = (Path(root) / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, ln in enumerate(text.splitlines(), 1):
            m = rx.match(ln)
            if m and leaf in [g for g in m.groups() if g]:
                hits.append({"path": rel, "line": i, "col": max(0, ln.find(leaf)),
                             "kind": "definition", "container": ""})
    return hits


def _guess_lang(root, name):
    """Which language a bare name probably belongs to: whichever has a
    definition of it, else the project's most common language."""
    counts = {}
    for rel in _files(root):
        lg = lang_of(rel)
        if lg:
            counts[lg] = counts.get(lg, 0) + 1
    return sorted(counts, key=lambda k: -counts[k])


def definitions(mgr, root, name, timeout=45):
    """-> (hits, how). hits: [{path, line, col, kind, container, text}]."""
    cont, leaf = _split(name)
    if not re.fullmatch(r"[A-Za-z_$][\w$]*", leaf or ""):
        raise LSPError(f"{name!r} isn't a symbol name")
    langs = _guess_lang(root, name)
    for lang in langs:
        c = mgr.client(root, lang) if mgr is not None else None
        if c is None:
            continue
        try:
            # some servers only index what is open: open a few files that
            # mention the name so workspace/symbol has something to search
            mentions = _mentions(root, leaf, lang)
            for rel in mentions[:12]:
                c.open(Path(root) / rel)
            res = c.request("workspace/symbol", {"query": leaf}, timeout=timeout) or []
            for _ in range(6):           # a server that just started is still indexing
                if res or not mentions:
                    break
                time.sleep(0.5)
                res = c.request("workspace/symbol", {"query": leaf}, timeout=timeout) or []
        except LSPError:
            continue
        hits = []
        for s in res:
            if s.get("name") != leaf and not str(s.get("name", "")).endswith("." + leaf):
                continue
            if cont and (s.get("containerName") or "").rpartition(".")[2] != cont:
                continue
            loc = s.get("location") or {}
            if "range" not in loc:
                continue
            p = path_of(loc.get("uri", ""))
            if not str(Path(p).resolve()).startswith(str(Path(root).resolve())):
                continue                                  # a library's, not the project's
            st = loc["range"]["start"]
            rel = _rel(root, p)
            hits.append({"path": rel, "line": st["line"] + 1, "col": st["character"],
                         "kind": _KIND.get(s.get("kind"), "symbol"),
                         "container": s.get("containerName") or ""})
        if hits:
            return _dedupe(root, hits), c.name
    hits = _ast_defs(root, name) if "python" in langs or not langs else []
    hits += [h for h in _pattern_defs(root, name) if lang_of(h["path"]) != "python"]
    return _dedupe(root, hits), "syntax (no language server)"


def _dedupe(root, hits):
    seen, out = set(), []
    for h in sorted(hits, key=lambda h: (h["path"], h["line"])):
        k = (h["path"], h["line"])
        if k in seen:
            continue
        seen.add(k)
        h["text"] = _line(root, h["path"], h["line"]).strip()
        out.append(h)
    return out


def _mentions(root, word, lang=None):
    rx = re.compile(r"\b" + re.escape(word) + r"\b")
    out = []
    for rel in _files(root, lang):
        try:
            if rx.search((Path(root) / rel).read_text(encoding="utf-8", errors="replace")):
                out.append(rel)
        except OSError:
            pass
    return out


def references(mgr, root, name, timeout=45, limit=200):
    """-> (refs, how, defs). refs: [{path, line, text}] excluding definitions."""
    defs, how = definitions(mgr, root, name, timeout)
    _cont, leaf = _split(name)
    if defs and not how.startswith("syntax"):
        d = defs[0]
        c = mgr.client(root, lang_of(d["path"]))
        if c is not None:
            try:
                for rel in _mentions(root, leaf, c.lang)[:200]:
                    c.open(Path(root) / rel)
                # the position must be ON the name: definitions point at the
                # start of the declaration line in some servers
                line_text = _line(root, d["path"], d["line"])
                col = line_text.find(leaf, d["col"]) if leaf in line_text else d["col"]
                res = c.request("textDocument/references", {
                    "textDocument": {"uri": uri(Path(root) / d["path"])},
                    "position": {"line": d["line"] - 1, "character": max(col, 0)},
                    "context": {"includeDeclaration": False}}, timeout=timeout) or []
                defset = {(x["path"], x["line"]) for x in defs}
                refs, seen = [], set()
                for loc in res:
                    p = path_of(loc.get("uri", ""))
                    rel = _rel(root, p)
                    ln = loc["range"]["start"]["line"] + 1
                    if (rel, ln) in seen or (rel, ln) in defset:
                        continue
                    seen.add((rel, ln))
                    refs.append({"path": rel, "line": ln, "text": _line(root, rel, ln).strip()})
                refs.sort(key=lambda r: (r["path"], r["line"]))
                return refs[:limit], c.name, defs
            except LSPError:
                pass
    rx = re.compile(r"\b" + re.escape(leaf) + r"\b")
    defset = {(x["path"], x["line"]) for x in defs}
    refs = []
    for rel in _files(root):
        try:
            text = (Path(root) / rel).read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        for i, ln in enumerate(text.splitlines(), 1):
            if rx.search(ln) and (rel, i) not in defset:
                refs.append({"path": rel, "line": i, "text": ln.strip()})
                if len(refs) >= limit:
                    return refs, "text search (no language server: whole-word matches)", defs
    return refs, "text search (no language server: whole-word matches)", defs


def _apply_text_edits(text, edits):
    """Apply LSP TextEdits. Positions count UTF-16 code units, as the protocol
    says, so a line with an emoji before the name still lands right."""
    starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]

    def offset(pos):
        ln = pos["line"]
        if ln >= len(starts):
            return len(text)
        idx = starts[ln]
        end = starts[ln + 1] - 1 if ln + 1 < len(starts) else len(text)
        units = 0
        while idx < end and units < pos["character"]:
            units += 2 if ord(text[idx]) > 0xFFFF else 1
            idx += 1
        return idx
    spans = sorted(((offset(e["range"]["start"]), offset(e["range"]["end"]), e["newText"])
                    for e in edits), reverse=True)
    for a, b, new in spans:
        text = text[:a] + new + text[b:]
    return text


def rename(mgr, root, name, new_name, timeout=60):
    """-> (plan, how): plan is [(rel, before, after)] for every file the
    server's rename touches. Raises LSPError when it can't be done safely."""
    if not re.fullmatch(r"[A-Za-z_$][\w$]*", new_name or ""):
        raise LSPError(f"{new_name!r} isn't a valid name")
    defs, how = definitions(mgr, root, name, timeout)
    if not defs:
        raise LSPError(f"no definition of {name} found")
    if how.startswith("syntax"):
        lang = lang_of(defs[0]["path"]) or "?"
        raise LSPError("rename needs a language server, so only real uses change: install "
                       + INSTALL.get(lang, "one for this language"))
    if len(defs) > 1:
        where = ", ".join(f"{d['path']}:{d['line']}" for d in defs[:5])
        raise LSPError(f"{name} is defined {len(defs)} times ({where}): say which, e.g. "
                       "Class.method")
    d = defs[0]
    c = mgr.client(root, lang_of(d["path"]))
    _cont, leaf = _split(name)
    for rel in _mentions(root, leaf, c.lang)[:200]:
        c.open(Path(root) / rel)
    line_text = _line(root, d["path"], d["line"])
    col = line_text.find(leaf, d["col"]) if leaf in line_text else d["col"]
    edit = c.request("textDocument/rename", {
        "textDocument": {"uri": uri(Path(root) / d["path"])},
        "position": {"line": d["line"] - 1, "character": max(col, 0)},
        "newName": new_name}, timeout=timeout)
    if not edit:
        raise LSPError(f"{c.name} found nothing to rename")
    per_file = {}
    for u, edits in (edit.get("changes") or {}).items():
        per_file.setdefault(path_of(u), []).extend(edits)
    for dc in edit.get("documentChanges") or []:
        if "textDocument" in dc:
            per_file.setdefault(path_of(dc["textDocument"]["uri"]), []).extend(dc.get("edits") or [])
        else:
            raise LSPError(f"{c.name} wants to {dc.get('kind', 'change')} a file; "
                           "do that rename by hand")
    plan = []
    rootp = Path(root).resolve()
    for p, edits in sorted(per_file.items()):
        rp = Path(p).resolve()
        if rootp not in rp.parents:
            raise LSPError(f"the rename would change {p}, outside the project")
        before = rp.read_text(encoding="utf-8")
        after = _apply_text_edits(before, edits)
        if after != before:
            plan.append((_rel(root, rp), before, after))
    return plan, c.name


def report_defs(name, hits, how, cap=30):
    if not hits:
        return f"DEFINITIONS of {name}: none found ({how})."
    rows = [f"{h['path']}:{h['line']}  {h['text'][:160]}"
            + (f"   [{h['kind']}{' in ' + h['container'] if h.get('container') else ''}]")
            for h in hits[:cap]]
    more = f"\n… {len(hits) - cap} more" if len(hits) > cap else ""
    return f"DEFINITIONS of {name} ({len(hits)}, via {how}):\n" + "\n".join(rows) + more


def report_refs(name, refs, how, defs, cap=80):
    head = (f"REFERENCES to {name} ({len(refs)}, via {how})"
            + (f"; defined at {defs[0]['path']}:{defs[0]['line']}" if defs else "") + ":")
    if not refs:
        return head + "\n(none outside the definition)"
    rows = [f"{r['path']}:{r['line']}  {r['text'][:160]}" for r in refs[:cap]]
    more = f"\n… {len(refs) - cap} more" if len(refs) > cap else ""
    return head + "\n" + "\n".join(rows) + more
