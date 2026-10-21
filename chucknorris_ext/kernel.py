"""kernel.py — the agent kernel: processes, a token scheduler, quotas,
signals and IPC. See docs/OS.md.

Tokens are the new CPU time. Every agent is a PROCESS: it has a parent, a
priority, a quota of tokens, dollars and seconds, and everything it spends
is charged to it and to every ancestor. Before each model call a process
asks the SCHEDULER for a slot; slots go to the most important process that
has waited longest, never to a stopped one, and never past a quota: there
the process is sent TERM (it gets exactly one more call, to write its
report) and then KILL.

Nothing here knows about GTK, the network or the model. The app supplies the
calls; the kernel decides who may make them, books what they cost and keeps
the journal that /top and replay read.
"""
import itertools
import threading
import time
from collections import deque

# states
READY, RUNNING, WAITING, STOPPED, DONE, FAILED, KILLED = (
    "ready", "running", "waiting", "stopped", "done", "failed", "killed")
LIVE = (READY, RUNNING, WAITING, STOPPED)
# signals
STOP, CONT, TERM, KILL, INFO = "STOP", "CONT", "TERM", "KILL", "INFO"


class ProcessKilled(Exception):
    """Raised in a process's thread when it may not continue (KILL, or it ran
    past its quota after TERM)."""


class Process:
    def __init__(self, pid, ppid, name, kind, brief, program, nice, quota, now):
        self.pid, self.ppid = pid, ppid
        self.name, self.kind, self.brief, self.program = name, kind, brief, program
        self.nice = max(-10, min(10, int(nice)))
        self.quota = dict(quota or {})          # tokens / usd / seconds; absent = no limit
        self.state = READY
        self.started, self.ended = now, None
        # own spend, and spend including every descendant
        self.own = {"prompt": 0, "completion": 0, "usd": 0.0, "calls": 0}
        self.total = {"prompt": 0, "completion": 0, "usd": 0.0, "calls": 0}
        self.children = []
        self.activity = ""
        self.exit_status, self.result = None, None
        self.mailbox = deque()
        self.events = deque(maxlen=500)
        self.terminating = False                # got TERM: finish up
        self.final_granted = False              # its one last call after TERM was granted
        self.term_reason = ""
        self.recent = deque(maxlen=64)          # (time, tokens) for the live rate
        self.done_event = threading.Event()
        self._stop_pending = False              # STOP arrived mid-call: park when it ends
        self._since = now                       # when it joined the slot queue

    def tokens(self):
        return self.total["prompt"] + self.total["completion"]

    def alive(self):
        return self.state in LIVE

    def info(self, now):
        """What the process is doing, for /top and the INFO signal. Free:
        it never calls a model."""
        cutoff = now - 10
        rate = sum(n for t, n in self.recent if t >= cutoff) / 10.0
        return {"pid": self.pid, "ppid": self.ppid, "name": self.name, "kind": self.kind,
                "state": self.state, "nice": self.nice, "activity": self.activity,
                "tokens": self.tokens(), "usd": round(self.total["usd"], 6),
                "calls": self.total["calls"], "rate": rate,
                "seconds": round((self.ended or now) - self.started, 1),
                "quota": dict(self.quota), "exit": self.exit_status,
                "terminating": self.terminating, "children": list(self.children),
                "brief": self.brief[:200]}


class Kernel:
    def __init__(self, slots=4, clock=time.monotonic, on_change=None):
        self.slots = max(1, int(slots))
        self.clock = clock
        self.on_change = on_change              # called (no args) when the table changes
        self.procs = {}
        self._pids = itertools.count(1)
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._queue = []                        # [seq, pid] waiting for a slot
        self._seq = itertools.count()
        self._running = set()                   # pids holding a slot
        self.board = {}                         # blackboard: key -> (value, by, time)
        self.journal = deque(maxlen=5000)       # (time, pid, event, detail)

    # ── lifecycle ──
    def spawn(self, name, kind="agent", brief="", parent=None, program="", nice=0,
              quota=None):
        """A new process. Its quota is clipped to what its parent has left:
        a child can never spend more than its parent was allowed."""
        with self._lock:
            pid = next(self._pids)
            par = self.procs.get(parent) if parent else None
            q = dict(quota or {})
            if par is not None:
                for k in ("tokens", "usd", "seconds"):
                    left = self._left(par, k)
                    if left is not None:
                        q[k] = left if q.get(k) is None else min(q[k], left)
            p = Process(pid, par.pid if par else 0, name, kind, brief, program, nice, q,
                        self.clock())
            self.procs[pid] = p
            if par is not None:
                par.children.append(pid)
            self._log(pid, "spawn", f"{kind} {name}" + (f" under {par.pid}" if par else ""))
        self._changed()
        return p

    def exit(self, pid, status=0, result=None):
        """A process finished (status 0 = success). Releases its slot."""
        with self._lock:
            p = self.procs.get(pid)
            if p is None or not p.alive():
                return
            p.state = DONE if status == 0 else FAILED
            p.exit_status, p.result = status, result
            p.ended = self.clock()
            p.activity = ""
            self._running.discard(pid)
            self._queue = [e for e in self._queue if e[1] != pid]
            self._log(pid, "exit", f"status {status}")
            p.done_event.set()
            self._cv.notify_all()
        self._changed()

    def wait(self, pid, timeout=None):
        """Block until a process ends -> (exit status, result), or None on timeout."""
        p = self.procs.get(pid)
        if p is None:
            return None
        if not p.done_event.wait(timeout):
            return None
        return p.exit_status, p.result

    def reap(self, keep=200):
        """Forget the oldest finished processes beyond `keep`."""
        with self._lock:
            dead = sorted((p for p in self.procs.values() if not p.alive() and p.pid != 1),
                          key=lambda p: p.ended or 0)
            for p in dead[:max(0, len(dead) - keep)]:
                self.procs.pop(p.pid, None)

    # ── the scheduler ──
    def acquire(self, pid, timeout=None):
        """Wait for a model-call slot. Returns True when granted; False on
        timeout. Raises ProcessKilled when the process may not go on. A
        process past its quota is sent TERM and gets ONE more slot, to write
        its report; after that it is killed."""
        end = None if timeout is None else self.clock() + timeout
        with self._cv:
            p = self.procs.get(pid)
            if p is None or not p.alive():
                raise ProcessKilled(f"process {pid} is not running")
            why = self._over(p)
            if why:
                if not p.terminating:
                    self._term(p, why)
                elif p.final_granted:
                    self._kill(p, why)
                    raise ProcessKilled(why)
            entry = [next(self._seq), pid]
            self._queue.append(entry)
            p._since = self.clock()
            if p.state != STOPPED:
                p.state = WAITING
            self._changed_locked()
            try:
                while True:
                    if not p.alive():
                        raise ProcessKilled(p.term_reason or "killed")
                    if (p.state != STOPPED and len(self._running) < self.slots
                            and self._next() == pid):
                        break
                    left = None if end is None else end - self.clock()
                    if left is not None and left <= 0:
                        return False
                    self._cv.wait(0.25 if left is None else min(0.25, left))
            finally:
                if entry in self._queue:
                    self._queue.remove(entry)
            self._running.add(pid)
            p.state = RUNNING
            if p.terminating:
                p.final_granted = True
            self._log(pid, "slot", f"{len(self._running)}/{self.slots}")
        self._changed()
        return True

    def release(self, pid):
        with self._cv:
            self._running.discard(pid)
            p = self.procs.get(pid)
            if p is not None and p.state == RUNNING:
                p.state = STOPPED if p._stop_pending else READY
            self._cv.notify_all()
        self._changed()

    def _next(self):
        """Which waiting process gets the next free slot: the lowest nice,
        aged by how long it has waited (a nice-10 process still runs
        eventually), then first come first served."""
        now = self.clock()
        best, key = None, None
        for seq, pid in self._queue:
            p = self.procs.get(pid)
            if p is None or p.state == STOPPED or not p.alive():
                continue
            waited = now - getattr(p, "_since", now)
            k = (p.nice - waited / 30.0, seq)
            if key is None or k < key:
                best, key = pid, k
        return best

    # ── accounting ──
    def charge(self, pid, prompt=0, completion=0, usd=0.0):
        """Book one model call to the process and every ancestor."""
        now = self.clock()
        with self._lock:
            p = self.procs.get(pid)
            if p is None:
                return
            p.own["prompt"] += int(prompt)
            p.own["completion"] += int(completion)
            p.own["usd"] += float(usd or 0)
            p.own["calls"] += 1
            p.recent.append((now, int(prompt) + int(completion)))
            q = p
            while q is not None:
                q.total["prompt"] += int(prompt)
                q.total["completion"] += int(completion)
                q.total["usd"] += float(usd or 0)
                q.total["calls"] += 1
                if q is not p:
                    q.recent.append((now, int(prompt) + int(completion)))
                q = self.procs.get(q.ppid)
            self._log(pid, "charge", f"{int(prompt)}+{int(completion)} tok ${float(usd or 0):.5f}")
        self._changed()

    def _left(self, p, k):
        lim = p.quota.get(k)
        if lim is None:
            return None
        used = (p.tokens() if k == "tokens" else p.total["usd"] if k == "usd"
                else self.clock() - p.started)
        return max(0, lim - used)

    def _over(self, p):
        for k, label in (("tokens", "token"), ("usd", "spending"), ("seconds", "time")):
            left = self._left(p, k)
            if left is not None and left <= 0:
                return f"{label} quota reached"
        return ""

    def over_quota(self, pid):
        with self._lock:
            p = self.procs.get(pid)
            return self._over(p) if p else ""

    # ── signals ──
    def signal(self, pid, sig):
        """STOP / CONT / TERM / KILL (to the process and its descendants), or
        INFO (returns what it is doing; free)."""
        with self._cv:
            p = self.procs.get(pid)
            if p is None:
                return None
            if sig == INFO:
                return p.info(self.clock())
            for q in self._tree(p):
                if not q.alive():
                    continue
                if sig == STOP:
                    if q.state != RUNNING:      # a running call finishes, then it parks
                        q.state = STOPPED
                    q._stop_pending = True
                    self._log(q.pid, "signal", "STOP")
                elif sig == CONT:
                    q._stop_pending = False
                    if q.state == STOPPED:
                        q.state = WAITING if any(e[1] == q.pid for e in self._queue) else READY
                    self._log(q.pid, "signal", "CONT")
                elif sig == TERM:
                    self._term(q, "asked to finish")
                elif sig == KILL:
                    self._kill(q, "killed")
            self._cv.notify_all()
        self._changed()
        return True

    def should_stop(self, pid):
        """True once the process must stop working (killed, or ended)."""
        p = self.procs.get(pid)
        return p is None or not p.alive()

    def terminating(self, pid):
        """True once the process was asked to finish: its next model call is
        its last, and should produce its report."""
        p = self.procs.get(pid)
        return bool(p and p.terminating)

    def _term(self, p, why):
        if not p.terminating:
            p.terminating = True
            p.term_reason = why
            self._log(p.pid, "signal", f"TERM ({why})")

    def _kill(self, p, why):
        if not p.alive():
            return
        p.state = KILLED
        p.term_reason = why
        p.exit_status = -9
        p.ended = self.clock()
        p.activity = ""
        self._running.discard(p.pid)
        self._log(p.pid, "signal", f"KILL ({why})")
        p.done_event.set()

    def _tree(self, p):
        out, todo = [], [p]
        while todo:
            q = todo.pop()
            out.append(q)
            todo.extend(self.procs[c] for c in q.children if c in self.procs)
        return out

    def renice(self, pid, nice):
        with self._lock:
            p = self.procs.get(pid)
            if p is None:
                return False
            p.nice = max(-10, min(10, int(nice)))
            self._log(pid, "renice", str(p.nice))
        self._changed()
        return True

    # ── what it's doing ──
    def note(self, pid, activity):
        p = self.procs.get(pid)
        if p is not None:
            p.activity = (activity or "")[:160]
            if activity:
                self._log(pid, "do", p.activity)
            self._changed()

    # ── IPC ──
    def send(self, pid, msg, sender=0):
        with self._cv:
            p = self.procs.get(pid)
            if p is None or not p.alive():
                return False
            p.mailbox.append((sender, msg, self.clock()))
            self._log(pid, "mail", f"from {sender}")
            self._cv.notify_all()
        return True

    def recv(self, pid, timeout=0):
        """The oldest message (sender, text, time), or None."""
        end = self.clock() + (timeout or 0)
        with self._cv:
            p = self.procs.get(pid)
            while p is not None and not p.mailbox:
                left = end - self.clock()
                if left <= 0:
                    return None
                self._cv.wait(min(0.25, left))
            return p.mailbox.popleft() if p and p.mailbox else None

    def post(self, key, value, by=0):
        """The blackboard: findings every process can read."""
        with self._lock:
            self.board[str(key)[:200]] = (value, by, time.time())
            self._log(by, "post", str(key)[:80])
        self._changed()

    def read(self, key):
        v = self.board.get(key)
        return v[0] if v else None

    # ── views ──
    def ps(self, all=False):
        now = self.clock()
        with self._lock:
            rows = [p.info(now) for p in self.procs.values() if all or p.alive()]
        return sorted(rows, key=lambda r: r["pid"])

    def events(self, pid=None, limit=100):
        with self._lock:
            ev = [e for e in self.journal if pid is None or e[1] == pid]
        return ev[-limit:]

    def _log(self, pid, ev, detail=""):
        self.journal.append((time.time(), pid, ev, detail))
        p = self.procs.get(pid)
        if p is not None:
            p.events.append((time.time(), ev, detail))

    def _changed_locked(self):
        pass                                    # the waiter notifies after leaving the lock

    def _changed(self):
        cb = self.on_change
        if cb is not None:
            try:
                cb()
            except Exception:
                pass
