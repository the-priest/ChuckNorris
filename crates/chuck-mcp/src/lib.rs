//! A Model Context Protocol client over stdio.
//!
//! MCP is how the agent ecosystem ships tools (GitHub, databases, browsers,
//! issue trackers…), each a small server speaking JSON-RPC on stdin and
//! stdout. Chuck reads the config format Claude Desktop uses, from
//! `~/.config/chucknorris/mcp.json` (shared with the Python app):
//!
//! ```json
//! {"mcpServers": {"github": {"command": "npx",
//!                            "args": ["-y", "@modelcontextprotocol/server-github"],
//!                            "env": {"GITHUB_TOKEN": "..."}}}}
//! ```
//!
//! Chuck's own keys: `"disabled": true` skips a server; `"trust": true` lets
//! its tools that change things run without asking (see `Tool::needs_yes`).
//!
//! Protocol: initialize / initialized, tools/list (paginated), tools/call
//! (text, image, resource and resource-link content; structured content;
//! isError), and the server's own ping and roots/list. Anything else a server
//! asks is answered "method not found" rather than left hanging.
//!
//! Every server runs in its own session and process group, is killed if
//! Chuck dies, and is ended with everything else when Chuck quits.

use serde_json::{json, Value};
use std::collections::{BTreeMap, HashMap, VecDeque};
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{channel, Sender};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

pub const PROTOCOL: &str = "2025-06-18";
pub const CALL_TIMEOUT: Duration = Duration::from_secs(120);
pub const START_TIMEOUT: Duration = Duration::from_secs(45);
/// A server that writes a line bigger than this is cut off, not trusted with memory.
const MAX_LINE: usize = 32 << 20;
const MAX_TOOLS: usize = 500;

/// One server as configured.
#[derive(Clone, Debug, PartialEq, Default)]
pub struct Spec {
    pub command: String,
    pub args: Vec<String>,
    pub env: BTreeMap<String, String>,
    pub cwd: Option<PathBuf>,
    /// Its tools that change things may run without the user's yes.
    pub trust: bool,
}

pub fn config_path() -> PathBuf {
    chuck_store::settings::config_dir().join("mcp.json")
}

/// {name: spec} from a Claude-Desktop-style mcp.json ("mcpServers", or the
/// servers at the top level). No file: no servers.
pub fn load_config(path: &Path) -> Result<BTreeMap<String, Spec>, String> {
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(BTreeMap::new()),
        Err(e) => return Err(format!("{}: {e}", path.display())),
    };
    let v: Value = serde_json::from_str(&text).map_err(|e| format!("{}: {e}", path.display()))?;
    let servers = v.get("mcpServers").unwrap_or(&v);
    let Some(map) = servers.as_object() else { return Ok(BTreeMap::new()) };
    let mut out = BTreeMap::new();
    for (name, s) in map {
        let Some(command) = s.get("command").and_then(Value::as_str).map(str::trim).filter(|c| !c.is_empty()) else {
            continue;
        };
        if s.get("disabled").and_then(Value::as_bool) == Some(true) || !name_ok(name) {
            continue;
        }
        let strs = |k: &str| s.get(k).and_then(Value::as_array).map(|a| a.iter().map(|x| match x {
            Value::String(t) => t.clone(),
            other => other.to_string(),
        }).collect()).unwrap_or_default();
        let env = s.get("env").and_then(Value::as_object).map(|m| m.iter().map(|(k, v)| (k.clone(), match v {
            Value::String(t) => t.clone(),
            other => other.to_string(),
        })).collect()).unwrap_or_default();
        out.insert(name.clone(), Spec {
            command: command.to_string(),
            args: strs("args"),
            env,
            cwd: s.get("cwd").and_then(Value::as_str).filter(|c| !c.is_empty()).map(PathBuf::from),
            trust: s.get("trust").and_then(Value::as_bool) == Some(true),
        });
    }
    Ok(out)
}

fn name_ok(n: &str) -> bool {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    R.get_or_init(|| regex::Regex::new(r"^[A-Za-z0-9_.:-]{1,64}$").unwrap()).is_match(n)
}

/// A server's tool, made safe to put in a prompt.
#[derive(Clone, Debug, PartialEq)]
pub struct Tool {
    pub name: String,
    pub description: String,
    /// Argument names in order, and which are required.
    pub props: Vec<String>,
    pub required: Vec<String>,
    /// The server says it only reads (annotations.readOnlyHint).
    pub read_only: bool,
    /// It may destroy or overwrite things. The protocol's default when a
    /// tool doesn't say: true.
    pub destructive: bool,
}

impl Tool {
    /// From a tools/list entry; None for a malformed one (it is dropped,
    /// not allowed to break every prompt).
    pub fn from_json(t: &Value) -> Option<Tool> {
        let name = t.get("name")?.as_str()?.trim();
        if !name_ok(name) {
            return None;
        }
        let description = t.get("description").and_then(Value::as_str).unwrap_or("").split_whitespace()
            .collect::<Vec<_>>().join(" ").chars().take(400).collect();
        let schema = t.get("inputSchema").cloned().unwrap_or(Value::Null);
        let props: Vec<String> = schema.get("properties").and_then(Value::as_object)
            .map(|m| m.keys().filter(|k| name_ok(k)).take(30).cloned().collect()).unwrap_or_default();
        let required = schema.get("required").and_then(Value::as_array)
            .map(|a| a.iter().filter_map(Value::as_str).filter(|r| props.iter().any(|p| p == r)).map(String::from).collect())
            .unwrap_or_default();
        let ann = t.get("annotations");
        let read_only = ann.and_then(|a| a.get("readOnlyHint")).and_then(Value::as_bool) == Some(true);
        let destructive = !read_only && ann.and_then(|a| a.get("destructiveHint")).and_then(Value::as_bool) != Some(false);
        Some(Tool { name: name.to_string(), description, props, required, read_only, destructive })
    }

    /// `name(req, opt?)`
    pub fn signature(&self) -> String {
        let args: Vec<String> = self.props.iter()
            .map(|p| if self.required.contains(p) { p.clone() } else { format!("{p}?") }).collect();
        format!("{}({})", self.name, args.join(", "))
    }

    /// Whether the user must say yes before it runs.
    pub fn needs_yes(&self, trusted: bool) -> bool {
        self.destructive && !trusted
    }
}

/// What a tool call returned.
#[derive(Clone, Debug, PartialEq, Default)]
pub struct Reply {
    pub ok: bool,
    pub text: String,
    /// (mime type, base64)
    pub images: Vec<(String, String)>,
}

type Pending = Mutex<HashMap<u64, Sender<Value>>>;

/// One running stdio server.
pub struct Server {
    pub name: String,
    pub spec: Spec,
    pub tools: Vec<Tool>,
    /// serverInfo from initialize.
    pub info: Value,
    stdin: Arc<Mutex<Option<ChildStdin>>>,
    pending: Arc<Pending>,
    next: AtomicU64,
    alive: Arc<AtomicBool>,
    stderr: Arc<Mutex<VecDeque<String>>>,
    pgid: i32,
    _guard: chuck_tools::Service,
}

/// `cmd` on PATH (or as a path).
pub fn which(cmd: &str) -> Option<PathBuf> {
    if cmd.contains('/') {
        let p = PathBuf::from(cmd);
        return p.is_file().then_some(p);
    }
    std::env::split_paths(&std::env::var_os("PATH")?).map(|d| d.join(cmd)).find(|p| {
        use std::os::unix::fs::PermissionsExt;
        p.metadata().is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
    })
}

fn send(stdin: &Mutex<Option<ChildStdin>>, msg: &Value) -> Result<(), String> {
    let mut line = serde_json::to_vec(msg).map_err(|e| e.to_string())?;
    line.push(b'\n');
    let mut g = stdin.lock().unwrap_or_else(|e| e.into_inner());
    let w = g.as_mut().ok_or("the server is stopped")?;
    w.write_all(&line).and_then(|_| w.flush()).map_err(|e| format!("the server is gone ({e})"))
}

/// One line from the server, at most MAX_LINE bytes (the rest is skipped).
fn read_line(r: &mut impl BufRead, buf: &mut Vec<u8>) -> std::io::Result<bool> {
    buf.clear();
    let n = r.by_ref().take(MAX_LINE as u64).read_until(b'\n', buf)?;
    if n == 0 {
        return Ok(false);
    }
    if buf.last() != Some(&b'\n') && n == MAX_LINE {
        let mut sink = Vec::new();
        loop {
            sink.clear();
            let k = r.by_ref().take(1 << 20).read_until(b'\n', &mut sink)?;
            if k == 0 || sink.last() == Some(&b'\n') {
                break;
            }
        }
        buf.clear(); // too big to be a message we'd act on
    }
    Ok(true)
}

impl std::fmt::Debug for Server {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        f.debug_struct("Server").field("name", &self.name).field("tools", &self.tools.len()).field("alive", &self.alive())
            .finish()
    }
}

impl Server {
    /// Start `spec` and complete the handshake. `roots`: the folders the
    /// server may be told about (roots/list).
    pub fn start(name: &str, spec: &Spec, roots: Arc<dyn Fn() -> Vec<PathBuf> + Send + Sync>) -> Result<Server, String> {
        let exe = which(&spec.command).ok_or_else(|| format!("command not found: {}", spec.command))?;
        let (tx, rx) = channel();
        let stderr: Arc<Mutex<VecDeque<String>>> = Arc::default();
        let tail = stderr.clone();
        let (args, env, cwd) = (spec.args.clone(), spec.env.clone(), spec.cwd.clone());
        // The process is started on a thread that lives exactly as long as it
        // (reading its stderr, then reaping it): the parent-death signal is tied
        // to the thread that started a process, not to the whole program.
        std::thread::Builder::new().name(format!("mcp-{name}")).spawn(move || {
            let mut c = Command::new(&exe);
            c.args(&args).envs(&env).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
            if let Some(d) = &cwd {
                c.current_dir(d);
            }
            unsafe {
                c.pre_exec(|| {
                    libc::setsid();
                    libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL);
                    Ok(())
                });
            }
            let mut child = match c.spawn() {
                Ok(ch) => ch,
                Err(e) => {
                    let _ = tx.send(Err(format!("couldn't start {}: {e}", exe.display())));
                    return;
                }
            };
            let err = child.stderr.take();
            let _ = tx.send(Ok((child.stdin.take(), child.stdout.take(), child.id() as i32)));
            if let Some(err) = err {
                for line in BufReader::new(err).lines() {
                    let Ok(line) = line else { break };
                    let line = line.trim_end().to_string();
                    if !line.is_empty() {
                        let mut t = tail.lock().unwrap_or_else(|e| e.into_inner());
                        t.push_back(line.chars().take(500).collect());
                        while t.len() > 20 {
                            t.pop_front();
                        }
                    }
                }
            }
            let _ = child.wait();
        }).map_err(|e| e.to_string())?;
        let (stdin, stdout, pid) = rx.recv_timeout(Duration::from_secs(10)).map_err(|_| "the server didn't start".to_string())??;
        let (Some(stdin), Some(stdout)) = (stdin, stdout) else { return Err("no pipes to the server".into()) };
        let stdin = Arc::new(Mutex::new(Some(stdin)));
        let pending: Arc<Pending> = Arc::default();
        let alive = Arc::new(AtomicBool::new(true));
        let (w, p, a, t) = (stdin.clone(), pending.clone(), alive.clone(), stderr.clone());
        std::thread::Builder::new().name(format!("mcp-read-{name}")).spawn(move || {
            let mut r = BufReader::new(stdout);
            let mut buf = Vec::new();
            while let Ok(true) = read_line(&mut r, &mut buf) {
                let Ok(msg) = serde_json::from_slice::<Value>(&buf) else { continue }; // a server logging to stdout
                let Some(id) = msg.get("id").filter(|i| i.is_u64() || i.is_string()).cloned() else { continue };
                if let Some(method) = msg.get("method").and_then(Value::as_str) {
                    let res = match method {
                        "ping" => json!({"result": {}}),
                        "roots/list" => json!({"result": {"roots": roots().iter().map(|r| json!({
                            "uri": format!("file://{}", r.display()),
                            "name": r.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_else(|| r.display().to_string()),
                        })).collect::<Vec<_>>()}}),
                        m => json!({"error": {"code": -32601, "message": format!("Chuck doesn't support {m}")}}),
                    };
                    let mut out = json!({"jsonrpc": "2.0", "id": id});
                    for (k, v) in res.as_object().unwrap() {
                        out[k] = v.clone();
                    }
                    let _ = send(&w, &out);
                    continue;
                }
                if let Some(n) = id.as_u64() {
                    if let Some(tx) = p.lock().unwrap_or_else(|e| e.into_inner()).remove(&n) {
                        let _ = tx.send(msg);
                    }
                }
            }
            a.store(false, Ordering::SeqCst);
            let why = t.lock().unwrap_or_else(|e| e.into_inner()).back().cloned();
            for (_, tx) in p.lock().unwrap_or_else(|e| e.into_inner()).drain() {
                let _ = tx.send(json!({"error": {"message": format!("the server exited{}",
                    why.as_ref().map(|w| format!(": {w}")).unwrap_or_default())}}));
            }
        }).map_err(|e| e.to_string())?;
        let mut s = Server { name: name.to_string(), spec: spec.clone(), tools: Vec::new(), info: Value::Null, stdin,
                             pending, next: AtomicU64::new(0), alive, stderr, pgid: pid,
                             _guard: chuck_tools::Service::register(pid) };
        let init = s.request("initialize", json!({
            "protocolVersion": PROTOCOL,
            "capabilities": {"roots": {"listChanged": false}},
            "clientInfo": {"name": "chuck", "version": env!("CARGO_PKG_VERSION")},
        }), START_TIMEOUT);
        let init = match init {
            Ok(i) => i,
            Err(e) => {
                s.stop();
                return Err(e);
            }
        };
        s.info = init.get("serverInfo").cloned().unwrap_or(Value::Null);
        s.notify("notifications/initialized")?;
        let mut cursor: Option<String> = None;
        for _ in 0..20 {
            let r = s.request("tools/list", cursor.as_ref().map_or(json!({}), |c| json!({"cursor": c})),
                              Duration::from_secs(30))?;
            s.tools.extend(r.get("tools").and_then(Value::as_array).into_iter().flatten().filter_map(Tool::from_json));
            cursor = r.get("nextCursor").and_then(Value::as_str).map(String::from);
            if cursor.is_none() || s.tools.len() >= MAX_TOOLS {
                break;
            }
        }
        s.tools.truncate(MAX_TOOLS);
        Ok(s)
    }

    pub fn alive(&self) -> bool {
        self.alive.load(Ordering::SeqCst)
    }

    /// The last thing the server wrote to stderr (why it failed, usually).
    pub fn last_error(&self) -> Option<String> {
        self.stderr.lock().unwrap_or_else(|e| e.into_inner()).back().cloned()
    }

    fn notify(&self, method: &str) -> Result<(), String> {
        send(&self.stdin, &json!({"jsonrpc": "2.0", "method": method})).map_err(|e| format!("{}: {e}", self.name))
    }

    pub fn request(&self, method: &str, params: Value, timeout: Duration) -> Result<Value, String> {
        self.request_until(method, params, timeout, &|| false)
    }

    /// A request that also ends when `stop()` says so (the user's Stop).
    pub fn request_until(&self, method: &str, params: Value, timeout: Duration, stop: &dyn Fn() -> bool)
        -> Result<Value, String> {
        if !self.alive() {
            return Err(format!("{}: the server isn't running{}", self.name,
                               self.last_error().map(|e| format!(" — {e}")).unwrap_or_default()));
        }
        let id = self.next.fetch_add(1, Ordering::SeqCst) + 1;
        let (tx, rx) = channel();
        self.pending.lock().unwrap_or_else(|e| e.into_inner()).insert(id, tx);
        if let Err(e) = send(&self.stdin, &json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params})) {
            self.pending.lock().unwrap_or_else(|e| e.into_inner()).remove(&id);
            return Err(format!("{}: {e}", self.name));
        }
        let deadline = std::time::Instant::now() + timeout;
        let resp = loop {
            let left = deadline.saturating_duration_since(std::time::Instant::now());
            let gone = |why: String| {
                self.pending.lock().unwrap_or_else(|e| e.into_inner()).remove(&id);
                // tell the server, as the protocol asks, so it can stop working on it
                let _ = send(&self.stdin, &json!({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                                  "params": {"requestId": id, "reason": why.clone()}}));
                format!("{}: {method} {why}", self.name)
            };
            if left.is_zero() {
                return Err(gone(format!("timed out after {}s", timeout.as_secs())));
            }
            if stop() {
                return Err(gone("stopped".into()));
            }
            match rx.recv_timeout(left.min(Duration::from_millis(200))) {
                Ok(r) => break r,
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => continue,
                Err(_) => return Err(gone("lost its answer".into())),
            }
        };
        if let Some(e) = resp.get("error") {
            let msg = e.get("message").and_then(Value::as_str).unwrap_or("error");
            let code = e.get("code").map(|c| format!(" ({c})")).unwrap_or_default();
            return Err(format!("{}: {msg}{code}", self.name));
        }
        Ok(resp.get("result").cloned().unwrap_or(Value::Null))
    }

    pub fn call(&self, tool: &str, args: Value, timeout: Duration) -> Result<Reply, String> {
        self.call_until(tool, args, timeout, &|| false)
    }

    pub fn call_until(&self, tool: &str, args: Value, timeout: Duration, stop: &dyn Fn() -> bool) -> Result<Reply, String> {
        let r = self.request_until("tools/call", json!({"name": tool, "arguments": args}), timeout, stop)?;
        let mut texts = Vec::new();
        let mut images = Vec::new();
        for c in r.get("content").and_then(Value::as_array).into_iter().flatten() {
            match c.get("type").and_then(Value::as_str) {
                Some("text") => texts.push(c.get("text").and_then(Value::as_str).unwrap_or("").to_string()),
                Some("image") => {
                    if let Some(d) = c.get("data").and_then(Value::as_str) {
                        images.push((c.get("mimeType").and_then(Value::as_str).unwrap_or("image/png").to_string(), d.to_string()));
                    }
                }
                Some("resource") => {
                    let res = c.get("resource").cloned().unwrap_or(Value::Null);
                    texts.push(res.get("text").and_then(Value::as_str).map(String::from).unwrap_or_else(||
                        format!("[resource {}]", res.get("uri").and_then(Value::as_str).unwrap_or(""))));
                }
                Some("resource_link") => texts.push(format!("[resource {}] {}", c.get("uri").and_then(Value::as_str).unwrap_or(""),
                                                            c.get("name").and_then(Value::as_str).unwrap_or(""))),
                _ => {}
            }
        }
        if texts.is_empty() {
            if let Some(sc) = r.get("structuredContent").filter(|v| !v.is_null()) {
                texts.push(serde_json::to_string_pretty(sc).unwrap_or_default().chars().take(20_000).collect());
            }
        }
        Ok(Reply { ok: r.get("isError").and_then(Value::as_bool) != Some(true), text: texts.join("\n"), images })
    }

    /// Close its stdin (the protocol's "please exit"), then end its group.
    pub fn stop(&self) {
        self.stdin.lock().unwrap_or_else(|e| e.into_inner()).take();
        for _ in 0..20 {
            if !self.alive() {
                break;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        unsafe {
            libc::killpg(self.pgid, libc::SIGTERM);
        }
        std::thread::sleep(Duration::from_millis(50));
        unsafe {
            libc::killpg(self.pgid, libc::SIGKILL);
        }
        for (_, tx) in self.pending.lock().unwrap_or_else(|e| e.into_inner()).drain() {
            let _ = tx.send(json!({"error": {"message": "the server was stopped"}}));
        }
    }
}

/// How a server is doing, for Settings.
#[derive(Clone, Debug, PartialEq)]
pub struct Status {
    pub name: String,
    /// "running", "failed", "starting" or "stopped".
    pub state: String,
    pub detail: String,
}

/// Every configured server.
pub struct Manager {
    path: PathBuf,
    servers: Mutex<BTreeMap<String, Arc<Server>>>,
    errors: Mutex<BTreeMap<String, String>>,
    roots: Arc<Mutex<Vec<PathBuf>>>,
    loading: AtomicBool,
    /// Bumped by every (re)start, so a server that finishes booting after a
    /// reload stands down.
    gen: AtomicU64,
}

impl Manager {
    pub fn new(path: PathBuf) -> Manager {
        Manager { path, servers: Mutex::default(), errors: Mutex::default(), roots: Arc::default(),
                  loading: AtomicBool::new(false), gen: AtomicU64::new(0) }
    }

    /// The folders servers are told about (the project, when there is one).
    pub fn set_roots(&self, roots: Vec<PathBuf>) {
        *self.roots.lock().unwrap() = roots;
    }

    pub fn configured(&self) -> bool {
        load_config(&self.path).is_ok_and(|c| !c.is_empty())
    }

    /// (Re)start every configured server, all at once; returns when each has
    /// started or failed (at most START_TIMEOUT, plus listing its tools).
    pub fn start_all(&self) {
        self.stop_all();
        let cfg = match load_config(&self.path) {
            Ok(c) => c,
            Err(e) => {
                self.errors.lock().unwrap().insert("mcp.json".into(), e);
                return;
            }
        };
        self.errors.lock().unwrap().clear();
        if cfg.is_empty() {
            return;
        }
        let gen = self.gen.fetch_add(1, Ordering::SeqCst) + 1;
        self.loading.store(true, Ordering::SeqCst);
        let roots = self.roots.clone();
        let roots: Arc<dyn Fn() -> Vec<PathBuf> + Send + Sync> = Arc::new(move || roots.lock().unwrap().clone());
        std::thread::scope(|sc| {
            for (name, spec) in &cfg {
                let roots = roots.clone();
                sc.spawn(move || {
                    let r = Server::start(name, spec, roots);
                    match r {
                        Ok(s) if self.gen.load(Ordering::SeqCst) == gen => {
                            self.servers.lock().unwrap().insert(name.clone(), Arc::new(s));
                        }
                        Ok(s) => s.stop(),
                        Err(e) => {
                            if self.gen.load(Ordering::SeqCst) == gen {
                                self.errors.lock().unwrap().insert(name.clone(), e);
                            }
                        }
                    }
                });
            }
        });
        self.loading.store(false, Ordering::SeqCst);
    }

    pub fn stop_all(&self) {
        self.gen.fetch_add(1, Ordering::SeqCst);
        let all: Vec<Arc<Server>> = std::mem::take(&mut *self.servers.lock().unwrap()).into_values().collect();
        std::thread::scope(|sc| {
            for s in &all {
                sc.spawn(move || s.stop());
            }
        });
    }

    /// The tool list for a prompt ("" when there are none). `read_only`:
    /// only the tools that say they only read.
    pub fn index(&self, read_only: bool, max_tools: usize) -> String {
        let mut lines = Vec::new();
        for (name, s) in self.servers.lock().unwrap().iter() {
            for t in s.tools.iter().filter(|t| !read_only || t.read_only) {
                if lines.len() >= max_tools {
                    break;
                }
                let mut d: String = t.description.chars().take(110).collect();
                if t.description.chars().count() > 110 {
                    d.push('…');
                }
                let mark = if t.read_only { " [reads]" } else if t.needs_yes(s.spec.trust) { " [asks the user]" } else { "" };
                lines.push(format!("- {name}.{}{mark}{}", t.signature(), if d.is_empty() { String::new() } else { format!(": {d}") }));
            }
        }
        if lines.is_empty() {
            return String::new();
        }
        format!("MCP TOOLS — call one with ```mcp <server>.<tool>``` + its JSON arguments + ``` (? = optional):\n{}",
                lines.join("\n"))
    }

    /// The server and tool a call names, with its arguments checked.
    pub fn resolve(&self, target: &str, args: &Value) -> Result<(Arc<Server>, Tool), String> {
        let (server, tool) = target.trim().split_once('.').ok_or("name the tool as server.tool")?;
        let servers = self.servers.lock().unwrap();
        let Some(s) = servers.get(server).cloned() else {
            let known = servers.keys().cloned().collect::<Vec<_>>().join(", ");
            return Err(match self.errors.lock().unwrap().get(server) {
                Some(e) => format!("the MCP server '{server}' failed to start: {e}"),
                None => format!("no MCP server '{server}' — running: {}", if known.is_empty() { "none" } else { &known }),
            });
        };
        drop(servers);
        let Some(t) = s.tools.iter().find(|t| t.name == tool).cloned() else {
            return Err(format!("{server} has no tool '{tool}' — it has: {}",
                               s.tools.iter().map(|t| t.name.as_str()).collect::<Vec<_>>().join(", ")));
        };
        let missing: Vec<&str> = t.required.iter().filter(|r| args.get(r.as_str()).is_none()).map(String::as_str).collect();
        if !missing.is_empty() {
            return Err(format!("{server}.{} is missing: {}", t.signature(), missing.join(", ")));
        }
        Ok((s, t))
    }

    pub fn status(&self) -> Vec<Status> {
        let cfg = match load_config(&self.path) {
            Ok(c) => c,
            Err(e) => return vec![Status { name: "mcp.json".into(), state: "failed".into(), detail: e }],
        };
        let servers = self.servers.lock().unwrap();
        let errors = self.errors.lock().unwrap();
        cfg.keys().map(|name| {
            let (state, detail) = match (servers.get(name), errors.get(name)) {
                (Some(s), _) if s.alive() => ("running", format!("{} tools{}", s.tools.len(),
                    s.info.get("version").and_then(Value::as_str).map(|v| format!(" · v{v}")).unwrap_or_default())),
                (Some(s), _) => ("failed", s.last_error().unwrap_or_else(|| "it exited".into())),
                (None, Some(e)) => ("failed", e.clone()),
                (None, None) if self.loading.load(Ordering::SeqCst) => ("starting", String::new()),
                (None, None) => ("stopped", String::new()),
            };
            Status { name: name.clone(), state: state.into(), detail }
        }).collect()
    }
}

impl Drop for Manager {
    fn drop(&mut self) {
        self.stop_all();
    }
}

/// "server.tool" and its JSON arguments, from a block: the target on the
/// first line and the arguments after it, or all on one line.
pub fn parse_call(arg: &str, body: &str) -> Result<(String, Value), String> {
    let (mut target, mut rest) = (arg.trim().to_string(), body.trim().to_string());
    if target.is_empty() {
        let (first, more) = rest.split_once('\n').map_or((rest.clone(), String::new()), |(a, b)| (a.to_string(), b.to_string()));
        target = first.trim().to_string();
        rest = more.trim().to_string();
    }
    if rest.is_empty() {
        if let Some((t, a)) = target.split_once(char::is_whitespace) {
            (target, rest) = (t.to_string(), a.trim().to_string());
        }
    }
    if !target.contains('.') {
        return Err("name the tool as server.tool on the first line".into());
    }
    let args: Value = if rest.is_empty() { json!({}) } else {
        serde_json::from_str(&rest).map_err(|e| format!("the arguments aren't valid JSON ({e})"))?
    };
    if !args.is_object() {
        return Err("the arguments must be a JSON object".into());
    }
    Ok((target, args))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn config_like_claude_desktop() {
        let d = std::env::temp_dir().join(format!("chuck-mcp-cfg-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let p = d.join("mcp.json");
        assert!(load_config(&p).unwrap().is_empty(), "no file: no servers");
        std::fs::write(&p, r#"{"mcpServers": {
            "gh": {"command": "npx", "args": ["-y", "server-github", 3], "env": {"TOKEN": "t", "N": 1}, "trust": true},
            "off": {"command": "x", "disabled": true},
            "nocmd": {"args": []},
            "bad name!": {"command": "x"}}}"#).unwrap();
        let c = load_config(&p).unwrap();
        assert_eq!(c.keys().collect::<Vec<_>>(), ["gh"]);
        assert_eq!(c["gh"], Spec { command: "npx".into(), args: vec!["-y".into(), "server-github".into(), "3".into()],
                                   env: [("N".to_string(), "1".to_string()), ("TOKEN".into(), "t".into())].into(), cwd: None,
                                   trust: true });
        std::fs::write(&p, "{ not json").unwrap();
        assert!(load_config(&p).is_err());
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn tools_are_cleaned_and_their_hints_read() {
        let t = Tool::from_json(&json!({"name": "search_issues", "description": "Find   issues\n in a repo",
            "inputSchema": {"properties": {"q": {}, "repo": {}, "bad key!": {}}, "required": ["q", "nope"]},
            "annotations": {"readOnlyHint": true}})).unwrap();
        assert_eq!(t.signature(), "search_issues(q, repo?)");
        assert_eq!(t.description, "Find issues in a repo");
        assert!(t.read_only && !t.destructive && !t.needs_yes(false));
        let w = Tool::from_json(&json!({"name": "create_issue"})).unwrap();
        assert!(!w.read_only && w.destructive, "unannotated: presumed destructive, as the protocol says");
        assert!(w.needs_yes(false) && !w.needs_yes(true));
        let safe = Tool::from_json(&json!({"name": "add_label", "annotations": {"destructiveHint": false}})).unwrap();
        assert!(!safe.needs_yes(false));
        assert!(Tool::from_json(&json!({"name": "rm -rf /"})).is_none());
        assert!(Tool::from_json(&json!({"description": "no name"})).is_none());
    }

    #[test]
    fn calls_parse_in_every_shape() {
        assert_eq!(parse_call("gh.search", "{\"q\": \"bug\"}").unwrap(), ("gh.search".into(), json!({"q": "bug"})));
        assert_eq!(parse_call("", "gh.search\n{\"q\": 1}").unwrap(), ("gh.search".into(), json!({"q": 1})));
        assert_eq!(parse_call("gh.search {\"q\": 2}", "").unwrap(), ("gh.search".into(), json!({"q": 2})));
        assert_eq!(parse_call("gh.list", "").unwrap(), ("gh.list".into(), json!({})));
        assert!(parse_call("search", "{}").is_err());
        assert!(parse_call("gh.x", "[1]").is_err());
        assert!(parse_call("gh.x", "{oops").unwrap_err().contains("valid JSON"));
    }
}
