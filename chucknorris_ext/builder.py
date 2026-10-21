"""builder.py — Chuck builds real things on disk, the way a careful engineer does.

The loop this exists to support:
    project  →  write complete files  →  verify  →  run tests  →  package  →  hand over

Everything happens inside ONE sandbox directory per project (~/ChuckProjects/<name>).
Every path is resolved and checked to be inside that root before anything is
written, so a stray `../../.ssh/id_rsa` in a generated filename can't escape.
Nothing here executes project code — running is the app's approve-to-run card.
"""
import os
import re
import json
import shutil
import zipfile
import subprocess
from pathlib import Path
from datetime import datetime

PROJECTS = Path.home() / "ChuckProjects"
PROJECTS.mkdir(parents=True, exist_ok=True)

_STATE = PROJECTS / ".current"
MAX_FILE_BYTES = 2_000_000
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", ".ruff_cache", "dist"}


def _slug(name):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-._")
    return (s or "project")[:64]


def project_path(name):
    return PROJECTS / _slug(name)


def project_open(name):
    """Create or switch to a project. Returns (path, created)."""
    p = project_path(name)
    created = not p.exists()
    p.mkdir(parents=True, exist_ok=True)
    try:
        _STATE.write_text(p.name)
    except Exception:
        pass
    return p, created


def current_project():
    """The project Chuck is working in, or None."""
    try:
        n = _STATE.read_text().strip()
    except Exception:
        return None
    p = PROJECTS / n
    return p if n and p.is_dir() else None


def project_list():
    out = []
    try:
        for p in sorted(PROJECTS.iterdir()):
            if p.is_dir() and not p.name.startswith("."):
                n = sum(1 for _ in p.rglob("*") if _.is_file())
                out.append((p.name, n))
    except Exception:
        pass
    return out


def _safe(root, rel):
    """Resolve rel inside root. Anything absolute, home-relative, or containing
    a dot-segment is REFUSED rather than quietly rewritten — silently turning
    '/etc/passwd' into '<project>/etc/passwd' is confusing and hides mistakes."""
    rel = (rel or "").strip()
    if not rel:
        raise ValueError("no filename given")
    if rel.startswith(("/", "~")):
        # The model often names the file by its full path, and it is usually
        # INSIDE the working folder ("/home/me/proj/app.html"). Refusing that
        # cost a whole round trip. Inside: use it. Anywhere else: refuse.
        base = Path(root).resolve()
        absp = Path(os.path.expanduser(rel)).resolve()
        if absp == base or base not in absp.parents:
            raise ValueError(f"give a path inside the project ({base}), not {rel!r}")
        rel = absp.relative_to(base).as_posix()
    if len(rel) > 1 and rel[1] == ":":
        raise ValueError(f"give a path inside the project, not {rel!r}")
    if "\\" in rel:
        raise ValueError("use forward slashes")
    parts = [p for p in rel.split("/") if p != ""]
    if not parts:
        raise ValueError("no filename given")
    for p in parts:
        if p == "." or p == ".." or set(p) == {"."}:
            raise ValueError(f"path segment not allowed: {p!r}")
        if p.startswith("..") or "\x00" in p:
            raise ValueError(f"path segment not allowed: {p!r}")
    root = Path(root).resolve()
    p = (root / "/".join(parts)).resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"path escapes the project: {rel}")
    return p


def write_file(project, rel, content):
    """Write one complete file into the project. Returns (ok, message, path)."""
    try:
        root = Path(project)
        if not root.is_dir():
            return False, f"no such project: {project}", None
        p = _safe(root, rel)
        data = content if isinstance(content, str) else str(content)
        if len(data.encode("utf-8", "ignore")) > MAX_FILE_BYTES:
            return False, "file too large (2MB cap)", None
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data, encoding="utf-8")
        if p.suffix == ".sh" or data.startswith("#!"):
            try:
                os.chmod(p, 0o755)
            except Exception:
                pass
        return True, f"wrote {p.relative_to(root)} ({len(data)} chars)", str(p)
    except Exception as ex:
        return False, f"couldn't write {rel}: {ex}", None


def delete_file(project, rel):
    try:
        root = Path(project)
        p = _safe(root, rel)
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
        else:
            return False, f"no such file: {rel}"
        return True, f"removed {rel}"
    except Exception as ex:
        return False, str(ex)


def tree(project, limit=200):
    """A readable file tree, so Chuck always knows what he has built so far."""
    root = Path(project)
    if not root.is_dir():
        return "(no project)"
    lines, n = [], 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS
                             and not d.startswith("."))
        rel = Path(dirpath).relative_to(root)
        depth = 0 if str(rel) == "." else len(rel.parts)
        if str(rel) != ".":
            lines.append("  " * (depth - 1) + rel.name + "/")
        for f in sorted(filenames):
            if f.startswith(".") or f.endswith(".pyc"):
                continue
            try:
                sz = (Path(dirpath) / f).stat().st_size
            except Exception:
                sz = 0
            lines.append("  " * depth + f"{f}  ({sz}b)")
            n += 1
            if n >= limit:
                lines.append("  \u2026 (truncated)")
                return "\n".join(lines)
    return "\n".join(lines) if lines else "(empty project)"


def read_file(project, rel, limit=60000):
    try:
        p = _safe(Path(project), rel)
        if not p.is_file():
            return False, f"no such file: {rel}"
        return True, p.read_text(encoding="utf-8", errors="ignore")[:limit]
    except Exception as ex:
        return False, str(ex)


def test_command(project):
    """The right way to test THIS project, discovered rather than assumed.

    Returns a LIST OF COMMANDS — every one must pass. Returning a single
    ["python3", "<first test file>"] meant a project with ten test files ran one
    of them and reported "tests passed", which is exactly the false green this
    whole pipeline exists to prevent.
    """
    root = Path(project)
    if (root / "run_tests.sh").is_file():
        return [["bash", "run_tests.sh"]]
    tests = sorted(root.glob("tests/test_*.py")) + sorted(root.glob("test_*.py"))
    if tests:
        # Run each test file individually with python3 rather than using pytest.
        # pytest collection can fail on files that call sys.exit() during import,
        # and individual runs give clearer output per file.
        return [["python3", str(t.relative_to(root))] for t in tests]
    if (root / "package.json").is_file():
        return [["npm", "test", "--silent"]]
    if (root / "Makefile").is_file():
        return [["make", "test"]]
    return None


def test_command_str(project):
    """The discovered test command(s) as one readable line (for the manifest)."""
    cmds = test_command(project)
    if not cmds:
        return None
    return " && ".join(" ".join(c) for c in cmds)


# Set by the app when the folder is sandboxed or read-only: argv -> argv
# (bubblewrap around it), or None when it can't be run safely.
RUN_WRAP = [None]


def _wrapped(argv):
    w = RUN_WRAP[0]
    return list(argv) if w is None else w(list(argv))


def run_tests(project, timeout=300):
    """Run the project's own tests. Returns (rc, output). rc=-1 means none found.

    Every discovered command runs; the first failure is returned, but the output
    of all of them is kept so the model sees the whole picture.
    """
    root = Path(project)
    cmds = test_command(root)
    if not cmds:
        return -1, "no tests found (add tests/test_*.py or run_tests.sh)"
    chunks, worst = [], 0
    budget = max(30, timeout // max(1, len(cmds)))
    for cmd in cmds:
        label = " ".join(cmd)
        try:
            argv = _wrapped(cmd)
            if argv is None:
                chunks.append(f"$ {label}  -> not run: this folder's access needs bubblewrap")
                worst = worst or 126
                continue
            p = subprocess.run(argv, cwd=str(root), capture_output=True,
                               text=True, timeout=budget, start_new_session=True)
            rc, out = p.returncode, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            rc, out = 124, f"timed out after {budget}s"
        except Exception as ex:
            rc, out = 1, f"couldn't run: {ex}"
        chunks.append(f"$ {label}  -> exit {rc}\n{out}")
        if rc != 0 and worst == 0:
            worst = rc
    return worst, ("\n\n".join(chunks))[:32000]


def package(project, note=None):
    """Zip the project into a single deliverable. Returns (ok, zip_path, message)."""
    root = Path(project)
    if not root.is_dir():
        return False, None, "no such project"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = PROJECTS / f"{root.name}-{stamp}.zip"
    n = 0
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for f in filenames:
                    if f.endswith(".pyc"):
                        continue
                    full = Path(dirpath) / f
                    z.write(full, str(Path(root.name) / full.relative_to(root)))
                    n += 1
            if note:
                z.writestr(f"{root.name}/BUILD_NOTES.md", note)
    except Exception as ex:
        return False, None, f"packaging failed: {ex}"
    size = out.stat().st_size
    return True, str(out), f"packaged {n} files \u2192 {out.name} ({size // 1024} KB)"


def manifest(project):
    """A compact JSON summary — what exists, how to test it."""
    root = Path(project)
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if f.endswith(".pyc"):
                continue
            files.append(str((Path(dirpath) / f).relative_to(root)))
    cmd = test_command_str(root)
    return json.dumps({"project": root.name, "files": sorted(files),
                       "test_command": cmd}, indent=2)


# ── surgical edits ───────────────────────────────────────────────────────────
# Rewriting a whole file to change three lines costs the full file in output
# tokens — the most expensive tokens there are — and invites the model to drop
# something on the way through. Aider's SEARCH/REPLACE block is the proven fix:
# quote the exact lines, give their replacement, nothing else is resent.
_EDIT_RE = re.compile(
    r"<{5,9} ?SEARCH[ \t]*\n(.*?)\n?={5,9}[ \t]*\n(.*?)\n?>{5,9} ?REPLACE", re.DOTALL)


def parse_edit_blocks(text):
    """[(search, replace), ...] from one or more SEARCH/REPLACE blocks."""
    return [(m.group(1), m.group(2)) for m in _EDIT_RE.finditer(text or "")]


def _loose_find(hay, needle):
    """Locate needle ignoring trailing whitespace on each line — the commonest
    way a model's quote differs from the file. Returns (start, end) or None,
    and only when the match is unique."""
    norm = lambda s: "\n".join(l.rstrip() for l in s.split("\n"))
    hl, nl = hay.split("\n"), norm(needle).split("\n")
    hits = []
    for i in range(len(hl) - len(nl) + 1):
        if [l.rstrip() for l in hl[i:i + len(nl)]] == nl:
            hits.append(i)
            if len(hits) > 1:
                return None
    if not hits:
        return None
    i = hits[0]
    start = sum(len(l) + 1 for l in hl[:i])
    end = start + len("\n".join(hl[i:i + len(nl)]))
    return start, end


def edit_file(project, rel, blocks_text):
    """Apply SEARCH/REPLACE blocks to one file. All-or-nothing: if any block
    fails to match exactly once, nothing is written. Returns (ok, message).

    An empty SEARCH on a file that does not exist yet creates it, so the same
    tool covers "new file" without a second syntax.
    """
    try:
        root = Path(project)
        p = _safe(root, rel)
        blocks = parse_edit_blocks(blocks_text)
        if not blocks:
            return False, ("no SEARCH/REPLACE block found — use <<<<<<< SEARCH / "
                           "======= / >>>>>>> REPLACE")
        if not p.exists():
            if len(blocks) == 1 and not blocks[0][0].strip():
                return write_file(root, rel, blocks[0][1] + "\n")[:2]
            return False, f"no such file: {rel} (read it first, or write it)"
        text = p.read_text(encoding="utf-8")
        for n, (search, repl) in enumerate(blocks, 1):
            if not search.strip():
                return False, f"block {n}: empty SEARCH on an existing file"
            c = text.count(search)
            if c == 1:
                text = text.replace(search, repl, 1)
                continue
            if c > 1:
                return False, (f"block {n}: SEARCH matches {c} places in {rel} — "
                               "include more surrounding lines so it is unique")
            span = _loose_find(text, search)
            if span is None:
                return False, (f"block {n}: SEARCH text not found in {rel} — read the "
                               "file and quote the current lines exactly")
            text = text[:span[0]] + repl + text[span[1]:]
        p.write_text(text, encoding="utf-8")
        return True, f"edited {rel} ({len(blocks)} change{'s' if len(blocks) != 1 else ''})"
    except Exception as ex:
        return False, f"couldn't edit {rel}: {ex}"


# ── search ───────────────────────────────────────────────────────────────────
_GREP_SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip",
                  ".gz", ".xz", ".zst", ".so", ".o", ".a", ".bin", ".pyc", ".lock",
                  ".woff", ".woff2", ".ttf", ".mp3", ".mp4", ".onnx"}


def grep(root, pattern, sub="", max_hits=60, max_line=220):
    """Regex search under root[/sub] -> (ok, text). Uses ripgrep when present
    (fast, honours .gitignore), else a pure-Python walk. Output is capped hard:
    a search exists to FIND things, and 2,000 matches pasted into the context
    window find nothing except the bill."""
    try:
        base = _safe(root, sub) if sub else Path(root).resolve()
    except ValueError as ex:
        return False, str(ex)
    if not base.exists():
        return False, f"no such path: {sub}"
    rg = shutil.which("rg")
    if rg:
        try:
            p = subprocess.run([rg, "-n", "--no-heading", "--color", "never",
                                "-M", str(max_line), "-m", "20", "-e", pattern,
                                str(base)], capture_output=True, text=True, timeout=30)
            if p.returncode not in (0, 1):
                return False, (p.stderr or "rg failed").strip()[:300]
            rows = (p.stdout or "").splitlines()
        except Exception as ex:
            return False, f"search failed: {ex}"
    else:
        try:
            rx = re.compile(pattern)
        except re.error as ex:
            return False, f"bad regex: {ex}"
        rows = []
        files = [base] if base.is_file() else None
        walker = ([(str(base.parent), [], [base.name])] if files
                  else os.walk(base))
        for dirpath, dirnames, filenames in walker:
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                           and not d.startswith(".")]
            for f in filenames:
                fp = Path(dirpath) / f
                if fp.suffix.lower() in _GREP_SKIP_EXT:
                    continue
                try:
                    if fp.stat().st_size > MAX_FILE_BYTES:
                        continue
                    with open(fp, "r", encoding="utf-8", errors="strict") as fh:
                        for i, line in enumerate(fh, 1):
                            if rx.search(line):
                                rows.append(f"{fp}:{i}:{line.rstrip()[:max_line]}")
                except (UnicodeDecodeError, OSError):
                    continue
                if len(rows) > max_hits * 3:
                    break
    rootp = str(Path(root).resolve()) + os.sep
    rows = [r[len(rootp):] if r.startswith(rootp) else r for r in rows]
    if not rows:
        return True, f"no matches for /{pattern}/"
    more = len(rows) - max_hits
    out = "\n".join(rows[:max_hits])
    if more > 0:
        out += f"\n… {more} more matches — narrow the pattern or the path"
    return True, out


# ── the big picture ──────────────────────────────────────────────────────────
# Reading a repository file by file to learn its shape is the slowest, most
# expensive way an agent can orient itself. Aider's answer is a repo map: every
# file with the names it defines, in a few thousand tokens. This is the
# stdlib-only version — real AST for Python, line patterns for the rest.
_DEF_PATTERNS = {
    (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"): re.compile(
        r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\*?\s+(\w+)|class\s+(\w+)"
        r"|(?:const|let)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>|interface\s+(\w+)"
        r"|type\s+(\w+)\s*=)"),
    (".go",): re.compile(r"^(?:func\s+(?:\([^)]*\)\s*)?(\w+)|type\s+(\w+))"),
    (".rs",): re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:fn|struct|enum|trait|mod)\s+(\w+)"),
    (".sh", ".bash"): re.compile(r"^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{?"),
    (".c", ".h", ".cpp", ".hpp", ".cc"): re.compile(
        r"^(?:[\w:<>*&\s]+?\s)?\**(\w+)\s*\([^;]*\)\s*\{?\s*$|^(?:struct|class|enum)\s+(\w+)"),
    (".java", ".kt", ".cs", ".swift"): re.compile(
        r"^\s*(?:public|private|protected|internal|open|data|abstract|final|static|\s)*"
        r"(?:class|interface|enum|object|struct|fun|func)\s+(\w+)"),
    (".rb",): re.compile(r"^\s*(?:def|class|module)\s+([\w.?!]+)"),
    (".php",): re.compile(r"^\s*(?:abstract\s+|final\s+)?(?:function|class|interface|trait)\s+(\w+)"),
    (".lua",): re.compile(r"^\s*(?:local\s+)?function\s+([\w.:]+)"),
}


def _project_files(root, limit=2000):
    """Files worth knowing about: git's view when it is a repo (so .gitignore
    is honoured), else a walk that skips the usual build and vendor dirs."""
    root = Path(root)
    if (root / ".git").exists() and shutil.which("git"):
        # the repo's config could run a command (core.fsmonitor): off, and in
        # the folder's sandbox when it has one
        argv = _wrapped(["git", "-c", "core.fsmonitor=false", "-C", str(root), "ls-files",
                         "-co", "--exclude-standard"])
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=20) if argv else None
            if p is not None and p.returncode == 0:
                return [f for f in p.stdout.splitlines() if f][:limit]
        except Exception:
            pass
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS
                             and not d.startswith("."))
        for f in sorted(filenames):
            if f.startswith("."):
                continue
            out.append(str((Path(dirpath) / f).relative_to(root)))
            if len(out) >= limit:
                return out
    return out


def _py_defs(text):
    import ast as _ast
    try:
        tree = _ast.parse(text)
    except SyntaxError:
        return None
    names = []
    for node in tree.body:
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            names.append(node.name + "()")
        elif isinstance(node, _ast.ClassDef):
            meths = [n.name for n in node.body
                     if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                     and not (n.name.startswith("__") and n.name != "__init__")]
            names.append(node.name + (f"[{', '.join(meths[:12])}"
                                      f"{', …' if len(meths) > 12 else ''}]" if meths else ""))
    return names


def repo_map(root, max_chars=16000):
    """'path (N lines): defs…' for every source file, capped. -> (ok, text)."""
    root = Path(root)
    if not root.is_dir():
        return False, f"no such folder: {root}"
    files = _project_files(root)
    lines, total = [], 0
    for rel in files:
        fp = root / rel
        if fp.suffix.lower() in _GREP_SKIP_EXT:
            continue
        try:
            if not fp.is_file() or fp.stat().st_size > MAX_FILE_BYTES:
                continue
            text = fp.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        n = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        ext = fp.suffix.lower()
        defs = None
        if ext == ".py":
            defs = _py_defs(text)
        else:
            for exts, rx in _DEF_PATTERNS.items():
                if ext in exts:
                    defs = []
                    for ln in text.splitlines():
                        m = rx.match(ln)
                        if m:
                            nm = next((g for g in m.groups() if g), None)
                            if nm and nm not in defs and nm not in ("if", "for", "while",
                                                                    "switch", "return"):
                                defs.append(nm)
                    break
        row = f"{rel} ({n} lines)"
        if defs:
            shown = ", ".join(defs[:30]) + (f", … +{len(defs) - 30}" if len(defs) > 30 else "")
            row += ": " + shown
        if total + len(row) > max_chars:
            lines.append(f"… {len(files) - len(lines)} more files — use glob/grep to narrow")
            break
        lines.append(row)
        total += len(row) + 1
    if not lines:
        return True, "(no readable source files)"
    return True, "\n".join(lines)


def glob_files(root, pattern, limit=300):
    """Paths under root matching a glob ('**/*.py', 'src/*.ts'), skipping
    vendor/build dirs. -> (ok, text)."""
    root = Path(root).resolve()
    pattern = (pattern or "").strip()
    if not pattern:
        return False, "no pattern given"
    if pattern.startswith("/") or ".." in Path(pattern).parts:
        return False, "give a pattern relative to the working folder"
    if "/" not in pattern and not pattern.startswith("**"):
        pattern = "**/" + pattern
    hits = []
    try:
        for p in root.glob(pattern):
            rel = p.relative_to(root)
            if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
                continue
            hits.append(str(rel) + ("/" if p.is_dir() else ""))
            if len(hits) >= limit:
                break
    except (ValueError, OSError) as ex:
        return False, f"bad pattern: {ex}"
    if not hits:
        return True, f"no files match {pattern}"
    hits.sort()
    more = f"\n… stopped at {limit} matches" if len(hits) >= limit else ""
    return True, "\n".join(hits) + more


def read_lines(path, start=None, end=None, max_chars=48000, label=None):
    """Read a text file, or a 1-based line range of it, for the model.

    -> (ok, text, footer). A file larger than max_chars comes back in pages:
    the footer says which lines were shown and exactly how to ask for the
    next ones, so a 6,000-line file is readable end to end instead of being
    silently cut at some byte count.
    """
    p = Path(path)
    try:
        if p.stat().st_size > 64 * 1024 * 1024:
            return False, f"{p.name} is over 64 MB — grep it instead", ""
        with open(p, "rb") as fh:
            head = fh.read(4096)
            if b"\x00" in head:
                return False, f"{p.name} looks binary", ""
            data = head + fh.read()
    except OSError as ex:
        return False, f"couldn't read {path}: {ex}", ""
    lines = data.decode("utf-8", "replace").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    n = len(lines)
    s = max(1, int(start or 1))
    e = min(n, int(end)) if end else n
    if s > n:
        return False, f"{p.name} has only {n} lines", ""
    out, used, last = [], 0, s - 1
    for i in range(s - 1, e):
        ln = lines[i]
        if used + len(ln) + 1 > max_chars and out:
            break
        out.append(ln)
        used += len(ln) + 1
        last = i + 1
    footer = ""
    name = label or str(path)            # echo the path the way it was asked for
    if last < n:
        footer = (f"[lines {s}-{last} of {n} shown — continue with "
                  f"```read {name}:{last + 1}-{min(n, last + (last - s + 1))}```]")
    elif s > 1 or end:
        footer = f"[lines {s}-{last} of {n}]"
    return True, "\n".join(out), footer


# ── diffs ────────────────────────────────────────────────────────────────────
def diff_lines(before, after, rel, max_lines=800):
    """Unified diff of two texts as a list of lines (capped), plus counts.
    -> (lines, added, removed). None before = a new file."""
    import difflib
    a = (before or "").splitlines()
    b = (after or "").splitlines()
    d = list(difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}", n=3, lineterm=""))
    added = sum(1 for ln in d if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in d if ln.startswith("-") and not ln.startswith("---"))
    if len(d) > max_lines:
        d = d[:max_lines] + [f"… {len(d) - max_lines} more diff lines"]
    return d, added, removed


# ── unified diffs: several files, one atomic change ─────────────────────────
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _diff_path(p):
    p = p.strip().split("\t")[0].strip()
    if p == "/dev/null":
        return None
    if p.startswith(("a/", "b/")):
        p = p[2:]
    return p


def parse_unified_diff(text):
    """-> [{'old': path|None, 'new': path|None, 'hunks': [(old_start, [lines])]}].
    Raises ValueError on a diff it can't read."""
    files, cur, hunk = [], None, None
    lines = (text or "").splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            cur = {"old": _diff_path(ln[4:]), "new": _diff_path(lines[i + 1][4:]), "hunks": []}
            files.append(cur)
            hunk = None
            i += 2
            continue
        m = _HUNK.match(ln)
        if m:
            if cur is None:
                raise ValueError("a hunk before any ---/+++ file header")
            hunk = (int(m.group(1)), [])
            cur["hunks"].append(hunk)
        elif hunk is not None and (ln.startswith((" ", "+", "-")) or ln == ""):
            hunk[1].append(ln if ln else " ")
        elif ln.startswith("\\"):                       # "\ No newline at end of file"
            pass
        elif ln.startswith(("diff ", "index ", "new file", "deleted file", "similarity",
                            "rename ", "old mode", "new mode")):
            pass
        i += 1
    if not files:
        raise ValueError("no ---/+++ file headers found")
    return files


def _find(hay, needle, near, loose):
    """Index of needle (a list of lines) in hay, nearest to `near`. -1 if absent;
    -2 if it occurs more than once at the same distance (ambiguous)."""
    if not needle:
        return min(max(near, 0), len(hay))
    norm = (lambda s: s.rstrip()) if not loose else (lambda s: " ".join(s.split()))
    n = [norm(x) for x in needle]
    hits = [i for i in range(len(hay) - len(n) + 1)
            if norm(hay[i]) == n[0] and [norm(x) for x in hay[i:i + len(n)]] == n]
    if not hits:
        return -1
    hits.sort(key=lambda i: abs(i - near))
    if len(hits) > 1 and abs(hits[0] - near) == abs(hits[1] - near):
        return -2
    return hits[0]


def apply_hunks(content, hunks, label):
    """Apply hunks to text. -> new text. Raises ValueError naming the hunk."""
    had_nl = content.endswith("\n") if content else True
    lines = content.splitlines() if content else []
    offset = 0
    for k, (start, body) in enumerate(hunks, 1):
        old = [x[1:] for x in body if x[:1] in (" ", "-")]
        new = [x[1:] for x in body if x[:1] in (" ", "+")]
        near = max(0, start - 1 + offset)
        at = _find(lines, old, near, loose=False)
        if at == -1:
            at = _find(lines, old, near, loose=True)
        if at == -1:
            raise ValueError(f"{label}: hunk {k} doesn't match the file (its context and "
                             "removed lines must be the file's current lines — read it again)")
        if at == -2:
            raise ValueError(f"{label}: hunk {k} matches more than one place — add context")
        lines[at:at + len(old)] = new
        offset += len(new) - len(old)
    return "\n".join(lines) + ("\n" if (had_nl and lines) else "")


def plan_patch(root, text):
    """Work out a whole diff without touching disk.
    -> [(rel, before|None, after|None)] — after None means delete. Raises ValueError."""
    out = []
    pending = {}                      # rel -> content after earlier sections of this diff

    def current(rel, path):
        if rel in pending:
            return pending[rel]
        return path.read_text(encoding="utf-8") if path.is_file() else None
    for f in parse_unified_diff(text):
        rel = f["new"] or f["old"]
        if rel is None:
            raise ValueError("a file with /dev/null on both sides")
        path = _safe(root, rel)
        before = current(rel, path)
        if f["new"] is None:                                     # deletion
            if before is None:
                raise ValueError(f"{rel}: can't delete — it doesn't exist")
            out.append((rel, before, None))
            pending[rel] = None
            continue
        if f["old"] is None and before is not None and f["hunks"]:
            # a "new file" that exists: apply as an edit only if it's a full rewrite
            raise ValueError(f"{rel}: already exists — diff it against its current content")
        after = apply_hunks(before or "", f["hunks"], rel)
        if f["old"] and f["new"] and f["old"] != f["new"]:     # a rename
            out.append((f["old"], current(f["old"], _safe(root, f["old"])), None))
            pending[f["old"]] = None
        out.append((rel, before, after))
        pending[rel] = after
    # one entry per file: the first 'before' (what's on disk), the last 'after'
    merged = {}
    for rel, before, after in out:
        merged[rel] = (merged[rel][0], after) if rel in merged else (before, after)
    return [(rel, b, a) for rel, (b, a) in merged.items()]
