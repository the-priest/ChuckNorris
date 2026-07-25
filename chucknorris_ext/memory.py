"""memory.py — Chuck's long-term memory: durable, relevance-scoped, prompt-safe.

Two hard goals:
  1. IMPECCABLE     — real durable facts (the user's hardware, preferences, names
     of their projects, standing instructions) survive across chats and are
     recalled when relevant.
  2. NO CONTAMINATION — the whole store is NEVER dumped into the prompt. Each turn
     only a tiny, relevance-scored slice is injected (a few lines), plus a small
     always-on "core". Storage is unbounded; injection is bounded and cheap.

Design (Basilisk memory.py lineage, stdlib-only):
  - Facts live as JSONL under ~/.local/share/chucknorris/memory/facts.jsonl.
  - Each fact: {id, text, kind, weight, ts, hits}. kind ∈ {core, fact}.
  - Recall = keyword overlap score between the current turn and each fact, top-k,
    above a floor. "core" facts (few, high-value) are always included.
  - Dedup on near-identical text so the store doesn't grow stale duplicates.
  - Chuck decides what to remember via a ```remember``` tool; nothing is stored
    silently, so the user can see and trust it. A ```forget``` tool prunes.
"""
import re
import json
import time
import threading
from pathlib import Path

MEM_DIR = Path.home() / ".local" / "share" / "chucknorris" / "memory"
MEM_DIR.mkdir(parents=True, exist_ok=True)
FACTS = MEM_DIR / "facts.jsonl"

_STOP = set(
    "the a an and or but if then of to in on at for with by is are was were be been "
    "being it its this that these those i you he she they we me my your his her their "
    "our as so do does did has have had will would can could should he's i'm you're "
    "what when where who why how which not no yes get got make made use used".split())
_MAX_INJECT = 6          # at most this many recalled facts per turn
_MAX_CORE = 4            # always-on core facts cap
_SCORE_FLOOR = 1         # need at least this much keyword overlap to recall
_MAX_FACTS = 400         # hard cap on stored facts (prune weakest beyond this)


def _tokens(text):
    return {w for w in re.findall(r"[a-z0-9][a-z0-9_+.-]{1,}", (text or "").lower())
            if w not in _STOP and len(w) > 2}


# recall() rewrites the whole store every turn (to bump hit counts) while
# remember() may be rewriting it from another thread. Both are full-file
# replacements, so an interleave loses one side's write entirely.
_LOCK = threading.RLock()


def _load():
    out = []
    if FACTS.exists():
        for line in FACTS.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _save_all(facts):
    tmp = FACTS.with_suffix(".tmp")
    tmp.write_text("\n".join(json.dumps(f, ensure_ascii=False) for f in facts))
    tmp.replace(FACTS)


_ID_SEQ = [0]


def _new_id(existing):
    """A unique id. int(time.time()*1000) % 10**9 wraps every ~11.6 days, and
    recall() bumps hit counts by id — a collision credited the wrong fact."""
    used = {f.get("id") for f in existing}
    while True:
        _ID_SEQ[0] += 1
        cand = (int(time.time() * 1000) + _ID_SEQ[0]) % 10 ** 9
        if cand not in used:
            return cand


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").strip())


def remember(text, kind="fact", weight=1.0):
    """Store a durable fact. Dedupes near-identical text. Returns (ok, message)."""
    text = _norm(text)
    if not text or len(text) < 3:
        return False, "nothing to remember"
    if len(text) > 400:
        text = text[:400]
    with _LOCK:
        return _remember_locked(text, kind, weight)


def _remember_locked(text, kind, weight):
    facts = _load()
    ntok = _tokens(text)
    for f in facts:
        ft = _tokens(f["text"])
        if not ntok or not ft:
            continue
        overlap = len(ntok & ft) / max(1, len(ntok | ft))
        if overlap > 0.8 or f["text"].lower() == text.lower():
            # refresh instead of duplicating
            f["ts"] = time.time(); f["weight"] = max(f.get("weight", 1.0), weight)
            _save_all(facts)
            return True, "already knew that (refreshed)"
    facts.append({"id": _new_id(facts), "text": text,
                  "kind": "core" if kind == "core" else "fact",
                  "weight": float(weight), "ts": time.time(), "hits": 0})
    # prune if over cap: drop lowest weight*recency, keep all core
    if len(facts) > _MAX_FACTS:
        core = [f for f in facts if f["kind"] == "core"]
        rest = sorted((f for f in facts if f["kind"] != "core"),
                      key=lambda f: (f.get("weight", 1) + f.get("hits", 0), f["ts"]),
                      reverse=True)[:_MAX_FACTS - len(core)]
        facts = core + rest
    _save_all(facts)
    return True, "noted"


def forget(query):
    """Remove facts matching query (substring or strong keyword overlap)."""
    with _LOCK:
        return _forget_locked(query)


def _forget_locked(query):
    facts = _load()
    if not facts:
        return False, "nothing stored"
    q = query.lower().strip()
    qtok = _tokens(query)
    kept, removed = [], 0
    for f in facts:
        ftxt = f["text"].lower()
        ft = _tokens(f["text"])
        overlap = (len(qtok & ft) / max(1, len(qtok))) if qtok else 0
        if q and (q in ftxt or overlap >= 0.6):
            removed += 1
        else:
            kept.append(f)
    if removed:
        _save_all(kept)
    return (removed > 0, f"forgot {removed} fact(s)" if removed else "no match to forget")


def recall(context_text):
    """Return a short list of fact strings relevant to context_text, plus core.

    This is what gets injected — deliberately tiny. Never returns the whole store.
    """
    with _LOCK:
        facts = _load()
        if not facts:
            return []
    ctok = _tokens(context_text)
    core = [f for f in facts if f["kind"] == "core"][:_MAX_CORE]
    scored = []
    for f in facts:
        if f["kind"] == "core":
            continue
        ft = _tokens(f["text"])
        score = len(ctok & ft)
        if score >= _SCORE_FLOOR:
            scored.append((score + f.get("weight", 1) * 0.1, f))
    scored.sort(key=lambda x: x[0], reverse=True)
    picked = core + [f for _s, f in scored[:_MAX_INJECT]]
    # bump hit counts for recalled facts (so useful ones survive pruning)
    if picked:
        ids = {f["id"] for f in picked}
        with _LOCK:
            fresh = _load()                 # re-read: another thread may have written
            for f in fresh:
                if f["id"] in ids:
                    f["hits"] = f.get("hits", 0) + 1
            _save_all(fresh)
    # de-dup while preserving order
    seen, out = set(), []
    for f in picked:
        if f["text"] not in seen:
            seen.add(f["text"]); out.append(f["text"])
    return out[:_MAX_INJECT + _MAX_CORE]


def memory_block(context_text):
    """The injected string (or '' if nothing relevant). Bounded + prefixed."""
    hits = recall(context_text)
    if not hits:
        return ""
    return "What you remember about this user (use if relevant, don't force it):\n- " + \
           "\n- ".join(hits)


def all_facts():
    """For a 'what do you remember' view — returns [(text, kind)]."""
    return [(f["text"], f["kind"]) for f in _load()]


def count():
    return len(_load())
