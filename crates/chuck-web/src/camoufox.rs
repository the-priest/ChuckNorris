//! Camoufox: a Firefox build sites can't tell from a person, the best
//! browser for reading pages that turn bots away.
//!
//! It is launched by its own Python package (which builds it a consistent
//! fingerprint) through Playwright, so Chuck drives it with a small bridge
//! script ([`BRIDGE`]) run by that package's Python: the request goes in
//! as a line of JSON, and the result comes back the same way. Every request
//! the page makes is asked of Chuck first and decided by the same
//! [`Judge`](crate::browser) as for Chromium, so research stays on the
//! public internet and an offline folder stays offline.
//!
//! Firefox can't be told to keep a page to one folder's files, so a page
//! from a restricted folder is served instead, from 127.0.0.1 at a random
//! port and an unguessable path, by a server that hands out that folder's
//! files and nothing else.
//!
//! The installer sets Camoufox up in its own environment
//! (`~/.local/share/chucknorris/camoufox`); a `camoufox` importable by the
//! system's `python3` works too.

use crate::browser::{Files, Judge, Net, Options, Report};
use crate::html;
use base64::Engine as _;
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, Receiver, RecvTimeoutError};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

/// The script the Python side runs.
pub const BRIDGE: &str = include_str!("camoufox_bridge.py");

/// How to set it up, for messages.
pub const INSTALL: &str = "re-run Chuck's installer (it sets Camoufox up), or: python3 -m venv \
    ~/.local/share/chucknorris/camoufox && ~/.local/share/chucknorris/camoufox/bin/pip install 'camoufox[geoip]' && \
    ~/.local/share/chucknorris/camoufox/bin/python -m camoufox fetch";

/// A Camoufox Chuck can run.
#[derive(Clone, Debug, PartialEq)]
pub struct Install {
    /// The Python whose `camoufox` package launches it.
    pub python: PathBuf,
    /// The browser binary: the user's choice, or the build the package has
    /// downloaded. Always handed to the package, so a launch never starts a
    /// download of its own (300 MB, cut off by the call's time limit, again
    /// on every page).
    pub exe: Option<PathBuf>,
    /// uBlock Origin is unpacked: without it the package would fetch it at
    /// launch, so it is left out instead.
    pub addons: bool,
    /// A current GeoIP database is there (else the package downloads one at
    /// launch): a proxy's country then sets the time zone and language.
    pub geoip: bool,
}

/// Where the installer puts Camoufox's own Python environment.
pub fn venv() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"))
        .join(".local/share/chucknorris/camoufox")
}

/// Asks a Python what its camoufox package has, without it downloading
/// anything: one JSON line.
const PROBE: &str = r#"
import json, os, sys
def say(**kw):
    print(json.dumps(kw)); sys.exit(0)
try:
    from camoufox.pkgman import camoufox_path, launch_path
except Exception as e:
    say(package=False, why=str(e).splitlines()[0] if str(e) else type(e).__name__)
try:
    path = camoufox_path(download_if_missing=False)
    try:
        exe = launch_path(path)
    except TypeError:
        exe = launch_path()
except Exception as e:
    say(package=True, why=(str(e).splitlines() or [type(e).__name__])[0])
addons = geoip = False
try:
    from camoufox.addons import get_addon_path
    addons = os.path.exists(os.path.join(get_addon_path("UBO"), "manifest.json"))
except Exception:
    pass
try:
    from camoufox.geolocation import get_mmdb_path, needs_update
    geoip = get_mmdb_path("ipv4").exists() and not needs_update()
except Exception:
    pass
say(package=True, exe=exe, addons=addons, geoip=geoip)
"#;

/// What a Python's camoufox package has: Ok(the browser and its extras),
/// Err((the package is there, why it can't run)). At most 30 s.
fn probe(python: &Path) -> Result<(PathBuf, bool, bool), (bool, String)> {
    let mut c = Command::new(python);
    // run from an empty place, without the caller's import paths (a user-site install still counts)
    c.args(["-c", PROBE]).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null())
        .current_dir(std::env::temp_dir()).env_remove("PYTHONPATH").env_remove("PYTHONSTARTUP");
    chuck_tools::tag(&mut c);
    let Ok(mut child) = c.spawn() else { return Err((false, format!("{} doesn't run", python.display()))) };
    let end = Instant::now() + Duration::from_secs(30);
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if Instant::now() < end => std::thread::sleep(Duration::from_millis(25)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return Err((false, format!("{} took over 30 s to answer", python.display())));
            }
        }
    }
    let mut out = String::new();
    if let Some(mut o) = child.stdout.take() {
        let _ = o.read_to_string(&mut out);
    }
    let v: Value = out.lines().rev().find_map(|l| serde_json::from_str(l).ok())
        .ok_or_else(|| (false, "no camoufox package".to_string()))?;
    let package = v["package"].as_bool().unwrap_or(false);
    match v["exe"].as_str() {
        Some(exe) => Ok((PathBuf::from(exe), v["addons"].as_bool().unwrap_or(false), v["geoip"].as_bool().unwrap_or(false))),
        None => Err((package, v["why"].as_str().unwrap_or("").to_string())),
    }
}

/// What `python`'s camoufox package can run, looked at without downloading
/// anything: Err says what is missing and how to get it.
pub fn check(python: &Path) -> Result<Install, String> {
    match probe(python) {
        Ok((exe, addons, geoip)) => Ok(Install { python: python.to_path_buf(), exe: Some(exe), addons, geoip }),
        Err((true, why)) => Err(format!("Camoufox's browser isn't downloaded ({}): run `{} -m camoufox fetch`, or \
            re-run Chuck's installer", why.trim_end_matches('.'), python.display())),
        Err((false, _)) => Err(format!("Camoufox isn't installed: {INSTALL}")),
    }
}

/// When Camoufox was last looked for, and what was found.
type Seen = Option<(Instant, Result<Install, String>)>;

/// Camoufox as installed: its own environment first, then the system's
/// Python. Answers are remembered for a while (Settings, the doctor and
/// every page read ask), a no for less long, so setting it up is noticed.
fn installed() -> Result<Install, String> {
    static SEEN: OnceLock<Mutex<Seen>> = OnceLock::new();
    let seen = SEEN.get_or_init(|| Mutex::new(None));
    if let Some((at, found)) = seen.lock().unwrap_or_else(|e| e.into_inner()).as_ref() {
        if at.elapsed() < if found.is_ok() { Duration::from_secs(600) } else { Duration::from_secs(30) } {
            return found.clone();
        }
    }
    let mut candidates = vec![venv().join("bin/python")];
    if let Some(p) = std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()).map(|d| d.join("python3"))
        .find(|p| p.is_file())
    {
        candidates.push(p);
    }
    let mut found = Err(format!("Camoufox isn't installed: {INSTALL}"));
    for py in candidates.into_iter().filter(|p| p.is_file()) {
        match check(&py) {
            Ok(i) => {
                found = Ok(i);
                break;
            }
            // the package without its browser: that is what to say, not "not installed"
            Err(e) if e.starts_with("Camoufox's browser") && found.as_ref().is_err_and(|f| f.contains("isn't installed")) => {
                found = Err(e);
            }
            Err(_) => {}
        }
    }
    *seen.lock().unwrap_or_else(|e| e.into_inner()) = Some((Instant::now(), found.clone()));
    found
}

/// Camoufox, ready to run (`exe`: a binary the user named).
pub fn find(exe: Option<PathBuf>) -> Result<Install, String> {
    if let Some(e) = &exe {
        if !e.is_file() {
            return Err(format!("Camoufox not found at {}", e.display()));
        }
    }
    let mut i = installed()?;
    if exe.is_some() {
        i.exe = exe;
    }
    Ok(i)
}

// ── a folder's files over HTTP, and nothing else ─────────────────────────

struct FileServer {
    /// "http://127.0.0.1:PORT/TOKEN/"
    prefix: String,
    root: PathBuf,
    stop: Arc<AtomicBool>,
}

impl Drop for FileServer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
    }
}

fn token() -> String {
    use std::hash::{BuildHasher, Hasher};
    let mut out = String::new();
    for i in 0..2u8 {
        let mut h = std::collections::hash_map::RandomState::new().build_hasher();
        h.write_u8(i);
        h.write_u128(std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_nanos());
        out.push_str(&format!("{:016x}", h.finish()));
    }
    out
}

fn content_type(p: &Path) -> &'static str {
    match p.extension().and_then(|e| e.to_str()).map(str::to_ascii_lowercase).as_deref() {
        Some("html" | "htm") => "text/html; charset=utf-8",
        Some("js" | "mjs") => "text/javascript; charset=utf-8",
        Some("css") => "text/css; charset=utf-8",
        Some("json" | "map") => "application/json",
        Some("svg") => "image/svg+xml",
        Some("png") => "image/png",
        Some("jpg" | "jpeg") => "image/jpeg",
        Some("gif") => "image/gif",
        Some("webp") => "image/webp",
        Some("ico") => "image/x-icon",
        Some("wasm") => "application/wasm",
        Some("woff2") => "font/woff2",
        Some("woff") => "font/woff",
        Some("ttf") => "font/ttf",
        Some("txt" | "md") => "text/plain; charset=utf-8",
        Some("xml") => "application/xml",
        Some("mp3") => "audio/mpeg",
        Some("ogg") => "audio/ogg",
        Some("wav") => "audio/wav",
        Some("mp4") => "video/mp4",
        Some("webm") => "video/webm",
        _ => "application/octet-stream",
    }
}

fn answer(mut s: TcpStream, files: &Files, root: &Path, token: &str) {
    s.set_read_timeout(Some(Duration::from_secs(10))).ok();
    let mut r = BufReader::new(match s.try_clone() {
        Ok(c) => c,
        Err(_) => return,
    });
    let mut line = String::new();
    if r.read_line(&mut line).is_err() {
        return;
    }
    loop {
        let mut h = String::new();
        match r.read_line(&mut h) {
            Ok(0) | Err(_) => break,
            Ok(_) if h.trim().is_empty() => break,
            Ok(_) => {}
        }
    }
    let mut parts = line.split_whitespace();
    let (method, target) = (parts.next().unwrap_or(""), parts.next().unwrap_or("/"));
    let path = target.split(['?', '#']).next().unwrap_or("");
    let found = path.strip_prefix(&format!("/{token}/")).and_then(|rel| {
        let rel = crate::browser::unpercent(rel);
        // the folder itself, or what is in it, never above or beside it
        if rel.split('/').any(|seg| seg == "..") {
            return None;
        }
        let mut p = root.join(rel.trim_start_matches('/'));
        if p.is_dir() {
            p = p.join("index.html");
        }
        (p.is_file() && files.allowed(&p)).then_some(p)
    });
    let body = found.as_ref().and_then(|p| std::fs::read(p).ok());
    let head = match (&found, &body) {
        (Some(p), Some(b)) => format!("HTTP/1.1 200 OK\r\nContent-Type: {}\r\nContent-Length: {}\r\nCache-Control: no-store\r\n\
            Connection: close\r\n\r\n", content_type(p), b.len()),
        _ => "HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\nContent-Length: 9\r\nConnection: close\r\n\r\nnot found".into(),
    };
    let _ = s.write_all(head.as_bytes());
    if method != "HEAD" {
        if let Some(b) = body {
            let _ = s.write_all(&b);
        }
    }
}

fn serve(files: &Files) -> Result<FileServer, String> {
    let root = files.root.clone().ok_or("no folder to serve")?;
    let root = std::fs::canonicalize(&root).map_err(|e| format!("{}: {e}", root.display()))?;
    let l = TcpListener::bind("127.0.0.1:0").map_err(|e| e.to_string())?;
    l.set_nonblocking(true).map_err(|e| e.to_string())?;
    let port = l.local_addr().map_err(|e| e.to_string())?.port();
    let tok = token();
    let stop = Arc::new(AtomicBool::new(false));
    let (st, files, r, t) = (stop.clone(), Files { root: Some(root.clone()), hidden: files.hidden.clone() }, root.clone(), tok.clone());
    std::thread::Builder::new().name("camoufox-files".into()).spawn(move || {
        while !st.load(Ordering::SeqCst) {
            match l.accept() {
                Ok((s, _)) => {
                    s.set_nonblocking(false).ok();
                    let (f, r, t) = (files.clone(), r.clone(), t.clone());
                    std::thread::spawn(move || answer(s, &f, &r, &t));
                }
                Err(_) => std::thread::sleep(Duration::from_millis(20)),
            }
        }
    }).map_err(|e| e.to_string())?;
    Ok(FileServer { prefix: format!("http://127.0.0.1:{port}/{tok}/"), root, stop })
}

impl FileServer {
    /// A file:// address in the folder as the server's.
    fn to_http(&self, url: &str) -> String {
        let Some(rest) = url.strip_prefix("file://") else { return url.to_string() };
        let (path, tail) = rest.find(['?', '#']).map_or((rest, ""), |i| (&rest[..i], &rest[i..]));
        let path = crate::browser::unpercent(path);
        let real = std::fs::canonicalize(&path).unwrap_or_else(|_| PathBuf::from(&path));
        match real.strip_prefix(&self.root) {
            Ok(rel) => format!("{}{}{tail}", self.prefix,
                               rel.to_string_lossy().split('/').map(crate::enc).collect::<Vec<_>>().join("/")),
            Err(_) => url.to_string(),
        }
    }

    /// The server's address back as the file it is.
    fn to_file(&self, s: &str) -> String {
        s.replace(&self.prefix, &format!("file://{}/", self.root.display()))
    }
}

// ── the bridge process ───────────────────────────────────────────────────

struct Bridge {
    child: Child,
    dir: PathBuf,
    /// "CHUCK_BROWSE=<id>": what the bridge and everything it starts carry.
    marker: String,
    _guard: chuck_tools::Service,
}

impl Drop for Bridge {
    fn drop(&mut self) {
        // the bridge's group, then whatever it started elsewhere (Playwright puts the browser in a group of its own)
        unsafe {
            libc::killpg(self.child.id() as i32, libc::SIGKILL);
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
        chuck_tools::kill_env(&self.marker);
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

enum Line {
    Out(String),
    /// The bridge's stdout closed (it exited).
    Gone,
}

/// A bridge at work: what goes to it, what comes from it, and its log's tail.
struct Running {
    _bridge: Bridge,
    stdin: std::process::ChildStdin,
    lines: Receiver<Line>,
    log: Arc<Mutex<Vec<String>>>,
}

fn start(python: &Path) -> Result<Running, String> {
    static N: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    let id = format!("{}-{}-{}", std::process::id(), N.fetch_add(1, Ordering::SeqCst), &token()[..8]);
    // an empty folder of its own to run in: nothing there for Python to import by accident
    let dir = std::env::temp_dir().join(format!("chuck-camoufox-{id}"));
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let marker = format!("CHUCK_BROWSE={id}");
    let mut c = Command::new(python);
    chuck_tools::tag(&mut c);
    c.args(["-c", BRIDGE]).current_dir(&dir).env("CHUCK_BROWSE", &id).env("PYTHONDONTWRITEBYTECODE", "1")
        .env("PYTHONUNBUFFERED", "1").env_remove("PYTHONPATH").env_remove("PYTHONSTARTUP")
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
    unsafe {
        c.pre_exec(|| {
            libc::setsid();
            libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL);
            Ok(())
        });
    }
    let mut child = c.spawn().map_err(|e| {
        let _ = std::fs::remove_dir_all(&dir);
        format!("couldn't start Camoufox's Python ({}): {e}", python.display())
    })?;
    let guard = chuck_tools::Service::register(child.id() as i32);
    let (stdin, stdout, stderr) = (child.stdin.take(), child.stdout.take(), child.stderr.take());
    let bridge = Bridge { child, dir, marker, _guard: guard };
    let stdin = stdin.ok_or("no pipe to Camoufox's Python")?;
    let (tx, rx) = channel();
    if let Some(out) = stdout {
        std::thread::Builder::new().name("camoufox-out".into()).spawn(move || {
            for line in BufReader::new(out).lines() {
                let Ok(line) = line else { break };
                if tx.send(Line::Out(line)).is_err() {
                    return;
                }
            }
            let _ = tx.send(Line::Gone);
        }).map_err(|e| e.to_string())?;
    }
    // its log, kept short, for when it fails; read to the end so a full pipe never freezes it
    let log = Arc::new(Mutex::new(Vec::new()));
    if let Some(mut err) = stderr {
        let log = log.clone();
        std::thread::Builder::new().name("camoufox-log".into()).spawn(move || {
            let mut buf = Vec::new();
            let _ = err.read_to_end(&mut buf);
            let text = String::from_utf8_lossy(&buf);
            let mut l = log.lock().unwrap_or_else(|e| e.into_inner());
            l.extend(text.lines().rev().filter(|x| !x.trim().is_empty()).take(12).map(String::from));
            l.reverse();
        }).map_err(|e| e.to_string())?;
    }
    Ok(Running { _bridge: bridge, stdin, lines: rx, log })
}

fn strings(v: &Value, key: &str) -> Vec<String> {
    v.get(key).and_then(Value::as_array).map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
        .unwrap_or_default()
}

/// What `browser::browse` does, in Camoufox.
pub fn browse(target: &str, actions: &[String], o: &Options, install: &Install) -> Result<Report, String> {
    let t0 = Instant::now();
    let mut url = crate::browser::to_url(target, o.base.as_deref(), &o.files)?;
    let original = url.clone();
    let mut judge = Judge::new(o.net.clone(), o.files.clone());
    // a restricted folder's files come from a server that has nothing else
    let server = match &o.files.root {
        Some(root) if root.is_dir() => Some(serve(&o.files)?),
        _ => None,
    };
    if let Some(sv) = &server {
        url = sv.to_http(&url);
        judge.own = Some(sv.prefix.to_ascii_lowercase());
    }
    // the actions, with every address already resolved (and refused here when it may not be opened)
    let mut size = o.size;
    let acts: Vec<Value> = actions.iter().map(|a| a.trim()).filter(|a| !a.is_empty()).map(|raw| {
        let (verb, arg) = raw.split_once(char::is_whitespace).map(|(v, a)| (v, a.trim())).unwrap_or((raw, ""));
        let verb = verb.to_ascii_lowercase();
        let mut a = json!({"raw": raw, "verb": verb, "arg": arg});
        match verb.as_str() {
            "goto" | "open" => match crate::browser::to_url(arg, o.base.as_deref(), &o.files) {
                Ok(u) => a["arg"] = json!(server.as_ref().map_or(u.clone(), |sv| sv.to_http(&u))),
                Err(e) => a["error"] = json!(e),
            },
            "size" => match crate::browser::window_size(arg) {
                Some((w, h)) => {
                    size = (w, h);
                    a["width"] = json!(w);
                    a["height"] = json!(h);
                }
                None => a["error"] = json!("size wants WIDTHxHEIGHT"),
            },
            "wait" | "scroll" | "unblock" if !arg.is_empty() && arg.parse::<f64>().is_err() => {
                a["error"] = json!(format!("`{arg}` isn't a number"));
            }
            _ => {}
        }
        a
    }).collect();
    let judging = o.net != Net::Any || server.is_some() || o.files.root.is_some();
    let scale = (crate::browser::SHOT_MAX / o.size.0.max(o.size.1) as f64).min(1.0);
    let proxy = match &o.proxy {
        Some(crate::Proxy::Url(u)) => Some(u.clone()),
        _ => None,
    };
    let req = json!({
        "url": url, "actions": acts, "width": o.size.0, "height": o.size.1, "scale": scale,
        // reading a page: the window its fingerprint says; checking one: the size asked for
        "viewport": o.screenshot || actions.iter().any(|a| a.trim_start().to_ascii_lowercase().starts_with("size")),
        "settle_ms": o.settle.as_millis() as u64, "timeout_ms": o.timeout.as_millis() as u64,
        "text_js": o.text_js.as_deref().unwrap_or("document.body ? document.body.innerText : ''"),
        "probe": crate::browser::CHALLENGE_PROBE, "challenge": crate::browser::CHALLENGE,
        "screenshot": o.screenshot, "judge": judging, "proxy": proxy,
        "exe": install.exe.as_ref().map(|e| e.display().to_string()),
        "addons": install.addons, "geoip": install.geoip,
    });
    let Running { _bridge, mut stdin, lines: rx, log } = start(&install.python)?;
    writeln!(stdin, "{req}").map_err(|e| format!("Camoufox's Python: {e}"))?;
    // the browser takes a while to start; the call's own limit is on top of that
    let deadline = t0 + o.timeout + Duration::from_secs(40);
    let stopped = || o.stop.as_ref().is_some_and(|s| s.load(Ordering::SeqCst));
    let result = loop {
        if stopped() {
            return Err("stopped".into());
        }
        let left = deadline.saturating_duration_since(Instant::now());
        if left.is_zero() {
            return Err(format!("Camoufox didn't finish within {}s", (o.timeout + Duration::from_secs(40)).as_secs()));
        }
        match rx.recv_timeout(left.min(Duration::from_millis(250))) {
            Ok(Line::Out(line)) => {
                let Ok(v) = serde_json::from_str::<Value>(&line) else { continue };
                if let Some(u) = v.get("judge").and_then(Value::as_str) {
                    let ok = judge.allow(u);
                    let answer = json!({"id": v.get("id").cloned().unwrap_or(Value::Null), "ok": ok});
                    writeln!(stdin, "{answer}").map_err(|e| format!("Camoufox's Python: {e}"))?;
                } else if let Some(r) = v.get("result") {
                    break r.clone();
                } else if let Some(e) = v.get("error").and_then(Value::as_str) {
                    return Err(format!("Camoufox: {e}"));
                }
            }
            Ok(Line::Gone) => {
                // give the log reader a moment to catch the last lines
                std::thread::sleep(Duration::from_millis(150));
                let tail = log.lock().unwrap_or_else(|e| e.into_inner()).join(" | ");
                return Err(format!("Camoufox's Python stopped without an answer{}",
                                   if tail.is_empty() { String::new() } else { format!(": {}", html::cut(&tail, 600)) }));
            }
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => return Err("Camoufox's Python went away".into()),
        }
    };
    let back = |s: String| server.as_ref().map_or(s.clone(), |sv| sv.to_file(&s));
    let backs = |v: Vec<String>| v.into_iter().map(back).collect::<Vec<_>>();
    if let Some(e) = result.get("nav_error").and_then(Value::as_str) {
        // the first load failing is the call failing, as for Chromium
        if judge.refused.iter().any(|r| url.starts_with(r.as_str()) || r.starts_with(url.as_str())) {
            return Err(format!("{original} may not be reached from here ({})", judge.why()));
        }
        return Err(format!("couldn't load {original}: {}", back(e.to_string())));
    }
    let shot = result.get("shot").and_then(Value::as_str)
        .and_then(|d| base64::engine::general_purpose::STANDARD.decode(d).ok());
    let final_url = back(result.get("final_url").and_then(Value::as_str).unwrap_or(&url).to_string());
    Ok(Report {
        url: original,
        final_url,
        title: result.get("title").and_then(Value::as_str).unwrap_or("").to_string(),
        text: result.get("text").and_then(Value::as_str).unwrap_or("").to_string(),
        errors: backs(strings(&result, "errors")),
        warnings: backs(strings(&result, "warnings")),
        logs: strings(&result, "logs"),
        failed: backs(strings(&result, "failed")),
        refused: crate::browser::dedup(judge.refused.clone()),
        steps: backs(strings(&result, "steps")),
        shot,
        status: result.get("status").and_then(Value::as_u64).map(|s| s as u16),
        ms: t0.elapsed().as_millis() as u64,
        size,
        engine: "Camoufox".into(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn get(url: &str) -> (u16, String) {
        let rest = url.strip_prefix("http://").unwrap();
        let (auth, path) = rest.split_once('/').unwrap();
        let mut s = TcpStream::connect(auth).unwrap();
        write!(s, "GET /{path} HTTP/1.1\r\nHost: {auth}\r\n\r\n").unwrap();
        let mut out = String::new();
        s.read_to_string(&mut out).unwrap();
        let code = out.split_whitespace().nth(1).unwrap().parse().unwrap();
        (code, out.split("\r\n\r\n").nth(1).unwrap_or("").to_string())
    }

    #[test]
    fn the_folder_server_hands_out_the_folder_and_nothing_else() {
        let d = std::env::temp_dir().join(format!("chuck-fox-serve-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(d.join("site/sub dir")).unwrap();
        std::fs::create_dir_all(d.join("site/.secret")).unwrap();
        std::fs::write(d.join("site/index.html"), "<p>home</p>").unwrap();
        std::fs::write(d.join("site/sub dir/a.js"), "x=1").unwrap();
        std::fs::write(d.join("site/.secret/key"), "k").unwrap();
        std::fs::write(d.join("beside.txt"), "no").unwrap();
        std::os::unix::fs::symlink(d.join("beside.txt"), d.join("site/link.txt")).unwrap();
        let root = std::fs::canonicalize(d.join("site")).unwrap();
        let files = Files { root: Some(root.clone()), hidden: vec![root.join(".secret")] };
        let sv = serve(&files).unwrap();
        assert_eq!(get(&sv.prefix), (200, "<p>home</p>".into()));
        assert_eq!(get(&format!("{}sub%20dir/a.js", sv.prefix)).0, 200);
        for bad in ["../beside.txt", "sub%20dir/../../beside.txt", "%2e%2e/beside.txt", ".secret/key", "link.txt", "nope.html"] {
            assert_eq!(get(&format!("{}{bad}", sv.prefix)).0, 404, "{bad}");
        }
        // without the token: nothing, even inside the folder
        let bare = sv.prefix.rsplitn(3, '/').nth(2).unwrap().to_string();
        assert_eq!(get(&format!("{bare}/index.html")).0, 404);
        // file:// addresses map to it and back
        let f = format!("file://{}/sub%20dir/a.js?v=1", root.display());
        let h = sv.to_http(&f);
        assert_eq!(h, format!("{}sub%20dir/a.js?v=1", sv.prefix));
        assert_eq!(sv.to_file(&format!("{h} failed")), format!("file://{}/sub%20dir/a.js?v=1 failed", root.display()));
        assert_eq!(sv.to_http("file:///etc/passwd"), "file:///etc/passwd", "outside the folder: not the server's");
        drop(sv);
        let _ = std::fs::remove_dir_all(&d);
    }
}
