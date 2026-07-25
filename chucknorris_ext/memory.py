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
import math
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
    "what when where who why how which not no yes get got make made use used "
    # Added after IDF scoring landed: in a store of a few hundred short facts,
    # a word appearing once looks maximally distinctive, so filler like "like"
    # or "want" scored as strongly as "nvidia" and pulled in nonsense. These
    # carry no retrieval signal at any corpus size.
    "like likes liked want wants need needs thing things stuff really very just "
    "about into from over under after before some any all one two more most much "
    "than there here now new old good bad best better also only even still "
    "please thanks ok okay sure fine yeah nah lot lots bit".split())
_MAX_INJECT = 6          # at most this many recalled facts per turn
_MAX_CORE = 4            # always-on core facts cap
_COVERAGE_FLOOR = 0.34   # a third of the question's content words must match
_RARE_TERM_SCORE = 1.2   # ...unless a single genuinely rare term matched
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


def _idf(facts):
    """Inverse document frequency across the store.

    Raw overlap counting treats every shared word as equally informative, so a
    fact matched on 'the machine' scored the same as one matched on 'nvidia' or
    'zfs'. Common words are down-weighted, distinctive ones dominate. No model,
    no server — it is arithmetic over a few hundred short strings.
    """
    n = max(1, len(facts))
    df = {}
    for f in facts:
        for t in set(_stems(f["text"])):
            df[t] = df.get(t, 0) + 1
    return {t: math.log(1 + n / (1 + c)) for t, c in df.items()}, n


def _stems(text):
    """Crude suffix stripping so 'installing' matches 'install'. Deliberately
    conservative: an aggressive stemmer produces false matches, which are worse
    than misses here — a wrong fact injected into the prompt is a lie Chuck
    then repeats confidently.

    Rule ORDER matters. Stripping 'es' before 's' turned 'trees' into 'tre',
    which matched nothing at all — so 'es' is only stripped after a sibilant
    ('boxes' -> 'box', 'matches' -> 'match') and plain 's' otherwise.
    """
    out = []
    for t in _tokens(text):
        if len(t) > 5 and t.endswith("ing"):
            t = t[:-3]
        elif len(t) > 4 and t.endswith("ed"):
            t = t[:-2]
        elif len(t) > 4 and t.endswith("es") and t[-3] in "sxzhi":
            t = t[:-2]
        elif len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


def recall(context_text):
    """Return a short list of fact strings relevant to context_text, plus core.

    This is what gets injected — deliberately tiny. Never returns the whole store.

    Scoring blends four signals, all cheap and local:
      * IDF-weighted term overlap — distinctive words count for more
      * explicit weight, set when the fact was stored
      * hit count, so facts that keep proving useful float up (log-damped, or a
        single popular fact would crowd out everything else forever)
      * recency, as a gentle tiebreak — a fact from this week beats one from
        six months ago when they're otherwise equal
    """
    with _LOCK:
        facts = _load()
        if not facts:
            return []
    cstem = set(_stems(context_text))
    idf, _n = _idf(facts)
    core = [f for f in facts if f["kind"] == "core"][:_MAX_CORE]
    now = time.time()
    scored = []
    for f in facts:
        if f["kind"] == "core":
            continue
        ft = set(_stems(f["text"]))
        shared = cstem & ft
        if not shared:
            continue
        weighted = sum(idf.get(t, 0.5) for t in shared)
        # GATE on coverage, RANK by IDF. Gating on the IDF sum looked right but
        # collapses in a small store: with a handful of facts the maximum
        # possible idf is ~0.4, so a perfect single-word match scored under any
        # sensible floor and recalled nothing. Coverage — how much of the
        # question's content actually matched — is scale-free, and a lone but
        # genuinely rare term still gets in via the absolute escape hatch.
        coverage = len(shared) / max(1, len(cstem))
        if coverage < _COVERAGE_FLOOR and weighted < _RARE_TERM_SCORE:
            continue
        # normalise by the fact's own length so a rambling fact can't win on
        # sheer surface area
        overlap = weighted / (1 + math.log(1 + len(ft)))
        age_days = max(0.0, (now - f.get("ts", now)) / 86400.0)
        score = (overlap
                 + coverage * 0.5
                 + f.get("weight", 1) * 0.15
                 + math.log(1 + f.get("hits", 0)) * 0.2
                 + max(0.0, 0.3 - age_days / 365.0))
        scored.append((score, f))
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
