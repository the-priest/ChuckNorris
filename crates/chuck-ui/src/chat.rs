//! Conversations kept on disk, one per project folder, so nothing you and
//! the fleet said disappears when the window closes. Everything shown in the
//! conversation is an entry here, and is drawn from the entry both live and
//! when it's opened again.

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

const KEEP: usize = 4000;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "k", rename_all = "snake_case")]
pub enum Entry {
    User { text: String },
    /// A message typed to the Lead while it worked.
    Steer { text: String },
    Lead {
        text: String,
        model: String,
        /// Said in Chat mode (by Chuck, not the fleet's Lead).
        #[serde(default)]
        chat: bool,
    },
    /// Chat mode proposed work for the fleet: a button sends it.
    Handoff { request: String },
    /// One line of the work lane (Pango markup).
    Act { markup: String },
    /// A card: DONE, INCOMPLETE, a question and its answer, a rewind.
    Card { kind: String, class: String, text: String },
    /// What the Lead saw in the browser.
    Browser(Box<Browsed>),
    /// A job that paused: what's missing, and a button that carries it on.
    Paused { head: String, text: String },
    /// Pictures Chat found.
    Gallery { query: String, items: Vec<Picture> },
    /// Videos Chat found.
    Videos { query: String, items: Vec<Video> },
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Picture {
    pub path: String,
    pub src: String,
    pub page: String,
    pub title: String,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Video {
    pub title: String,
    pub url: String,
    pub thumb: Option<String>,
    pub length: Option<String>,
}

/// One ```browse```, as the card shows it.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Browsed {
    pub url: String,
    pub title: String,
    pub size: (u32, u32),
    pub ms: u64,
    pub engine: String,
    pub errors: Vec<String>,
    pub failed: Vec<String>,
    pub warnings: Vec<String>,
    pub steps: Vec<String>,
    pub refused: Vec<String>,
    /// The screenshot (it may be gone by the time the chat is reopened).
    pub shot: Option<String>,
    /// What the vision model saw.
    pub look: Option<String>,
}

/// A task, as last shown.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct TaskSnap {
    pub id: String,
    pub title: String,
    pub state: String,
    pub workers: Vec<String>,
    pub deps: Vec<String>,
    pub criteria: Vec<String>,
    pub note: String,
}

/// Where the last job got to, so the panel shows it when you come back.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Progress {
    pub plan: Vec<(String, bool)>,
    pub finished: bool,
    pub tasks: Vec<TaskSnap>,
    pub phase: String,
    pub unmet: Vec<String>,
    pub board: Vec<(String, String, Option<String>, Vec<String>)>,
    /// The checkpoint taken before the last job touched anything (/undo, /diff).
    #[serde(default)]
    pub checkpoint: Option<String>,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Saved {
    pub folder: String,
    /// What the Lead is told of earlier turns: (role, text).
    pub history: Vec<(String, String)>,
    pub entries: Vec<Entry>,
    #[serde(default)]
    pub progress: Progress,
}

/// The conversation as Markdown (/export): what was said, what the fleet
/// did, the cards.
pub fn markdown(s: &Saved) -> String {
    let plain = |m: &str| {
        let mut out = String::new();
        let mut tag = false;
        for c in m.chars() {
            match c {
                '<' => tag = true,
                '>' if tag => tag = false,
                c if !tag => out.push(c),
                _ => {}
            }
        }
        out.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", "\"").replace("&amp;", "&")
    };
    let mut out = format!("# Chuck — {}\n\n", if s.folder.is_empty() { "Chat".to_string() } else { s.folder.clone() });
    let mut in_lane = false;
    for e in &s.entries {
        if !matches!(e, Entry::Act { .. }) && in_lane {
            out.push('\n');
            in_lane = false;
        }
        match e {
            Entry::User { text } => out.push_str(&format!("**You:** {}\n\n", text.trim())),
            Entry::Steer { text } => out.push_str(&format!("**You (while it worked):** {}\n\n", text.trim())),
            Entry::Lead { text, model, chat } => out.push_str(&format!("**{}**{}\n\n{}\n\n", if *chat { "Chuck" } else { "Lead" },
                if model.is_empty() { String::new() } else { format!(" · {model}") }, text.trim())),
            Entry::Act { markup } => {
                in_lane = true;
                out.push_str(&format!("    {}\n", plain(markup).trim()));
            }
            Entry::Card { kind, text, .. } => {
                out.push_str(&format!("> **{kind}**\n>\n{}\n\n", text.trim().lines().map(|l| format!("> {l}")).collect::<Vec<_>>().join("\n")));
            }
            Entry::Handoff { request } => out.push_str(&format!("> **FOR THE FLEET**\n>\n> {}\n\n", request.trim())),
            Entry::Paused { head, text } => {
                out.push_str(&format!("> **{head}**\n>\n{}\n\n", text.trim().lines().map(|l| format!("> {l}")).collect::<Vec<_>>().join("\n")));
            }
            Entry::Gallery { query, items } => {
                out.push_str(&format!("> **PICTURES** — {query}\n"));
                for p in items {
                    out.push_str(&format!("> - [{}]({}) ({})\n", if p.title.is_empty() { "picture" } else { &p.title }, p.src, p.page));
                }
                out.push('\n');
            }
            Entry::Videos { query, items } => {
                out.push_str(&format!("> **VIDEOS** — {query}\n"));
                for v in items {
                    out.push_str(&format!("> - [{}]({}){}\n", if v.title.is_empty() { &v.url } else { &v.title }, v.url,
                                          v.length.as_ref().map(|l| format!(" ({l})")).unwrap_or_default()));
                }
                out.push('\n');
            }
            Entry::Browser(b) => {
                out.push_str(&format!("> **BROWSER** {} ({}×{}, {} JS error{})\n", b.url, b.size.0, b.size.1, b.errors.len(),
                                      if b.errors.len() == 1 { "" } else { "s" }));
                for x in b.errors.iter().chain(&b.failed) {
                    out.push_str(&format!("> - {x}\n"));
                }
                if let Some(l) = &b.look {
                    out.push_str(&format!(">\n> {}\n", l.trim().replace('\n', "\n> ")));
                }
                out.push('\n');
            }
        }
    }
    out
}

/// The key Chat-mode conversations without a project folder are kept under.
pub fn loose() -> PathBuf {
    PathBuf::from("chuck:chat")
}

fn dir() -> PathBuf {
    chuck_store::ledger::data_dir().join("fleet-chats")
}

pub fn path_for(folder: &Path) -> PathBuf {
    let key = chuck_store::checkpoint::sha1_hex(folder.to_string_lossy().as_bytes());
    dir().join(format!("{}.json", &key[..16]))
}

/// A conversation file, wherever it is.
pub fn load_path(p: &Path) -> Option<Saved> {
    serde_json::from_str(&std::fs::read_to_string(p).ok()?).ok()
}

pub fn load(folder: &Path) -> Option<Saved> {
    let text = std::fs::read_to_string(path_for(folder)).ok()?;
    serde_json::from_str(&text).ok()
}

/// Written whole and renamed into place, readable only by you.
pub fn save(folder: &Path, s: &Saved) {
    let mut s = s.clone();
    if s.entries.len() > KEEP {
        let cut = s.entries.len() - KEEP;
        s.entries.drain(..cut);
    }
    let p = path_for(folder);
    let _ = std::fs::create_dir_all(dir());
    if let Ok(text) = serde_json::to_string(&s) {
        let tmp = p.with_extension("tmp");
        if chuck_store::write_private(&tmp, text.as_bytes()).is_ok() {
            let _ = std::fs::rename(&tmp, &p);
        }
    }
}

/// Start over: the old conversation is kept beside, dated.
pub fn archive(folder: &Path) {
    let p = path_for(folder);
    if p.exists() {
        let stamp = chuck_store::ledger::local_stamp("%Y%m%d-%H%M%S");
        let _ = std::fs::rename(&p, p.with_extension(format!("{stamp}.json")));
    }
}

// ── every conversation, for the Recent list ────────────────────────────

/// What kind of file a listed conversation is.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Kind {
    /// A folder's (or Chat's) conversation as it stands.
    Current,
    /// An earlier one, kept when a new conversation was started.
    Archived,
    /// One from the Python app (opened as a copy; the original stays).
    Python,
}

#[derive(Clone, Debug)]
pub struct Listed {
    pub path: PathBuf,
    pub title: String,
    /// The project folder ("" for a Chat-mode conversation).
    pub folder: String,
    pub modified: std::time::SystemTime,
    pub kind: Kind,
    /// Lower-cased text to search in.
    hay: String,
}

impl Listed {
    pub fn matches(&self, q: &str) -> bool {
        let q = q.trim().to_lowercase();
        q.is_empty() || q.split_whitespace().all(|w| self.hay.contains(w))
    }
}

/// The Python app's saved chats.
fn python_dir() -> PathBuf {
    chuck_store::ledger::data_dir().join("chats")
}

/// Its scaffolding messages (tool results and the like), not what anyone typed.
const PY_INTERNAL: &[&str] = &["TOOL RESULTS", "RESEARCH BUDGET", "STEP BUDGET", "CONTINUE:", "I ran ", "FILE ",
    "CONVERSATION SUMMARY", "[MESSAGE FROM THE USER"];

fn py_text(c: &serde_json::Value) -> String {
    match c {
        serde_json::Value::String(s) => s.clone(),
        serde_json::Value::Array(parts) => parts.iter().filter_map(|p| p.get("text").and_then(|t| t.as_str()))
            .collect::<Vec<_>>().join(" "),
        _ => String::new(),
    }
}

/// A Python chat as a conversation here: what you typed and what Chuck said.
fn from_python(v: &serde_json::Value) -> Saved {
    let mut s = Saved { folder: v.get("workspace").and_then(|w| w.as_str()).unwrap_or("").to_string(), ..Saved::default() };
    for m in v.get("history").and_then(|h| h.as_array()).into_iter().flatten() {
        let role = m.get("role").and_then(|r| r.as_str()).unwrap_or("");
        let mut text = py_text(m.get("content").unwrap_or(&serde_json::Value::Null));
        if let Some(rest) = text.strip_prefix("[Since your last reply:") {
            text = rest.split_once("]\n\n").map_or(String::new(), |(_, t)| t.to_string());
        }
        for cut in ["\n\n--- attached file:", "\n\n[attached file:", "\n[image attached:"] {
            if let Some(i) = text.find(cut) {
                text.truncate(i);
            }
        }
        match role {
            "user" if !PY_INTERNAL.iter().any(|p| text.starts_with(p)) && !text.trim().is_empty() => {
                s.entries.push(Entry::User { text: text.trim().to_string() });
                s.history.push(("user".into(), text.trim().to_string()));
            }
            "assistant" => {
                let said = chuck_tools::prose(&text, &["bash", "sh", "write", "edit", "patch", "read", "grep", "glob",
                    "search", "fetch", "images", "videos", "py", "python", "node", "plan", "ask", "task", "browse"]);
                if !said.trim().is_empty() {
                    s.entries.push(Entry::Lead { text: said.trim().to_string(), model: String::new(), chat: true });
                    s.history.push(("lead".into(), said.trim().to_string()));
                }
            }
            _ => {}
        }
    }
    s
}

fn summary(s: &Saved) -> (String, String) {
    let title = s.entries.iter().find_map(|e| match e {
        Entry::User { text } if !text.starts_with('/') => Some(text.clone()),
        _ => None,
    }).unwrap_or_else(|| "New conversation".into());
    let mut hay = String::new();
    for e in &s.entries {
        if let Entry::User { text } | Entry::Lead { text, .. } | Entry::Steer { text } = e {
            hay.push_str(&text.to_lowercase());
            hay.push('\n');
            if hay.len() > 40_000 {
                break;
            }
        }
    }
    (title.split_whitespace().collect::<Vec<_>>().join(" "), hay)
}

thread_local! {
    /// Parsed files by (path, modified): reading every conversation again on each keystroke would be slow.
    static SEEN: std::cell::RefCell<std::collections::HashMap<PathBuf, (std::time::SystemTime, Option<Listed>)>> =
        std::cell::RefCell::new(std::collections::HashMap::new());
}

fn read_listed(path: &Path, kind: Kind, modified: std::time::SystemTime) -> Option<Listed> {
    SEEN.with(|seen| {
        if let Some((m, l)) = seen.borrow().get(path) {
            if *m == modified {
                return l.clone();
            }
        }
        let l = (|| {
            let text = std::fs::read_to_string(path).ok()?;
            let (title, folder, hay) = if kind == Kind::Python {
                let v: serde_json::Value = serde_json::from_str(&text).ok()?;
                let s = from_python(&v);
                let (t, hay) = summary(&s);
                let t = v.get("title").and_then(|t| t.as_str()).map(|t| t.split_whitespace().collect::<Vec<_>>().join(" "))
                    .filter(|t| !t.is_empty()).unwrap_or(t);
                (t, s.folder, hay)
            } else {
                let s: Saved = serde_json::from_str(&text).ok()?;
                if s.entries.is_empty() {
                    return None;
                }
                let (t, hay) = summary(&s);
                (t, s.folder, hay)
            };
            let hay = format!("{} {} {hay}", title.to_lowercase(), folder.to_lowercase());
            Some(Listed { path: path.to_path_buf(), title, folder, modified, kind, hay })
        })();
        seen.borrow_mut().insert(path.to_path_buf(), (modified, l.clone()));
        l
    })
}

/// Every conversation, newest first: this app's (current and earlier) and
/// the Python app's.
pub fn list(limit: usize) -> Vec<Listed> {
    let mut files: Vec<(std::time::SystemTime, PathBuf, Kind)> = Vec::new();
    for (d, python) in [(dir(), false), (python_dir(), true)] {
        let Ok(rd) = std::fs::read_dir(&d) else { continue };
        for e in rd.flatten() {
            let p = e.path();
            let name = e.file_name().to_string_lossy().into_owned();
            let Ok(m) = e.metadata() else { continue };
            if !m.is_file() || e.file_type().is_ok_and(|t| t.is_symlink()) || !name.ends_with(".json") {
                continue;
            }
            let kind = if python {
                Kind::Python
            } else if name.matches('.').count() > 1 {
                Kind::Archived
            } else {
                Kind::Current
            };
            files.push((m.modified().unwrap_or(std::time::UNIX_EPOCH), p, kind));
        }
    }
    files.sort_by_key(|f| std::cmp::Reverse(f.0));
    files.into_iter().filter_map(|(m, p, k)| read_listed(&p, k, m)).take(limit).collect()
}

/// The conversation's key: its folder, or Chat's loose key.
fn key_of(folder: &str) -> PathBuf {
    if folder.is_empty() { loose() } else { PathBuf::from(folder) }
}

/// Make `l` the conversation its folder opens with (the one there now is
/// kept as an earlier one). -> the folder (None: a Chat-mode conversation).
pub fn reopen(l: &Listed) -> Result<Option<PathBuf>, String> {
    let key = key_of(&l.folder);
    match l.kind {
        Kind::Current => {}
        Kind::Archived => {
            archive(&key);
            std::fs::rename(&l.path, path_for(&key)).map_err(|e| e.to_string())?;
        }
        Kind::Python => {
            let v: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(&l.path).map_err(|e| e.to_string())?)
                .map_err(|e| e.to_string())?;
            let s = from_python(&v);
            archive(&key);
            save(&key, &s);
        }
    }
    Ok((!l.folder.is_empty()).then(|| PathBuf::from(&l.folder)))
}

/// Delete a conversation for good.
pub fn delete(l: &Listed) -> std::io::Result<()> {
    std::fs::remove_file(&l.path)
}

/// "3 min ago", "yesterday", "12 Mar".
pub fn ago(t: std::time::SystemTime) -> String {
    let secs = std::time::SystemTime::now().duration_since(t).map(|d| d.as_secs()).unwrap_or(0);
    match secs {
        0..=59 => "just now".into(),
        60..=3599 => format!("{} min ago", secs / 60),
        3600..=86_399 => format!("{} h ago", secs / 3600),
        86_400..=172_799 => "yesterday".into(),
        _ if secs < 7 * 86_400 => format!("{} days ago", secs / 86_400),
        _ => {
            let at = t.duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
            chuck_store::ledger::stamp_at(at, "%-d %b")
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_conversation_comes_back_as_it_was() {
        let home = std::env::temp_dir().join(format!("chuck-chat-{}", std::process::id()));
        std::fs::create_dir_all(&home).unwrap();
        std::env::set_var("HOME", &home);
        let folder = home.join("proj");
        let s = Saved { folder: folder.display().to_string(), history: vec![("user".into(), "hi".into())],
                        entries: vec![Entry::User { text: "hi".into() },
                                      Entry::Lead { text: "**hello**".into(), model: "m".into(), chat: true },
                                      Entry::Handoff { request: "install rg".into() },
                                      Entry::Act { markup: "<b>x</b>".into() },
                                      Entry::Card { kind: "DONE".into(), class: "done-card".into(), text: "ok".into() }],
                        progress: Progress { plan: vec![("a".into(), true)], finished: true, ..Default::default() } };
        save(&folder, &s);
        assert_eq!(load(&folder), Some(s.clone()));
        archive(&folder);
        let md = markdown(&s);
        assert!(md.contains("**You:** hi") && md.contains("**Chuck** · m\n\n**hello**") && md.contains("    x")
                && md.contains("> **DONE**") && md.contains("> **FOR THE FLEET**"), "{md}");
        assert_eq!(load(&folder), None);
        // an old file, from before Chat mode, still opens
        std::fs::write(path_for(&folder), r#"{"folder":"x","history":[],"entries":[{"k":"lead","text":"t","model":"m"}]}"#).unwrap();
        assert_eq!(load(&folder).unwrap().entries, vec![Entry::Lead { text: "t".into(), model: "m".into(), chat: false }]);
        let _ = std::fs::remove_dir_all(&home);
    }
}
