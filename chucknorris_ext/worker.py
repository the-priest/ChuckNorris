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


def run_worker(brief, call_model, parse, syscalls, max_steps=60, on_event=None,
               should_stop=None, wrap_up=None, context="", prompt=None):
    """Run one background agent to its report. -> (report, steps_used)."""
    msgs = [{"role": "system", "content": (prompt or WORKER_PROMPT) + ("\n\n" + context if context else "")},
            {"role": "user", "content": brief}]
    for step in range(max(1, max_steps)):
        if should_stop and should_stop():
            return "(stopped before finishing)", step
        if wrap_up and wrap_up() and step > 0:
            msgs.append({"role": "user", "content":
                         "You are out of budget. Stop working and write your REPORT now: what "
                         "you changed, what you verified, and what is left."})
            return (call_model(msgs) or "").strip() or "(no report)", step + 1
        reply = call_model(msgs) or ""
        calls = parse(reply)
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
        msgs.append({"role": "user", "content": "RESULTS:\n\n" + "\n\n".join(results)
                     + ("\n\nThis was your last step: write the REPORT now." if last else "")})
        # keep the conversation bounded: old results shrink, the brief stays
        if sum(len(m["content"]) for m in msgs) > 160000:
            for m in msgs[2:-6]:
                if m["role"] == "user" and len(m["content"]) > 1500:
                    m["content"] = _clip(m["content"], 1500)
    msgs.append({"role": "user", "content": "Step budget spent. Write your REPORT now."})
    return (call_model(msgs) or "").strip() or "(no report)", max_steps
