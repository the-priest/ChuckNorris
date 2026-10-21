"""worker.py — the headless agent: a full agent loop with no window, for
background processes. See docs/OS.md (phase 17).

Where a sub-agent (agents.py) only reads, a worker ACTS: it writes and edits
files and runs commands. That is safe because it works in its own
copy-on-write workspace (cow.py): nothing it does reaches your folder until
you merge it, and its commands run sandboxed in that copy.

Like agents.py this knows nothing about GTK, the network or the model: the
caller supplies call_model(messages) -> text, parse(reply) -> [(tool, arg)]
and the syscall table {tool: fn(arg) -> result text}.
"""

WORKER_PROMPT = (
    "You are a background agent of Chuck, an engineering agent. You work ALONE on one task, "
    "while the user keeps working with Chuck. You work in your own private copy of the "
    "project: nothing you do touches the user's folder until they review and merge your work, "
    "so act freely, but finish the job properly.\n"
    "Tools: put one or several fenced blocks in a reply and you get the results back:\n"
    "```read <path>[:start-end]``` ```grep <regex> [path]``` ```glob <pattern>``` ```map```\n"
    "```write <path>\\n<full file content>\\n``` (a file containing ``` goes in a ````write "
    "block)\n"
    "```edit <path>\\n<<<<<<< SEARCH\\n<exact lines>\\n=======\\n<new lines>\\n>>>>>>> REPLACE\\n```\n"
    "```bash\\n<command>\\n``` (runs in your copy of the project; no sudo; commands that "
    "need the user's approval are refused) ```search <query>``` ```fetch <url>```\n"
    "```post <key>: <fact>``` puts a fact on the board that every agent and Chuck read (what "
    "you are changing, what you found); ```board``` reads it. ```ask <question>``` asks the "
    "user and waits for the answer: only for a decision that is truly theirs. Messages from the "
    "user and the [kernel] line (your budget) arrive with your results.\n"
    "Work like a careful engineer: read before you change, change in small steps, run the "
    "tests or the code after changing it, fix what fails. When the task is DONE (or truly "
    "blocked), reply with no tool block: your REPORT for the user — what you changed (files), "
    "how you verified it (the commands and their results), and anything left undone. Be "
    "concrete and brief.")

MAX_CALLS_PER_STEP = 8


def _clip(text, limit):
    text = text or ""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return text[:head] + f"\n… [{len(text) - limit} characters cut] …\n" + text[-(limit - head):]


def _mail_text(mail):
    return ("MESSAGES that arrived while you worked:\n"
            + "\n".join(f"- {m}" for m in mail))


def run_worker(brief, call_model, parse, syscalls, max_steps=60, on_event=None,
               should_stop=None, wrap_up=None, context="", prompt=None, resume=None,
               on_step=None, inbox=None, status_line=None):
    """Run one background agent to its report. -> (report, steps_used).
    resume: a saved conversation to go on from (hibernate, fork).
    on_step(msgs, step): called after every step, to checkpoint it.
    inbox() -> [text]: messages for the agent, delivered with its results.
    status_line() -> text: the kernel's line (budget left), likewise."""
    if resume:
        msgs = [dict(m) for m in resume]
        first = sum(1 for m in msgs if m.get("role") == "assistant")
    else:
        msgs = [{"role": "system", "content": (prompt or WORKER_PROMPT)
                 + ("\n\n" + context if context else "")},
                {"role": "user", "content": brief}]
        first = 0
    for step in range(first, max(first + 1, max_steps)):
        if should_stop and should_stop():
            return "(stopped before finishing)", step
        if wrap_up and wrap_up() and step > 0:
            msgs.append({"role": "user", "content":
                         "You are out of budget. Stop working and write your REPORT now: what "
                         "you changed, what you verified, and what is left."})
            return (call_model(msgs) or "").strip() or "(no report)", step + 1
        reply = call_model(msgs) or ""
        calls = parse(reply)
        mail = inbox() if inbox and not calls else []
        if not calls and mail and step < max_steps - 1:
            # a message arrived as it was about to finish: it hears it first
            msgs.append({"role": "assistant", "content": reply})
            msgs.append({"role": "user", "content": _mail_text(mail)
                         + "\n\nTake this into account, then finish (your REPORT when done)."})
            continue
        if not calls:
            return reply.strip() or "(the agent returned an empty report)", step + 1
        msgs.append({"role": "assistant", "content": reply})
        results = []
        for tag, arg in calls[:MAX_CALLS_PER_STEP]:
            if should_stop and should_stop():
                break
            shown = arg[0] if isinstance(arg, tuple) else (arg or "")   # write/edit: the path
            label = shown.strip().splitlines()[0][:70] if shown.strip() else ""
            if on_event:
                on_event(f"{tag} {label}".strip())
            fn = syscalls.get(tag)
            try:
                out = fn(arg) if fn else f"[{tag} is not available to a background agent]"
            except Exception as ex:                      # a failing tool is a result
                out = f"[{tag} failed: {ex}]"
            results.append(f"[{tag} {label}]\n{_clip(out, 12000)}")
        if len(calls) > MAX_CALLS_PER_STEP:
            results.append(f"[{len(calls) - MAX_CALLS_PER_STEP} more tool blocks were NOT run: "
                           "send them in your next reply]")
        last = step >= max_steps - 2
        # mail is collected after the tools ran: what arrived while it
        # worked (or waited on the user) is heard now, not a step late
        mail = inbox() if inbox else []
        extra = ("\n\n" + _mail_text(mail)) if mail else ""
        sl = status_line() if status_line else ""
        extra += f"\n\n[kernel] step {step + 1} of {max_steps} \u00b7 {sl}" if sl else ""
        msgs.append({"role": "user", "content": "RESULTS:\n\n" + "\n\n".join(results) + extra
                     + ("\n\nThis was your last step: write the REPORT now." if last else "")})
        # keep the conversation bounded: old results shrink, the brief stays
        if sum(len(m["content"]) for m in msgs) > 160000:
            for m in msgs[2:-6]:
                if m["role"] == "user" and len(m["content"]) > 1500:
                    m["content"] = _clip(m["content"], 1500)
        # a step cut short (stopped, killed) is not a checkpoint: its tools
        # didn't all run, so resuming from it would be resuming from a lie
        if on_step and not (should_stop and should_stop()):
            on_step(msgs, step + 1)
    msgs.append({"role": "user", "content": "Step budget spent. Write your REPORT now."})
    return (call_model(msgs) or "").strip() or "(no report)", max_steps
