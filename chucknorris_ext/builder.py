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
    if rel.startswith(("/", "~")) or (len(rel) > 1 and rel[1] == ":"):
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
        if shutil.which("pytest"):
            return [["pytest", "-q"]]
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
            p = subprocess.run(cmd, cwd=str(root), capture_output=True,
                               text=True, timeout=budget)
            rc, out = p.returncode, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            rc, out = 124, f"timed out after {budget}s"
        except Exception as ex:
            rc, out = 1, f"couldn't run: {ex}"
        chunks.append(f"$ {label}  -> exit {rc}\n{out}")
        if rc != 0 and worst == 0:
            worst = rc
    return worst, "\n\n".join(chunks)[-8000:]


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
