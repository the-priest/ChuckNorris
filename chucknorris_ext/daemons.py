"""daemons.py — agents that sleep until something needs them. See docs/OS.md
(phase 19).

An agent loop that polls ("is the disk full yet?") burns tokens on nothing.
A daemon here costs NOTHING while it sleeps: its trigger is checked by plain
code — the clock, a file's mtime, `systemctl --failed`, the disk, a
command's output — and only when it fires is an agent woken, with a quota,
to look and report. Reports land in the inbox.

Triggers (parse_when understands what people write):
    every 30m / every 2h / every day           {"every": seconds}
    daily 08:30 / every day at 8:30            {"daily": "08:30"}
    when ~/proj/src changes                    {"watch": path}
    when a service fails                       {"unit_failed": True}
    when disk / is above 90%                   {"disk": "/", "above": 90}
    when `checkupdates` changes                {"command": "...", "changes": True}

Pure: the probes (mtime, failed units, disk, running a command) are passed
in, so every trigger is tested without a clock or a system.
"""
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

DAEMON_PROMPT = (
    "You are a daemon of Chuck, an engineering agent: woken by a trigger while the user is "
    "away, you LOOK and REPORT. You change nothing: files, services and packages are "
    "read-only to you, and a command that would change something is refused.\n"
    "Tools: put one or several fenced blocks in a reply and you get the results back:\n"
    "```read <path>[:start-end]``` ```grep <regex> [path]``` ```glob <pattern>``` ```map```\n"
    "```bash\n<read-only command>\n``` (journalctl, systemctl status, df, git log, "
    "pacman -Q, …) ```search <query>``` ```fetch <url>```\n"
    "Find out what happened and why, briefly. Then reply with no tool block: your REPORT — "
    "the first line a one-line headline (\"nothing to do\" when there is nothing to do), "
    "then what you found, and the exact commands or changes you PROPOSE (the user will "
    "decide). Be concrete and brief.")

_UNITS = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600, "hour": 3600,
          "d": 86400, "day": 86400, "w": 604800, "week": 604800}


COMMAND_POLL = 300          # seconds between runs of a trigger's command


class DaemonError(ValueError):
    pass


def parse_when(text):
    """'every 2h' -> {"every": 7200} etc. Raises DaemonError with a hint."""
    t = (text or "").strip().lower()
    m = re.fullmatch(r"(?:every\s+day\s+at|daily(?:\s+at)?|each\s+day\s+at)\s+(\d{1,2})(?::(\d\d))?\s*(am|pm)?", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if m.group(3) == "pm" and h < 12:
            h += 12
        if m.group(3) == "am" and h == 12:
            h = 0
        if h > 23 or mi > 59:
            raise DaemonError("that time doesn't exist")
        return {"daily": f"{h:02d}:{mi:02d}"}
    m = re.fullmatch(r"every\s+(\d+)?\s*([a-z]+?)s?", t)
    if m and m.group(2) in _UNITS:
        n = int(m.group(1) or 1) * _UNITS[m.group(2)]
        if n < 60:
            raise DaemonError("the shortest interval is a minute")
        return {"every": n}
    m = re.fullmatch(r"when\s+(?:the\s+)?disk\s+(\S+)?\s*(?:is\s+)?(?:above|over|>)\s*(\d{1,3})\s*%?", t)
    if m:
        return {"disk": m.group(1) or "/", "above": max(1, min(99, int(m.group(2))))}
    if re.fullmatch(r"when\s+(?:a|any)\s+(?:service|unit)\s+fails", t):
        return {"unit_failed": True}
    m = re.fullmatch(r"when\s+`([^`]+)`\s+changes", (text or "").strip(), re.IGNORECASE)
    if m:
        return {"command": m.group(1).strip(), "changes": True}
    m = re.fullmatch(r"when\s+(.+?)\s+changes", (text or "").strip(), re.IGNORECASE)
    if m:
        return {"watch": os.path.expanduser(m.group(1).strip().strip("'\""))}
    raise DaemonError("say when: 'every 30m', 'daily 08:30', 'when ~/src changes', "
                      "'when a service fails', 'when disk / is above 90%' or "
                      "'when `cmd` changes'")


def describe(trig):
    if "every" in trig:
        n = trig["every"]
        for name, sec in (("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60)):
            if n % sec == 0:
                k = n // sec
                return f"every {name}" if k == 1 else f"every {k} {name}s"
        return f"every {n}s"
    if "daily" in trig:
        return f"daily at {trig['daily']}"
    if "watch" in trig:
        return f"when {trig['watch']} changes"
    if trig.get("unit_failed"):
        return "when a service fails"
    if "disk" in trig:
        return f"when disk {trig['disk']} is above {trig['above']}%"
    if "command" in trig:
        return f"when `{trig['command']}` changes"
    return "?"


# Without bubblewrap a daemon's command runs on the bare system, so only
# commands that are read-only by nature are let through.
_RO_CMDS = {
    "df", "du", "ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "wc", "stat",
    "file", "find", "uname", "uptime", "free", "ps", "journalctl", "systemctl", "pacman",
    "checkupdates", "git", "ip", "ss", "lsblk", "findmnt", "sensors", "date", "who", "w",
    "last", "id", "whoami", "hostname", "hostnamectl", "timedatectl", "sort", "uniq", "cut",
    "tr", "echo", "printf", "nproc", "lscpu", "lspci", "lsusb", "which", "test", "true",
    "column", "jq", "diff", "md5sum", "sha256sum", "tree", "readlink", "realpath", "basename",
    "dirname", "nvidia-smi", "paccache", "pactree", "expac", "loginctl", "busctl", "less",
    "zcat", "xargs", "env", "printenv", "locale", "lsmod", "dmesg", "inxi", "fastfetch"}
_SYSTEMCTL_RO = {"status", "list-units", "list-timers", "list-unit-files", "list-sockets",
                 "list-dependencies", "list-jobs", "list-machines", "is-active", "is-failed",
                 "is-enabled", "is-system-running", "show", "cat", "help", "get-default"}
_GIT_RO = {"status", "log", "diff", "show", "branch", "rev-parse", "ls-files", "remote",
           "describe", "blame", "shortlog", "tag", "rev-list", "cat-file", "ls-tree",
           "reflog", "config", "stash"}
_LOGIN_RO = {"list-sessions", "list-users", "list-seats", "show-session", "show-user",
             "session-status", "user-status", "seat-status"}
_BUS_RO = {"list", "status", "tree", "introspect", "get-property", "--help"}


def _verb(words):
    return next((w for w in words[1:] if not w.startswith("-")), "")


def read_only_problem(cmd, strict=False):
    """Why a daemon may NOT run this command ('' when it may). Every daemon
    command runs with the whole system read-only; what is checked here is
    what a read-only filesystem doesn't stop (the system bus: services,
    power) and, with `strict` (no sandbox at all), anything not read-only
    by nature."""
    c = (cmd or "").strip()
    if not c:
        return "an empty command"
    if strict:
        if re.search(r"`|\$\(|<\(", c):
            return "no command substitution without the sandbox"
        rest = re.sub(r"\d?>\s*/dev/null|2>&1|&>\s*/dev/null", "", c)
        if ">" in rest:
            return "no writing to files"
    for seg in re.split(r"\|\|?|&&|;|\n|&", c):
        words = seg.split()
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*", words[0]):
            words = words[1:]
        if not words:
            continue
        if words[0] in ("sudo", "doas", "pkexec", "su", "run0"):
            return "no root for a daemon"
        prog = words[0].rsplit("/", 1)[-1]
        if prog == "xargs" or prog == "env":
            inner = [w for w in words[1:] if not w.startswith("-") and "=" not in w]
            if inner:
                why = read_only_problem(" ".join(inner), strict)
                if why:
                    return why
            continue
        verb = _verb(words)
        if prog == "systemctl" and verb and verb not in _SYSTEMCTL_RO:
            return f"systemctl {verb} changes the system"
        if prog == "loginctl" and verb and verb not in _LOGIN_RO:
            return f"loginctl {verb} changes the system"
        if prog == "busctl" and verb and verb not in _BUS_RO:
            return f"busctl {verb} can change the system"
        if prog in ("dbus-send", "gdbus", "qdbus", "shutdown", "reboot", "poweroff", "halt",
                    "kill", "pkill", "killall", "rm", "dd", "mkfs", "chmod", "chown"):
            return f"{prog} isn't read-only"
        if prog == "journalctl" and re.search(r"--(?:vacuum|rotate|flush|relinquish|sync)", seg):
            return "journalctl maintenance changes the journal"
        if prog == "find" and re.search(r"-(?:delete|exec|execdir|ok|okdir|fprint\w*|fls)\b", seg):
            return "find may only list"
        if prog == "pacman" and not re.search(r"(?:^|\s)-(?:Q|S[silg]|F)\w*|--query", seg):
            return "pacman may only query"
        if prog == "git" and verb and verb not in _GIT_RO:
            return f"git {verb} changes the repository"
        if strict:
            if prog not in _RO_CMDS:
                return (f"{prog} isn't on the read-only list (install bubblewrap and a "
                        "daemon may run anything read-only)")
            if prog == "git" and verb in ("config", "stash", "tag", "branch", "remote") \
                    and len([w for w in words[2:] if not w.startswith("-")]) > 0:
                return f"git {verb} with arguments can change things"
            if prog == "dmesg" and re.search(r"\s-(?:c|C|-clear|-read-clear)\b", seg):
                return "dmesg may only read"
    return ""


def tree_signature(path, limit=20000):
    """A cheap fingerprint of a file or folder: newest mtime, file count, size."""
    p = Path(os.path.expanduser(path))
    if not p.exists():
        return "missing"
    if p.is_file():
        st = p.stat()
        return f"{st.st_mtime_ns}:{st.st_size}"
    newest, n, size = 0, 0, 0
    for dp, dns, fns in os.walk(p):
        dns[:] = [d for d in dns if not d.startswith(".") and d not in ("node_modules", "__pycache__",
                                                                         "target", ".venv")]
        for f in fns:
            try:
                st = os.stat(os.path.join(dp, f))
            except OSError:
                continue
            newest, n, size = max(newest, st.st_mtime_ns), n + 1, size + st.st_size
            if n >= limit:
                break
    return f"{newest}:{n}:{size}"


def check(spec, now, probe):
    """Is this daemon due? -> (due, why, new_state). Free: no model call.
    probe: object with mtime(path), failed_units(), disk_percent(path),
    run(cmd) -> output, and localtime(t)."""
    trig, st = spec["when"], dict(spec.get("state") or {})
    last = spec.get("last_run") or 0
    if "every" in trig:
        due = now - last >= trig["every"]
        return due, describe(trig), st
    if "daily" in trig:
        hh, mm = (int(x) for x in trig["daily"].split(":"))
        lt = probe.localtime(now)
        target = now - ((lt.tm_hour * 60 + lt.tm_min - (hh * 60 + mm)) * 60 + lt.tm_sec)
        due = now >= target and last < target
        return due, f"it's {trig['daily']}", st
    if "watch" in trig:
        sig = probe.mtime(trig["watch"])
        prev = st.get("sig")
        st["sig"] = sig
        return (prev is not None and sig != prev), f"{trig['watch']} changed", st
    if trig.get("unit_failed"):
        units = sorted(probe.failed_units())
        prev = st.get("units")
        st["units"] = units
        new = [u for u in units if u not in (prev or [])]
        return (prev is not None and bool(new)), "failed: " + ", ".join(new[:5]), st
    if "disk" in trig:
        pct = probe.disk_percent(trig["disk"])
        was = st.get("above", False)
        now_above = pct is not None and pct > trig["above"]
        st["above"] = now_above
        # edge-triggered, and again every 6 hours while it stays above
        due = now_above and (not was or now - last >= 6 * 3600)
        return due, f"disk {trig['disk']} is {pct:.0f}% full" if pct is not None else "", st
    if "command" in trig:
        # a command (checkupdates asks the mirrors) runs every `poll` seconds,
        # not at every 30-second tick
        if now - st.get("polled", 0) < trig.get("poll", COMMAND_POLL):
            return False, "", st
        st["polled"] = now
        out = probe.run(trig["command"])
        h = hashlib.sha1((out or "").encode("utf-8", "replace")).hexdigest()
        prev = st.get("hash")
        st["hash"] = h
        st["out"] = (out or "")[-2000:]
        return (prev is not None and h != prev), f"`{trig['command']}` output changed", st
    return False, "", st


def trigger_detail(spec):
    """What the trigger saw, for the woken agent (no need to look again)."""
    trig, st = spec["when"], spec.get("state") or {}
    if "command" in trig and st.get("out") is not None:
        return f"`{trig['command']}` now prints:\n{st['out']}"
    if trig.get("unit_failed") and st.get("units"):
        return "Failed units now: " + ", ".join(st["units"][:20])
    return ""


class Registry:
    """The daemons, persisted as JSON (survives restarts)."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.items = []
        self.load()

    def load(self):
        try:
            data = json.loads(self.path.read_text())
            self.items = [d for d in data if isinstance(d, dict) and d.get("when")]
        except (OSError, ValueError):
            self.items = []

    def save(self):
        with self.lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.items, indent=1))
                os.replace(tmp, self.path)
            except OSError:
                pass

    def add(self, name, when, brief, folder="", kind="report", quota=150000):
        trig = parse_when(when) if isinstance(when, str) else when
        nid = max([d["id"] for d in self.items] or [0]) + 1
        d = {"id": nid, "name": (name or brief.splitlines()[0])[:60], "when": trig,
             "brief": brief.strip(), "folder": str(folder or ""), "kind": kind,
             "quota": int(quota), "enabled": True, "last_run": 0, "runs": 0,
             "state": {}, "created": time.time()}
        self.items.append(d)
        self.save()
        return d

    def get(self, did):
        return next((d for d in self.items if d["id"] == int(did)), None)

    def remove(self, did):
        n = len(self.items)
        self.items = [d for d in self.items if d["id"] != int(did)]
        self.save()
        return len(self.items) < n


class Inbox:
    """Daemon reports, newest last, persisted."""

    def __init__(self, path, keep=200):
        self.path, self.keep = Path(path), keep
        try:
            self.items = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.items = []

    def add(self, daemon, why, report, pid=None, extra=None):
        nid = max([i["id"] for i in self.items] or [0]) + 1
        it = {"id": nid, "daemon": daemon, "why": why, "report": report, "pid": pid,
              "time": time.time(), "unread": True}
        if extra:
            it.update(extra)
        self.items = (self.items + [it])[-self.keep:]
        self.save()
        return it

    def unread(self):
        return [i for i in self.items if i.get("unread")]

    def mark_read(self):
        for i in self.items:
            i["unread"] = False
        self.save()

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.items, indent=1))
            os.replace(tmp, self.path)
        except OSError:
            pass
