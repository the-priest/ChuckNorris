//! A real browser, headless, driven over the DevTools protocol.
//!
//! Two uses:
//! - reading a page a plain fetch can't: one that is empty until its
//!   JavaScript runs, or behind a bot check that clears itself in a real
//!   browser after a few seconds ([`read_page`]);
//! - checking a page Chuck built ([`browse`]): load a URL or a local file
//!   at a window size, act on it (click, type, press keys, scroll, wait,
//!   evaluate), and report what breaks web apps — uncaught exceptions,
//!   console errors, failed requests and HTTP errors, with file and line —
//!   plus the visible text and a screenshot.
//!
//! The user's own Chromium-family browser is used (Brave, Chromium, Chrome,
//! Edge, Vivaldi: they all speak the protocol), started with a throwaway
//! profile and nothing that phones home, in its own process group, and
//! killed with that group when the call ends (or Chuck does). Every request
//! the page makes can be judged first: a page read for research reaches
//! only the public internet, however it redirects or what it loads; a job
//! in an offline folder reaches no network at all.

use crate::{html, public_address, Proxy};
use base64::Engine as _;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant};

const BRAVE: &[&str] = &["brave", "brave-browser", "brave-browser-stable", "brave-browser-beta", "/opt/brave-bin/brave",
    "/opt/brave.com/brave/brave", "/usr/lib/brave-bin/brave", "/snap/bin/brave",
    "/var/lib/flatpak/exports/bin/com.brave.Browser", "~/.local/share/flatpak/exports/bin/com.brave.Browser"];
const CHROMIUM: &[&str] = &["chromium", "chromium-browser", "/usr/lib/chromium/chromium", "/snap/bin/chromium",
    "/var/lib/flatpak/exports/bin/org.chromium.Chromium", "~/.local/share/flatpak/exports/bin/org.chromium.Chromium",
    "/opt/pw-browsers/chromium"];
const CHROME: &[&str] = &["google-chrome", "google-chrome-stable", "/opt/google/chrome/chrome", "chrome",
    "microsoft-edge", "microsoft-edge-stable", "vivaldi", "vivaldi-stable"];
/// What Settings offers.
pub const ENGINES: &[&str] = &["auto", "brave", "chromium", "chrome"];

fn expand(p: &str) -> PathBuf {
    match p.strip_prefix("~/") {
        Some(rest) => std::env::var_os("HOME").map(PathBuf::from).unwrap_or_default().join(rest),
        None => PathBuf::from(p),
    }
}

fn executable(p: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    p.metadata().is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
}

fn first(names: &[&str]) -> Option<PathBuf> {
    names.iter().find_map(|n| {
        let p = expand(n);
        if n.contains('/') {
            return executable(&p).then_some(p);
        }
        std::env::split_paths(&std::env::var_os("PATH")?).map(|d| d.join(n)).find(|p| executable(p))
    })
}

/// The browser to drive: CHROME_BIN / CHUCK_CHROME win, then the preferred
/// one ("auto", "brave", "chromium", "chrome", or a path), then Brave,
/// Chromium, Chrome. Err says what to install.
pub fn find(pref: &str) -> Result<PathBuf, String> {
    for var in ["CHROME_BIN", "CHUCK_CHROME"] {
        if let Some(p) = std::env::var_os(var).map(PathBuf::from).filter(|p| executable(p)) {
            return Ok(p);
        }
    }
    let pref = pref.trim();
    let low = pref.to_ascii_lowercase();
    if pref.contains('/') {
        let p = expand(pref);
        return if executable(&p) { Ok(p) } else { Err(format!("browser not found or not executable: {pref}")) };
    }
    let group = match low.as_str() {
        "brave" => Some(BRAVE),
        "chromium" => Some(CHROMIUM),
        "chrome" => Some(CHROME),
        _ => None,
    };
    if let Some(g) = group {
        return first(g).ok_or_else(|| format!("{} isn't installed (or isn't on PATH): install it, or pick \
            Automatic in Settings › Research", pref[..1].to_uppercase() + &pref[1..]));
    }
    [BRAVE, CHROMIUM, CHROME].iter().find_map(|g| first(g))
        .ok_or_else(|| "no browser found: install Brave or Chromium (pacman -S chromium / apt install chromium)".into())
}

/// "Brave", "Chromium", "Chrome"… from the executable.
pub fn engine_name(exe: &Path) -> String {
    let real = std::fs::canonicalize(exe).unwrap_or_else(|_| exe.to_path_buf());
    let s = format!("{} {}", exe.display(), real.display()).to_ascii_lowercase();
    for (k, v) in [("brave", "Brave"), ("chromium", "Chromium"), ("edge", "Edge"), ("vivaldi", "Vivaldi"),
                   ("chrome", "Chrome")] {
        if s.contains(k) {
            return v.into();
        }
    }
    exe.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_else(|| "browser".into())
}

// ── a minimal WebSocket client (RFC 6455): enough for the DevTools protocol ──

fn random_u64() -> u64 {
    use std::hash::{BuildHasher, Hasher};
    let mut h = std::collections::hash_map::RandomState::new().build_hasher();
    h.write_u128(Instant::now().elapsed().as_nanos() ^ std::process::id() as u128);
    h.finish()
}

struct Ws {
    sock: TcpStream,
    buf: Vec<u8>,
}

#[derive(Debug)]
enum WsErr {
    Timeout,
    Closed(String),
}

impl Ws {
    fn connect(url: &str, timeout: Duration) -> Result<Ws, String> {
        let rest = url.strip_prefix("ws://").ok_or("only ws:// is spoken to the browser")?;
        let (auth, path) = rest.split_once('/').map(|(a, p)| (a, format!("/{p}"))).unwrap_or((rest, "/".into()));
        let addr = auth.to_socket_addrs_one().ok_or_else(|| format!("bad address {auth}"))?;
        let mut sock = TcpStream::connect_timeout(&addr, timeout).map_err(|e| e.to_string())?;
        sock.set_nodelay(true).ok();
        sock.set_read_timeout(Some(timeout)).ok();
        let key = base64::engine::general_purpose::STANDARD.encode(random_u64().to_le_bytes().repeat(2));
        write!(sock, "GET {path} HTTP/1.1\r\nHost: {auth}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\
            Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").map_err(|e| e.to_string())?;
        let mut head = Vec::new();
        let mut chunk = [0u8; 4096];
        let at = loop {
            let n = sock.read(&mut chunk).map_err(|e| format!("websocket handshake: {e}"))?;
            if n == 0 {
                return Err("websocket handshake: the browser closed the connection".into());
            }
            head.extend_from_slice(&chunk[..n]);
            if let Some(i) = head.windows(4).position(|w| w == b"\r\n\r\n") {
                break i;
            }
            if head.len() > 65536 {
                return Err("websocket handshake: no end to the answer".into());
            }
        };
        let status = String::from_utf8_lossy(&head[..head.iter().position(|&b| b == b'\r').unwrap_or(at)]).to_string();
        if !status.contains(" 101") {
            return Err(format!("websocket handshake refused: {status}"));
        }
        Ok(Ws { sock, buf: head[at + 4..].to_vec() })
    }

    fn send(&mut self, text: &str) -> Result<(), String> {
        let data = text.as_bytes();
        let n = data.len();
        let mut frame = Vec::with_capacity(n + 14);
        frame.push(0x81); // FIN + text
        if n < 126 {
            frame.push(0x80 | n as u8);
        } else if n < 65536 {
            frame.push(0x80 | 126);
            frame.extend_from_slice(&(n as u16).to_be_bytes());
        } else {
            frame.push(0x80 | 127);
            frame.extend_from_slice(&(n as u64).to_be_bytes());
        }
        let mask = (random_u64() as u32).to_le_bytes();
        frame.extend_from_slice(&mask);
        frame.extend(data.iter().enumerate().map(|(i, b)| b ^ mask[i % 4]));
        self.sock.write_all(&frame).map_err(|e| e.to_string())
    }

    /// `n` bytes, waiting at most until `end`.
    fn take(&mut self, n: usize, end: Instant) -> Result<Vec<u8>, WsErr> {
        let mut chunk = vec![0u8; 65536];
        while self.buf.len() < n {
            let left = end.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return Err(WsErr::Timeout);
            }
            self.sock.set_read_timeout(Some(left.max(Duration::from_millis(1)))).ok();
            match self.sock.read(&mut chunk) {
                Ok(0) => return Err(WsErr::Closed("the browser closed the connection".into())),
                Ok(k) => self.buf.extend_from_slice(&chunk[..k]),
                Err(e) if matches!(e.kind(), std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut) => {
                    return Err(WsErr::Timeout);
                }
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
                Err(e) => return Err(WsErr::Closed(e.to_string())),
            }
        }
        Ok(self.buf.drain(..n).collect())
    }

    /// The next whole text message, or Timeout at `end`. A frame half read
    /// when time runs out stays buffered for the next call.
    fn recv(&mut self, end: Instant) -> Result<String, WsErr> {
        let mut parts: Vec<u8> = Vec::new();
        loop {
            // peek the whole frame before consuming any of it
            let need_head = self.frame_len(end)?;
            let frame = self.take(need_head, end)?;
            let (b1, b2) = (frame[0], frame[1]);
            let (fin, op) = (b1 & 0x80 != 0, b1 & 0x0f);
            let mut i = 2;
            let mut n = (b2 & 0x7f) as usize;
            if n == 126 {
                n = u16::from_be_bytes([frame[2], frame[3]]) as usize;
                i = 4;
            } else if n == 127 {
                n = u64::from_be_bytes(frame[2..10].try_into().unwrap()) as usize;
                i = 10;
            }
            let mask = (b2 & 0x80 != 0).then(|| [frame[i], frame[i + 1], frame[i + 2], frame[i + 3]]);
            if mask.is_some() {
                i += 4;
            }
            let mut payload = frame[i..i + n].to_vec();
            if let Some(m) = mask {
                payload.iter_mut().enumerate().for_each(|(k, b)| *b ^= m[k % 4]);
            }
            match op {
                0x8 => return Err(WsErr::Closed("the browser closed the connection".into())),
                0x9 => {
                    let mut pong = vec![0x8a, 0x80 | payload.len().min(125) as u8];
                    let mask = (random_u64() as u32).to_le_bytes();
                    pong.extend_from_slice(&mask);
                    pong.extend(payload.iter().take(125).enumerate().map(|(k, b)| b ^ mask[k % 4]));
                    let _ = self.sock.write_all(&pong);
                }
                0x0..=0x2 => {
                    parts.extend_from_slice(&payload);
                    if fin {
                        return Ok(String::from_utf8_lossy(&parts).into_owned());
                    }
                }
                _ => {}
            }
        }
    }

    /// How many bytes the next frame is, header included (reading only
    /// what that takes, without consuming it).
    fn frame_len(&mut self, end: Instant) -> Result<usize, WsErr> {
        self.fill(2, end)?;
        let (b2, mut head) = (self.buf[1], 2usize);
        let len7 = (b2 & 0x7f) as usize;
        head += match len7 {
            126 => 2,
            127 => 8,
            _ => 0,
        };
        if b2 & 0x80 != 0 {
            head += 4;
        }
        self.fill(head, end)?;
        let n = match len7 {
            126 => u16::from_be_bytes([self.buf[2], self.buf[3]]) as usize,
            127 => u64::from_be_bytes(self.buf[2..10].try_into().unwrap()) as usize,
            n => n,
        };
        if n > 256 << 20 {
            return Err(WsErr::Closed("a message bigger than 256 MB".into()));
        }
        Ok(head + n)
    }

    fn fill(&mut self, n: usize, end: Instant) -> Result<(), WsErr> {
        if self.buf.len() >= n {
            return Ok(());
        }
        let got = self.take(n, end)?;
        // put it back: fill only makes sure it's there
        let mut back = got;
        back.extend_from_slice(&self.buf);
        self.buf = back;
        Ok(())
    }
}

trait OneAddr {
    fn to_socket_addrs_one(&self) -> Option<std::net::SocketAddr>;
}

impl OneAddr for &str {
    fn to_socket_addrs_one(&self) -> Option<std::net::SocketAddr> {
        use std::net::ToSocketAddrs;
        self.to_socket_addrs().ok()?.next()
    }
}

// ── what the page may reach ──────────────────────────────────────────────

/// Which requests a page may make.
#[derive(Clone, Debug, PartialEq)]
pub enum Net {
    /// Anything (checking a page Chuck built, which may talk to a local server).
    Any,
    /// The public internet only (research: a page must not reach the router
    /// or a local service, whatever it redirects to or loads).
    Public(Proxy),
    /// No network at all (a job in a folder sandboxed offline).
    Offline,
}

/// Local files the browser may open (None: any).
#[derive(Clone, Debug, Default)]
pub struct Files {
    pub root: Option<PathBuf>,
    /// Never these, even inside the root (credential folders).
    pub hidden: Vec<PathBuf>,
}

impl Files {
    fn allowed(&self, p: &Path) -> bool {
        let Some(root) = &self.root else { return true };
        let real = std::fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf());
        let root = std::fs::canonicalize(root).unwrap_or_else(|_| root.clone());
        real.starts_with(&root) && !self.hidden.iter().any(|h| real.starts_with(h))
    }
}

/// A URL from what the agent wrote: http(s), file:, about: and data: pass
/// through, a bare localhost:port gets http://, anything else is a file
/// (relative to `base`; a folder means its index.html).
pub fn to_url(target: &str, base: Option<&Path>, files: &Files) -> Result<String, String> {
    let t = target.trim().trim_matches(['\'', '"', '`']);
    let lower = t.to_ascii_lowercase();
    if lower.starts_with("file:") {
        let path = unpercent(t.trim_start_matches("file://").trim_start_matches("file:"));
        let path = path.split(['?', '#']).next().unwrap_or("").to_string();
        if !files.allowed(Path::new(&path)) {
            return Err(format!("{path} is outside this folder (its access is restricted)"));
        }
        return Ok(t.to_string());
    }
    static SCHEME: OnceLock<regex::Regex> = OnceLock::new();
    static LOCAL: OnceLock<regex::Regex> = OnceLock::new();
    if SCHEME.get_or_init(|| regex::Regex::new(r"(?i)^[a-z][a-z0-9+.-]*://").unwrap()).is_match(t)
        || lower.starts_with("about:") || lower.starts_with("data:")
    {
        return Ok(t.to_string());
    }
    if LOCAL.get_or_init(|| regex::Regex::new(r"^(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:\d+)?(/.*)?$").unwrap()).is_match(t) {
        return Ok(format!("http://{t}"));
    }
    if t.is_empty() {
        return Err("give a URL or a file path".into());
    }
    let mut p = expand(t);
    if p.is_relative() {
        if let Some(b) = base {
            p = b.join(p);
        }
    }
    if p.is_dir() {
        p = p.join("index.html");
    }
    if !p.is_file() {
        return Err(format!("no such file: {t}"));
    }
    if !files.allowed(&p) {
        return Err(format!("{t} is outside this folder (its access is restricted)"));
    }
    let abs = std::fs::canonicalize(&p).unwrap_or(p);
    Ok(format!("file://{}", abs.display().to_string().split('/').map(crate::enc).collect::<Vec<_>>().join("/")))
}

/// %XX decoded (a path: `+` stays a plus).
fn unpercent(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() && s.is_char_boundary(i + 1) && s.is_char_boundary(i + 3) {
            if let Ok(v) = u8::from_str_radix(&s[i + 1..i + 3], 16) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(b[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

// ── the browser process and one page in it ──────────────────────────────

struct Browser {
    child: Child,
    profile: PathBuf,
    _guard: chuck_tools::Service,
}

impl Drop for Browser {
    fn drop(&mut self) {
        unsafe {
            libc::killpg(self.child.id() as i32, libc::SIGKILL);
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = std::fs::remove_dir_all(&self.profile);
    }
}

fn launch(exe: &Path, proxy: Option<&Proxy>, file_access: bool, startup: Duration) -> Result<(Browser, String), String> {
    static N: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    let profile = std::env::temp_dir().join(format!("chuck-browse-{}-{}-{:x}", std::process::id(),
                                                    N.fetch_add(1, Ordering::SeqCst), random_u64() as u32));
    std::fs::create_dir_all(&profile).map_err(|e| e.to_string())?;
    let mut args: Vec<String> = ["--headless=new", "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
        "--disable-gpu", "--hide-scrollbars", "--mute-audio", "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
        // no phoning home: component updates, metrics, safe-browsing pings
        "--disable-background-networking", "--disable-component-update", "--disable-sync", "--disable-default-apps",
        "--disable-domain-reliability", "--disable-client-side-phishing-detection", "--metrics-recording-only",
        "--no-pings", "--disable-breakpad", "--password-store=basic", "--use-mock-keychain",
        "--disable-features=Translate,MediaRouter,OptimizationHints"]
        .iter().map(|s| s.to_string()).collect();
    args.push(format!("--user-data-dir={}", profile.display()));
    if file_access {
        args.push("--allow-file-access-from-files".into()); // a local page's own assets
    }
    match proxy {
        Some(Proxy::None) => args.push("--no-proxy-server".into()),
        Some(Proxy::Url(u)) => args.push(format!("--proxy-server={u}")),
        _ => {}
    }
    if unsafe { libc::geteuid() } == 0 {
        args.push("--no-sandbox".into()); // Chromium refuses to run as root otherwise
    }
    args.push("about:blank".into());
    let mut c = Command::new(exe);
    chuck_tools::tag(&mut c);
    c.args(&args).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::piped());
    unsafe {
        c.pre_exec(|| {
            libc::setsid();
            libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL);
            Ok(())
        });
    }
    let mut child = c.spawn().map_err(|e| {
        let _ = std::fs::remove_dir_all(&profile);
        format!("couldn't start {}: {e}", exe.display())
    })?;
    let guard = chuck_tools::Service::register(child.id() as i32);
    let err = child.stderr.take();
    let browser = Browser { child, profile, _guard: guard };
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::Builder::new().name("browser-log".into()).spawn(move || {
        let Some(err) = err else { return };
        let mut tx = Some(tx);
        // keep reading its log to the end: a full stderr pipe would freeze the browser
        for line in BufReader::new(err).lines() {
            let Ok(line) = line else { break };
            if let Some(i) = line.find("DevTools listening on ") {
                if let Some(t) = tx.take() {
                    let _ = t.send(line[i + 22..].trim().to_string());
                }
            }
        }
    }).map_err(|e| e.to_string())?;
    match rx.recv_timeout(startup) {
        Ok(ws) if ws.starts_with("ws://") => Ok((browser, ws)),
        Ok(other) => Err(format!("the browser answered with an odd address: {other}")),
        Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => Err("the browser exited at start".into()),
        Err(_) => Err(format!("the browser didn't start within {}s", startup.as_secs())),
    }
}

/// One page target. `call` returns a result; events pile up in `events`.
struct Page {
    ws: Ws,
    next: u64,
    events: Vec<Value>,
    net: Net,
    files: Files,
    /// Hosts judged already (name → allowed).
    judged: HashMap<String, bool>,
    /// Requests refused because of `net`.
    pub refused: Vec<String>,
    stop: Option<Arc<AtomicBool>>,
    /// Nothing runs past this.
    deadline: Instant,
    /// The top frame (from the first navigation).
    frame: Option<String>,
}

fn stopped(stop: &Option<Arc<AtomicBool>>) -> bool {
    stop.as_ref().is_some_and(|s| s.load(Ordering::SeqCst))
}

impl Page {
    /// Handle one message: an answer to `want` comes back; events are kept
    /// (and paused requests decided at once).
    fn handle(&mut self, msg: Value, want: Option<u64>) -> Result<Option<Value>, String> {
        if let (Some(w), Some(id)) = (want, msg.get("id").and_then(Value::as_u64)) {
            if id == w {
                if let Some(e) = msg.get("error") {
                    return Err(e.get("message").and_then(Value::as_str).unwrap_or("error").to_string());
                }
                return Ok(Some(msg.get("result").cloned().unwrap_or(json!({}))));
            }
            return Ok(None);
        }
        match msg.get("method").and_then(Value::as_str) {
            Some("Fetch.requestPaused") => self.decide(&msg["params"])?,
            Some(_) => self.events.push(msg),
            None => {}
        }
        Ok(None)
    }

    fn decide(&mut self, p: &Value) -> Result<(), String> {
        let id = p.get("requestId").and_then(Value::as_str).unwrap_or("").to_string();
        let url = p.pointer("/request/url").and_then(Value::as_str).unwrap_or("").to_string();
        let ok = self.may_reach(&url);
        if !ok {
            self.refused.push(html::cut(&url, 200).to_string());
        }
        let n = self.next_id();
        let msg = if ok {
            json!({"id": n, "method": "Fetch.continueRequest", "params": {"requestId": id}})
        } else {
            json!({"id": n, "method": "Fetch.failRequest", "params": {"requestId": id, "errorReason": "AccessDenied"}})
        };
        self.ws.send(&msg.to_string())
    }

    fn may_reach(&mut self, url: &str) -> bool {
        let lower = url.to_ascii_lowercase();
        if lower.starts_with("data:") || lower.starts_with("blob:") || lower.starts_with("about:") {
            return true;
        }
        if lower.starts_with("file://") {
            let path = unpercent(&url[7..]);
            return self.files.allowed(Path::new(path.split(['?', '#']).next().unwrap_or("")));
        }
        let web = ["http://", "https://", "ws://", "wss://"].iter().any(|s| lower.starts_with(s));
        match &self.net {
            Net::Any => true,
            Net::Offline => false,
            Net::Public(proxy) => {
                if !web {
                    return false;
                }
                let host = crate::host_port(url).map(|(h, p)| format!("{h}:{p}")).unwrap_or_default();
                if let Some(v) = self.judged.get(&host) {
                    return *v;
                }
                let http = url.replacen("ws", "http", 1);
                let v = public_address(if lower.starts_with("ws") { &http } else { url }, proxy).is_ok();
                self.judged.insert(host, v);
                v
            }
        }
    }

    fn next_id(&mut self) -> u64 {
        self.next += 1;
        self.next
    }

    fn call(&mut self, method: &str, params: Value, timeout: Duration) -> Result<Value, String> {
        let id = self.next_id();
        self.ws.send(&json!({"id": id, "method": method, "params": params}).to_string())?;
        let end = (Instant::now() + timeout).min(self.deadline);
        loop {
            if stopped(&self.stop) {
                return Err("stopped".into());
            }
            let slice = (Instant::now() + Duration::from_millis(250)).min(end);
            match self.ws.recv(slice) {
                Ok(text) => {
                    let Ok(msg) = serde_json::from_str::<Value>(&text) else { continue };
                    if let Some(r) = self.handle(msg, Some(id)).map_err(|e| format!("{method}: {e}"))? {
                        return Ok(r);
                    }
                }
                Err(WsErr::Timeout) if Instant::now() < end => {}
                Err(WsErr::Timeout) => return Err(format!("{method} timed out")),
                Err(WsErr::Closed(e)) => return Err(e),
            }
        }
    }

    /// Collect events for a while (the page settling, timers firing).
    fn pump(&mut self, d: Duration) -> Result<(), String> {
        let end = (Instant::now() + d).min(self.deadline);
        while Instant::now() < end {
            if stopped(&self.stop) {
                return Err("stopped".into());
            }
            let slice = (Instant::now() + Duration::from_millis(250)).min(end);
            match self.ws.recv(slice) {
                Ok(text) => {
                    if let Ok(msg) = serde_json::from_str::<Value>(&text) {
                        self.handle(msg, None)?;
                    }
                }
                Err(WsErr::Timeout) => {}
                Err(WsErr::Closed(e)) => return Err(e),
            }
        }
        Ok(())
    }

    fn wait_for(&mut self, method: &str, d: Duration) -> Result<bool, String> {
        let start = self.events.len();
        let end = Instant::now() + d;
        loop {
            if self.events[start..].iter().any(|e| e.get("method").and_then(Value::as_str) == Some(method)) {
                return Ok(true);
            }
            let left = end.saturating_duration_since(Instant::now());
            if left.is_zero() || Instant::now() >= self.deadline {
                return Ok(false);
            }
            self.pump(left.min(Duration::from_millis(200)))?;
        }
    }

    fn evaluate(&mut self, expr: &str) -> Result<Value, String> {
        let r = self.call("Runtime.evaluate", json!({"expression": expr, "returnByValue": true, "awaitPromise": true,
                                                       "userGesture": true}), Duration::from_secs(15))?;
        if let Some(d) = r.get("exceptionDetails") {
            let msg = d.pointer("/exception/description").and_then(Value::as_str)
                .or_else(|| d.get("text").and_then(Value::as_str)).unwrap_or("error");
            return Err(msg.lines().next().unwrap_or("error").to_string());
        }
        Ok(r.pointer("/result/value").cloned().unwrap_or(Value::Null))
    }

    fn navigate(&mut self, url: &str) -> Result<(), String> {
        let r = self.call("Page.navigate", json!({"url": url}), Duration::from_secs(30))?;
        if self.frame.is_none() {
            self.frame = r.get("frameId").and_then(Value::as_str).map(String::from);
        }
        if let Some(e) = r.get("errorText").and_then(Value::as_str) {
            if e == "net::ERR_ACCESS_DENIED" || e == "net::ERR_BLOCKED_BY_CLIENT" {
                return Err(format!("{url} may not be reached from here ({})", match self.net {
                    Net::Offline => "this folder is sandboxed offline",
                    Net::Public(_) => "web reads stay on the public internet",
                    Net::Any => "refused",
                }));
            }
            return Err(format!("couldn't load {url}: {e}"));
        }
        self.wait_for("Page.loadEventFired", Duration::from_secs(20))?;
        Ok(())
    }
}

/// The page target of a freshly started browser.
fn open_page(browser_ws: &str) -> Result<String, String> {
    let rest = browser_ws.strip_prefix("ws://").ok_or("bad browser address")?;
    let auth = rest.split('/').next().unwrap_or("");
    let mut ws = Ws::connect(browser_ws, Duration::from_secs(10))?;
    for i in 0..50u64 {
        ws.send(&json!({"id": i + 1, "method": "Target.getTargets"}).to_string())?;
        let end = Instant::now() + Duration::from_secs(5);
        loop {
            let text = match ws.recv(end) {
                Ok(t) => t,
                Err(WsErr::Timeout) => break,
                Err(WsErr::Closed(e)) => return Err(e),
            };
            let Ok(v) = serde_json::from_str::<Value>(&text) else { continue };
            if v.get("id").and_then(Value::as_u64) != Some(i + 1) {
                continue;
            }
            if let Some(t) = v.pointer("/result/targetInfos").and_then(Value::as_array)
                .and_then(|ts| ts.iter().find(|t| t.get("type").and_then(Value::as_str) == Some("page")))
            {
                let id = t.get("targetId").and_then(Value::as_str).unwrap_or("");
                return Ok(format!("ws://{auth}/devtools/page/{id}"));
            }
            break;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    Err("no page in the browser".into())
}

// ── keys and clicks ──────────────────────────────────────────────────────

fn key_of(name: &str) -> Result<(String, String, u32), String> {
    let k = match name.to_ascii_lowercase().as_str() {
        "enter" | "return" => ("Enter", "Enter", 13),
        "tab" => ("Tab", "Tab", 9),
        "escape" | "esc" => ("Escape", "Escape", 27),
        "space" => (" ", "Space", 32),
        "backspace" => ("Backspace", "Backspace", 8),
        "delete" | "del" => ("Delete", "Delete", 46),
        "arrowleft" | "left" => ("ArrowLeft", "ArrowLeft", 37),
        "arrowup" | "up" => ("ArrowUp", "ArrowUp", 38),
        "arrowright" | "right" => ("ArrowRight", "ArrowRight", 39),
        "arrowdown" | "down" => ("ArrowDown", "ArrowDown", 40),
        "home" => ("Home", "Home", 36),
        "end" => ("End", "End", 35),
        "pageup" => ("PageUp", "PageUp", 33),
        "pagedown" => ("PageDown", "PageDown", 34),
        _ => {
            let mut cs = name.chars();
            let (Some(ch), None) = (cs.next(), cs.next()) else {
                return Err(format!("unknown key `{name}` (Enter, Tab, Escape, Space, Backspace, Delete, arrows, \
                    Home, End, PageUp, PageDown, or one character)"));
            };
            let code = if ch.is_ascii_alphabetic() { format!("Key{}", ch.to_ascii_uppercase()) } else if ch.is_ascii_digit() {
                format!("Digit{ch}") } else { String::new() };
            return Ok((ch.to_string(), code, ch.to_ascii_uppercase() as u32));
        }
    };
    Ok((k.0.to_string(), k.1.to_string(), k.2))
}

fn press(page: &mut Page, name: &str) -> Result<(), String> {
    let (key, code, vk) = key_of(name)?;
    let base = json!({"key": key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk});
    let text = if key.chars().count() == 1 { key.clone() } else if key == "Enter" { "\r".into() } else { String::new() };
    let mut down = base.clone();
    if text.is_empty() {
        down["type"] = json!("rawKeyDown");
    } else {
        down["type"] = json!("keyDown");
        down["text"] = json!(text);
    }
    page.call("Input.dispatchKeyEvent", down, Duration::from_secs(10))?;
    let mut up = base;
    up["type"] = json!("keyUp");
    page.call("Input.dispatchKeyEvent", up, Duration::from_secs(10))?;
    Ok(())
}

fn click(page: &mut Page, target: &str) -> Result<(), String> {
    static XY: OnceLock<regex::Regex> = OnceLock::new();
    let (x, y) = if let Some(c) = XY.get_or_init(|| regex::Regex::new(r"^\s*(\d+)\s*[, ]\s*(\d+)\s*$").unwrap()).captures(target) {
        (c[1].parse::<f64>().unwrap_or(0.0), c[2].parse::<f64>().unwrap_or(0.0))
    } else {
        let sel = serde_json::to_string(target).unwrap_or_default();
        let b = page.evaluate(&format!("(()=>{{const e=document.querySelector({sel}); if(!e) return null; \
            e.scrollIntoView({{block:'center'}}); const r=e.getBoundingClientRect(); \
            return [r.left+r.width/2, r.top+r.height/2];}})()"))?;
        match b.as_array() {
            Some(a) if a.len() == 2 => (a[0].as_f64().unwrap_or(0.0), a[1].as_f64().unwrap_or(0.0)),
            _ => return Err(format!("no element matches `{target}`")),
        }
    };
    for t in ["mouseMoved", "mousePressed", "mouseReleased"] {
        page.call("Input.dispatchMouseEvent", json!({"type": t, "x": x, "y": y, "button": "left", "clickCount": 1}),
                  Duration::from_secs(10))?;
    }
    Ok(())
}

// ── what the page said ──────────────────────────────────────────────────

fn src(d: &Value) -> String {
    let url = d.get("url").and_then(Value::as_str).unwrap_or("");
    let path = url.split(['?', '#']).next().unwrap_or("");
    // a script inline in the page is reported at the page's own address
    let name = match path.rsplit('/').next().unwrap_or("") {
        "" if !path.is_empty() => "the page",
        n => n,
    };
    match (name.is_empty(), d.get("lineNumber").and_then(Value::as_i64)) {
        (true, _) => String::new(),
        (false, Some(l)) => format!(" ({name}:{})", l + 1),
        (false, None) => format!(" ({name})"),
    }
}

fn arg_text(a: &Value) -> String {
    match a.get("value") {
        Some(Value::String(s)) => s.clone(),
        Some(v) => v.to_string(),
        None => a.get("description").and_then(Value::as_str).map(String::from)
            .unwrap_or_else(|| a.get("type").and_then(Value::as_str).unwrap_or("").to_string()),
    }
}

fn dedup(v: Vec<String>) -> Vec<String> {
    let mut seen = std::collections::HashSet::new();
    v.into_iter().filter(|x| seen.insert(x.clone())).collect()
}

/// (errors, warnings, logs, failed requests) from the page's events.
fn collect(events: &[Value]) -> (Vec<String>, Vec<String>, Vec<String>, Vec<String>) {
    let (mut errors, mut warnings, mut logs, mut failed) = (Vec::new(), Vec::new(), Vec::new(), Vec::new());
    let mut urls: HashMap<String, String> = HashMap::new();
    for e in events {
        let p = &e["params"];
        match e.get("method").and_then(Value::as_str).unwrap_or("") {
            "Runtime.consoleAPICalled" => {
                let text = p.get("args").and_then(Value::as_array).map(|a| a.iter().map(arg_text).collect::<Vec<_>>().join(" "))
                    .unwrap_or_default();
                let at = p.pointer("/stackTrace/callFrames/0").map(src).unwrap_or_default();
                match p.get("type").and_then(Value::as_str).unwrap_or("") {
                    "error" | "assert" => errors.push(format!("console.error: {text}{at}")),
                    "warning" => warnings.push(format!("console.warn: {text}{at}")),
                    _ => logs.push(text),
                }
            }
            "Runtime.exceptionThrown" => {
                let d = &p["exceptionDetails"];
                let desc = d.pointer("/exception/description").and_then(Value::as_str)
                    .or_else(|| d.get("text").and_then(Value::as_str)).unwrap_or("");
                errors.push(format!("Uncaught {}{}", desc.lines().next().unwrap_or("").replace("Uncaught ", ""), src(d)));
            }
            "Log.entryAdded" => {
                let en = &p["entry"];
                let source = en.get("source").and_then(Value::as_str).unwrap_or("browser");
                // network errors are reported once, under failed requests
                if en.get("level").and_then(Value::as_str) == Some("error") && !matches!(source, "javascript" | "network") {
                    let url = en.get("url").and_then(Value::as_str).map(|u| format!(" ({u})")).unwrap_or_default();
                    errors.push(format!("{source}: {}{url}", en.get("text").and_then(Value::as_str).unwrap_or("")));
                }
            }
            "Network.requestWillBeSent" => {
                if let (Some(id), Some(u)) = (p.get("requestId").and_then(Value::as_str), p.pointer("/request/url").and_then(Value::as_str)) {
                    urls.insert(id.into(), u.into());
                }
            }
            "Network.loadingFailed" if p.get("canceled").and_then(Value::as_bool) != Some(true) => {
                let id = p.get("requestId").and_then(Value::as_str).unwrap_or("");
                failed.push(format!("{}: {}", urls.get(id).map(String::as_str).unwrap_or("?"),
                                    p.get("errorText").and_then(Value::as_str).unwrap_or("failed")));
            }
            "Network.responseReceived" => {
                let r = &p["response"];
                let st = r.get("status").and_then(Value::as_i64).unwrap_or(0);
                let url = r.get("url").and_then(Value::as_str).unwrap_or("?");
                // the browser asks for a favicon by itself: not the page's failure
                if st >= 400 && !url.split(['?', '#']).next().unwrap_or("").ends_with("/favicon.ico") {
                    failed.push(format!("{url}: HTTP {st}"));
                }
            }
            _ => {}
        }
    }
    (dedup(errors), dedup(warnings), logs, dedup(failed))
}

// ── bot walls ────────────────────────────────────────────────────────────

/// A bot check's words ("Just a moment…", "Checking your browser").
pub fn challenged(text: &str) -> bool {
    static RX: OnceLock<regex::Regex> = OnceLock::new();
    RX.get_or_init(|| regex::Regex::new(
        r"(?i)just a moment\.\.\.|checking (?:if the site connection is secure|your browser)|attention required! \| cloudflare|\
          cf-browser-verification|challenge-platform|enable javascript and cookies to continue|\
          please enable js and disable any ad blocker|captcha-delivery|px-captcha|verify (?:that )?you are (?:a )?human|\
          are you a robot").unwrap()).is_match(text)
}

const CHALLENGE_PROBE: &str = "document.title + '\\n' + (document.body ? document.body.innerText.slice(0, 3000) : '')";
/// The readable part: an <article> or <main> when the page has one.
pub const READER_JS: &str = "(document.querySelector('article') || document.querySelector('main') \
    || document.querySelector('[role=main]') || document.body || {innerText: ''}).innerText";

// ── the calls ────────────────────────────────────────────────────────────

/// How to run one browse.
#[derive(Clone, Debug)]
pub struct Options {
    /// The browser (from [`find`]).
    pub exe: PathBuf,
    pub size: (u32, u32),
    /// Relative file paths are read from here.
    pub base: Option<PathBuf>,
    pub files: Files,
    pub net: Net,
    /// The browser's proxy (None: the system's).
    pub proxy: Option<Proxy>,
    pub screenshot: bool,
    /// After load, before the actions.
    pub settle: Duration,
    /// The whole call, start to report.
    pub timeout: Duration,
    /// What "the text" is (default: the whole body).
    pub text_js: Option<String>,
    pub stop: Option<Arc<AtomicBool>>,
}

impl Options {
    pub fn new(exe: PathBuf) -> Options {
        Options { exe, size: (1280, 800), base: None, files: Files::default(), net: Net::Any, proxy: None, screenshot: true,
                  settle: Duration::from_millis(600), timeout: Duration::from_secs(60), text_js: None, stop: None }
    }
}

/// What the browser saw.
#[derive(Clone, Debug, Default)]
pub struct Report {
    pub url: String,
    /// Where it ended up (after redirects and goto).
    pub final_url: String,
    pub title: String,
    pub text: String,
    pub errors: Vec<String>,
    pub warnings: Vec<String>,
    pub logs: Vec<String>,
    pub failed: Vec<String>,
    /// Requests the page wasn't allowed to make.
    pub refused: Vec<String>,
    /// Each action, and how it went.
    pub steps: Vec<String>,
    /// PNG.
    pub shot: Option<Vec<u8>>,
    /// The HTTP status the page itself last came with (None: a file, or no answer).
    pub status: Option<u16>,
    pub ms: u64,
    pub size: (u32, u32),
    pub engine: String,
}

/// The largest side a screenshot is taken at (a model reads no more).
const SHOT_MAX: f64 = 1568.0;

/// "1024x768" as a window size, within reason.
fn window_size(arg: &str) -> Option<(u32, u32)> {
    static WH: OnceLock<regex::Regex> = OnceLock::new();
    let c = WH.get_or_init(|| regex::Regex::new(r"^(\d{2,5})\s*[x×]\s*(\d{2,5})$").unwrap()).captures(arg.trim())?;
    Some((c[1].parse::<u32>().ok()?.clamp(200, 3840), c[2].parse::<u32>().ok()?.clamp(200, 2400)))
}

/// Load `target`, run `actions`, report. Err: no browser, it didn't start,
/// or the target can't be opened at all; an action failing is part of the
/// report.
pub fn browse(target: &str, actions: &[String], o: &Options) -> Result<Report, String> {
    let t0 = Instant::now();
    let url = to_url(target, o.base.as_deref(), &o.files)?;
    let deadline = t0 + o.timeout;
    let (_browser, bws) = launch(&o.exe, o.proxy.as_ref(), o.files.root.is_none(), Duration::from_secs(20))?;
    let ws = Ws::connect(&open_page(&bws)?, Duration::from_secs(10))?;
    let mut page = Page { ws, next: 0, events: Vec::new(), net: o.net.clone(), files: o.files.clone(), judged: HashMap::new(),
                          refused: Vec::new(), stop: o.stop.clone(), deadline: deadline + Duration::from_secs(20), frame: None };
    let short = Duration::from_secs(10);
    for d in ["Runtime", "Log", "Page", "Network"] {
        page.call(&format!("{d}.enable"), json!({}), short)?;
    }
    if o.net != Net::Any || o.files.root.is_some() {
        page.call("Fetch.enable", json!({"patterns": [{"urlPattern": "*"}]}), short)?;
    }
    // nothing downloads to the user's disk behind their back
    let _ = page.call("Browser.setDownloadBehavior", json!({"behavior": "deny"}), short);
    // headless Chrome announces itself as "HeadlessChrome" and some sites refuse that outright
    if let Ok(v) = page.call("Browser.getVersion", json!({}), short) {
        if let Some(ua) = v.get("userAgent").and_then(Value::as_str).filter(|u| u.contains("Headless")) {
            let _ = page.call("Network.setUserAgentOverride", json!({"userAgent": ua.replace("HeadlessChrome", "Chrome"),
                                                                     "acceptLanguage": "en-US,en;q=0.9"}), short);
        }
    }
    let metrics = |w: u32, h: u32, scale: f64| json!({"width": w, "height": h, "deviceScaleFactor": scale, "mobile": false});
    let mut size = o.size;
    page.call("Emulation.setDeviceMetricsOverride", metrics(size.0, size.1, 1.0), short)?;
    page.navigate(&url)?;
    page.pump(o.settle)?;
    let mut steps = Vec::new();
    for raw in actions.iter().map(|a| a.trim()).filter(|a| !a.is_empty()) {
        if Instant::now() >= deadline {
            steps.push(format!("{raw}: skipped (time limit)"));
            continue;
        }
        let (verb, arg) = raw.split_once(char::is_whitespace).map(|(v, a)| (v, a.trim())).unwrap_or((raw, ""));
        let num = |d: f64| -> Result<f64, String> {
            if arg.is_empty() { Ok(d) } else { arg.parse::<f64>().map_err(|_| format!("`{arg}` isn't a number")) }
        };
        let r: Result<String, String> = (|| {
            match verb.to_ascii_lowercase().as_str() {
                "click" => click(&mut page, arg).map(|_| "ok".into()),
                "type" => page.call("Input.insertText", json!({"text": arg}), short).map(|_| "ok".into()),
                "press" | "key" => {
                    for k in arg.split_whitespace() {
                        press(&mut page, k)?;
                    }
                    Ok("ok".into())
                }
                "wait" => page.pump(Duration::from_secs_f64((num(500.0)? / 1000.0).clamp(0.0, 10.0))).map(|_| "ok".into()),
                "unblock" => {
                    let end = Instant::now() + Duration::from_secs_f64((num(10000.0)? / 1000.0).clamp(0.0, 20.0));
                    while Instant::now() < end && challenged(page.evaluate(CHALLENGE_PROBE)?.as_str().unwrap_or("")) {
                        page.pump(Duration::from_millis(500))?;
                    }
                    Ok("ok".into())
                }
                "scroll" => page.evaluate(&format!("window.scrollBy(0, {})", num(600.0)? as i64)).map(|_| "ok".into()),
                "eval" | "js" => page.evaluate(arg).map(|v| match v {
                    Value::Null => "→ undefined".into(),
                    v => format!("→ {}", html::cut(&v.to_string(), 1500)),
                }),
                "goto" | "open" => {
                    let u = to_url(arg, o.base.as_deref(), &o.files)?;
                    page.navigate(&u).map(|_| "ok".into())
                }
                "size" => {
                    size = window_size(arg).ok_or("size wants WIDTHxHEIGHT")?;
                    page.call("Emulation.setDeviceMetricsOverride", metrics(size.0, size.1, 1.0), short).map(|_| "ok".into())
                }
                other => Err(format!("unknown action `{other}` (click, type, press, wait, scroll, eval, goto, size)")),
            }
        })();
        if r.as_ref().is_err_and(|e| e == "stopped") {
            return Err("stopped".into());
        }
        let _ = page.pump(Duration::from_millis(150));
        steps.push(format!("{raw}: {}", r.unwrap_or_else(|e| format!("FAILED: {e}"))));
    }
    page.pump(Duration::from_millis(200))?;
    let title = page.evaluate("document.title").ok().and_then(|v| v.as_str().map(String::from)).unwrap_or_default();
    let final_url = page.evaluate("location.href").ok().and_then(|v| v.as_str().map(String::from)).unwrap_or_else(|| url.clone());
    let text = page.evaluate(o.text_js.as_deref().unwrap_or("document.body ? document.body.innerText : ''")).ok()
        .and_then(|v| v.as_str().map(String::from)).unwrap_or_default();
    let shot = if o.screenshot {
        let scale = (SHOT_MAX / size.0.max(size.1) as f64).min(1.0);
        if scale < 1.0 {
            let _ = page.call("Emulation.setDeviceMetricsOverride", metrics(size.0, size.1, scale), short);
        }
        page.call("Page.captureScreenshot", json!({"format": "png"}), Duration::from_secs(30)).ok()
            .and_then(|r| r.get("data").and_then(Value::as_str).map(String::from))
            .and_then(|d| base64::engine::general_purpose::STANDARD.decode(d).ok())
    } else {
        None
    };
    let (errors, warnings, logs, failed) = collect(&page.events);
    let status = page.events.iter().rev().find(|e| e.get("method").and_then(Value::as_str) == Some("Network.responseReceived")
        && e.pointer("/params/type").and_then(Value::as_str) == Some("Document")
        && page.frame.as_deref().is_none_or(|f| e.pointer("/params/frameId").and_then(Value::as_str) == Some(f)))
        .and_then(|e| e.pointer("/params/response/status").and_then(Value::as_u64)).map(|s| s as u16);
    Ok(Report { url, final_url, title, text, errors, warnings, logs, failed, refused: dedup(page.refused.clone()), steps, shot, status,
                ms: t0.elapsed().as_millis() as u64, size, engine: engine_name(&o.exe) })
}

/// A page read in a real browser: its scripts run and a bot check is waited
/// out. -> (the report, whether it is still behind the check).
pub fn read_page(url: &str, o: &Options) -> Result<(Report, bool), String> {
    let mut o = o.clone();
    o.settle = Duration::from_millis(1200);
    o.screenshot = false;
    o.text_js = Some(READER_JS.into());
    let mut r = browse(url, &["unblock 10000".into()], &o)?;
    static SP: OnceLock<regex::Regex> = OnceLock::new();
    static NL: OnceLock<regex::Regex> = OnceLock::new();
    let t = SP.get_or_init(|| regex::Regex::new(r"[ \t\u{a0}]+").unwrap()).replace_all(&r.text, " ");
    r.text = NL.get_or_init(|| regex::Regex::new(r"\n\s*\n+").unwrap()).replace_all(&t, "\n\n").trim().to_string();
    let blocked = challenged(&format!("{}\n{}", r.title, html::cut(&r.text, 3000)));
    Ok((r, blocked))
}

/// The report as the model reads it.
pub fn report(r: &Report, text_cap: usize) -> String {
    let mut out = vec![format!("BROWSE {} ({}x{}, {} ms, {}) — title: {}", r.url, r.size.0, r.size.1, r.ms, r.engine,
                               if r.title.is_empty() { "(none)" } else { &r.title })];
    if r.final_url != r.url && !r.final_url.is_empty() {
        out.push(format!("Ended at: {}", r.final_url));
    }
    let list = |v: &[String], cap: usize| v.iter().take(cap).map(|x| format!("\n  {}", html::cut(x, 400))).collect::<String>();
    out.push(format!("JS errors ({}):{}", r.errors.len(), if r.errors.is_empty() { " none".into() } else { list(&r.errors, 20) }));
    if !r.failed.is_empty() {
        out.push(format!("Failed requests ({}):{}", r.failed.len(), list(&r.failed, 15)));
    }
    if !r.refused.is_empty() {
        out.push(format!("Not allowed to reach ({}):{}", r.refused.len(), list(&r.refused, 10)));
    }
    if !r.warnings.is_empty() {
        out.push(format!("Warnings ({}):{}", r.warnings.len(), list(&r.warnings, 10)));
    }
    if !r.logs.is_empty() {
        let skip = r.logs.len().saturating_sub(10);
        out.push(format!("Console log (last {}):{}", r.logs.len() - skip, r.logs[skip..].iter()
            .map(|x| format!("\n  {}", html::cut(x, 300))).collect::<String>()));
    }
    if !r.steps.is_empty() {
        out.push(format!("Actions:{}", r.steps.iter().map(|s| format!("\n  {s}")).collect::<String>()));
    }
    let t = r.text.trim();
    out.push(format!("Visible text: {}", if t.is_empty() { "(none — a canvas page, or nothing rendered)".to_string() } else {
        let c = html::cut(t, text_cap);
        format!("{c}{}", if c.len() < t.len() { " …" } else { "" })
    }));
    out.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keys() {
        assert_eq!(key_of("Enter").unwrap().2, 13);
        assert_eq!(key_of("a").unwrap(), ("a".into(), "KeyA".into(), 'A' as u32));
        assert_eq!(key_of("7").unwrap().1, "Digit7");
        assert!(key_of("Hyper").is_err());
    }

    #[test]
    fn urls_from_what_the_agent_wrote() {
        let any = Files::default();
        assert_eq!(to_url("https://x.org/a", None, &any).unwrap(), "https://x.org/a");
        assert_eq!(to_url("localhost:8000/x", None, &any).unwrap(), "http://localhost:8000/x");
        let d = std::env::temp_dir().join(format!("chuck-tourl-{}", std::process::id()));
        std::fs::create_dir_all(d.join("site dir")).unwrap();
        std::fs::write(d.join("site dir/index.html"), "<p>x</p>").unwrap();
        let u = to_url("site dir", Some(&d), &any).unwrap();
        assert!(u.starts_with("file:///") && u.ends_with("site%20dir/index.html"), "{u}");
        assert!(to_url("nope.html", Some(&d), &any).is_err());
        let fenced = Files { root: Some(d.join("site dir")), hidden: vec![] };
        assert!(to_url("site dir/index.html", Some(&d), &fenced).is_ok());
        std::fs::write(d.join("outside.html"), "x").unwrap();
        assert!(to_url("outside.html", Some(&d), &fenced).unwrap_err().contains("outside this folder"));
        assert!(to_url(&format!("file://{}/outside.html", d.display()), None, &fenced).is_err());
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn walls() {
        assert!(challenged("Just a moment...\nChecking your browser"));
        assert!(!challenged("An article about moments"));
    }

    #[test]
    fn events_become_a_report() {
        let ev = vec![
            json!({"method": "Runtime.exceptionThrown", "params": {"exceptionDetails": {"text": "Uncaught",
                "exception": {"description": "TypeError: x is undefined\n    at f (app.js:3)"}, "url": "file:///s/app.js", "lineNumber": 2}}}),
            json!({"method": "Runtime.consoleAPICalled", "params": {"type": "error", "args": [{"type": "string", "value": "bad"}, {"type": "number", "value": 2}]}}),
            json!({"method": "Runtime.consoleAPICalled", "params": {"type": "log", "args": [{"type": "undefined"}]}}),
            json!({"method": "Network.requestWillBeSent", "params": {"requestId": "1", "request": {"url": "http://x/a.js"}}}),
            json!({"method": "Network.loadingFailed", "params": {"requestId": "1", "errorText": "net::ERR_FILE_NOT_FOUND"}}),
            json!({"method": "Network.responseReceived", "params": {"response": {"url": "http://x/b", "status": 404}}}),
        ];
        let (e, _w, l, f) = collect(&ev);
        assert_eq!(e, vec!["Uncaught TypeError: x is undefined (app.js:3)", "console.error: bad 2"]);
        assert_eq!(l, vec!["undefined"]);
        assert_eq!(f, vec!["http://x/a.js: net::ERR_FILE_NOT_FOUND", "http://x/b: HTTP 404"]);
    }
}
