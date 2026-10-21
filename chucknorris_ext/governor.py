"""governor.py — which model each call runs on. See docs/OS.md (phase 20).

A CPU governor trades speed for power. This one trades model strength for
money: not every call needs the strongest model. A sub-agent grepping for a
function name, a daemon reading the journal, a summary of old turns: a
fast, cheap model does those as well, for a fraction of the price. Writing
code and deciding what to do next is where a strong model earns its price.

    performance  every call on the chat model (what you picked)
    balanced     the chat and background writers on the chat model;
                 read-only sub-agents, daemons, summaries on the fast model
    powersave    everything on the fast model

The fast model is Settings' choice, or the cheapest suggested model that
costs less than the chat model (free models are rate-limited and weak
agents, so they are never picked by themselves).

Pure: prices and the suggested list are passed in.
"""

MODES = ("performance", "balanced", "powersave")
DEFAULT = "performance"
# the roles each mode moves to the fast model
FAST_ROLES = {
    "performance": frozenset(),
    "balanced": frozenset({"sub", "daemon", "summary", "reader"}),
    "powersave": frozenset({"main", "sub", "daemon", "summary", "reader", "background"}),
}
ROLE_NAMES = {"main": "the chat", "background": "background agents",
              "sub": "sub-agents (task)", "daemon": "daemons", "reader": "report programs",
              "summary": "summaries"}


def mode_of(settings):
    m = str((settings or {}).get("governor") or DEFAULT).strip().lower()
    return m if m in MODES else DEFAULT


def _weight(price):
    """One number to compare models by: agents read far more than they
    write, so input counts three times."""
    return 3 * price[0] + price[2]


def fast_model(settings, chat, prices, suggested, price_of=None):
    """The model the governor moves cheap work to: never one that costs MORE
    than the chat model, and when the chat model's price is unknown (a model
    the table doesn't know), the chat model itself. price_of(model) -> the
    price with the user's own overrides, when the app has them."""
    want = str((settings or {}).get("fast_model") or "").strip()
    if want:
        return want
    look = price_of or (lambda m: prices.get((m or "").lower()))
    mine = look(chat)
    if not mine:
        return chat
    best, best_w = chat, _weight(mine)
    for cand in suggested:
        p = look(cand)
        if not p or (p[0] == 0 and p[2] == 0):
            continue
        w = _weight(p)
        if w < best_w:
            best, best_w = cand, w
    return best


def pick(settings, role, chat, prices, suggested, price_of=None):
    """The model for one call by `role` (main, sub, daemon, summary, reader,
    background)."""
    if role in FAST_ROLES[mode_of(settings)]:
        return fast_model(settings, chat, prices, suggested, price_of)
    return chat


def table(settings, chat, prices, suggested, price_of=None):
    """[(role label, model)] for /governor."""
    return [(ROLE_NAMES[r], pick(settings, r, chat, prices, suggested, price_of))
            for r in ("main", "background", "sub", "reader", "daemon", "summary")]


def saving(settings, chat, prices, suggested, price_of=None):
    """How much cheaper the fast model is, as a fraction (0.0 when it's the
    same model or prices are unknown)."""
    fast = fast_model(settings, chat, prices, suggested, price_of)
    look = price_of or (lambda m: prices.get((m or "").lower()))
    a, b = look(chat), look(fast)
    if not a or not b or fast == chat or _weight(a) <= 0:
        return 0.0
    return max(0.0, 1 - _weight(b) / _weight(a))
