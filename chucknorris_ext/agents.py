"""agents.py — parallel sub-agents.

Chuck can split a big job into briefs and hand each to a sub-agent that runs
its OWN model loop at the same time as the others: survey three corners of a
repo at once, research four libraries side by side, read a dozen files in
parallel. Each one reports back a short, factual summary, and only the
summaries enter Chuck's context — which is the other half of the point: twenty
file reads cost the main conversation four paragraphs instead of twenty files.

Sub-agents are READ-ONLY by construction. They get search, fetch, read, grep,
glob and the repo map — never a shell, never a write. Parallel writers would
trample each other's edits and race each other's sudo prompts; parallel
readers cannot. Acting stays with the main agent, one verified step at a time.

Nothing here knows about GTK or the network: the caller supplies call_model
(messages -> reply text) and the tool functions, so the loop is testable with
plain stand-ins.
"""
import re
import threading
from concurrent.futures import ThreadPoolExecutor

SUB_PROMPT = (
    "You are a sub-agent of Chuck, an engineering agent. You work on ONE brief, in parallel "
    "with other sub-agents, and report back to Chuck (not to the user).\n"
    "You have READ-ONLY tools; you cannot run commands or change files. Put one or several "
    "fenced tool blocks in a reply; you get the results back:\n"
    "```search <query>``` ```fetch <url>``` ```read <path>[:start-end]``` "
    "```grep <regex> [path]``` ```glob <pattern>``` ```map```\n"
    "Be efficient: a few targeted calls, never the same one twice. When you know enough, reply "
    "with your REPORT and no tool block: concrete findings first, with file paths and line "
    "numbers or URLs as evidence, then anything you could not determine. No preamble, no "
    "filler, under 400 words unless the brief needs more.")

TOOL_RE = re.compile(r"```(search|fetch|read|grep|glob|map)(?![A-Za-z])[ \t]*\n?(.*?)```",
                     re.DOTALL)
MAX_CALLS_PER_STEP = 6


def _clip(text, limit):
    text = text or ""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return (text[:head] + f"\n… [{len(text) - limit} characters cut] …\n"
            + text[-(limit - head):])


def run_subagent(brief, call_model, tools, max_steps=8, on_event=None,
                 should_stop=None, context="", wrap_up=None):
    """Run one sub-agent to completion. Returns its report (a string).

    call_model(messages) -> str     one blocking model call
    tools[name](arg) -> str         read-only tool implementations
    on_event(text)                  progress ("grep TODO src/") for the UI
    wrap_up() -> bool               the kernel asked it to finish (TERM, or its
                                    quota is spent): its next call writes the report
    """
    msgs = [{"role": "system", "content": SUB_PROMPT + ("\n\n" + context if context else "")},
            {"role": "user", "content": brief}]
    seen = set()
    for step in range(max(1, max_steps)):
        if should_stop and should_stop():
            return "(stopped before finishing)"
        if wrap_up and wrap_up() and step > 0:
            msgs.append({"role": "user", "content":
                         "You are out of budget. Write your REPORT now from what you have "
                         "found so far; say what you could not finish."})
            return (call_model(msgs) or "").strip() or "(no report)"
        reply = call_model(msgs) or ""
        calls = TOOL_RE.findall(reply)
        if not calls:
            return reply.strip() or "(the sub-agent returned an empty report)"
        msgs.append({"role": "assistant", "content": reply})
        results = []
        for tag, arg in calls[:MAX_CALLS_PER_STEP]:
            arg = arg.strip()
            key = (tag, arg)
            if key in seen:
                results.append(f"[{tag} {arg[:80]}] already done — use the earlier result")
                continue
            seen.add(key)
            if on_event:
                on_event(f"{tag} {arg.splitlines()[0][:70] if arg else ''}".strip())
            fn = tools.get(tag)
            try:
                out = fn(arg) if fn else f"[{tag} is not available]"
            except Exception as ex:                      # a tool failing is a result
                out = f"[{tag} failed: {ex}]"
            results.append(f"[{tag} {arg[:80]}]\n{_clip(out, 12000)}")
        last = step >= max_steps - 2
        msgs.append({"role": "user", "content": "RESULTS:\n\n" + "\n\n".join(results)
                     + ("\n\nThis was your last tool step: write the REPORT now."
                        if last else "")})
    msgs.append({"role": "user", "content":
                 "Step budget spent. Write your REPORT now from what you have."})
    if should_stop and should_stop():
        return "(stopped before finishing)"
    return (call_model(msgs) or "").strip() or "(no report)"


def run_parallel(briefs, run_one, max_workers=4):
    """Run run_one(index, brief) for every brief concurrently; results in
    brief order. A brief that raises reports its error instead of sinking the
    batch."""
    results = [None] * len(briefs)
    lock = threading.Lock()

    def go(i):
        try:
            r = run_one(i, briefs[i])
        except Exception as ex:
            r = f"(sub-agent failed: {ex})"
        with lock:
            results[i] = r

    ex = ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(briefs) or 1)))
    try:
        list(ex.map(go, range(len(briefs))))
    finally:
        ex.shutdown(wait=True)
    return results
