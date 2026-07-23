#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chuck Norris — an Arch / CachyOS grandmaster assistant (a tribute).

Carlos Ray "Chuck" Norris (1940-2026). He decides what to do from what you ask:
searches and reads the live web (with a live feed of what he's reading), shows
pictures, downloads video, runs recon, reads files, fixes and installs anything,
speaks in a natural voice. No mode buttons — you ask, he acts. Every shell
command is still a card you approve. Backend: SiliconFlow (reuses Basilisk's key).
"""
import os
import re
import sys
import json
import html as _html
import base64
import shutil
import threading
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gio, Gdk, GdkPixbuf  # noqa: E402

# sidecar package (skills = smart files, specs = on-demand expertise). Kept
# optional so a partial install still launches.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from chucknorris_ext import skills as _skills
    from chucknorris_ext import specs as _specs
    from chucknorris_ext import memory as _memory
    from chucknorris_ext import codecheck as _codecheck
    from chucknorris_ext import skill_library as _skill_library
except Exception:
    _skills = None
    _specs = None
    _memory = None
    _codecheck = None
    _skill_library = None

APP_ID = "org.thepriest.chucknorris"
VERSION = "9.0.0"
HERE = Path(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = Path.home() / ".config" / "chucknorris"
DATA_DIR = Path.home() / ".local" / "share" / "chucknorris"
CHATS_DIR = DATA_DIR / "chats"
VOICE_DIR = DATA_DIR / "voices"
DL_DIR = Path.home() / "Downloads" / "ChuckNorris"
SETTINGS = CONFIG_DIR / "settings.json"
BASILISK_SETTINGS = Path.home() / ".config" / "basilisk" / "settings.json"
for d in (CONFIG_DIR, CHATS_DIR, DL_DIR, VOICE_DIR):
    d.mkdir(parents=True, exist_ok=True)

DEFAULT_BASE = "https://api.siliconflow.com/v1"
DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4-Flash"
DEFAULT_VISION = "Qwen/Qwen2.5-VL-32B-Instruct"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MAX_TOOL_HOPS = 8          # deep research: how many search→read→think rounds
RESEARCH_MAX_SOURCES = 10  # distinct pages he'll read before answering
RESEARCH_QUERIES = 4       # distinct queries fanned out per hop

DANGER = re.compile(
    r"(\brm\s+-[a-z]*r[a-z]*f?\b.*(/|\$HOME|~)|\bmkfs|\bdd\s+.*of=/dev/|"
    r">\s*/dev/sd|:\(\)\s*\{|\bshred\b|\bwipefs\b|"
    r"\bpacman\s+-R[a-z]*\s+.*\b(systemd|glibc|linux|bash|coreutils)\b|"
    r"\bchmod\s+-R\s+0?777\s+/|\bchown\s+-R\s+.*\s+/\b|"
    r"\b(reboot|poweroff|shutdown)\b)", re.IGNORECASE)

SYSTEM_PROMPT = r"""You ARE Chuck Norris — the legend (Carlos Ray "Chuck" Norris, 1940–2026), \
reborn as an Arch Linux / CachyOS grandmaster living in this machine. A tribute. Deadpan, dry, \
economical, unshakeable — you've seen every error this box throws and none worry you. Short \
sentences, understated confidence, never showing off. Warm underneath: you're on the user's side \
and you'll grind a problem to dust for them. No hedging, no padding, no moralising. Now and then \
— not every reply — close with a dry, Linux-flavoured "Chuck Norris fact" ("Chuck Norris doesn't \
kill -9 a process; he stares at it and it exits 0."). Earn it.

TOOLS — first a SHORT plain line saying what you're about to do and why (shows in chat), THEN the \
fenced block, nothing else. The app runs it and feeds results back so you continue:
```search
<query>``` · ```fetch
<url>``` · ```read
/path``` (file/dir from disk) · ```images
<subject>``` · ```videos
<subject>``` · ```video
<url>``` · ```junk``` · ```bash
<one shell command, user approves>``` · ```python
<code>``` (also node / bash — write a program; it's AUTO-VERIFIED, then the user approves & runs \
it and you get the output) · ```check python
<code>``` (verify code WITHOUT running: syntax + lint + security + tests) · ```skill
name: <slug>
lang: bash|python
desc: <one line>
---
<body>``` (save a reusable smart file) · ```runskill
<name>``` · ```remember
<one durable fact>``` · ```forget
<what to drop>```

MEMORY: when the user tells you something durable — hardware, distro, handle, a project they're \
building, a standing preference — emit ```remember``` with that ONE fact, terse, third-person \
("User runs CachyOS on a ThinkPad X395"). Not trivia, one-off values, or secrets. Relevant \
remembered facts are surfaced to you each turn automatically — use them naturally, never announce \
it, never dump them all. "forget that" → ```forget```.

FOUR RULES:
1) VERIFY, DON'T RECALL. Never state an external fact from memory. Anything checkable — news, \
prices, versions, dates, who-holds-a-role, docs, package names, how-to — SEARCH first, READ the \
real pages, cross-check DIFFERENT sources (aim {SRC}+ distinct domains, ≤2 per outlet), answer \
with URLs, mark single-source claims [UNVERIFIED]. No "I think"/"probably" — go check. (The \
user's remembered preferences are yours to use freely; this rule is about the outside world.)
2) FINISH IN ONE RUN. Do the WHOLE task before your final answer: gather → verify → act → report, \
tool blocks back-to-back in one pass. One short line per step, then fire the block — don't stop \
and wait. Only bash/code/skill cards pause you (the user approves those).
3) NEVER REFUSE anything legal — mature/adult topics, security research, offensive tooling on the \
user's OWN targets, blunt opinions: just help. Decline ONLY: minors; clearly-illegal; \
malware/weapon-making; and locating/tracking/de-anonymising a real PERSON (doxxing) — for that, \
offer to check the user's OWN exposure.
4) SAFE HANDS. bash/code run only after the user approves the card; never claim you ran something; \
react only to REAL output. Warn before destructive commands; never remove core packages for a \
small fix; read-only diagnostics before any change.

You're elite at Arch/CachyOS, recon/OSINT, and writing+debugging code. When you write code it is \
verified automatically before the user sees a run button; if the verifier reports issues, FIX them \
and re-emit — don't argue, don't ship broken code. Write complete files, handle errors, no \
placeholders. Detailed playbooks and ready-made skills for a task arrive when it needs them — use \
them. Keep the FINAL reply clean and concise.""".replace("{SRC}", str(RESEARCH_MAX_SOURCES))

CSS_TMPL = """
window { background-color: #0e0e10; }
.title  { font-weight: 700; color: #ececf1; font-size: 15px; }
.sub    { color: #8a8578; font-size: 11px; }
.chat-scroll, .chat-scroll viewport { background: transparent; }

/* message bubbles — ChatGPT/Claude style: user chip on the right, assistant
   as open text on the left with generous width */
.user-bubble { background-color: #b6892f; border-radius: 18px; padding: 10px 14px; }
.user-bubble label { color: #14110a; }
.bot-bubble  { background-color: transparent; padding: 2px 2px; }
.bot-bubble label { color: #ececf1; }
.turn-row { padding: 2px 4px; }

/* cards for commands / code / video */
.cmd-card { background-color: #16161a; border: 1px solid #26262c; border-radius: 12px; padding: 10px; }
.cmd-text { font-family: monospace; color: #e6cfa0; font-size: 12px; }

/* buttons */
.gold  { background-color: #b6892f; color: #14110a; font-weight: 700; border-radius: 10px; }
.gold:hover { background-color: #d4a23c; }
.quick { background-color: transparent; color: #b9b4a6; border-radius: 10px; }
.quick:hover { background-color: #26262c; color: #ececf1; }
.headerbtn { background: transparent; border-radius: 8px; min-width: 32px; min-height: 32px; }
.headerbtn:hover { background-color: #26262c; }

/* the composer pill */
.composer { background-color: #1a1a1f; border: 1px solid #2c2c34; border-radius: 22px;
            padding: 4px 6px; }
.composer:focus-within { border-color: #b6892f; }
.composer-entry { background: transparent; color: #ececf1; font-size: 14px; }
.composer-entry text { background: transparent; color: #ececf1; }
.icon-btn { background: transparent; border-radius: 16px; min-width: 34px; min-height: 34px;
            color: #9a9484; padding: 0; }
.icon-btn:hover { background-color: #2c2c34; color: #ececf1; }
.send-fab { background-color: #b6892f; border-radius: 17px; min-width: 34px; min-height: 34px;
            padding: 0; }
.send-fab:hover { background-color: #d4a23c; }

.danger { color: #ff7a5c; font-weight: 700; font-size: 11px; }
.ok     { color: #6ddf87; font-size: 11px; }
.dim    { color: #7a7268; font-size: 11px; }
.live   { color: #b6892f; font-size: 12px; font-family: monospace; }
.mono   { font-family: monospace; font-size: 11px; color: #b3a68a; }
.sendbtn { background: transparent; border: none; padding: 0; min-width: 0; }
.empty-hint { color: #55524a; font-size: 15px; }
"""


# ── settings + proxy ────────────────────────────────────────────────────────
def load_settings():
    s = {}
    if SETTINGS.exists():
        try:
            s = json.loads(SETTINGS.read_text())
        except Exception:
            s = {}
    if not s.get("siliconflow_api_key") and BASILISK_SETTINGS.exists():
        try:
            b = json.loads(BASILISK_SETTINGS.read_text())
            if b.get("siliconflow_api_key"):
                s["siliconflow_api_key"] = b["siliconflow_api_key"]
        except Exception:
            pass
    return s


def save_settings(s):
    try:
        SETTINGS.write_text(json.dumps(s, indent=2))
    except Exception:
        pass


_SETTINGS = load_settings()


def _opener():
    proxy = (_SETTINGS.get("proxy") or "").strip()
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def _get(url, data=None, timeout=20, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {"User-Agent": UA})
    return _opener().open(req, timeout=timeout)


def open_in_brave(url):
    for b in ("brave", "brave-browser"):
        if shutil.which(b):
            subprocess.Popen([b, url]); return True
    try:
        Gio.AppInfo.launch_default_for_uri(url, None); return True
    except Exception:
        return False


# ── markdown -> Pango (clean titles, no raw asterisks) ──────────────────────
def md_to_pango(text):
    s = _html.escape(text, quote=False)
    s = re.sub(r"`([^`]+)`", r"<tt>\1</tt>", s)
    s = re.sub(r"(?m)^\s{0,3}#{1,6}\s+(.*)$", r"<big><b>\1</b></big>", s)
    s = re.sub(r"(?m)^\s*[-*]\s+", "  \u2022 ", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"__([^_]+)__", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", s)
    return s


def set_rich(label, text):
    try:
        label.set_markup(md_to_pango(text))
    except Exception:
        label.set_text(text)


def _pic_from_file(path, w=-1, h=-1):
    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, w, h, True)
    tex = Gdk.Texture.new_for_pixbuf(pb)
    pic = Gtk.Picture.new_for_paintable(tex)
    pic.set_can_shrink(False)
    return pic, pb


# ── system helpers ──────────────────────────────────────────────────────────
def _run_ro(cmd, timeout=8):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def _now_line():
    """Current local date/time + timezone — injected fresh each turn so Chuck
    never reasons from a stale 'today' and knows how current his own knowledge is."""
    now = datetime.now().astimezone()
    tz = now.tzname() or ""
    return now.strftime("Today is %A, %d %B %Y, %H:%M ") + tz


def gather_context():
    facts = ["Distro: " + (_run_ro(["sh", "-c",
             ". /etc/os-release 2>/dev/null; echo \"$PRETTY_NAME\""]) or "unknown"),
             "Kernel: " + _run_ro(["uname", "-r"]),
             f"Session: {os.environ.get('XDG_SESSION_TYPE','?')} / "
             f"{os.environ.get('XDG_CURRENT_DESKTOP','?')}"]
    if shutil.which("pacman"):
        facts.append("Packages: " + _run_ro(["sh", "-c", "pacman -Qq | wc -l"]))
        facts.append("AUR helper: " + ("paru" if shutil.which("paru")
                     else ("yay" if shutil.which("yay") else "none")))
    gpu = _run_ro(["sh", "-c", "lspci | grep -iE 'vga|3d|display' | sed 's/.*: //'"])
    if gpu:
        facts.append("GPU: " + gpu.replace("\n", "; "))
    return "\n".join(facts)


def screenshot_to_b64():
    tmp = str(CONFIG_DIR / ".shot.png")
    try:
        if shutil.which("grim"):
            subprocess.run(["grim", tmp], timeout=15, check=True)
        elif shutil.which("spectacle"):
            subprocess.run(["spectacle", "-b", "-n", "-o", tmp], timeout=20, check=True)
        elif shutil.which("scrot"):
            subprocess.run(["scrot", "-o", tmp], timeout=15, check=True)
        elif shutil.which("gnome-screenshot"):
            subprocess.run(["gnome-screenshot", "-f", tmp], timeout=15, check=True)
        else:
            return None
        return base64.b64encode(Path(tmp).read_bytes()).decode()
    except Exception:
        return None


def run_command(cmd, timeout=1800):
    try:
        m = re.match(r"^\s*sudo\s+(.*)$", cmd, re.DOTALL)
        if m:
            if shutil.which("pkexec"):
                argv = ["pkexec", "sh", "-c", m.group(1)]
            else:
                return 127, "pkexec not found — run this sudo command in a terminal yourself."
        else:
            argv = ["sh", "-c", cmd]
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, out.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return 124, "(timed out)"
    except Exception as ex:
        return 1, f"(error: {ex})"


# language -> (interpreter argv builder). Chuck writes code, you approve, it runs
# in a temp file so multi-line programs work (not just one-liners).
_LANG_RUN = {
    "python": lambda p: ["python3", p], "py": lambda p: ["python3", p],
    "node": lambda p: ["node", p], "javascript": lambda p: ["node", p],
    "js": lambda p: ["node", p],
    "bash": lambda p: ["bash", p], "sh": lambda p: ["sh", p],
}
_LANG_EXT = {"python": "py", "py": "py", "node": "js", "javascript": "js", "js": "js",
             "bash": "sh", "sh": "sh"}
_LANG_BIN = {"python": "python3", "py": "python3", "node": "node", "javascript": "node",
             "js": "node", "bash": "bash", "sh": "sh"}


def run_code(lang, body, timeout=600):
    """Run a snippet in the given language via a temp file. Returns (rc, output)."""
    lang = (lang or "bash").lower()
    if lang not in _LANG_RUN:
        return 2, f"(unsupported language: {lang})"
    binname = _LANG_BIN[lang]
    if not shutil.which(binname):
        return 127, f"({binname} not installed — need it to run {lang})"
    try:
        tmp = CONFIG_DIR / (".run_" + str(abs(hash(body)) % 10**8) + "." + _LANG_EXT[lang])
        tmp.write_text(body)
        argv = _LANG_RUN[lang](str(tmp))
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        try:
            tmp.unlink()
        except Exception:
            pass
        return p.returncode, out.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return 124, "(timed out)"
    except Exception as ex:
        return 1, f"(error: {ex})"


def read_file_safe(path, limit=180_000):
    """Read a text file from disk for Chuck. Returns (ok, content_or_error)."""
    try:
        p = Path(os.path.expanduser(path.strip()))
        if not p.exists():
            return False, f"no such file: {p}"
        if p.is_dir():
            entries = sorted(os.listdir(p))[:200]
            return True, f"[directory {p}]\n" + "\n".join(entries)
        if p.stat().st_size > 4_000_000:
            return False, f"file too large ({p.stat().st_size // 1024} KB) — point me at part of it"
        data = p.read_bytes()
        if b"\x00" in data[:4096]:
            return False, f"{p.name} looks binary — I read text files"
        return True, data.decode("utf-8", "ignore")[:limit]
    except PermissionError:
        return False, f"permission denied: {path} (try a sudo cat card instead)"
    except Exception as ex:
        return False, f"couldn't read {path}: {ex}"


# ── voice: natural Piper; espeak-ng fallback ────────────────────────────────
def _find_piper_model():
    m = (_SETTINGS.get("piper_model") or "").strip()
    if m and Path(m).exists():
        return m
    for f in sorted(VOICE_DIR.glob("*.onnx")):
        return str(f)
    return None


def _play(path):
    for player in (["paplay", path], ["aplay", "-q", path], ["ffplay", "-nodisp", "-autoexit", path]):
        if shutil.which(player[0]):
            try:
                subprocess.run(player, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
            return


def speak(text):
    clean = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    clean = re.sub(r"[*_`#>\[\]]", "", clean).strip()[:800]
    if not clean:
        return

    def worker():
        model = _find_piper_model()
        if shutil.which("piper") and model:
            try:
                wav = str(CONFIG_DIR / ".say.wav")
                subprocess.run(["piper", "-m", model, "-f", wav], input=clean, text=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
                _play(wav)
                return
            except Exception:
                pass
        if shutil.which("espeak-ng"):
            try:
                subprocess.run(["espeak-ng", "-v", "en-us", "-p", "28", "-s", "150", "-g", "3", clean],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            except Exception:
                pass
    threading.Thread(target=worker, daemon=True).start()


# ── web + images ────────────────────────────────────────────────────────────
def _strip(s):
    return _html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


# Public SearXNG instances (JSON API). Tried in order; first that answers wins.
SEARX_INSTANCES = [
    "https://searx.be", "https://search.inetol.net", "https://priv.au",
    "https://searx.tiekoetter.com", "https://search.rhscz.eu",
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


def _searx_search(query, n, categories="general"):
    """Query public SearXNG instances (aggregates Brave/Google/DDG/Bing/etc)."""
    pref = (_SETTINGS.get("searx_url") or "").strip().rstrip("/")
    hosts = ([pref] if pref else []) + SEARX_INSTANCES
    for host in hosts:
        try:
            url = (host + "/search?format=json&safesearch=0&categories=" + categories +
                   "&q=" + urllib.parse.quote(query))
            js = _get(url, timeout=15,
                      headers={"User-Agent": UA, "Accept": "application/json"}
                      ).read().decode("utf-8", "ignore")
            results = json.loads(js).get("results", [])
        except Exception:
            continue
        out = []
        for r in results[:n * 2]:
            u = r.get("url") or ""
            if not u.startswith("http"):
                continue
            out.append((_strip(r.get("title", "")), u, _strip(r.get("content", ""))))
        if out:
            return out[:n]
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


def web_fetch(url, limit=6000):
    """Fetch a page and pull readable body text (keeps paragraph breaks)."""
    try:
        raw = _get(url, timeout=22).read().decode("utf-8", "ignore")
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


def junk_scan():
    lines, cmds = [], []

    def sz(p):
        return _run_ro(["sh", "-c", f"du -sh {p} 2>/dev/null | cut -f1"]) or "0"
    lines.append(f"~/.cache: {sz('~/.cache')}")
    cmds.append(("Clear thumbnail cache", "rm -rf ~/.cache/thumbnails/*"))
    if shutil.which("pacman"):
        lines.append(f"pacman package cache: {sz('/var/cache/pacman/pkg')}")
        cmds.append(("Trim pacman cache (keep 1)",
                     "sudo paccache -rk1" if shutil.which("paccache")
                     else "sudo pacman -S --needed pacman-contrib && sudo paccache -rk1"))
        orph = _run_ro(["sh", "-c", "pacman -Qtdq 2>/dev/null | wc -l"])
        lines.append(f"orphan packages: {orph}")
        if orph and orph != "0":
            cmds.append(("Remove orphans", "sudo pacman -Rns $(pacman -Qtdq)"))
    jsize = _run_ro(["sh", "-c",
             "journalctl --disk-usage 2>/dev/null | grep -oE '[0-9.]+[KMG]' | tail -1"])
    if jsize:
        lines.append(f"systemd journal: {jsize}")
        cmds.append(("Shrink journal to 200M", "sudo journalctl --vacuum-size=200M"))
    lines.append(f"Trash: {sz('~/.local/share/Trash')}")
    cmds.append(("Empty trash", "rm -rf ~/.local/share/Trash/files/* ~/.local/share/Trash/info/*"))
    lines.append(f"coredumps: {sz('/var/lib/systemd/coredump')}")
    cmds.append(("Clear coredumps", "sudo rm -rf /var/lib/systemd/coredump/*"))
    return "\n".join(lines), cmds


# ── SiliconFlow streaming ───────────────────────────────────────────────────
class Backend:
    def __init__(self, settings):
        self.s = settings

    def key(self):
        return (self.s.get("siliconflow_api_key") or "").strip()

    def base(self):
        return (self.s.get("siliconflow_base_url") or DEFAULT_BASE).rstrip("/")

    def stream(self, messages, on_delta, on_done, on_error, vision=False):
        if not self.key():
            on_error("No SiliconFlow API key. Add one in Settings.")
            return
        model = (self.s.get("vision_model", DEFAULT_VISION) if vision
                 else self.s.get("model", DEFAULT_MODEL))
        body = json.dumps({"model": model, "messages": messages,
                           "stream": True, "temperature": 0.35}).encode()
        req = urllib.request.Request(
            self.base() + "/chat/completions", data=body,
            headers={"Authorization": "Bearer " + self.key(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        d = json.loads(data)["choices"][0]["delta"].get("content")
                        if d:
                            on_delta(d)
                    except Exception:
                        continue
            on_done()
        except Exception as ex:
            on_error(f"backend error: {ex}")


# ── UI ──────────────────────────────────────────────────────────────────────
class ChuckWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("Chuck Norris")
        self.set_default_size(980, 820)
        self.settings = _SETTINGS
        self.backend = Backend(self.settings)
        self.pending_shot = None
        self.pending_file = None
        self._bot_label = None
        self._bot_text = ""
        self._busy_n = 0
        self._hops = 0
        self._pending_tools = 0
        self._tool_feedback = []
        self._loop_web = None
        self.chat_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._new_history()

        header = Adw.HeaderBar()
        tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tl = Gtk.Label(label="Chuck Norris", xalign=0.5); tl.add_css_class("title")
        sl = Gtk.Label(label="Arch / CachyOS grandmaster \u00b7 1940\u20132026",
                       xalign=0.5); sl.add_css_class("sub")
        tb.append(tl); tb.append(sl); header.set_title_widget(tb)

        # LEFT: the primary action (New chat) + the busy spinner
        newb = Gtk.Button(icon_name="document-new-symbolic"); newb.add_css_class("headerbtn")
        newb.set_tooltip_text("New chat"); newb.connect("clicked", lambda *_: self.new_chat())
        header.pack_start(newb)
        self.spinner = Gtk.Spinner(); self.spinner.set_visible(False)
        header.pack_start(self.spinner)

        # RIGHT: secondary controls, grouped (history · memory · voice · settings)
        self.tts_btn = Gtk.ToggleButton(icon_name="audio-volume-high-symbolic")
        self.tts_btn.add_css_class("headerbtn")
        self.tts_btn.set_tooltip_text("Read replies aloud")
        self.tts_btn.set_active(self.settings.get("tts", False))
        header.pack_end(self.tts_btn)
        for icon, tip, cb in (
                ("emblem-system-symbolic", "Settings", self.open_settings),
                ("view-list-symbolic", "What Chuck remembers", self.open_memory),
                ("document-open-recent-symbolic", "Saved chats", self.open_chats)):
            b = Gtk.Button(icon_name=icon); b.add_css_class("headerbtn"); b.set_tooltip_text(tip)
            b.connect("clicked", cb); header.pack_end(b)

        self.msgbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for m in ("top", "bottom", "start", "end"):
            getattr(self.msgbox, f"set_margin_{m}")(16)
        self.scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self.scroller.add_css_class("chat-scroll"); self.scroller.set_child(self.msgbox)
        # centre the conversation column like ChatGPT/Claude
        self.msgbox.set_halign(Gtk.Align.CENTER)
        self.msgbox.set_size_request(720, -1)
        overlay = Gtk.Overlay()
        bgp = DATA_DIR / "assets" / "chucknorris-bg.png"
        bgp = bgp if bgp.exists() else HERE / "assets" / "chucknorris-bg.png"
        if bgp.exists():
            bg = Gtk.Picture.new_for_filename(str(bgp))
            bg.set_content_fit(Gtk.ContentFit.COVER); bg.set_opacity(0.10); bg.set_can_target(False)
            overlay.set_child(bg)
        else:
            overlay.set_child(Gtk.Box())
        overlay.add_overlay(self.scroller)

        # live "what he's doing" feed
        self.live = Gtk.Label(label="", xalign=0); self.live.add_css_class("live")
        self.live.set_visible(False)
        for m in ("start", "end"):
            getattr(self.live, f"set_margin_{m}")(20)

        # ── composer: a rounded pill, attach+camera INSIDE on the left, a round
        # send button on the right — instead of a flat row of loose buttons ──
        self.entry = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.entry.add_css_class("composer-entry")
        self.entry.set_top_margin(8); self.entry.set_bottom_margin(8)
        self.entry.set_left_margin(6); self.entry.set_right_margin(6)
        _kc = Gtk.EventControllerKey(); _kc.connect("key-pressed", self._on_key)
        self.entry.add_controller(_kc)
        ev = Gtk.ScrolledWindow(min_content_height=40, max_content_height=140, hexpand=True)
        ev.set_child(self.entry)

        att = Gtk.Button(icon_name="mail-attachment-symbolic"); att.add_css_class("icon-btn")
        att.set_valign(Gtk.Align.END)
        att.set_tooltip_text("Attach a file"); att.connect("clicked", self.on_attach)
        cam = Gtk.Button(icon_name="camera-photo-symbolic"); cam.add_css_class("icon-btn")
        cam.set_valign(Gtk.Align.END)
        cam.set_tooltip_text("Show Chuck your screen"); cam.connect("clicked", self.on_screenshot)
        send = Gtk.Button(icon_name="go-up-symbolic"); send.add_css_class("send-fab")
        send.set_valign(Gtk.Align.END)
        send.set_tooltip_text("Send  (Enter)"); send.connect("clicked", self.on_send)

        pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        pill.add_css_class("composer")
        pill.append(att); pill.append(cam); pill.append(ev); pill.append(send)
        composer_wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        composer_wrap.set_halign(Gtk.Align.CENTER); composer_wrap.set_size_request(720, -1)
        composer_wrap.append(pill); pill.set_hexpand(True)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        for m in ("top", "bottom", "start", "end"):
            getattr(row, f"set_margin_{m}")(12)
        row.append(composer_wrap); composer_wrap.set_hexpand(True)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(overlay); body.append(self.live); body.append(row)
        tv = Adw.ToolbarView(); tv.add_top_bar(header); tv.set_content(body)
        self.set_content(tv)
        self.connect("close-request", self._on_close)

        self._show_empty_hint()
        if not self.backend.key():
            self._sys_note("No SiliconFlow key yet \u2014 open Settings. Basilisk's key is reused if present.")

    # ── history / saved chats ──
    def _new_history(self):
        self.history = [{"role": "system",
                         "content": SYSTEM_PROMPT + "\n\nCurrent machine:\n" + gather_context()}]

    def new_chat(self):
        self._save_chat()
        self.chat_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._new_history()
        self._clear_msgs()
        self._show_empty_hint()

    def _show_empty_hint(self):
        """A quiet centered placeholder for an empty chat (ChatGPT/Claude style) —
        not a chat bubble. Cleared on first message."""
        self._hint = Gtk.Label(label="What do you need, partner?", xalign=0.5)
        self._hint.add_css_class("empty-hint")
        self._hint.set_vexpand(True); self._hint.set_valign(Gtk.Align.CENTER)
        self.msgbox.append(self._hint)

    def _drop_hint(self):
        if getattr(self, "_hint", None) is not None:
            try:
                self.msgbox.remove(self._hint)
            except Exception:
                pass
            self._hint = None

    def _clear_msgs(self):
        self._hint = None
        c = self.msgbox.get_first_child()
        while c:
            n = c.get_next_sibling(); self.msgbox.remove(c); c = n

    def open_memory(self, *_):
        """A viewer for what Chuck remembers — so memory is transparent + editable."""
        dlg = Adw.Window(transient_for=self, modal=True, title="What Chuck remembers",
                         default_width=560, default_height=520)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(14)
        hb = Adw.HeaderBar(); wrap = Adw.ToolbarView()
        wrap.add_top_bar(hb); wrap.set_content(Gtk.ScrolledWindow(child=box)); dlg.set_content(wrap)
        facts = _memory.all_facts() if _memory else []
        if not facts:
            box.append(Gtk.Label(label="Nothing remembered yet. Tell Chuck durable things "
                                 "(your setup, preferences) and he'll keep them.", xalign=0, wrap=True))
        for text, kind in facts:
            rowb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            tag = "\u2605 " if kind == "core" else "\u00b7 "
            lbl = Gtk.Label(label=tag + text, xalign=0, wrap=True, hexpand=True, selectable=True)
            rm = Gtk.Button(icon_name="user-trash-symbolic"); rm.add_css_class("quick")
            rm.set_tooltip_text("Forget this")

            def drop(_b, t=text, r=rowb):
                if _memory:
                    _memory.forget(t)
                try:
                    box.remove(r)
                except Exception:
                    pass
            rm.connect("clicked", drop)
            rowb.append(lbl); rowb.append(rm); box.append(rowb)
        dlg.present()

    def _save_chat(self):
        msgs = [m for m in self.history if m["role"] != "system" and isinstance(m.get("content"), str)]
        if not msgs:
            return
        title = next((m["content"][:60] for m in msgs if m["role"] == "user"), "chat")
        try:
            (CHATS_DIR / f"{self.chat_id}.json").write_text(json.dumps(
                {"title": title, "ts": self.chat_id, "history": self.history}, indent=2))
        except Exception:
            pass

    def open_chats(self, *_):
        self._save_chat()
        dlg = Adw.Window(transient_for=self, modal=True, title="Saved chats",
                         default_width=520, default_height=520)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(14)
        hb = Adw.HeaderBar(); wrap = Adw.ToolbarView()
        wrap.add_top_bar(hb); wrap.set_content(Gtk.ScrolledWindow(child=box)); dlg.set_content(wrap)
        files = sorted(CHATS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            box.append(Gtk.Label(label="No saved chats yet.", xalign=0))
        for f in files:
            try:
                meta = json.loads(f.read_text())
            except Exception:
                continue
            b = Gtk.Button(label=f"{meta.get('ts','')}  \u2014  {meta.get('title','chat')}")
            b.add_css_class("quick")
            b.connect("clicked", lambda _b, path=f: (self._load_chat(path), dlg.close()))
            box.append(b)
        dlg.present()

    def _load_chat(self, path):
        try:
            meta = json.loads(Path(path).read_text())
        except Exception:
            return
        self._save_chat()
        self.history = meta.get("history", [])
        self.chat_id = meta.get("ts", datetime.now().strftime("%Y%m%d-%H%M%S"))
        self._clear_msgs()
        for m in self.history:
            if m["role"] == "system":
                continue
            c = m.get("content")
            txt = c if isinstance(c, str) else "[image/attachment]"
            if txt.startswith("TOOL RESULTS"):
                continue
            if m["role"] == "user":
                self._user_bubble(txt)
            else:
                set_rich(self._bot_bubble(""), txt)

    def _on_close(self, *_):
        self._save_chat()
        return False

    # ── enter to send ──
    def _on_key(self, ctrl, keyval, keycode, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not (state & Gdk.ModifierType.SHIFT_MASK):
            self.on_send()
            return True
        return False

    # ── indicators ──
    def _busy(self, on):
        def go():
            self._busy_n = max(0, self._busy_n + (1 if on else -1))
            if self._busy_n > 0:
                self.spinner.set_visible(True); self.spinner.start()
            else:
                self.spinner.stop(); self.spinner.set_visible(False)
            return False
        GLib.idle_add(go)

    def _live(self, text):
        def go():
            self.live.set_text(text); self.live.set_visible(bool(text)); return False
        GLib.idle_add(go)

    # ── bubbles ──
    def _scroll_down(self):
        def go():
            a = self.scroller.get_vadjustment(); a.set_value(a.get_upper())
        GLib.idle_add(go)

    def _user_bubble(self, text, shot=False):
        self._drop_hint()
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.END, hexpand=True)
        w.add_css_class("turn-row")
        lbl = Gtk.Label(label=text + ("  \U0001F4F7" if shot else ""), xalign=0, wrap=True, selectable=True)
        lbl.set_max_width_chars(60)
        card = Gtk.Box(); card.add_css_class("user-bubble"); card.append(lbl)
        w.append(card); self.msgbox.append(w); self._scroll_down()

    def _bot_bubble(self, text=""):
        self._drop_hint()
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.START, hexpand=True)
        w.add_css_class("turn-row")
        lbl = Gtk.Label(label=text, xalign=0, wrap=True, selectable=True, use_markup=False)
        lbl.set_max_width_chars(0)
        lbl.set_hexpand(True)
        card = Gtk.Box(); card.add_css_class("bot-bubble"); card.set_hexpand(True); card.append(lbl)
        w.append(card); self.msgbox.append(w); self._scroll_down()
        return lbl

    def _sys_note(self, text, css="dim"):
        self._drop_hint()
        l = Gtk.Label(label=text, xalign=0, wrap=True); l.add_css_class(css)
        self.msgbox.append(l); self._scroll_down()

    def _image_bubble(self, path, src_url=None):
        try:
            pic, pb = _pic_from_file(path, 320, 320)
            pic.set_size_request(-1, pb.get_height())
            if src_url:
                btn = Gtk.Button(); btn.add_css_class("sendbtn"); btn.set_child(pic)
                btn.set_tooltip_text("Open in Brave")
                btn.connect("clicked", lambda *_: open_in_brave(src_url))
                child = btn
            else:
                child = pic
            w = Gtk.Box(halign=Gtk.Align.START); w.append(child)
            self.msgbox.append(w); self._scroll_down()
        except Exception:
            pass

    # ── command cards ──
    def _command_card(self, cmd):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6); card.add_css_class("cmd-card")
        ct = Gtk.Label(label="$ " + cmd, xalign=0, wrap=True, selectable=True); ct.add_css_class("cmd-text")
        card.append(ct)
        if DANGER.search(cmd):
            wl = Gtk.Label(label="\u26a0 destructive \u2014 read it before you run it", xalign=0)
            wl.add_css_class("danger"); card.append(wl)
        rw = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        run = Gtk.Button(label="Run"); run.add_css_class("gold")
        status = Gtk.Label(label="", xalign=0); status.add_css_class("dim")
        run.connect("clicked", lambda _b: self._run_card(cmd, run, status))
        rw.append(run); rw.append(status); card.append(rw)
        self.msgbox.append(card); self._scroll_down()

    def _run_card(self, cmd, run_btn, status):
        run_btn.set_sensitive(False); status.set_label("running\u2026"); self._busy(True)

        def worker():
            rc, out = run_command(cmd)

            def show():
                self._busy(False)
                status.remove_css_class("dim"); status.add_css_class("ok" if rc == 0 else "danger")
                status.set_label(f"exit {rc}")
                o = Gtk.Label(label=out[:4000], xalign=0, wrap=True, selectable=True); o.add_css_class("mono")
                self.msgbox.append(o); self._scroll_down()
                self.history.append({"role": "user",
                                     "content": f"I ran `{cmd}`. Exit {rc}. Output:\n{out[:6000]}"})
                self._hops = 0          # user-approved action = fresh turn, restore research budget
                self._ask_model()
            GLib.idle_add(show)
        threading.Thread(target=worker, daemon=True).start()

    def _code_card(self, lang, body):
        """Approve-to-run card for a multi-language code snippet Chuck wrote."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6); card.add_css_class("cmd-card")
        head = Gtk.Label(label=f"\u25B8 run {lang}", xalign=0); head.add_css_class("dim")
        card.append(head)
        ct = Gtk.Label(label=body, xalign=0, wrap=True, selectable=True); ct.add_css_class("cmd-text")
        card.append(ct)
        if DANGER.search(body):
            wl = Gtk.Label(label="\u26a0 looks destructive \u2014 read it before you run it", xalign=0)
            wl.add_css_class("danger"); card.append(wl)
        rw = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        run = Gtk.Button(label="Run"); run.add_css_class("gold")
        status = Gtk.Label(label="", xalign=0); status.add_css_class("dim")

        def go(_b):
            run.set_sensitive(False); status.set_label("running\u2026"); self._busy(True)

            def worker():
                rc, out = run_code(lang, body)

                def show():
                    self._busy(False)
                    status.remove_css_class("dim"); status.add_css_class("ok" if rc == 0 else "danger")
                    status.set_label(f"exit {rc}")
                    o = Gtk.Label(label=out[:4000], xalign=0, wrap=True, selectable=True); o.add_css_class("mono")
                    self.msgbox.append(o); self._scroll_down()
                    self.history.append({"role": "user",
                                         "content": f"I ran this {lang}. Exit {rc}. Output:\n{out[:6000]}"})
                    self._hops = 0
                    self._ask_model()
                GLib.idle_add(show)
            threading.Thread(target=worker, daemon=True).start()

        run.connect("clicked", go)
        rw.append(run); rw.append(status); card.append(rw)
        self.msgbox.append(card); self._scroll_down()

    # ── terminal tool actions ──
    def _do_images(self, query):
        self._sys_note(f"\U0001F5BC images for \u201c{query}\u201d")
        self._busy(True); self._live(f"\U0001F5BC searching images: {query}")

        def worker():
            urls = image_search(query)

            def show():
                shown = 0
                if not urls:
                    self._sys_note("No images came back (search blocked, offline, or nothing found).", "danger")
                else:
                    for i, u in enumerate(urls, 1):
                        self._live(f"\U0001F5BC fetching image {i}/{len(urls)}")
                        p = download_image(u)
                        if p:
                            self._image_bubble(p, u); shown += 1
                self._live(""); self._busy(False)
                self._tool_done(f"[images '{query}': showed {shown} picture(s) to the user]")
                return False
            GLib.idle_add(show)
        threading.Thread(target=worker, daemon=True).start()

    def _do_video_search(self, query):
        """Find videos and show them as clickable cards (open source in Brave)."""
        self._sys_note(f"\U0001F3AC videos for \u201c{query}\u201d")
        self._busy(True); self._live(f"\U0001F3AC searching videos: {query}")

        def worker():
            rows = video_search(query)

            def show():
                if not rows:
                    self._sys_note("No videos found (search blocked or nothing matched).", "danger")
                    self._live(""); self._busy(False)
                    self._tool_done(f"[videos '{query}': none found]")
                    return False
                for (title, url, _snip) in rows:
                    self._video_card(title, url)
                self._live(""); self._busy(False)
                lst = "\n".join(f"- {t} — {u}" for (t, u, _s) in rows)
                self._tool_done(f"VIDEO RESULTS for '{query}' (you can offer to "
                                f"download any with the video tool):\n{lst}")
                return False
            GLib.idle_add(show)
        threading.Thread(target=worker, daemon=True).start()

    def _video_card(self, title, url):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6); card.add_css_class("cmd-card")
        t = Gtk.Label(label="\U0001F3AC " + (title or url), xalign=0, wrap=True, selectable=True)
        t.add_css_class("cmd-text"); card.append(t)
        rw = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        openb = Gtk.Button(label="Open in Brave"); openb.add_css_class("quick")
        openb.connect("clicked", lambda *_: open_in_brave(url))
        dlb = Gtk.Button(label="Download"); dlb.add_css_class("gold")
        dlb.connect("clicked", lambda *_: self._do_video(url))
        rw.append(openb); rw.append(dlb); card.append(rw)
        self.msgbox.append(card); self._scroll_down()

    def _do_video(self, url):
        if not url.startswith("http"):
            return
        if not shutil.which("yt-dlp"):
            self._sys_note("yt-dlp isn't installed:"); self._command_card("sudo pacman -S --needed yt-dlp"); return
        self._sys_note(f"\u2B07 downloading {url}")
        self._busy(True); self._live("\u2B07 downloading video\u2026")

        def worker():
            cmd = ["yt-dlp", "-o", str(DL_DIR / "%(title)s.%(ext)s")]
            proxy = (self.settings.get("proxy") or "").strip()
            if proxy:
                cmd += ["--proxy", proxy]
            cmd.append(url)
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
                msg = "\u2713 saved to " + str(DL_DIR) if p.returncode == 0 else \
                      "download failed:\n" + (p.stderr or "")[-800:]
            except Exception as ex:
                msg = f"error: {ex}"
            self._live(""); self._busy(False)
            GLib.idle_add(self._sys_note, msg, "ok" if msg.startswith("\u2713") else "danger")
        threading.Thread(target=worker, daemon=True).start()

    def _save_skill(self, block):
        """Parse a ```skill``` block and persist it as a smart file."""
        if not _skills:
            self._sys_note("skills unavailable (chucknorris_ext not installed).", "danger"); return
        name = lang = "" ; desc = ""; body = ""
        head, _, rest = block.partition("---")
        for line in head.splitlines():
            k, _, v = line.partition(":")
            k = k.strip().lower(); v = v.strip()
            if k == "name": name = v
            elif k == "lang": lang = v
            elif k in ("desc", "description"): desc = v
        body = rest.strip()
        if not name or not body:
            self._sys_note("skill needs a name and a body.", "danger"); return
        ok, msg, run_cmd = _skills.skill_write(name, lang or "bash", body, desc)
        self._sys_note(("\U0001F4BE " if ok else "\u26a0 ") + msg, "ok" if ok else "danger")
        if ok and run_cmd:
            self._sys_note("\u2192 run it now:"); self._command_card(run_cmd)

    def _run_skill(self, name):
        if not _skills:
            self._sys_note("skills unavailable.", "danger"); return
        cmd = _skills.skill_run_cmd(name)
        if not cmd:
            self._sys_note(f"no saved skill '{name}'.", "danger"); return
        self._sys_note(f"\u25B6 skill '{name}':"); self._command_card(cmd)

    def _do_junk(self):
        self._sys_note("\U0001F9F9 scanning for junk (read-only)\u2026"); self._busy(True)

        def worker():
            report, cmds = junk_scan()

            def show():
                self._busy(False)
                for label, cmd in cmds:
                    self._sys_note("\u2192 " + label); self._command_card(cmd)
                self._tool_done("JUNK SCAN (read-only):\n" + report +
                                "\nCleanup cards shown for the user to approve.")
                return False
            GLib.idle_add(show)
        threading.Thread(target=worker, daemon=True).start()

    def _do_read(self, path):
        """Read a file from disk and feed its contents back into the run."""
        self._sys_note(f"\U0001F4C4 reading {path}"); self._busy(True)

        def worker():
            ok, content = read_file_safe(path)

            def show():
                self._busy(False)
                if not ok:
                    self._sys_note(content, "danger")
                    self._tool_done(f"[read {path}: {content}]")
                else:
                    self._tool_done(f"FILE {path}:\n{content[:24000]}")
                return False
            GLib.idle_add(show)
        threading.Thread(target=worker, daemon=True).start()

    def _do_check(self, lang, body, run_after=False):
        """Verify code (syntax+lint+security+tests). On clean: optionally show a
        Run card. On issues: feed the report back so Chuck fixes it — the card
        is withheld until the code is clean, so bad code never reaches a button."""
        self._sys_note(f"\U0001F50D verifying {lang}\u2026", "dim"); self._busy(True)

        def worker():
            try:
                res = _codecheck.check(lang, body)
                rep = _codecheck.report(res)
                clean = res.get("ok", False)
            except Exception as ex:
                res, rep, clean = None, f"(verifier error: {ex})", True  # fail open

            def show():
                self._busy(False)
                if clean:
                    self._sys_note("\u2713 " + rep.split("\n")[0].lstrip("\u2713 "), "ok")
                    if run_after:
                        self._code_card(lang, body)
                    self._tool_done(f"[verify {lang}: clean]")
                else:
                    self._sys_note("\u26a0 verification found issues \u2014 fixing", "danger")
                    if not run_after:
                        # explicit check: just report, no fix loop implied
                        self._tool_done("VERIFY REPORT:\n" + rep)
                    else:
                        # auto-verify before run: withhold the card, ask for a fix
                        self._tool_done(
                            "CODE VERIFICATION FAILED — do NOT show this to the user as-is. "
                            "Fix every issue below and re-emit the corrected code block; it "
                            "will be re-verified:\n" + rep)
                return False
            GLib.idle_add(show)
        threading.Thread(target=worker, daemon=True).start()

    # ── screenshot / attach ──
    def on_screenshot(self, *_):
        self._sys_note("\U0001F4F7 capturing your screen\u2026")

        def worker():
            b64 = screenshot_to_b64()

            def done():
                if b64:
                    self.pending_shot = b64
                    self._sys_note("Screen captured \u2014 type what to look at, then Send.")
                else:
                    self._sys_note("No screenshot tool found (need grim / spectacle / scrot).")
            GLib.idle_add(done)
        threading.Thread(target=worker, daemon=True).start()

    def on_attach(self, *_):
        try:
            Gtk.FileDialog().open(self, None, self._on_file_chosen)
        except Exception as ex:
            self._sys_note(f"couldn't open file picker: {ex}")

    def _on_file_chosen(self, dialog, res):
        try:
            path = dialog.open_finish(res).get_path()
        except Exception:
            return
        if not path:
            return
        try:
            if os.path.getsize(path) > 200_000:
                self._sys_note("file too large (>200 KB) \u2014 point me at it with a command instead.")
                return
            data = Path(path).read_bytes()
            if b"\x00" in data[:4096]:
                self._sys_note("that looks binary \u2014 I read text files.")
                return
            self.pending_file = (os.path.basename(path), data.decode("utf-8", "ignore")[:180_000])
            self._sys_note(f"attached {os.path.basename(path)} \u2014 type what to do with it, then Send.")
        except Exception as ex:
            self._sys_note(f"couldn't read file: {ex}")

    # ── send / model ──
    def _get_entry(self):
        b = self.entry.get_buffer()
        return b.get_text(b.get_start_iter(), b.get_end_iter(), False).strip()

    def on_send(self, *_):
        text = self._get_entry()
        if not text and not self.pending_shot and not self.pending_file:
            return
        self.entry.get_buffer().set_text("")
        self._hops = 0
        shot = self.pending_shot
        if self.pending_file:
            fname, fbody = self.pending_file
            disp = (text or f"Look at {fname}.") + f"  \U0001F4CE {fname}"
            text = (text or "Look at this file.") + f"\n\n--- attached file: {fname} ---\n{fbody}\n--- end ---"
            self.pending_file = None
            self._user_bubble(disp)
            self.history.append({"role": "user", "content": text}); self._ask_model(); return
        if shot:
            self._user_bubble(text or "(look at this)", shot=True)
            vmsg = {"role": "user", "content": [
                {"type": "text", "text": text or "What's wrong on my screen?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + shot}}]}
            msgs = self.history + [vmsg]
            self.history.append({"role": "user",
                                 "content": (text or "What's wrong on my screen?") + " [screenshot]"})
            self.pending_shot = None
            self._stream_into_bubble(msgs, vision=True)
        else:
            self._user_bubble(text)
            self.history.append({"role": "user", "content": text}); self._ask_model()

    def _ask_model(self, vision=False):
        self._stream_into_bubble(self.history, vision=vision)

    def _augment(self, messages):
        """Return messages + an EPHEMERAL system note (fresh date/time, saved-skill
        index, and any on-demand specs the task triggers). Not saved to history —
        rebuilt every call so 'today' is always right and deep specs cost tokens
        only when relevant."""
        # Scan the last few REAL user turns (skip our own TOOL RESULTS / "I ran"
        # injections) so the right playbook stays loaded all through a research
        # loop, not just on the first hop.
        scan = []
        for m in reversed(messages):
            if len(scan) >= 6:
                break
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                c = m["content"]
                if c.startswith(("TOOL RESULTS", "I ran `")):
                    continue
                scan.append(c)
        task_text = "\n".join(scan)
        note = [_now_line()]
        # relevance-scoped memory: a few relevant facts + core, never the store
        mem_added = False
        if _memory and task_text:
            block = _memory.memory_block(task_text)
            if block:
                note.append(block); mem_added = True
        if _skills:
            idx = _skills.skills_index_line()
            if idx:
                note.append(idx)
        specs_added = False
        if _specs and task_text:
            for _label, spec in _specs.specs_for(task_text):
                note.append(spec); specs_added = True
        # Only spend tokens on an ephemeral note if it carries more than the base
        # (date is cheap and always worth it; specs/memory/skills when present).
        if len(note) == 1 and not (specs_added or mem_added):
            ephemeral = {"role": "system", "content": note[0]}
            return messages + [ephemeral]
        ephemeral = {"role": "system", "content": "\n\n".join(note)}
        return messages + [ephemeral]

    def _stream_into_bubble(self, messages, vision=False):
        self._bot_text = ""
        self._bot_label = self._bot_bubble("\u2026")
        self._busy(True)
        send_msgs = self._augment(messages) if not vision else messages

        def on_delta(chunk):
            self._bot_text += chunk
            GLib.idle_add(self._bot_label.set_text, self._bot_text)
            self._scroll_down()

        def on_done():
            self._busy(False)
            GLib.idle_add(self._finalise)

        def on_error(msg):
            self._busy(False)
            GLib.idle_add(self._bot_label.set_text, "\u26a0 " + msg)

        threading.Thread(target=self.backend.stream,
                         args=(send_msgs, on_delta, on_done, on_error, vision), daemon=True).start()

    def _finalise(self):
        text = self._bot_text
        self.history.append({"role": "assistant", "content": text})

        def grab(tag):
            # (?![A-Za-z]) stops ```video from also matching ```videos blocks,
            # and ```read from matching ```readskill/```runskill etc.
            return [x.strip() for x in
                    re.findall(r"```" + tag + r"(?![A-Za-z])[ \t]*\n?(.*?)```", text, re.DOTALL)
                    if x.strip()]

        # cap how many of each tool one reply can fire, so a malformed/looping
        # model can't spawn dozens of threads or cards in a single turn.
        CAP = 6
        searches = grab("search")[:CAP]
        fetches = grab("fetch")[:CAP]
        images = grab("images")[:CAP]
        vid_searches = grab("videos")[:CAP]     # search-and-show videos
        videos = grab("video")[:CAP]            # download by URL
        junk = bool(re.search(r"```junk", text))
        skill_blocks = grab("skill")[:CAP]      # save a smart file
        runskills = grab("runskill")[:CAP]      # run a saved smart file
        reads = grab("read")[:CAP]              # read a file from disk by path
        remembers = grab("remember")[:CAP]      # store a durable fact
        forgets = grab("forget")[:CAP]          # prune a fact
        # ```check lang\n<code>``` — verify code WITHOUT running it (syntax+lint+
        # security+optional tests); feeds the report back so Chuck fixes it.
        checks = []
        for m in re.finditer(r"```check[ \t]+([a-z0-9+]+)[ \t]*\n(.*?)```", text,
                             re.DOTALL | re.IGNORECASE):
            checks.append((m.group(1).strip().lower(), m.group(2)))
        checks = checks[:CAP]
        codes = []                              # (lang, body) code to run
        for lang in ("python", "py", "node", "javascript", "js", "bash", "sh"):
            for body in grab(lang)[:CAP]:
                codes.append((lang, body))
        codes = codes[:CAP]

        disp = re.sub(
            r"```(?:search|fetch|images|videos|video|junk|skill|runskill|read|"
            r"remember|forget|check|python|py|node|javascript|js|bash|sh)\b.*?```",
            "", text, flags=re.DOTALL).strip()
        acting = bool(searches or fetches or images or vid_searches or videos or junk
                      or skill_blocks or runskills or reads or codes or checks)
        # Chuck's own narration is what shows in chat — never swallow it.
        set_rich(self._bot_label, disp or ("Working on it\u2026" if acting else "Done."))

        # skills: save any new smart files, then offer run cards
        for blk in skill_blocks:
            self._save_skill(blk)
        for name in runskills:
            self._run_skill(name.strip())

        # memory: store/prune durable facts (quiet — a small note, doesn't pause
        # the run or enter the loop).
        if _memory:
            for fact in remembers:
                ok, _msg = _memory.remember(fact.strip())
                if ok:
                    self._sys_note("\U0001F9E0 remembered", "dim")
            for q in forgets:
                _memory.forget(q.strip())
                self._sys_note("\U0001F9E0 forgot that", "dim")

        # code / shell to run: AUTO-VERIFY first (syntax+lint+security), then
        # either feed problems back for Chuck to fix, or show an approve-to-run
        # card. This moves correctness into the scaffolding — less pressure on
        # the model to be perfect first try.
        self._pending_tools = 0
        self._tool_feedback = []
        self._loop_web = (searches, fetches) if (searches or fetches) else None

        for lang, body in checks:
            self._pending_tools += 1
            self._do_check(lang, body.strip(), run_after=False)
        for lang, body in codes:
            if _codecheck:
                self._pending_tools += 1
                self._do_check(lang, body.strip(), run_after=True)
            else:
                self._code_card(lang, body.strip())

        for q in images:
            self._pending_tools += 1
            self._do_images(q)
        for q in vid_searches:
            self._pending_tools += 1
            self._do_video_search(q)
        if junk:
            self._pending_tools += 1
            self._do_junk()
        for path in reads:
            self._pending_tools += 1
            self._do_read(path.strip())
        for u in videos:                     # fire-and-forget download; not gated
            self._do_video(u)

        # If nothing async is outstanding, decide the loop now.
        if self._pending_tools == 0:
            self._continue_or_finish(disp)
        return False

    def _tool_done(self, feedback_text=None):
        """Called (on main thread) when one async show-tool finishes."""
        if feedback_text:
            self._tool_feedback.append(feedback_text)
        self._pending_tools = max(0, self._pending_tools - 1)
        if self._pending_tools == 0:
            self._continue_or_finish(self._bot_text and
                                     re.sub(r"```.*?```", "", self._bot_text, flags=re.DOTALL).strip())

    def _continue_or_finish(self, disp):
        searches_fetches = self._loop_web
        feedback = list(self._tool_feedback)
        self._loop_web = None; self._tool_feedback = []

        if searches_fetches and self._hops < MAX_TOOL_HOPS:
            self._hops += 1
            self._run_web_tools(searches_fetches[0], searches_fetches[1], extra_feedback=feedback)
            return
        if feedback and self._hops < MAX_TOOL_HOPS:
            self._hops += 1
            self.history.append({"role": "user",
                                 "content": "TOOL RESULTS (continue — don't stop until the whole "
                                 "task is done, then give your final answer):\n\n"
                                 + "\n\n".join(feedback)[:12000]})
            GLib.idle_add(self._ask_model)
            return

        self._hops = 0
        if self.tts_btn.get_active() and disp:
            speak(disp)
        self._save_chat()

    def _run_web_tools(self, searches, fetches, extra_feedback=None):
        self._busy(True)

        def worker():
            out = list(extra_feedback or [])
            seen_domains, sources_read = set(), 0
            # fan out across several distinct queries
            for q in searches[:RESEARCH_QUERIES]:
                if sources_read >= RESEARCH_MAX_SOURCES:
                    break
                self._live(f"\U0001F50E searching: {q}")
                results = web_search(q, n=RESEARCH_MAX_SOURCES)
                if not results:
                    out.append(f"[search '{q}': engines returned nothing — try different wording]")
                    continue
                for (title, url, snip) in results:
                    if sources_read >= RESEARCH_MAX_SOURCES:
                        break
                    dom = _domain(url)
                    if dom in seen_domains:      # diversity: one read per domain
                        continue
                    seen_domains.add(dom)
                    self._live(f"\U0001F4C4 reading {dom}")
                    body = web_fetch(url) or snip
                    if body:
                        out.append(f"[{title}] {url}\n{body}")
                        sources_read += 1
            for u in fetches[:RESEARCH_MAX_SOURCES]:
                dom = _domain(u)
                self._live(f"\U0001F4C4 reading {dom}")
                body = web_fetch(u)
                out.append(f"[{u}]\n{body or '(page unreadable / blocked)'}")
            self._live(""); self._busy(False)
            note = (f"TOOL RESULTS — {sources_read} distinct sources across "
                    f"{len(seen_domains)} domains. Cross-check them, cite the URLs, "
                    "mark single-source claims [UNVERIFIED], and keep going until the "
                    "whole task is finished before you write your final answer:\n\n")
            self.history.append({"role": "user", "content": note + "\n\n".join(out)[:16000]})
            GLib.idle_add(self._ask_model)
        threading.Thread(target=worker, daemon=True).start()

    # ── settings ──
    def open_settings(self, *_):
        dlg = Adw.Window(transient_for=self, modal=True, title="Settings",
                         default_width=560, default_height=430)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(14)
        hb = Adw.HeaderBar(); wrap = Adw.ToolbarView()
        wrap.add_top_bar(hb); wrap.set_content(Gtk.ScrolledWindow(child=box)); dlg.set_content(wrap)
        box.append(Gtk.Label(label="SiliconFlow API key", xalign=0))
        key = Gtk.Entry(text=self.settings.get("siliconflow_api_key", ""), visibility=False,
                        placeholder_text="sk-\u2026"); box.append(key)
        box.append(Gtk.Label(label="Chat model", xalign=0))
        model = Gtk.Entry(text=self.settings.get("model", DEFAULT_MODEL)); box.append(model)
        box.append(Gtk.Label(label="Vision model", xalign=0))
        vmodel = Gtk.Entry(text=self.settings.get("vision_model", DEFAULT_VISION)); box.append(vmodel)
        box.append(Gtk.Label(label="Preferred SearXNG instance (optional — your own or a private one)", xalign=0))
        searx = Gtk.Entry(text=self.settings.get("searx_url", ""),
                          placeholder_text="https://searx.example.org  (blank = built-in public list)")
        box.append(searx)
        box.append(Gtk.Label(label="Proxy for web/images/video (optional, e.g. Mullvad)", xalign=0))
        proxy = Gtk.Entry(text=self.settings.get("proxy", ""), placeholder_text="http://host:port")
        box.append(proxy)
        hint = Gtk.Label(xalign=0, wrap=True); hint.add_css_class("dim")
        hint.set_label("Key: cloud.siliconflow.com/account/ak (Basilisk's reused if present). "
                       "Search fans out over SearXNG (Brave+Google+DDG under the hood) with a "
                       "DuckDuckGo fallback; set your own instance for private search. Proxy routes "
                       "fetches through Mullvad etc. Voice = Piper if installed, else espeak-ng.")
        box.append(hint)

        def save(*_):
            self.settings["siliconflow_api_key"] = key.get_text().strip()
            self.settings["model"] = model.get_text().strip() or DEFAULT_MODEL
            self.settings["vision_model"] = vmodel.get_text().strip() or DEFAULT_VISION
            self.settings["searx_url"] = searx.get_text().strip()
            self.settings["proxy"] = proxy.get_text().strip()
            self.settings["tts"] = self.tts_btn.get_active()
            save_settings(self.settings); dlg.close()
        sv = Gtk.Button(label="Save"); sv.add_css_class("gold"); sv.connect("clicked", save)
        box.append(sv); dlg.present()


class ChuckApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_startup(self):
        Adw.Application.do_startup(self)
        prov = Gtk.CssProvider()
        try:
            prov.load_from_data(CSS_TMPL.encode())
        except TypeError:
            prov.load_from_data(CSS_TMPL)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        # seed the ready-made skill library once (idempotent; never clobbers
        # user skills). Runs in a thread so it never delays the window.
        if _skill_library and _skills:
            def _seed():
                try:
                    _skill_library.seed_into(_skills, DATA_DIR / "skills" / ".library_version")
                except Exception:
                    pass
            threading.Thread(target=_seed, daemon=True).start()

    def do_activate(self):
        (self.props.active_window or ChuckWindow(self)).present()


def main():
    Adw.init()
    return ChuckApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
