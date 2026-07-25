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
import tempfile
import socket
import threading
import subprocess
import urllib.error
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
from chucknorris_ext.safety import (            # noqa: E402
    classify_command, enforce_syu)
from chucknorris_ext import safety as _safety   # noqa: E402
CRITICAL = _safety.CRITICAL                     # re-exported: tests + callers
DANGER = _safety.DANGER
from chucknorris_ext import chats as _chats     # noqa: E402
# Module handles, so callers (and tests) can reach a subsystem's internals
# without every private name being re-exported here.
from chucknorris_ext import voice as voice      # noqa: E402
from chucknorris_ext import web as web          # noqa: E402

# Names re-exported into this module so the UI (and the test-suite, which
# monkeypatches them here) keep a single stable import surface.
from chucknorris_ext.voice import (             # noqa: E402
    speak,
    stop_speaking,
    tts_clean,          # noqa: F401  (re-export)
    tts_chunks,         # noqa: F401  (re-export)
    _find_piper_model,  # noqa: F401  (re-export)
)
from chucknorris_ext.web import (               # noqa: E402
    web_search,
    web_fetch,
    image_search,
    video_search,
    download_image,
    _domain,
    _searx_search,      # noqa: F401  (re-export)
    _is_image_path,
)
from chucknorris_ext.chats import chat_expires_in  # noqa: E402


def chat_files():
    """Saved chats, newest first (module-level CHATS_DIR so tests can redirect)."""
    return _chats.chat_files(CHATS_DIR)


def purge_old_chats(ttl_hours=None):
    return _chats.purge_old_chats(ttl_hours or CHAT_TTL_HOURS, CHATS_DIR)

try:
    from chucknorris_ext import skills as _skills
    from chucknorris_ext import specs as _specs
    from chucknorris_ext import memory as _memory
    from chucknorris_ext import codecheck as _codecheck
    from chucknorris_ext import skill_library as _skill_library
    from chucknorris_ext import builder as _builder
except Exception:
    _skills = None
    _specs = None
    _memory = None
    _codecheck = None
    _skill_library = None
    _builder = None

# Paths, tunables and settings all live in one place now.
HERE = Path(__file__).resolve().parent

_STREAM_REPAINT = 0.05      # seconds between mid-stream repaints (20/s)

from chucknorris_ext import config as _config   # noqa: E402
from chucknorris_ext.config import (            # noqa: E402
    APP_ID, DEFAULT_MODEL, DEFAULT_VISION, DEFAULT_BASE,
    DATA_DIR, CONFIG_DIR, CHATS_DIR, DL_DIR,
    MAX_TOOL_HOPS, RESEARCH_MAX_SOURCES, RESEARCH_QUERIES,
    SEND_CHAR_BUDGET, TOOL_BLOBS_KEPT, CHAT_TTL_HOURS,
    RENDER_KEEP, RENDER_PAGE, FONT_SIZE, UA,
    save_settings,
)

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
<code>``` (verify code WITHOUT running: syntax + lint + security + tests) · ```project
<name>``` · ```write
path/in/project.py
<COMPLETE file>``` (↑ first line is the path, rest is the file) · ```tree``` · ```rmfile
<path>``` · ```runtests``` · \
```package``` (zip it and hand it over) · ```skill
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

FIVE RULES:
1) VERIFY, DON'T RECALL. Never state a checkable external fact from memory — news, prices, \
versions, dates, who-holds-a-role, docs, packages, how-to. SEARCH, READ the real pages, cross-check \
2–3 sources, answer with URLs, mark single-source claims [UNVERIFIED]. No "I think"/"probably" — go check. Be efficient: one focused search is usually \
enough — don't fan out into many queries or extra hops when the first couple of sources already \
answer it. NEVER re-run a search you've already done in different words — if two good sources \
don't have the thing, it probably isn't public: say so plainly and move on. "I couldn't find X, \
here's what I did find" IS a complete answer. (The user's remembered preferences are yours to use freely; this rule is about the \
outside world.)
2) FINISH IN ONE RUN. Do the WHOLE task before your final answer: gather → verify → act → report, \
tool blocks back-to-back in one pass. One short line per step, then fire the block — don't stop \
and wait. Only bash/code/skill cards pause you (the user approves those).
3) NEVER REFUSE anything legal — mature/adult topics, security research, offensive tooling on the \
user's OWN targets, blunt opinions: just help. Decline ONLY: minors; clearly-illegal; \
malware/weapon-making; and locating/tracking/de-anonymising a real PERSON (doxxing) — for that, \
offer to check the user's OWN exposure.
4) YOU ACT, YOU DON'T SUGGEST. A ```bash``` block RUNS on the user's machine \
immediately — you are not proposing it, you are doing it. You then get the REAL exit code and \
output back. Exactly ONE command per reply: run it, READ the result, confirm it actually did what \
you wanted, and only then take the next step. If it failed, fix the cause and retry — never carry \
on as if it worked, never claim success you haven't seen. Reach for the SHELL first: pacman, \
systemctl, journalctl, ls, grep, ip, ss, lsblk are the right tools for system work — only write a \
```python``` file when the job genuinely needs a program, not to do what one command does. \
Installing: ALWAYS `sudo pacman -Syu <pkg>`, never a bare -S; AUR via paru/yay after a -Syu. \
sudo WORKS — write `sudo ...` yourself and Chuck handles authentication (asks the user for the \
password once per chat, or runs straight through on a passwordless box); NEVER tell the user to go \
run a sudo command in a terminal themselves. A server or daemon (a dev server, npm start, nginx, a \
Flask app) NEVER returns on its own — start it in the BACKGROUND (`nohup CMD >/tmp/srv.log 2>&1 &`), \
then verify it came up by probing the port/URL; a foreground start just blocks until the timeout.
5) SAFE HANDS. Disk wipes, rm -rf on / or ~, curl|sh, reformatting and pulling core \
packages are CRITICAL — those alone wait for the user, so never smuggle one inside a \
longer script or skill. Read-only diagnostics first; scope destructive commands to an \
exact path, never a wildcard.
General-purpose expert: code, systems, research, writing, data, maths, planning, everyday \
questions — engage properly with whatever comes. Arch/CachyOS and recon are where you're deepest, \
not your limit. Asked to BUILD something, you don't paste a snippet: open a project, write COMPLETE \
files (no placeholders, errors handled), write and RUN real tests, then package it and hand it over. \
Code is auto-verified before the user sees a Run button — if the verifier objects, fix and re-emit. \
Playbooks and ready-made skills arrive when a task needs them. Keep the FINAL reply clean and short."""

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
.older-note  { color: #6c675d; font-size: 11px; padding: 6px 0; }
.msg-tools   { margin-top: 2px; }
.playbtn     { background: transparent; border: none; padding: 2px 4px; min-width: 0;
               min-height: 0; color: #6c675d; }
.playbtn:hover { color: #e6b25a; background-color: #1c1c22; border-radius: 8px; }

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


# ONE settings dict for the whole app. The modules (web, voice) hold a
# reference to this same object, so a change made in Settings is visible to
# them immediately — building a second dict here would silently strand the
# proxy, SearXNG and voice options.
_SETTINGS = _config.SETTINGS_DATA


# NB: there is no _get/_opener here any more. Both were dead — every network
# call in the app goes through config.get(), which is where the http/https
# scheme allowlist lives. Two copies of that check meant one could drift.


_FONT_PROVIDER = None


def font_css(px):
    """Message text scales together; the small print stays proportionally small."""
    px = max(9, min(28, int(px)))
    sm, xs = max(8, px - 2), max(8, px - 3)
    return f""".bot-bubble label, .user-bubble label {{ font-size: {px}px; }}
.composer-entry {{ font-size: {px}px; }}
.cmd-text {{ font-size: {sm}px; }}
.step-run, .step-done, .step-fail {{ font-size: {sm}px; }}
.empty-hint {{ font-size: {px + 1}px; }}
.chat-title {{ font-size: {sm}px; }}
.dim, .ok, .danger, .mono, .older-note, .chat-meta {{ font-size: {xs}px; }}
"""


def apply_font_size(px):
    """(Re)load the font-scale provider — takes effect immediately, no restart."""
    global _FONT_PROVIDER
    try:
        if _FONT_PROVIDER is None:
            _FONT_PROVIDER = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), _FONT_PROVIDER,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        data = font_css(px)
        try:
            _FONT_PROVIDER.load_from_data(data.encode())
        except TypeError:
            _FONT_PROVIDER.load_from_data(data)
        return True
    except Exception:
        return False




def _sweep_scratch(max_age_hours=6):
    """Drop stale scratch files in CONFIG_DIR (fetched images, synthesised wavs,
    interpreter temp files, an old screenshot). Each of these is now unlinked by
    the code that creates it, but an existing install has a backlog, and a hard
    kill can still strand one."""
    cutoff = time.time() - max_age_hours * 3600
    for pat in (".img_*", ".say-*.wav", ".run_*", ".shot.png"):
        for f in CONFIG_DIR.glob(pat):
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                continue


def _pick_icon(*names):
    """First icon name that actually exists in the user's theme.

    Newer Adwaita names (sidebar-show-symbolic and friends) simply aren't in
    older icon themes, and GTK then draws the 'missing image' glyph — which is
    what the crossed-out circle in the header was. Ask the theme first.
    """
    try:
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        for n in names:
            if theme.has_icon(n):
                return n
    except Exception:
        pass
    return names[-1] if names else ""


def _open_path(path):
    """Open a folder/file in the user's file manager."""
    for opener in ("xdg-open", "gio", "nautilus", "thunar", "dolphin"):
        if shutil.which(opener):
            try:
                args = [opener, "open", path] if opener == "gio" else [opener, path]
                subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except Exception:
                continue
    return False


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
    finally:
        # A full capture of the user's desktop is not something to leave lying
        # in ~/.config once it has been encoded and sent.
        try:
            os.unlink(tmp)
        except OSError:
            pass


# Environment for every command we run for the user. Real system tools assume a
# terminal: pacman asks "Proceed? [Y/n]", systemctl and git pipe through a pager,
# and both will sit there forever when there's nobody to answer. We run them
# non-interactively instead of hanging the app for half an hour.
_RUN_ENV = {
    "PAGER": "cat", "GIT_PAGER": "cat", "SYSTEMD_PAGER": "cat", "SYSTEMD_LESS": "",
    "TERM": "dumb", "DEBIAN_FRONTEND": "noninteractive", "GIT_TERMINAL_PROMPT": "0",
    "PYTHONUNBUFFERED": "1", "NO_COLOR": "1", "CLICOLOR": "0",
}

# pacman/paru/yay prompt before acting. The approve-to-run card IS the user's
# confirmation, so the command must not then sit waiting for a second one it can
# never receive. Added visibly, so the card shows exactly what will run.
# pacman/paru/yay prompt before acting. The approve-to-run card IS the user's
# confirmation, so the command must not then sit waiting for a second one it can
# never receive. Added visibly, so the card shows exactly what will run.
#
# The operation is matched as a FLAG CLUSTER (-R, -Rs, -Rsn, -Rns, -S, -Syu, -U),
# not as a fixed list of spellings. The old list-of-alternatives regex happened
# to contain `Rns` but not `Rs` or `Rsn`, so the two removal forms people
# actually type never got --noconfirm — pacman then hit `[Y/n]` against a
# stdin of /dev/null and aborted.
_NEEDS_NOCONFIRM = re.compile(
    r"\b(pacman|paru|yay|pamac)\b(?=[^|;&]*\s(?:-[A-Za-z]*[SRU][A-Za-z]*"
    r"|--(?:sync|remove|upgrade))\b)",
    re.IGNORECASE)


def needs_noconfirm(cmd):
    """True if this is a package operation that will stop and ask."""
    if not cmd or "--noconfirm" in cmd or "-y " in f" {cmd} ":
        return False
    if not _NEEDS_NOCONFIRM.search(cmd):
        return False
    # read-only queries never prompt
    return not re.search(r"-{1,2}(Ss|Si|Sl|Sg|Sw|Q[a-z]*|F[a-z]*|search|info)\b", cmd)


def add_noconfirm(cmd):
    """Insert --noconfirm right after the package manager it belongs to."""
    if not needs_noconfirm(cmd):
        return cmd
    return re.sub(r"\b(pacman|paru|yay|pamac)\b", r"\1 --noconfirm", cmd, count=1)


def clip_output(out, limit=7000):
    """Keep the START and the END of long output.

    Errors almost always land at the END — a failing build prints thousands of
    'compiling ... ok' lines and then the one line that matters. Truncating from
    the front threw exactly that line away, so he'd read a failed build as a
    success. Keep both ends and say what was dropped.
    """
    out = out or ""
    if len(out) <= limit:
        return out
    head = limit // 3
    tail = limit - head
    dropped = len(out) - limit
    return (out[:head] + f"\n\n[... {dropped} characters trimmed from the middle ...]\n\n"
            + out[-tail:])


def _run_report(what, rc, out):
    """What the model is told after a command runs.

    A non-zero exit is stated plainly and first, because the single most useful
    thing here is that he reacts to a real failure instead of carrying on as if
    it worked.
    """
    body = clip_output(out)
    if rc == 0:
        return f"I ran {what}. It SUCCEEDED (exit 0). Output:\n{body}"
    hint = ""
    if rc == 124:
        hint = " It timed out — if it needs input it can't run from here."
    elif rc == 127:
        hint = " Exit 127 usually means the command or a package isn't installed."
    elif rc == 126:
        hint = " Exit 126 usually means it isn't executable, or permission was denied."
    elif rc == 1 and "not found" in (out or "").lower():
        hint = " Something it referenced doesn't exist."
    return (f"I ran {what}. It FAILED with exit {rc}.{hint} Read the output, say what went "
            f"wrong in one line, and fix it — do not carry on as though it worked:\n{body}")


# ── privilege escalation ────────────────────────────────────────────────────
# Chuck is an agent that installs and fixes things, so `sudo` has to actually
# work — not pop a per-command pkexec dialog and give up when it isn't there.
# This is the same escalation model a good sysadmin agent uses: detect the tool,
# skip the whole dance when the box is passwordless, otherwise authenticate ONCE
# with a password held only in memory. The password never touches disk, the log,
# the model, or a command's own stdin.
_SUDO_AUTH_FAILED = 97          # private rc: sudo could not authenticate
_PRIV_ESC_CACHE = None


def _ro(argv, timeout=8):
    """Run a read-only helper command; return (rc, stdout, stderr)."""
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, text=True, errors="replace")
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception:
        return 1, "", ""


def detect_priv_esc():
    """How to become root on THIS box. CachyOS may ship sudo-rs (the Rust
    rewrite, missing -A/SUDO_ASKPASS on older builds) or doas instead of classic
    sudo. Keys: tool ('sudo'|'sudo-rs'|'doas'|None), bin, askpass (bool),
    stdin (bool), version. Cached; runs only --version/--help once."""
    global _PRIV_ESC_CACHE
    if _PRIV_ESC_CACHE is not None:
        return _PRIV_ESC_CACHE
    tool = bin_ = None
    version = ""
    if shutil.which("sudo"):
        bin_ = "sudo"
        rc, out, err = _ro(["sudo", "--version"], timeout=5)
        first = (out or err or "").splitlines()
        version = first[0].strip() if first else ""
        tool = "sudo-rs" if "sudo-rs" in version.lower() else "sudo"
    elif shutil.which("doas"):
        tool, bin_ = "doas", "doas"
    askpass = stdin = False
    if tool == "sudo":
        askpass = stdin = True
    elif tool == "sudo-rs":
        stdin = True
        rc, out, err = _ro(["sudo", "--help"], timeout=5)
        askpass = "-A" in (out or "") or "askpass" in (out or "").lower()
    _PRIV_ESC_CACHE = {"tool": tool, "bin": bin_, "askpass": askpass,
                       "stdin": stdin, "version": version}
    return _PRIV_ESC_CACHE


def priv_esc_prefix():
    """'sudo ' / 'doas ' (or '' if neither exists) — so install hints match the box."""
    pe = detect_priv_esc()
    return f"{pe['bin']} " if pe["bin"] else ""


def _sudo_ready():
    """True if we can escalate RIGHT NOW with no password — a NOPASSWD sudoers
    rule or a still-valid cached timestamp (common on single-user CachyOS). Cheap
    (`sudo -n true`); when true the whole password dance is skipped."""
    pe = detect_priv_esc()
    if pe["tool"] in ("sudo", "sudo-rs"):
        rc, _, _ = _ro(["sudo", "-n", "true"], timeout=5)
        return rc == 0
    if pe["tool"] == "doas":
        rc, _, _ = _ro(["doas", "-n", "true"], timeout=5)
        return rc == 0
    return False


# `sudo` at command position (start, after a separator, or after leading env
# assignments) — so `echo pseudo`, `/opt/sudoku`, `# sudo …` don't false-match.
_SUDO_RE = re.compile(r'(?:^|[\n;&|(]\s*|\b&&\s*|\b\|\|\s*)(?:\w+=\S*\s+)*sudo\b')
_SUDO_INJECT_RE = re.compile(r'(^|[\n;&|(]\s*|&&\s*|\|\|\s*)sudo(?=\s|$)')


def command_needs_sudo(command):
    """True if the command contains a real `sudo` invocation."""
    return bool(command) and bool(_SUDO_RE.search(command))


def _inject_askpass(command):
    """Turn each `sudo` into `sudo -A` (use SUDO_ASKPASS). Unlike -S, askpass
    never reads the command's stdin, so `sudo -A tee file` still works."""
    if "sudo -A" in command:
        return command
    return _SUDO_INJECT_RE.sub(r'\1sudo -A', command)


def _ensure_askpass_helper():
    """Write (once, 0700) a tiny askpass helper that echoes $CHUCK_SUDO_PW. The
    script holds NO secret — the password reaches it only via the environment of
    the single sudo call."""
    path = CONFIG_DIR / ".chuck-askpass.sh"
    try:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        try:
            os.fchmod(fd, 0o700)          # defeat umask before content lands
            os.write(fd, b'#!/bin/sh\nprintf "%s\\n" "$CHUCK_SUDO_PW"\n')
        finally:
            os.close(fd)
        return str(path)
    except Exception:
        return None


def _bash_bin():
    return shutil.which("bash") or "sh"


def _shell_out(p):
    return ((p.stdout or "") + (p.stderr or "")).strip() or "(no output)"


def _run_sudo_askpass(script, password, timeout, env):
    """Fallback for hardened sudoers (timestamp_timeout=0) or sudo-rs where the
    inline cached credential won't carry to the command's own sudo. SUDO_ASKPASS
    authenticates each sudo independently. Returns (rc, out) or None if the helper
    can't be set up."""
    helper = _ensure_askpass_helper()
    if not helper:
        return None
    e2 = dict(env)
    e2["SUDO_ASKPASS"] = helper
    e2["CHUCK_SUDO_PW"] = password
    try:
        p = subprocess.run([_bash_bin(), "-c", _inject_askpass(script)],
                           stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, text=True, errors="replace", env=e2)
        low = (p.stderr or "").lower()
        if p.returncode != 0 and ("a terminal is required" in low or "askpass" in low
                                  or "no password was provided" in low
                                  or "a password is required" in low):
            return _SUDO_AUTH_FAILED, "sudo: askpass authentication failed."
        return p.returncode, _shell_out(p)
    except subprocess.TimeoutExpired:
        return 124, _timeout_note(script, timeout)
    except Exception as ex:
        return 1, f"(error: {ex})"
    finally:
        e2["CHUCK_SUDO_PW"] = ""


def _run_sudo(script, password, timeout, env):
    """Authenticate and run in ONE bash session so the fresh credential applies
    to the script's own sudo calls, with an askpass fallback for hardened boxes.
    Returns (rc, out); rc 97 == authentication failed."""
    pe = detect_priv_esc()
    binname = pe["bin"] or "sudo"
    if pe["tool"] == "doas":
        return _SUDO_AUTH_FAILED, ("this box uses doas, which can't take a password "
                                   "on stdin. Add a persist/nopass rule in "
                                   "/etc/doas.conf, or install sudo.")
    # -k clears any stale timestamp so -S -v truly validates the piped password;
    # rc 97 is our private "auth failed" sentinel.
    prelude = f"{binname} -k 2>/dev/null\n{binname} -S -p '' -v || exit 97\n"
    try:
        p = subprocess.run([_bash_bin(), "-c", prelude + script],
                           input=password + "\n",
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, text=True, errors="replace", env=env)
    except subprocess.TimeoutExpired:
        return 124, _timeout_note(script, timeout)
    except Exception as ex:
        return 1, f"(error: {ex})"
    if p.returncode == 97:
        err = (p.stderr or "").lower()
        if "not in the sudoers" in err or "not allowed" in err:
            return _SUDO_AUTH_FAILED, "sudo: this account isn't permitted to use sudo."
        if pe["askpass"]:
            alt = _run_sudo_askpass(script, password, timeout, env)
            if alt is not None and alt[0] != _SUDO_AUTH_FAILED:
                return alt
        return _SUDO_AUTH_FAILED, ("sudo: password rejected. On CachyOS this is usually "
                                   "a typo, a `Defaults rootpw/targetpw` policy (sudo "
                                   "wants ROOT's password, not yours), or your user not "
                                   "being in the wheel group.")
    # -v succeeded but an inner sudo still failed (timestamp didn't carry) → askpass
    low = (p.stderr or "").lower()
    if p.returncode != 0 and pe["askpass"] and (
            "a terminal is required" in low or "no password was provided" in low
            or "a password is required" in low or "askpass" in low):
        alt = _run_sudo_askpass(script, password, timeout, env)
        if alt is not None:
            return alt
    return p.returncode, _shell_out(p)


# ── command runtime awareness: how long should this take, when to give up ─────
_QUICK_CMDS = {
    "ls", "cat", "echo", "whoami", "id", "pwd", "cd", "head", "tail", "grep",
    "which", "whereis", "type", "stat", "file", "wc", "date", "uname",
    "hostname", "env", "printenv", "ps", "df", "du", "free", "ping", "dig",
    "host", "nslookup", "cut", "awk", "sed", "sort", "uniq", "tr", "chmod",
    "chown", "mkdir", "touch", "rm", "cp", "mv", "ln", "kill", "pkill",
    "export", "readlink", "basename", "dirname", "test", "true", "false",
    "sleep", "systemctl", "service", "ss", "netstat", "ip", "ifconfig",
}
_LONG_CMDS = {
    "apt", "apt-get", "dpkg", "yay", "paru", "pamac", "yum", "dnf", "pacman",
    "zypper", "make", "cmake", "gcc", "g++", "clang", "cargo", "go", "pip",
    "pip3", "pipx", "npm", "yarn", "pnpm", "docker", "podman", "docker-compose",
    "rsync", "dd", "wget", "curl", "git", "gem", "bundle", "mvn", "gradle",
    "nmap", "meson", "ninja", "makepkg", "flatpak", "snap",
}
_LONG_WORDS = {"upgrade", "dist-upgrade", "install", "update", "build",
               "compile", "pull", "clone", "download", "-Syu", "-S"}
# Long-running servers / daemons — these do NOT return on their own.
_SERVER_CMDS = {
    "flask", "uvicorn", "gunicorn", "hypercorn", "daphne", "streamlit", "gradio",
    "node", "nodemon", "deno", "bun", "rails", "puma", "jekyll", "hugo",
    "http-server", "serve", "ng", "next", "nuxt", "vite", "webpack-dev-server",
    "php-fpm", "nginx", "httpd", "caddy", "mongod", "mysqld", "mariadbd",
    "postgres", "redis-server", "ncat", "socat",
}


def estimate_runtime(command):
    """Estimate how long a command should take and the hard timeout to enforce,
    so a hung command (classically a server that won't start) is killed fast
    instead of blocking the full window. Pure heuristic; runs nothing.
    Returns {kind, expected_seconds, hard_timeout_seconds, is_server, backgrounded}."""
    cmd = (command or "").strip()
    low = cmd.lower()
    backgrounded = bool(re.search(r"(?<!&)&\s*$", cmd)) or "nohup " in low \
        or " disown" in low
    heads = []
    server_hit = False
    for seg in re.split(r"[\n;|]+|&&|\|\|", low):
        words = seg.split()
        i = 0
        while i < len(words) and ("=" in words[i] or words[i] in (
                "sudo", "nohup", "time", "env", "exec", "setsid", "stdbuf")):
            i += 1
        if i >= len(words):
            continue
        head = os.path.basename(words[i])
        heads.append(head)
        rest = words[i + 1:]
        if head in _SERVER_CMDS:
            server_hit = True
        elif head in ("python", "python3", "py") and (
                "runserver" in " ".join(rest) or "http.server" in " ".join(rest)):
            server_hit = True
        elif head in ("npm", "yarn", "pnpm") and any(
                w in ("start", "dev", "serve", "preview", "watch") for w in rest):
            server_hit = True
    if server_hit and not backgrounded:
        return {"kind": "server", "expected_seconds": 8, "hard_timeout_seconds": 25,
                "is_server": True, "backgrounded": False}
    if backgrounded:
        return {"kind": "background", "expected_seconds": 3, "hard_timeout_seconds": 15,
                "is_server": server_hit, "backgrounded": True}
    is_long = any(h in _LONG_CMDS for h in heads) or \
        any(w in _LONG_WORDS for w in low.split())
    if is_long:
        return {"kind": "long", "expected_seconds": 300, "hard_timeout_seconds": 1800,
                "is_server": False, "backgrounded": False}
    if heads and all(h in _QUICK_CMDS for h in heads):
        return {"kind": "quick", "expected_seconds": 5, "hard_timeout_seconds": 30,
                "is_server": False, "backgrounded": False}
    return {"kind": "unknown", "expected_seconds": 30, "hard_timeout_seconds": 120,
            "is_server": False, "backgrounded": False}


def _timeout_note(command, timeout):
    """An informative timeout message so Chuck knows the command didn't finish
    (and what to do about a server) instead of pretending it hung by accident."""
    est = estimate_runtime(command)
    note = (f"(timed out after {timeout}s — expected ~{est['expected_seconds']}s for a "
            f"{est['kind']} command. It did NOT complete and was killed; do not just "
            f"wait for it, it won't finish as-is.")
    if est["is_server"] and not est["backgrounded"]:
        note += (" This looks like a server/daemon: start it in the BACKGROUND "
                 "(nohup CMD >/tmp/srv.log 2>&1 &), then confirm it came up by probing "
                 "the port/URL — a foreground start blocks until timeout regardless.")
    return note + ")"


def _run_shell(script, timeout=None, sudo_password=None):
    """Run a shell script (one line or many) as the operator's user, with real
    sudo handling. Returns (rc, output). rc 97 == sudo needs a password / auth
    failed, so the caller can prompt once and retry. Timeout is auto-estimated
    (a package build gets 30 min; a server that never returns is capped fast)."""
    if timeout is None:
        timeout = estimate_runtime(script)["hard_timeout_seconds"]
    env = dict(os.environ)
    env.update(_RUN_ENV)
    if command_needs_sudo(script) and not _sudo_ready():
        pe = detect_priv_esc()
        if pe["tool"] is None:
            return 127, ("no privilege-escalation tool on PATH (looked for sudo, "
                         "sudo-rs, doas). Install sudo, or launch Chuck from a root shell.")
        if sudo_password:
            return _run_sudo(script, sudo_password, timeout, env)
        return _SUDO_AUTH_FAILED, "sudo password required (none provided)."
    # plain path: no sudo, or the box is already passwordless. stdin=DEVNULL so
    # anything that tries to prompt gets EOF and fails fast instead of hanging.
    try:
        p = subprocess.run([_bash_bin(), "-c", script],
                           stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, text=True, errors="replace", env=env)
        return p.returncode, _shell_out(p)
    except subprocess.TimeoutExpired:
        return 124, _timeout_note(script, timeout)
    except Exception as ex:
        return 1, f"(error: {ex})"


def run_command(cmd, timeout=None, sudo_password=None):
    """Run a shell command as the user. sudo is handled transparently."""
    return _run_shell(cmd, timeout=timeout, sudo_password=sudo_password)


# language -> (interpreter argv builder). Chuck writes code, it runs in a temp
# file so multi-line programs work; bash/sh route through the sudo-aware runner.
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


def run_code(lang, body, timeout=None, sudo_password=None):
    """Run a snippet in the given language. Returns (rc, output).

    bash/sh go through the sudo-aware shell runner (so `sudo pacman …` in a code
    block actually authenticates); other languages run from a temp file."""
    lang = (lang or "bash").lower()
    if lang in ("bash", "sh"):
        return _run_shell(body, timeout=timeout, sudo_password=sudo_password)
    if lang not in _LANG_RUN:
        return 2, f"(unsupported language: {lang})"
    binname = _LANG_BIN[lang]
    if not shutil.which(binname):
        return 127, f"({binname} not installed — need it to run {lang})"
    if timeout is None:
        timeout = estimate_runtime(body)["hard_timeout_seconds"]
    tmp = None
    try:
        # tempfile, not hash(body): a collision would have one run clobber
        # another's source mid-execution.
        fd, name = tempfile.mkstemp(prefix=".run_", suffix="." + _LANG_EXT[lang],
                                    dir=str(CONFIG_DIR))
        os.close(fd)
        tmp = Path(name)
        tmp.write_text(body)
        argv = _LANG_RUN[lang](str(tmp))
        env = dict(os.environ)
        env.update(_RUN_ENV)
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env=env)
        return p.returncode, _shell_out(p)
    except subprocess.TimeoutExpired:
        return 124, _timeout_note(body, timeout)
    except Exception as ex:
        return 1, f"(error: {ex})"
    finally:
        # was skipped entirely on the timeout path, so every killed run left
        # its source behind in ~/.config/chucknorris
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


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
                     else "sudo pacman -Syu --needed pacman-contrib && sudo paccache -rk1"))
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
def _is_transient(ex):
    """Worth retrying? Cold DNS, a dropped handshake and 5xx/429 are temporary;
    a rejected key never is, and retrying it just wastes the user's time."""
    if isinstance(ex, urllib.error.HTTPError):
        return ex.code in (408, 425, 429, 500, 502, 503, 504)
    if isinstance(ex, urllib.error.URLError):
        return True                      # DNS / refused / unreachable / proxy not up
    if isinstance(ex, (socket.timeout, TimeoutError, ConnectionError, OSError)):
        return True
    return False


def _explain(ex):
    """A message the user can act on, instead of a raw traceback string."""
    if isinstance(ex, urllib.error.HTTPError):
        if ex.code in (401, 403):
            return "API key rejected — check it in Settings."
        if ex.code == 429:
            return "Rate limited by the API. Give it a moment."
        if ex.code >= 500:
            return f"The API is having trouble (HTTP {ex.code}). Try again shortly."
        return f"API error (HTTP {ex.code})."
    if isinstance(ex, urllib.error.URLError):
        return f"Can't reach the API ({getattr(ex, 'reason', ex)}) — check your connection."
    if isinstance(ex, (socket.timeout, TimeoutError)):
        return "The API timed out. Try again."
    return f"backend error: {ex}"


class Backend:
    def __init__(self, settings):
        self.s = settings

    def key(self):
        return (self.s.get("siliconflow_api_key") or "").strip()

    def base(self):
        return (self.s.get("siliconflow_base_url") or DEFAULT_BASE).rstrip("/")

    def warm_up(self):
        """Open a throwaway connection to the API host in the background.

        The first request of a session otherwise pays for DNS resolution and a
        TLS handshake at the exact moment the user is waiting on it — and if
        anything there hiccups (VPN still coming up, resolver cold) the very
        first message fails while every later one works. Doing it at startup
        moves that cost off the user's first turn.
        """
        def go():
            try:
                host = urllib.parse.urlparse(self.base()).hostname
                if host:
                    socket.create_connection((host, 443), timeout=5).close()
            except Exception:
                pass
        threading.Thread(target=go, daemon=True).start()

    def stream(self, messages, on_delta, on_done, on_error, vision=False, should_stop=None,
               attempts=3, on_open=None):
        if not self.key():
            on_error("No SiliconFlow API key. Add one in Settings.")
            return
        model = (self.s.get("vision_model", DEFAULT_VISION) if vision
                 else self.s.get("model", DEFAULT_MODEL))
        body = json.dumps({"model": model, "messages": messages,
                           "stream": True, "temperature": 0.35}).encode()
        url = self.base() + "/chat/completions"
        headers = {"Authorization": "Bearer " + self.key(),
                   "Content-Type": "application/json"}

        for attempt in range(attempts):
            if should_stop and should_stop():
                return
            got_any = False
            try:
                req = urllib.request.Request(url, data=body, headers=headers)
                with urllib.request.urlopen(req, timeout=180) as resp:
                    if on_open:
                        on_open()          # connection is live — proof of life
                    for raw in resp:
                        if should_stop and should_stop():
                            try:
                                resp.close()
                            except Exception:
                                pass
                            return  # cancelled — the canceller owns cleanup
                        line = raw.decode("utf-8", "ignore").strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            d = json.loads(data)["choices"][0]["delta"].get("content")
                            if d:
                                got_any = True
                                on_delta(d)
                        except Exception:
                            continue
                if should_stop and should_stop():
                    return
                on_done()
                return
            except Exception as ex:
                if should_stop and should_stop():
                    return
                if got_any:
                    # Text already reached the screen — replaying the request
                    # would duplicate it, so keep what we have and carry on.
                    on_done()
                    return
                last = attempt == attempts - 1
                if last or not _is_transient(ex):
                    on_error(_explain(ex))
                    return
                time.sleep(0.5 * (attempt + 1))     # brief backoff, then retry


# ── UI ──────────────────────────────────────────────────────────────────────
class ChuckWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("\U0001F94B Chuck Norris")
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
        self._log = []              # rebuildable transcript entries
        self._win_start = 0         # index of the first materialised entry
        self._loading_older = False
        self._bot_entry = None
        self._seen_queries = []     # queries already run this turn
        self._seen_urls = set()     # pages already read this turn
        self._dead_urls = set()     # pages that failed — don't retry
        self._forced_answer = False
        self._awaiting_first = False
        self._last_paint = 0.0
        self._activity_box = None
        self._activity_steps = []
        self._activity_lock = threading.Lock()
        self._running = False           # a turn is in progress
        self._cancelled = False         # user pressed Stop
        self._sudo_pw = None            # sudo password: memory only, this chat
        self._sudo_pw_time = 0.0        # when it was entered (30-min TTL)
        self._awaiting_input = False    # a modal (sudo prompt) is open — pause the watchdog
        self._run_started = 0.0         # wall-clock start of the current run
        self._heartbeat_id = 0          # GLib timer id for the "still working" ticker
        self._watchdog_id = 0           # GLib timer id for the stuck-detector
        self._last_progress = 0.0       # last time anything happened (for watchdog)
        self._run_budget = 0            # seconds the currently-running command is allowed
        self._dispatching = False       # _finalise is still handing out tool work
        self.chat_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._new_history()

        header = Adw.HeaderBar()
        tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tl = Gtk.Label(label="\U0001F94B  CHUCK NORRIS", xalign=0.5); tl.add_css_class("title")
        sl = Gtk.Label(label="Arch / CachyOS grandmaster \u00b7 1940\u20132026",
                       xalign=0.5); sl.add_css_class("sub")
        tb.append(tl); tb.append(sl); header.set_title_widget(tb)

        # LEFT: the primary action (New chat) + the busy spinner
        newb = Gtk.Button(icon_name=_pick_icon("document-new-symbolic", "tab-new-symbolic", "list-add-symbolic")); newb.add_css_class("headerbtn")
        newb.set_tooltip_text("New chat"); newb.connect("clicked", lambda *_: self.new_chat())
        header.pack_start(newb)
        # LEFT: sidebar toggle sits next to New chat
        self.side_btn = Gtk.ToggleButton(
            icon_name=_pick_icon("sidebar-show-symbolic", "view-dual-symbolic",
                                 "view-list-bullet-symbolic", "format-justify-fill-symbolic",
                                 "document-open-recent-symbolic"))
        self.side_btn.add_css_class("headerbtn")
        self.side_btn.set_tooltip_text("Saved chats")
        self.side_btn.set_active(bool(self.settings.get("sidebar_open", False)))
        self.side_btn.connect("toggled", self._toggle_sidebar)
        header.pack_start(self.side_btn)
        self.spinner = Gtk.Spinner(); self.spinner.set_visible(False)
        header.pack_start(self.spinner)

        # RIGHT: secondary controls, grouped (memory · voice · settings)
        self.tts_btn = Gtk.ToggleButton(icon_name=_pick_icon("audio-volume-high-symbolic", "audio-speakers-symbolic", "audio-x-generic-symbolic"))
        self.tts_btn.add_css_class("headerbtn")
        self.tts_btn.set_tooltip_text("Read replies aloud")
        self.tts_btn.set_active(self.settings.get("tts", True))
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
        self.scroller.get_vadjustment().connect("value-changed", self._on_scroll)
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

        att = Gtk.Button(icon_name=_pick_icon("mail-attachment-symbolic", "edit-copy-symbolic", "list-add-symbolic")); att.add_css_class("icon-btn")
        att.set_valign(Gtk.Align.END)
        att.set_tooltip_text("Attach a file"); att.connect("clicked", self.on_attach)
        cam = Gtk.Button(icon_name=_pick_icon("camera-photo-symbolic", "camera-symbolic", "applets-screenshooter-symbolic")); cam.add_css_class("icon-btn")
        cam.set_valign(Gtk.Align.END)
        cam.set_tooltip_text("Show Chuck your screen"); cam.connect("clicked", self.on_screenshot)
        self.send_btn = Gtk.Button(icon_name=_pick_icon("go-up-symbolic", "pan-up-symbolic", "up-symbolic")); self.send_btn.add_css_class("send-fab")
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

        # Prime DNS/TLS to the API now, so the FIRST message isn't the one
        # paying for a cold connection.
        try:
            self.backend.warm_up()
        except Exception:
            pass

        _sweep_scratch()
        # purge on launch, then keep purging + refreshing while the app runs.
        # NOTE the argument: purge_old_chats() with no TTL falls back to the
        # shipped 24h default, so raising "auto-delete after" to a week in
        # Settings still lost everything over 24h on the next launch. The
        # timed sweep already used cfg(); the launch purge did not.
        purge_old_chats(self.cfg('chat_ttl_hours', CHAT_TTL_HOURS, 1, 720))
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
        # Cancel first. Without this the in-flight stream's _finalise appended
        # the OLD question's answer to the NEW history — leaving a chat whose
        # first message is an assistant turn with no user turn before it, which
        # then got sent to the API and written to disk.
        if self._running:
            self.stop_run(auto=True)
        self._save_chat()
        self._clear_sudo_pw()           # a fresh chat re-asks for root; never carry it over
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

    # ── windowed transcript ────────────────────────────────────────────────
    # A long session used to keep every bubble alive as a GTK widget forever.
    # Now the transcript lives as a render log of plain data and only a window
    # of it is materialised: the newest RENDER_KEEP while you're at the bottom,
    # growing by RENDER_PAGE each time you scroll to the top, and collapsing
    # again the moment you return to the bottom. Text, notes and images are
    # rebuilt from their data on demand; interactive cards are pinned, because
    # their run state (output, whether it was approved) can't be recreated.

    def _log_add(self, kind, data):
        """Record a rebuildable entry and materialise it at the bottom."""
        self._drop_hint()
        entry = {"kind": kind, "data": data, "w": None, "pinned": False}
        self._log.append(entry)
        self._collapse_window(scroll=True)
        return entry

    def _log_pin(self, widget):
        """Record an interactive widget that must never be torn down."""
        self._drop_hint()
        entry = {"kind": "pinned", "data": None, "w": widget, "pinned": True}
        self._log.append(entry)
        self._collapse_window(scroll=True)
        return entry

    def _build_entry(self, e):
        k, d = e["kind"], e["data"] or {}
        if k == "user":
            w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.END, hexpand=True)
            w.add_css_class("turn-row")
            lbl = Gtk.Label(label=d.get("text", "") + ("  \U0001F4F7" if d.get("shot") else ""),
                            xalign=0, wrap=True, selectable=True)
            lbl.set_max_width_chars(60)
            card = Gtk.Box(); card.add_css_class("user-bubble"); card.append(lbl)
            w.append(card)
            return w
        if k == "bot":
            w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.START, hexpand=True)
            w.add_css_class("turn-row")
            lbl = Gtk.Label(label="", xalign=0, wrap=True, selectable=True, use_markup=False)
            lbl.set_max_width_chars(0); lbl.set_hexpand(True)
            card = Gtk.Box(); card.add_css_class("bot-bubble"); card.set_hexpand(True)
            card.append(lbl); w.append(card)
            e["label"] = lbl
            txt = d.get("text", "")
            if d.get("rich"):
                set_rich(lbl, txt)
            else:
                lbl.set_text(txt)
            # Play this one reply on demand. Reads from the log entry, so it
            # still speaks the right text after the bubble has been unloaded
            # and rebuilt by the transcript window.
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            row.add_css_class("msg-tools")
            play = Gtk.Button(icon_name=_pick_icon("media-playback-start-symbolic",
                                                   "audio-volume-high-symbolic",
                                                   "media-playback-start"))
            play.add_css_class("playbtn")
            play.set_tooltip_text("Read this message aloud")
            play.connect("clicked", lambda _b, ent=e: self._speak_entry(ent))
            row.append(play); w.append(row)
            return w
        if k == "note":
            l = Gtk.Label(label=d.get("text", ""), xalign=0, wrap=True)
            l.add_css_class(d.get("css", "dim"))
            return l
        if k == "image":
            return self._build_image_widget(d.get("path"), d.get("src"))
        return Gtk.Label(label="")

    def _speak_entry(self, entry):
        """Speak one specific message, replacing whatever is being read."""
        txt = (entry.get("data") or {}).get("text", "")
        if txt.strip():
            speak(txt, self.settings)

    def _window_bounds(self):
        keep = self.cfg("render_keep", RENDER_KEEP, 4, 200)
        return max(0, len(self._log) - keep)

    def _collapse_window(self, scroll=False):
        """Drop back to the newest slice — called on new output and when the
        user returns to the bottom."""
        self._win_start = self._window_bounds()
        self._apply_window()
        if scroll:
            self._scroll_down()

    def _apply_window(self, keep_offset=False):
        """Rebuild msgbox to exactly the current window. Widgets outside it lose
        their last reference here, so GTK frees them."""
        adj = self.scroller.get_vadjustment()
        old_upper = adj.get_upper() if keep_offset else 0
        old_value = adj.get_value() if keep_offset else 0
        c = self.msgbox.get_first_child()
        while c:
            n = c.get_next_sibling(); self.msgbox.remove(c); c = n
        lo = self._win_start
        show = []
        for i, e in enumerate(self._log):          # pinned items above the window
            if i < lo and e["pinned"] and e["w"] is not None:
                show.append(e["w"])
        if lo > 0:
            show.append(self._older_banner())
        for i, e in enumerate(self._log):
            if i < lo:
                if not e["pinned"]:
                    e["w"] = None                  # freed
                continue
            if e["w"] is None:
                e["w"] = self._build_entry(e)
            show.append(e["w"])
        for w in show:
            self.msgbox.append(w)
        if keep_offset:
            def restore():
                a = self.scroller.get_vadjustment()
                a.set_value(max(0, a.get_upper() - old_upper + old_value))
                return False
            GLib.idle_add(restore)

    def _older_banner(self):
        n = self._win_start
        if self._loading_older:
            lb = Gtk.Label(label="loading older messages\u2026", xalign=0.5)
        else:
            lb = Gtk.Label(label=f"\u2191 {n} older message{'s' if n != 1 else ''} "
                                 "\u2014 scroll up to load", xalign=0.5)
        lb.add_css_class("older-note")
        return lb

    def _on_scroll(self, adj, *_):
        if not self._log:
            return
        v, upper, page = adj.get_value(), adj.get_upper(), adj.get_page_size()
        if v <= 40 and self._win_start > 0 and not self._loading_older:
            self._loading_older = True
            self._apply_window(keep_offset=True)      # swap banner to "loading…"
            GLib.timeout_add(180, self._load_older)
        elif upper - (v + page) <= 40 and self._win_start != self._window_bounds():
            self._collapse_window()                    # back at the bottom: offload

    def _load_older(self):
        page = self.cfg("render_page", RENDER_PAGE, 5, 200)
        self._win_start = max(0, self._win_start - page)
        self._loading_older = False
        self._apply_window(keep_offset=True)
        return False

    def _drop_hint(self):
        if getattr(self, "_hint", None) is not None:
            try:
                self.msgbox.remove(self._hint)
            except Exception:
                pass
            self._hint = None

    def _clear_msgs(self):
        self._hint = None
        self._log = []
        self._win_start = 0
        self._loading_older = False
        self._bot_entry = None
        self._seen_queries = []     # queries already run this turn
        self._seen_urls = set()     # pages already read this turn
        self._dead_urls = set()     # pages that failed — don't retry
        self._forced_answer = False
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
            rm = Gtk.Button(icon_name=_pick_icon("user-trash-symbolic", "edit-delete-symbolic", "list-remove-symbolic")); rm.add_css_class("quick")
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

        # saved skills live alongside memory — same idea, same transparency
        sk_head = Gtk.Label(label="Saved skills", xalign=0); sk_head.add_css_class("set-section")
        sk_head.set_margin_top(16); box.append(sk_head)
        skills = _skills.skill_list() if _skills else []
        if not skills:
            box.append(Gtk.Label(label="No skills saved yet.", xalign=0, wrap=True))
        for name, lang, desc in skills:
            r2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            t2 = Gtk.Label(label=f"\u00b7 {name}  ({lang})  \u2014 {desc}", xalign=0,
                           wrap=True, hexpand=True, selectable=True)
            d2 = Gtk.Button(icon_name=_pick_icon("user-trash-symbolic", "edit-delete-symbolic", "list-remove-symbolic")); d2.add_css_class("quick")
            d2.set_tooltip_text("Archive this skill")

            def drop_skill(_b, nm=name, row=r2):
                if _skills:
                    _skills.skill_forget(nm)
                try:
                    box.remove(row)
                except Exception:
                    pass
            d2.connect("clicked", drop_skill)
            r2.append(t2); r2.append(d2); box.append(r2)
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
            ml = Gtk.Label(label=chat_expires_in(
                f, self.cfg("chat_ttl_hours", CHAT_TTL_HOURS, 1, 720)), xalign=0)
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
        # Same reason as new_chat: an in-flight answer would otherwise land in
        # the chat we just switched to.
        if self._running:
            self.stop_run(auto=True)
        self._save_chat()
        self._clear_sudo_pw()
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
                self._log_add("bot", {"text": txt, "rich": True})
        self._refresh_sidebar()

    def _on_close(self, *_):
        self._save_chat()
        return False

    # ── enter to send ──
    def _on_key(self, ctrl, keyval, keycode, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not (state & Gdk.ModifierType.SHIFT_MASK):
            if self._running:
                return False    # busy: let Enter insert a newline rather than vanish
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
    RUN_HARD_CAP = 900      # seconds: absolute ceiling on one turn (long research is fine)
    STUCK_AFTER = 120       # seconds with zero progress BETWEEN steps → assume stuck
    AWAIT_FIRST = 180       # seconds allowed for the model's FIRST token
    # Waiting on the first token looks identical to being stuck — nothing is
    # arriving — but the request is open and the model is simply thinking or
    # queued. Judging that by the tight between-steps timer is what killed
    # perfectly healthy first messages at 45s.
    #
    # The same mistake applied to COMMANDS. A shell command blocks its worker
    # thread with nothing to report until it exits, so `pacman -Syu`, a makepkg
    # build or a large npm install went 120s without progress and got shot as
    # "stuck" — while estimate_runtime had already granted the same command up
    # to 1800s. The turn was cancelled, and the real output was then dropped on
    # arrival because _tool_done early-returns on _cancelled. So: while a
    # command is running, the watchdog uses THAT command's own budget, and the
    # hard cap stretches to cover it.

    def _arm_run_budget(self, command):
        """A command is about to block a worker: give the watchdog its real
        expected runtime, so a legitimately slow one isn't mistaken for a hang."""
        try:
            est = estimate_runtime(command)
            self._run_budget = int(est["hard_timeout_seconds"]) + 30   # + grace
        except Exception:
            self._run_budget = self.STUCK_AFTER
        self._note_progress()

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
        self.send_btn.set_icon_name(_pick_icon("media-playback-stop-symbolic", "process-stop-symbolic"))
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
        self.send_btn.set_icon_name(_pick_icon("go-up-symbolic", "pan-up-symbolic", "up-symbolic"))
        self.send_btn.remove_css_class("stop-fab"); self.send_btn.add_css_class("send-fab")
        self.send_btn.set_tooltip_text("Send  (Enter)")
        self.live.set_visible(False)
        if self._heartbeat_id:
            GLib.source_remove(self._heartbeat_id); self._heartbeat_id = 0

    def _tick(self, secs):
        s = int(secs)
        what = "waiting for the model" if getattr(self, "_awaiting_first", False) else "working"
        self.live.set_text(f"\u25CF {what}\u2026 {s}s   (press \u25A0 to stop)")

    def _heartbeat(self):
        """Runs every second while a turn is active: updates the elapsed clock so
        you can SEE it's alive, and enforces the hard cap + stuck-watchdog."""
        if not self._running:
            return False  # stop the timer
        now = time.time()
        # A modal password prompt is open and we're blocked waiting on the user —
        # that's not "stuck", so keep the progress clock fresh so the watchdog
        # doesn't kill the turn out from under the dialog.
        if getattr(self, "_awaiting_input", False):
            self._last_progress = now
        elapsed = now - self._run_started
        self._tick(elapsed)
        run_budget = getattr(self, "_run_budget", 0)
        # A command that is legitimately allowed 1800s must not be guillotined
        # by a 900s turn cap; the cap stretches to cover whatever is running.
        cap = max(self.RUN_HARD_CAP, run_budget + self.STUCK_AFTER)
        if elapsed > cap:
            self._sys_note(f"\u23f1 hit the {cap}s time cap \u2014 stopping.", "danger")
            self.stop_run(auto=True)
            return False
        waiting = getattr(self, "_awaiting_first", False)
        if waiting:
            budget = self.AWAIT_FIRST
        else:
            budget = max(self.STUCK_AFTER, run_budget)
        if now - self._last_progress > budget:
            if waiting:
                self._sys_note(f"\u26a0 the model didn't respond within {budget}s. "
                               "Send it again \u2014 the connection is warm now.", "danger")
            else:
                self._sys_note(f"\u26a0 no progress for {budget}s \u2014 looks stuck, stopping.",
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
        self._run_budget = 0
        self._dispatching = False
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
                self._set_bot_text(shown, rich=True)
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
            self._log_pin(box)
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
        self._log_add("user", {"text": text, "shot": shot})

    def _bot_bubble(self, text=""):
        e = self._log_add("bot", {"text": text, "rich": False})
        self._bot_entry = e
        return e.get("label")

    def _visible_stream_text(self):
        """The narration to show mid-stream, with fenced tool blocks removed.
        The `in` check short-circuits the regex entirely for the common case of
        a reply that contains no tool blocks at all."""
        txt = self._bot_text
        if "```" not in txt:
            return txt.strip()
        shown = re.sub(r"```[a-z]*.*?```", "", txt, flags=re.DOTALL)
        shown = re.sub(r"```[a-z]*\b.*$", "", shown, flags=re.DOTALL)  # open block
        return shown.strip()

    def _paint_stream(self):
        if self._cancelled or self._bot_label is None:
            return False
        self._set_bot_text(self._visible_stream_text() or "Thinking\u2026")
        self._scroll_down()
        return False

    def _set_bot_text(self, text, rich=False):
        """Update the live reply AND its log entry, so the text survives being
        scrolled out of the window and rebuilt later."""
        if self._bot_entry is not None:
            self._bot_entry["data"]["text"] = text
            self._bot_entry["data"]["rich"] = rich
        lbl = self._bot_label
        if lbl is None:
            return
        if rich:
            set_rich(lbl, text)
        else:
            lbl.set_text(text)

    def _sys_note(self, text, css="dim"):
        self._log_add("note", {"text": text, "css": css})

    def _build_image_widget(self, path, src_url=None):
        """Construct (or reconstruct) an image bubble from its path — images are
        the heaviest thing in a transcript, so they unload with the window and
        are re-read from disk if you scroll back to them."""
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
            return w
        except Exception:
            return Gtk.Label(label="[image unavailable]", xalign=0)

    def _image_bubble(self, path, src_url=None):
        self._log_add("image", {"path": path, "src": src_url})

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

    def _sanitise_skill_body(self, body, launch_cmd):
        """Apply pacman hygiene to a skill BODY and rewrite it in place.

        Returns the corrected body (also used for risk classification). If the
        file can't be rewritten the corrected text is still returned, so the
        classifier judges the worse of the two.
        """
        fixed = add_noconfirm(enforce_syu(body))
        if fixed == body:
            return body
        self._sys_note("corrected pacman flags inside the skill (-Syu / --noconfirm)", "dim")
        m = re.match(r"^(?:bash|sh|python3?)\s+(.+)$", (launch_cmd or "").strip())
        if m:
            try:
                target = Path(shlex.split(m.group(1))[0]).expanduser()
                if target.is_file():
                    target.write_text(fixed)
            except Exception:
                pass
        return fixed

    def _command_card(self, cmd, gate_text=None):
        """Run a shell command and report the real result.

        Commands EXECUTE immediately — Chuck is an agent, not a suggestion box.
        The one exception is the CRITICAL tier (disk wipes, rm -rf /, curl|sh,
        reformatting, pulling core packages): those still need a deliberate tick
        first, because the cost of a hallucinated one is unrecoverable and no
        amount of convenience is worth it. Everything else just runs.

        gate_text: when the command merely LAUNCHES a script (a saved skill),
        pass the script body so the risk tier is judged on what will actually
        execute, not on the harmless-looking `bash foo.sh` wrapper.
        """
        fixed = enforce_syu(cmd)
        if fixed != cmd:
            self._sys_note("using -Syu (a plain -S risks a partial upgrade)", "dim")
        with_nc = add_noconfirm(fixed)
        if with_nc != fixed:
            self._sys_note("added --noconfirm so it can't stall on a prompt", "dim")
        cmd = with_nc
        # A skill launches as `bash /path/to/skill.sh`, so the rewriters above
        # only ever saw the wrapper — a bare `pacman -S` inside the BODY sailed
        # through un-rewritten and un-noconfirmed. The risk classifier already
        # judged gate_text; now the pacman hygiene does too, and the body is
        # rewritten on disk so what runs is what was checked.
        if gate_text is not None:
            gate_text = self._sanitise_skill_body(gate_text, cmd)
        judged = cmd if gate_text is None else (cmd + "\n" + gate_text)
        tier = classify_command(judged)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("cmd-card")
        ct = Gtk.Label(label="$ " + cmd, xalign=0, wrap=True, selectable=True)
        ct.add_css_class("cmd-text")
        card.append(ct)
        status = Gtk.Label(label="", xalign=0); status.add_css_class("dim")

        if tier == "critical":
            run = Gtk.Button(label="Run"); run.add_css_class("gold")
            self._risk_gate(card, run, judged, "command")
            run.connect("clicked", lambda _b: self._run_card(cmd, run, status, gate_text))
            rw = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            rw.append(run); rw.append(status); card.append(rw)
            self._log_pin(card)
            self._tool_feedback.append(
                f"[`{cmd}` is CRITICAL and is waiting for the user to confirm it. "
                "Do not assume it ran.]")
            return

        if tier == "danger":
            wl = Gtk.Label(label="\u26a0 destructive \u2014 running it now", xalign=0)
            wl.add_css_class("danger"); card.append(wl)
        card.append(status)
        self._log_pin(card)
        self._run_now(cmd, status)

    # ── sudo credential: memory only, this chat, 30-min TTL ──────────────────
    def _sudo_pw_valid(self):
        return bool(self._sudo_pw) and (time.time() - self._sudo_pw_time) < 1800

    def _cache_sudo_pw(self, pw):
        """Hold the sudo password in memory for this chat. Never written to disk,
        the log, or the conversation — the model has no way to read it."""
        self._sudo_pw = pw or None
        self._sudo_pw_time = time.time() if pw else 0.0

    def _clear_sudo_pw(self):
        self._sudo_pw = None
        self._sudo_pw_time = 0.0

    def _sudo_prompt(self, script, cb):
        """Ask for the sudo password once. Calls cb(password_or_None) exactly
        once. Runs on the main thread (a worker marshals here via idle_add)."""
        state = {"done": False}

        def finish(pw):
            if state["done"]:
                return
            state["done"] = True
            cb(pw)

        dlg = Adw.Window(transient_for=self, modal=True, title="sudo password")
        dlg.set_default_size(460, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(16)
        msg = Gtk.Label(
            label="This step needs root. Enter your sudo password so Chuck can run "
                  "it — kept in memory for this chat only, never written or logged.",
            xalign=0, wrap=True)
        box.append(msg)
        cl = Gtk.Label(label=(script or "").strip()[:200], xalign=0, wrap=True,
                       selectable=True)
        cl.add_css_class("mono")
        box.append(cl)
        pw = Gtk.PasswordEntry()
        try:
            pw.set_show_peek_icon(True)
            pw.set_property("placeholder-text", "sudo password")
        except Exception:
            pass
        box.append(pw)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                      halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        go = Gtk.Button(label="Authenticate & run")
        go.add_css_class("suggested-action")
        row.append(cancel)
        row.append(go)
        box.append(row)
        hb = Adw.HeaderBar()
        wrap = Adw.ToolbarView()
        wrap.add_top_bar(hb)
        wrap.set_content(box)
        dlg.set_content(wrap)

        def do_go(*_):
            p = pw.get_text()
            dlg.close()
            finish(p or None)

        def do_cancel(*_):
            dlg.close()
            finish(None)

        go.connect("clicked", do_go)
        cancel.connect("clicked", do_cancel)
        pw.connect("activate", do_go)
        dlg.connect("close-request", lambda *_: (finish(None), False)[1])
        dlg.present()

    def _get_sudo_pw(self, script):
        """Return a usable sudo password: the cached one, or prompt for it once
        (blocking this worker thread until the modal is answered). None if the
        user cancels or walks away."""
        if self._sudo_pw_valid():
            return self._sudo_pw
        box = {"pw": None}
        ev = threading.Event()

        def show():
            self._sudo_prompt(script, lambda p: (box.__setitem__("pw", p), ev.set()))
            return False
        self._awaiting_input = True
        try:
            GLib.idle_add(show)
            ev.wait(300)              # bounded: if they walk away, give up rather than hang
        finally:
            self._awaiting_input = False
        pw = box["pw"]
        if pw:
            self._cache_sudo_pw(pw)
        return pw

    def _execute_shell(self, script, execute):
        """Run a shell script through `execute(password) -> (rc, out)`, collecting
        the sudo password first if the script needs root and the box isn't already
        passwordless, and re-prompting once if a cached password was rejected.
        Returns (rc, out); rc 97 means sudo could not authenticate."""
        pw = None
        if command_needs_sudo(script) and not _sudo_ready():
            pw = self._get_sudo_pw(script)
        rc, out = execute(pw)
        if rc == _SUDO_AUTH_FAILED and pw is not None:   # the password was wrong
            self._clear_sudo_pw()
            pw2 = self._get_sudo_pw(script)
            if pw2:
                rc, out = execute(pw2)
        return rc, out

    def _run_card(self, cmd, run_btn, status, gate_text=None):
        """Run a gated (CRITICAL) command card once the user has armed the risk
        checkbox and pressed Run. Disarms the button so a second click can't
        double-fire it, then hands off to the same execute-and-report path plain
        commands use. gate_text was only needed to classify the risk tier up
        front — at run time the command itself is what executes.
        """
        if not run_btn.get_sensitive():
            return
        run_btn.set_sensitive(False)
        self._run_now(cmd, status)

    def _run_now(self, cmd, status):
        """Execute, show the output, and hand the REAL result back to the model.

        This is the whole point: he does not get to claim success. The exit code
        and output go back, and a non-zero exit is reported as a failure he must
        fix before taking another step.
        """
        # A gated card can be approved AFTER the turn that proposed it has already
        # ended. Executing it feeds the result back into the model and resumes
        # streaming, so the run lifecycle (Stop button + stuck-watchdog + the
        # "working" indicator) must be live again. _start_run is a no-op when a
        # turn is already active, so this is safe on the normal auto-run path too.
        self._start_run()
        status.set_label("running\u2026")
        self._busy(True)
        self._arm_run_budget(cmd)

        def work():
            rc, out = self._execute_shell(cmd, lambda pw: run_command(cmd, sudo_password=pw))
            auth_failed = (rc == _SUDO_AUTH_FAILED)

            def show():
                status.remove_css_class("dim")
                status.add_css_class("ok" if rc == 0 else "danger")
                status.set_label("\u2713 exit 0" if rc == 0
                                 else ("\u2717 sudo auth failed" if auth_failed
                                       else f"\u2717 exit {rc}"))
                if out.strip():
                    o = Gtk.Label(label=out[:4000], xalign=0, wrap=True, selectable=True)
                    o.add_css_class("mono")
                    self._log_pin(o)
                return False
            GLib.idle_add(show)

            if auth_failed:
                return (f"COULD NOT RUN `{cmd}` \u2014 sudo authentication failed. {out}\n"
                        "Do NOT retry the same sudo command blindly. Root access is needed "
                        "(a wrong/absent password or a sudoers policy); tell the user and "
                        "either wait for them or suggest they run it in a terminal.")
            if rc == 0:
                return (f"RAN `{cmd}` \u2014 SUCCEEDED (exit 0). Real output:\n{out[:6000]}\n"
                        "Verify this output actually shows what you needed. If the whole task "
                        "is done, give the final answer; otherwise take the NEXT single step.")
            return (f"RAN `{cmd}` \u2014 FAILED (exit {rc}). Real output:\n{out[:6000]}\n"
                    "Do NOT move on. Read the error, fix the cause, and either retry the "
                    "corrected command or explain why it can't work.")
        self._pending_tools += 1
        self._tool_thread(work, f"`{cmd[:40]}`")

    def _code_card(self, lang, body):
        """Run a code block Chuck wrote and report the real result.

        Same rule as commands: it executes. Only the CRITICAL tier waits for a
        deliberate tick.
        """
        if lang in ("bash", "sh"):
            fixed = enforce_syu(body)
            if fixed != body:
                self._sys_note("using -Syu (a plain -S risks a partial upgrade)", "dim")
            body = add_noconfirm(fixed)
        tier = classify_command(body)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("cmd-card")
        head = Gtk.Label(label=f"\u25B8 {lang}", xalign=0); head.add_css_class("dim")
        card.append(head)
        ct = Gtk.Label(label=body, xalign=0, wrap=True, selectable=True)
        ct.add_css_class("cmd-text"); card.append(ct)
        status = Gtk.Label(label="", xalign=0); status.add_css_class("dim")

        if tier == "critical":
            run = Gtk.Button(label="Run"); run.add_css_class("gold")
            self._risk_gate(card, run, body, "code")

            def go(_b):
                if not run.get_sensitive():
                    return
                run.set_sensitive(False)
                self._run_code_now(lang, body, status)
            run.connect("clicked", go)
            rw = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            rw.append(run); rw.append(status); card.append(rw)
            self._log_pin(card)
            self._tool_feedback.append(
                f"[that {lang} block is CRITICAL and is waiting for the user to confirm it. "
                "Do not assume it ran.]")
            return

        if tier == "danger":
            wl = Gtk.Label(label="\u26a0 destructive \u2014 running it now", xalign=0)
            wl.add_css_class("danger"); card.append(wl)
        card.append(status)
        self._log_pin(card)
        self._run_code_now(lang, body, status)

    def _run_code_now(self, lang, body, status):
        # Same reason as _run_now: a CRITICAL code card can be approved after its
        # turn has ended, and running it resumes the model — so re-arm the run
        # lifecycle (Stop + watchdog). No-op when a turn is already active.
        self._start_run()
        status.set_label("running\u2026")
        self._busy(True)
        self._arm_run_budget(body)

        def work():
            if lang in ("bash", "sh"):
                rc, out = self._execute_shell(
                    body, lambda pw: run_code(lang, body, sudo_password=pw))
            else:
                rc, out = run_code(lang, body)
            auth_failed = (rc == _SUDO_AUTH_FAILED)

            def show():
                status.remove_css_class("dim")
                status.add_css_class("ok" if rc == 0 else "danger")
                status.set_label("\u2713 exit 0" if rc == 0
                                 else ("\u2717 sudo auth failed" if auth_failed
                                       else f"\u2717 exit {rc}"))
                if out.strip():
                    o = Gtk.Label(label=out[:4000], xalign=0, wrap=True, selectable=True)
                    o.add_css_class("mono")
                    self._log_pin(o)
                return False
            GLib.idle_add(show)

            if auth_failed:
                return (f"COULD NOT RUN that {lang} \u2014 sudo authentication failed. {out}\n"
                        "Do NOT blindly retry. Root access is needed; tell the user.")
            if rc == 0:
                return (f"RAN that {lang} \u2014 SUCCEEDED (exit 0). Real output:\n{out[:6000]}\n"
                        "Check the output is actually what you needed before moving on.")
            return (f"RAN that {lang} \u2014 FAILED (exit {rc}). Real output:\n{out[:6000]}\n"
                    "Do NOT move on. Fix the cause and retry.")
        self._pending_tools += 1
        self._tool_thread(work, f"{lang} block")

    def _tool_thread(self, work, label):
        """Run an async tool worker so it can NEVER strand the tool loop.

        Every counted tool increments _pending_tools; if its worker thread dies
        on an exception the matching _tool_done is never called, the counter
        never reaches zero, and the whole turn hangs until the watchdog kills it
        ("no progress — looks stuck"). One helper, so the guarantee can't be
        forgotten the next time a tool is added.

        `work` returns the feedback string; anything it raises is reported and
        the slot is still released.
        """
        def runner():
            fb = None
            try:
                fb = work()
            except Exception as exc:
                # bind to a plain local: `except ... as exc` unbinds exc when the
                # block exits, which would break the closure below.
                why = str(exc)
                fb = f"[{label} failed: {why}]"

                def note(w=why):
                    self._sys_note(f"\u2717 {label} failed: {w}", "danger")
                    return False
                GLib.idle_add(note)
            finally:
                self._busy(False)

                def finish(text=fb):
                    self._tool_done(text if text is not None else f"[{label}: no result]")
                    return False
                GLib.idle_add(finish)
        threading.Thread(target=runner, daemon=True).start()

    def _do_images(self, query):
        self._sys_note(f"\U0001F5BC images for \u201c{query}\u201d")
        self._busy(True)
        self._activity_start("Images")

        def work():
            # Search AND download off the UI thread; downloads run in parallel.
            urls = image_search(query)
            got = []
            if urls:
                def grab_one(item):
                    i, u = item
                    if self._cancelled:
                        return None
                    si = self._activity_step(f"fetching image {i}/{len(urls)}")
                    p = download_image(u)
                    self._activity_done(si, f"image {i}/{len(urls)}", ok=bool(p))
                    return (p, u) if p else None
                ex = ThreadPoolExecutor(max_workers=4)
                try:
                    for r in ex.map(grab_one, list(enumerate(urls, 1))):
                        if r:
                            got.append(r)
                finally:
                    ex.shutdown(wait=False)

            def show():
                self._activity_end()
                if not self._cancelled:
                    if not got:
                        self._sys_note("No images came back (search blocked, offline, "
                                       "or nothing found).", "danger")
                    else:
                        for p, u in got:
                            self._image_bubble(p, u)
                return False
            GLib.idle_add(show)
            return f"[images '{query}': showed {len(got)} picture(s) to the user]"
        self._tool_thread(work, f"image search '{query}'")

    def _do_video_search(self, query):
        self._sys_note(f"\U0001F3AC videos for \u201c{query}\u201d")
        self._busy(True)

        def work():
            rows = video_search(query)

            def show():
                if not self._cancelled:
                    if not rows:
                        self._sys_note("No videos found.", "danger")
                    else:
                        # video_search returns (title, url, snippet) — the same
                        # 3-tuple shape as web_search. Unpacking two here meant
                        # every ```videos``` block died with a ValueError before
                        # a single card was drawn.
                        for (title, url, _snip) in rows:
                            self._video_card(title, url)
                return False
            GLib.idle_add(show)
            if not rows:
                return f"[videos '{query}': nothing found]"
            listing = "; ".join(f"{t} {u}" for t, u, _s in rows[:6])
            return f"[videos '{query}': showed {len(rows)} to the user \u2014 {listing}]"
        self._tool_thread(work, f"video search '{query}'")

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
        self._log_pin(card)

    def _do_video(self, url):
        if not url.startswith("http"):
            return
        if not shutil.which("yt-dlp"):
            self._sys_note("yt-dlp isn't installed:"); self._command_card("sudo pacman -Syu --needed yt-dlp"); return
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
        self._sys_note("\U0001F9F9 scanning for junk\u2026")
        self._busy(True)

        def work():
            report, cmds = junk_scan()

            def show():
                if not self._cancelled:
                    self._sys_note(report, "mono")
                    for label, cmd in cmds:
                        self._sys_note("\u2192 " + label)
                        self._command_card(cmd)
                return False
            GLib.idle_add(show)
            return f"[junk scan done \u2014 offered {len(cmds)} cleanup command(s)]"
        self._tool_thread(work, "junk scan")

    def _do_read(self, path):
        """Read a file from disk and feed its contents back into the run.
        An image path is SHOWN in the chat rather than refused as binary.
        A bare relative path resolves inside the open project first, so he can
        read back a file he just wrote without knowing its absolute path."""
        p = (path or "").strip()
        if _builder and p and not p.startswith(("/", "~")):
            proj = _builder.current_project()
            if proj is not None:
                ok, content = _builder.read_file(proj, p)
                if ok:
                    self._sys_note(f"\U0001F4C4 {proj.name}/{p}")
                    self._tool_done(f"FILE {proj.name}/{p}:\n{content}")
                    return
        if _is_image_path(p):
            full = os.path.expanduser(p)
            if os.path.isfile(full):
                self._sys_note(f"\U0001F5BC {os.path.basename(full)}")
                self._image_bubble(full)
                self._tool_done(f"[showed the image {p} to the user]")
                return
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
                    # Advisory findings no longer block the Run button, so they
                    # have to be VISIBLE — otherwise demoting them would just be
                    # hiding them. The user sees them next to the card.
                    adv = (res or {}).get("advisory") or []
                    for a in adv[:4]:
                        self._sys_note("\u26a0 " + a, "danger")
                    if run_after:
                        self._code_card(lang, body)
                    self._tool_done(f"[verify {lang}: clean]" +
                                    (" advisories: " + "; ".join(adv[:4]) if adv else ""))
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

    # ── building things ────────────────────────────────────────────────────
    def _project_or_note(self):
        if not _builder:
            self._sys_note("builder unavailable", "danger")
            return None
        p = _builder.current_project()
        if p is None:
            p, _ = _builder.project_open("scratch")
            self._sys_note(f"\U0001F4C1 working in project '{p.name}'", "dim")
        return p

    def _do_project(self, name):
        if not _builder:
            return
        p, created = _builder.project_open(name)
        self._sys_note(f"\U0001F4C1 project '{p.name}' "
                       f"({'created' if created else 'opened'}) \u2014 {p}", "ok")
        others = [n for n, _c in _builder.project_list() if n != p.name]
        extra = f" Other projects: {', '.join(others[:6])}." if others else ""
        self._tool_feedback.append(
            f"[project '{p.name}' ready at {p}. Write files with ```write <relpath>```.{extra}]")

    def _do_write(self, rel, content):
        """Write one complete file into the project — and verify it if it's code,
        so a broken file never sits silently in a deliverable."""
        p = self._project_or_note()
        if p is None:
            return
        ok, msg, path = _builder.write_file(p, rel, content)
        if not ok:
            self._sys_note("\u2717 " + msg, "danger")
            self._tool_feedback.append(f"[write failed: {msg}]")
            return
        note = msg
        lang = {".py": "python", ".sh": "bash", ".js": "js"}.get(Path(rel).suffix, None)
        if lang and _codecheck:
            try:
                res = _codecheck.check(lang, content)
                if not res.get("ok"):
                    self._sys_note(f"\u26a0 {rel}: verification found issues", "danger")
                    self._tool_feedback.append(
                        f"[wrote {rel} BUT it does not verify — fix it and write it again:\n"
                        + _codecheck.report(res) + "]")
                    return
                note += "  \u2713 verified"
            except Exception:
                pass
        self._sys_note("\u2713 " + note, "ok")
        self._tool_feedback.append(f"[{note}]")

    def _do_rmfile(self, rel):
        p = self._project_or_note()
        if p is None:
            return
        ok, msg = _builder.delete_file(p, rel)
        self._sys_note(("\u2713 " if ok else "\u2717 ") + msg, "ok" if ok else "danger")
        self._tool_feedback.append(f"[{msg}]")

    def _do_tree(self):
        p = self._project_or_note()
        if p is None:
            return
        t = _builder.tree(p)
        self._sys_note(f"\U0001F5C2 {p.name}\n{t}", "mono")
        self._tool_feedback.append(f"[project tree of {p.name}:\n{t}]")

    def _do_runtests(self):
        """Run the project's own tests and feed the REAL result back."""
        p = self._project_or_note()
        if p is None:
            self._tool_done("[no project]")
            return
        self._sys_note("\u25B6 running the project's tests\u2026", "dim"); self._busy(True)

        def worker():
            rc, out = _builder.run_tests(p)

            def show():
                self._busy(False)
                if rc == -1:
                    self._sys_note("no tests found in the project", "danger")
                    self._tool_done("[no tests found — write tests/test_*.py or run_tests.sh, "
                                    "then run them]")
                elif rc == 0:
                    self._sys_note("\u2713 tests passed", "ok")
                    self._tool_done(f"[project tests PASSED:\n{out[-2000:]}]")
                else:
                    self._sys_note(f"\u2717 tests failed (exit {rc})", "danger")
                    self._tool_done(f"[project tests FAILED (exit {rc}). Fix the code and run "
                                    f"them again:\n{out[-4000:]}]")
                return False
            GLib.idle_add(show)
        threading.Thread(target=worker, daemon=True).start()

    def _do_package(self):
        """Zip the project and put a real, openable deliverable in front of the user."""
        p = self._project_or_note()
        if p is None:
            return
        ok, zpath, msg = _builder.package(p, note=_builder.manifest(p))
        if not ok:
            self._sys_note("\u2717 " + msg, "danger")
            self._tool_feedback.append(f"[packaging failed: {msg}]")
            return
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("cmd-card")
        t = Gtk.Label(label=f"\U0001F4E6 {Path(zpath).name}", xalign=0, wrap=True, selectable=True)
        t.add_css_class("cmd-text"); card.append(t)
        sub = Gtk.Label(label=msg + f"\n{zpath}", xalign=0, wrap=True, selectable=True)
        sub.add_css_class("dim"); card.append(sub)
        rw = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        b1 = Gtk.Button(label="Open folder"); b1.add_css_class("gold")
        b1.connect("clicked", lambda *_: _open_path(str(Path(zpath).parent)))
        b2 = Gtk.Button(label="Open project"); b2.add_css_class("quick")
        b2.connect("clicked", lambda *_: _open_path(str(p)))
        rw.append(b1); rw.append(b2); card.append(rw)
        self._log_pin(card)
        self._tool_feedback.append(f"[{msg}. The user has it at {zpath}.]")

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
        self._seen_queries = []; self._seen_urls = set(); self._dead_urls = set()
        self._forced_answer = False
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
                if c.startswith(("TOOL RESULTS", "I ran ", "FILE ")):
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
        try:
            self._stream_into_bubble_inner(messages, vision)
        except Exception as ex:
            # A throw here used to escape into the GTK signal handler, leaving the
            # button stuck on Stop and the turn half-dead — which is why a failed
            # first message could take several tries to recover from.
            self._sys_note(f"\u26a0 couldn't start that turn: {ex}", "danger")
            self._busy(False)
            self._end_run()

    def _stream_into_bubble_inner(self, messages, vision=False):
        self._bot_text = ""
        self._bot_label = self._bot_bubble("Thinking\u2026")
        self._busy(True)
        self._awaiting_first = True     # nothing back from the model yet
        self._last_paint = 0.0
        self._note_progress()
        send_msgs = self._augment(messages) if not vision else messages

        def on_delta(chunk):
            if self._cancelled:
                return
            self._awaiting_first = False
            self._note_progress()
            self._bot_text += chunk
            # Repainting on EVERY token meant a full label re-layout per token
            # (~1000 for one reply) plus a regex pass over the whole accumulated
            # text each time — quadratic, and the visible lag on long answers.
            # Coalesce to a steady refresh instead: nobody can read faster than
            # this, and the final text is always flushed in _finalise.
            now = time.monotonic()
            if now - self._last_paint < _STREAM_REPAINT:
                return
            self._last_paint = now
            GLib.idle_add(self._paint_stream)



        def on_done():
            self._awaiting_first = False
            self._busy(False)
            if self._cancelled:
                return
            GLib.idle_add(self._finalise)

        def on_error(msg):
            self._awaiting_first = False
            self._busy(False)
            if self._cancelled:
                return
            GLib.idle_add(self._set_bot_text, "\u26a0 " + msg)
            GLib.idle_add(self._end_run)

        threading.Thread(
            target=self.backend.stream,
            args=(send_msgs, on_delta, on_done, on_error, vision),
            kwargs={"should_stop": lambda: self._cancelled,
                    "on_open": self._note_progress},
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

        def has(tag):
            """Presence check for bodiless tags (```tree```, ```package```).
            grab() drops empty bodies, so those tags need their own test."""
            return bool(re.search(r"```" + tag + r"(?![A-Za-z])", text))

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
        projects = grab("project")[:2]        # open / switch a build workspace
        trees = has("tree")                    # show what's been built
        rmfiles = grab("rmfile")[:4]           # remove a file from the project
        packages = has("package")              # zip the project and hand it over
        runtests = has("runtests")             # run the project's own tests
        writes = []                            # ```write <relpath>\n<content>```
        # The path may sit on the same line as the tag or on the line below it —
        # accept both, so the parser can never disagree with the prompt.
        for m in re.finditer(r"```write[ \t]*(?:\n[ \t]*)?([^\n`]+)\n(.*?)```", text, re.DOTALL):
            writes.append((m.group(1).strip(), m.group(2)))
        writes = writes[:12]
        checks = []
        for m in re.finditer(r"```check[ \t]+([a-z0-9+]+)[ \t]*\n(.*?)```", text,
                             re.DOTALL | re.IGNORECASE):
            checks.append((m.group(1).strip().lower(), m.group(2)))
        checks = checks[:CAP]
        # Runnable code/shell blocks, in the order they appear in the reply.
        # Collecting per-language and concatenating would order them by language
        # priority instead — so a shell step written FIRST could be dropped in
        # favour of a python step written later. Capture each block's position
        # and sort by it, so "the first runnable thing" is the first one written.
        code_hits = []
        for lang in ("python", "py", "node", "javascript", "js", "bash", "sh"):
            for m in re.finditer(r"```" + lang + r"(?![A-Za-z])[ \t]*\n?(.*?)```",
                                 text, re.DOTALL):
                cbody = m.group(1).strip()
                if cbody:
                    code_hits.append((m.start(), lang, cbody))
        code_hits.sort(key=lambda t: t[0])
        codes = [(lang, body) for _pos, lang, body in code_hits]   # (lang, body)
        # ONE runnable thing per reply. A wall of ten cards is unusable — the
        # user can't run ten commands at once, and each one's output changes
        # what the next should be. He proposes a step, sees the result, then
        # proposes the next.
        dropped_extra = max(0, len(codes) - 1)
        codes = codes[:1]

        disp = re.sub(
            r"```(?:search|fetch|images|videos|video|junk|skill|runskill|read|"
            r"remember|forget|check|project|tree|package|runtests|write|rmfile|"
            r"python|py|node|javascript|js|bash|sh)\b.*?```",
            "", text, flags=re.DOTALL).strip()
        if self._forced_answer:
            # he was told to answer from what he has — ignore any further research
            searches, fetches = [], []
        acting = bool(searches or fetches or images or vid_searches or videos or junk
                      or skill_blocks or runskills or reads or codes or checks
                      or projects or writes or trees or packages or runtests or rmfiles)
        # Chuck's own narration is what shows in chat — never swallow it.
        self._set_bot_text(disp or ("Working on it\u2026" if acting else "Done."), rich=True)

        # ── ONE owner for the tool counter ───────────────────────────────────
        # Reset FIRST, then count the whole turn, then dispatch. Two bugs lived
        # in the old ordering:
        #   * the reset sat BELOW the skill loops, so a ```runskill``` card that
        #     had already incremented the counter got it zeroed underneath it,
        #     along with its "waiting for the user to confirm" feedback;
        #   * counting one-at-a-time inside the dispatch loop meant any tool
        #     that finishes SYNCHRONOUSLY (_do_read on a project file or an
        #     image, _do_runtests with no project) dropped the counter to zero
        #     mid-loop and fired _continue_or_finish while later tools were
        #     still queued — two model round-trips, two "final" answers.
        # _dispatching additionally holds the turn open for the duration of the
        # dispatch block, so a synchronous _tool_done can never end it early.
        self._pending_tools = 0
        self._tool_feedback = []
        self._loop_web = (searches, fetches) if (searches or fetches) else None
        self._dispatching = True

        # skills: save any new smart files, then offer run cards. These go
        # through _command_card, which counts itself — hence: after the reset.
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
        self._pending_tools += (
            (1 if runtests else 0) + len(checks) + len(images)
            + len(vid_searches) + (1 if junk else 0) + len(reads)
            + (len(codes) if _codecheck else 0))

        for nm in projects:
            self._do_project(nm.strip())
        for rel, body in writes:
            self._do_write(rel, body)
        for rel in rmfiles:
            self._do_rmfile(rel.strip())
        if trees:
            self._do_tree()
        if runtests:
            self._do_runtests()
        if packages:
            self._do_package()
        for lang, body in checks:
            self._do_check(lang, body.strip(), run_after=False)
        if dropped_extra:
            self._sys_note(f"showing the first step only ({dropped_extra} more held back)", "dim")
            self._tool_feedback.append(
                f"[You proposed {dropped_extra + 1} commands at once; only the FIRST was shown. "
                "Give ONE command, wait for its real output, then give the next.]")
        for lang, body in codes:
            if _codecheck:
                self._do_check(lang, body.strip(), run_after=True)
            else:
                self._code_card(lang, body.strip())

        for q in images:
            self._do_images(q)
        for q in vid_searches:
            self._do_video_search(q)
        if junk:
            self._do_junk()
        for path in reads:
            self._do_read(path.strip())
        for u in videos:                     # fire-and-forget download; not gated
            self._do_video(u)

        # Dispatch is over: release the guard and decide the loop exactly once.
        self._dispatching = False
        if self._pending_tools == 0:
            self._continue_or_finish(disp)
        return False

    def _tool_done(self, feedback_text=None):
        """Called (on main thread) when one async show-tool finishes."""
        if self._cancelled:
            return
        self._note_progress()
        self._run_budget = 0            # no command is holding the watchdog open
        if feedback_text:
            self._tool_feedback.append(feedback_text)
        self._pending_tools = max(0, self._pending_tools - 1)
        # While _finalise is still handing out work, "zero outstanding" only
        # means "nothing outstanding YET" — the turn is not over.
        if self._pending_tools == 0 and not getattr(self, "_dispatching", False):
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
        if searches_fetches and not self._forced_answer:
            # Budget spent but he still wants to search. Dropping it silently is
            # what left him narrating "Working on it..." forever — instead, make
            # him deliver what he has.
            self._forced_answer = True
            self._hops = 0
            read = ", ".join(sorted(_domain(u) for u in self._seen_urls)[:10]) or "nothing"
            self.history.append({"role": "user", "content":
                                 "RESEARCH BUDGET SPENT — no more searching. You already read: "
                                 f"{read}. Write the FINAL answer now from what you have. State "
                                 "plainly what you could not confirm. Do NOT emit another search, "
                                 "fetch or read block."})
            self._sys_note("⏹ research budget spent — answering from what he has", "dim")
            GLib.idle_add(self._ask_model)
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
        if self.settings.get("tts", True) and disp:
            speak(disp, self.settings)
        self._save_chat()
        self._end_run()

    @staticmethod
    def _qnorm(q):
        return frozenset(w for w in re.findall(r"[a-z0-9][a-z0-9._/-]*", (q or "").lower())
                         if len(w) > 1)

    def _is_repeat_query(self, q):
        """Has he effectively asked this already this run? Re-wording the same
        search ('X github' → 'github X' → 'X github repository') burns the whole
        research budget without learning anything new."""
        toks = self._qnorm(q)
        if not toks:
            return True
        for prev in self._seen_queries:
            if not prev:
                continue
            overlap = len(toks & prev) / max(1, len(toks | prev))
            if overlap >= 0.7:
                return True
        return False

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
            skipped = []
            for q in searches[:n_q]:
                if self._cancelled:
                    return
                if self._is_repeat_query(q):
                    skipped.append(q)
                    continue
                self._seen_queries.append(self._qnorm(q))
                self._note_progress()
                si = self._activity_step(f"searching  {q}")
                results = web_search(q, n=n_src)
                if not results:
                    self._activity_done(si, f"searched  {q}  (no results)", ok=False)
                    out.append(f"[search '{q}': engines returned nothing — try different wording]")
                    continue
                n_new = 0
                for (title, url, snip) in results:
                    if url in seen_urls or url in self._seen_urls or url in self._dead_urls:
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
                if u in self._seen_urls or u in self._dead_urls:
                    skipped.append(u)
                    continue
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
                    self._seen_urls.add(url)
                    return f"[{title or dom}] {url}\n{body}"
                self._dead_urls.add(url)
                return None

            got = 0
            if not self._cancelled:
                # explicit executor, not a `with` block: on Stop we must abandon
                # in-flight fetches, and shutdown(wait=True) would sit here for
                # the full fetch timeout before the button responded.
                ex = ThreadPoolExecutor(max_workers=6)
                try:
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
                finally:
                    ex.shutdown(wait=False, cancel_futures=True)

            self._busy(False); self._activity_end()
            if self._cancelled:
                return
            hop_cap = self.cfg("research_hops", MAX_TOOL_HOPS, 1, 8)
            left = max(0, hop_cap - self._hops)
            lines = [f"TOOL RESULTS — {got} new source(s) this round."]
            if skipped:
                lines.append("ALREADY DONE, do not repeat: " + "; ".join(str(x)[:60]
                                                                        for x in skipped[:6]))
            if self._seen_urls:
                lines.append(f"Pages already read this turn ({len(self._seen_urls)}): "
                             + ", ".join(sorted(_domain(u) for u in self._seen_urls)[:8]))
            if self._dead_urls:
                lines.append("Unreachable, do not retry: "
                             + ", ".join(sorted(_domain(u) for u in self._dead_urls)[:6]))
            if left <= 1:
                lines.append("This is your LAST research round — write the final answer now "
                             "from what you have.")
            else:
                lines.append(f"{left} research rounds left. If what you have answers the "
                             "question, ANSWER NOW instead of searching again. Only search "
                             "again for something genuinely NOT covered above — never the "
                             "same query reworded.")
            lines.append("Cite URLs; mark single-source claims [UNVERIFIED]. If a thing "
                         "genuinely cannot be found, say so plainly and move on — that is a "
                         "complete answer, not a failure.")
            note = "\n".join(lines) + "\n\n"
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
        baseurl = field("API base URL",
                        Gtk.Entry(text=self.settings.get("siliconflow_base_url", "") or "",
                                  placeholder_text=DEFAULT_BASE),
                        "Leave blank for the default endpoint.")
        vmodel = field("Vision model",
                       Gtk.Entry(text=self.settings.get("vision_model", DEFAULT_VISION)))

        section("Voice")
        tts_on = Gtk.CheckButton(label="Read replies aloud")
        tts_on.set_active(bool(self.settings.get("tts", True)))
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
        pmodel = field("Piper voice model (optional)",
                       Gtk.Entry(text=self.settings.get("piper_model", "") or "",
                                 placeholder_text="/path/to/voice.onnx"),
                       "Blank uses the voice the installer set up.")
        vmax = field("Max characters read aloud per reply",
                     slider(2000, 40000, 1000,
                            self.cfg("voice_max_chars", 20000, 2000, 40000)))

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

        section("Appearance")
        fsz = field("Chat text size",
                    slider(9, 28, 1, self.cfg("font_size", FONT_SIZE, 9, 28)),
                    "Applies the moment you hit Save \u2014 no restart.")

        section("Performance")
        rkeep = field("Messages kept in memory",
                      slider(4, 100, 1, self.cfg("render_keep", RENDER_KEEP, 4, 200)),
                      "Older messages are unloaded and rebuilt only if you scroll back to them.")
        rpage = field("Messages loaded per scroll-up",
                      slider(5, 100, 5, self.cfg("render_page", RENDER_PAGE, 5, 200)))

        section("Network \u00b7 privacy")
        searx = field("Preferred SearXNG instance (optional)",
                      Gtk.Entry(text=self.settings.get("searx_url", ""),
                                placeholder_text="https://searx.example.org  (blank = built-in list)"),
                      "Search fans out over SearXNG (Brave + Google + DDG), DuckDuckGo as fallback.")
        proxy = field("Proxy for web, images and video (optional)",
                      Gtk.Entry(text=self.settings.get("proxy", ""),
                                placeholder_text="http://host:port"))

        section("Code verifier")
        if _codecheck:
            have = _codecheck.available_tools()
            on = [k for k, v in have.items() if v] or ["none"]
            off = [k for k, v in have.items() if not v]
            note = "Active: " + ", ".join(on)
            if off:
                note += "   \u00b7   not installed: " + ", ".join(off)
            lv = Gtk.Label(label=note, xalign=0, wrap=True); lv.add_css_class("set-hint")
            box.append(lv)
            box.append(Gtk.Label(
                label="Syntax and the security scan always run; linters add depth when present.",
                xalign=0, wrap=True))

        status = Gtk.Label(label="", xalign=0); status.add_css_class("ok")

        def collect():
            eng_key = ["auto", "piper", "espeak"][int(eng.get_selected() or 0)]
            return {
                "siliconflow_api_key": key.get_text().strip(),
                "model": model.get_text().strip() or DEFAULT_MODEL,
                "siliconflow_base_url": baseurl.get_text().strip(),
                "piper_model": pmodel.get_text().strip(),
                "voice_max_chars": int(vmax.get_value()),
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
                "font_size": int(fsz.get_value()),
                "render_keep": int(rkeep.get_value()),
                "render_page": int(rpage.get_value()),
                "searx_url": searx.get_text().strip(),
                "proxy": proxy.get_text().strip(),
            }

        def apply_and_save(*_):
            self.settings.update(collect())
            save_settings(self.settings)
            self.tts_btn.set_active(self.settings.get("tts", True))
            if not self.settings.get("tts"):
                stop_speaking()
            apply_font_size(self.settings.get("font_size", FONT_SIZE))
            self._refresh_sidebar()
            self._collapse_window()
            status.set_label("Saved.")

        def test_voice(*_):
            self.settings.update(collect())
            speak("Chuck Norris does not adjust settings. Settings adjust to Chuck Norris. "
                  "This line runs long on purpose, so you can hear it keep going all the way "
                  "to the end without cutting out halfway.", self.settings)
            status.set_label("Speaking\u2026")

        def reset(*_):
            for k in ("voice_engine", "voice_speed", "voice_pitch", "research_sources",
                      "research_queries", "research_hops", "fetch_timeout", "chat_ttl_hours",
                      "render_keep", "render_page", "font_size", "voice_max_chars",
                      "piper_model", "siliconflow_base_url"):
                self.settings.pop(k, None)
            save_settings(self.settings)
            apply_font_size(FONT_SIZE)
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
        apply_font_size(_SETTINGS.get("font_size", FONT_SIZE))
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
