"""browser.py — a headless browser the agent can drive, with no dependencies.

Chuck builds web pages and games; to check one it used to take a screenshot
and squint. That misses what actually breaks web apps: a JavaScript exception
on load, a 404 for app.js, a console error when a key is pressed. This module
launches the user's Chromium headless and drives it over the DevTools
protocol, through a small stdlib WebSocket client, so it can:

  * load a URL or a local file at a chosen window size;
  * collect console errors and warnings, uncaught exceptions, failed requests
    and HTTP error responses — with file and line where the browser gives them;
  * act on the page: click, type, press keys, scroll, wait, evaluate JS;
  * return a screenshot (PNG) and the page's title and visible text.

Nothing here knows about GTK or the model; browse() takes plain arguments and
returns a plain dict.
"""
import base64
import json
import os
import re
import select
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

_BRAVE = ("brave", "brave-browser", "brave-browser-stable", "brave-browser-beta",
          "/opt/brave-bin/brave", "/opt/brave.com/brave/brave", "/usr/lib/brave-bin/brave",
          "/snap/bin/brave", "/var/lib/flatpak/exports/bin/com.brave.Browser",
          "~/.local/share/flatpak/exports/bin/com.brave.Browser")
_CHROMIUM = ("chromium", "chromium-browser", "/usr/lib/chromium/chromium", "/snap/bin/chromium",
             "/var/lib/flatpak/exports/bin/org.chromium.Chromium",
             "~/.local/share/flatpak/exports/bin/org.chromium.Chromium",
             "/opt/pw-browsers/chromium")
_CHROME = ("google-chrome", "google-chrome-stable", "/opt/google/chrome/chrome", "chrome",
           "microsoft-edge", "microsoft-edge-stable", "vivaldi", "vivaldi-stable")
ENGINES = ("auto", "brave", "chromium", "chrome", "camoufox")
CAMOUFOX_INSTALL = ("pip install --user -U 'camoufox[geoip]' && python3 -m camoufox fetch")


def _first(names):
    for n in names:
        p = os.path.expanduser(n)
        if os.sep in p:
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
        else:
            w = shutil.which(p)
            if w:
                return w
    return None


def find_chrome(prefer="auto"):
    """A Chromium-family browser (they all speak the DevTools protocol):
    CHROME_BIN wins, then the preferred one, then Brave, Chromium, Chrome."""
    env = os.environ.get("CHROME_BIN") or os.environ.get("CHUCK_CHROME")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    order = {"brave": (_BRAVE,), "chromium": (_CHROMIUM,), "chrome": (_CHROME,)}.get(
        prefer, ())
    for group in order + (_BRAVE, _CHROMIUM, _CHROME):
        found = _first(group)
        if found:
            return found
    return None


def camoufox_available():
    try:
        import camoufox.sync_api  # noqa: F401
        return True
    except Exception:
        return False


def resolve_engine(pref="auto"):
    """-> ('cdp', executable) or ('camoufox', executable_path_or_None).
    Raises RuntimeError with what to install when the choice isn't available."""
    pref = (pref or "auto").strip()
    low = pref.lower()
    if low == "camoufox" or (os.sep in pref and "camoufox" in os.path.basename(pref).lower()):
        if not camoufox_available():
            raise RuntimeError("Camoufox isn't installed. Install it with: " + CAMOUFOX_INSTALL)
        return "camoufox", (os.path.expanduser(pref) if os.sep in pref else None)
    if os.sep in pref or pref.startswith("~"):
        p = os.path.expanduser(pref)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return "cdp", p
        raise RuntimeError(f"browser not found or not executable: {pref}")
    groups = {"brave": _BRAVE, "chromium": _CHROMIUM, "chrome": _CHROME}
    if low in groups:
        exe = _first(groups[low])                     # that browser, or a clear no
        if not exe:
            raise RuntimeError(f"{pref.capitalize()} isn't installed (or isn't on PATH) — "
                               "install it, or pick Automatic in Settings")
        return "cdp", exe
    exe = find_chrome("auto")
    if exe:
        return "cdp", exe
    if low == "auto" and camoufox_available():
        return "camoufox", None
    raise RuntimeError("no browser found: install Brave or Chromium (or Camoufox: "
                       + CAMOUFOX_INSTALL + ")")


# ── a minimal WebSocket client (RFC 6455), enough for the DevTools protocol ──
class _WS:
    def __init__(self, url, timeout=20):
        u = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (("?" + u.query) if u.query else "")
        req = (f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake: connection closed")
            head += chunk
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status + b" ":
            raise ConnectionError(f"websocket handshake refused: {status!r}")
        self.buf = head.split(b"\r\n\r\n", 1)[1]

    def send(self, text):
        data = text.encode()
        n = len(data)
        hdr = bytearray([0x81])                          # FIN + text
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126); hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127); hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        self.sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        """The next complete text message (control frames handled here)."""
        parts = []
        while True:
            b1, b2 = self._read(2)
            fin, op = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if op == 0x8:
                raise ConnectionError("websocket closed by the browser")
            if op == 0x9:                                 # ping -> pong
                self.sock.sendall(bytes([0x8A, len(payload)]) + payload[:125])
                continue
            if op in (0x1, 0x2, 0x0):
                parts.append(payload)
                if fin:
                    return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class _CDP:
    """One page target. call() returns a result; events pile up in .events."""
    def __init__(self, ws_url):
        self.ws = _WS(ws_url)
        self.next_id = 0
        self.events = []

    def call(self, method, params=None, timeout=20):
        self.next_id += 1
        mid = self.next_id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            self.ws.sock.settimeout(max(0.05, end - time.time()))
            try:
                msg = json.loads(self.ws.recv())
            except socket.timeout:
                break
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)
        raise TimeoutError(f"{method} timed out")

    def pump(self, seconds):
        """Collect events for a while (the page settling, timers firing)."""
        end = time.time() + seconds
        while time.time() < end:
            self.ws.sock.settimeout(max(0.02, end - time.time()))
            try:
                msg = json.loads(self.ws.recv())
            except (socket.timeout, TimeoutError):
                return
            if "method" in msg:
                self.events.append(msg)

    def wait_for(self, method, seconds):
        start = len(self.events)
        end = time.time() + seconds
        while time.time() < end:
            if any(e.get("method") == method for e in self.events[start:]):
                return True
            self.pump(min(0.2, max(0.02, end - time.time())))
        return False

    def evaluate(self, expr, timeout=15):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                           "awaitPromise": True, "userGesture": True},
                      timeout=timeout)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            msg = (d.get("exception") or {}).get("description") or d.get("text") or "error"
            raise RuntimeError(msg.splitlines()[0])
        return (r.get("result") or {}).get("value")


def _launch(chrome, profile, startup=20):
    args = [chrome, "--headless=new", "--remote-debugging-port=0",
            f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
            "--disable-gpu", "--hide-scrollbars", "--mute-audio",
            "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
            # no phoning home: component updates, metrics, safe-browsing pings
            "--disable-background-networking", "--disable-component-update",
            "--disable-sync", "--disable-default-apps", "--disable-domain-reliability",
            "--disable-client-side-phishing-detection", "--metrics-recording-only",
            "--no-pings", "about:blank"]
    if FILES_ROOT[0] is None:
        args.insert(-1, "--allow-file-access-from-files")   # a local page's own assets
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args.insert(1, "--no-sandbox")            # Chromium refuses to run as root otherwise
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, start_new_session=True)
    end = time.time() + startup
    fd, buf = proc.stderr.fileno(), b""
    while time.time() < end:
        # readline() would block past the deadline on a browser that prints
        # nothing: wait for data with the time that's left
        ready, _w, _x = select.select([fd], [], [], max(0.0, end - time.time()))
        if not ready:
            break
        chunk = os.read(fd, 65536)
        if not chunk:
            proc.wait(timeout=5)
            raise RuntimeError("the browser exited at start")
        buf = (buf + chunk)[-65536:]
        m = re.search(rb"DevTools listening on (ws://\S+)", buf)
        if m:
            # keep reading its log: a full stderr pipe would freeze the browser
            threading.Thread(target=_drain, args=(proc.stderr,), daemon=True,
                             name="browser-log").start()
            return proc, m.group(1).decode()
    proc.kill()
    raise RuntimeError(f"the browser didn't start within {startup}s")


def _drain(f):
    try:
        while f.read(65536):
            pass
    except (OSError, ValueError):
        pass


def _page_ws(browser_ws):
    port = urllib.parse.urlparse(browser_ws).port
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as r:
                pages = [t for t in json.loads(r.read()) if t.get("type") == "page"]
            if pages:
                return pages[0]["webSocketDebuggerUrl"]
        except OSError:
            pass
        time.sleep(0.1)
    raise RuntimeError("no page target in the browser")


# keys the agent may press: name -> (key, code, windowsVirtualKeyCode)
_KEYS = {
    "enter": ("Enter", "Enter", 13), "tab": ("Tab", "Tab", 9), "escape": ("Escape", "Escape", 27),
    "esc": ("Escape", "Escape", 27), "space": (" ", "Space", 32), "backspace": ("Backspace", "Backspace", 8),
    "delete": ("Delete", "Delete", 46), "arrowleft": ("ArrowLeft", "ArrowLeft", 37),
    "arrowup": ("ArrowUp", "ArrowUp", 38), "arrowright": ("ArrowRight", "ArrowRight", 39),
    "arrowdown": ("ArrowDown", "ArrowDown", 40), "left": ("ArrowLeft", "ArrowLeft", 37),
    "up": ("ArrowUp", "ArrowUp", 38), "right": ("ArrowRight", "ArrowRight", 39),
    "down": ("ArrowDown", "ArrowDown", 40), "home": ("Home", "Home", 36), "end": ("End", "End", 35),
    "pageup": ("PageUp", "PageUp", 33), "pagedown": ("PageDown", "PageDown", 34),
}


def _key(cdp, name):
    k = _KEYS.get(name.lower())
    if k is None:
        if len(name) == 1:
            ch = name
            code = ("Key" + ch.upper()) if ch.isalpha() else ("Digit" + ch if ch.isdigit() else "")
            k = (ch, code, ord(ch.upper()))
        else:
            raise ValueError(f"unknown key {name!r}")
    key, code, vk = k
    base = {"key": key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
    text = key if len(key) == 1 else ("\r" if key == "Enter" else "")
    cdp.call("Input.dispatchKeyEvent", dict(base, type="keyDown", text=text) if text
             else dict(base, type="rawKeyDown"))
    cdp.call("Input.dispatchKeyEvent", dict(base, type="keyUp"))


def _click(cdp, target):
    m = re.fullmatch(r"\s*(\d+)\s*[, ]\s*(\d+)\s*", target)
    if m:
        x, y = int(m.group(1)), int(m.group(2))
    else:
        box = cdp.evaluate(
            "(()=>{const e=document.querySelector(%s); if(!e) return null;"
            "e.scrollIntoView({block:'center'}); const r=e.getBoundingClientRect();"
            "return [r.left+r.width/2, r.top+r.height/2];})()" % json.dumps(target))
        if not box:
            raise ValueError(f"no element matches {target!r}")
        x, y = box
    for t in ("mouseMoved", "mousePressed", "mouseReleased"):
        cdp.call("Input.dispatchMouseEvent", {"type": t, "x": x, "y": y, "button": "left",
                                              "clickCount": 1})


def _src(details):
    url = details.get("url") or ""
    line = details.get("lineNumber")
    name = url.rsplit("/", 1)[-1] if url else ""
    return f" ({name}:{line + 1})" if name and line is not None else (f" ({name})" if name else "")


def _collect(events):
    """console errors/warnings, exceptions, failed requests -> lists of strings."""
    errors, warnings, logs, failed = [], [], [], []
    req_urls = {}
    for e in events:
        m, p = e.get("method"), e.get("params", {})
        if m == "Runtime.consoleAPICalled":
            text = " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", []))
            frames = ((p.get("stackTrace") or {}).get("callFrames") or [{}])
            where = _src(frames[0]) if frames else ""
            if p.get("type") in ("error", "assert"):
                errors.append(f"console.error: {text}{where}")
            elif p.get("type") == "warning":
                warnings.append(f"console.warn: {text}{where}")
            else:
                logs.append(text)
        elif m == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails", {})
            desc = ((d.get("exception") or {}).get("description") or d.get("text") or "")
            errors.append("Uncaught " + desc.splitlines()[0].replace("Uncaught ", "")
                          + _src(d))
        elif m == "Log.entryAdded":
            en = p.get("entry", {})
            # network errors are reported once, under failed requests
            if en.get("level") == "error" and en.get("source") not in ("javascript", "network"):
                errors.append(f"{en.get('source', 'browser')}: {en.get('text', '')}"
                              + (f" ({en['url']})" if en.get("url") else ""))
        elif m == "Network.requestWillBeSent":
            req_urls[p.get("requestId")] = (p.get("request") or {}).get("url", "")
        elif m == "Network.loadingFailed" and not p.get("canceled"):
            failed.append(f"{req_urls.get(p.get('requestId'), '?')}: {p.get('errorText')}")
        elif m == "Network.responseReceived":
            r = p.get("response", {})
            if (r.get("status") or 0) >= 400:
                failed.append(f"{r.get('url')}: HTTP {r.get('status')}")
    dedup = lambda xs: list(dict.fromkeys(xs))       # noqa: E731
    return dedup(errors), dedup(warnings), logs, dedup(failed)


# Set by the app when the working folder's access is restricted: local files
# may be opened only inside this folder, never in the hidden credential
# folders, and pages get no file:// access beyond their own file.
FILES_ROOT = [None]
HIDDEN = [()]


def _file_allowed(path):
    root = FILES_ROOT[0]
    if root is None:
        return True
    rp, rr = os.path.realpath(path), os.path.realpath(root)
    if not (rp == rr or rp.startswith(rr + os.sep)):
        return False
    return not any(rp == h or rp.startswith(h + os.sep) for h in HIDDEN[0])


def to_url(target, root=None):
    """A URL from what the agent wrote: http(s)/file URLs pass through, a bare
    host:port gets http://, anything else is a file path (relative to root)."""
    t = (target or "").strip().strip("'\"")
    if t.lower().startswith("file:"):
        path = urllib.parse.unquote(urllib.parse.urlparse(t).path)
        if not _file_allowed(path):
            raise FileNotFoundError(f"{path} is outside this folder (its access is restricted)")
        return t
    if re.match(r"^[a-z][a-z0-9+.-]*://", t, re.I) or t.startswith(("about:", "data:")):
        return t
    if re.match(r"^(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:\d+)?(/.*)?$", t):
        return "http://" + t
    p = os.path.expanduser(t)
    if not os.path.isabs(p) and root:
        p = os.path.join(str(root), p)
    if os.path.isdir(p):
        p = os.path.join(p, "index.html")
    if not os.path.isfile(p):
        raise FileNotFoundError(f"no such file: {t}")
    if not _file_allowed(p):
        raise FileNotFoundError(f"{t} is outside this folder (its access is restricted)")
    return "file://" + urllib.parse.quote(os.path.abspath(p))


def browse(target, actions=(), size=(1280, 800), root=None, shot_path=None,
           settle=0.6, chrome=None, timeout=60, engine="auto", text_js=None):
    """Load target, run actions, report. -> dict(url, title, text, errors,
    warnings, logs, failed, steps, shot, ms, engine). Raises RuntimeError when
    no browser is available or it can't start.

    engine: 'auto' (Brave, Chromium, Chrome — then Camoufox), 'brave',
    'chromium', 'chrome', 'camoufox', or a path to a browser binary."""
    if not chrome:
        kind, exe = resolve_engine(engine)
        if kind == "camoufox":
            r = browse_playwright(target, actions, size, root, shot_path, settle, timeout,
                                  launcher=_camoufox_launcher(exe), text_js=text_js)
            r["engine"] = "Camoufox"
            return r
        chrome = exe
    r = _browse_cdp(target, actions, size, root, shot_path, settle, chrome, timeout, text_js)
    r["engine"] = os.path.basename(chrome)
    return r


# ── reading a page that refused a plain fetch ───────────────────────────────
# Bot walls ("Just a moment…", "Checking your browser") clear themselves in a
# real browser after a few seconds; JavaScript-only sites need their scripts
# run before there is any text at all. read_page() does both.
CHALLENGE = re.compile(
    r"(?i)just a moment\.\.\.|checking (?:if the site connection is secure|your browser)"
    r"|attention required! \| cloudflare|cf-browser-verification|challenge-platform"
    r"|enable javascript and cookies to continue|please enable js and disable any ad blocker"
    r"|captcha-delivery|px-captcha|verify (?:that )?you are (?:a )?human|are you a robot")
_CHALLENGE_PROBE = ("document.title + '\\n' + "
                    "(document.body ? document.body.innerText.slice(0, 3000) : '')")
# The readable part: an <article> or <main> when the page has one.
READER_JS = ("(document.querySelector('article') || document.querySelector('main') "
             "|| document.querySelector('[role=main]') || document.body || {innerText: ''}).innerText")


def challenged(text):
    return bool(CHALLENGE.search(text or ""))


def read_page(url, engine="auto", timeout=45, limit=4000):
    """Load url in a real browser, wait out a bot check, return
    (text, engine name, still_blocked). Raises RuntimeError when no browser
    can run."""
    r = browse(url, actions=("unblock 10000",), settle=1.2, timeout=timeout,
               engine=engine, text_js=READER_JS)
    text = re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", r.get("text") or "")).strip()
    blocked = challenged((r.get("title") or "") + "\n" + text[:3000])
    return text[:limit], r.get("engine", ""), blocked


def _browse_cdp(target, actions, size, root, shot_path, settle, chrome, timeout, text_js=None):
    url = to_url(target, root)
    t0 = time.time()
    profile = tempfile.mkdtemp(prefix="chuck_browse_")
    proc = cdp = None
    try:
        proc, bws = _launch(chrome, profile)
        cdp = _CDP(_page_ws(bws))
        for dom in ("Runtime", "Log", "Page", "Network"):
            cdp.call(f"{dom}.enable")
        try:
            # headless Chrome announces itself as "HeadlessChrome" and some
            # sites refuse that outright; present the ordinary browser it is
            ua = cdp.call("Browser.getVersion").get("userAgent", "")
            if "Headless" in ua:
                cdp.call("Network.setUserAgentOverride",
                         {"userAgent": ua.replace("HeadlessChrome", "Chrome"),
                          "acceptLanguage": "en-US,en;q=0.9"})
        except Exception:
            pass
        w, h = size
        cdp.call("Emulation.setDeviceMetricsOverride",
                 {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False})
        cdp.call("Page.navigate", {"url": url})
        cdp.wait_for("Page.loadEventFired", 20)
        cdp.pump(settle)
        steps = []
        for raw in actions:
            if time.time() - t0 > timeout:
                steps.append(f"{raw}: skipped (time limit)")
                continue
            verb, _, arg = raw.strip().partition(" ")
            verb, arg = verb.lower(), arg.strip()
            try:
                if verb == "click":
                    _click(cdp, arg); res = "ok"
                elif verb == "type":
                    cdp.call("Input.insertText", {"text": arg}); res = "ok"
                elif verb in ("press", "key"):
                    for k in arg.split():
                        _key(cdp, k)
                    res = "ok"
                elif verb == "wait":
                    cdp.pump(min(10.0, float(arg or 500) / 1000.0)); res = "ok"
                elif verb == "unblock":                      # read_page: outwait a bot check
                    end = time.time() + min(20.0, float(arg or 10000) / 1000.0)
                    while time.time() < end and challenged(cdp.evaluate(_CHALLENGE_PROBE) or ""):
                        cdp.pump(0.5)
                    res = "ok"
                elif verb == "scroll":
                    cdp.evaluate(f"window.scrollBy(0, {int(float(arg or 600))})"); res = "ok"
                elif verb in ("eval", "js"):
                    v = cdp.evaluate(arg)
                    res = "→ " + (json.dumps(v)[:1500] if v is not None else "undefined")
                elif verb in ("goto", "open"):
                    cdp.call("Page.navigate", {"url": to_url(arg, root)})
                    cdp.wait_for("Page.loadEventFired", 20); res = "ok"
                elif verb == "size":
                    m = re.fullmatch(r"(\d+)\s*[x×]\s*(\d+)", arg)
                    if not m:
                        raise ValueError("size wants WIDTHxHEIGHT")
                    cdp.call("Emulation.setDeviceMetricsOverride",
                             {"width": int(m.group(1)), "height": int(m.group(2)),
                              "deviceScaleFactor": 1, "mobile": False}); res = "ok"
                else:
                    raise ValueError(f"unknown action {verb!r} (click, type, press, wait, "
                                     "scroll, eval, goto, size)")
                cdp.pump(0.15)
            except Exception as ex:                          # an action failing is a result
                res = f"FAILED: {ex}"
            steps.append(f"{raw.strip()}: {res}")
        cdp.pump(0.2)
        title = cdp.evaluate("document.title") or ""
        text = cdp.evaluate(text_js or "document.body ? document.body.innerText : ''") or ""
        shot = None
        if shot_path:
            data = cdp.call("Page.captureScreenshot", {"format": "png"}, timeout=30)["data"]
            with open(shot_path, "wb") as fh:
                fh.write(base64.b64decode(data))
            shot = shot_path
        errors, warnings, logs, failed = _collect(cdp.events)
        return {"url": url, "title": title, "text": text, "errors": errors,
                "warnings": warnings, "logs": logs, "failed": failed, "steps": steps,
                "shot": shot, "ms": int((time.time() - t0) * 1000), "size": size}
    finally:
        if cdp is not None:
            cdp.ws.close()
        if proc is not None:
            try:
                os.killpg(proc.pid, 9)
            except OSError:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        shutil.rmtree(profile, ignore_errors=True)


# ── Playwright backend (Camoufox: a Firefox build for undetectable browsing) ──
# Camoufox doesn't speak the Chromium DevTools protocol; it is driven through
# Playwright (which its Python package wraps). The page API is the same for
# any Playwright browser, so this path is exercised with any of them; only
# the launcher is Camoufox-specific.
def _camoufox_launcher(executable=None):
    from contextlib import contextmanager

    @contextmanager
    def launch():
        from camoufox.sync_api import Camoufox
        kw = {"headless": True}
        if executable:
            kw["executable_path"] = executable
        with Camoufox(**kw) as browser:
            yield browser
    return launch


_PW_KEYS = {"enter": "Enter", "tab": "Tab", "escape": "Escape", "esc": "Escape",
            "space": "Space", "backspace": "Backspace", "delete": "Delete",
            "arrowleft": "ArrowLeft", "arrowup": "ArrowUp", "arrowright": "ArrowRight",
            "arrowdown": "ArrowDown", "left": "ArrowLeft", "up": "ArrowUp",
            "right": "ArrowRight", "down": "ArrowDown", "home": "Home", "end": "End",
            "pageup": "PageUp", "pagedown": "PageDown"}
_STACK_LOC = re.compile(r"((?:file|https?)://[^\s)]+?):(\d+):(\d+)")


def _pw_where(url, line):
    name = (url or "").rsplit("/", 1)[-1]
    return f" ({name}:{line})" if name and line else (f" ({name})" if name else "")


def browse_playwright(target, actions, size, root, shot_path, settle, timeout, launcher,
                      text_js=None):
    url = to_url(target, root)
    t0 = time.time()
    errors, warnings, logs, failed, steps = [], [], [], [], []
    with launcher() as browser:
        w, h = size
        page = browser.new_page(viewport={"width": w, "height": h})

        def on_console(m):
            loc = m.location or {}
            where = _pw_where(loc.get("url"), (loc.get("lineNumber") or 0) + 1
                              if loc.get("url") else None)
            if m.type in ("error", "assert"):
                if m.text.startswith("Failed to load resource"):
                    return                       # reported once, under failed requests
                errors.append(f"console.error: {m.text}{where}")
            elif m.type == "warning":
                warnings.append(f"console.warn: {m.text}{where}")
            else:
                logs.append(m.text)

        def on_error(e):
            msg = getattr(e, "message", None) or str(e)
            name = getattr(e, "name", "") or ""
            hit = _STACK_LOC.search(getattr(e, "stack", "") or "")
            where = _pw_where(hit.group(1), hit.group(2)) if hit else ""
            if name and not msg.startswith(name):
                msg = f"{name}: {msg}"
            errors.append(f"Uncaught {msg.splitlines()[0]}{where}")

        def on_failed(req):
            f = req.failure
            failed.append(f"{req.url}: {f if isinstance(f, str) else (f or {}).get('errorText', f)}")

        def on_response(resp):
            if resp.status >= 400:
                failed.append(f"{resp.url}: HTTP {resp.status}")
        page.on("console", on_console)
        page.on("pageerror", on_error)
        page.on("requestfailed", on_failed)
        page.on("response", on_response)
        try:
            page.goto(url, wait_until="load", timeout=20000)
        except Exception as ex:
            errors.append(f"navigation: {str(ex).splitlines()[0]}")
        page.wait_for_timeout(int(settle * 1000))
        for raw in actions:
            if time.time() - t0 > timeout:
                steps.append(f"{raw}: skipped (time limit)")
                continue
            verb, _, arg = raw.strip().partition(" ")
            verb, arg = verb.lower(), arg.strip()
            try:
                if verb == "click":
                    m = re.fullmatch(r"\s*(\d+)\s*[, ]\s*(\d+)\s*", arg)
                    if m:
                        page.mouse.click(int(m.group(1)), int(m.group(2)))
                    else:
                        if page.query_selector(arg) is None:
                            raise ValueError(f"no element matches {arg!r}")
                        page.click(arg, timeout=5000)
                    res = "ok"
                elif verb == "type":
                    page.keyboard.type(arg); res = "ok"
                elif verb in ("press", "key"):
                    for k in arg.split():
                        page.keyboard.press(_PW_KEYS.get(k.lower(), k))
                    res = "ok"
                elif verb == "wait":
                    page.wait_for_timeout(min(10000, int(float(arg or 500)))); res = "ok"
                elif verb == "unblock":                      # read_page: outwait a bot check
                    end = time.time() + min(20.0, float(arg or 10000) / 1000.0)
                    while time.time() < end and challenged(page.evaluate(_CHALLENGE_PROBE) or ""):
                        page.wait_for_timeout(500)
                    res = "ok"
                elif verb == "scroll":
                    page.mouse.wheel(0, int(float(arg or 600))); res = "ok"
                elif verb in ("eval", "js"):
                    v = page.evaluate(arg)
                    res = "→ " + (json.dumps(v)[:1500] if v is not None else "undefined")
                elif verb in ("goto", "open"):
                    page.goto(to_url(arg, root), wait_until="load", timeout=20000); res = "ok"
                elif verb == "size":
                    m = re.fullmatch(r"(\d+)\s*[x×]\s*(\d+)", arg)
                    if not m:
                        raise ValueError("size wants WIDTHxHEIGHT")
                    page.set_viewport_size({"width": int(m.group(1)), "height": int(m.group(2))})
                    res = "ok"
                else:
                    raise ValueError(f"unknown action {verb!r} (click, type, press, wait, "
                                     "scroll, eval, goto, size)")
                page.wait_for_timeout(150)
            except Exception as ex:
                res = f"FAILED: {str(ex).splitlines()[0]}"
            steps.append(f"{raw.strip()}: {res}")
        page.wait_for_timeout(200)
        title = page.title() or ""
        text = page.evaluate(text_js or "document.body ? document.body.innerText : ''") or ""
        shot = None
        if shot_path:
            page.screenshot(path=shot_path)
            shot = shot_path
    dedup = lambda xs: list(dict.fromkeys(xs))       # noqa: E731
    return {"url": url, "title": title, "text": text, "errors": dedup(errors),
            "warnings": dedup(warnings), "logs": logs, "failed": dedup(failed),
            "steps": steps, "shot": shot, "ms": int((time.time() - t0) * 1000), "size": size}


def report(r, text_cap=1500):
    """The result as feedback for the model."""
    w, h = r["size"]
    eng = f", {r['engine']}" if r.get("engine") else ""
    out = [f"BROWSE {r['url']} ({w}x{h}, {r['ms']} ms{eng}) — title: {r['title'] or '(none)'}"]
    out.append(f"JS errors ({len(r['errors'])}):" + ("\n  " + "\n  ".join(r["errors"][:20])
                                                    if r["errors"] else " none"))
    if r["failed"]:
        out.append(f"Failed requests ({len(r['failed'])}):\n  " + "\n  ".join(r["failed"][:15]))
    if r["warnings"]:
        out.append(f"Warnings ({len(r['warnings'])}):\n  " + "\n  ".join(r["warnings"][:10]))
    if r["logs"]:
        out.append("Console log (last 10):\n  " + "\n  ".join(x[:300] for x in r["logs"][-10:]))
    if r["steps"]:
        out.append("Actions:\n  " + "\n  ".join(r["steps"]))
    t = (r["text"] or "").strip()
    out.append("Visible text: " + ((t[:text_cap] + (" …" if len(t) > text_cap else ""))
                                   if t else "(none — a canvas page, or nothing rendered)"))
    return "\n".join(out)
