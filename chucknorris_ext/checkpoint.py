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
import json
import os
import shutil
import subprocess
import time
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
    def __init__(self, msg, before=None):
        super().__init__(msg)
        self.before = before                 # set when a restore failed part-way


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

    def _unlock(self):
        """A git killed by a timeout leaves index.lock behind, and every later
        snapshot would fail on it. Only Chuck uses this repository and it does
        one thing at a time, so a lock found here is always stale."""
        try:
            (self.git_dir / "index.lock").unlink()
        except OSError:
            pass

    def _names(self, *args):
        out = self._git(*args, "-z", check=False)
        return [p for p in out.split("\0") if p]

    def _manifest_path(self, sha):
        return self.git_dir / "chuck-manifest" / f"{sha}.json"

    def _record_manifest(self, sha):
        """What a snapshot can't hold: the ignored files and the empty folders
        that existed then. Restore must never delete or remove those."""
        ignored = self._names("ls-files", "--others", "--ignored", "--exclude-standard",
                              "--directory")
        empty, n = [], 0
        for dp, dns, fns in os.walk(self.work):
            dns[:] = [d for d in dns if d != ".git"]
            n += 1
            if n > 20000:
                break
            if not dns and not fns:
                empty.append(os.path.relpath(dp, self.work))
        try:
            p = self._manifest_path(sha)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"ignored": ignored, "empty": empty}))
        except OSError:
            pass

    def _manifest(self, sha):
        try:
            return json.loads(self._manifest_path(sha).read_text())
        except (OSError, ValueError):
            return {"ignored": [], "empty": []}

    def nested(self):
        """Folders that are git repositories of their own: their insides
        aren't snapshotted (git keeps only a pointer to them)."""
        out = []
        for line in self._git("ls-files", "-s", "-z", check=False).split("\0"):
            if line.startswith("160000 "):
                out.append(line.split("\t", 1)[-1])
        return out

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
        self._unlock()
        self._git("add", "-A", "--ignore-errors", check=False)
        self._git("commit", "--quiet", "--allow-empty", "--no-verify", "-m", label[:200] or "step")
        sha = self._git("rev-parse", "HEAD").strip()
        self._record_manifest(sha)
        try:
            os.utime(self.git_dir / "chuck-folder")     # last used, for prune()
        except OSError:
            pass
        return sha

    def changed_since(self, sha):
        """Paths that differ between the snapshot and the folder now."""
        self._unlock()
        self._git("add", "-A", "--ignore-errors", check=False)
        return self._names("diff", "--cached", "--name-only", "--no-renames", sha)

    def restore(self, sha):
        """Make the folder what it was at sha -> (before_sha, changed paths).
        Files that were ignored at the snapshot (a .env, a data folder) are
        never deleted, even if a later step changed .gitignore; folders that
        existed then are never removed."""
        self._git("cat-file", "-e", sha + "^{commit}")
        before = self.snapshot("before rewind")
        try:
            changed = self._names("diff", "--name-only", "--no-renames", sha, before)
            added = self._names("diff", "--name-only", "--no-renames", "--diff-filter=A",
                                sha, before)
            man = self._manifest(sha)
            ign = [p.rstrip("/") for p in man.get("ignored", [])]
            keep_dirs = set(man.get("empty", []))

            def was_ignored(rel):
                return any(rel == p or rel.startswith(p + "/") for p in ign)
            # put back what changed or was deleted first, then remove what's new
            if self._git("ls-tree", "-r", "--name-only", sha).strip():
                self._unlock()
                self._git("checkout", sha, "--", ".")
            for rel in added:
                if was_ignored(rel):
                    continue
                p = self.work / rel
                try:
                    if p.is_file() or p.is_symlink():
                        p.unlink()
                except OSError:
                    continue
                d = p.parent
                while d != self.work and d.is_dir():
                    if os.path.relpath(d, self.work) in keep_dirs:
                        break
                    try:
                        d.rmdir()                  # only if now empty
                    except OSError:
                        break
                    d = d.parent
            self._git("reset", "--quiet", "--soft", before, check=False)
        except CheckpointError as ex:
            raise CheckpointError(f"{ex} (the folder may be part-way back; undo the rewind to "
                                  "return to how it was)", before=before)
        return before, [c for c in changed if c not in added or not was_ignored(c)]


def prune(store, days=30):
    """Delete the snapshots of folders not worked in for `days` days, or that
    have been missing for a week (not just a drive that isn't plugged in
    today). -> how many were removed."""
    gone, cutoff, week = 0, time.time() - days * 86400, time.time() - 7 * 86400
    try:
        repos = [d for d in Path(store).iterdir() if d.is_dir()]
    except OSError:
        return 0
    for d in repos:
        mark = d / "chuck-folder"
        try:
            folder = mark.read_text().strip()
            used = mark.stat().st_mtime
        except OSError:
            continue                        # not one of ours (or half-made): leave it
        if used < cutoff or (used < week and not (folder and os.path.isdir(folder))):
            shutil.rmtree(d, ignore_errors=True)
            gone += 1
    return gone
