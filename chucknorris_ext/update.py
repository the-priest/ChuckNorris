"""update.py — know when there's a newer Chuck, and install it from inside the app.

Once a day (Settings can turn it off) Chuck reads VERSION from config.py on
GitHub; a newer one gets a toast. /update installs it without the installer's
package steps (those need sudo, and there is no terminal to ask in):

  * a git clone is updated with `git pull --ff-only` — local edits are never
    overwritten; a pull that can't fast-forward says so and stops;
  * an installed copy downloads ONE archive of the branch (every file from the
    same commit), checks it is complete and compiles, and only then swaps it
    in, keeping the old copy until the swap has succeeded.

Nothing is changed when any check fails. A restart picks up the new code.
"""
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = os.environ.get("CHUCK_REPO", "the-priest/ChuckNorris")
BRANCH = os.environ.get("CHUCK_BRANCH", "main")
CONFIG_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/chucknorris_ext/config.py"
ARCHIVE_URL = f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/{BRANCH}"
CHECK_EVERY = 24 * 3600
_VERSION_RE = re.compile(r'^VERSION\s*=\s*["\']([0-9][0-9A-Za-z.\-+]*)["\']', re.M)
_MODULE_RE = re.compile(r'(?:from chucknorris_ext import ([a-z_]+)|_optional\("([a-z_]+)"\)'
                        r'|^\s*from \. import ([a-z_]+)|^\s*from \.([a-z_]+) import)', re.M)


class UpdateError(Exception):
    pass


def parse_version(text):
    m = _VERSION_RE.search(text or "")
    return m.group(1) if m else None


def _key(v):
    """'15.10.2' -> (15, 10, 2); anything after a non-digit is ignored."""
    out = []
    for part in (v or "").split("."):
        m = re.match(r"\d+", part)
        out.append(int(m.group()) if m else 0)
    return tuple(out + [0] * (4 - len(out)))


def is_newer(remote, local):
    return bool(remote) and _key(remote) > _key(local)


def _open(url, opener, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "chuck-update",
                                               "Cache-Control": "no-cache"})
    op = opener.open if opener else urllib.request.urlopen
    return op(req, timeout=timeout)


def remote_version(opener=None, timeout=10):
    """(ok, version | message)."""
    try:
        with _open(CONFIG_URL, opener, timeout) as r:
            v = parse_version(r.read(200_000).decode("utf-8", "replace"))
    except Exception as ex:
        return False, f"couldn't reach GitHub ({ex.__class__.__name__})"
    return (True, v) if v else (False, "no version in the remote config")


def due(state_path, now=None):
    """True when the last check is more than a day old (or never happened)."""
    try:
        last = float(json.loads(Path(state_path).read_text()).get("checked", 0))
    except (OSError, ValueError, AttributeError):
        last = 0
    return (now or time.time()) - last >= CHECK_EVERY


def mark_checked(state_path, version=None, now=None):
    try:
        p = Path(state_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"checked": now or time.time(), "seen": version}))
    except OSError:
        pass


def is_git_clone(app_dir):
    return (Path(app_dir) / ".git").exists()


def required_modules(app_py):
    """Every chucknorris_ext module the app imports, and the modules THOSE
    import (config pulls in net: the app never names it, but can't start
    without it)."""
    ext = Path(app_py).parent / "chucknorris_ext"
    seen, todo = set(), [Path(app_py)]
    while todo:
        try:
            text = todo.pop().read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for groups in _MODULE_RE.findall(text):
            name = next(g for g in groups if g)
            if name not in seen:
                seen.add(name)
                todo.append(ext / f"{name}.py")
    return sorted(seen)


def verify_tree(src):
    """Raise UpdateError unless src holds a complete Chuck that compiles."""
    src = Path(src)
    app = src / "chucknorris.py"
    ext = src / "chucknorris_ext"
    if not app.is_file() or not ext.is_dir():
        raise UpdateError("the download isn't a Chuck source tree")
    missing = [m for m in required_modules(app) if not (ext / f"{m}.py").is_file()]
    if missing:
        raise UpdateError("the download is missing modules: " + ", ".join(missing))
    for f in [app, *sorted(ext.glob("*.py"))]:
        try:
            compile(f.read_bytes(), str(f), "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as ex:
            raise UpdateError(f"{f.name} doesn't compile: {ex}")
    v = parse_version((ext / "config.py").read_text(encoding="utf-8", errors="replace"))
    if not v:
        raise UpdateError("the download has no version")
    return v


def _extract(archive, dest):
    with tarfile.open(archive) as tf:
        try:
            tf.extractall(dest, filter="data")          # no absolute paths, no links out
        except TypeError:                                # Python < 3.12
            root = Path(dest).resolve()
            for m in tf.getmembers():
                target = (root / m.name).resolve()
                if (root not in target.parents and target != root) or m.issym() or m.islnk():
                    raise UpdateError("the archive has an unsafe path: " + m.name)
            tf.extractall(dest)
    dirs = [d for d in Path(dest).iterdir() if d.is_dir()]
    if len(dirs) != 1:
        raise UpdateError("unexpected archive layout")
    return dirs[0]


def swap_in(src, app_dir, asset_dir=None):
    """Replace app_dir's chucknorris.py and chucknorris_ext with src's.
    The old package stays as chucknorris_ext.old until the new one is in."""
    src, app_dir = Path(src), Path(app_dir)
    ext, old = app_dir / "chucknorris_ext", app_dir / "chucknorris_ext.old"
    staged = app_dir / "chucknorris_ext.new"
    shutil.rmtree(staged, ignore_errors=True)
    shutil.copytree(src / "chucknorris_ext", staged,
                    ignore=shutil.ignore_patterns("__pycache__"))
    tmp_app = app_dir / "chucknorris.py.new"
    shutil.copy2(src / "chucknorris.py", tmp_app)
    shutil.rmtree(old, ignore_errors=True)
    if ext.exists():
        os.replace(ext, old)
    swapped = False
    try:
        os.replace(staged, ext)
        swapped = True
        os.replace(tmp_app, app_dir / "chucknorris.py")
    except OSError:
        # put the working copy back: a new package beside the old app would
        # not start at all
        try:
            if swapped:
                shutil.rmtree(ext, ignore_errors=True)
            if old.exists() and not ext.exists():
                os.replace(old, ext)
        finally:
            shutil.rmtree(staged, ignore_errors=True)
            try:
                tmp_app.unlink()
            except OSError:
                pass
        raise
    shutil.rmtree(old, ignore_errors=True)
    if asset_dir and (src / "assets").is_dir():
        try:
            Path(asset_dir).mkdir(parents=True, exist_ok=True)
            for f in (src / "assets").iterdir():
                if f.is_file():
                    shutil.copy2(f, Path(asset_dir) / f.name)
        except OSError:
            pass                                         # art is not worth failing over


def apply(app_dir, asset_dir=None, opener=None, timeout=60, log=lambda s: None):
    """Install the latest Chuck into app_dir -> (ok, message, version|None)."""
    app_dir = Path(app_dir)
    if is_git_clone(app_dir):
        if not shutil.which("git"):
            return False, "this is a git clone but git isn't installed", None
        log("git pull --ff-only")
        try:
            p = subprocess.run(["git", "-C", str(app_dir), "pull", "--ff-only", "--quiet"],
                               capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as ex:
            return False, f"git pull failed: {ex}", None
        if p.returncode != 0:
            return False, ("git pull couldn't fast-forward (local changes or a diverged "
                           "branch) — nothing was changed:\n" + (p.stderr or p.stdout).strip()), None
        try:
            v = verify_tree(app_dir)
        except UpdateError as ex:
            return False, f"pulled, but {ex}", None
        return True, f"updated the clone to v{v}", v
    with tempfile.TemporaryDirectory(prefix="chuck-update-") as tmp:
        archive = Path(tmp) / "src.tar.gz"
        log("downloading " + ARCHIVE_URL)
        try:
            with _open(ARCHIVE_URL, opener, timeout) as r, open(archive, "wb") as fh:
                shutil.copyfileobj(r, fh)
        except Exception as ex:
            return False, f"download failed ({ex.__class__.__name__}) — nothing was changed", None
        try:
            log("checking the download")
            src = _extract(archive, Path(tmp) / "x")
            v = verify_tree(src)
            log(f"installing v{v}")
            swap_in(src, app_dir, asset_dir)
        except (UpdateError, tarfile.TarError) as ex:
            return False, f"{ex} — nothing was changed", None
        except OSError as ex:
            return False, f"couldn't write the new files: {ex}", None
    return True, f"installed v{v}", v
