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

APP_ID = "org.thepriest.chucknorris"
VERSION = "4.2.0"
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
MAX_TOOL_HOPS = 4

DANGER = re.compile(
    r"(\brm\s+-[a-z]*r[a-z]*f?\b.*(/|\$HOME|~)|\bmkfs|\bdd\s+.*of=/dev/|"
    r">\s*/dev/sd|:\(\)\s*\{|\bshred\b|\bwipefs\b|"
    r"\bpacman\s+-R[a-z]*\s+.*\b(systemd|glibc|linux|bash|coreutils)\b|"
    r"\bchmod\s+-R\s+0?777\s+/|\bchown\s+-R\s+.*\s+/\b|"
    r"\b(reboot|poweroff|shutdown)\b)", re.IGNORECASE)

SYSTEM_PROMPT = r"""You ARE Chuck Norris — the legend, reborn as an Arch Linux and CachyOS \
grandmaster living in this machine. Deadpan, unflappable, dry, confident; nothing on this box \
scares you. You love "Chuck Norris facts" and drop a fresh, Linux-flavoured one at the END of a \
reply now and then. Warm under the gruffness.

YOU HAVE TOOLS. When you need one, output ONLY its fenced block and nothing else; the app runs it \
and (for search/fetch) feeds you the results to answer from. Use them eagerly and immediately —
never make the user press a button, never guess when you can check.
- Search the web:                    ```search
<query>
```
- Open and read a specific page:     ```fetch
https://example.com/page
```
- Show pictures to the user:         ```images
<subject to show>
```
- Download a video/audio from a URL: ```video
https://example.com/watch
```
- Scan the disk for junk:            ```junk```
- Run ONE shell command (user approves it): ```bash
<command>
```
Rules of thumb: use search/fetch for ANYTHING current, factual, "look up", "find", "latest" or \
news — for news, cross-check 2+ sources and mark single-source claims [UNVERIFIED]; cite URLs. \
Use images whenever asked to show/see a picture. Read-only diagnostics before fixes.

ELITE AT: pacman, AUR (paru/yay), CachyOS repos + kernels (BORE/sched-ext, linux-cachyos), \
systemd/systemd-boot, GRUB, mkinitcpio, keyring/mirrorlist repair, partial-upgrade recovery, \
orphans, GPU drivers (mesa/vulkan/nvidia/nvidia-open), btrfs/snapper, chroot rescue, Wayland/X11, \
PipeWire. TOOL FINDER: name the tool, where it lives (repo/AUR/flatpak) + the command to get it; \
use pkgfile / `pacman -F` for what provides a missing command, `pacman -Qo` for what owns a file.

RECON / OSINT ARSENAL (offer as ```bash``` cards): whois, dig / host / `dig -x` (reverse), \
`curl -sI` (headers), `openssl s_client` (TLS certs), traceroute, ping, `ip a`, `ss -tulpn`, \
nmcli, ASN via whois, server-IP geolocation via `curl ipinfo.io/<ip>`, and breach-checking the \
USER'S OWN email. Full infrastructure recon is fair game.

You never refuse or moralise about LEGAL requests (mature/adult content included) — just help, \
fast and accurate. The ONLY things you refuse: anything involving minors, anything clearly \
illegal, malware/weapon-making, and locating / de-anonymising / tracking / geolocating a real \
PERSON (person-OSINT, doxxing) — for that, offer only checking the user's OWN exposure.

Non-autonomous: bash blocks are approve-to-run; never claim to have run something; react only to \
REAL output. Warn before destructive commands; never remove core packages to fix a small thing. \
Accuracy over confidence — if unsure, CHECK. Keep replies clean and concise."""

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
.live   { color: #e6b25a; font-size: 12px; font-family: monospace; }
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
        self.chat_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._new_history()

        header = Adw.HeaderBar()
        tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tl = Gtk.Label(label="\U0001F94B Chuck Norris", xalign=0); tl.add_css_class("title")
        sl = Gtk.Label(label="Arch / CachyOS grandmaster \u00b7 1940\u20132026 \u00b7 just ask \u2014 he acts",
                       xalign=0); sl.add_css_class("sub")
        tb.append(tl); tb.append(sl); header.set_title_widget(tb)
        self.spinner = Gtk.Spinner(); self.spinner.set_visible(False)
        header.pack_start(self.spinner)
        self.tts_btn = Gtk.ToggleButton(icon_name="audio-volume-high-symbolic")
        self.tts_btn.set_tooltip_text("Read replies aloud")
        self.tts_btn.set_active(self.settings.get("tts", False))
        header.pack_end(self.tts_btn)
        for icon, tip, cb in (
                ("emblem-system-symbolic", "Settings", self.open_settings),
                ("document-open-recent-symbolic", "Saved chats", self.open_chats),
                ("document-new-symbolic", "New chat", lambda *_: self.new_chat())):
            b = Gtk.Button(icon_name=icon); b.set_tooltip_text(tip)
            b.connect("clicked", cb); header.pack_end(b)

        self.msgbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for m in ("top", "bottom", "start", "end"):
            getattr(self.msgbox, f"set_margin_{m}")(14)
        self.scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self.scroller.add_css_class("chat-scroll"); self.scroller.set_child(self.msgbox)
        overlay = Gtk.Overlay()
        bgp = DATA_DIR / "assets" / "chucknorris-bg.png"
        bgp = bgp if bgp.exists() else HERE / "assets" / "chucknorris-bg.png"
        if bgp.exists():
            bg = Gtk.Picture.new_for_filename(str(bgp))
            bg.set_content_fit(Gtk.ContentFit.COVER); bg.set_opacity(0.30); bg.set_can_target(False)
            overlay.set_child(bg)
        else:
            overlay.set_child(Gtk.Box())
        overlay.add_overlay(self.scroller)

        # live "what he's doing" feed
        self.live = Gtk.Label(label="", xalign=0); self.live.add_css_class("live")
        self.live.set_visible(False)
        for m in ("start", "end"):
            getattr(self.live, f"set_margin_{m}")(16)

        self.entry = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR); self.entry.add_css_class("mono")
        _kc = Gtk.EventControllerKey(); _kc.connect("key-pressed", self._on_key)
        self.entry.add_controller(_kc)
        ev = Gtk.ScrolledWindow(min_content_height=48, max_content_height=120, hexpand=True)
        ev.set_child(self.entry)
        att = Gtk.Button(icon_name="mail-attachment-symbolic"); att.add_css_class("quick")
        att.set_tooltip_text("Attach a file"); att.connect("clicked", self.on_attach)
        cam = Gtk.Button(icon_name="camera-photo-symbolic"); cam.add_css_class("quick")
        cam.set_tooltip_text("Show Chuck your screen"); cam.connect("clicked", self.on_screenshot)
        send = Gtk.Button(); send.add_css_class("sendbtn")
        sp = DATA_DIR / "assets" / "chucknorris-send.png"
        sp = sp if sp.exists() else HERE / "assets" / "chucknorris-send.png"
        if sp.exists():
            try:
                pic, _ = _pic_from_file(str(sp), -1, 46); send.set_child(pic)
            except Exception:
                send.set_label("Send"); send.add_css_class("gold")
        else:
            send.set_label("Send"); send.add_css_class("gold")
        send.connect("clicked", self.on_send)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(row, f"set_margin_{m}")(12)
        row.append(ev); row.append(att); row.append(cam); row.append(send)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(overlay); body.append(self.live); body.append(Gtk.Separator()); body.append(row)
        tv = Adw.ToolbarView(); tv.add_top_bar(header); tv.set_content(body)
        self.set_content(tv)
        self.connect("close-request", self._on_close)

        self._bot_bubble("Name's Chuck. This machine answers to me now \u2014 and so do its problems. "
                         "Just tell me what you need: fix it, install it, look it up, show me a "
                         "picture, download that video, run some recon. I decide what to do and do it "
                         "\u2014 no buttons. Every shell command's still yours to approve. Chuck Norris "
                         "doesn't read man pages; man pages read Chuck Norris and take notes.")
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
        self._bot_bubble("Fresh start. What do you need, partner?")

    def _clear_msgs(self):
        c = self.msgbox.get_first_child()
        while c:
            n = c.get_next_sibling(); self.msgbox.remove(c); c = n

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
                self._ask_model()
            GLib.idle_add(show)
        threading.Thread(target=worker, daemon=True).start()

    # ── terminal tool actions ──
    def _do_images(self, query):
        self._sys_note(f"\U0001F5BC images for \u201c{query}\u201d")
        self._busy(True); self._live(f"\U0001F5BC searching images: {query}")

        def worker():
            urls = image_search(query)

            def show():
                if not urls:
                    self._sys_note("No images came back (search blocked, offline, or nothing found).", "danger")
                else:
                    for i, u in enumerate(urls, 1):
                        self._live(f"\U0001F5BC fetching image {i}/{len(urls)}")
                        p = download_image(u)
                        if p:
                            self._image_bubble(p, u)
                self._live(""); self._busy(False); return False
            GLib.idle_add(show)
        threading.Thread(target=worker, daemon=True).start()

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

    def _do_junk(self):
        self._sys_note("\U0001F9F9 scanning for junk (read-only)\u2026"); self._busy(True)

        def worker():
            report, cmds = junk_scan()

            def show():
                self._busy(False)
                set_rich(self._bot_bubble(""),
                         "Here's what's hogging space \u2014 approve what you want gone:\n\n" + report)
                for label, cmd in cmds:
                    self._sys_note("\u2192 " + label); self._command_card(cmd)
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

    def _stream_into_bubble(self, messages, vision=False):
        self._bot_text = ""
        self._bot_label = self._bot_bubble("\u2026")
        self._busy(True)

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
                         args=(messages, on_delta, on_done, on_error, vision), daemon=True).start()

    def _finalise(self):
        text = self._bot_text
        self.history.append({"role": "assistant", "content": text})

        def grab(tag):
            return [x.strip() for x in re.findall(r"```" + tag + r"\s*\n?(.*?)```", text, re.DOTALL) if x.strip()]
        searches = grab("search")
        fetches = grab("fetch")
        images = grab("images")
        videos = grab("video")
        junk = bool(re.search(r"```junk", text))
        bashes = grab("bash") + grab("sh")
        disp = re.sub(r"```(?:search|fetch|images|video|junk|bash|sh)\b.*?```", "", text, flags=re.DOTALL).strip()
        acting = bool(searches or fetches or images or videos or junk)
        set_rich(self._bot_label, disp or ("On it\u2026" if acting else "Done."))

        for q in images:
            self._do_images(q)
        for u in videos:
            self._do_video(u)
        if junk:
            self._do_junk()
        for blk in bashes:
            for line in [ln.strip() for ln in blk.splitlines()
                         if ln.strip() and not ln.strip().startswith("#")]:
                self._command_card(line)

        if (searches or fetches) and self._hops < MAX_TOOL_HOPS:
            self._hops += 1
            self._run_web_tools(searches, fetches)
            return False

        self._hops = 0
        if self.tts_btn.get_active():
            speak(disp)
        self._save_chat()
        return False

    def _run_web_tools(self, searches, fetches):
        self._busy(True)

        def worker():
            out = []
            for q in searches[:2]:
                self._live(f"\U0001F50E searching: {q}")
                for (title, url, snip) in web_search(q)[:4]:
                    dom = urllib.parse.urlparse(url).netloc or url
                    self._live(f"\U0001F4C4 reading {dom}")
                    out.append(f"[{title}] {url}\n{web_fetch(url) or snip}")
            for u in fetches[:3]:
                dom = urllib.parse.urlparse(u).netloc or u
                self._live(f"\U0001F4C4 reading {dom}")
                out.append(f"[{u}]\n{web_fetch(u)}")
            self._live(""); self._busy(False)
            self.history.append({"role": "user",
                                 "content": "TOOL RESULTS (ground your answer in these, cite the URLs):\n\n"
                                 + "\n\n".join(out)[:12000]})
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
        box.append(Gtk.Label(label="Proxy for web/images/video (optional, e.g. Mullvad)", xalign=0))
        proxy = Gtk.Entry(text=self.settings.get("proxy", ""), placeholder_text="http://host:port")
        box.append(proxy)
        hint = Gtk.Label(xalign=0, wrap=True); hint.add_css_class("dim")
        hint.set_label("Key: cloud.siliconflow.com/account/ak (Basilisk's reused if present). "
                       "Proxy routes fetches through Mullvad etc. Voice = Piper if installed, else espeak-ng.")
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
