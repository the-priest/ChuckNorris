"""aiwatch.py — talk to Chuck from your own editor. See docs/OS.md (phase 25).

With /watch on, Chuck watches the working folder. Write a comment that ends
(or starts) with AI! in any file, in any editor, and save it:

    def total(items):   # make this skip None values AI!

and Chuck does what it says, then removes the marker. A comment with AI?
is a question: Chuck answers it in the chat and changes nothing. Watching
costs nothing: it's a cheap scan of what changed (mtimes), no model call
until a marker appears; a marker is acted on once.

Pure: no GTK. The app sends what scan() finds as a turn.
"""
import os
import re
from pathlib import Path

SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", ".cache", "target", ".next", ".nuxt", ".gradle", "dist", "build", ".tox",
        ".chuck-conflicts", ".idea", ".vscode"}
MAX_FILES = 8000
MAX_BYTES = 1_000_000
# a comment, with AI! or AI? at its end or its start
_COMMENT = r"(?:#|//|--|;|%|/\*|<!--|\*)"
MARK = re.compile(
    r"(?:" + _COMMENT + r"\s*(?P<a>.*?)\s*\bAI(?P<k1>[!?])\s*(?:\*/|-->)?\s*$"
    r"|" + _COMMENT + r"\s*AI(?P<k2>[!?])\s+(?P<b>.*?)\s*(?:\*/|-->)?\s*$)")


def _files(root):
    n = 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP and not d.startswith(".")]
        for f in fns:
            n += 1
            if n > MAX_FILES:
                return
            yield os.path.join(dp, f)


def snapshot(root):
    """{path: mtime_ns} of every file worth watching."""
    out = {}
    for p in _files(root):
        try:
            out[p] = os.stat(p).st_mtime_ns
        except OSError:
            continue
    return out


def markers_in(path, root, context=3):
    """[(rel, line_no, kind, text, context)] in one file; kind '!' or '?'."""
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return []
        data = Path(path).read_bytes()
    except OSError:
        return []
    if b"\0" in data[:4096]:
        return []                                    # binary
    lines = data.decode("utf-8", "replace").splitlines()
    rel = os.path.relpath(path, root)
    out = []
    for i, ln in enumerate(lines):
        if "AI" not in ln:
            continue
        m = MARK.search(ln)
        if not m:
            continue
        kind = m.group("k1") or m.group("k2")
        text = (m.group("a") if m.group("k1") else m.group("b")) or ""
        lo, hi = max(0, i - context), min(len(lines), i + context + 1)
        ctx = "\n".join(f"{n + 1:>5}  {lines[n]}" for n in range(lo, hi))
        out.append((rel, i + 1, kind, text.strip(), ctx))
    return out


def scan(root, before):
    """What changed since `before` (a snapshot) -> (new snapshot, markers in
    the changed files)."""
    now = snapshot(root)
    changed = [p for p, t in now.items() if before.get(p) != t]
    found = []
    for p in changed:
        found += markers_in(p, root)
    return now, found


def prompt_for(found):
    """The turn Chuck is given for what it found."""
    acts = [f for f in found if f[2] == "!"]
    asks = [f for f in found if f[2] == "?"]
    parts = []
    if acts:
        parts.append("I left instructions in the code (comments marked AI!). Do what each one asks, "
                     "then REMOVE the AI! markers (keep any other comment text only if it still "
                     "makes sense):")
        for rel, n, _k, text, ctx in acts:
            parts.append(f"- {rel}:{n}: {text or '(see the code around it)'}\n```\n{ctx}\n```")
    if asks:
        parts.append(("Also, " if acts else "") + "answer the question(s) I left in the code "
                     "(comments marked AI?) here in the chat; don't change files for them, and "
                     "remove the AI? markers afterwards:")
        for rel, n, _k, text, ctx in asks:
            parts.append(f"- {rel}:{n}: {text or '(see the code around it)'}\n```\n{ctx}\n```")
    return "\n\n".join(parts)
