"""chats.py — saved conversations and their 24h retention.

Part of Chuck Norris. Split out of the original single file so each concern
can be read, tested and changed on its own.
"""
import re
import time
from pathlib import Path

from . import config

CHAT_TTL_HOURS = config.CHAT_TTL_HOURS

# ── saved-chat retention ────────────────────────────────────────────────────
_CHAT_NAME = re.compile(r"^\d{8}-\d{6}\.json$")   # exactly the ids we write


def chat_files(chats_dir=None):
    """Every saved chat, newest first. Only real files we wrote — no symlinks,
    no recursion, no surprises."""
    out = []
    try:
        root = Path(chats_dir or config.CHATS_DIR)
        for p in root.iterdir():
            if p.is_symlink() or not p.is_file():
                continue
            if not _CHAT_NAME.match(p.name):
                continue
            try:
                out.append((p.stat().st_mtime, p))
            except OSError:
                continue          # vanished between iterdir() and stat()
        # Sorting INSIDE the guard, on mtimes already captured: the retention
        # sweep runs on a timer and can unlink a file between the listing and
        # the sort, which used to raise FileNotFoundError straight into the
        # GTK callback that rebuilds the sidebar.
        out.sort(key=lambda t: t[0], reverse=True)
    except Exception:
        return []
    return [p for _mtime, p in out]


def purge_old_chats(ttl_hours=CHAT_TTL_HOURS, chats_dir=None):
    """Delete saved chats untouched for longer than the TTL. Deliberately narrow:
    it only ever unlinks plain files directly inside CHATS_DIR whose names match
    the exact id pattern this app writes — it cannot walk out of that directory
    or touch anything else."""
    cutoff = time.time() - ttl_hours * 3600
    removed = 0
    for p in chat_files(chats_dir):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except Exception:
            continue
    return removed


def chat_expires_in(path, ttl_hours=CHAT_TTL_HOURS):
    """Human string for how long this chat has left before it self-deletes."""
    try:
        left = (Path(path).stat().st_mtime + ttl_hours * 3600) - time.time()
    except Exception:
        return ""
    if left <= 0:
        return "expiring"
    h = int(left // 3600)
    m = int((left % 3600) // 60)
    return f"{h}h {m}m left" if h else f"{m}m left"
