"""ledger.py — tamper-evident record of every command Chuck actually ran.

Chat scrollback is not evidence: it is editable, it gets purged after the
retention window, and it does not survive a crash. This is an append-only JSONL
file, 0600, where each entry carries the SHA-256 of the previous one.

The chain does not stop anyone with write access from rewriting the file — it
makes an *undetected* rewrite hard. Removing or editing an entry breaks every
`prev` link after it, and `verify()` reports the first index where the chain
parts. That is the honest claim: tamper-EVIDENT, not tamper-proof. Anyone who
can write this file can also rewrite the whole chain, so the guarantee is
against accidental loss and casual after-the-fact editing, not against an
attacker who already owns the account.

Entries are written after the command completes, so a hard kill mid-command
leaves no entry. The `started` timestamp records when it began.
"""

import os
import json
import time
import hashlib
import threading

from chucknorris_ext import config

LEDGER = config.DATA_DIR / "ledger.jsonl"
ANCHOR = config.DATA_DIR / "ledger.anchor"
GENESIS = "0" * 64
MAX_OUTPUT_KEPT = 4000
MAX_LEDGER_BYTES = 8_000_000      # roll over past this; see _rotate()
_TAIL_BYTES = 65_536              # enough to hold the last entry, comfortably

_LOCK = threading.RLock()
# Cached chain head, together with the identity of the file it came from:
# (path, (mtime_ns, size), head). Caching the hash alone would be wrong the
# moment LEDGER is repointed (the test suite does exactly that) or the file is
# replaced underneath us — the next entry would then claim a predecessor from
# a different chain and verify() would report a break that never happened.
_LAST = [None]


def _digest(entry):
    """Hash of an entry's canonical form, excluding its own hash field."""
    payload = {k: entry[k] for k in sorted(entry) if k != "hash"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _anchor():
    """Chain head inherited from a rotated-out file, or GENESIS."""
    try:
        v = ANCHOR.read_text().strip()
        return v if len(v) == 64 else GENESIS
    except OSError:
        return GENESIS


def _sig():
    try:
        st = LEDGER.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _last_hash():
    """The hash of the newest entry — O(1) after the first call.

    This used to re-read the entire ledger line by line on EVERY command, so
    the cost of recording a command grew with the number of commands ever
    recorded. On a machine that has been used for a while that is a full file
    scan before every single run. The head is cached in memory after the first
    read, and a cold read seeks to the tail instead of walking from the start.
    """
    with _LOCK:
        cached = _LAST[0]
        sig = _sig()
        if cached and cached[0] == str(LEDGER) and cached[1] == sig:
            return cached[2]
        head = _anchor()
        if LEDGER.exists():
            try:
                size = LEDGER.stat().st_size
                with LEDGER.open("rb") as fh:
                    if size > _TAIL_BYTES:
                        fh.seek(-_TAIL_BYTES, os.SEEK_END)
                        fh.readline()          # discard the partial first line
                    tail = fh.read().decode("utf-8", "replace")
                for line in reversed(tail.splitlines()):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        head = json.loads(line).get("hash", head)
                        break
                    except Exception:
                        continue
            except OSError:
                pass
        _LAST[0] = (str(LEDGER), sig, head)
        return head


def _rotate():
    """Roll the ledger over once it gets large, WITHOUT breaking the chain.

    An append-only file that is never rotated is a file that eventually eats
    the disk. Rotating naively would restart the chain at GENESIS and silently
    discard the link between the two halves — so the outgoing file's final hash
    is written to an anchor, and the first entry of the new file points at it.
    verify() checks against the anchor, so a rotation is provably not a gap.
    """
    try:
        if not LEDGER.exists() or LEDGER.stat().st_size < MAX_LEDGER_BYTES:
            return
        head = _last_hash()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        LEDGER.rename(LEDGER.with_name(f"{LEDGER.name}.{stamp}"))
        fd = os.open(str(ANCHOR), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(head)
        _LAST[0] = (str(LEDGER), _sig(), head)
    except OSError:
        pass


def record(command, rc, output, kind="shell", chat_id=None, started=None):
    """Append one command to the ledger. Returns the entry's hash, or None.

    Never raises: a ledger failure must not take down a run that already
    happened. The command's own result is the thing that matters.
    """
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _rotate()
        out = (output or "")
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "started": started or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "kind": kind,
            "chat": chat_id or "",
            "command": (command or "")[:2000],
            "rc": rc,
            # the full output is hashed, only a head is stored — so a truncated
            # record can still be checked against the real output if kept
            "output_sha256": hashlib.sha256(out.encode("utf-8", "replace")).hexdigest(),
            "output_bytes": len(out.encode("utf-8", "replace")),
            "output_head": out[:MAX_OUTPUT_KEPT],
            "prev": _last_hash(),
        }
        entry["hash"] = _digest(entry)
        # Under the lock: two commands finishing together would otherwise read
        # the same `prev` and write two entries claiming the same predecessor —
        # a chain that verify() correctly reports as broken, caused by nothing
        # more sinister than concurrency.
        with _LOCK:
            fd = os.open(str(LEDGER), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            _LAST[0] = (str(LEDGER), _sig(), entry["hash"])
        try:
            os.chmod(LEDGER, 0o600)
        except OSError:
            pass
        return entry["hash"]
    except Exception:
        return None


def read_all(limit=None):
    """Every entry, oldest first. Malformed lines are skipped, not fatal."""
    out = []
    if not LEDGER.exists():
        return out
    try:
        with LEDGER.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except OSError:
        return out
    return out[-limit:] if limit else out


def verify():
    """Check the chain. Returns (ok, count, first_bad_index_or_None, message)."""
    entries = read_all()
    if not entries:
        return True, 0, None, "ledger is empty"
    prev = _anchor()
    for i, e in enumerate(entries):
        if e.get("prev") != prev:
            return False, len(entries), i, (
                f"entry {i} claims prev={str(e.get('prev'))[:12]}\u2026 but the "
                f"previous entry hashes to {prev[:12]}\u2026 \u2014 an entry was "
                "edited or removed at or before here")
        if _digest(e) != e.get("hash"):
            return False, len(entries), i, (
                f"entry {i} has been modified: its contents no longer match its "
                "own hash")
        prev = e["hash"]
    return True, len(entries), None, f"chain intact across {len(entries)} entries"


def summary(limit=15):
    """Human-readable tail, for a ```ledger``` request."""
    entries = read_all(limit)
    if not entries:
        return "The ledger is empty \u2014 no commands have been run yet."
    ok, count, bad, msg = verify()
    lines = [f"Evidence ledger \u2014 {count} entries, {'VERIFIED' if ok else 'BROKEN'}: {msg}",
             f"({LEDGER})", ""]
    for e in entries:
        mark = "\u2713" if e.get("rc") == 0 else f"\u2717 rc={e.get('rc')}"
        lines.append(f"{e.get('ts', '?')}  {mark}  {e.get('command', '')[:90]}")
    return "\n".join(lines)
