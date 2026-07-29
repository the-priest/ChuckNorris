"""config.py — paths, tunables and the settings file.
Every other module reads its defaults from here, so there is exactly one place
to look when you want to know where something lives or what a limit is.
"""
import os
import json

import urllib.parse
import urllib.request
from pathlib import Path

from . import net as _net

APP_ID = "org.thepriest.chucknorris"
VERSION = "12.1.0"

DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4-Flash"
DEFAULT_VISION = "Qwen/Qwen2.5-VL-32B-Instruct"
DEFAULT_BASE = "https://api.siliconflow.com/v1"

DATA_DIR = Path.home() / ".local" / "share" / "chucknorris"
CONFIG_DIR = Path.home() / ".config" / "chucknorris"
CHATS_DIR = DATA_DIR / "chats"
DL_DIR = Path.home() / "Downloads" / "ChuckNorris"
VOICE_DIR = DATA_DIR / "voices"
SETTINGS = CONFIG_DIR / "settings.json"
BASILISK_SETTINGS = Path.home() / ".config" / "basilisk" / "settings.json"

for _d in (CONFIG_DIR, CHATS_DIR, DL_DIR, VOICE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── tunables (all overridable in Settings) ──────────────────────────────────
MAX_TOOL_HOPS = 4          # research: search→read→think rounds
RESEARCH_MAX_SOURCES = 3   # distinct pages read before answering
RESEARCH_QUERIES = 2       # distinct queries fanned out per hop
SEND_CHAR_BUDGET = 60_000  # conversation carried per turn (~15k tokens)
TOOL_BLOBS_KEPT = 2        # most recent tool-result blobs sent in full
CHAT_TTL_HOURS = 24        # saved chats self-delete this long after last use
RENDER_KEEP = 10           # message bubbles kept alive at the bottom
RENDER_PAGE = 20           # more revealed per scroll-up
FONT_SIZE = 14             # chat text size in px

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def load_settings():
    """Read settings.json, tolerating anything that isn't a JSON object.

    A corrupt-but-parseable file ([] or null) would otherwise sail past the
    except and blow up on the first .get() — at startup, so the app simply
    wouldn't launch.
    """
    s = {}
    if SETTINGS.exists():
        try:
            s = json.loads(SETTINGS.read_text())
        except Exception:
            s = {}
        if not isinstance(s, dict):
            s = {}
    if not s.get("siliconflow_api_key") and BASILISK_SETTINGS.exists():
        try:
            b = json.loads(BASILISK_SETTINGS.read_text())
            if isinstance(b, dict) and b.get("siliconflow_api_key"):
                s["siliconflow_api_key"] = b["siliconflow_api_key"]
        except Exception:
            pass
    return s


def save_settings(s):
    """Write settings 0600, atomically.

    This file holds the SiliconFlow API key in plaintext. write_text() creates
    with 0666 & ~umask — 0644 on a normal box — so the key was readable by every
    account on the machine. Writing then chmod'ing leaves a window where it is
    briefly world-readable, so the descriptor is opened 0600 from the start and
    renamed into place.
    """
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(CONFIG_DIR, 0o700)
        except OSError:
            pass
        tmp = SETTINGS.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(s, indent=2))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.replace(str(tmp), str(SETTINGS))
        os.chmod(SETTINGS, 0o600)
    except Exception:
        pass


def harden_existing_permissions():
    """Tighten anything already on disk from a previous version."""
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass
    for f in (SETTINGS,):
        try:
            if f.exists():
                os.chmod(f, 0o600)
        except OSError:
            pass


SETTINGS_DATA = load_settings()

# ── network ─────────────────────────────────────────────────────────────────
_ALLOWED_SCHEMES = ("http", "https")


def proxy():
    """The configured proxy, or "". One reader, so nothing can disagree."""
    return (SETTINGS_DATA.get("proxy") or "").strip()


def proxy_covers_api():
    """Whether the MODEL API goes through the proxy too.

    Defaults to True whenever a proxy is set, and that default is the honest
    one: someone who points this at Mullvad and then watches their prompts
    leave over the bare connection has been misled by their own settings panel.
    Page fetches were always proxied; the API call was not, which meant the
    single most identifying stream of traffic the app produces was the one
    thing the proxy never touched.
    """
    if not proxy():
        return False
    return bool(SETTINGS_DATA.get("proxy_api", True))


def _cache_ttl():
    try:
        v = float(SETTINGS_DATA.get("web_cache_seconds", _net.CACHE_TTL))
    except Exception:
        return _net.CACHE_TTL
    return max(0.0, min(600.0, v))


def get(url, data=None, timeout=20, headers=None, cache=False, max_bytes=None):
    """Open a URL. http/https only — urllib also speaks file: and ftp:, and a
    `file:///home/you/.ssh/id_rsa` fetch would quietly read a local secret into
    the conversation. Checked here so every caller inherits the restriction.

    Connections are pooled and kept alive (see net.py), so the second request to
    a host in the same turn skips DNS and the TLS handshake entirely. Pass
    cache=True for a plain page read: identical GETs inside the cache window are
    served from memory instead of the network, which is what makes a re-read of
    the same source during a multi-hop research turn free.
    """
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"blocked URL scheme {scheme!r} (only http/https allowed)")
    return _net.request(url, data=data, timeout=timeout,
                        headers=headers or {"User-Agent": UA},
                        proxy=proxy(),
                        cache_ttl=_cache_ttl() if cache else 0.0,
                        max_bytes=max_bytes)


def api_opener():
    """A urllib opener for the MODEL API, or None to use the plain default.

    Returning None in the ordinary case is deliberate: the default path stays
    exactly `urllib.request.urlopen`, which is what the startup tests drive and
    what has always been in use. The opener only appears when the user has
    actually asked for the API to be proxied.
    """
    if not proxy_covers_api():
        return None
    px = proxy()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": px, "https": px}))


def api_connect_host(base_url):
    """(host, port) worth warming before the first request.

    With the API proxied, the useful handshake to pre-open is the one to the
    PROXY — warming the API's own host would be both useless and, on a privacy
    setup, a direct connection to the one host the user asked not to contact
    directly.
    """
    if proxy_covers_api():
        p = urllib.parse.urlsplit(proxy() if "//" in proxy() else "//" + proxy())
        return (p.hostname, p.port or 8080) if p.hostname else (None, None)
    host = urllib.parse.urlparse(base_url).hostname
    return (host, 443) if host else (None, None)


def network_settings_changed():
    """Drop pooled sockets and cached bodies after a settings change.

    A connection opened before the proxy was switched on is still a direct
    connection; reusing it would quietly defeat the setting the user just
    changed. Cheap to rebuild, so it is always dropped rather than reasoned
    about.
    """
    _net.close_all()
    _net.cache_clear()
