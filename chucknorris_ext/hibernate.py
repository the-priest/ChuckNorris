"""hibernate.py — processes that survive a crash, and time travel. See
docs/OS.md (phase 21).

After every step, a background agent's or a program's whole state is written
to disk: its conversation, the step it is on, and (for a writer) a snapshot
of its copy of the folder. If Chuck closes, crashes or the machine reboots,
the next start offers to resume it exactly where it was.

The same record is the journal of the run, step by step, so a run can be
REPLAYED (what did it see, what did it do, at step 7?) and FORKED: start a
new process from any step, with the files as they were at that step and,
if you like, a new instruction ("at step 7 you went for a rewrite; patch it
instead").

Pure apart from the files it keeps: the app supplies the snapshots.
"""
import json
import os
import re
import time
from pathlib import Path

FORK_NOTE = ("THE USER FORKED YOU AT THIS POINT: you are a new copy of the agent, starting "
             "again from here. Their instruction: {instruction}")


def new_key(pid):
    return f"{int(time.time() * 1000)}-{pid}"


class Store:
    """One JSON file per hibernated process."""

    def __init__(self, path):
        self.dir = Path(path)

    def _file(self, key):
        if not re.fullmatch(r"[0-9]+-[0-9]+", str(key)):
            raise ValueError(f"bad process key {key!r}")
        return self.dir / f"{key}.json"

    def save(self, rec):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            f = self._file(rec["key"])
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(rec))
            os.replace(tmp, f)
            return True
        except (OSError, ValueError, TypeError):
            return False

    def load(self):
        """Every record, oldest first; unreadable files are skipped."""
        out = []
        try:
            files = list(self.dir.glob("*.json"))
        except OSError:
            return out
        for f in files:
            try:
                rec = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and rec.get("key") and isinstance(rec.get("msgs"), list):
                out.append(rec)
        return sorted(out, key=lambda r: r.get("time", 0))

    def remove(self, key):
        try:
            self._file(key).unlink()
        except (OSError, ValueError):
            pass

    def prune(self, days=14):
        cutoff = time.time() - days * 86400
        for rec in self.load():
            if rec.get("time", 0) < cutoff:
                self.remove(rec["key"])


def steps_of(msgs):
    """The run as steps: [(n, what the agent wrote, what it got back)].
    msgs is [system, brief, reply1, results1, reply2, results2, ...]."""
    out, n = [], 0
    for i in range(2, len(msgs) - 1, 2):
        a, r = msgs[i], msgs[i + 1]
        if a.get("role") != "assistant" or r.get("role") != "user":
            break
        n += 1
        out.append((n, a.get("content") or "", r.get("content") or ""))
    return out


def fork_messages(msgs, step, instruction=""):
    """The conversation as it was after `step` steps, with the user's new
    instruction where the copy picks up."""
    step = max(0, min(int(step), len(steps_of(msgs))))
    out = [dict(m) for m in msgs[:2 + 2 * step]]
    last = out[-1]
    last["content"] = (last.get("content") or "").replace(
        "\n\nThis was your last step: write the REPORT now.", "")
    if instruction.strip():
        last["content"] += "\n\n" + FORK_NOTE.format(instruction=instruction.strip())
    return out


def tool_labels(reply):
    """'read parser.py, bash pytest -q' for a step's reply (for the replay)."""
    labels = []
    for m in re.finditer(r"```(\w+)[ \t]*([^\n`]*)\n?([^`]*)```", reply or ""):
        tag, arg = m.group(1), (m.group(2).strip() or m.group(3).strip().split("\n")[0])
        if tag in ("read", "grep", "glob", "map", "bash", "sh", "write", "edit", "search", "fetch"):
            labels.append(f"{tag} {arg[:50]}".strip())
    return ", ".join(labels[:6]) + (" …" if len(labels) > 6 else "")
