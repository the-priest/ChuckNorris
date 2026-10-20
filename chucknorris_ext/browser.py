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
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

_CANDIDATES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
               "chrome", "brave", "brave-browser", "microsoft-edge", "vivaldi")


def find_chrome():
    """The first Chromium-family browser on this machine, or None."""
    env = os.environ.get("CHROME_BIN") or os.environ.get("CHUCK_CHROME")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    for name in _CANDIDATES:
        p = shutil.which(name)
        if p:
            return p
    for p in ("/opt/pw-browsers/chromium", "/usr/lib/chromium/chromium",
              "/snap/bin/chromium", "/opt/google/chrome/chrome"):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


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


def _launch(chrome, profile):
    args = [chrome, "--headless=new", "--remote-debugging-port=0",
            f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
            "--disable-gpu", "--hide-scrollbars", "--mute-audio",
            "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
            "--allow-file-access-from-files", "about:blank"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args.insert(1, "--no-sandbox")            # Chromium refuses to run as root otherwise
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, start_new_session=True)
    end = time.time() + 20
    line = b""
    while time.time() < end:
        line = proc.stderr.readline()
        if not line and proc.poll() is not None:
            raise RuntimeError("the browser exited at start")
        m = re.search(rb"DevTools listening on (ws://\S+)", line)
        if m:
            return proc, m.group(1).decode()
    proc.kill()
    raise RuntimeError("the browser didn't start within 20s")


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


def to_url(target, root=None):
    """A URL from what the agent wrote: http(s)/file URLs pass through, a bare
    host:port gets http://, anything else is a file path (relative to root)."""
    t = (target or "").strip().strip("'\"")
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
    return "file://" + urllib.parse.quote(os.path.abspath(p))


def browse(target, actions=(), size=(1280, 800), root=None, shot_path=None,
           settle=0.6, chrome=None, timeout=60):
    """Load target, run actions, report. -> dict(url, title, text, errors,
    warnings, logs, failed, steps, shot, ms). Raises RuntimeError when no
    browser is available or it can't start."""
    chrome = chrome or find_chrome()
    if not chrome:
        raise RuntimeError("no Chromium-family browser found (install chromium, or set "
                           "CHROME_BIN)")
    url = to_url(target, root)
    t0 = time.time()
    profile = tempfile.mkdtemp(prefix="chuck_browse_")
    proc = cdp = None
    try:
        proc, bws = _launch(chrome, profile)
        cdp = _CDP(_page_ws(bws))
        for dom in ("Runtime", "Log", "Page", "Network"):
            cdp.call(f"{dom}.enable")
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
        text = cdp.evaluate("document.body ? document.body.innerText : ''") or ""
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


def report(r, text_cap=1500):
    """The result as feedback for the model."""
    w, h = r["size"]
    out = [f"BROWSE {r['url']} ({w}x{h}, {r['ms']} ms) — title: {r['title'] or '(none)'}"]
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
