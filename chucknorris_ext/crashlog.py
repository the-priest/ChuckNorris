"""crashlog.py — every error Chuck hits on your machine, written down.

Exceptions raised inside GTK callbacks, in worker threads and in tools used
to print to a terminal nobody was looking at (Chuck is launched from the app
menu), so "it's buggy" came with nothing to go on. They now go to

    ~/.local/state/chucknorris/chuck.log   (XDG_STATE_HOME honoured)

with a timestamp, Chuck's version and the traceback; the file rotates at
1 MB (one old copy kept). Only tracebacks are written — never settings,
prompts or keys. /doctor shows the latest entries and Settings opens the file.
"""
import os
import sys
import threading
import time
import traceback
from pathlib import Path

MAX_BYTES = 1_000_000
_LOCK = threading.Lock()
_VERSION = ["?"]


def log_path():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "chucknorris" / "chuck.log"


def record(kind, text):
    """Append one entry. Never raises: logging must not become the crash."""
    try:
        p = log_path()
        with _LOCK:
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists() and p.stat().st_size > MAX_BYTES:
                os.replace(p, p.with_suffix(".log.1"))
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(f"=== {stamp}  v{_VERSION[0]}  {kind}\n{text.rstrip()}\n\n")
    except Exception:
        pass


def set_version(version):
    _VERSION[0] = version


def install(version):
    """Route uncaught exceptions (UI thread and worker threads) to the log,
    keeping whatever handler was there before."""
    set_version(version)
    prev = sys.excepthook

    def hook(t, v, tb):
        if not issubclass(t, KeyboardInterrupt):
            record("uncaught " + t.__name__, "".join(traceback.format_exception(t, v, tb)))
        prev(t, v, tb)
    sys.excepthook = hook
    prev_thread = getattr(threading, "excepthook", None)

    def thread_hook(args):
        if not issubclass(args.exc_type, (KeyboardInterrupt, SystemExit)):
            record(f"thread {getattr(args.thread, 'name', '?')}",
                   "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                                      args.exc_traceback)))
        if prev_thread:
            prev_thread(args)
    threading.excepthook = thread_hook


def recent(limit=5, days=7):
    """[(when, kind, last line of the traceback)], newest first."""
    p = log_path()
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out, cutoff = [], time.time() - days * 86400
    for block in text.split("=== ")[1:]:
        head, _, body = block.partition("\n")
        parts = head.split("  ")
        when = parts[0].strip()
        try:
            ts = time.mktime(time.strptime(when, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        if ts < cutoff:
            continue
        lines = [ln for ln in body.strip().splitlines() if ln.strip()]
        out.append((when, parts[-1].strip() if len(parts) > 2 else "", lines[-1] if lines else ""))
    return out[::-1][:limit]
