"""checkpoint.py — snapshots of the whole working folder, so anything can be undone.

`undo` reverts the files Chuck wrote. It can't revert what a COMMAND changed:
a code generator, `npm init`, a script that rewrote twenty files, a `rm`.
Before every step that acts, the folder is committed into a private "shadow"
git repository that lives in Chuck's data directory, with the folder as its
work tree. Your own .git is never read or written; a folder that isn't a git
repo works the same.

restore() puts every file back as it was at a snapshot: changed files are
restored, files created since are removed, deleted ones come back. It first
snapshots the present, so a rewind can itself be undone.

.gitignore files in the folder are honoured, and dependency and build folders
(node_modules, .venv, target, …) are left out: they're big and rebuildable.
"""
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

EXCLUDE = """\
node_modules/
.venv/
venv/
__pycache__/
*.pyc
.mypy_cache/
.pytest_cache/
.ruff_cache/
.cache/
target/
.next/
.nuxt/
.gradle/
*.iso
*.img
*.qcow2
"""


class CheckpointError(Exception):
    pass


class Shadow:
    def __init__(self, work, store, timeout=20):
        self.work = Path(work).resolve()
        if self.work in (Path("/"), Path.home().resolve()):
            raise CheckpointError("checkpoints need a project folder, not / or your home")
        key = hashlib.sha1(str(self.work).encode()).hexdigest()[:16]
        self.git_dir = Path(store) / key
        self.timeout = timeout

    def _git(self, *args, check=True):
        if not shutil.which("git"):
            raise CheckpointError("git isn't installed")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update({"GIT_DIR": str(self.git_dir), "GIT_WORK_TREE": str(self.work),
                    "GIT_AUTHOR_NAME": "Chuck", "GIT_AUTHOR_EMAIL": "chuck@localhost",
                    "GIT_COMMITTER_NAME": "Chuck", "GIT_COMMITTER_EMAIL": "chuck@localhost",
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
        try:
            p = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.quotepath=off",
                                "-c", "gc.auto=200", "-c", "commit.gpgsign=false", *args],
                               cwd=str(self.work), env=env, capture_output=True, text=True,
                               errors="replace", timeout=self.timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise CheckpointError(f"the folder is too big to snapshot in {self.timeout}s")
        except OSError as ex:
            raise CheckpointError(str(ex))
        if check and p.returncode != 0:
            raise CheckpointError((p.stderr or p.stdout).strip()[-300:] or f"git {args[0]} failed")
        return p.stdout

    def _ensure(self):
        if (self.git_dir / "HEAD").exists():
            return
        self.git_dir.parent.mkdir(parents=True, exist_ok=True)
        self._git("init", "--quiet")
        (self.git_dir / "info").mkdir(exist_ok=True)
        (self.git_dir / "info" / "exclude").write_text(EXCLUDE)
        (self.git_dir / "chuck-folder").write_text(str(self.work) + "\n")

    def snapshot(self, label):
        """Commit the folder as it is now -> commit id."""
        self._ensure()
        self._git("add", "-A", "--ignore-errors", check=False)
        self._git("commit", "--quiet", "--allow-empty", "--no-verify", "-m", label[:200] or "step")
        return self._git("rev-parse", "HEAD").strip()

    def changed_since(self, sha):
        """Paths that differ between the snapshot and the folder now."""
        self._git("add", "-A", "--ignore-errors", check=False)
        out = self._git("diff", "--cached", "--name-only", "--no-renames", sha, check=False)
        return [ln for ln in out.splitlines() if ln]

    def restore(self, sha):
        """Make the folder what it was at sha -> (before_sha, changed paths)."""
        self._git("cat-file", "-e", sha + "^{commit}")
        before = self.snapshot("before rewind")
        changed = [ln for ln in self._git("diff", "--name-only", "--no-renames", sha, before)
                   .splitlines() if ln]
        added = [ln for ln in self._git("diff", "--name-only", "--no-renames",
                                         "--diff-filter=A", sha, before).splitlines() if ln]
        for rel in added:                       # created since the snapshot: remove
            p = self.work / rel
            try:
                if p.is_file() or p.is_symlink():
                    p.unlink()
            except OSError:
                pass
            d = p.parent
            while d != self.work and d.is_dir():   # and folders that are now empty
                try:
                    d.rmdir()
                except OSError:
                    break
                d = d.parent
        if self._git("ls-tree", "-r", "--name-only", sha).strip():
            self._git("checkout", sha, "--", ".")
        self._git("reset", "--quiet", "--soft", before, check=False)
        return before, changed
