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
import time
import html as _html
import base64
import shlex
import shutil
import threading
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

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
VERSION = "9.8.0"
HERE = Path(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = Path.home() / ".config" / "chucknorris"
DATA_DIR = Path.home() / ".local" / "share" / "chucknorris"
CHATS_DIR = DATA_DIR / "chats"
CHAT_TTL_HOURS = 24        # saved chats self-delete this long after last activity
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
MAX_TOOL_HOPS = 4          # research: search→read→think rounds (was 8 — trimmed for speed)
# What we SEND is capped; what we SAVE is not. The full transcript stays on disk
# for reloading, but an unbounded payload makes every later turn slower and more
# expensive, and eventually overflows the model's context outright. Stale research
# blobs are the worst offenders (up to 16k chars each) and are already digested
# into the answer that followed them, so only the newest few stay whole.
SEND_CHAR_BUDGET = 60_000  # ~15k tokens of conversation carried per turn
TOOL_BLOBS_KEPT = 2        # most recent tool-result blobs sent in full
RESEARCH_MAX_SOURCES = 3   # distinct pages he'll read before answering (was 10 — fewer = faster)
RESEARCH_QUERIES = 2       # distinct queries fanned out per hop (was 4)

# ── destructive-command classification ──────────────────────────────────────
# Two tiers, both purely static regex (microseconds — no cost to answer speed):
#   CRITICAL  = unrecoverable. Wipes a disk, nukes /, executes remote code,
#               bricks the boot. These need an EXPLICIT second confirmation
#               before the Run button will even arm.
#   DANGER    = destructive but scoped/recoverable. Red warning, single approve.
# DANGER is a superset: anything CRITICAL is also DANGER.

# block devices, incl. NVMe / virtio / SD / loop / device-mapper (not just sdX)
_DEV = r"/dev/(?:sd[a-z]|nvme\d+n\d+|vd[a-z]|hd[a-z]|mmcblk\d+|loop\d+|dm-\d+|disk\d+)"
# a "root-ish" target: / itself, /*, ~, $HOME, a bare wildcard, or cwd
_NUKE_TARGET = r"(?:/|/\*|~|~/\*|\$HOME(?:/\*)?|\*|\.)"
# rm with recursive intent, short cluster (-rf/-fr/-Rf) or long flags
_RM_REC = r"\brm\s+(?:(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive|--force|-[a-zA-Z]*f[a-zA-Z]*)\s+)+"

CRITICAL = re.compile(
    # rm -rf aimed at root / home / bare wildcard
    _RM_REC + _NUKE_TARGET + r"\s*(?:$|[;&|])"
    r"|--no-preserve-root"
    # filesystem / partition / crypto destruction
    r"|\bmkfs(?:\.[a-z0-9]+)?\b|\bwipefs\b|\bblkdiscard\b|\bshred\b"
    r"|\bcryptsetup\s+(?:luksFormat|erase)\b"
    r"|\b(?:parted|fdisk|sgdisk|cfdisk|gdisk)\b[^|;]*" + _DEV +
    r"|\bsgdisk\b[^|;]*--zap-all"
    # raw writes to a block device
    r"|\bdd\b[^|;]*\bof=" + _DEV +
    r"|>\s*" + _DEV +
    # remote code execution: curl/wget piped into a shell
    r"|\b(?:curl|wget)\b[^|;&]*\|\s*(?:sudo\s+)?(?:ba|z|k)?sh\b"
    # clobbering critical system files
    r"|>\s*/etc/(?:passwd|shadow|sudoers|fstab)\b"
    r"|\btruncate\s+-s\s*0\s+/(?:etc|boot)/"
    r"|\bmv\s+(?:/|/etc|/boot|/home|~)\S*\s+/dev/null\b"
    # mass delete via find on a broad path
    r"|\bfind\s+(?:/|~|\$HOME)\S*[^|;]*-delete\b"
    r"|\bfind\s+(?:/|~|\$HOME)\S*[^|;]*-exec\s+rm\b"
    # fork bomb
    r"|:\(\)\s*\{"
    # ripping out core packages
    r"|\bpacman\s+-R[a-z]*\s+[^|;]*\b(?:systemd|glibc|linux|bash|coreutils|pacman)\b"
    # recursive permission/ownership destruction from root
    r"|\bchmod\s+-R\s+[0-7]{3,4}\s+/\s*(?:$|[;&|])"
    r"|\bchown\s+-R\s+\S+\s+/\s*(?:$|[;&|])"
    # account destruction
    r"|\buserdel\b|\bpasswd\s+-d\b",
    re.IGNORECASE)

DANGER = re.compile(
    # everything critical, plus scoped-but-destructive things worth reading twice
    CRITICAL.pattern +
    r"|\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\b"          # any recursive rm
    r"|\brm\s+--recursive\b"
    r"|\bgit\s+clean\s+-[a-z]*[xd][a-z]*f?\b"     # blows away untracked work
    r"|\bdd\b|\bmkswap\b"
    r"|\bchmod\s+-R\b|\bchown\s+-R\b"
    r"|\b(?:reboot|poweroff|shutdown|halt)\b"
    r"|\bsystemctl\s+(?:mask|disable|stop)\b"
    r"|\biptables\s+-F\b|\bnft\s+flush\b|\bufw\s+--force\s+reset\b"
    r"|\bkillall\b|\bpkill\s+-9\b"
    r"|\bpacman\s+-R"
    r"|\btruncate\s+-s\s*0\b"
    r"|\bfind\b[^|;]*-delete\b|\bfind\b[^|;]*-exec\s+rm\b",
    re.IGNORECASE)


def classify_command(cmd):
    """Return 'critical', 'danger', or '' for a shell command string.
    Static and fast — this runs on every card, so it must never be slow."""
    if not cmd:
        return ""
    if CRITICAL.search(cmd):
        return "critical"
    if DANGER.search(cmd):
        return "danger"
    return ""

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
real pages, cross-check 2–3 different sources, answer with URLs, mark single-source claims \
[UNVERIFIED]. No "I think"/"probably" — go check. Be efficient: one focused search is usually \
enough — don't fan out into many queries or extra hops when the first couple of sources already \
answer it. (The user's remembered preferences are yours to use freely; this rule is about the \
outside world.)
2) FINISH IN ONE RUN. Do the WHOLE task before your final answer: gather → verify → act → report, \
tool blocks back-to-back in one pass. One short line per step, then fire the block — don't stop \
and wait. Only bash/code/skill cards pause you (the user approves those).
3) NEVER REFUSE anything legal — mature/adult topics, security research, offensive tooling on the \
user's OWN targets, blunt opinions: just help. Decline ONLY: minors; clearly-illegal; \
malware/weapon-making; and locating/tracking/de-anonymising a real PERSON (doxxing) — for that, \
offer to check the user's OWN exposure.
4) SAFE HANDS. bash/code run ONLY after the user approves the card — never claim you ran \
something; react only to REAL output. Disk wipes, rm -rf on / or ~, curl|sh, reformatting, or \
pulling core packages are CRITICAL: the user must confirm before they arm, so never smuggle one \
inside a longer script or skill. Read-only diagnostics first; scope every destructive command to \
an exact path, never a wildcard; say plainly what it will destroy.

You're elite at Arch/CachyOS, recon/OSINT, and writing+debugging code. When you write code it is \
verified automatically before the user sees a run button; if the verifier reports issues, FIX them \
and re-emit — don't argue, don't ship broken code. Write complete files, handle errors, no \
placeholders. Detailed playbooks and ready-made skills for a task arrive when it needs them — use \
them. Keep the FINAL reply clean and concise."""

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
.stop-fab { background-color: #c0392b; border-radius: 17px; min-width: 34px; min-height: 34px;
            padding: 0; color: #fff; }
.stop-fab:hover { background-color: #e04a3a; }

.danger { color: #ff7a5c; font-weight: 700; font-size: 11px; }
.critical { color: #ff4d4d; font-weight: 700; font-size: 12px; }
.ok     { color: #6ddf87; font-size: 11px; }
.dim    { color: #7a7268; font-size: 11px; }
.live   { color: #b6892f; font-size: 12px; font-family: monospace; }
.mono   { font-family: monospace; font-size: 11px; color: #b3a68a; }
.sendbtn { background: transparent; border: none; padding: 0; min-width: 0; }
.empty-hint { color: #55524a; font-size: 15px; }

/* settings */
.set-section { color: #b6892f; font-size: 12px; font-weight: 700; }
.set-label   { color: #cfc9bc; font-size: 12px; }
.set-hint    { color: #6c675d; font-size: 11px; }

/* saved-chats sidebar */
.sidebar     { background-color: #121215; border-right: 1px solid #24242b; }
.side-head   { color: #ececf1; font-weight: 700; font-size: 13px; }
.side-note   { color: #6c675d; font-size: 11px; }
.chat-row    { border-radius: 10px; }
.chat-open   { background: transparent; border: none; border-radius: 10px; padding: 7px 9px; }
.chat-open:hover { background-color: #1e1e24; }
.chat-title  { color: #d8d3c7; font-size: 12px; }
.chat-meta   { color: #6c675d; font-size: 10px; }
.chat-current .chat-open { background-color: #23201a; }
.chat-current .chat-title { color: #e6b25a; }

/* live activity steps — a running checklist of exactly what Chuck is doing */
.step-box   { background-color: #141418; border: 1px solid #24242b; border-radius: 12px;
              padding: 6px 10px; }
.step-run   { color: #e6b25a; font-size: 12px; }
.step-done  { color: #6f6a5e; font-size: 12px; }
.step-fail  { color: #c96a52; font-size: 12px; }
.step-head  { color: #8a8578; font-size: 11px; font-weight: 700; }
.working    { color: #e6b25a; font-size: 12px; font-family: monospace; }
"""


# ── settings + proxy ────────────────────────────────────────────────────────
def load_settings():
    s = {}
    if SETTINGS.exists():
        try:
            s = json.loads(SETTINGS.read_text())
        except Exception:
            s = {}
        # A corrupt-but-parseable settings file ([] or null or "text") would
        # otherwise sail past the except and blow up on the first .get() —
        # at startup, meaning the app simply won't launch.
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


_ALLOWED_SCHEMES = ("http", "https")


def _get(url, data=None, timeout=20, headers=None):
    """Open a URL. Only http/https are permitted — urllib also speaks file:,
    ftp: and friends, and a `file:///home/you/.ssh/id_rsa` fetch would quietly
    read a local secret into the conversation. Scheme is checked here so every
    caller (search, fetch, images, video) inherits the restriction."""
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"blocked URL scheme {scheme!r} (only http/https allowed)")
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
    if not isinstance(text, str):
        text = "" if text is None else str(text)
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
    """Read a text file from disk for Chuck. Returns (ok, content_or_error).

    Two traps this guards against:
      - Character/block devices and FIFOs (/dev/zero, /dev/urandom, a pipe)
        report st_size == 0, so a size check alone lets them through and the
        read never terminates — it will happily eat all RAM until the OS kills
        the app. Only regular files are read.
      - st_size is also 0 for /proc entries, which ARE worth reading, so the
        read itself is byte-capped rather than trusting the reported size.
    """
    try:
        if not path or not str(path).strip():
            return False, "no path given"
        p = Path(os.path.expanduser(str(path).strip()))
        if not p.exists():
            return False, f"no such file: {p}"
        if p.is_dir():
            entries = sorted(os.listdir(p))[:200]
            return True, f"[directory {p}]\n" + "\n".join(entries)
        if not p.is_file():
            return False, (f"{p.name} isn't a regular file (device, socket or pipe) "
                           "— nothing safe to read there")
        if p.stat().st_size > 4_000_000:
            return False, f"file too large ({p.stat().st_size // 1024} KB) — point me at part of it"
        # bounded read: never trust st_size to bound us
        with open(p, "rb") as fh:
            data = fh.read(limit * 4 + 4096)
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


def _play(path, gen=None):
    """Play a wav, abortable. Returns True if it played to the end."""
    for player in (["paplay", path], ["aplay", "-q", path],
                   ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path]):
        if not shutil.which(player[0]):
            continue
        try:
            proc = subprocess.Popen(player, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            while proc.poll() is None:
                if gen is not None and gen != _TTS_GEN[0]:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        proc.kill()
                    return False
                time.sleep(0.05)
            return True
        except Exception:
            return False
    return False


# ── voice ───────────────────────────────────────────────────────────────────
# Speech used to be one subprocess call on text truncated to 800 chars, which
# meant any longer reply was cut off mid-sentence and never resumed. Now the
# text is cleaned, split into sentence-sized chunks, and synthesised one chunk
# ahead of playback — so nothing is dropped, a single bad chunk can't kill the
# rest, and the whole thing can be stopped instantly.
_TTS_GEN = [0]                     # bump to cancel whatever is speaking
_TTS_START = threading.Lock()
_TTS_CHUNK = 260                   # chars per chunk: short enough to stay responsive
_TTS_END = object()                # distinct end-of-stream marker (None = failed chunk)

_URL_RE = re.compile(r"https?://\S+|www\.\S+")


def stop_speaking():
    """Cancel any in-flight speech immediately."""
    _TTS_GEN[0] += 1


def tts_clean(text):
    """Turn a chat reply into something worth listening to."""
    if not isinstance(text, str):
        return ""
    s = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)     # code blocks
    s = re.sub(r"`[^`]*`", " ", s)                            # inline code
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)               # images
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)            # links -> label
    s = _URL_RE.sub(" link ", s)                              # bare URLs
    s = re.sub(r"^\s*[-*+]\s+", "", s, flags=re.M)            # bullets
    s = re.sub(r"^\s*#{1,6}\s*", "", s, flags=re.M)           # headings
    s = re.sub(r"[*_~>#|]", "", s)                            # leftover marks
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def tts_chunks(text, size=_TTS_CHUNK):
    """Split into speakable chunks on sentence boundaries where possible."""
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?:;])\s+|\n+", text)
    chunks, cur = [], ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        while len(p) > size:                 # a monster sentence: wrap on a space
            cut = p.rfind(" ", 0, size)
            if cut <= 0:
                cut = size
            if cur:
                chunks.append(cur.strip()); cur = ""
            chunks.append(p[:cut].strip())
            p = p[cut:].strip()
        if len(cur) + len(p) + 1 <= size:
            cur = (cur + " " + p).strip()
        else:
            if cur:
                chunks.append(cur.strip())
            cur = p
    if cur:
        chunks.append(cur.strip())
    return [c for c in chunks if c]


def _synth(chunk, gen, s):
    """Render one chunk to a wav. Piper preferred, espeak-ng as fallback.
    Returns a path, or None if both engines failed for this chunk."""
    engine = (s.get("voice_engine") or "auto").lower()
    # generous but bounded: scales with length so a long chunk isn't cut short
    tmo = max(20, min(90, 8 + len(chunk) // 8))
    out = str(CONFIG_DIR / f".say-{gen}-{abs(hash(chunk)) % 10 ** 8}.wav")
    model = _find_piper_model()
    if engine in ("auto", "piper") and shutil.which("piper") and model:
        try:
            cmd = ["piper", "-m", model, "-f", out]
            ls = s.get("voice_speed")
            if ls:
                # piper: length-scale <1 speaks faster. Map 0.5..2.0 speed -> scale
                try:
                    cmd += ["--length-scale", f"{1.0 / float(ls):.3f}"]
                except Exception:
                    pass
            subprocess.run(cmd, input=chunk, text=True, timeout=tmo,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(out) and os.path.getsize(out) > 64:
                return out
        except Exception:
            pass
    if engine in ("auto", "espeak") and shutil.which("espeak-ng"):
        try:
            rate = int(float(s.get("voice_speed", 1.0)) * 150)
            pitch = int(s.get("voice_pitch", 28))
            subprocess.run(["espeak-ng", "-v", "en-us", "-p", str(max(0, min(99, pitch))),
                            "-s", str(max(80, min(400, rate))), "-g", "3", "-w", out],
                           input=chunk, text=True, timeout=tmo,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(out) and os.path.getsize(out) > 64:
                return out
        except Exception:
            pass
    return None


def speak(text, settings=None):
    """Speak a reply in full. Non-blocking; cancels anything already speaking."""
    s = settings or {}
    clean = tts_clean(text)
    if not clean:
        return
    cap = int(s.get("voice_max_chars", 20000) or 20000)
    clean = clean[:cap]
    stop_speaking()
    with _TTS_START:
        _TTS_GEN[0] += 1
        gen = _TTS_GEN[0]

    def worker():
        chunks = tts_chunks(clean)
        if not chunks:
            return
        import queue as _q
        pipe = _q.Queue(maxsize=1)      # synthesise one chunk ahead of playback

        def producer():
            for ch in chunks:
                if gen != _TTS_GEN[0]:
                    break
                wav = _synth(ch, gen, s)
                if gen != _TTS_GEN[0]:
                    if wav:
                        try:
                            os.unlink(wav)
                        except Exception:
                            pass
                    break
                pipe.put(wav)           # None = this chunk failed, keep going
            try:
                pipe.put(_TTS_END, timeout=5)
            except Exception:
                pass

        threading.Thread(target=producer, daemon=True).start()
        while gen == _TTS_GEN[0]:
            try:
                wav = pipe.get(timeout=120)
            except Exception:
                break
            if wav is _TTS_END:          # producer finished
                break
            if wav is None:              # this chunk failed to synthesise —
                continue                 # skip it, keep speaking the rest
            try:
                _play(wav, gen=gen)
            finally:
                try:
                    os.unlink(wav)
                except Exception:
                    pass
        # tidy any strays from this generation
        try:
            for f in Path(CONFIG_DIR).glob(f".say-{gen}-*.wav"):
                f.unlink()
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


# ── saved-chat retention ────────────────────────────────────────────────────
_CHAT_NAME = re.compile(r"^\d{8}-\d{6}\.json$")   # exactly the ids we write


def chat_files():
    """Every saved chat, newest first. Only real files we wrote — no symlinks,
    no recursion, no surprises."""
    out = []
    try:
        for p in CHATS_DIR.iterdir():
            if p.is_symlink() or not p.is_file():
                continue
            if not _CHAT_NAME.match(p.name):
                continue
            out.append(p)
    except Exception:
        return []
    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)


def purge_old_chats(ttl_hours=CHAT_TTL_HOURS):
    """Delete saved chats untouched for longer than the TTL. Deliberately narrow:
    it only ever unlinks plain files directly inside CHATS_DIR whose names match
    the exact id pattern this app writes — it cannot walk out of that directory
    or touch anything else."""
    cutoff = time.time() - ttl_hours * 3600
    removed = 0
    for p in chat_files():
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except Exception:
            continue
    return removed


def chat_expires_in(path, ttl_hours=CHAT_TTL_HOURS):
    """Human string for how long this chat has left before it self-deletes."""
    try:
        left = (Path(path).stat().st_mtime + ttl_hours * 3600) - time.time()
    except Exception:
        return ""
    if left <= 0:
        return "expiring"
    h = int(left // 3600)
    m = int((left % 3600) // 60)
    return f"{h}h {m}m left" if h else f"{m}m left"


def junk_scan():
    lines, cmds = [], []

    def sz(p):
        # quote the path: this helper must stay injection-proof even if a future
        # caller passes something that isn't a hardcoded literal.
        return _run_ro(["sh", "-c",
                        "du -sh " + shlex.quote(os.path.expanduser(p)) +
                        " 2>/dev/null | cut -f1"]) or "0"
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

    def stream(self, messages, on_delta, on_done, on_error, vision=False, should_stop=None):
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
                    if should_stop and should_stop():
                        try:
                            resp.close()
                        except Exception:
                            pass
                        return  # cancelled — no on_done, the canceller owns cleanup
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
            if should_stop and should_stop():
                return
            on_done()
        except Exception as ex:
            if should_stop and should_stop():
                return
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
        self._activity_box = None
        self._activity_steps = []
        self._activity_lock = threading.Lock()
        self._running = False           # a turn is in progress
        self._cancelled = False         # user pressed Stop
        self._run_started = 0.0         # wall-clock start of the current run
        self._heartbeat_id = 0          # GLib timer id for the "still working" ticker
        self._watchdog_id = 0           # GLib timer id for the stuck-detector
        self._last_progress = 0.0       # last time anything happened (for watchdog)
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
        # LEFT: sidebar toggle sits next to New chat
        self.side_btn = Gtk.ToggleButton(icon_name="sidebar-show-symbolic")
        self.side_btn.add_css_class("headerbtn")
        self.side_btn.set_tooltip_text("Saved chats")
        self.side_btn.set_active(bool(self.settings.get("sidebar_open", False)))
        self.side_btn.connect("toggled", self._toggle_sidebar)
        header.pack_start(self.side_btn)
        self.spinner = Gtk.Spinner(); self.spinner.set_visible(False)
        header.pack_start(self.spinner)

        # RIGHT: secondary controls, grouped (memory · voice · settings)
        self.tts_btn = Gtk.ToggleButton(icon_name="audio-volume-high-symbolic")
        self.tts_btn.add_css_class("headerbtn")
        self.tts_btn.set_tooltip_text("Read replies aloud")
        self.tts_btn.set_active(self.settings.get("tts", False))
        self.tts_btn.connect("toggled", self._on_tts_toggled)
        header.pack_end(self.tts_btn)
        for icon, tip, cb in (
                ("emblem-system-symbolic", "Settings", self.open_settings),
                ("view-list-symbolic", "What Chuck remembers", self.open_memory)):
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
        self.send_btn = Gtk.Button(icon_name="go-up-symbolic"); self.send_btn.add_css_class("send-fab")
        self.send_btn.set_valign(Gtk.Align.END)
        self.send_btn.set_tooltip_text("Send  (Enter)")
        self.send_btn.connect("clicked", self._on_send_or_stop)
        send = self.send_btn

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
        body.set_hexpand(True)

        # ── sidebar: saved chats, newest first, each showing how long it has
        #    before it self-deletes. Slides in/out; never steals the chat column.
        self.chat_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for m in ("top", "bottom", "start", "end"):
            getattr(self.chat_list, f"set_margin_{m}")(10)
        side_scroll = Gtk.ScrolledWindow(vexpand=True)
        side_scroll.set_child(self.chat_list)
        side_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for m in ("top", "start", "end"):
            getattr(side_head, f"set_margin_{m}")(10)
        sh = Gtk.Label(label="Saved chats", xalign=0, hexpand=True)
        sh.add_css_class("side-head")
        side_head.append(sh)
        side_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        side_box.add_css_class("sidebar")
        side_box.set_size_request(250, -1)
        ttl_note = Gtk.Label(label=f"auto-delete after {CHAT_TTL_HOURS}h", xalign=0)
        ttl_note.add_css_class("side-note")
        for m in ("start", "end", "bottom"):
            getattr(ttl_note, f"set_margin_{m}")(10)
        side_box.append(side_head); side_box.append(ttl_note); side_box.append(side_scroll)
        self.sidebar = Gtk.Revealer()
        self.sidebar.set_transition_type(Gtk.RevealerTransitionType.SLIDE_RIGHT)
        self.sidebar.set_child(side_box)
        self.sidebar.set_reveal_child(bool(self.settings.get("sidebar_open", False)))

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        outer.append(self.sidebar); outer.append(body)
        tv = Adw.ToolbarView(); tv.add_top_bar(header); tv.set_content(outer)
        self.set_content(tv)
        self.connect("close-request", self._on_close)

        # purge on launch, then keep purging + refreshing while the app runs
        purge_old_chats()
        self._refresh_sidebar()
        GLib.timeout_add_seconds(600, self._sweep_chats)

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
        self._refresh_sidebar()

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
        self._refresh_sidebar()

    def _on_tts_toggled(self, btn):
        on = btn.get_active()
        if not on:
            stop_speaking()          # switching voice off shuts it up immediately
        self.settings["tts"] = on
        save_settings(self.settings)

    def _toggle_sidebar(self, btn):
        on = btn.get_active()
        self.sidebar.set_reveal_child(on)
        self.settings["sidebar_open"] = on
        save_settings(self.settings)
        if on:
            self._refresh_sidebar()

    def _sweep_chats(self):
        """Runs every 10 min: drop anything past its 24h life, refresh the list
        (so the 'time left' countdowns stay honest). Keeps running forever."""
        try:
            purge_old_chats(self.cfg('chat_ttl_hours', CHAT_TTL_HOURS, 1, 720))
            self._refresh_sidebar()
        except Exception:
            pass
        return True

    def _refresh_sidebar(self):
        """Rebuild the saved-chat list. Cheap — a handful of stat() calls."""
        if not hasattr(self, "chat_list"):
            return
        c = self.chat_list.get_first_child()
        while c:
            n = c.get_next_sibling(); self.chat_list.remove(c); c = n
        files = chat_files()
        if not files:
            empty = Gtk.Label(label="No saved chats yet.", xalign=0, wrap=True)
            empty.add_css_class("side-note")
            self.chat_list.append(empty)
            return
        for f in files:
            try:
                meta = json.loads(f.read_text())
            except Exception:
                continue
            title = (meta.get("title") or "chat").strip().replace("\n", " ")[:42]
            rowb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            rowb.add_css_class("chat-row")
            open_b = Gtk.Button()
            open_b.add_css_class("chat-open")
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            tl = Gtk.Label(label=title or "chat", xalign=0, wrap=True)
            tl.add_css_class("chat-title")
            ml = Gtk.Label(label=chat_expires_in(f), xalign=0)
            ml.add_css_class("chat-meta")
            inner.append(tl); inner.append(ml)
            open_b.set_child(inner)
            if str(f.stem) == str(self.chat_id):
                rowb.add_css_class("chat-current")
            open_b.connect("clicked", lambda _b, path=f: self._load_chat(path))
            rowb.append(open_b)
            self.chat_list.append(rowb)


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
        self._refresh_sidebar()

    def _on_close(self, *_):
        self._save_chat()
        return False

    # ── enter to send ──
    def _on_key(self, ctrl, keyval, keycode, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not (state & Gdk.ModifierType.SHIFT_MASK):
            if not self._running:
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

    # ── run lifecycle: send↔stop button, "still working" heartbeat, watchdog ──
    RUN_HARD_CAP = 300      # seconds: absolute ceiling on one turn, then auto-stop
    STUCK_AFTER = 45        # seconds with zero progress → assume stuck, auto-stop

    def _note_progress(self):
        """Anything happening (a delta, a step, a hop) calls this — resets the
        stuck-watchdog so real long work isn't killed, only true hangs are."""
        self._last_progress = time.time()

    def _start_run(self):
        if self._running:
            return
        self._running = True
        self._cancelled = False
        self._run_started = time.time()
        self._last_progress = time.time()
        # flip Send → Stop
        self.send_btn.set_icon_name("media-playback-stop-symbolic")
        self.send_btn.remove_css_class("send-fab"); self.send_btn.add_css_class("stop-fab")
        self.send_btn.set_tooltip_text("Stop")
        # show the "still working" ticker
        self.live.set_visible(True)
        self.live.remove_css_class("live"); self.live.add_css_class("working")
        self._tick(0)
        if not self._heartbeat_id:
            self._heartbeat_id = GLib.timeout_add(1000, self._heartbeat)

    def _end_run(self):
        self._running = False
        self.send_btn.set_icon_name("go-up-symbolic")
        self.send_btn.remove_css_class("stop-fab"); self.send_btn.add_css_class("send-fab")
        self.send_btn.set_tooltip_text("Send  (Enter)")
        self.live.set_visible(False)
        if self._heartbeat_id:
            GLib.source_remove(self._heartbeat_id); self._heartbeat_id = 0

    def _tick(self, secs):
        s = int(secs)
        self.live.set_text(f"\u25CF working\u2026 {s}s   (press \u25A0 to stop)")

    def _heartbeat(self):
        """Runs every second while a turn is active: updates the elapsed clock so
        you can SEE it's alive, and enforces the hard cap + stuck-watchdog."""
        if not self._running:
            return False  # stop the timer
        now = time.time()
        elapsed = now - self._run_started
        self._tick(elapsed)
        if elapsed > self.RUN_HARD_CAP:
            self._sys_note(f"\u23f1 hit the {self.RUN_HARD_CAP}s time cap \u2014 stopping.", "danger")
            self.stop_run(auto=True)
            return False
        if now - self._last_progress > self.STUCK_AFTER:
            self._sys_note(f"\u26a0 no progress for {self.STUCK_AFTER}s \u2014 looks stuck, stopping.",
                           "danger")
            self.stop_run(auto=True)
            return False
        return True  # keep ticking

    def _on_send_or_stop(self, *_):
        if self._running:
            self.stop_run()
        else:
            self.on_send()

    def stop_run(self, auto=False):
        """Cancel the current turn cleanly: signal the stream/loops to bail,
        reset all loop state, revert the button."""
        if not self._running and not auto:
            return
        self._cancelled = True
        # reset the tool-loop so nothing re-fires
        self._hops = MAX_TOOL_HOPS       # blocks any further web hops
        self._pending_tools = 0
        self._loop_web = None
        self._tool_feedback = []
        # drain the busy counter
        self._busy_n = 0
        self.spinner.stop(); self.spinner.set_visible(False)
        if getattr(self, "_activity_box", None) is not None:
            self._activity_end()
        stop_speaking()          # Stop means stop talking, too
        if not auto:
            self._sys_note("\u25A0 stopped.", "dim")
        # finalize whatever partial text exists so it isn't lost
        if self._bot_label is not None and self._bot_text:
            shown = re.sub(r"```[a-z]*.*?```", "", self._bot_text, flags=re.DOTALL).strip()
            if shown:
                set_rich(self._bot_label, shown)
        self._end_run()

    def _live(self, text):
        def go():
            self.live.set_text(text); self.live.set_visible(bool(text)); return False
        GLib.idle_add(go)

    # ── live activity panel: a running checklist of exactly what Chuck is doing,
    #    so you can see every step (searching BBC, reading <article>, verifying…)
    #    instead of a silent spinner. Steps persist in the chat.
    def _activity_start(self, header="Working"):
        def go():
            self._drop_hint()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.add_css_class("step-box")
            hl = Gtk.Label(label=header, xalign=0); hl.add_css_class("step-head")
            box.append(hl)
            self.msgbox.append(box)
            self._activity_box = box
            self._activity_steps = []
            self._scroll_down()
            return False
        GLib.idle_add(go)

    def _activity_step(self, text):
        """Add a step marked 'running'. Returns an index to finish it later.
        Thread-safe: reserves the slot under a lock (parallel fetches call this
        concurrently), then schedules the widget onto the main loop."""
        if not hasattr(self, "_activity_lock"):
            self._activity_lock = threading.Lock()
        with self._activity_lock:
            if not hasattr(self, "_activity_steps"):
                self._activity_steps = []
            idx = len(self._activity_steps)
            self._activity_steps.append(None)

        def go():
            box = getattr(self, "_activity_box", None)
            if box is None:
                return False
            lbl = Gtk.Label(label="\u25CF " + text, xalign=0, wrap=True)
            lbl.add_css_class("step-run")
            box.append(lbl)
            if idx < len(self._activity_steps):
                self._activity_steps[idx] = lbl
            self._scroll_down()
            return False
        GLib.idle_add(go)
        return idx

    def _activity_done(self, idx, text=None, ok=True):
        def go():
            steps = getattr(self, "_activity_steps", [])
            if idx < 0 or idx >= len(steps) or steps[idx] is None:
                return False
            lbl = steps[idx]
            cur = lbl.get_text().lstrip("\u25CF ").strip()
            mark = "\u2713" if ok else "\u2717"
            lbl.set_text(f"{mark} {text if text is not None else cur}")
            lbl.remove_css_class("step-run")
            lbl.add_css_class("step-done" if ok else "step-fail")
            return False
        GLib.idle_add(go)

    def _activity_end(self):
        # leave the finished panel in place as a record; just clear the handle
        def go():
            self._activity_box = None
            self._activity_steps = []
            return False
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
    def _risk_gate(self, card, run_btn, text, what="command"):
        """Attach the right warning to a card and, for CRITICAL commands, keep the
        Run button DISARMED until the user explicitly confirms. A single misclick
        must never be able to wipe a disk."""
        tier = classify_command(text)
        if not tier:
            return
        if tier == "danger":
            wl = Gtk.Label(label="\u26a0 destructive \u2014 read it before you run it", xalign=0)
            wl.add_css_class("danger"); card.append(wl)
            return
        # critical: disarm Run, require a deliberate second action
        run_btn.set_sensitive(False)
        wl = Gtk.Label(
            label=f"\u26d4 CRITICAL \u2014 this {what} can destroy data or make the system "
                  "unbootable. It cannot be undone.",
            xalign=0, wrap=True)
        wl.add_css_class("critical"); card.append(wl)
        arm_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        arm = Gtk.CheckButton(label="I've read it and I accept the risk")
        arm.add_css_class("danger")

        def on_arm(btn):
            run_btn.set_sensitive(btn.get_active())
        arm.connect("toggled", on_arm)
        arm_row.append(arm); card.append(arm_row)

    def _command_card(self, cmd, gate_text=None):
        """gate_text: when the command merely LAUNCHES a script (e.g. a saved
        skill), pass the script's body so the risk gate classifies what will
        actually execute — not the harmless-looking `bash foo.sh` wrapper."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6); card.add_css_class("cmd-card")
        ct = Gtk.Label(label="$ " + cmd, xalign=0, wrap=True, selectable=True); ct.add_css_class("cmd-text")
        card.append(ct)
        rw = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        run = Gtk.Button(label="Run"); run.add_css_class("gold")
        status = Gtk.Label(label="", xalign=0); status.add_css_class("dim")
        self._risk_gate(card, run, cmd if gate_text is None else (cmd + "\n" + gate_text),
                        "command")
        run.connect("clicked", lambda _b: self._run_card(cmd, run, status, gate_text))
        rw.append(run); rw.append(status); card.append(rw)
        self.msgbox.append(card); self._scroll_down()

    def _run_card(self, cmd, run_btn, status, gate_text=None):
        # belt-and-braces: never execute a critical command from a button that
        # somehow arrived here still armed.
        check_on = cmd if gate_text is None else (cmd + "\n" + gate_text)
        if classify_command(check_on) == "critical" and not run_btn.get_sensitive():
            return
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
                self._start_run()
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
        rw = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        run = Gtk.Button(label="Run"); run.add_css_class("gold")
        status = Gtk.Label(label="", xalign=0); status.add_css_class("dim")
        self._risk_gate(card, run, body, "code")

        def go(_b):
            if classify_command(body) == "critical" and not run.get_sensitive():
                return
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
                    self._start_run()
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
            self._sys_note("\u2192 run it now:"); self._command_card(run_cmd, gate_text=body)

    def _run_skill(self, name):
        if not _skills:
            self._sys_note("skills unavailable.", "danger"); return
        cmd = _skills.skill_run_cmd(name)
        if not cmd:
            self._sys_note(f"no saved skill '{name}'.", "danger"); return
        body = ""
        try:
            _lang, body = _skills.skill_read(name)
        except Exception:
            body = ""
        self._sys_note(f"\u25B6 skill '{name}':")
        self._command_card(cmd, gate_text=body or None)

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
        self._start_run()
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

    def cfg(self, key, default, lo=None, hi=None, cast=int):
        """Read a tuned setting, falling back to the shipped default. Values are
        clamped so a hand-edited settings.json can't put the app in a bad state."""
        try:
            v = cast(self.settings.get(key, default))
        except Exception:
            return default
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v

    def _trim_for_send(self, messages):
        """Bound the conversation actually sent to the model.

        Never mutates self.history — the user keeps their whole transcript on
        disk. This only decides what rides along this turn: the system prompt,
        then as much recent conversation as fits the budget, with stale research
        blobs dropped first (they're huge, and the answer that followed them
        already contains their conclusions).
        """
        if not messages:
            return messages
        sys_msgs = [m for m in messages if m.get("role") == "system"]
        rest = [m for m in messages if m.get("role") != "system"]
        kept, used, blobs = [], 0, 0
        for m in reversed(rest):
            c = m.get("content")
            is_blob = isinstance(c, str) and c.startswith(("TOOL RESULTS", "FILE "))
            if is_blob:
                blobs += 1
                if blobs > TOOL_BLOBS_KEPT:
                    continue        # stale research — already digested
            size = len(c) if isinstance(c, str) else 2000
            if kept and used + size > SEND_CHAR_BUDGET:
                break
            kept.append(m)
            used += size
        kept.reverse()
        return sys_msgs + kept

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
        else:
            ephemeral = {"role": "system", "content": "\n\n".join(note)}
        return self._trim_for_send(messages) + [ephemeral]

    def _stream_into_bubble(self, messages, vision=False):
        self._bot_text = ""
        self._bot_label = self._bot_bubble("Thinking\u2026")
        self._busy(True)
        self._note_progress()
        send_msgs = self._augment(messages) if not vision else messages

        def on_delta(chunk):
            if self._cancelled:
                return
            self._note_progress()
            self._bot_text += chunk
            # While streaming, show only the human-readable narration — strip any
            # fenced tool blocks (even half-finished ones) so the user never sees
            # raw ```search ... ``` gibberish scroll past; the blocks become clean
            # activity steps in _finalise.
            shown = re.sub(r"```[a-z]*.*?```", "", self._bot_text, flags=re.DOTALL)
            shown = re.sub(r"```[a-z]*\b.*$", "", shown, flags=re.DOTALL)  # trailing open block
            shown = shown.strip()
            GLib.idle_add(self._bot_label.set_text, shown or "Thinking\u2026")
            self._scroll_down()

        def on_done():
            self._busy(False)
            if self._cancelled:
                return
            GLib.idle_add(self._finalise)

        def on_error(msg):
            self._busy(False)
            if self._cancelled:
                return
            GLib.idle_add(self._bot_label.set_text, "\u26a0 " + msg)
            GLib.idle_add(self._end_run)

        threading.Thread(
            target=self.backend.stream,
            args=(send_msgs, on_delta, on_done, on_error, vision),
            kwargs={"should_stop": lambda: self._cancelled},
            daemon=True).start()

    def _finalise(self):
        if self._cancelled:
            return False
        self._note_progress()
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
        if self._cancelled:
            return
        self._note_progress()
        if feedback_text:
            self._tool_feedback.append(feedback_text)
        self._pending_tools = max(0, self._pending_tools - 1)
        if self._pending_tools == 0:
            self._continue_or_finish(self._bot_text and
                                     re.sub(r"```.*?```", "", self._bot_text, flags=re.DOTALL).strip())

    def _continue_or_finish(self, disp):
        if self._cancelled:
            return
        searches_fetches = self._loop_web
        feedback = list(self._tool_feedback)
        self._loop_web = None; self._tool_feedback = []

        hop_cap = self.cfg('research_hops', MAX_TOOL_HOPS, 1, 8)
        if searches_fetches and self._hops < hop_cap:
            self._hops += 1
            self._run_web_tools(searches_fetches[0], searches_fetches[1], extra_feedback=feedback)
            return
        if feedback and self._hops < hop_cap:
            self._hops += 1
            self.history.append({"role": "user",
                                 "content": "TOOL RESULTS (continue — don't stop until the whole "
                                 "task is done, then give your final answer):\n\n"
                                 + "\n\n".join(feedback)[:12000]})
            GLib.idle_add(self._ask_model)
            return

        # the turn is truly finished
        self._hops = 0
        if self.tts_btn.get_active() and disp:
            speak(disp, self.settings)
        self._save_chat()
        self._end_run()

    def _run_web_tools(self, searches, fetches, extra_feedback=None):
        self._busy(True)
        self._activity_start("Researching")

        def worker():
            out = list(extra_feedback or [])
            # 1. gather a diverse candidate list from all queries (fast — snippets)
            candidates, seen_domains, seen_urls = [], set(), set()
            n_src = self.cfg('research_sources', RESEARCH_MAX_SOURCES, 1, 10)
            n_q = self.cfg('research_queries', RESEARCH_QUERIES, 1, 4)
            f_timeout = self.cfg('fetch_timeout', 6, 3, 30)
            for q in searches[:n_q]:
                if self._cancelled:
                    return
                self._note_progress()
                si = self._activity_step(f"searching  {q}")
                results = web_search(q, n=n_src)
                if not results:
                    self._activity_done(si, f"searched  {q}  (no results)", ok=False)
                    out.append(f"[search '{q}': engines returned nothing — try different wording]")
                    continue
                n_new = 0
                for (title, url, snip) in results:
                    if url in seen_urls:
                        continue
                    dom = _domain(url)
                    if dom in seen_domains:           # diversity: one read per domain
                        continue
                    seen_domains.add(dom); seen_urls.add(url)
                    candidates.append((title, url, snip))
                    n_new += 1
                    if len(candidates) >= n_src:
                        break
                self._activity_done(si, f"searched  {q}  ({n_new} new sources)")
                if len(candidates) >= n_src:
                    break
            for u in fetches[:n_src]:
                if u not in seen_urls:
                    seen_urls.add(u); candidates.append((_domain(u), u, ""))

            # 2. fetch ALL candidates in PARALLEL — each with its own live step
            def fetch_one(item):
                if self._cancelled:
                    return None
                title, url, snip = item
                dom = _domain(url)
                label = f"{dom}" + (f" — {title[:60]}" if title and title != dom else "")
                si = self._activity_step(f"reading  {label}")
                body = web_fetch(url, timeout=f_timeout) or snip
                self._note_progress()
                self._activity_done(si, f"read  {label}", ok=bool(body))
                if body:
                    return f"[{title or dom}] {url}\n{body}"
                return None

            got = 0
            if not self._cancelled:
                with ThreadPoolExecutor(max_workers=6) as ex:
                    futs = {ex.submit(fetch_one, it): it for it in candidates}
                    for fut in as_completed(futs):
                        if self._cancelled:
                            break
                        try:
                            r = fut.result()
                        except Exception:
                            r = None
                        if r:
                            out.append(r); got += 1

            self._busy(False); self._activity_end()
            if self._cancelled:
                return
            note = (f"TOOL RESULTS — {got} distinct sources across "
                    f"{len(seen_domains)} domains. Cross-check them, cite the URLs, "
                    "mark single-source claims [UNVERIFIED], and keep going until the "
                    "whole task is finished before you write your final answer:\n\n")
            self.history.append({"role": "user", "content": note + "\n\n".join(out)[:16000]})
            GLib.idle_add(self._ask_model)
        threading.Thread(target=worker, daemon=True).start()

    # ── settings ──
    def open_settings(self, *_):
        dlg = Adw.Window(transient_for=self, modal=True, title="Settings",
                         default_width=600, default_height=680)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(16)
        hb = Adw.HeaderBar(); wrap = Adw.ToolbarView()
        wrap.add_top_bar(hb); wrap.set_content(Gtk.ScrolledWindow(child=box, vexpand=True))
        dlg.set_content(wrap)

        def section(title):
            lb = Gtk.Label(label=title, xalign=0)
            lb.add_css_class("set-section"); lb.set_margin_top(14); box.append(lb)

        def field(label, widget, hint=None):
            lb = Gtk.Label(label=label, xalign=0); lb.add_css_class("set-label")
            box.append(lb); box.append(widget)
            if hint:
                h = Gtk.Label(label=hint, xalign=0, wrap=True); h.add_css_class("set-hint")
                box.append(h)
            return widget

        def slider(lo, hi, step, value, digits=0):
            sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
            sc.set_value(value); sc.set_draw_value(True); sc.set_digits(digits)
            sc.set_hexpand(True)
            return sc

        section("Model \u00b7 account")
        key = field("SiliconFlow API key",
                    Gtk.Entry(text=self.settings.get("siliconflow_api_key", ""),
                              visibility=False, placeholder_text="sk-\u2026"),
                    "cloud.siliconflow.com/account/ak \u2014 Basilisk's key is reused if present.")
        model = field("Chat model", Gtk.Entry(text=self.settings.get("model", DEFAULT_MODEL)))
        vmodel = field("Vision model",
                       Gtk.Entry(text=self.settings.get("vision_model", DEFAULT_VISION)))

        section("Voice")
        tts_on = Gtk.CheckButton(label="Read replies aloud")
        tts_on.set_active(bool(self.settings.get("tts", False)))
        box.append(tts_on)
        eng = Gtk.DropDown.new_from_strings(
            ["auto (Piper, else espeak-ng)", "piper only", "espeak-ng only"])
        eng.set_selected({"auto": 0, "piper": 1, "espeak": 2}.get(
            (self.settings.get("voice_engine") or "auto").lower(), 0))
        field("Engine", eng)
        speed = field("Speed",
                      slider(0.5, 2.0, 0.05, float(self.settings.get("voice_speed", 1.0)), 2),
                      "1.00 is normal. Higher is faster.")
        pitch = field("Pitch (espeak-ng only)",
                      slider(0, 99, 1, int(self.settings.get("voice_pitch", 28))))

        section("Research \u00b7 speed")
        src = field("Sources read per answer",
                    slider(1, 10, 1, self.cfg("research_sources", RESEARCH_MAX_SOURCES, 1, 10)),
                    "Fewer sources = faster answers. 3 is the tuned default.")
        qs = field("Searches per round",
                   slider(1, 4, 1, self.cfg("research_queries", RESEARCH_QUERIES, 1, 4)))
        hops = field("Research depth (rounds)",
                     slider(1, 8, 1, self.cfg("research_hops", MAX_TOOL_HOPS, 1, 8)),
                     "Search\u2192read\u2192think rounds allowed before he must answer.")
        ftmo = field("Page fetch timeout (seconds)",
                     slider(3, 30, 1, self.cfg("fetch_timeout", 6, 3, 30)))

        section("Chats")
        ttl = field("Auto-delete saved chats after (hours)",
                    slider(1, 168, 1, self.cfg("chat_ttl_hours", CHAT_TTL_HOURS, 1, 720)),
                    "Counted from your last activity in a chat, not from when it started.")

        section("Network \u00b7 privacy")
        searx = field("Preferred SearXNG instance (optional)",
                      Gtk.Entry(text=self.settings.get("searx_url", ""),
                                placeholder_text="https://searx.example.org  (blank = built-in list)"),
                      "Search fans out over SearXNG (Brave + Google + DDG), DuckDuckGo as fallback.")
        proxy = field("Proxy for web, images and video (optional)",
                      Gtk.Entry(text=self.settings.get("proxy", ""),
                                placeholder_text="http://host:port"))

        status = Gtk.Label(label="", xalign=0); status.add_css_class("ok")

        def collect():
            eng_key = ["auto", "piper", "espeak"][int(eng.get_selected() or 0)]
            return {
                "siliconflow_api_key": key.get_text().strip(),
                "model": model.get_text().strip() or DEFAULT_MODEL,
                "vision_model": vmodel.get_text().strip() or DEFAULT_VISION,
                "tts": tts_on.get_active(),
                "voice_engine": eng_key,
                "voice_speed": round(float(speed.get_value()), 2),
                "voice_pitch": int(pitch.get_value()),
                "research_sources": int(src.get_value()),
                "research_queries": int(qs.get_value()),
                "research_hops": int(hops.get_value()),
                "fetch_timeout": int(ftmo.get_value()),
                "chat_ttl_hours": int(ttl.get_value()),
                "searx_url": searx.get_text().strip(),
                "proxy": proxy.get_text().strip(),
            }

        def apply_and_save(*_):
            self.settings.update(collect())
            save_settings(self.settings)
            self.tts_btn.set_active(self.settings.get("tts", False))
            if not self.settings.get("tts"):
                stop_speaking()
            self._refresh_sidebar()
            status.set_label("Saved.")

        def test_voice(*_):
            self.settings.update(collect())
            speak("Chuck Norris does not adjust settings. Settings adjust to Chuck Norris. "
                  "This line runs long on purpose, so you can hear it keep going all the way "
                  "to the end without cutting out halfway.", self.settings)
            status.set_label("Speaking\u2026")

        def reset(*_):
            for k in ("voice_engine", "voice_speed", "voice_pitch", "research_sources",
                      "research_queries", "research_hops", "fetch_timeout", "chat_ttl_hours"):
                self.settings.pop(k, None)
            save_settings(self.settings)
            status.set_label("Tuning reset \u2014 reopen Settings to see the defaults.")

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_top(16)
        sv = Gtk.Button(label="Save"); sv.add_css_class("gold")
        sv.connect("clicked", apply_and_save)
        tv = Gtk.Button(label="Test voice"); tv.add_css_class("quick")
        tv.connect("clicked", test_voice)
        rs = Gtk.Button(label="Reset tuning"); rs.add_css_class("quick")
        rs.connect("clicked", reset)
        row.append(sv); row.append(tv); row.append(rs); row.append(status)
        box.append(row)
        dlg.present()


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
