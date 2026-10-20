"""A local site that behaves like the real web's awkward pages, for the
stealth-fetch tests: a bot wall that clears once JavaScript runs, a page that
is empty without JavaScript, one that refuses anything that isn't a browser,
and a wall that never clears."""
import http.server
import socketserver
import threading

ARTICLE = ("<article><h1>Real article</h1><p>" + "The answer is forty-two. " * 30
           + "</p></article>")
WALL = """<!doctype html><html><head><title>Just a moment...</title></head>
<body><div id="cf-browser-verification">Checking your browser before accessing the site.</div>
<script>setTimeout(function(){document.title='Readable page';
document.body.innerHTML=%r;}, 1500)</script></body></html>""" % ARTICLE
STUCK = """<!doctype html><html><head><title>Just a moment...</title></head>
<body><div class="challenge-platform">Verify you are human</div></body></html>"""
SHELL = """<!doctype html><html><head><title>App</title></head><body><div id="root"></div>
<noscript>You need to enable JavaScript to run this app.</noscript>
<script>document.getElementById('root').innerHTML = %r;</script></body></html>""" % ARTICLE
PLAIN = "<html><head><title>Plain</title></head><body>" + ARTICLE + "</body></html>"


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        status, body = 200, PLAIN
        if path == "/wall":
            body = WALL
        elif path == "/stuck":
            status, body = 403, STUCK
        elif path == "/shell":
            body = SHELL
        elif path == "/browsers-only":
            if "Headless" in self.headers.get("User-Agent", "") or not self.headers.get("Sec-Fetch-Mode"):
                status, body = 403, "<html><title>Access denied</title><body>Forbidden</body></html>"
        elif path == "/missing":
            status, body = 404, "<html><body>Not here</body></html>"
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="stealth-site").start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"
