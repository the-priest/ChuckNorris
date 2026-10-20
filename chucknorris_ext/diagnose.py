"""diagnose.py — LSP-lite: real problems in files on disk, from real tools.

```diagnose``` checks the whole working folder; ```diagnose src/ app.py``` only
those paths. Each language is checked with the best tool the machine has:

  Python      ruff (errors + likely bugs) or pyflakes, then mypy or pyright
              for types when installed; py_compile when nothing else is
  JS / TS     tsc --noEmit when there's a tsconfig, eslint when the project
              configures it, node --check otherwise
  shell       shellcheck, or bash -n
  Rust / Go   cargo check / go vet when it's a cargo or go project
  JSON, TOML  parsed, with the line of the first error

Output is one line per problem, `path:line:col: message [tool]`, grouped and
capped, plus which checks were skipped for lack of a tool — so "no problems"
never silently means "nothing was checked".
"""
import json
import os
import re
import shutil
import subprocess

try:
    import tomllib
except ImportError:                                  # Python < 3.11
    tomllib = None

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
              "target", ".mypy_cache", ".ruff_cache", ".pytest_cache", ".next", ".cache"}
_EXT = {".py": "python", ".pyi": "python", ".js": "js", ".mjs": "js", ".cjs": "js",
        ".jsx": "js", ".ts": "ts", ".tsx": "ts", ".sh": "shell", ".bash": "shell",
        ".json": "json", ".toml": "toml"}
MAX_FILES = 2000
MAX_FINDINGS = 150


def _run(argv, cwd, timeout=120, stdin=None):
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, errors="replace",
                           timeout=timeout, input=stdin,
                           stdin=None if stdin is not None else subprocess.DEVNULL)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"(timed out after {timeout}s)"
    except OSError as ex:
        return 127, str(ex)


def collect(root, paths=()):
    """Files to check: the given paths (files or folders) or the whole root."""
    root = os.path.abspath(root)
    starts = [os.path.join(root, p) if not os.path.isabs(p) else p for p in paths] or [root]
    out = []
    for s in starts:
        s = os.path.normpath(s)
        if os.path.isfile(s):
            out.append(s)
            continue
        for dp, dns, fns in os.walk(s):
            dns[:] = [d for d in dns if d not in _SKIP_DIRS and not d.startswith(".")]
            for f in fns:
                if os.path.splitext(f)[1].lower() in _EXT:
                    out.append(os.path.join(dp, f))
                    if len(out) >= MAX_FILES:
                        return out
    return out


def _rel(root, p):
    try:
        return os.path.relpath(p, root)
    except ValueError:
        return p


def _norm(root, out, tool, pattern=r"^(?P<f>[^\s:][^:]*):(?P<l>\d+):(?:(?P<c>\d+):)?\s*(?P<m>.+)$"):
    """Tool output lines -> 'rel:line:col: message [tool]'."""
    res = []
    for ln in out.splitlines():
        m = re.match(pattern, ln.strip())
        if not m:
            continue
        f = m.group("f")
        if not os.path.isabs(f):
            f = os.path.join(root, f)
        col = m.group("c")
        res.append(f"{_rel(root, f)}:{m.group('l')}{':' + col if col else ''}: "
                   f"{m.group('m').strip()} [{tool}]")
    return res


def _python(root, files, notes):
    found = []
    if shutil.which("ruff"):
        rc, out = _run(["ruff", "check", "--isolated", "--select", "E9,F,B",
                        "--output-format", "concise", "--no-cache", *files], root)
        found += _norm(root, out, "ruff")
    elif shutil.which("pyflakes"):
        rc, out = _run(["pyflakes", *files], root)
        found += _norm(root, out, "pyflakes")
    else:
        notes.append("ruff/pyflakes not installed — Python checked for syntax only")
        for f in files:
            rc, out = _run(["python3", "-m", "py_compile", f], root, timeout=20)
            if rc:
                m = re.search(r'line (\d+)', out)
                found.append(f"{_rel(root, f)}:{m.group(1) if m else 1}: "
                             f"{out.strip().splitlines()[-1]} [py_compile]")
    if shutil.which("mypy"):
        rc, out = _run(["mypy", "--ignore-missing-imports", "--follow-imports=silent",
                        "--no-error-summary", "--hide-error-context", "--no-color-output",
                        "--show-column-numbers", *files], root, timeout=300)
        found += [x for x in _norm(root, out, "mypy") if ": note:" not in x]
    elif shutil.which("pyright"):
        rc, out = _run(["pyright", "--outputjson", *files], root, timeout=300)
        try:
            for d in json.loads(out).get("generalDiagnostics", []):
                if d.get("severity") == "error":
                    r = d["range"]["start"]
                    found.append(f"{_rel(root, d['file'])}:{r['line'] + 1}:{r['character'] + 1}: "
                                 f"{d['message'].splitlines()[0]} [pyright]")
        except (ValueError, KeyError):
            pass
    else:
        notes.append("mypy/pyright not installed — Python types unchecked")
    return found


def _js(root, files, notes):
    found = []
    tsconfig = os.path.isfile(os.path.join(root, "tsconfig.json"))
    tsc = shutil.which("tsc") or (os.path.join(root, "node_modules", ".bin", "tsc")
                                  if os.path.isfile(os.path.join(root, "node_modules", ".bin", "tsc"))
                                  else None)
    if tsconfig and tsc:
        rc, out = _run([tsc, "--noEmit", "--pretty", "false", "-p", root], root, timeout=300)
        found += _norm(root, out, "tsc",
                       r"^(?P<f>[^(]+)\((?P<l>\d+),(?P<c>\d+)\):\s*(?P<m>.+)$")
    elif any(f.endswith((".ts", ".tsx")) for f in files):
        notes.append("TypeScript found but no tsc + tsconfig.json — types unchecked")
    eslint_cfg = any(os.path.exists(os.path.join(root, n)) for n in (
        "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs", ".eslintrc",
        ".eslintrc.js", ".eslintrc.json", ".eslintrc.cjs", ".eslintrc.yml"))
    eslint = shutil.which("eslint") or (os.path.join(root, "node_modules", ".bin", "eslint")
                                        if os.path.isfile(os.path.join(root, "node_modules", ".bin", "eslint"))
                                        else None)
    js_files = [f for f in files if f.endswith((".js", ".mjs", ".cjs", ".jsx"))]
    if eslint and eslint_cfg:
        rc, out = _run([eslint, "--format", "unix", *files], root, timeout=300)
        found += _norm(root, out, "eslint")
    elif shutil.which("node"):
        for f in js_files[:300]:
            if f.endswith(".jsx"):
                continue
            rc, out = _run(["node", "--check", f], root, timeout=20)
            if rc:
                m = re.search(r":(\d+)\n", out)
                msg = next((ln for ln in out.splitlines() if "Error" in ln), out.strip()[:200])
                found.append(f"{_rel(root, f)}:{m.group(1) if m else 1}: {msg.strip()} [node]")
    elif js_files:
        notes.append("node not installed — JavaScript unchecked")
    return found


def _shell(root, files, notes):
    if shutil.which("shellcheck"):
        rc, out = _run(["shellcheck", "-f", "gcc", "-S", "warning", *files], root)
        return _norm(root, out, "shellcheck")
    notes.append("shellcheck not installed — shell checked for syntax only")
    found = []
    for f in files:
        rc, out = _run(["bash", "-n", f], root, timeout=20)
        for ln in out.splitlines():
            m = re.match(r"^(.*?): line (\d+): (.+)$", ln)
            if m:
                found.append(f"{_rel(root, f)}:{m.group(2)}: {m.group(3)} [bash -n]")
    return found


def _data(root, files):
    found = []
    for f in files:
        try:
            with open(f, "rb") as fh:
                raw = fh.read()
            if f.endswith(".json"):
                json.loads(raw.decode("utf-8"))
            elif tomllib is not None:
                tomllib.loads(raw.decode("utf-8"))
        except UnicodeDecodeError:
            found.append(f"{_rel(root, f)}:1: not valid UTF-8 [parse]")
        except json.JSONDecodeError as ex:
            found.append(f"{_rel(root, f)}:{ex.lineno}:{ex.colno}: {ex.msg} [json]")
        except Exception as ex:                      # tomllib.TOMLDecodeError
            m = re.search(r"line (\d+), column (\d+)", str(ex))
            where = f"{m.group(1)}:{m.group(2)}" if m else "1"
            found.append(f"{_rel(root, f)}:{where}: {str(ex).split(' (at')[0]} [toml]")
    return found


def _project(root, notes, whole):
    found = []
    if not whole:
        return found
    if os.path.isfile(os.path.join(root, "Cargo.toml")):
        if shutil.which("cargo"):
            rc, out = _run(["cargo", "check", "--quiet", "--message-format", "short"], root,
                           timeout=600)
            found += _norm(root, out, "cargo")
        else:
            notes.append("Cargo project but cargo not installed")
    if os.path.isfile(os.path.join(root, "go.mod")):
        if shutil.which("go"):
            rc, out = _run(["go", "vet", "./..."], root, timeout=600)
            found += _norm(root, out, "go vet")
        else:
            notes.append("Go module but go not installed")
    return found


def diagnose(root, paths=()):
    """-> (findings, checked_summary, notes)."""
    root = os.path.abspath(root)
    files = collect(root, paths)
    by = {}
    for f in files:
        by.setdefault(_EXT.get(os.path.splitext(f)[1].lower()), []).append(f)
    notes, found = [], []
    if by.get("python"):
        found += _python(root, by["python"], notes)
    if by.get("js") or by.get("ts"):
        found += _js(root, by.get("js", []) + by.get("ts", []), notes)
    if by.get("shell"):
        found += _shell(root, by["shell"], notes)
    if by.get("json") or by.get("toml"):
        found += _data(root, by.get("json", []) + by.get("toml", []))
    found += _project(root, notes, whole=not paths)
    def key(x):
        m = re.match(r"^(.*?):(\d+)", x)
        return (m.group(1), int(m.group(2))) if m else (x, 0)
    found = sorted(dict.fromkeys(found), key=key)
    summary = ", ".join(f"{len(v)} {k}" for k, v in sorted(by.items()) if k)
    return found, summary or "no checkable files", notes


def report(found, summary, notes, label="the working folder"):
    head = f"DIAGNOSE {label}: checked {summary}"
    if not found:
        body = "No problems found."
    else:
        shown = found[:MAX_FINDINGS]
        body = (f"{len(found)} problem(s):\n" + "\n".join(shown)
                + (f"\n… {len(found) - MAX_FINDINGS} more" if len(found) > MAX_FINDINGS else ""))
    tail = ("\nNot checked: " + "; ".join(notes)) if notes else ""
    return f"{head}\n{body}{tail}"
