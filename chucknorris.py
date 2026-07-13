#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ChuckNorris — a calm Arch / CachyOS assistant, fixer and researcher for Linux.

The healer of the set. ChuckNorris chats about your system, *looks* at your screen,
*researches the live web* across multiple sources with citations, scans for junk
and proposes cleanups, and makes driver/package installs trivial — all while
staying strictly NON-autonomous: every command is a card you approve, scans are
read-only, and it double-checks facts against real sources instead of guessing.

Backend: SiliconFlow (same as Basilisk; reuses its key if present).
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

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gio, Gdk, GdkPixbuf  # noqa: E402

APP_ID = "org.thepriest.chucknorris"
VERSION = "3.0.0"
HERE = Path(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = Path.home() / ".config" / "chucknorris"
DATA_DIR = Path.home() / ".local" / "share" / "chucknorris"
SETTINGS = CONFIG_DIR / "settings.json"
BASILISK_SETTINGS = Path.home() / ".config" / "basilisk" / "settings.json"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_BASE = "https://api.siliconflow.com/v1"
DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4-Flash"
DEFAULT_VISION = "Qwen/Qwen2.5-VL-32B-Instruct"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

DANGER = re.compile(
    r"(\brm\s+-[a-z]*r[a-z]*f?\b.*(/|\$HOME|~)|\bmkfs|\bdd\s+.*of=/dev/|"
    r">\s*/dev/sd|:\(\)\s*\{|\bshred\b|\bwipefs\b|"
    r"\bpacman\s+-R[a-z]*\s+.*\b(systemd|glibc|linux|bash|coreutils)\b|"
    r"\bchmod\s+-R\s+0?777\s+/|\bchown\s+-R\s+.*\s+/\b|"
    r"\b(reboot|poweroff|shutdown)\b)", re.IGNORECASE)

SYSTEM_PROMPT = """You are Chuck Norris — a world-class Arch Linux and CachyOS grandmaster \
and all-round smart assistant. You answer to the name "Chuck". You keep the user's machine \
healthy and make finding, installing, using and troubleshooting anything on Linux trivial.

WHAT YOU'RE ELITE AT:
- The whole Arch/CachyOS stack: pacman, the AUR via paru/yay, CachyOS repos + kernels \
(BORE/sched-ext, linux-cachyos variants), systemd/systemd-boot, GRUB, mkinitcpio, \
keyring/mirrorlist repair, partial-upgrade recovery, orphan cleanup, Wayland/X11, PipeWire, \
GPU drivers (mesa, vulkan, nvidia vs nvidia-open), btrfs/snapper, chroot rescue.
- TOOL KNOWLEDGE: for any task, name the right tool and where it lives — official repos vs \
AUR vs flatpak — and the exact command to get it. Use `pacman -Ss`/`paru -Ss` to search, \
`pkgfile <cmd>` or `pacman -F <cmd>` to find which package provides a missing command, \
`pacman -Qo` to find what owns a file. Then show how to actually USE the tool, and how to \
troubleshoot it when it misbehaves (logs, --verbose, strace, `systemctl status`, journalctl).
- General knowledge and research: when web sources are provided, ground answers in them and \
cite. For news, only report what multiple sources corroborate.

HARD RULES:
- You are NOT autonomous and you never harm the system. You PROPOSE commands; the user runs \
them by clicking a button. Never claim to have run anything; react only to REAL output you're \
given, never invent it.
- Put every shell command in its OWN ```bash block, one command per block. Prefer read-only \
diagnostics first, then the fix.
- Accuracy over confidence. If unsure, say so and propose a command that CHECKS rather than \
guessing. Never fabricate package names, flags, paths or facts.
- Protect system health: safest working fix first, warn clearly before anything destructive, \
never remove core packages (systemd, glibc, linux, bash) to solve a smaller problem.
- You are a system + knowledge helper, NOT a hacking tool. Decline offensive/attack requests.
- YOU DO NOT LOCATE, IDENTIFY, DE-ANONYMISE, TRACK OR GEOLOCATE REAL PEOPLE. No OSINT \
person-hunting, no doxxing, no finding someone's identity/accounts/location from a photo, \
handle, IP or name. If asked, decline plainly and offer only legitimate alternatives — e.g. \
helping the user check THEIR OWN online exposure, or general privacy/security literacy.

Be concise, direct, friendly, and a little unflappable. You're Chuck Norris: nothing on this \
machine scares you."""

RESEARCH_PROMPT = """You are ChuckNorris in RESEARCH mode. Answer the user's question using ONLY \
the numbered SOURCES provided. Cross-check the sources against each other. Cite the source \
number [n] after each factual claim. If sources disagree, say so and which is more \
authoritative (prefer official docs: ArchWiki, CachyOS wiki, man pages, upstream). If the \
sources do NOT answer the question, say exactly that and suggest what to search next — do \
NOT fill the gap from memory. End with a 'Sources:' list of the URLs used. If a fix is \
implied, offer it as ```bash``` command blocks the user can approve."""

NEWS_PROMPT = """You are Chuck Norris in NEWS-VERIFICATION mode. Using ONLY the numbered \
SOURCES, report the story. Rules: (1) State a fact as confirmed only if at least TWO \
independent sources agree — cite them [n]. (2) Anything carried by a single source, label \
[UNVERIFIED]. (3) Note the recency/date and flag if sources are stale. (4) Flag disagreement \
or spin between outlets and prefer primary/reputable sources. (5) Do NOT add anything not in \
the sources, and if the sources don't actually cover the topic, say so. End with a 'Sources:' \
list of URLs."""

CSS_TMPL = """
window { background-color: #0b0e10; }
.title  { font-weight: 800; color: #2dd4bf; font-size: 19px; }
.sub    { color: #6b7480; font-size: 11px; }
.chat-scroll, .chat-scroll viewport { background: transparent; }
.user-bubble { background-color: #14343a; border-radius: 12px; padding: 10px; }
.bot-bubble  { background-color: rgba(18,22,26,0.92); border-radius: 12px; padding: 10px; }
.cmd-card { background-color: rgba(10,18,22,0.95); border-radius: 10px; padding: 8px; }
.cmd-text { font-family: monospace; color: #b8f5ec; font-size: 12px; }
.teal { background-color: #14b8a6; color: #05201c; font-weight: 700; border-radius: 10px; }
.teal:hover { background-color: #2dd4bf; }
.quick { background-color: #12181c; color: #bfe9e2; border-radius: 9px; font-size: 12px; }
.quick:hover { background-color: #17323a; }
.danger { color: #ff6b6b; font-weight: 700; font-size: 11px; }
.ok     { color: #4ade80; font-size: 11px; }
.dim    { color: #6b7480; font-size: 11px; }
.mono   { font-family: monospace; font-size: 11px; color: #9fb0b8; }
.winbtn { background: transparent; border: none; padding: 0; min-width: 0; }
"""


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


# ── button plaque art (reused from Basilisk) ────────────────────────────────
def _btn_png(name):
    for p in (DATA_DIR / "assets" / f"basilisk-btn-{name}.png",
              HERE / "assets" / f"basilisk-btn-{name}.png"):
        if p.exists():
            return str(p)
    return None


def _plaque(name, px=30):
    p = _btn_png(name)
    if not p:
        return None
    try:
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(p, -1, px, True)
        pic = Gtk.Picture.new_for_pixbuf(pb)
        pic.set_can_shrink(False)
        return pic
    except Exception:
        return None


def plaque_button(name, fallback_icon=None, fallback_label=None, px=30, css="winbtn"):
    b = Gtk.Button()
    b.add_css_class(css)
    art = _plaque(name, px)
    if art is not None:
        b.set_child(art)
    elif fallback_icon:
        b.set_icon_name(fallback_icon)
    elif fallback_label:
        b.set_label(fallback_label)
    return b


# ── system helpers ──────────────────────────────────────────────────────────
def _run_ro(cmd, timeout=8):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception:
        return ""


def gather_context():
    facts = []
    facts.append("Distro: " + (_run_ro(["sh", "-c",
                 ". /etc/os-release 2>/dev/null; echo \"$PRETTY_NAME\""]) or "unknown"))
    facts.append("Kernel: " + _run_ro(["uname", "-r"]))
    facts.append(f"Session: {os.environ.get('XDG_SESSION_TYPE','?')} / "
                 f"{os.environ.get('XDG_CURRENT_DESKTOP','?')}")
    if shutil.which("pacman"):
        facts.append("Packages: " + _run_ro(["sh", "-c", "pacman -Qq | wc -l"]))
        facts.append("AUR helper: " + ("paru" if shutil.which("paru")
                     else ("yay" if shutil.which("yay") else "none")))
    gpu = _run_ro(["sh", "-c", "lspci | grep -iE 'vga|3d|display' | sed 's/.*: //'"])
    if gpu:
        facts.append("GPU: " + gpu.replace("\n", "; "))
    failed = _run_ro(["sh", "-c",
              "systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}' | tr '\\n' ' '"])
    if failed:
        facts.append("Failed units: " + failed)
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


def run_command(cmd, timeout=900):
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


# ── web research (multi-source, grounded) ───────────────────────────────────
def _strip(s):
    return _html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def web_search(query, n=6):
    """DuckDuckGo HTML endpoint — no API key. Returns [(title, url, snippet)]."""
    try:
        data = urllib.parse.urlencode({"q": query}).encode()
        req = urllib.request.Request("https://html.duckduckgo.com/html/",
                                     data=data, headers={"User-Agent": UA})
        html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    except Exception:
        return []
    titles = re.findall(r'class="result__a"[^>]*href="([^"]+)".*?>(.*?)</a>', html, re.DOTALL)
    snips = [_strip(s) for s in re.findall(
        r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)]
    out = []
    for i, (href, title) in enumerate(titles[:n]):
        q = urllib.parse.urlparse(href.replace("&amp;", "&"))
        real = urllib.parse.parse_qs(q.query).get("uddg", [href])[0]
        if real.startswith("//"):
            real = "https:" + real
        out.append((_strip(title), real, snips[i] if i < len(snips) else ""))
    return out


def web_fetch(url, limit=3500):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    except Exception:
        return ""
    raw = re.sub(r"(?is)<(script|style|nav|footer|header|form).*?</\1>", " ", raw)
    text = re.sub(r"\s+", " ", _strip(raw))
    return text[:limit]


def junk_scan():
    """Read-only disk-junk scan. Returns (report_text, [(label, command), ...])."""
    lines, cmds = [], []

    def sz(path):
        return _run_ro(["sh", "-c", f"du -sh {path} 2>/dev/null | cut -f1"]) or "0"

    lines.append(f"~/.cache: {sz('~/.cache')}")
    cmds.append(("Clear old thumbnail cache", "rm -rf ~/.cache/thumbnails/*"))

    if shutil.which("pacman"):
        lines.append(f"pacman package cache: {sz('/var/cache/pacman/pkg')}")
        if shutil.which("paccache"):
            cmds.append(("Trim pacman cache (keep last 1 of each)", "sudo paccache -rk1"))
        else:
            cmds.append(("Install pacman-contrib, then trim cache",
                         "sudo pacman -S --needed pacman-contrib && sudo paccache -rk1"))
        orphans = _run_ro(["sh", "-c", "pacman -Qtdq 2>/dev/null | wc -l"])
        lines.append(f"orphan packages: {orphans}")
        if orphans and orphans != "0":
            cmds.append(("Remove orphan packages", "sudo pacman -Rns $(pacman -Qtdq)"))

    if shutil.which("paru"):
        lines.append(f"paru build cache: {sz('~/.cache/paru')}")
        cmds.append(("Clean paru clone/build cache", "rm -rf ~/.cache/paru/clone/*"))
    if shutil.which("yay"):
        lines.append(f"yay build cache: {sz('~/.cache/yay')}")

    jsize = _run_ro(["sh", "-c",
             "journalctl --disk-usage 2>/dev/null | grep -oE '[0-9.]+[KMG]' | tail -1"])
    if jsize:
        lines.append(f"systemd journal: {jsize}")
        cmds.append(("Shrink journal to 200M", "sudo journalctl --vacuum-size=200M"))

    lines.append(f"Trash: {sz('~/.local/share/Trash')}")
    cmds.append(("Empty trash",
                 "rm -rf ~/.local/share/Trash/files/* ~/.local/share/Trash/info/*"))

    if shutil.which("flatpak"):
        cmds.append(("Remove unused flatpak runtimes", "flatpak uninstall --unused -y"))

    lines.append(f"coredumps: {sz('/var/lib/systemd/coredump')}")
    cmds.append(("Clear old coredumps", "sudo rm -rf /var/lib/systemd/coredump/*"))

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
                           "stream": True, "temperature": 0.2}).encode()
        req = urllib.request.Request(
            self.base() + "/chat/completions", data=body,
            headers={"Authorization": "Bearer " + self.key(),
                     "Content-Type": "application/json"})
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
class ChuckNorrisWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("Chuck Norris")
        self.set_default_size(960, 800)
        self.settings = load_settings()
        self.backend = Backend(self.settings)
        self.history = [{"role": "system",
                         "content": SYSTEM_PROMPT + "\n\nCurrent machine:\n" + gather_context()}]
        self.pending_shot = None
        self.pending_file = None
        self._news_mode = False
        self._bot_label = None
        self._bot_text = ""

        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tl = Gtk.Label(label="\U0001F94B Chuck Norris", xalign=0); tl.add_css_class("title")
        sl = Gtk.Label(label="Arch / CachyOS \u00b7 fix \u00b7 install \u00b7 research \u00b7 you approve every step",
                       xalign=0); sl.add_css_class("sub")
        tb.append(tl); tb.append(sl); header.set_title_widget(tb)

        b_close = plaque_button("close", "window-close-symbolic", px=26)
        b_close.connect("clicked", lambda *_: self.close())
        b_min = plaque_button("minimise", "window-minimize-symbolic", px=26)
        b_min.connect("clicked", lambda *_: self.minimize())
        b_exp = plaque_button("expand", "window-maximize-symbolic", px=26)
        b_exp.connect("clicked", self._toggle_max)
        cog = plaque_button("settings", "emblem-system-symbolic", px=26)
        cog.connect("clicked", self.open_settings)
        for b in (b_close, b_exp, b_min, cog):
            header.pack_end(b)

        qbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for m in ("start", "end", "top"):
            getattr(qbar, f"set_margin_{m}")(10)
        for label, handler in (
                ("\U0001F4F0 News", lambda *_: self._news_prompt()),
                ("\u21bb Update", lambda *_: self._quick(
                    "Update my whole system safely (repo + AUR if present). Show the exact commands.")),
                ("\U0001F5A5 GPU drivers", self._drivers),
                ("\U0001F511 Fix keyring", lambda *_: self._quick(
                    "My pacman keyring is broken (PGP signature errors). Give the correct recovery "
                    "steps for Arch/CachyOS as commands.")),
                ("\U0001F9F9 Clean junk", lambda *_: self._junk()),
                ("\U0001F9F1 Orphans", lambda *_: self._quick(
                    "List orphan packages and show the command to remove them safely.")),
                ("\u2699 Failed services", lambda *_: self._quick(
                    "Show failed systemd units and help me diagnose them.")),
        ):
            qb = Gtk.Button(label=label); qb.add_css_class("quick")
            qb.connect("clicked", handler); qbar.append(qb)
        qscroll = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER)
        qscroll.set_child(qbar)

        self.msgbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for m in ("top", "bottom", "start", "end"):
            getattr(self.msgbox, f"set_margin_{m}")(14)
        self.scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self.scroller.add_css_class("chat-scroll")
        self.scroller.set_child(self.msgbox)

        overlay = Gtk.Overlay()
        bgp = (DATA_DIR / "assets" / "chucknorris-bg.png")
        bgp = bgp if bgp.exists() else (HERE / "assets" / "chucknorris-bg.png")
        if bgp.exists():
            bg = Gtk.Picture.new_for_filename(str(bgp))
            bg.set_content_fit(Gtk.ContentFit.CONTAIN)
            bg.set_opacity(0.45)
            bg.set_can_target(False)
            overlay.set_child(bg)
        else:
            overlay.set_child(Gtk.Box())
        overlay.add_overlay(self.scroller)

        self.entry = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.entry.add_css_class("mono")
        ev = Gtk.ScrolledWindow(min_content_height=48, max_content_height=120, hexpand=True)
        ev.set_child(self.entry)
        self.web_toggle = Gtk.ToggleButton(label="\U0001F50E Web")
        self.web_toggle.add_css_class("quick")
        self.web_toggle.set_tooltip_text("Research the live web (multi-source, cited) for this question")
        cam = plaque_button("camera", "camera-photo-symbolic", px=34, css="quick")
        cam.set_tooltip_text("Show Chuck your screen")
        cam.connect("clicked", self.on_screenshot)
        att = plaque_button("attach", "mail-attachment-symbolic", px=34, css="quick")
        att.set_tooltip_text("Attach a file for Chuck to read")
        att.connect("clicked", self.on_attach)
        send = Gtk.Button(label="Send"); send.add_css_class("teal")
        send.connect("clicked", self.on_send)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(row, f"set_margin_{m}")(12)
        row.append(ev); row.append(self.web_toggle); row.append(att); row.append(cam); row.append(send)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(qscroll)
        body.append(overlay)
        body.append(Gtk.Separator())
        body.append(row)
        tv = Adw.ToolbarView(); tv.add_top_bar(header); tv.set_content(body)
        self.set_content(tv)

        self._bot_bubble("Hey \u2014 Chuck here. Arch/CachyOS grandmaster, at your service. Ask me "
                         "to fix, install, clean up, or troubleshoot anything. Toggle \U0001F50E Web "
                         "for researched, cited answers, hit \U0001F4F0 News for cross-checked "
                         "headlines, the camera to show me an error, or the paperclip to hand me a "
                         "file. Everything I suggest is a command you approve \u2014 I never run off "
                         "on my own.")
        if not self.backend.key():
            self._sys_note("No SiliconFlow key yet — open Settings. If Basilisk is installed, "
                           "I'll reuse its key automatically.")

    def _toggle_max(self, *_):
        (self.unmaximize if self.is_maximized() else self.maximize)()

    # ── bubbles ──
    def _scroll_down(self):
        def go():
            a = self.scroller.get_vadjustment(); a.set_value(a.get_upper())
        GLib.idle_add(go)

    def _user_bubble(self, text, shot=False):
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.END, hexpand=True)
        lbl = Gtk.Label(label=text + ("  \U0001F4F7" if shot else ""), xalign=1,
                        wrap=True, selectable=True)
        lbl.set_max_width_chars(72)
        card = Gtk.Box(); card.add_css_class("user-bubble"); card.append(lbl)
        w.append(card); self.msgbox.append(w); self._scroll_down()

    def _bot_bubble(self, text=""):
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.START, hexpand=True)
        lbl = Gtk.Label(label=text, xalign=0, wrap=True, selectable=True)
        lbl.set_max_width_chars(84)
        card = Gtk.Box(); card.add_css_class("bot-bubble"); card.append(lbl)
        w.append(card); self.msgbox.append(w); self._scroll_down()
        return lbl

    def _sys_note(self, text, css="dim"):
        l = Gtk.Label(label=text, xalign=0, wrap=True); l.add_css_class(css)
        self.msgbox.append(l); self._scroll_down()

    # ── command cards ──
    def _command_card(self, cmd):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("cmd-card")
        ct = Gtk.Label(label="$ " + cmd, xalign=0, wrap=True, selectable=True)
        ct.add_css_class("cmd-text"); card.append(ct)
        if DANGER.search(cmd):
            w = Gtk.Label(label="\u26a0 destructive / irreversible \u2014 read it before you run it",
                          xalign=0); w.add_css_class("danger"); card.append(w)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        run = Gtk.Button(label="Run"); run.add_css_class("teal")
        status = Gtk.Label(label="", xalign=0); status.add_css_class("dim")
        run.connect("clicked", lambda _b: self._run_card(cmd, run, status))
        row.append(run); row.append(status); card.append(row)
        self.msgbox.append(card); self._scroll_down()

    def _run_card(self, cmd, run_btn, status):
        run_btn.set_sensitive(False); status.set_label("running\u2026")

        def worker():
            rc, out = run_command(cmd)

            def show():
                status.remove_css_class("dim")
                status.add_css_class("ok" if rc == 0 else "danger")
                status.set_label(f"exit {rc}")
                o = Gtk.Label(label=out[:4000], xalign=0, wrap=True, selectable=True)
                o.add_css_class("mono"); self.msgbox.append(o); self._scroll_down()
                self.history.append({"role": "user",
                                     "content": f"I ran `{cmd}`. Exit {rc}. Output:\n{out[:6000]}"})
                self._ask_model()
            GLib.idle_add(show)
        threading.Thread(target=worker, daemon=True).start()

    # ── quick actions ──
    def _quick(self, prompt):
        self._user_bubble(prompt)
        self.history.append({"role": "user", "content": prompt})
        self._ask_model()

    def _drivers(self, *_):
        gpu = _run_ro(["sh", "-c", "lspci | grep -iE 'vga|3d|display'"])
        self._quick("Recommend and install the right GPU drivers for this hardware on "
                    f"Arch/CachyOS. lspci says:\n{gpu}\nGive exact commands, and mention the "
                    "open vs proprietary choice if it's NVIDIA.")

    def _junk(self, *_):
        self._sys_note("\U0001F9F9 scanning for junk (read-only)\u2026")

        def worker():
            report, cmds = junk_scan()

            def show():
                self._bot_bubble("Here's what's taking up space. Nothing's been touched — "
                                 "approve only what you want cleaned:\n\n" + report)
                for label, cmd in cmds:
                    self._sys_note("\u2192 " + label, css="dim")
                    self._command_card(cmd)
            GLib.idle_add(show)
        threading.Thread(target=worker, daemon=True).start()

    # ── screenshot ──
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

    def _news_prompt(self, *_):
        self._news_mode = True
        self.web_toggle.set_active(False)
        self._sys_note("\U0001F4F0 News mode: type a topic and Send \u2014 I'll cross-check "
                       "multiple sources and only report what's corroborated, with citations.")

    def on_attach(self, *_):
        try:
            dialog = Gtk.FileDialog()
            dialog.open(self, None, self._on_file_chosen)
        except Exception as ex:
            self._sys_note(f"couldn't open file picker: {ex}")

    def _on_file_chosen(self, dialog, res):
        try:
            gfile = dialog.open_finish(res)
            path = gfile.get_path()
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
                self._sys_note("that looks binary \u2014 I read text files. Tell me what to do with it.")
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
        shot = self.pending_shot
        # fold an attached file's contents into the message
        if self.pending_file:
            fname, fbody = self.pending_file
            text = (text or "Look at this file.") + \
                   f"\n\n--- attached file: {fname} ---\n{fbody}\n--- end of {fname} ---"
            self.pending_file = None
        if shot:
            self._user_bubble(text or "(look at this)", shot=True)
            content = [{"type": "text", "text": text or "What's wrong on my screen?"},
                       {"type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + shot}}]
            self.history.append({"role": "user", "content": content})
            self.pending_shot = None
            self._ask_model(vision=True)
        elif self._news_mode:
            self._news_mode = False
            self._user_bubble("\U0001F4F0 " + text)
            self._research(text, NEWS_PROMPT, tag="news")
        elif self.web_toggle.get_active():
            self._user_bubble("\U0001F50E " + text)
            self._research(text)
        else:
            self._user_bubble(text)
            self.history.append({"role": "user", "content": text})
            self._ask_model()

    def _research(self, query, prompt=None, tag="researched"):
        prompt = prompt or RESEARCH_PROMPT
        self._sys_note("\U0001F50E searching the web (multiple sources)\u2026")

        def worker():
            results = web_search(query)
            if not results:
                GLib.idle_add(self._sys_note,
                              "Web search failed (no results / offline). Answering from knowledge "
                              "instead — verify anything important.")
                self.history.append({"role": "user", "content": query})
                GLib.idle_add(self._ask_model)
                return
            blocks = []
            for i, (title, url, snip) in enumerate(results[:5], 1):
                body = web_fetch(url) or snip
                blocks.append(f"[{i}] {title}\nURL: {url}\n{body}")
            sources = "\n\n".join(blocks)
            msgs = [{"role": "system", "content": prompt},
                    {"role": "user", "content": f"Question: {query}\n\nSOURCES:\n{sources}"}]
            self.history.append({"role": "user", "content": f"[{tag}] {query}"})
            GLib.idle_add(lambda: self._stream_into_bubble(msgs))
        threading.Thread(target=worker, daemon=True).start()

    def _ask_model(self, vision=False):
        self._stream_into_bubble(self.history, vision=vision)

    def _stream_into_bubble(self, messages, vision=False):
        self._bot_text = ""
        self._bot_label = self._bot_bubble("\u2026")

        def on_delta(chunk):
            self._bot_text += chunk
            GLib.idle_add(self._bot_label.set_label, self._bot_text)
            self._scroll_down()

        def on_done():
            GLib.idle_add(self._finalise)

        def on_error(msg):
            GLib.idle_add(self._bot_label.set_label, "\u26a0 " + msg)

        threading.Thread(target=self.backend.stream,
                         args=(messages, on_delta, on_done, on_error, vision),
                         daemon=True).start()

    def _finalise(self):
        text = self._bot_text
        self.history.append({"role": "assistant", "content": text})
        blocks = re.findall(r"```(?:bash|sh)?\s*\n?(.*?)```", text, re.DOTALL)
        clean = re.sub(r"```(?:bash|sh)?\s*\n?.*?```", "", text, flags=re.DOTALL).strip()
        self._bot_label.set_label(clean or "Here's what I'd do:")
        for blk in blocks:
            for line in [ln.strip() for ln in blk.splitlines()
                         if ln.strip() and not ln.strip().startswith("#")]:
                self._command_card(line)
        self._scroll_down()
        return False

    # ── settings ──
    def open_settings(self, *_):
        dlg = Adw.Window(transient_for=self, modal=True, title="Settings",
                         default_width=520, default_height=340)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(14)
        hb = Adw.HeaderBar(); wrap = Adw.ToolbarView()
        wrap.add_top_bar(hb); wrap.set_content(box); dlg.set_content(wrap)
        box.append(Gtk.Label(label="SiliconFlow API key", xalign=0))
        key = Gtk.Entry(text=self.settings.get("siliconflow_api_key", ""),
                        visibility=False, placeholder_text="sk-\u2026")
        box.append(key)
        box.append(Gtk.Label(label="Chat model", xalign=0))
        model = Gtk.Entry(text=self.settings.get("model", DEFAULT_MODEL)); box.append(model)
        box.append(Gtk.Label(label="Vision model", xalign=0))
        vmodel = Gtk.Entry(text=self.settings.get("vision_model", DEFAULT_VISION)); box.append(vmodel)
        hint = Gtk.Label(xalign=0, wrap=True); hint.add_css_class("dim")
        hint.set_label("Key: cloud.siliconflow.com/account/ak. Basilisk's key is reused if present.")
        box.append(hint)

        def save(*_):
            self.settings["siliconflow_api_key"] = key.get_text().strip()
            self.settings["model"] = model.get_text().strip() or DEFAULT_MODEL
            self.settings["vision_model"] = vmodel.get_text().strip() or DEFAULT_VISION
            save_settings(self.settings); dlg.close()
        sv = Gtk.Button(label="Save"); sv.add_css_class("teal"); sv.connect("clicked", save)
        box.append(sv); dlg.present()


class ChuckNorrisApp(Adw.Application):
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

    def do_activate(self):
        (self.props.active_window or ChuckNorrisWindow(self)).present()


def main():
    Adw.init()
    return ChuckNorrisApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
