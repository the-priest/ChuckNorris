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
_HIT_FLUSH_SECS = 120    # how often recall() may write hit counts back to disk
_HIT_FLUSH = [0.0]


def _tokens(text):
    return {w for w in re.findall(r"[a-z0-9][a-z0-9_+.-]{1,}", (text or "").lower())
            if w not in _STOP and len(w) > 2}


# recall() rewrites the whole store every turn (to bump hit counts) while
# remember() may be rewriting it from another thread. Both are full-file
# replacements, so an interleave loses one side's write entirely.
_LOCK = threading.RLock()


# The store is re-read, re-parsed and re-scored on EVERY turn. It is small, but
# "small" work done unconditionally on the critical path before the model is
# even called is still latency the user sits through. Cache it against the
# file's mtime+size so an unchanged store is parsed exactly once per process,
# and derive the expensive bits (per-fact stems, IDF) alongside it.
_CACHE = {"sig": None, "facts": [], "stems": {}, "idf": None}


def _sig():
    try:
        st = FACTS.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _load():
    sig = _sig()
    if sig is not None and _CACHE["sig"] == sig:
        return _CACHE["facts"]
    out = []
    if FACTS.exists():
        try:
            for line in FACTS.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        except OSError:
            return _CACHE["facts"] if _CACHE["sig"] else []
    _CACHE.update({"sig": sig, "facts": out, "stems": {}, "idf": None})
    return out


def _save_all(facts):
    tmp = FACTS.with_suffix(".tmp")
    tmp.write_text("\n".join(json.dumps(f, ensure_ascii=False) for f in facts))
    tmp.replace(FACTS)
    # Re-point the cache at what we just wrote rather than invalidating it: the
    # next recall() would otherwise re-read and re-parse a file this process
    # already holds in memory.
    _CACHE.update({"sig": _sig(), "facts": facts, "stems": {}, "idf": None})


def _fact_stems(f):
    """Stems for one fact, memoised by id. Recomputed for every fact on every
    turn otherwise — twice, once for IDF and once for scoring."""
    key = f.get("id")
    hit = _CACHE["stems"].get(key)
    if hit is None:
        hit = set(_stems(f["text"]))
        _CACHE["stems"][key] = hit
    return hit


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
    if _CACHE["idf"] is not None:
        return _CACHE["idf"]
    n = max(1, len(facts))
    df = {}
    for f in facts:
        for t in _fact_stems(f):
            df[t] = df.get(t, 0) + 1
    out = ({t: math.log(1 + n / (1 + c)) for t, c in df.items()}, n)
    _CACHE["idf"] = out
    return out


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


# ── category → instance bridging ────────────────────────────────────────────
# The gap this closes: people ask by CATEGORY and facts are stored as
# INSTANCES. "what distro am I on" shares not one token with "User runs CachyOS
# on a ThinkPad X395", so the store held the answer and recalled nothing — the
# single most obvious question you can ask a memory, missed.
#
# This is a small hand-written map, not a thesaurus and not a model. It covers
# the vocabulary this tool actually lives in: distros, hardware, the shell.
# Matches through it are scored at HALF weight, so a real token match always
# beats a bridged one and a bridge alone can never drag in a fact that a direct
# match wouldn't have reached. Anything outside the map behaves exactly as
# before — "what's the weather like" still recalls nothing at all.
_RELATED_RAW = {
    "distro": "cachyos arch linux endeavouros manjaro fedora debian ubuntu nixos",
    "os": "cachyos arch linux distro kernel",
    "laptop": "thinkpad x395 framework macbook notebook dell lenovo asus",
    "machine": "laptop desktop thinkpad server box rig",
    "computer": "laptop desktop thinkpad machine box",
    "pc": "laptop desktop machine rig",
    "gpu": "nvidia amdgpu radeon intel graphics driver dkms nouveau",
    "graphics": "nvidia amdgpu radeon gpu driver",
    "cpu": "ryzen intel amd processor core",
    "network": "subnet ip vlan router wifi ethernet dns gateway lan",
    "wifi": "wireless wlan ssid network",
    "shell": "bash zsh fish terminal prompt",
    "editor": "vim neovim nvim emacs helix nano vscode",
    "browser": "firefox brave chromium librewolf",
    "terminal": "kitty alacritty foot wezterm konsole shell",
    "desktop": "kde plasma gnome hyprland sway i3 xfce wayland",
    "package": "pacman paru yay aur repo",
    "disk": "nvme ssd hdd btrfs ext4 zfs partition filesystem",
    "filesystem": "btrfs ext4 zfs xfs disk partition",
    "language": "python rust go bash javascript",
    "handle": "username nickname github alias",
    "repo": "github gitlab repository git",
    "project": "repo tool app build",
    "job": "work role employer",
    "phone": "android pixel samsung iphone",
    "vpn": "mullvad wireguard proxy tailscale",
    "firewall": "iptables nftables ufw firewalld",
}
_RELATED = {}
for _k, _v in _RELATED_RAW.items():
    _ks = _stems(_k)
    _vs = set(_stems(_v))
    for _kk in _ks:
        _RELATED.setdefault(_kk, set()).update(_vs)
_BRIDGE_WEIGHT = 0.5     # a bridged match is worth half a real one


def _bridge(cstem):
    """Extra stems worth looking for, given what the user actually typed."""
    out = set()
    for t in cstem:
        rel = _RELATED.get(t)
        if rel:
            out |= rel
    return out - cstem


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
    bridged = _bridge(cstem)
    idf, _n = _idf(facts)
    answerable = max(1, len([t for t in cstem if t in idf or t in _RELATED])
                     or len(cstem))
    core = [f for f in facts if f["kind"] == "core"][:_MAX_CORE]
    now = time.time()
    scored = []
    for f in facts:
        if f["kind"] == "core":
            continue
        ft = _fact_stems(f)
        shared = cstem & ft
        soft = (bridged & ft) - shared
        if not shared and not soft:
            continue
        weighted = (sum(idf.get(t, 0.5) for t in shared)
                    + _BRIDGE_WEIGHT * sum(idf.get(t, 0.5) for t in soft))
        # GATE on coverage, RANK by IDF. Gating on the IDF sum looked right but
        # collapses in a small store: with a handful of facts the maximum
        # possible idf is ~0.4, so a perfect single-word match scored under any
        # sensible floor and recalled nothing. Coverage — how much of the
        # question's content actually matched — is scale-free, and a lone but
        # genuinely rare term still gets in via the absolute escape hatch.
        # Coverage is measured against the ANSWERABLE part of the question,
        # not its raw word count. "what language do I write in" carries one
        # word the store could possibly speak to ("language") and one it could
        # not ("write") — dividing by both halved the score of a perfectly good
        # match and pushed it under the floor. A word that appears in no fact
        # and bridges to nothing is not evidence of a miss; it is just a word.
        coverage = (len(shared) + _BRIDGE_WEIGHT * len(soft)) / answerable
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
    # Bump hit counts for recalled facts (so useful ones survive pruning) —
    # in memory now, on disk at most every _HIT_FLUSH_SECS. Rewriting the whole
    # store on every single turn just to increment a counter meant a full
    # serialise + fsync + rename on the critical path of every message, for a
    # number nothing reads until the store overflows 400 facts.
    if picked:
        ids = {f["id"] for f in picked}
        with _LOCK:
            fresh = _load()
            for f in fresh:
                if f["id"] in ids:
                    f["hits"] = f.get("hits", 0) + 1
            now = time.time()
            if now - _HIT_FLUSH[0] >= _HIT_FLUSH_SECS:
                _HIT_FLUSH[0] = now
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


def flush():
    """Persist any in-memory hit counts. Called when the window closes so a
    session's worth of counters isn't lost to the batching above."""
    with _LOCK:
        try:
            _save_all(_load())
            _HIT_FLUSH[0] = time.time()
        except Exception:
            pass


def all_facts():
    """For a 'what do you remember' view — returns [(text, kind)]."""
    return [(f["text"], f["kind"]) for f in _load()]


def count():
    return len(_load())
