"""web.py — search across engines, page fetching, images and video.

Part of Chuck Norris — split out of the original single file.
"""
import html as _html
import json
import random
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config
from .config import CONFIG_DIR, UA  # noqa: F401

_SETTINGS = config.SETTINGS_DATA


def _get(url, data=None, timeout=20, headers=None):
    """Single network entry point — delegates to config.get so the scheme
    allowlist is enforced in exactly one place."""
    return config.get(url, data=data, timeout=timeout, headers=headers)


# ── web + images ────────────────────────────────────────────────────────────
def _strip(s):
    return _html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


# Public SearXNG instances (JSON API). Tried in order; first that answers wins.
SEARX_INSTANCES = [
    "https://searx.be", "https://search.inetol.net", "https://priv.au",
    "https://searx.tiekoetter.com", "https://search.rhscz.eu",
    "https://searxng.site", "https://opnxng.com", "https://search.bus-hit.me",
]


def _domain(url):
    try:
        return urllib.parse.urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return url


def _ddg_html_search(query, n):
    """DuckDuckGo HTML endpoint — the original engine, now a fallback."""
    for host in ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"):
        try:
            data = urllib.parse.urlencode({"q": query}).encode()
            html = _get(host, data=data).read().decode("utf-8", "ignore")
        except Exception:
            continue
        titles = re.findall(r'class="result__a"[^>]*href="([^"]+)".*?>(.*?)</a>', html, re.DOTALL)
        snips = [_strip(s) for s in re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)]
        out = []
        for i, (href, title) in enumerate(titles[:n]):
            q = urllib.parse.urlparse(href.replace("&amp;", "&"))
            real = urllib.parse.parse_qs(q.query).get("uddg", [href])[0]
            if real.startswith("//"):
                real = "https:" + real
            out.append((_strip(title), real, snips[i] if i < len(snips) else ""))
        if out:
            return out
    return []


_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif", ".svg")


def _is_image_path(p):
    return isinstance(p, str) and p.strip().lower().endswith(_IMAGE_EXT)


def _searx_one(host, query, n, categories, timeout):
    url = (host.rstrip("/") + "/search?format=json&safesearch=0&categories=" + categories +
           "&q=" + urllib.parse.quote(query))
    js = _get(url, timeout=timeout,
              headers={"User-Agent": UA, "Accept": "application/json"}
              ).read().decode("utf-8", "ignore")
    out = []
    for r in json.loads(js).get("results", [])[:n * 2]:
        u = r.get("url") or ""
        if not u.startswith("http"):
            continue
        out.append((_strip(r.get("title", "")), u, _strip(r.get("content", ""))))
    return out


def _searx_search(query, n, categories="general"):
    """Query SearXNG (which aggregates Brave/Google/DDG/Bing) for real results.

    Instances are hit CONCURRENTLY and the first usable answer wins. Probing
    them one at a time meant two slow or rate-limited hosts cost 30 seconds
    before the fallback even started — the single worst case for answer speed.
    The public list is shuffled so we don't hammer the same host every search.
    """
    pref = (_SETTINGS.get("searx_url") or "").strip().rstrip("/")
    pool = list(SEARX_INSTANCES)
    random.shuffle(pool)
    hosts = ([pref] if pref else []) + pool
    if pref:
        try:                                    # a self-hosted instance gets first refusal
            got = _searx_one(pref, query, n, categories, 8)
            if got:
                return got[:n]
        except Exception:
            pass
        hosts = pool
    # NB: a `with ThreadPoolExecutor(...)` block calls shutdown(wait=True) on
    # exit, so returning early from inside it still blocks until every straggler
    # finishes — which would defeat the whole point of probing in parallel.
    ex = ThreadPoolExecutor(max_workers=4)
    try:
        futs = {ex.submit(_searx_one, h, query, n, categories, 7): h for h in hosts[:4]}
        for fut in as_completed(futs):
            try:
                got = fut.result()
            except Exception:
                continue
            if got:
                return got[:n]
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return []


def web_search(query, n=8):
    """Aggregate across engines and DEDUPE BY DOMAIN so sources are diverse.

    SearXNG (Brave+Google+DDG+Bing under the hood) first, DDG-HTML as fallback.
    Returns up to n results, at most 2 per domain, so 'cross-check' means
    genuinely different outlets.
    """
    pool = _searx_search(query, n + 6) or []
    if len(pool) < 3:
        seen_u = {u for _, u, _ in pool}
        pool += [r for r in _ddg_html_search(query, n + 6) if r[1] not in seen_u]
    out, per_dom, seen = [], {}, set()
    for title, url, snip in pool:
        if url in seen:
            continue
        d = _domain(url)
        if per_dom.get(d, 0) >= 2:          # cap 2 per outlet → diversity
            continue
        seen.add(url); per_dom[d] = per_dom.get(d, 0) + 1
        out.append((title, url, snip))
        if len(out) >= n:
            break
    return out


def video_search(query, n=6):
    """Find actual videos (title, page-url, thumbnail) via SearXNG video category."""
    rows = _searx_search(query, n, categories="videos")
    return [(t, u, s) for (t, u, s) in rows][:n]


_ARTICLE_TAGS = re.compile(r"(?is)<(script|style|nav|footer|header|form|aside|noscript|svg).*?</\1>")
_BLOCK = re.compile(r"(?is)</(p|div|li|h[1-6]|br|tr|section|article)>")


def web_fetch(url, limit=4000, timeout=6):
    """Fetch a page and pull readable body text (keeps paragraph breaks).
    Returns the body text only; titles for the activity feed come from the
    search results, so no extra request is made."""
    try:
        raw = _get(url, timeout=timeout).read().decode("utf-8", "ignore")
    except Exception:
        return ""
    raw = _ARTICLE_TAGS.sub(" ", raw)
    # Prefer <article>/<main> if present — that's usually the real story.
    m = re.search(r"(?is)<article[^>]*>(.*?)</article>", raw) or \
        re.search(r"(?is)<main[^>]*>(.*?)</main>", raw)
    body = m.group(1) if m else raw
    body = _BLOCK.sub("\n", body)
    text = re.sub(r"[ \t]+", " ", _strip(body))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text[:limit]




def image_search(query, n=6):
    """Images via SearXNG first, DDG i.js as fallback."""
    rows = _searx_search(query, n, categories="images")
    urls = []
    for _, u, _ in rows:
        if u.startswith("http"):
            urls.append(u)
    if urls:
        return urls[:n]
    try:
        page = _get("https://duckduckgo.com/?q=" + urllib.parse.quote(query) +
                    "&iax=images&ia=images").read().decode("utf-8", "ignore")
        m = re.search(r'vqd=["\']?([\d-]+)["\']?', page)
        if not m:
            return []
        api = ("https://duckduckgo.com/i.js?l=us-en&o=json&q=" + urllib.parse.quote(query) +
               "&vqd=" + m.group(1) + "&f=,,,&p=1")
        js = _get(api, headers={"User-Agent": UA, "Referer": "https://duckduckgo.com/"}
                  ).read().decode("utf-8", "ignore")
        return [r["image"] for r in json.loads(js).get("results", [])[:n] if r.get("image")]
    except Exception:
        return []


def download_image(url, timeout=25):
    try:
        hdr = {"User-Agent": UA, "Referer": "https://duckduckgo.com/", "Accept": "image/*"}
        resp = _get(url, timeout=timeout, headers=hdr)
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
               "image/webp": ".webp", "image/avif": ".avif"}.get(
                   resp.headers.get("Content-Type", "").split(";")[0].strip(), ".img")
        raw = resp.read()
        if not raw:
            return None
        tmp = CONFIG_DIR / (".img_" + str(abs(hash(url)) % 10**8) + ext)
        tmp.write_bytes(raw)
        return str(tmp)
    except Exception:
        return None

