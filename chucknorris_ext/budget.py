"""budget.py — what the whole OS may spend: a daily and a monthly limit, across
every chat, agent, program, daemon and mission. See docs/OS.md (phase 24).

A per-chat cap stops one conversation. It doesn't stop twenty background
agents, a mission and three daemons each spending a little. This ledger
books every model call's real cost by day, on disk, so the limits hold
across chats and restarts. Over the limit, the chat pauses and asks;
unattended agents are refused their next call and report why.

Pure: no GTK. Thread-safe (agents' calls are booked from many threads).
"""
import json
import os
import threading
import time
from pathlib import Path


def _day(now=None):
    return time.strftime("%Y-%m-%d", time.localtime(now if now is not None else time.time()))


class Ledger:
    """Spend per day, in dollars, kept for a year."""

    def __init__(self, path, keep_days=400):
        self.path, self.keep = Path(path), keep_days
        self.lock = threading.Lock()
        self.days, self.extra = {}, {}
        self._dirty, self._saved = False, 0.0
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            for k, src in (("days", data.get("days")), ("extra", data.get("extra"))):
                if isinstance(src, dict):
                    clean = {d: float(v) for d, v in src.items()
                             if isinstance(d, str) and isinstance(v, (int, float)) and v >= 0}
                    setattr(self, k, clean)

    def add(self, usd, now=None):
        if not usd or usd <= 0:
            return
        with self.lock:
            d = _day(now)
            self.days[d] = self.days.get(d, 0.0) + float(usd)
            self._dirty = True
        if time.time() - self._saved > 2:          # at most every 2 s; save() on close
            self.save()

    def today(self, now=None):
        return self.days.get(_day(now), 0.0)

    def month(self, now=None):
        m = _day(now)[:7]
        return sum(v for d, v in self.days.items() if d.startswith(m))

    def history(self, n=14, now=None):
        """[(day, usd)] for the last n days, oldest first."""
        t = now if now is not None else time.time()
        return [(_day(t - i * 86400), self.days.get(_day(t - i * 86400), 0.0))
                for i in range(n - 1, -1, -1)]

    def allow_more(self, scope, usd, now=None):
        """The user said 'go on': this much more for today (or this month)."""
        key = _day(now) if scope == "day" else _day(now)[:7]
        with self.lock:
            self.extra[key] = self.extra.get(key, 0.0) + float(usd)
            self._dirty = True
        self.save()

    def over(self, day_limit, month_limit, now=None):
        """-> ("day"|"month", limit incl. allowances, spent) when a limit is
        reached, else None. A limit of 0 means none."""
        d = _day(now)
        if day_limit and day_limit > 0:
            lim = day_limit + self.extra.get(d, 0.0)
            if self.today(now) >= lim:
                return ("day", lim, self.today(now))
        if month_limit and month_limit > 0:
            lim = month_limit + self.extra.get(d[:7], 0.0)
            if self.month(now) >= lim:
                return ("month", lim, self.month(now))
        return None

    def save(self):
        with self.lock:
            if not self._dirty:
                return
            cutoff = _day(time.time() - self.keep * 86400)
            self.days = {d: v for d, v in self.days.items() if d >= cutoff}
            self.extra = {d: v for d, v in self.extra.items() if d >= cutoff[:7]}
            data = {"days": self.days, "extra": self.extra}
            self._dirty, self._saved = False, time.time()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.path)
        except OSError:
            pass
