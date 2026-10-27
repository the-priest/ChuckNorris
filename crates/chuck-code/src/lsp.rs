//! "Where is this defined, who uses it, rename it everywhere" — by meaning,
//! through the language servers people already have: pyright, basedpyright,
//! pylsp or jedi for Python; typescript-language-server (or TypeScript 7's
//! own tsc) for JS/TS; rust-analyzer; gopls; clangd. A server answers by
//! meaning, so `refs save` finds calls to THIS save, not every word "save".
//!
//! Without one, definitions come from syntax and references from a
//! whole-word search, and the answer says so. Rename needs a real server: a
//! text-level rename is how unrelated code gets broken.
//!
//! One server per (folder, language), started on first use, run inside the
//! folder's sandbox when it has one (some servers run project code:
//! rust-analyzer builds proc macros), ended with the app.

use crate::syntax::{self, Def, Ref};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{channel, Sender};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

#[derive(Clone, Debug, PartialEq)]
pub struct LspError(pub String);

impl std::fmt::Display for LspError {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

fn err<T>(s: impl Into<String>) -> Result<T, LspError> {
    Err(LspError(s.into()))
}

/// (language, its file extensions, the servers to try in order)
pub type Lang = (&'static str, &'static [&'static str], &'static [&'static [&'static str]]);

pub const LANGS: &[Lang] = &[
    ("python", &["py", "pyi"], &[&["pyright-langserver", "--stdio"], &["basedpyright-langserver", "--stdio"], &["pylsp"],
                                 &["jedi-language-server"]]),
    ("typescript", &["ts", "tsx", "js", "jsx", "mjs", "cjs"], &[&["typescript-language-server", "--stdio"],
                                                               &["tsc", "--lsp", "--stdio"]]),
    ("rust", &["rs"], &[&["rust-analyzer"]]),
    ("go", &["go"], &[&["gopls"]]),
    ("c", &["c", "h", "cc", "cpp", "hpp", "cxx"], &[&["clangd"]]),
];

/// What to install, for the message when there is none.
pub fn install_hint(lang: &str) -> &'static str {
    match lang {
        "python" => "pyright (pip install --user pyright, or npm i -g pyright)",
        "typescript" => "TypeScript 7 (npm i -g typescript: its tsc is a language server) or typescript-language-server",
        "rust" => "rust-analyzer (rustup component add rust-analyzer)",
        "go" => "gopls (go install golang.org/x/tools/gopls@latest)",
        "c" => "clangd",
        _ => "a language server for it",
    }
}

pub fn lang_of(path: &str) -> Option<&'static str> {
    let ext = Path::new(path).extension()?.to_string_lossy().to_lowercase();
    LANGS.iter().find(|(_, exts, _)| exts.contains(&ext.as_str())).map(|(l, _, _)| *l)
}

fn lang_id(path: &str, lang: &str) -> String {
    match Path::new(path).extension().map(|e| e.to_string_lossy().to_lowercase()).as_deref() {
        Some("tsx") => "typescriptreact".into(),
        Some("js" | "mjs" | "cjs") => "javascript".into(),
        Some("jsx") => "javascriptreact".into(),
        Some("cc" | "cpp" | "hpp" | "cxx") => "cpp".into(),
        _ => lang.into(),
    }
}

/// A rustup proxy for an uninstalled rust-analyzer is on PATH and fails at
/// once; starting it would cost a timeout per question. Checked once.
fn runs(argv0: &str) -> Option<PathBuf> {
    static WORKS: OnceLock<Mutex<HashMap<String, Option<PathBuf>>>> = OnceLock::new();
    let mut w = WORKS.get_or_init(Default::default).lock().unwrap();
    if let Some(r) = w.get(argv0) {
        return r.clone();
    }
    let found = chuck_mcp_which(argv0).filter(|p| {
        let out = Command::new(p).arg("--version").stdin(Stdio::null()).output();
        match out {
            Ok(o) => {
                let e = String::from_utf8_lossy(&o.stderr).to_lowercase();
                // some servers (pyright-langserver) reject --version and still run
                o.status.success() || !(e.contains("unknown binary") || e.contains("not installed"))
            }
            Err(_) => false,
        }
    });
    w.insert(argv0.to_string(), found.clone());
    found
}

fn chuck_mcp_which(cmd: &str) -> Option<PathBuf> {
    std::env::split_paths(&std::env::var_os("PATH")?).map(|d| d.join(cmd)).find(|p| {
        use std::os::unix::fs::PermissionsExt;
        p.metadata().is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
    })
}

/// typescript-language-server needs a TypeScript: the project's, else the
/// global one's tsserver.js.
fn ts_options(root: &Path) -> Option<Value> {
    if root.join("node_modules/typescript/lib/tsserver.js").is_file() {
        return Some(json!({}));
    }
    for cand in ["tsserver", "tsc"] {
        if let Some(p) = chuck_mcp_which(cand).and_then(|p| p.canonicalize().ok()) {
            let lib = p.parent()?.parent()?.join("lib/tsserver.js");
            if lib.is_file() {
                return Some(json!({"tsserver": {"path": lib}}));
            }
        }
    }
    None
}

fn tsc_major() -> u32 {
    chuck_mcp_which("tsc").and_then(|p| Command::new(p).arg("--version").stdin(Stdio::null()).output().ok())
        .and_then(|o| String::from_utf8_lossy(&o.stdout).split_whitespace().last()
            .and_then(|v| v.split('.').next()?.parse().ok())).unwrap_or(0)
}

/// The argv of the first installed server for `lang` that can work in `root`.
pub fn server_for(lang: &str, root: &Path) -> Option<Vec<String>> {
    let (_, _, cands) = LANGS.iter().find(|(l, _, _)| *l == lang)?;
    for argv in cands.iter() {
        let Some(exe) = runs(argv[0]) else { continue };
        if argv[0] == "typescript-language-server" && ts_options(root).is_none() {
            continue;
        }
        if argv[0] == "tsc" && tsc_major() < 7 {
            continue; // TypeScript 7's native compiler is its own server; older ones aren't
        }
        let mut v = vec![exe.display().to_string()];
        v.extend(argv[1..].iter().map(|s| s.to_string()));
        return Some(v);
    }
    None
}

pub fn uri(p: &Path) -> String {
    let p = p.canonicalize().unwrap_or_else(|_| p.to_path_buf());
    let mut out = String::from("file://");
    for b in p.to_string_lossy().bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'/' | b'-' | b'_' | b'.' | b'~' => out.push(b as char),
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

pub fn path_of(u: &str) -> PathBuf {
    let p = u.strip_prefix("file://").unwrap_or(u);
    let b = p.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() && b[i + 1].is_ascii_hexdigit() && b[i + 2].is_ascii_hexdigit() {
            let h = |c: u8| (c as char).to_digit(16).unwrap() as u8;
            out.push(h(b[i + 1]) * 16 + h(b[i + 2]));
            i += 3;
        } else {
            out.push(b[i]);
            i += 1;
        }
    }
    PathBuf::from(String::from_utf8_lossy(&out).into_owned())
}

/// How a server's command line is wrapped (the folder's sandbox). None from
/// the wrap: it can't be run safely here.
pub type Wrap = Arc<dyn Fn(Vec<String>) -> Option<Vec<String>> + Send + Sync>;

type Waiting = Mutex<HashMap<u64, Sender<Value>>>;
/// (version, (mtime ns, size))
type Seen = (i64, (u128, u64));

/// One language server.
pub struct Client {
    pub name: String,
    pub lang: &'static str,
    pub root: PathBuf,
    stdin: Arc<Mutex<Option<ChildStdin>>>,
    waiting: Arc<Waiting>,
    next: AtomicU64,
    dead: Arc<AtomicBool>,
    /// what the server last saw of each file
    opened: Mutex<HashMap<PathBuf, Seen>>,
    pgid: i32,
    _guard: chuck_tools::Service,
}

fn frame(msg: &Value) -> Vec<u8> {
    let body = serde_json::to_vec(msg).unwrap_or_default();
    let mut out = format!("Content-Length: {}\r\n\r\n", body.len()).into_bytes();
    out.extend(body);
    out
}

fn write(stdin: &Mutex<Option<ChildStdin>>, msg: &Value) -> Result<(), LspError> {
    let mut g = stdin.lock().unwrap_or_else(|e| e.into_inner());
    let w = g.as_mut().ok_or_else(|| LspError("the server stopped".into()))?;
    w.write_all(&frame(msg)).and_then(|_| w.flush()).map_err(|e| LspError(format!("the server is gone ({e})")))
}

/// One message from the server (Content-Length framed), at most 64 MB.
fn read_msg(r: &mut impl BufRead) -> std::io::Result<Option<Value>> {
    let mut len: Option<usize> = None;
    loop {
        let mut line = String::new();
        if r.read_line(&mut line)? == 0 {
            return Ok(None);
        }
        let l = line.trim();
        if l.is_empty() {
            if len.is_some() {
                break;
            }
            continue;
        }
        if let Some(v) = l.to_ascii_lowercase().strip_prefix("content-length:") {
            len = v.trim().parse().ok();
        }
    }
    let n = len.unwrap_or(0);
    if n > 64 << 20 {
        std::io::copy(&mut r.by_ref().take(n as u64), &mut std::io::sink())?;
        return Ok(Some(Value::Null));
    }
    let mut buf = vec![0; n];
    r.read_exact(&mut buf)?;
    Ok(Some(serde_json::from_slice(&buf).unwrap_or(Value::Null)))
}

impl Client {
    pub fn start(argv: Vec<String>, root: &Path, lang: &'static str, wrap: Option<&Wrap>) -> Result<Client, LspError> {
        let root = root.canonicalize().map_err(|e| LspError(e.to_string()))?;
        let name = Path::new(&argv[0]).file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        let argv = match wrap {
            Some(w) => w(argv).ok_or_else(|| LspError("this folder's access needs bubblewrap to run a language server".into()))?,
            None => argv,
        };
        let (tx, rx) = channel();
        let r2 = root.clone();
        // started on a thread that lives as long as the server (the parent-death
        // signal follows the thread that started a process)
        std::thread::Builder::new().name(format!("lsp-{name}")).spawn(move || {
            let mut c = Command::new(&argv[0]);
            c.args(&argv[1..]).current_dir(&r2).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null());
            unsafe {
                c.pre_exec(|| {
                    libc::setsid();
                    libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL);
                    Ok(())
                });
            }
            match c.spawn() {
                Ok(mut child) => {
                    let _ = tx.send(Ok((child.stdin.take(), child.stdout.take(), child.id() as i32)));
                    let _ = child.wait();
                }
                Err(e) => {
                    let _ = tx.send(Err(e.to_string()));
                }
            }
        }).map_err(|e| LspError(e.to_string()))?;
        let (stdin, stdout, pid) = rx.recv_timeout(Duration::from_secs(10)).map_err(|_| LspError("didn't start".into()))?
            .map_err(LspError)?;
        let (Some(stdin), Some(stdout)) = (stdin, stdout) else { return err("no pipes to the server") };
        let stdin = Arc::new(Mutex::new(Some(stdin)));
        let waiting: Arc<Waiting> = Arc::default();
        let dead = Arc::new(AtomicBool::new(false));
        let (w, wt, d, r3) = (stdin.clone(), waiting.clone(), dead.clone(), root.clone());
        std::thread::Builder::new().name(format!("lsp-read-{name}")).spawn(move || {
            let mut r = BufReader::new(stdout);
            while let Ok(Some(msg)) = read_msg(&mut r) {
                let Some(id) = msg.get("id").cloned().filter(|i| !i.is_null()) else { continue };
                if let Some(method) = msg.get("method").and_then(Value::as_str) {
                    // the server asks us something: answer so it never waits on us
                    let params = msg.get("params").cloned().unwrap_or(Value::Null);
                    let result = match method {
                        "workspace/configuration" => Value::Array(vec![Value::Null;
                            params.get("items").and_then(Value::as_array).map_or(0, Vec::len)]),
                        "workspace/workspaceFolders" => json!([{"uri": uri(&r3), "name": r3.file_name()
                            .map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()}]),
                        _ => Value::Null, // registerCapability, workDoneProgress/create…
                    };
                    let _ = write(&w, &json!({"jsonrpc": "2.0", "id": id, "result": result}));
                    continue;
                }
                if let Some(n) = id.as_u64() {
                    if let Some(tx) = wt.lock().unwrap_or_else(|e| e.into_inner()).remove(&n) {
                        let _ = tx.send(msg);
                    }
                }
            }
            d.store(true, Ordering::SeqCst);
            wt.lock().unwrap_or_else(|e| e.into_inner()).clear(); // dropped senders wake every waiter
        }).map_err(|e| LspError(e.to_string()))?;
        let c = Client { name, lang, root: root.clone(), stdin, waiting, next: AtomicU64::new(0), dead,
                         opened: Mutex::default(), pgid: pid, _guard: chuck_tools::Service::register(pid) };
        let caps = json!({
            "textDocument": {"definition": {"linkSupport": false}, "references": {}, "rename": {"prepareSupport": false},
                             "documentSymbol": {"hierarchicalDocumentSymbolSupport": true},
                             "synchronization": {"didSave": false}},
            "workspace": {"symbol": {}, "workspaceEdit": {"documentChanges": true}, "workspaceFolders": true,
                          "configuration": true},
        });
        let folder = json!([{"uri": uri(&root), "name": root.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()}]);
        let init = c.request("initialize", json!({
            "processId": std::process::id(), "rootUri": uri(&root), "rootPath": root, "capabilities": caps,
            "clientInfo": {"name": "chuck"}, "initializationOptions": if lang == "typescript" { ts_options(&root) } else { None },
            "workspaceFolders": folder,
        }), Duration::from_secs(60));
        if let Err(e) = init {
            c.kill();
            return Err(e);
        }
        c.notify("initialized", json!({}))?;
        Ok(c)
    }

    pub fn alive(&self) -> bool {
        !self.dead.load(Ordering::SeqCst)
    }

    pub fn request(&self, method: &str, params: Value, timeout: Duration) -> Result<Value, LspError> {
        if !self.alive() {
            return err(format!("{} isn't running", self.name));
        }
        let id = self.next.fetch_add(1, Ordering::SeqCst) + 1;
        let (tx, rx) = channel();
        self.waiting.lock().unwrap_or_else(|e| e.into_inner()).insert(id, tx);
        write(&self.stdin, &json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}))?;
        let msg = rx.recv_timeout(timeout).map_err(|e| {
            self.waiting.lock().unwrap_or_else(|e| e.into_inner()).remove(&id);
            LspError(match e {
                std::sync::mpsc::RecvTimeoutError::Timeout => format!("{} didn't answer {method} within {}s", self.name,
                                                                      timeout.as_secs()),
                _ => format!("{} stopped", self.name),
            })
        })?;
        if let Some(e) = msg.get("error") {
            return err(format!("{}: {}", self.name, e.get("message").and_then(Value::as_str).unwrap_or("error")));
        }
        Ok(msg.get("result").cloned().unwrap_or(Value::Null))
    }

    pub fn notify(&self, method: &str, params: Value) -> Result<(), LspError> {
        write(&self.stdin, &json!({"jsonrpc": "2.0", "method": method, "params": params}))
    }

    /// didOpen, or didChange when the file changed on disk since the server
    /// last saw it, or didClose when it's gone.
    pub fn open(&self, path: &Path) {
        let path = path.canonicalize().unwrap_or_else(|_| path.to_path_buf());
        let mut opened = self.opened.lock().unwrap();
        let (text, stamp) = match (std::fs::read(&path), path.metadata()) {
            (Ok(b), Ok(m)) => (String::from_utf8_lossy(&b).into_owned(),
                               (m.modified().ok().and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                                    .map_or(0, |d| d.as_nanos()), m.len())),
            _ => {
                if opened.remove(&path).is_some() {
                    let _ = self.notify("textDocument/didClose", json!({"textDocument": {"uri": uri(&path)}}));
                }
                return;
            }
        };
        match opened.get(&path).cloned() {
            Some((_, seen)) if seen == stamp => {}
            Some((ver, _)) => {
                opened.insert(path.clone(), (ver + 1, stamp));
                let _ = self.notify("textDocument/didChange", json!({
                    "textDocument": {"uri": uri(&path), "version": ver + 1}, "contentChanges": [{"text": text}]}));
            }
            None => {
                opened.insert(path.clone(), (1, stamp));
                let p = path.to_string_lossy().into_owned();
                let _ = self.notify("textDocument/didOpen", json!({"textDocument": {
                    "uri": uri(&path), "languageId": lang_id(&p, self.lang), "version": 1, "text": text}}));
            }
        }
    }

    /// Re-send every open file that changed on disk (an agent's edits, a
    /// command's, yours), so no answer is about an old version.
    pub fn sync(&self) {
        let paths: Vec<PathBuf> = self.opened.lock().unwrap().keys().cloned().collect();
        for p in paths {
            self.open(&p);
        }
    }

    fn kill(&self) {
        self.stdin.lock().unwrap_or_else(|e| e.into_inner()).take();
        unsafe {
            libc::killpg(self.pgid, libc::SIGKILL);
        }
        self.dead.store(true, Ordering::SeqCst);
    }

    /// shutdown + exit, as the protocol asks; then the group goes regardless.
    pub fn close(&self) {
        if self.alive() {
            let _ = self.request("shutdown", Value::Null, Duration::from_secs(3));
            let _ = self.notify("exit", Value::Null);
            for _ in 0..20 {
                if !self.alive() {
                    break;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
        }
        self.kill();
    }
}

/// Servers per (folder, language), started on demand.
#[derive(Default)]
pub struct Manager {
    clients: Mutex<HashMap<(PathBuf, &'static str), Arc<Client>>>,
    /// why a server wouldn't start (asked again only after a restart)
    pub failed: Mutex<HashMap<(PathBuf, &'static str), String>>,
    wrap: Mutex<Option<Wrap>>,
    wrap_key: Mutex<String>,
    /// language -> the server to use instead of looking for one
    servers: Mutex<HashMap<String, Vec<String>>>,
}

impl Manager {
    pub fn new() -> Manager {
        Manager::default()
    }

    /// Use this server for `lang` (settings `lsp_servers`, or a test's).
    pub fn set_server(&self, lang: &str, argv: Vec<String>) {
        self.servers.lock().unwrap().insert(lang.to_string(), argv);
    }

    /// How servers are run from now on (the folder's sandbox), named by
    /// `key` (folder and access): running ones are restarted under a new
    /// one, and kept when it's the same.
    pub fn set_wrap(&self, key: &str, wrap: Option<Wrap>) {
        let mut k = self.wrap_key.lock().unwrap();
        if *k == key {
            return;
        }
        *k = key.to_string();
        *self.wrap.lock().unwrap() = wrap;
        drop(k);
        self.stop_all();
    }

    /// A running server, or None: none installed, or it failed to start
    /// (then the syntax and text fallbacks answer, and `failed` says why).
    pub fn client(&self, root: &Path, lang: &'static str) -> Option<Arc<Client>> {
        let key = (root.canonicalize().ok()?, lang);
        let mut cs = self.clients.lock().unwrap();
        if let Some(c) = cs.get(&key).filter(|c| c.alive()) {
            return Some(c.clone());
        }
        if self.failed.lock().unwrap().contains_key(&key) {
            return None;
        }
        let custom = self.servers.lock().unwrap().get(lang).cloned().filter(|a| !a.is_empty());
        let argv = custom.or_else(|| server_for(lang, &key.0))?;
        let wrap = self.wrap.lock().unwrap().clone();
        match Client::start(argv, &key.0, lang, wrap.as_ref()) {
            Ok(c) => {
                let c = Arc::new(c);
                cs.insert(key, c.clone());
                Some(c)
            }
            Err(e) => {
                self.failed.lock().unwrap().insert(key, e.0);
                None
            }
        }
    }

    pub fn stop_all(&self) {
        let all: Vec<Arc<Client>> = self.clients.lock().unwrap().drain().map(|(_, c)| c).collect();
        std::thread::scope(|s| {
            for c in &all {
                s.spawn(move || c.close());
            }
        });
        self.failed.lock().unwrap().clear();
    }
}

impl Drop for Manager {
    fn drop(&mut self) {
        self.stop_all();
    }
}

// ── positions ───────────────────────────────────────────────────────────

/// An LSP position (UTF-16 columns, as the protocol counts) -> a byte offset.
pub fn offset(text: &str, line: usize, character: usize) -> usize {
    let mut start = 0;
    for _ in 0..line {
        match text[start..].find('\n') {
            Some(i) => start += i + 1,
            None => return text.len(),
        }
    }
    let end = text[start..].find('\n').map_or(text.len(), |i| start + i);
    let mut units = 0;
    for (i, ch) in text[start..end].char_indices() {
        if units >= character {
            return start + i;
        }
        units += ch.len_utf16();
    }
    end
}

fn pos(v: &Value) -> (usize, usize) {
    (v["line"].as_u64().unwrap_or(0) as usize, v["character"].as_u64().unwrap_or(0) as usize)
}

/// The text a range covers.
pub fn range_text<'t>(text: &'t str, range: &Value) -> &'t str {
    let (a, b) = (pos(&range["start"]), pos(&range["end"]));
    let (a, b) = (offset(text, a.0, a.1), offset(text, b.0, b.1));
    &text[a.min(b)..b.max(a)]
}

/// Apply TextEdits (UTF-16 positions), last first.
pub fn apply_edits(text: &str, edits: &[Value]) -> String {
    let mut spans: Vec<(usize, usize, String)> = edits.iter().map(|e| {
        let (a, b) = (pos(&e["range"]["start"]), pos(&e["range"]["end"]));
        (offset(text, a.0, a.1), offset(text, b.0, b.1), e["newText"].as_str().unwrap_or("").to_string())
    }).collect();
    spans.sort_by(|x, y| y.0.cmp(&x.0).then(y.1.cmp(&x.1)));
    let mut out = text.to_string();
    for (a, b, new) in spans {
        out.replace_range(a..b.max(a), &new);
    }
    out
}

/// The UTF-16 column of `leaf` as a whole word at or after `col16` (where
/// the server's declaration starts), else anywhere on the line.
pub fn name_col(line: &str, leaf: &str, col16: usize) -> Option<usize> {
    let rx = regex::Regex::new(&format!(r"(?:^|[^\w$])({})(?:[^\w$]|$)", regex::escape(leaf))).ok()?;
    let from = offset(line, 0, col16);
    let m = rx.captures_at(line, from).or_else(|| rx.captures(line))?.get(1)?;
    Some(syntax::utf16(&line[..m.start()]))
}

const KINDS: &[&str] = &["", "file", "module", "namespace", "package", "class", "method", "property", "field",
    "constructor", "enum", "interface", "function", "variable", "constant", "string", "number", "boolean", "array",
    "object", "key", "null", "enum member", "struct", "event", "operator", "type parameter"];

// ── the questions ───────────────────────────────────────────────────────

/// The project's languages, most files first.
fn langs_of(root: &Path) -> Vec<&'static str> {
    let mut counts: HashMap<&'static str, usize> = HashMap::new();
    for rel in syntax::project_files(root, 5000) {
        if let Some(l) = lang_of(&rel) {
            *counts.entry(l).or_default() += 1;
        }
    }
    let mut v: Vec<_> = counts.into_iter().collect();
    v.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(b.0)));
    v.into_iter().map(|(l, _)| l).collect()
}

fn exts(lang: &str) -> &'static [&'static str] {
    LANGS.iter().find(|(l, _, _)| *l == lang).map_or(&[], |(_, e, _)| e)
}

fn dedupe(root: &Path, mut hits: Vec<Def>) -> Vec<Def> {
    hits.sort_by(|a, b| (a.path.as_str(), a.line).cmp(&(b.path.as_str(), b.line)));
    hits.dedup_by(|a, b| a.path == b.path && a.line == b.line);
    for h in &mut hits {
        if h.text.is_empty() {
            h.text = syntax::line_of(root, &h.path, h.line).trim().to_string();
        }
    }
    hits
}

/// -> (definitions, how they were found).
pub fn definitions(mgr: Option<&Manager>, root: &Path, name: &str, timeout: Duration) -> Result<(Vec<Def>, String), LspError> {
    let (cont, leaf) = syntax::split(name);
    if !syntax::valid_name(leaf) {
        return err(format!("`{name}` isn't a symbol name"));
    }
    let langs = langs_of(root);
    for &lang in &langs {
        let Some(c) = mgr.and_then(|m| m.client(root, lang)) else { continue };
        // some servers only index what is open: open a few files that mention it
        c.sync();
        let mentions = syntax::mentions(root, leaf, Some(exts(lang)));
        for rel in mentions.iter().take(12) {
            c.open(&root.join(rel));
        }
        let mut res = c.request("workspace/symbol", json!({"query": leaf}), timeout);
        for _ in 0..6 {
            // a server that just started is still indexing
            if mentions.is_empty() || res.as_ref().is_ok_and(|r| r.as_array().is_some_and(|a| !a.is_empty())) {
                break;
            }
            std::thread::sleep(Duration::from_millis(500));
            res = c.request("workspace/symbol", json!({"query": leaf}), timeout);
        }
        let Ok(res) = res else { continue };
        let mut hits = Vec::new();
        for s in res.as_array().into_iter().flatten() {
            let sname = s["name"].as_str().unwrap_or("");
            if sname != leaf && !sname.ends_with(&format!(".{leaf}")) {
                continue;
            }
            let container = s["containerName"].as_str().unwrap_or("").to_string();
            if cont.is_some_and(|c| container.rsplit('.').next() != Some(c)) {
                continue;
            }
            let loc = &s["location"];
            if loc.get("range").is_none() {
                continue;
            }
            let Some(rel) = syntax::rel_in(root, &path_of(loc["uri"].as_str().unwrap_or(""))) else {
                continue; // a library's, not the project's
            };
            let (line, col) = pos(&loc["range"]["start"]);
            let kind = KINDS.get(s["kind"].as_u64().unwrap_or(0) as usize).copied().filter(|k| !k.is_empty()).unwrap_or("symbol");
            hits.push(Def { path: rel, line: line + 1, col, kind: kind.into(), container, text: String::new() });
        }
        if !hits.is_empty() {
            return Ok((dedupe(root, hits), c.name.clone()));
        }
    }
    Ok((dedupe(root, syntax::defs(root, name)), "syntax (no language server)".into()))
}

/// -> (references, how, definitions). Definitions are left out of the references.
pub fn references(mgr: Option<&Manager>, root: &Path, name: &str, timeout: Duration, limit: usize)
    -> Result<(Vec<Ref>, String, Vec<Def>), LspError> {
    let (defs, how) = definitions(mgr, root, name, timeout)?;
    let (_, leaf) = syntax::split(name);
    if let (Some(d), false) = (defs.first(), how.starts_with("syntax")) {
        if let Some(c) = lang_of(&d.path).and_then(|l| mgr.and_then(|m| m.client(root, l))) {
            c.sync();
            for rel in syntax::mentions(root, leaf, Some(exts(c.lang))).iter().take(200) {
                c.open(&root.join(rel));
            }
            // the position must be ON the name: some servers point at the declaration's start
            let col = name_col(&syntax::line_of(root, &d.path, d.line), leaf, d.col).unwrap_or(d.col);
            let res = c.request("textDocument/references", json!({
                "textDocument": {"uri": uri(&root.join(&d.path))},
                "position": {"line": d.line - 1, "character": col},
                "context": {"includeDeclaration": false},
            }), timeout);
            if let Ok(res) = res {
                let mut refs: Vec<Ref> = Vec::new();
                for loc in res.as_array().into_iter().flatten() {
                    let Some(rel) = syntax::rel_in(root, &path_of(loc["uri"].as_str().unwrap_or(""))) else { continue };
                    let line = pos(&loc["range"]["start"]).0 + 1;
                    if refs.iter().any(|r| r.path == rel && r.line == line) || defs.iter().any(|x| x.path == rel && x.line == line) {
                        continue;
                    }
                    let text = syntax::line_of(root, &rel, line).trim().to_string();
                    refs.push(Ref { path: rel, line, text });
                }
                refs.sort_by(|a, b| (a.path.as_str(), a.line).cmp(&(b.path.as_str(), b.line)));
                refs.truncate(limit);
                return Ok((refs, c.name.clone(), defs));
            }
        }
    }
    Ok((syntax::text_refs(root, leaf, &defs, limit), "text search (no language server: whole-word matches)".into(), defs))
}

/// One file a rename changes: (relative path, before, after).
pub type FilePlan = (String, String, String);

/// What a rename would change, file by file, checked to be safe. Nothing is
/// written here.
pub fn rename(mgr: Option<&Manager>, root: &Path, name: &str, new_name: &str, timeout: Duration)
    -> Result<(Vec<FilePlan>, String), LspError> {
    if !syntax::valid_name(new_name) {
        return err(format!("`{new_name}` isn't a valid name"));
    }
    let (defs, how) = definitions(mgr, root, name, timeout)?;
    let Some(d) = defs.first() else { return err(format!("no definition of {name} found")) };
    if how.starts_with("syntax") {
        let lang = lang_of(&d.path).unwrap_or("?");
        return err(format!("rename needs a language server, so only real uses change: install {}", install_hint(lang)));
    }
    if defs.len() > 1 {
        let where_: Vec<String> = defs.iter().take(5).map(|d| format!("{}:{}", d.path, d.line)).collect();
        return err(format!("{name} is defined {} times ({}): say which, e.g. Class.method", defs.len(), where_.join(", ")));
    }
    let (_, leaf) = syntax::split(name);
    let c = lang_of(&d.path).and_then(|l| mgr.and_then(|m| m.client(root, l)))
        .ok_or_else(|| LspError("the language server stopped".into()))?;
    c.sync();
    for rel in syntax::mentions(root, leaf, Some(exts(c.lang))).iter().take(200) {
        c.open(&root.join(rel));
    }
    let col = name_col(&syntax::line_of(root, &d.path, d.line), leaf, d.col)
        .ok_or_else(|| LspError(format!("{}:{} no longer has {leaf} on it: try again", d.path, d.line)))?;
    let edit = c.request("textDocument/rename", json!({
        "textDocument": {"uri": uri(&root.join(&d.path))},
        "position": {"line": d.line - 1, "character": col},
        "newName": new_name,
    }), timeout)?;
    if edit.is_null() {
        return err(format!("{} found nothing to rename", c.name));
    }
    let mut per_file: HashMap<PathBuf, Vec<Value>> = HashMap::new();
    for (u, edits) in edit["changes"].as_object().into_iter().flatten() {
        per_file.entry(path_of(u)).or_default().extend(edits.as_array().cloned().unwrap_or_default());
    }
    for dc in edit["documentChanges"].as_array().into_iter().flatten() {
        if dc.get("textDocument").is_some() {
            per_file.entry(path_of(dc["textDocument"]["uri"].as_str().unwrap_or(""))).or_default()
                .extend(dc["edits"].as_array().cloned().unwrap_or_default());
        } else {
            return err(format!("{} wants to {} a file; do that rename by hand", c.name, dc["kind"].as_str().unwrap_or("change")));
        }
    }
    let mut files: Vec<(PathBuf, Vec<Value>)> = per_file.into_iter().collect();
    files.sort_by(|a, b| a.0.cmp(&b.0));
    let mut plan = Vec::new();
    for (p, edits) in files {
        let rel = syntax::rel_in(root, &p).ok_or_else(|| LspError(format!("the rename would change {}, outside the project",
                                                                           p.display())))?;
        let before = std::fs::read_to_string(root.join(&rel)).map_err(|e| LspError(format!("{rel}: {e}")))?;
        for e in &edits {
            // every edit must replace the name itself: anything else means the
            // server saw a different version of the file than the one on disk
            if range_text(&before, &e["range"]) != leaf {
                return err(format!("{rel} changed while renaming (the server and the file disagree): nothing was \
                    changed, try again"));
            }
        }
        let after = apply_edits(&before, &edits);
        if after != before {
            plan.push((rel, before, after));
        }
    }
    Ok((plan, c.name.clone()))
}

pub fn report_defs(name: &str, hits: &[Def], how: &str, cap: usize) -> String {
    if hits.is_empty() {
        return format!("DEFINITIONS of {name}: none found ({how}).");
    }
    let rows: Vec<String> = hits.iter().take(cap).map(|h| format!("{}:{}  {}   [{}{}]", h.path, h.line,
        h.text.chars().take(160).collect::<String>(), h.kind,
        if h.container.is_empty() { String::new() } else { format!(" in {}", h.container) })).collect();
    let more = if hits.len() > cap { format!("\n… {} more", hits.len() - cap) } else { String::new() };
    format!("DEFINITIONS of {name} ({}, via {how}):\n{}{more}", hits.len(), rows.join("\n"))
}

pub fn report_refs(name: &str, refs: &[Ref], how: &str, defs: &[Def], cap: usize) -> String {
    let head = format!("REFERENCES to {name} ({}, via {how}){}:", refs.len(),
                       defs.first().map(|d| format!("; defined at {}:{}", d.path, d.line)).unwrap_or_default());
    if refs.is_empty() {
        return format!("{head}\n(none outside the definition)");
    }
    let rows: Vec<String> = refs.iter().take(cap).map(|r| format!("{}:{}  {}", r.path, r.line,
        r.text.chars().take(160).collect::<String>())).collect();
    let more = if refs.len() > cap { format!("\n… {} more", refs.len() - cap) } else { String::new() };
    format!("{head}\n{}{more}", rows.join("\n"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn positions_count_utf16() {
        let t = "a😀b = 1\nsecond line\n";
        assert_eq!(offset(t, 0, 3), "a😀".len(), "the emoji is two UTF-16 units");
        assert_eq!(offset(t, 1, 0), t.find("second").unwrap());
        assert_eq!(offset(t, 1, 99), t.find("second line").unwrap() + "second line".len());
        assert_eq!(offset(t, 9, 0), t.len());
        let edits = vec![json!({"range": {"start": {"line": 0, "character": 3}, "end": {"line": 0, "character": 4}}, "newText": "bee"}),
                         json!({"range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 6}}, "newText": "2nd"})];
        assert_eq!(apply_edits(t, &edits), "a😀bee = 1\n2nd line\n");
        assert_eq!(range_text(t, &edits[0]["range"]), "b");
        assert_eq!(name_col("    def save(self):", "save", 4), Some(8));
        assert_eq!(name_col("x = saver + save", "save", 0), Some(12));
        assert_eq!(name_col("nothing", "save", 0), None);
        assert_eq!(lang_of("src/a.TSX"), Some("typescript"));
        assert_eq!(path_of(&uri(Path::new("/tmp"))), Path::new("/tmp").canonicalize().unwrap());
        assert_eq!(path_of("file:///a%20b/c%C3%A9.py"), PathBuf::from("/a b/cé.py"));
    }
}
