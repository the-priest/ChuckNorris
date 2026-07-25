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

from chucknorris_ext import config

LEDGER = config.DATA_DIR / "ledger.jsonl"
GENESIS = "0" * 64
MAX_OUTPUT_KEPT = 4000


def _digest(entry):
    """Hash of an entry's canonical form, excluding its own hash field."""
    payload = {k: entry[k] for k in sorted(entry) if k != "hash"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _last_hash():
    if not LEDGER.exists():
        return GENESIS
    last = None
    try:
        with LEDGER.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last = line
    except OSError:
        return GENESIS
    if not last:
        return GENESIS
    try:
        return json.loads(last).get("hash", GENESIS)
    except Exception:
        return GENESIS


def record(command, rc, output, kind="shell", chat_id=None, started=None):
    """Append one command to the ledger. Returns the entry's hash, or None.

    Never raises: a ledger failure must not take down a run that already
    happened. The command's own result is the thing that matters.
    """
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
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
        fd = os.open(str(LEDGER), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
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
    prev = GENESIS
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
