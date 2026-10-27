# Chuck's bridge to Camoufox (a Firefox build sites can't tell from a person).
#
# Camoufox is launched by its own Python package, which builds a consistent
# fingerprint for it, and driven through Playwright. Chuck runs this script
# with that package's Python and talks to it over stdin/stdout, one JSON
# object per line:
#
#   Chuck  -> bridge   the request: {"url", "actions", "width", "height", ...}
#   bridge -> Chuck    {"judge": url, "id": n}   may the page load this?
#   Chuck  -> bridge   {"id": n, "ok": true|false}
#   bridge -> Chuck    {"result": {...}} or {"error": "..."}, then it exits
#
# Every request the page makes (and every WebSocket) is judged by Chuck, the
# same rules as for Chromium, unless the request says nothing needs judging.
# Nothing here decides what is allowed.
import base64
import json
import os
import re
import sys
import time

# Libraries print; the protocol owns the real stdout. Anything else written
# to fd 1 goes to stderr instead.
_OUT = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr


def send(obj):
    _OUT.write(json.dumps(obj) + "\n")
    _OUT.flush()


def recv():
    line = sys.stdin.readline()
    if not line:                      # Chuck went away: so do we
        os._exit(0)
    return json.loads(line)


KEYS = {"enter": "Enter", "return": "Enter", "tab": "Tab", "escape": "Escape", "esc": "Escape",
        "space": "Space", "backspace": "Backspace", "delete": "Delete", "del": "Delete",
        "arrowleft": "ArrowLeft", "arrowup": "ArrowUp", "arrowright": "ArrowRight",
        "arrowdown": "ArrowDown", "left": "ArrowLeft", "up": "ArrowUp", "right": "ArrowRight",
        "down": "ArrowDown", "home": "Home", "end": "End", "pageup": "PageUp", "pagedown": "PageDown"}
STACK_LOC = re.compile(r"((?:file|https?)://[^\s)]+?):(\d+):(\d+)")


def where(url, line):
    path = (url or "").split("?")[0].split("#")[0]
    name = path.rsplit("/", 1)[-1] or ("the page" if path else "")
    if not name:
        return ""
    return f" ({name}:{line})" if line else f" ({name})"


def first_line(ex):
    return (str(ex).strip().splitlines() or [type(ex).__name__])[0]


def main():
    req = recv()
    t0 = time.time()
    deadline = t0 + req.get("timeout_ms", 60000) / 1000.0
    challenge = re.compile(req["challenge"])
    errors, warnings, logs, failed, steps = [], [], [], [], []
    state = {"status": None, "n": 0}

    def judge(url):
        state["n"] += 1
        n = state["n"]
        send({"judge": url, "id": n})
        while True:
            a = recv()
            if a.get("id") == n:
                return bool(a.get("ok"))

    from camoufox.sync_api import Camoufox

    prefs = {
        # no requests the page didn't make: link prefetch, DNS prefetch, speculative connections
        "network.dns.disablePrefetch": True,
        "network.prefetch-next": False,
        "network.http.speculative-parallel-limit": 0,
        "browser.download.useDownloadDir": False,
    }
    common = {"headless": True}
    if req.get("proxy"):
        common["proxy"] = {"server": req["proxy"]}
    if req.get("exe"):
        common["executable_path"] = req["exe"]
    private = dict(common, block_webrtc=True, firefox_user_prefs=prefs)
    # best first: with a proxy, a time zone and language that match its country (needs
    # camoufox[geoip]); then without that; then the plainest launch an old Camoufox takes
    attempts = ([dict(private, geoip=True)] if req.get("proxy") else []) + [private, common]
    launch_error = None
    for kw in attempts:
        try:
            cm = Camoufox(**kw)
            browser = cm.__enter__()
            break
        except Exception as ex:
            launch_error = ex
    else:
        raise launch_error

    try:
        ctx_kw = {"accept_downloads": False, "service_workers": "block"}
        if req.get("viewport"):
            ctx_kw["viewport"] = {"width": req["width"], "height": req["height"]}
            if req.get("scale", 1.0) < 1.0:
                ctx_kw["device_scale_factor"] = req["scale"]
        context = browser.new_context(**ctx_kw)

        if req.get("judge"):
            def on_route(route):
                try:
                    ok = judge(route.request.url)
                    if ok:
                        route.continue_()
                    else:
                        route.abort("blockedbyclient")
                except Exception:
                    pass              # the page went away meanwhile
            context.route(re.compile(r".*"), on_route)
            if hasattr(context, "route_web_socket"):
                def on_ws(ws):
                    try:
                        if judge(ws.url):
                            ws.connect_to_server()
                        else:
                            ws.close()
                    except Exception:
                        pass
                context.route_web_socket(re.compile(r".*"), on_ws)
            else:
                # no way to judge sockets in this Playwright: no sockets at all
                context.add_init_script("delete window.WebSocket; delete window.EventSource;")

        page = context.new_page()

        def on_console(m):
            loc = m.location or {}
            at = where(loc.get("url"), (loc.get("lineNumber") or 0) + 1 if loc.get("url") else None)
            if m.type in ("error", "assert"):
                if m.text.startswith("Failed to load resource"):
                    return            # reported once, under failed requests
                errors.append(f"console.error: {m.text}{at}")
            elif m.type == "warning":
                warnings.append(f"console.warn: {m.text}{at}")
            else:
                logs.append(m.text)

        def on_error(e):
            msg = getattr(e, "message", None) or str(e)
            name = getattr(e, "name", "") or ""
            hit = STACK_LOC.search(getattr(e, "stack", "") or "")
            at = where(hit.group(1), hit.group(2)) if hit else ""
            if name and not msg.startswith(name):
                msg = f"{name}: {msg}"
            errors.append(f"Uncaught {(msg.splitlines() or [''])[0]}{at}")

        def on_failed(r):
            f = r.failure
            failed.append(f"{r.url}: {f if isinstance(f, str) else (f or {}).get('errorText', f)}")

        def on_response(r):
            try:
                if r.request.resource_type == "document" and r.frame == page.main_frame:
                    state["status"] = r.status
            except Exception:
                pass
            path = r.url.split("?")[0].split("#")[0]
            if r.status >= 400 and not path.endswith("/favicon.ico"):
                failed.append(f"{r.url}: HTTP {r.status}")

        page.on("console", on_console)
        page.on("pageerror", on_error)
        page.on("requestfailed", on_failed)
        page.on("response", on_response)

        nav_error = None
        try:
            page.goto(req["url"], wait_until="load", timeout=20000)
        except Exception as ex:
            nav_error = first_line(ex)
        page.wait_for_timeout(int(req.get("settle_ms", 600)))

        def probe():
            return page.evaluate(req["probe"]) or ""

        for a in req.get("actions", []):
            raw, verb, arg = a["raw"], a["verb"], a["arg"]
            if time.time() > deadline:
                steps.append(f"{raw}: skipped (time limit)")
                continue
            try:
                if a.get("error"):
                    raise ValueError(a["error"])
                if verb == "click":
                    m = re.fullmatch(r"\s*(\d+)\s*[, ]\s*(\d+)\s*", arg)
                    if m:
                        page.mouse.click(int(m.group(1)), int(m.group(2)))
                    else:
                        if page.query_selector(arg) is None:
                            raise ValueError(f"no element matches `{arg}`")
                        page.click(arg, timeout=5000)
                    res = "ok"
                elif verb == "type":
                    page.keyboard.type(arg)
                    res = "ok"
                elif verb in ("press", "key"):
                    for k in arg.split():
                        page.keyboard.press(KEYS.get(k.lower(), k))
                    res = "ok"
                elif verb == "wait":
                    page.wait_for_timeout(max(0, min(10000, int(float(arg or 500)))))
                    res = "ok"
                elif verb == "unblock":       # wait out a bot check
                    end = time.time() + max(0.0, min(20.0, float(arg or 10000) / 1000.0))
                    while time.time() < min(end, deadline) and challenge.search(probe()):
                        page.wait_for_timeout(500)
                    res = "ok"
                elif verb == "scroll":
                    page.mouse.wheel(0, int(float(arg or 600)))
                    res = "ok"
                elif verb in ("eval", "js"):
                    v = page.evaluate(arg)
                    res = "→ " + (json.dumps(v)[:1500] if v is not None else "undefined")
                elif verb in ("goto", "open"):
                    page.goto(arg, wait_until="load", timeout=20000)
                    res = "ok"
                elif verb == "size":
                    page.set_viewport_size({"width": a["width"], "height": a["height"]})
                    res = "ok"
                else:
                    raise ValueError(f"unknown action `{verb}` (click, type, press, wait, scroll, eval, goto, size)")
                page.wait_for_timeout(150)
            except Exception as ex:   # an action failing is part of the report
                res = f"FAILED: {first_line(ex)}"
            steps.append(f"{raw}: {res}")

        page.wait_for_timeout(200)

        def safe(f, default=""):
            try:
                return f()
            except Exception:
                return default

        title = safe(page.title)
        final_url = safe(lambda: page.url, req["url"])
        text = safe(lambda: page.evaluate(req["text_js"]) or "")
        shot = None
        if req.get("screenshot"):
            png = safe(lambda: page.screenshot(type="png", timeout=30000), None)
            shot = base64.b64encode(png).decode() if png else None
        send({"result": {"title": title, "final_url": final_url, "text": text if isinstance(text, str) else str(text),
                         "errors": errors, "warnings": warnings, "logs": logs, "failed": failed,
                         "steps": steps, "shot": shot, "status": state["status"], "nav_error": nav_error,
                         "ms": int((time.time() - t0) * 1000)}})
    finally:
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as ex:       # noqa: BLE001 — whatever it is, Chuck hears it
        msg = first_line(ex)
        if "not installed" in msg.lower() or "camoufox fetch" in msg.lower():
            msg += " (its browser hasn't been downloaded: run `python -m camoufox fetch` with Camoufox's Python)"
        send({"error": f"{type(ex).__name__}: {msg}"})
