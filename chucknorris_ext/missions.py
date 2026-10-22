"""missions.py — big goals, run as a graph of agents. See docs/OS.md (phase 23).

A mission is a goal too big for one agent ("migrate the API to async",
"set up CI and make every test pass"). A planner turns it into a small graph
of tasks with dependencies. The app's orchestrator then runs it like `make`:
every task whose dependencies are finished starts as its own agent (several
at once), a code task's work is VERIFIED by running the project's tests on
its copy before it is merged, a task whose tests fail is sent back to fix
them, and later tasks start from a folder that already has the earlier
tasks' work in it. The whole mission is one process group in the kernel with
one budget: /kill on the mission ends every agent in it.

Pure: no GTK, no model, no processes. The app plans, launches and verifies;
this module parses plans, keeps the graph honest and decides what's next.
"""
import json
import os
import re
import time
from pathlib import Path

KINDS = ("code", "research", "check")
MAX_TASKS = 12
# task states
PENDING, RUNNING, VERIFYING, MERGED, DONE, FAILED, SKIPPED, CONFLICT = (
    "pending", "running", "verifying", "merged", "done", "failed", "skipped", "conflict")
FINISHED = (MERGED, DONE, SKIPPED)
# mission states
PLANNING, AWAITING, ACTIVE, PAUSED, COMPLETE, CANCELLED = (
    "planning", "awaiting approval", "running", "paused", "complete", "cancelled")

PLAN_PROMPT = (
    "You plan missions for a team of AI coding agents working on the user's project. Break "
    "the goal into a SMALL graph of tasks (2 to {max} tasks; fewer is better). Each task is "
    "done by one agent working alone in its own copy of the project, so a task must be "
    "self-contained and concrete: which files or areas, what to change, how to know it's done. "
    "Kinds: \"code\" (changes files; its work is tested and merged before dependent tasks "
    "start), \"research\" (reads docs or the web, changes nothing), \"check\" (reviews or "
    "audits, changes nothing). Use deps for real ordering only: tasks without a dependency "
    "between them run AT THE SAME TIME, so two code tasks that touch the same files must "
    "not run in parallel (make one depend on the other).\n"
    "Reply with ONLY a JSON object, no prose:\n"
    '{{"tasks": [{{"id": "t1", "title": "<short>", "kind": "code|research|check", '
    '"deps": [], "brief": "<what to do, concretely>"}}], "notes": "<one line: risks or '
    'assumptions>"}}')


class MissionError(ValueError):
    pass


def _json_in(text):
    """The first JSON object in a reply (models wrap it in prose or fences)."""
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    start = text.find("{")
    if start < 0:
        raise MissionError("the plan isn't JSON")
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if esc:
                esc = False                     # this character is escaped
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except ValueError as ex:
                    raise MissionError(f"the plan isn't valid JSON ({ex})") from None
    raise MissionError("the plan's JSON is cut off")


def parse_plan(text, max_tasks=MAX_TASKS):
    """A planner's reply -> (tasks, notes). Every task gets a unique id, real
    dependencies and a kind; a cycle or an unknown dependency is refused."""
    data = _json_in(text)
    raw = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        raise MissionError("the plan has no tasks")
    if len(raw) > max_tasks:
        raise MissionError(f"the plan has {len(raw)} tasks; at most {max_tasks}")
    tasks, seen = [], set()
    for i, t in enumerate(raw, 1):
        if not isinstance(t, dict):
            raise MissionError(f"task {i} isn't an object")
        tid = re.sub(r"[^A-Za-z0-9_-]", "", str(t.get("id") or f"t{i}"))[:20] or f"t{i}"
        if tid in seen:
            raise MissionError(f"two tasks are called {tid}")
        seen.add(tid)
        brief = str(t.get("brief") or "").strip()
        title = str(t.get("title") or brief.split("\n")[0]).strip()[:80]
        if not brief and not title:
            raise MissionError(f"task {tid} says nothing")
        kind = str(t.get("kind") or "code").strip().lower()
        kind = kind if kind in KINDS else "code"
        deps = t.get("deps") or []
        deps = [str(d) for d in deps] if isinstance(deps, list) else [str(deps)]
        deps = [re.sub(r"[^A-Za-z0-9_-]", "", d)[:20] for d in deps]   # cleaned like ids
        tasks.append({"id": tid, "title": title or tid, "brief": brief or title, "kind": kind,
                      "deps": deps, "state": PENDING, "attempts": 0, "pid": None,
                      "report": "", "verify": "", "note": ""})
    ids = {t["id"] for t in tasks}
    for t in tasks:
        bad = [d for d in t["deps"] if d not in ids or d == t["id"]]
        if bad:
            raise MissionError(f"task {t['id']} depends on {', '.join(bad)}, which isn't a task")
    order(tasks)                                    # raises on a cycle
    notes = str(data.get("notes") or "").strip()[:400] if isinstance(data, dict) else ""
    return tasks, notes


def order(tasks):
    """The tasks in an order that respects every dependency (Kahn)."""
    deps = {t["id"]: set(t["deps"]) for t in tasks}
    out, ready = [], [t["id"] for t in tasks if not deps[t["id"]]]
    while ready:
        n = ready.pop(0)
        out.append(n)
        for t in tasks:
            if n in deps[t["id"]]:
                deps[t["id"]].discard(n)
                if not deps[t["id"]] and t["id"] not in out and t["id"] not in ready:
                    ready.append(t["id"])
    if len(out) != len(tasks):
        stuck = [t["id"] for t in tasks if t["id"] not in out]
        raise MissionError(f"the plan has a dependency cycle ({', '.join(stuck)})")
    return out


def task(m, tid):
    return next((t for t in m["tasks"] if t["id"] == tid), None)


def ready(m, running_limit=3):
    """Tasks that may start now: pending, every dependency finished, within
    the parallel limit. Two code tasks never start together when the plan
    would let them touch the same files is the planner's job; the limit
    keeps the money flowing at a pace you can watch."""
    if m["state"] != ACTIVE:
        return []
    busy = sum(1 for t in m["tasks"] if t["state"] in (RUNNING, VERIFYING))
    room = max(0, running_limit - busy)
    out = []
    for t in m["tasks"]:
        if len(out) >= room:
            break
        if t["state"] == PENDING and all(task(m, d)["state"] in FINISHED for d in t["deps"]):
            out.append(t)
    return out


def blocked(m):
    """A failed or conflicting task stops everything that depends on it."""
    return [t for t in m["tasks"] if t["state"] in (FAILED, CONFLICT)]


def is_complete(m):
    return all(t["state"] in FINISHED for t in m["tasks"])


def context_for(m, t, limit=6000):
    """What a task's agent is told about the mission and the work before it."""
    done = [x for x in m["tasks"] if x["id"] in t["deps"] or
            (x["state"] in FINISHED and x["kind"] != "code" and x["report"])]
    lines = [f"You are one agent in a MISSION: {m['goal']}",
             f"Your task ({t['id']}, {t['kind']}): {t['title']}",
             "Do ONLY your task; other agents do the rest. Finish with a short REPORT."]
    if t["kind"] == "code":
        lines.append("Your work is checked by running the project's tests on your copy before "
                     "it is merged, so run them yourself and leave them passing.")
    if done:
        lines.append("\nWhat the tasks before you found or did:")
        for x in done:
            lines.append(f"- {x['id']} ({x['title']}): {x['report'][:limit // max(1, len(done))]}")
    return "\n".join(lines) + "\n\nTHE TASK:\n" + t["brief"]


def new_mission(goal, folder, tasks=None, notes=""):
    return {"id": f"m{int(time.time() * 1000)}", "goal": goal.strip(), "folder": str(folder or ""),
            "tasks": tasks or [], "notes": notes, "state": PLANNING, "created": time.time(),
            "pid": None, "spent_tokens": 0, "spent_usd": 0.0, "log": []}


def log(m, text):
    m["log"] = (m.get("log") or [])[-200:] + [(time.time(), text[:300])]


def progress(m):
    n = len(m["tasks"])
    done = sum(1 for t in m["tasks"] if t["state"] in FINISHED)
    return done, n


_TASK_STATES = (PENDING, RUNNING, VERIFYING, MERGED, DONE, FAILED, SKIPPED, CONFLICT)
_MISSION_STATES = (PLANNING, AWAITING, ACTIVE, PAUSED, COMPLETE, CANCELLED)


def normalize(m):
    """A mission read from disk, every field present and of the right type,
    or None when it can't be one (a damaged file must never stop Chuck from
    starting, nor take the other missions down with it)."""
    if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not re.fullmatch(r"m\d+", m["id"]):
        return None
    raw = m.get("tasks")
    if not isinstance(raw, list):
        return None

    def num(v, d, kind=float):
        return kind(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else d
    tasks, seen = [], set()
    for t in raw:
        if not isinstance(t, dict):
            continue
        tid = re.sub(r"[^A-Za-z0-9_-]", "", str(t.get("id") or ""))[:20]
        if not tid or tid in seen:
            continue
        seen.add(tid)
        title = str(t.get("title") or t.get("brief") or tid)[:80]
        deps = t.get("deps") if isinstance(t.get("deps"), list) else []
        pid = t.get("pid")
        tasks.append({**t, "id": tid, "title": title, "brief": str(t.get("brief") or title),
                      "kind": t.get("kind") if t.get("kind") in KINDS else "code",
                      "deps": [str(d) for d in deps], "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
                      "state": t.get("state") if t.get("state") in _TASK_STATES else PENDING,
                      "attempts": num(t.get("attempts"), 0, int),
                      "report": str(t.get("report") or ""), "verify": str(t.get("verify") or ""),
                      "note": str(t.get("note") or "")})
    for t in tasks:                                  # a dependency on a task that's gone
        t["deps"] = [d for d in t["deps"] if d in seen and d != t["id"]]
    try:
        order(tasks)
    except MissionError:
        for t in tasks:
            t["deps"] = []                            # a cycle can't be resumed as is
    state = m.get("state") if m.get("state") in _MISSION_STATES else CANCELLED
    log_ = [e for e in m.get("log") or [] if isinstance(e, (list, tuple)) and len(e) == 2] \
        if isinstance(m.get("log"), list) else []
    return {**m, "goal": str(m.get("goal") or "(no goal)"), "folder": str(m.get("folder") or ""),
            "tasks": tasks, "notes": str(m.get("notes") or ""), "state": state,
            "created": num(m.get("created"), time.time()), "pid": None,
            "spent_tokens": num(m.get("spent_tokens"), 0, int), "spent_usd": num(m.get("spent_usd"), 0.0),
            "spent_seconds": num(m.get("spent_seconds"), 0.0),
            "log": log_}


class Store:
    """Missions on disk, one JSON file each: a mission outlives the window."""

    def __init__(self, path):
        self.dir = Path(path)

    def save(self, m):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            f = self.dir / f"{m['id']}.json"
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(m))
            os.replace(tmp, f)
            return True
        except (OSError, TypeError, KeyError):
            return False

    def load(self):
        out = []
        try:
            files = sorted(self.dir.glob("m*.json"))
        except OSError:
            return out
        for f in files:
            try:
                m = normalize(json.loads(f.read_text()))
            except (OSError, ValueError):
                continue
            if m is not None:
                out.append(m)
        return out

    def remove(self, mid):
        if re.fullmatch(r"m\d+", str(mid)):
            try:
                (self.dir / f"{mid}.json").unlink()
            except OSError:
                pass
