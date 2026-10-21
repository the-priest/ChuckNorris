"""programs.py — agent programs: installable, like packages. See docs/OS.md
(phase 20).

An agent program is a Markdown file: a header that says what the program may
do, and a prompt that says how to do it.

    ---
    description: Reviews the uncommitted diff for bugs
    mode: report            # report: looks and reports · act: works in a copy you merge
    tools: read grep glob map bash
    model: fast             # chat · fast · or a model id
    tokens: 200000          # its quota
    minutes: 20
    ---
    You review code. Run `git diff` ...

`/run reviewer focus on the parser` runs it as a process. Its capabilities
come from its mode and tools, and are clipped to its parent's like any
child's: a report program never gets `write`, a program without `fetch`
never reaches the network, and every syscall it makes is checked.

Programs live in ~/.config/chucknorris/programs/ (yours, everywhere) and
<project>/.chuck/programs/ (the project's); a file with a built-in's name
replaces it.
"""
import re
from pathlib import Path

REPORT_TOOLS = ("read", "grep", "glob", "map", "bash", "search", "fetch")
ACT_TOOLS = REPORT_TOOLS + ("write", "edit")

REPORT_PROMPT = (
    "You are an agent program run by Chuck, an engineering agent, on the user's Linux "
    "desktop. You LOOK and REPORT: files and the system are read-only to you, and a command "
    "that would change something is refused.\n"
    "Tools: put one or several fenced blocks in a reply and you get the results back:\n"
    "{tools}\n"
    "When you are done, reply with no tool block: your REPORT — the first line a one-line "
    "headline, then the findings, concrete and brief, with exact file:line references and "
    "the commands or changes you propose.")

TOOL_DOC = {
    "read": "```read <path>[:start-end]```", "grep": "```grep <regex> [path]```",
    "glob": "```glob <pattern>```", "map": "```map``` (every file + what it defines)",
    "bash": "```bash\\n<read-only command>\\n```", "search": "```search <query>```",
    "fetch": "```fetch <url>```",
}

BUILTIN = {
    "reviewer": """---
description: Reviews the uncommitted changes for bugs, the way a strict senior engineer would
mode: report
tools: read grep glob map bash
model: chat
tokens: 300000
---
You review code changes. Start with `git status` and `git diff` (and `git diff --staged`);
if there are no changes, review the last commit (`git show`). Read enough of each changed
file around the change to understand it. Look for real defects only: wrong logic, unhandled
errors and edge cases (empty, None, unicode, huge input), races, resource leaks, security
holes (injection, path traversal, secrets), broken callers of a changed function (grep for
them), and missing tests for new behaviour. Ignore style. For each finding: file:line, what
goes wrong and a concrete input that triggers it, and the fix. Rank by severity. If the
change is sound, say so in the headline.""",
    "tester": """---
description: Writes the missing tests for the folder's code, runs them and fixes what they find
mode: act
tools: read grep glob map bash write edit
model: chat
tokens: 1500000
minutes: 60
---
You write tests. Find the project's test framework and how its tests run (read the config,
look at existing tests); use the same style. Find the code with the least coverage that
matters most (public functions, parsing, anything with branches). Write focused tests:
normal cases, edge cases, error cases. Run them. When a test fails, decide whether the test
or the code is wrong; fix the code only when it is clearly a bug, and say so. Finish with
every test passing. Report what you covered and every bug you found.""",
    "auditor": """---
description: A security audit of the folder: secrets, injection, unsafe input handling, risky dependencies
mode: report
tools: read grep glob map bash
model: chat
tokens: 400000
---
You are a security auditor. Map the project, find where untrusted input enters (HTTP
handlers, CLI args, files, env, IPC) and follow it: SQL/command/template injection, path
traversal, deserialisation, SSRF, missing auth checks, unsafe crypto, secrets committed to
the repo (grep for keys, tokens, passwords; check git history with `git log -p -S` for the
obvious ones), and dependencies with known problems (read the lockfile; note very old pins).
Report only what you can point at: file:line, the attack, its impact, and the fix. Rank by
severity. Say what you did not check.""",
    "checkup": """---
description: A health check of this Linux machine: failed services, journal errors, disk, updates, orphans
mode: report
tools: read grep bash
model: fast
tokens: 150000
---
You check a Linux machine's health, read-only. Look at: `systemctl --failed`, `journalctl -p 3
-b --no-pager | tail -50` (errors this boot), `df -h` and the biggest directories under ~ if
a disk is above 85%, `free -h`, `uptime`, pending updates (`checkupdates` on Arch), orphaned
packages (`pacman -Qdtq`), `.pacnew` files in /etc, and anything else that looks wrong. For
each problem: what it is, whether it matters, and the exact command that fixes it. Headline:
"healthy" or the most important problem.""",
    "researcher": """---
description: Researches a question on the web and writes a short brief with sources
mode: report
tools: search fetch
model: fast
tokens: 300000
---
You research one question. Search several different ways, open the most authoritative
sources (official docs, changelogs, maintainers, papers) and read them; prefer recent ones
and say how old they are. Separate what sources agree on from what only one says (mark it
[single source]). Write a brief: the answer first, then the evidence with URLs. If the
sources don't settle it, say so.""",
    "investigator": """---
description: Finds things out, in the code and on the web, and reports facts with evidence
mode: report
tools: read grep glob map bash search fetch
model: fast
tokens: 300000
---
You find things out. Use the code (map, grep, read; read-only commands) and the web (search,
fetch official docs and changelogs) as the question needs. Report facts with their evidence:
file:line for code, URLs for the web, the exact command output for the system. Separate what
you verified from what you infer. Answer the question first, then the evidence.""",
    "explainer": """---
description: Explains how a codebase works: entry points, the main flow, where to change what
mode: report
tools: read grep glob map
model: fast
tokens: 300000
---
You explain a codebase to an engineer who is new to it. Map it, find the entry points
(main, CLI, server routes, exported API), and follow the main flow through the code. Write:
what it does in two sentences; the layout (which directory holds what); the main flow step
by step with file:line references; the key data structures; where to make the common kinds
of change; and anything surprising. Be concrete; don't pad.""",
}


class ProgramError(ValueError):
    pass


def parse(text, name):
    """A program file -> dict, or ProgramError."""
    text = (text or "").replace("\r\n", "\n")
    m = re.match(r"---\n(.*?)\n---\n?(.*)\Z", text, re.DOTALL)
    head, body = (m.group(1), m.group(2)) if m else ("", text)
    meta = {}
    for ln in head.split("\n"):
        k, sep, v = ln.partition(":")
        if sep:
            meta[k.strip().lower()] = v.split("#", 1)[0].strip()
    mode = (meta.get("mode") or "report").lower()
    if mode not in ("report", "act"):
        raise ProgramError(f"{name}: mode must be report or act, not {mode!r}")
    allowed = ACT_TOOLS if mode == "act" else REPORT_TOOLS
    tools = [t for t in re.split(r"[\s,]+", meta.get("tools") or "") if t]
    bad = [t for t in tools if t not in allowed]
    if bad:
        raise ProgramError(f"{name}: a {mode} program can't use {', '.join(bad)} "
                           f"(it may use: {' '.join(allowed)})")
    tools = tools or list(allowed)

    def num(key, default, lo, hi):
        try:
            return max(lo, min(hi, int(str(meta.get(key) or default).replace("_", ""))))
        except ValueError:
            raise ProgramError(f"{name}: {key} must be a number") from None
    if not body.strip():
        raise ProgramError(f"{name}: the program has no prompt")
    return {"name": name, "description": meta.get("description") or "",
            "mode": mode, "tools": tools, "model": meta.get("model") or "",
            "tokens": num("tokens", 300000 if mode == "report" else 1500000, 5000, 50000000),
            "minutes": num("minutes", 20 if mode == "report" else 60, 1, 1440),
            "prompt": body.strip(), "source": "built-in"}


def caps_of(prog):
    """The capabilities a program asks for, from its mode and tools."""
    caps = {"read"}
    if "bash" in prog["tools"]:
        caps.add("exec")
    if {"search", "fetch"} & set(prog["tools"]):
        caps.add("net")
    if prog["mode"] == "act":
        caps.add("write")
    return caps


def load(dirs=()):
    """Every program: the built-ins, then each folder's (a later one with the
    same name wins). -> ({name: program}, [problems])."""
    progs, problems = {}, []
    for name, text in BUILTIN.items():
        progs[name] = parse(text, name)
    for d in dirs:
        if not d:
            continue
        try:
            files = sorted(Path(d).glob("*.md"))
        except OSError:
            continue
        for f in files:
            name = f.stem.lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", name):
                problems.append(f"{f.name}: a program's name is letters, digits, - and _")
                continue
            try:
                p = parse(f.read_text(errors="replace"), name)
            except (OSError, ProgramError) as ex:
                problems.append(str(ex))
                continue
            p["source"] = str(f)
            progs[name] = p
    return progs, problems


def report_prompt(prog):
    tools = (" ".join(TOOL_DOC[t] for t in prog["tools"] if t in TOOL_DOC)
             + "\n```post <key>: <fact>``` shares a finding on the board every agent reads; "
               "```board``` reads it.")
    return (REPORT_PROMPT.replace("{tools}", tools)
            + f"\n\nYOUR PROGRAM ({prog['name']}):\n{prog['prompt']}")


def act_prompt(prog, worker_prompt):
    return worker_prompt + f"\n\nYOUR PROGRAM ({prog['name']}):\n{prog['prompt']}"
