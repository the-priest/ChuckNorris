"""compress.py — shrink bulky tool output before it is billed as input tokens.

A fetched webpage, a directory listing or a package-manager transcript can be
tens of thousands of characters, and every one of them is re-sent on every
subsequent hop of the same turn. That is the single biggest avoidable cost in a
long session.

Two rules, in this order:

  1. The MOST RECENT results stay whole. Chuck is usually acting on what he just
     read; summarising it is how an agent starts hallucinating detail it no
     longer has. Only older blobs are squeezed.

  2. Compression is EXTRACTIVE, never generative. No second model call, no
     paraphrase — it keeps real lines from the original and marks what it cut.
     A summarising pass would cost tokens to save tokens and could invent
     content; keeping head, tail and error lines cannot.

Errors are always kept. Losing the one line that says what went wrong in order
to save 200 tokens is a bad trade every single time.
"""

import re

# lines worth keeping out of the middle of anything
_SIGNAL = re.compile(
    r"\b(error|fail(ed|ure)?|denied|refused|not found|no such|cannot|can't|unable|"
    r"warning|fatal|traceback|exception|conflict|missing|timed? ?out|"
    r"exit(ed)? (code|status)|permission)\b", re.IGNORECASE)


def _squeeze(text, head_lines=25, tail_lines=15, max_signal=20):
    """Head + any signal lines + tail, with an explicit note about the gap."""
    lines = text.splitlines()
    if len(lines) <= head_lines + tail_lines + 10:
        return text
    head = lines[:head_lines]
    tail = lines[-tail_lines:]
    middle = lines[head_lines:-tail_lines]
    signal = [ln for ln in middle if _SIGNAL.search(ln)][:max_signal]
    cut = len(middle) - len(signal)
    out = list(head)
    if signal:
        out.append(f"    \u2026 [{cut} unremarkable lines omitted; "
                   f"{len(signal)} notable ones kept] \u2026")
        out.extend(signal)
    else:
        out.append(f"    \u2026 [{cut} lines omitted \u2014 nothing matched an "
                   "error or warning pattern] \u2026")
    out.extend(tail)
    return "\n".join(out)


def compress_blob(text, budget_chars=3000):
    """Compress one tool-result blob to roughly budget_chars."""
    if not text or len(text) <= budget_chars:
        return text, False
    squeezed = _squeeze(text)
    if len(squeezed) > budget_chars:
        # still too big: hard-clip the middle, keeping both ends
        keep = budget_chars // 2
        squeezed = (squeezed[:keep]
                    + f"\n    \u2026 [{len(squeezed) - budget_chars} characters "
                      "cut from the middle] \u2026\n"
                    + squeezed[-keep:])
    return squeezed, True


def compress_history(history, keep_recent=2, budget_chars=3000, min_len=1500):
    """Return (new_history, chars_saved).

    Only TOOL RESULTS user-turns are touched — never the system prompt, never
    anything the user typed, never Chuck's own replies. The newest `keep_recent`
    tool blobs are left whole.
    """
    idxs = [i for i, m in enumerate(history)
            if m.get("role") == "user"
            and isinstance(m.get("content"), str)
            and m["content"].startswith("TOOL RESULTS")]
    if len(idxs) <= keep_recent:
        return history, 0
    squeezable = idxs[:-keep_recent] if keep_recent else idxs
    out = list(history)
    saved = 0
    for i in squeezable:
        original = out[i]["content"]
        if len(original) < min_len:
            continue
        new, did = compress_blob(original, budget_chars)
        if did:
            saved += len(original) - len(new)
            out[i] = dict(out[i])
            out[i]["content"] = new + "\n[older tool output, trimmed to save context]"
    return out, saved
