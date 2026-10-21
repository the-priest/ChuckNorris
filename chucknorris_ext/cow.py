"""cow.py — copy-on-write workspaces, so several agents can change code at
once. See docs/OS.md (phase 17).

A background agent never writes into your folder. It gets two copies of it:
`base` (the folder as it was when the agent started, never touched again)
and `work` (where it edits, builds and tests). When it is done, merge()
brings its changes home file by file, three-way:

  * a file only the agent changed  -> takes the agent's version
  * a file only you changed since  -> stays yours
  * a file both changed            -> `git merge-file` merges them; if the
                                      edits overlap, nothing is written and
                                      the agent's version is set aside for you
  * a file the agent created/deleted -> created / deleted, unless you changed
                                      it in the meantime (then it's a conflict)

Dependency and build folders are not copied (they are big and rebuildable);
a folder too big to copy is refused rather than half-copied.
"""
import filecmp
import os
import shutil
import subprocess
from pathlib import Path

SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", ".cache", "target", ".next", ".nuxt", ".gradle", "dist", "build",
        ".tox", ".chuck-conflicts"}
MAX_BYTES = 400 * 1024 * 1024
MAX_FILES = 50000


class CowError(Exception):
    pass


def _files(root):
    """Relative paths of every file worth copying under root."""
    root = Path(root)
    out, n = [], 0
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if d not in SKIP)
        for f in fns:
            p = Path(dp) / f
            if p.is_symlink() and not p.exists():
                continue
            out.append(str(p.relative_to(root)))
            n += 1
            if n > MAX_FILES:
                raise CowError(f"the folder has more than {MAX_FILES} files to copy")
    return sorted(out)


def _copy_tree(src, dst):
    size = 0
    for rel in _files(src):
        s, d = Path(src) / rel, Path(dst) / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        try:
            if s.is_symlink():
                os.symlink(os.readlink(s), d)
                continue
            size += s.stat().st_size
            if size > MAX_BYTES:
                raise CowError(f"the folder is over {MAX_BYTES // 2**20} MB "
                               "(not counting dependencies) — too big to copy")
            shutil.copy2(s, d)
        except OSError as ex:
            if isinstance(ex, CowError):
                raise
            continue                            # unreadable: the agent won't have it


class Workspace:
    """An agent's private copy of a folder. dir/base is the starting point,
    dir/work is where the agent works."""

    def __init__(self, source, dir_):
        self.source = Path(source).resolve()
        self.dir = Path(dir_)
        self.base = self.dir / "base"
        self.work = self.dir / "work"

    @classmethod
    def create(cls, source, store, name):
        source = Path(source).resolve()
        if not source.is_dir():
            raise CowError(f"no such folder: {source}")
        if source in (Path("/"), Path.home().resolve()):
            raise CowError("a background agent needs a project folder, not / or your home")
        d = Path(store) / name
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        ws = cls(source, d)
        try:
            _copy_tree(source, ws.base)
            ws.base.mkdir(parents=True, exist_ok=True)
            _copy_tree(ws.base, ws.work)
            ws.work.mkdir(parents=True, exist_ok=True)
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise
        return ws

    def changes(self):
        """What the agent changed: {rel: 'added' | 'modified' | 'deleted'}."""
        base, work = set(_files(self.base)), set(_files(self.work))
        out = {}
        for rel in work - base:
            out[rel] = "added"
        for rel in base - work:
            out[rel] = "deleted"
        for rel in base & work:
            if not filecmp.cmp(self.base / rel, self.work / rel, shallow=False):
                out[rel] = "modified"
        return dict(sorted(out.items()))

    def diff(self, limit=200000):
        """A unified diff of the agent's changes (git diff --no-index)."""
        parts = []
        for rel, kind in self.changes().items():
            a = "base/" + rel if kind != "added" else os.devnull
            b = "work/" + rel if kind != "deleted" else os.devnull
            try:
                p = subprocess.run(["git", "diff", "--no-index", "--no-color", "--", a, b],
                                   cwd=str(self.dir), capture_output=True, text=True,
                                   errors="replace", timeout=30)
                parts.append(p.stdout.replace(" a/base/", " a/").replace(" b/work/", " b/")
                             .replace(" b/base/", " b/").replace(" a/work/", " a/")
                             .replace("--- a/base/", "--- a/").replace("+++ b/work/", "+++ b/"))
            except (OSError, subprocess.TimeoutExpired):
                parts.append(f"--- {rel} ({kind})\n")
            if sum(len(x) for x in parts) > limit:
                parts.append("… (diff cut)")
                break
        return "".join(parts)

    def merge(self, conflict_dir=None):
        """Bring the agent's changes into the source folder, three-way.
        -> {"applied": [...], "merged": [...], "conflicts": [...], "kept": [...]}.
        A conflicting file is never written: the agent's version goes to
        <source>/.chuck-conflicts/<rel> for you to look at."""
        res = {"applied": [], "merged": [], "conflicts": [], "kept": []}
        cdir = Path(conflict_dir) if conflict_dir else self.source / ".chuck-conflicts"
        for rel, kind in self.changes().items():
            mine = self.source / rel
            base, theirs = self.base / rel, self.work / rel
            mine_now = mine.exists()
            mine_same_as_base = (mine_now and base.exists()
                                 and filecmp.cmp(mine, base, shallow=False))
            if kind == "added":
                if not mine_now:
                    mine.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(theirs, mine)
                    res["applied"].append(rel)
                elif filecmp.cmp(mine, theirs, shallow=False):
                    res["kept"].append(rel)
                else:
                    self._park(cdir, rel, theirs)
                    res["conflicts"].append(rel)
            elif kind == "deleted":
                if not mine_now:
                    res["kept"].append(rel)
                elif mine_same_as_base:
                    mine.unlink()
                    res["applied"].append(rel)
                else:
                    res["conflicts"].append(rel)     # you changed what it deleted
            else:                                   # modified
                if not mine_now:
                    self._park(cdir, rel, theirs)
                    res["conflicts"].append(rel)     # you deleted what it changed
                elif mine_same_as_base:
                    shutil.copy2(theirs, mine)
                    res["applied"].append(rel)
                elif filecmp.cmp(mine, theirs, shallow=False):
                    res["kept"].append(rel)
                else:
                    merged = self._merge3(mine, base, theirs)
                    if merged is None:
                        self._park(cdir, rel, theirs)
                        res["conflicts"].append(rel)
                    else:
                        mine.write_bytes(merged)
                        res["merged"].append(rel)
        return res

    @staticmethod
    def _merge3(mine, base, theirs):
        """git merge-file -p: the merged bytes, or None on overlapping edits."""
        try:
            p = subprocess.run(["git", "merge-file", "-p", "-q", str(mine), str(base), str(theirs)],
                               capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return p.stdout if p.returncode == 0 else None

    @staticmethod
    def _park(cdir, rel, theirs):
        dest = Path(cdir) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(theirs, dest)

    def remove(self):
        shutil.rmtree(self.dir, ignore_errors=True)
