"""pricing.py — what a turn actually costs, in dollars, from real token counts.

The meter used to be chars/4 and nothing else: an estimate of tokens, and no
idea at all what they cost. SiliconFlow reports real usage on the response
(prompt, completion, cache hits, reasoning), so the meter now prefers those and
prices them against a per-model table. chars/4 survives only as the fallback
for a response that carried no usage block.

PRICES are list prices in USD per 1M tokens on SiliconFlow's international
site, as published October 2026. They drift; anything here can be overridden in
Settings (custom input/output price), and an unknown model simply shows tokens
without a dollar figure rather than inventing one.
"""
import json
import urllib.request

# model id (lower-case) -> (input, cached_input, output)  USD per 1M tokens.
# cached_input None means "no separate cache price published" — billed at the
# input rate, which is the conservative assumption.
PRICES = {
    "deepseek-ai/deepseek-v4-flash": (0.13, None, 0.28),
    "deepseek-ai/deepseek-v4.1-flash": (0.158, 0.01, 0.633),
    "zai-org/glm-5.3": (1.40, 0.26, 4.40),
    "zai-org/glm-5.3-flash": (0.15, None, 0.50),
    "zai-org/glm-5.1": (1.40, None, 4.40),
    "deepseek-ai/deepseek-v3.2": (0.27, 0.135, 0.42),
    "deepseek-ai/deepseek-v3": (0.25, None, 1.00),
    "qwen/qwen3.6-35b-a3b": (0.24, 0.15, 1.80),
    "qwen/qwen3.6-27b": (0.30, None, 3.20),
    "qwen/qwen3-30b-a3b-instruct-2507": (0.09, None, 0.30),
}

# SiliconFlow's free tier. The set rotates, so these are the long-standing
# ones; "Fetch from account" in Settings lists whatever the key can actually
# use. Free models are rate-limited and smaller — fine for chat, weaker as an
# agent.
FREE = [
    "Qwen/Qwen3-8B",
    "THUDM/GLM-4-9B-0414",
    "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B",
]
for _m in FREE:
    PRICES[_m.lower()] = (0.0, None, 0.0)

# The picker, in order. GLM-5.3-Flash is the default because it is natively
# multimodal: the same model that writes the code can look at the screenshot.
SUGGESTED = [
    "zai-org/GLM-5.3-Flash",
    "deepseek-ai/DeepSeek-V4.1-Flash",
    "deepseek-ai/DeepSeek-V4-Flash",
    "zai-org/GLM-5.3",
]

# Models known to take images. Anything else gets screenshots routed to the
# vision model instead of failing on an image it cannot read.
VISION = {"zai-org/glm-5.3-flash", "zai-org/glm-5.3", "qwen/qwen3.6-35b-a3b",
          "qwen/qwen3.6-27b"}


def sees_images(model):
    return (model or "").strip().lower() in VISION


def is_free(model):
    p = PRICES.get((model or "").strip().lower())
    return bool(p) and p[0] == 0 and p[2] == 0


def fetch_models(base_url, key, opener=None, timeout=15):
    """Chat models this key can use, from GET {base}/models -> (ok, [ids] | msg)."""
    if not key:
        return False, "no API key"
    url = base_url.rstrip("/") + "/models?type=text&sub_type=chat"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key})
    try:
        op = opener.open if opener else urllib.request.urlopen
        with op(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception as ex:
        return False, f"couldn't list models ({ex.__class__.__name__})"
    ids = sorted({m.get("id") for m in (data.get("data") or []) if isinstance(m, dict)
                  and m.get("id")})
    return (True, ids) if ids else (False, "no models in response")


def price_for(model, settings=None):
    """(input, cached_input, output) per 1M tokens, or None if unknown.

    A custom price in Settings wins over the table — that is how a user keeps
    the meter honest when SiliconFlow changes a price or they run a model the
    table has never heard of.
    """
    s = settings or {}
    table = PRICES.get((model or "").strip().lower())
    try:
        ci = float(s.get("price_in") or 0)
        co = float(s.get("price_out") or 0)
    except (TypeError, ValueError):
        ci = co = 0.0
    chat_model = (s.get("model") or "").strip().lower()
    # a custom price is for the chat model it was set for (not the fallback,
    # vision or a free model), and a side left at 0 keeps the table's price
    if (ci > 0 or co > 0) and (not chat_model or (model or "").strip().lower() == chat_model):
        t_in, t_cached, t_out = table if table else (0.0, None, 0.0)
        return (ci if ci > 0 else t_in, t_cached if ci <= 0 else None, co if co > 0 else t_out)
    return table


def usage_from_chunk(d):
    """Normalise a response's usage block to
    {prompt, completion, cached, reasoning} — or None if there isn't one.

    Providers disagree on where cache hits live: SiliconFlow and DeepSeek use a
    top-level prompt_cache_hit_tokens, OpenAI-style servers nest them under
    prompt_tokens_details.cached_tokens. Both are read.
    """
    u = (d or {}).get("usage") if isinstance(d, dict) else None
    if not isinstance(u, dict):
        return None
    try:
        prompt = int(u.get("prompt_tokens") or 0)
        completion = int(u.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        return None
    if not (prompt or completion):
        return None
    cached = u.get("prompt_cache_hit_tokens")
    if cached is None:
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
    reasoning = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
    try:
        cached = max(0, min(prompt, int(cached or 0)))
        reasoning = max(0, int(reasoning or 0))
    except (TypeError, ValueError):
        cached, reasoning = 0, 0
    return {"prompt": prompt, "completion": completion,
            "cached": cached, "reasoning": reasoning}


def cost(usage, price):
    """Dollar cost of one usage record, or None when the price is unknown.

    Reasoning tokens are already inside completion_tokens on every
    OpenAI-compatible API, so they are NOT added again — double-billing the
    thinking would make a reasoning model look twice as expensive as it is.
    """
    if not usage or not price:
        return None
    p_in, p_cache, p_out = price
    if p_cache is None:
        p_cache = p_in
    fresh = max(0, usage["prompt"] - usage.get("cached", 0))
    return (fresh * p_in + usage.get("cached", 0) * p_cache
            + usage["completion"] * p_out) / 1_000_000


def fmt_usd(x):
    """$0.0042 for fractions of a cent, $1.24 once it is real money."""
    if x is None:
        return ""
    if x < 0.01:
        return f"${x:.4f}"
    if x < 0.1:
        return f"${x:.3f}"
    return f"${x:.2f}"                   # $0.50, not $0.500


def fmt_tokens(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def fetch_balance(base_url, key, opener=None, timeout=10):
    """Account balance from GET {base}/user/info -> (ok, text).

    The endpoint returns {"data": {"balance": "0.88", "totalBalance": "88.88",
    ...}}. totalBalance includes granted credit, so it is the number that says
    how much more the user can spend; balance is shown when that is all there is.
    """
    if not key:
        return False, "no API key"
    req = urllib.request.Request(base_url.rstrip("/") + "/user/info",
                                 headers={"Authorization": "Bearer " + key})
    try:
        op = opener.open if opener else urllib.request.urlopen
        with op(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "ignore")).get("data") or {}
    except Exception as ex:
        return False, f"couldn't fetch balance ({ex.__class__.__name__})"
    total = data.get("totalBalance") or data.get("balance")
    if total in (None, ""):
        return False, "no balance in response"
    cur = "\u00a5" if ".cn" in base_url else "$"     # the China site bills in yuan
    try:
        return True, f"{cur}{float(total):.2f}"
    except (TypeError, ValueError):
        return True, str(total)
