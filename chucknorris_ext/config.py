"""config.py — paths, tunables and the settings file.
Every other module reads its defaults from here, so there is exactly one place
to look when you want to know where something lives or what a limit is.
"""
import json

import urllib.request
from pathlib import Path

APP_ID = "org.thepriest.chucknorris"
VERSION = "12.0.1"

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
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(s, indent=2))
    except Exception:
        pass


SETTINGS_DATA = load_settings()

# ── network ─────────────────────────────────────────────────────────────────
_ALLOWED_SCHEMES = ("http", "https")


def _opener():
    px = (SETTINGS_DATA.get("proxy") or "").strip()
    if px:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": px, "https": px}))
    return urllib.request.build_opener()


def get(url, data=None, timeout=20, headers=None):
    """Open a URL. http/https only — urllib also speaks file: and ftp:, and a
    `file:///home/you/.ssh/id_rsa` fetch would quietly read a local secret into
    the conversation. Checked here so every caller inherits the restriction."""
    import urllib.parse
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"blocked URL scheme {scheme!r} (only http/https allowed)")
    req = urllib.request.Request(url, data=data, headers=headers or {"User-Agent": UA})
    return _opener().open(req, timeout=timeout)
