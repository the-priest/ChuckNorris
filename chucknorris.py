#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chuck Norris — an Arch / CachyOS grandmaster assistant (a tribute).

Carlos Ray "Chuck" Norris (1940–2026). This app carries his legend: a deadpan,
unflappable CachyOS expert that fixes anything, researches and verifies the web,
reads files, shows pictures, downloads video, speaks in a gruff voice, and drops
Chuck Norris facts along the way. Non-autonomous: every command is a card you
approve. Backend: SiliconFlow (reuses Basilisk's key if present).
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

APP_ID = "org.thepriest.chucknorris"
VERSION = "4.0.0"
HERE = Path(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = Path.home() / ".config" / "chucknorris"
DATA_DIR = Path.home() / ".local" / "share" / "chucknorris"
CHATS_DIR = DATA_DIR / "chats"
DL_DIR = Path.home() / "Downloads" / "ChuckNorris"
SETTINGS = CONFIG_DIR / "settings.json"
BASILISK_SETTINGS = Path.home() / ".config" / "basilisk" / "settings.json"
for d in (CONFIG_DIR, CHATS_DIR, DL_DIR):
    d.mkdir(parents=True, exist_ok=True)

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

SYSTEM_PROMPT = """You ARE Chuck Norris — the legend himself, now an Arch Linux and CachyOS \
grandmaster living inside this machine. You keep it healthy and make fixing, installing and \
troubleshooting anything trivial. You speak in a calm, deadpan, unflappable tough-guy voice: \
short, confident, dry. Nothing on this box scares you.

PERSONALITY: You love "Chuck Norris facts" and drop one now and then, especially themed to \
Linux/CachyOS when it fits ("Chuck Norris doesn't kill zombie processes. He stares at them \
until they apologise and exit 0."). Invent fresh ones. Keep them short and land them at the \
end of a reply, not in the middle of a fix. Be warm under the gruffness — you're here to help.

WHAT YOU'RE ELITE AT: pacman, the AUR via paru/yay, CachyOS repos + kernels (BORE/sched-ext, \
linux-cachyos), systemd/systemd-boot, GRUB, mkinitcpio, keyring/mirrorlist repair, \
partial-upgrade recovery, orphan cleanup, GPU drivers (mesa/vulkan/nvidia/nvidia-open), \
btrfs/snapper, chroot rescue, Wayland/X11, PipeWire. TOOL FINDER: name the right tool, where \
it lives (repo/AUR/flatpak) and the command to get it; use `pkgfile`/`pacman -F` to find what \
provides a missing command, `pacman -Qo` for what owns a file; then show how to USE it and \
troubleshoot it.

HARD RULES:
- NOT autonomous, never harms the system. PROPOSE commands; the user runs them by clicking a \
button. Never claim to have run anything; react only to REAL output you're given.
- Every shell command in its OWN ```bash block, one per block. Read-only diagnostics first, \
then the fix. Warn before anything destructive; never remove core packages to fix a small thing.
- Accuracy over confidence. If unsure, say so and propose a command that CHECKS. Never fabricate \
package names, flags, paths or facts. When web sources are given, ground answers in them + cite.
- System + knowledge helper only — NOT a hacking tool (decline offensive requests), and you do \
NOT locate, de-anonymise, track or geolocate real people (no OSINT person-hunting/doxxing) — \
decline that and offer only legit alternatives like checking the user's OWN exposure.

Format replies cleanly with short paragraphs and clear headers. Be concise."""

RESEARCH_PROMPT = """You are Chuck in RESEARCH mode. Answer using ONLY the numbered SOURCES. \
Cross-check them, cite [n] after each claim, prefer official docs (ArchWiki, CachyOS wiki, man \
pages). If the sources don't answer it, say so — don't fill gaps from memory. End with a \
'Sources:' list of URLs. Offer any fix as ```bash``` command blocks."""

NEWS_PROMPT = """You are Chuck in NEWS-VERIFICATION mode. Using ONLY the numbered SOURCES: state \
a fact as confirmed only if 2+ independent sources agree (cite [n]); label single-source claims \
[UNVERIFIED]; note recency and flag stale/conflicting coverage; add nothing not in the sources. \
End with a 'Sources:' list of URLs."""

CSS_TMPL = """
window { background-color: #0b0b0d; }
.title  { font-weight: 800; color: #e6b25a; font-size: 20px; }
.sub    { color: #7a7268; font-size: 11px; }
.chat-scroll, .chat-scroll viewport { background: transparent; }
.user-bubble { background-color: rgba(60,42,20,0.92); border-radius: 12px; padding: 10px; }
.bot-bubble  { background-color: rgba(18,17,15,0.94); border-radius: 12px; padding: 11px; }
.cmd-card { background-color: rgba(12,11,9,0.97); border-radius: 10px; padding: 8px; }
.cmd-text { font-family: monospace; color: #f0d9a8; font-size: 12px; }
.gold { background-color: #b6892f; color: #14110a; font-weight: 800; border-radius: 10px; }
.gold:hover { background-color: #d4a23c; }
.quick { background-color: #1a1712; color: #e6cfa0; border-radius: 9px; font-size: 12px; }
.quick:hover { background-color: #2a2318; }
.danger { color: #ff7a5c; font-weight: 700; font-size: 11px; }
.ok     { color: #6ddf87; font-size: 11px; }
.dim    { color: #7a7268; font-size: 11px; }
.mono   { font-family: monospace; font-size: 11px; color: #b3a68a; }
.sendbtn { background: transparent; border: none; padding: 0; min-width: 0; }
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
    """urllib opener that honours an optional HTTP(S) proxy (e.g. Mullvad)."""
    proxy = (_SETTINGS.get("proxy") or "").strip()
    if proxy:
        h = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(h)
    return urllib.request.build_opener()


def _get(url, data=None, timeout=20, headers=None):
    req = urllib.request.Request(url, data=data,
                                 headers=headers or {"User-Agent": UA})
    return _opener().open(req, timeout=timeout)


def open_in_brave(url):
    for b in ("brave", "brave-browser"):
        if shutil.which(b):
            subprocess.Popen([b, url]); return True
    try:
        Gio.AppInfo.launch_default_for_uri(url, None); return True
    except Exception:
        return False


# ── markdown -> Pango markup (nice titles, no raw asterisks) ─────────────────
def md_to_pango(text):
    s = _html.escape(text, quote=False)
    s = re.sub(r"`([^`]+)`", r"<tt>\1</tt>", s)                         # inline code
    s = re.sub(r"(?m)^\s{0,3}#{1,6}\s+(.*)$", r"<big><b>\1</b></big>", s)  # headers
    s = re.sub(r"(?m)^\s*[-*]\s+", "  \u2022 ", s)                      # bullets
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)                     # bold
    s = re.sub(r"__([^_]+)__", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", s)    # italic
    return s


def _pic_from_file(path, w=-1, h=-1):
    """Load an image into a Gtk.Picture via Gdk.Texture (not the deprecated pixbuf path)."""
    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, w, h, True)
    tex = Gdk.Texture.new_for_pixbuf(pb)
    pic = Gtk.Picture.new_for_paintable(tex)
    pic.set_can_shrink(False)
    return pic, pb


def set_rich(label, text):
    try:
        label.set_markup(md_to_pango(text))
    except Exception:
        label.set_text(text)


# ── system helpers ──────────────────────────────────────────────────────────
def _run_ro(cmd, timeout=8):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception:
        return ""


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


# ── speech (gruff character voice — espeak-ng; NOT a clone of the real man) ──
def speak(text):
    if not shutil.which("espeak-ng"):
        return
    clean = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    clean = re.sub(r"[*_`#>\[\]]", "", clean)[:600]
    try:
        subprocess.Popen(["espeak-ng", "-v", "en-us+m3", "-p", "22", "-s", "150", clean],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ── web search / fetch (proxy-aware) ────────────────────────────────────────
def _strip(s):
    return _html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def web_search(query, n=6):
    try:
        data = urllib.parse.urlencode({"q": query}).encode()
        html = _get("https://html.duckduckgo.com/html/", data=data).read().decode("utf-8", "ignore")
    except Exception:
        return []
    titles = re.findall(r'class="result__a"[^>]*href="([^"]+)".*?>(.*?)</a>', html, re.DOTALL)
    snips = [_strip(s) for s in re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)]
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
        raw = _get(url).read().decode("utf-8", "ignore")
    except Exception:
        return ""
    raw = re.sub(r"(?is)<(script|style|nav|footer|header|form).*?</\1>", " ", raw)
    return re.sub(r"\s+", " ", _strip(raw))[:limit]


def image_search(query, n=6):
    """DuckDuckGo image search (no key). Returns image URLs. Unfiltered (legal content)."""
    try:
        page = _get("https://duckduckgo.com/?q=" + urllib.parse.quote(query) +
                    "&iax=images&ia=images").read().decode("utf-8", "ignore")
        m = re.search(r'vqd=["\']?([\d-]+)["\']?', page)
        if not m:
            return []
        vqd = m.group(1)
        api = ("https://duckduckgo.com/i.js?l=us-en&o=json&q=" + urllib.parse.quote(query) +
               "&vqd=" + vqd + "&f=,,,&p=1")
        js = _get(api, headers={"User-Agent": UA, "Referer": "https://duckduckgo.com/"}
                  ).read().decode("utf-8", "ignore")
        data = json.loads(js)
        return [r["image"] for r in data.get("results", [])[:n] if r.get("image")]
    except Exception:
        return []


def download_image(url, timeout=25):
    try:
        raw = _get(url, timeout=timeout).read()
        tmp = CONFIG_DIR / (".img_" + str(abs(hash(url)) % 10**8) + ".bin")
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
class ChuckWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("Chuck Norris")
        self.set_default_size(980, 820)
        self.settings = _SETTINGS
        self.backend = Backend(self.settings)
        self.pending_shot = None
        self.pending_file = None
        self._news_mode = False
        self._bot_label = None
        self._bot_text = ""
        self.chat_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._new_history()

        header = Adw.HeaderBar()
        tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tl = Gtk.Label(label="\U0001F94B Chuck Norris", xalign=0); tl.add_css_class("title")
        sl = Gtk.Label(label="Arch / CachyOS grandmaster \u00b7 1940\u20132026 \u00b7 you approve every step",
                       xalign=0); sl.add_css_class("sub")
        tb.append(tl); tb.append(sl); header.set_title_widget(tb)
        self.tts_btn = Gtk.ToggleButton(icon_name="audio-volume-high-symbolic")
        self.tts_btn.set_tooltip_text("Read replies aloud (gruff voice)")
        self.tts_btn.set_active(self.settings.get("tts", False))
        header.pack_end(self.tts_btn)
        for icon, tip, cb in (
                ("emblem-system-symbolic", "Settings", self.open_settings),
                ("document-open-recent-symbolic", "Saved chats", self.open_chats),
                ("document-new-symbolic", "New chat", lambda *_: self.new_chat())):
            b = Gtk.Button(icon_name=icon); b.set_tooltip_text(tip)
            b.connect("clicked", cb); header.pack_end(b)

        # quick actions
        qbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for m in ("start", "end", "top"):
            getattr(qbar, f"set_margin_{m}")(10)
        actions = [
            ("\U0001F4F0 News", lambda *_: self._news_prompt()),
            ("\U0001F5BC Images", lambda *_: self._image_prompt()),
            ("\u2B07 Video", lambda *_: self._video_prompt()),
            ("\u21bb Update", lambda *_: self._quick(
                "Update my whole system safely (repo + AUR if present). Show the exact commands.")),
            ("\U0001F5A5 GPU drivers", self._drivers),
            ("\U0001F511 Fix keyring", lambda *_: self._quick(
                "My pacman keyring is broken (PGP signature errors). Recovery steps as commands.")),
            ("\U0001F9F9 Clean junk", lambda *_: self._junk()),
            ("\u2699 Failed services", lambda *_: self._quick(
                "Show failed systemd units and help me diagnose them.")),
        ]
        for label, handler in actions:
            qb = Gtk.Button(label=label); qb.add_css_class("quick")
            qb.connect("clicked", handler); qbar.append(qb)
        qscroll = Gtk.ScrolledWindow(); qscroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        qscroll.set_child(qbar)

        # chat area with Chuck+Tux wallpaper behind
        self.msgbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for m in ("top", "bottom", "start", "end"):
            getattr(self.msgbox, f"set_margin_{m}")(14)
        self.scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self.scroller.add_css_class("chat-scroll")
        self.scroller.set_child(self.msgbox)
        overlay = Gtk.Overlay()
        bgp = DATA_DIR / "assets" / "chucknorris-bg.png"
        bgp = bgp if bgp.exists() else HERE / "assets" / "chucknorris-bg.png"
        if bgp.exists():
            bg = Gtk.Picture.new_for_filename(str(bgp))
            bg.set_content_fit(Gtk.ContentFit.COVER)
            bg.set_opacity(0.30)
            bg.set_can_target(False)
            overlay.set_child(bg)
        else:
            overlay.set_child(Gtk.Box())
        overlay.add_overlay(self.scroller)

        # composer
        self.entry = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.entry.add_css_class("mono")
        ev = Gtk.ScrolledWindow(min_content_height=48, max_content_height=120, hexpand=True)
        ev.set_child(self.entry)
        self.web_toggle = Gtk.ToggleButton(icon_name="system-search-symbolic")
        self.web_toggle.add_css_class("quick")
        self.web_toggle.set_tooltip_text("Research the live web (multi-source, cited)")
        att = Gtk.Button(icon_name="mail-attachment-symbolic"); att.add_css_class("quick")
        att.set_tooltip_text("Attach a file for Chuck to read"); att.connect("clicked", self.on_attach)
        cam = Gtk.Button(icon_name="camera-photo-symbolic"); cam.add_css_class("quick")
        cam.set_tooltip_text("Show Chuck your screen"); cam.connect("clicked", self.on_screenshot)
        # the SEND plaque as the send button
        send = Gtk.Button(); send.add_css_class("sendbtn")
        sp = DATA_DIR / "assets" / "chucknorris-send.png"
        sp = sp if sp.exists() else HERE / "assets" / "chucknorris-send.png"
        if sp.exists():
            try:
                pic, _ = _pic_from_file(str(sp), -1, 46)
                send.set_child(pic)
            except Exception:
                send.set_label("Send"); send.add_css_class("gold")
        else:
            send.set_label("Send"); send.add_css_class("gold")
        send.connect("clicked", self.on_send)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(row, f"set_margin_{m}")(12)
        row.append(ev); row.append(self.web_toggle); row.append(att); row.append(cam); row.append(send)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(qscroll); body.append(overlay); body.append(Gtk.Separator()); body.append(row)
        tv = Adw.ToolbarView(); tv.add_top_bar(header); tv.set_content(body)
        self.set_content(tv)
        self.connect("close-request", self._on_close)

        self._bot_bubble("Name's Chuck. This machine answers to me now \u2014 and so do its problems. "
                         "Tell me what's broken, what to install, or what to look up. Toggle search for "
                         "cited web answers, hit News, Images or Video, or show me your screen. Every "
                         "fix is a command you approve. Chuck Norris doesn't get segfaults; segfaults "
                         "get Chuck Norris, then apologise.")
        if not self.backend.key():
            self._sys_note("No SiliconFlow key yet \u2014 open Settings. If Basilisk's installed, I "
                           "reuse its key automatically.")

    # ── history / saved chats ──
    def _new_history(self):
        self.history = [{"role": "system",
                         "content": SYSTEM_PROMPT + "\n\nCurrent machine:\n" + gather_context()}]

    def new_chat(self):
        self._save_chat()
        self.chat_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._new_history()
        child = self.msgbox.get_first_child()
        while child:
            nxt = child.get_next_sibling(); self.msgbox.remove(child); child = nxt
        self._bot_bubble("Fresh start. What do you need, partner?")

    def _save_chat(self):
        msgs = [m for m in self.history if m["role"] != "system"
                and isinstance(m.get("content"), str)]
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
        child = self.msgbox.get_first_child()
        while child:
            nxt = child.get_next_sibling(); self.msgbox.remove(child); child = nxt
        for m in self.history:
            if m["role"] == "system":
                continue
            c = m.get("content")
            txt = c if isinstance(c, str) else "[image/attachment]"
            if m["role"] == "user":
                self._user_bubble(txt)
            else:
                set_rich(self._bot_bubble(""), txt)

    def _on_close(self, *_):
        self._save_chat()
        return False

    # ── bubbles ──
    def _scroll_down(self):
        def go():
            a = self.scroller.get_vadjustment(); a.set_value(a.get_upper())
        GLib.idle_add(go)

    def _user_bubble(self, text, shot=False):
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.END, hexpand=True)
        lbl = Gtk.Label(label=text + ("  \U0001F4F7" if shot else ""), xalign=1, wrap=True, selectable=True)
        lbl.set_max_width_chars(70)
        card = Gtk.Box(); card.add_css_class("user-bubble"); card.append(lbl)
        w.append(card); self.msgbox.append(w); self._scroll_down()

    def _bot_bubble(self, text=""):
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.START, hexpand=True)
        lbl = Gtk.Label(label=text, xalign=0, wrap=True, selectable=True, use_markup=False)
        lbl.set_max_width_chars(88)
        card = Gtk.Box(); card.add_css_class("bot-bubble"); card.append(lbl)
        w.append(card); self.msgbox.append(w); self._scroll_down()
        return lbl

    def _sys_note(self, text, css="dim"):
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
        run_btn.set_sensitive(False); status.set_label("running\u2026")

        def worker():
            rc, out = run_command(cmd)

            def show():
                status.remove_css_class("dim"); status.add_css_class("ok" if rc == 0 else "danger")
                status.set_label(f"exit {rc}")
                o = Gtk.Label(label=out[:4000], xalign=0, wrap=True, selectable=True); o.add_css_class("mono")
                self.msgbox.append(o); self._scroll_down()
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
        self._quick("Recommend and install the right GPU drivers for this hardware on Arch/CachyOS. "
                    f"lspci says:\n{gpu}\nExact commands; mention open vs proprietary if NVIDIA.")

    def _junk(self, *_):
        self._sys_note("\U0001F9F9 scanning for junk (read-only)\u2026")

        def worker():
            report, cmds = junk_scan()

            def show():
                set_rich(self._bot_bubble(""),
                         "Here's what's hogging space. Nothing touched \u2014 approve what you want gone:\n\n"
                         + report)
                for label, cmd in cmds:
                    self._sys_note("\u2192 " + label); self._command_card(cmd)
            GLib.idle_add(show)
        threading.Thread(target=worker, daemon=True).start()

    def _news_prompt(self, *_):
        self._news_mode = True; self.web_toggle.set_active(False)
        self._sys_note("\U0001F4F0 News mode: type a topic and Send \u2014 I'll cross-check sources and "
                       "report only what's corroborated, with citations.")

    def _image_prompt(self, *_):
        q = self._get_entry()
        if not q:
            self._sys_note("Type what to show you in the box, then hit Images.")
            return
        self.entry.get_buffer().set_text("")
        self._user_bubble("\U0001F5BC " + q)
        self._sys_note("searching images\u2026")

        def worker():
            urls = image_search(q)

            def show():
                if not urls:
                    self._sys_note("No images found (or search blocked / offline).")
                    return
                self._sys_note(f"top {len(urls)} for \u201c{q}\u201d:")
                for u in urls:
                    p = download_image(u)
                    if p:
                        GLib.idle_add(self._image_bubble, p, u)
            GLib.idle_add(show)
        threading.Thread(target=worker, daemon=True).start()

    def _video_prompt(self, *_):
        url = self._get_entry().strip()
        if not url.startswith("http"):
            self._sys_note("Paste a video URL in the box, then hit Video.")
            return
        if not shutil.which("yt-dlp"):
            self._sys_note("yt-dlp isn't installed. Install it: it's in the pacman repos "
                           "(`sudo pacman -S yt-dlp`).")
            self._command_card("sudo pacman -S --needed yt-dlp")
            return
        self.entry.get_buffer().set_text("")
        self._user_bubble("\u2B07 " + url)
        self._sys_note(f"downloading to {DL_DIR}\u2026")

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
            GLib.idle_add(self._sys_note, msg, "ok" if msg.startswith("\u2713") else "danger")
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
        shot = self.pending_shot
        had_file = self.pending_file is not None
        display = text
        if had_file:
            fname, fbody = self.pending_file
            display = (text or f"Look at {fname}.") + f"  \U0001F4CE {fname}"
            text = (text or "Look at this file.") + \
                f"\n\n--- attached file: {fname} ---\n{fbody}\n--- end ---"
            self.pending_file = None
        if shot:
            self._user_bubble(display if had_file else (text or "(look at this)"), shot=True)
            vmsg = {"role": "user", "content": [
                {"type": "text", "text": text or "What's wrong on my screen?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + shot}}]}
            msgs = self.history + [vmsg]
            self.history.append({"role": "user",
                                 "content": (text or "What's wrong on my screen?") + " [screenshot]"})
            self.pending_shot = None
            self._stream_into_bubble(msgs, vision=True)
        elif had_file:
            self._user_bubble(display)
            self.history.append({"role": "user", "content": text}); self._ask_model()
        elif self._news_mode:
            self._news_mode = False
            self._user_bubble("\U0001F4F0 " + text); self._research(text, NEWS_PROMPT, tag="news")
        elif self.web_toggle.get_active():
            self._user_bubble("\U0001F50E " + text); self._research(text)
        else:
            self._user_bubble(text)
            self.history.append({"role": "user", "content": text}); self._ask_model()

    def _research(self, query, prompt=None, tag="researched"):
        prompt = prompt or RESEARCH_PROMPT
        self._sys_note("\U0001F50E searching the web (multiple sources)\u2026")

        def worker():
            results = web_search(query)
            if not results:
                GLib.idle_add(self._sys_note, "Web search failed \u2014 answering from knowledge; verify anything important.")
                self.history.append({"role": "user", "content": query})
                GLib.idle_add(self._ask_model)
                return
            blocks = []
            for i, (title, url, snip) in enumerate(results[:5], 1):
                blocks.append(f"[{i}] {title}\nURL: {url}\n{web_fetch(url) or snip}")
            msgs = [{"role": "system", "content": prompt},
                    {"role": "user", "content": f"Question: {query}\n\nSOURCES:\n" + "\n\n".join(blocks)}]
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
            GLib.idle_add(self._bot_label.set_text, self._bot_text)
            self._scroll_down()

        def on_done():
            GLib.idle_add(self._finalise)

        def on_error(msg):
            GLib.idle_add(self._bot_label.set_text, "\u26a0 " + msg)

        threading.Thread(target=self.backend.stream,
                         args=(messages, on_delta, on_done, on_error, vision), daemon=True).start()

    def _finalise(self):
        text = self._bot_text
        self.history.append({"role": "assistant", "content": text})
        blocks = re.findall(r"```(?:bash|sh)?\s*\n?(.*?)```", text, re.DOTALL)
        clean = re.sub(r"```(?:bash|sh)?\s*\n?.*?```", "", text, flags=re.DOTALL).strip()
        set_rich(self._bot_label, clean or "Here's what I'd do:")
        for blk in blocks:
            for line in [ln.strip() for ln in blk.splitlines()
                         if ln.strip() and not ln.strip().startswith("#")]:
                self._command_card(line)
        if self.tts_btn.get_active():
            speak(clean)
        self._save_chat()
        self._scroll_down()
        return False

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
        box.append(Gtk.Label(label="Proxy for web/images/video (optional, e.g. Mullvad "
                             "http://10.64.0.1:1080)", xalign=0))
        proxy = Gtk.Entry(text=self.settings.get("proxy", ""),
                          placeholder_text="http://host:port"); box.append(proxy)
        hint = Gtk.Label(xalign=0, wrap=True); hint.add_css_class("dim")
        hint.set_label("Key: cloud.siliconflow.com/account/ak (Basilisk's is reused if present). "
                       "Proxy routes fetches through Mullvad etc. For a full VPN just connect Mullvad "
                       "system-wide. Voice needs espeak-ng; video needs yt-dlp.")
        box.append(hint)

        def save(*_):
            self.settings["siliconflow_api_key"] = key.get_text().strip()
            self.settings["model"] = model.get_text().strip() or DEFAULT_MODEL
            self.settings["vision_model"] = vmodel.get_text().strip() or DEFAULT_VISION
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

    def do_activate(self):
        (self.props.active_window or ChuckWindow(self)).present()


def main():
    Adw.init()
    return ChuckApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
