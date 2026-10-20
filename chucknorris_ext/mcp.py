"""mcp.py — a Model Context Protocol client (stdio transport), stdlib only.

MCP is how the agent ecosystem ships tools: GitHub, databases, Figma, Sentry,
browsers, filesystems… each as a small server speaking JSON-RPC over stdin and
stdout. Chuck reads the same config file format Claude Desktop uses:

    ~/.config/chucknorris/mcp.json
    {"mcpServers": {"github": {"command": "npx",
                               "args": ["-y", "@modelcontextprotocol/server-github"],
                               "env": {"GITHUB_TOKEN": "..."}}}}

so any server's documented snippet can be pasted in as-is. Servers start in
the background, their tools are listed to the model, and the model calls one
with a ```mcp server.tool``` block whose body is the JSON arguments.

Protocol coverage: initialize / initialized, tools/list (paginated),
tools/call (text, image and resource content, isError), server-initiated
ping and roots/list; anything else a server asks for is answered with
"method not found" rather than left hanging.
"""
import json
import os
import shutil
import subprocess
import threading
import time

PROTOCOL = "2025-06-18"
CALL_TIMEOUT = 120
START_TIMEOUT = 45


class MCPError(RuntimeError):
    pass


class Server:
    """One stdio MCP server: a process plus a reader thread."""

    def __init__(self, name, spec, client_version="15", roots=None):
        self.name, self.spec = name, spec
        self.client_version = client_version
        self.roots = roots or (lambda: [])
        self.proc = None
        self.tools = []
        self.info = {}
        self.error = ""
        self.stderr_tail = []
        self._lock = threading.Lock()
        self._next = 0
        self._waiting = {}                      # id -> [Event, response]
        self._write_lock = threading.Lock()

    # ── lifecycle ──
    def start(self):
        cmd = self.spec.get("command")
        if not cmd:
            raise MCPError("no command configured")
        exe = shutil.which(cmd) or (cmd if os.path.isfile(cmd) else None)
        if not exe:
            raise MCPError(f"command not found: {cmd}")
        env = dict(os.environ)
        env.update({k: str(v) for k, v in (self.spec.get("env") or {}).items()})
        self.proc = subprocess.Popen([exe] + [str(a) for a in self.spec.get("args") or []],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=env,
                                     cwd=self.spec.get("cwd") or None,
                                     start_new_session=True)
        # long-lived by design (they live as long as the server): named, so
        # anything waiting for "work to finish" can tell them apart
        threading.Thread(target=self._read_loop, daemon=True,
                         name=f"mcp-read-{self.name}").start()
        threading.Thread(target=self._err_loop, daemon=True,
                         name=f"mcp-err-{self.name}").start()
        res = self.request("initialize", {
            "protocolVersion": PROTOCOL, "capabilities": {"roots": {"listChanged": False}},
            "clientInfo": {"name": "chuck", "version": self.client_version}},
            timeout=START_TIMEOUT)
        self.info = res.get("serverInfo") or {}
        self.notify("notifications/initialized")
        self.tools = self._list_tools()
        return self

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.proc.pid, 9)
            except OSError:
                self.proc.kill()
        with self._lock:
            for ev in self._waiting.values():
                ev[1] = {"error": {"message": "server stopped"}}
                ev[0].set()
            self._waiting.clear()

    # ── JSON-RPC ──
    def _send(self, msg):
        data = (json.dumps(msg) + "\n").encode()
        with self._write_lock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError) as ex:
                raise MCPError(f"{self.name}: server is gone ({ex})")

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def request(self, method, params=None, timeout=CALL_TIMEOUT):
        if not self.alive():
            raise MCPError(f"{self.name}: server is not running"
                           + (f" — {self.stderr_tail[-1]}" if self.stderr_tail else ""))
        with self._lock:
            self._next += 1
            mid = self._next
            slot = [threading.Event(), None]
            self._waiting[mid] = slot
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
        if not slot[0].wait(timeout):
            with self._lock:
                self._waiting.pop(mid, None)
            raise MCPError(f"{self.name}: {method} timed out after {timeout}s")
        resp = slot[1] or {}
        if "error" in resp:
            e = resp["error"] or {}
            raise MCPError(f"{self.name}: {e.get('message', 'error')}"
                           + (f" ({e.get('code')})" if e.get("code") is not None else ""))
        return resp.get("result") or {}

    def _read_loop(self):
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue                         # a server logging to stdout: ignore it
            if not isinstance(msg, dict):
                continue
            if "method" in msg and "id" in msg:      # the server asks US something
                self._answer(msg)
            elif "id" in msg:
                with self._lock:
                    slot = self._waiting.pop(msg["id"], None)
                if slot:
                    slot[1] = msg
                    slot[0].set()
        with self._lock:                          # stdout closed: wake every waiter
            for slot in self._waiting.values():
                slot[1] = {"error": {"message": "server exited"
                                     + (f": {self.stderr_tail[-1]}" if self.stderr_tail else "")}}
                slot[0].set()
            self._waiting.clear()

    def _err_loop(self):
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.stderr_tail = (self.stderr_tail + [line])[-20:]

    def _answer(self, msg):
        m = msg.get("method")
        if m == "ping":
            res = {"result": {}}
        elif m == "roots/list":
            res = {"result": {"roots": [{"uri": "file://" + r, "name": os.path.basename(r) or r}
                                        for r in self.roots()]}}
        else:
            res = {"error": {"code": -32601, "message": f"Chuck doesn't support {m}"}}
        try:
            self._send({"jsonrpc": "2.0", "id": msg["id"], **res})
        except MCPError:
            pass

    def _list_tools(self):
        tools, cursor = [], None
        for _ in range(20):                       # pages
            r = self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=30)
            tools += r.get("tools") or []
            cursor = r.get("nextCursor")
            if not cursor:
                break
        return tools

    def call(self, tool, args, timeout=CALL_TIMEOUT):
        return self.request("tools/call", {"name": tool, "arguments": args or {}}, timeout=timeout)


def load_config(path):
    """{name: spec} from a Claude-Desktop-style mcp.json ('mcpServers'), or {}."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as ex:
        raise MCPError(f"{path}: {ex}")
    servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
    return {k: v for k, v in servers.items()
            if isinstance(v, dict) and v.get("command") and not v.get("disabled")}


def _sig(tool):
    """'name(req, opt?)' from a tool's inputSchema."""
    sch = tool.get("inputSchema") or {}
    props = sch.get("properties") or {}
    req = set(sch.get("required") or [])
    args = [p if p in req else p + "?" for p in props]
    return f"{tool.get('name')}({', '.join(args)})"


class Manager:
    """All configured servers. Starts them in the background; never blocks the UI."""

    def __init__(self, config_path, client_version="15", roots=None, on_change=None):
        self.path = config_path
        self.client_version = client_version
        self.roots = roots
        self.on_change = on_change
        self.servers = {}
        self.errors = {}
        self.lock = threading.Lock()
        self.loading = False

    def start_all(self, wait=False):
        try:
            cfg = load_config(self.path)
        except MCPError as ex:
            self.errors = {"mcp.json": str(ex)}
            cfg = {}
        self.stop_all()
        if not cfg:
            return

        def boot(name, spec):
            s = Server(name, spec, self.client_version, self.roots)
            try:
                s.start()
                with self.lock:
                    self.servers[name] = s
                    self.errors.pop(name, None)
            except Exception as ex:
                s.stop()
                with self.lock:
                    self.errors[name] = str(ex)
            if self.on_change:
                self.on_change()

        self.loading = True
        threads = [threading.Thread(target=boot, args=(n, sp), daemon=True)
                   for n, sp in cfg.items()]
        for t in threads:
            t.start()

        def done():
            for t in threads:
                t.join()
            self.loading = False
            if self.on_change:
                self.on_change()
        if wait:
            done()
        else:
            threading.Thread(target=done, daemon=True).start()

    def stop_all(self):
        with self.lock:
            servers, self.servers = list(self.servers.values()), {}
        for s in servers:
            s.stop()

    def index(self, desc_cap=110, max_tools=80):
        """The tool list for the system prompt ('' when there are none)."""
        lines = []
        with self.lock:
            servers = sorted(self.servers.items())
        for name, s in servers:
            for t in s.tools:
                if len(lines) >= max_tools:
                    break
                d = " ".join((t.get("description") or "").split())
                if len(d) > desc_cap:
                    d = d[:desc_cap - 1] + "…"
                lines.append(f"- {name}.{_sig(t)}" + (f": {d}" if d else ""))
        if not lines:
            return ""
        return ("MCP TOOLS — call one with ```mcp <server>.<tool>\n{json arguments}``` "
                "(? = optional):\n" + "\n".join(lines))

    def call(self, target, args, timeout=CALL_TIMEOUT):
        """-> (ok, text, images[(mime, b64)])"""
        server, _, tool = (target or "").strip().partition(".")
        with self.lock:
            s = self.servers.get(server)
        if s is None:
            known = ", ".join(sorted(self.servers)) or "none running"
            err = self.errors.get(server)
            return False, (f"no MCP server '{server}'" + (f" (it failed to start: {err})" if err
                                                           else f" — servers: {known}")), []
        spec = next((t for t in s.tools if t.get("name") == tool), None)
        if spec is None:
            return False, (f"{server} has no tool '{tool}' — it has: "
                           + ", ".join(t.get("name", "?") for t in s.tools)), []
        missing = [r for r in (spec.get("inputSchema") or {}).get("required") or []
                   if r not in (args or {})]
        if missing:
            return False, f"{server}.{_sig(spec)} is missing: {', '.join(missing)}", []
        try:
            r = s.call(tool, args, timeout=timeout)
        except MCPError as ex:
            return False, str(ex), []
        texts, images = [], []
        for c in r.get("content") or []:
            kind = c.get("type")
            if kind == "text":
                texts.append(c.get("text", ""))
            elif kind == "image" and c.get("data"):
                images.append((c.get("mimeType") or "image/png", c["data"]))
            elif kind == "resource":
                res = c.get("resource") or {}
                texts.append(res.get("text") or f"[resource {res.get('uri', '')}]")
            elif kind == "resource_link":
                texts.append(f"[resource {c.get('uri', '')}] {c.get('name', '')}")
        if r.get("structuredContent") is not None and not texts:
            texts.append(json.dumps(r["structuredContent"], indent=1)[:20000])
        return (not r.get("isError")), "\n".join(texts), images

    def status(self):
        """[(name, state, detail)] for the Settings list."""
        try:
            cfg = load_config(self.path)
        except MCPError as ex:
            return [("mcp.json", "error", str(ex))]
        out = []
        with self.lock:
            for name in cfg:
                s = self.servers.get(name)
                if s is not None and s.alive():
                    v = s.info.get("version")
                    out.append((name, "running", f"{len(s.tools)} tools"
                                + (f" · v{v}" if v else "")))
                elif name in self.errors:
                    out.append((name, "failed", self.errors[name]))
                else:
                    out.append((name, "starting" if self.loading else "stopped", ""))
        return out
