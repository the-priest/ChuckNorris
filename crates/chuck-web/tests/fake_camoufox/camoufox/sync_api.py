"""A stand-in for camoufox.sync_api, for Chuck's tests: the slice of the
Playwright API the bridge uses, behaving the way it does (route handlers
asked for every request, events for errors and responses, a browser
process in a session of its own). The site is described by the JSON file
named in FAKE_SITE_FILE; what happened is appended to FAKE_LOG."""
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

SITE = json.load(open(os.environ["FAKE_SITE_FILE"]))


def log(**kw):
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps(kw) + "\n")


class Camoufox:
    def __init__(self, **kw):
        self.kw = kw
        if SITE.get("not_installed"):
            raise FileNotFoundError("Camoufox is not installed. Please run `camoufox fetch`")
        if kw.get("geoip") and SITE.get("no_geoip"):
            raise ImportError("the geoip extra isn't installed")

    def __enter__(self):
        # Playwright starts the browser in a session of its own
        self.proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
        log(launch={k: v for k, v in self.kw.items()}, browser_pid=self.proc.pid)
        return Browser()

    def __exit__(self, *a):
        if not SITE.get("leave_browser"):
            self.proc.kill()


class Browser:
    def new_context(self, **kw):
        log(context=kw)
        return Context()


class Route:
    def __init__(self, url):
        self.request = Request(url, "document")
        self.result = None

    def continue_(self):
        self.result = "continue"

    def abort(self, code="failed"):
        self.result = "abort:" + code


class Request:
    def __init__(self, url, kind):
        self.url = url
        self.resource_type = kind
        self.failure = None


class Response:
    def __init__(self, url, status, kind, frame):
        self.url = url
        self.status = status
        self.request = Request(url, kind)
        self.frame = frame


class Msg:
    def __init__(self, type_, text, url=None, line=0):
        self.type = type_
        self.text = text
        self.location = {"url": url, "lineNumber": line} if url else {}


class Err:
    def __init__(self, message, stack=""):
        self.message = message
        self.name = "Error"
        self.stack = stack


class Context:
    def __init__(self):
        self.routes = []
        self.scripts = []

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def route_web_socket(self, pattern, handler):
        pass

    def add_init_script(self, s):
        self.scripts.append(s)

    def new_page(self):
        return Page(self)


class Mouse:
    def __init__(self, page):
        self.page = page

    def click(self, x, y):
        self.page.did.append(f"click {x},{y}")

    def wheel(self, dx, dy):
        self.page.did.append(f"wheel {dy}")


class Keyboard:
    def __init__(self, page):
        self.page = page

    def type(self, t):
        self.page.did.append(f"type {t}")

    def press(self, k):
        self.page.did.append(f"press {k}")


class Page:
    def __init__(self, ctx):
        self.ctx = ctx
        self.handlers = {}
        self.main_frame = object()
        self.url = "about:blank"
        self.html = ""
        self.did = []
        self.mouse = Mouse(self)
        self.keyboard = Keyboard(self)

    def on(self, ev, fn):
        self.handlers.setdefault(ev, []).append(fn)

    def emit(self, ev, *a):
        for fn in self.handlers.get(ev, []):
            fn(*a)

    def allowed(self, url):
        """What the routes say, as Firefox would ask them."""
        if not url.startswith(("http://", "https://")):
            return True
        for _, h in self.ctx.routes:
            r = Route(url)
            h(r)
            return r.result == "continue"
        return True

    def fetch(self, url):
        if url.startswith("http://127.0.0.1"):
            try:
                with urllib.request.urlopen(url, timeout=5) as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, ""
        return 200, SITE.get("remote", {}).get(url, "<title>Remote</title><p>remote page</p>")

    def goto(self, url, wait_until="load", timeout=30000):
        if SITE.get("hang"):
            time.sleep(300)
        if not self.allowed(url):
            raise Exception("net::ERR_BLOCKED_BY_CLIENT at " + url)
        status, html = self.fetch(url)
        self.url, self.html = url, html
        self.emit("response", Response(url, status, "document", self.main_frame))
        for src in re.findall(r'src="([^"]+)"', html):
            u = urllib.parse.urljoin(url, src)
            if u.startswith("file:"):
                r = Request(u, "script")
                r.failure = "NS_ERROR_DOM_BAD_URI"
                self.emit("requestfailed", r)
            elif not self.allowed(u):
                r = Request(u, "script")
                r.failure = "NS_ERROR_FAILURE"
                self.emit("requestfailed", r)
            else:
                st, _ = self.fetch(u)
                self.emit("response", Response(u, st, "script", self.main_frame))
        for m in re.findall(r"<!-- error: (.*?) -->", html):
            self.emit("pageerror", Err(m, f"@{url}:3:5"))
        for m in re.findall(r"<!-- console: (.*?) -->", html):
            self.emit("console", Msg("error", m, url, 6))
        return None

    def wait_for_timeout(self, ms):
        pass

    def title(self):
        m = re.search(r"<title>(.*?)</title>", self.html)
        return m.group(1) if m else ""

    def text(self):
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>|<!--.*?-->", " ", re.sub(r"<title>.*?</title>", "", self.html))).strip()

    def evaluate(self, expr):
        self.did.append("eval " + expr[:30])
        if "innerText" in expr and "document.title" in expr:
            return self.title() + "\n" + self.text()
        if "innerText" in expr:
            return self.text()
        return SITE.get("eval", {}).get(expr)

    def query_selector(self, sel):
        return object() if sel.lstrip("#") in self.html else None

    def click(self, sel, timeout=0):
        self.did.append("click " + sel)

    def set_viewport_size(self, s):
        self.did.append(f"size {s['width']}x{s['height']}")

    def screenshot(self, type="png", timeout=0):
        log(did=self.did)
        return b"\x89PNG\r\n\x1a\nfake"
