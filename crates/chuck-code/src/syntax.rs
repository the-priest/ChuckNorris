//! Code by its shape: the project's files, where names are defined (by each
//! language's definition patterns, Python's by indentation so a method knows
//! its class), whole-word mentions, and the repo map — what an agent reads
//! first to know a project. The fallback when no language server is there,
//! and always honest that it is one.

use regex::Regex;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::OnceLock;

/// Files bigger than this aren't source anyone reads.
pub const MAX_FILE_BYTES: u64 = 2 << 20;
const SKIP_EXT: &[&str] = &["png", "jpg", "jpeg", "gif", "webp", "ico", "svg", "pdf", "zip", "gz", "xz", "zst", "tar",
    "so", "o", "a", "bin", "pyc", "lock", "woff", "woff2", "ttf", "otf", "mp3", "mp4", "wav", "ogg", "onnx", "wasm",
    "db", "sqlite", "jar", "class", "exe", "dll"];

/// A definition found in the code.
#[derive(Clone, Debug, PartialEq)]
pub struct Def {
    pub path: String,
    /// 1-based.
    pub line: usize,
    /// UTF-16 column where the declaration (or its name) starts.
    pub col: usize,
    pub kind: String,
    pub container: String,
    pub text: String,
}

/// A use of a name.
#[derive(Clone, Debug, PartialEq)]
pub struct Ref {
    pub path: String,
    pub line: usize,
    pub text: String,
}

/// Files worth knowing about, relative to `root`: git's view when it's a
/// repository (so .gitignore counts), else a walk that skips build, vendor
/// and hidden folders. Regular files only.
pub fn project_files(root: &Path, limit: usize) -> Vec<String> {
    if root.join(".git").exists() {
        // core.fsmonitor would run a program the repository names: off
        let out = Command::new("git").args(["-c", "core.fsmonitor=false", "-C"]).arg(root)
            .args(["ls-files", "-co", "--exclude-standard", "-z"]).stdin(Stdio::null()).stderr(Stdio::null()).output();
        if let Some(o) = out.ok().filter(|o| o.status.success()) {
            return String::from_utf8_lossy(&o.stdout).split('\0').filter(|f| !f.is_empty())
                .filter(|f| root.join(f).symlink_metadata().is_ok_and(|m| m.is_file()))
                .take(limit).map(String::from).collect();
        }
    }
    let mut out = Vec::new();
    let walk = walkdir::WalkDir::new(root).follow_links(false).sort_by_file_name().into_iter()
        .filter_entry(|e| e.depth() == 0 || !(e.file_name().to_string_lossy().starts_with('.')
            || e.file_type().is_dir() && chuck_tools::cow::SKIP.contains(&e.file_name().to_string_lossy().as_ref())));
    for e in walk.flatten() {
        if e.file_type().is_file() {
            if let Ok(rel) = e.path().strip_prefix(root) {
                out.push(rel.to_string_lossy().into_owned());
                if out.len() >= limit {
                    break;
                }
            }
        }
    }
    out
}

/// A source file's text, or None (too big, binary, not UTF-8).
pub fn source(root: &Path, rel: &str) -> Option<String> {
    let p = root.join(rel);
    let ext = Path::new(rel).extension().map(|e| e.to_string_lossy().to_lowercase()).unwrap_or_default();
    if SKIP_EXT.contains(&ext.as_str()) || p.metadata().ok()?.len() > MAX_FILE_BYTES {
        return None;
    }
    std::fs::read_to_string(p).ok()
}

fn re(p: &str) -> Regex {
    Regex::new(p).expect("valid regex")
}

/// The definition pattern for a file's extension (Python is handled apart).
fn pattern(ext: &str) -> Option<&'static Regex> {
    static P: OnceLock<Vec<(&'static [&'static str], Regex)>> = OnceLock::new();
    let ps = P.get_or_init(|| vec![
        (&["js", "mjs", "cjs", "jsx", "ts", "tsx"][..], re(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\*?\s+(\w+)|class\s+(\w+)|(?:const|let)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>|interface\s+(\w+)|type\s+(\w+)\s*=)")),
        (&["go"][..], re(r"^(?:func\s+(?:\([^)]*\)\s*)?(\w+)|type\s+(\w+))")),
        (&["rs"][..], re(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:(?:async|const|unsafe|extern\s+\S+)\s+)*(?:fn|struct|enum|trait|mod|type|union)\s+(\w+)")),
        (&["sh", "bash"][..], re(r"^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{?")),
        (&["c", "h", "cpp", "hpp", "cc", "cxx"][..], re(r"^(?:[\w:<>*&\s]+?\s)?\**(\w+)\s*\([^;]*\)\s*\{?\s*$|^(?:struct|class|enum)\s+(\w+)")),
        (&["java", "kt", "cs", "swift"][..], re(r"^\s*(?:(?:public|private|protected|internal|open|data|abstract|final|static|sealed)\s+)*(?:class|interface|enum|object|struct|fun|func)\s+(\w+)")),
        (&["rb"][..], re(r"^\s*(?:def|class|module)\s+([\w.?!]+)")),
        (&["php"][..], re(r"^\s*(?:abstract\s+|final\s+)?(?:function|class|interface|trait)\s+(\w+)")),
        (&["lua"][..], re(r"^\s*(?:local\s+)?function\s+([\w.:]+)")),
    ]);
    ps.iter().find(|(exts, _)| exts.contains(&ext)).map(|(_, r)| r)
}

const NOT_NAMES: &[&str] = &["if", "for", "while", "switch", "return", "catch", "sizeof"];

/// (name, line, col, kind, container) for each definition in one file.
pub fn defs_in(rel: &str, text: &str) -> Vec<(String, usize, usize, &'static str, String)> {
    let ext = Path::new(rel).extension().map(|e| e.to_string_lossy().to_lowercase()).unwrap_or_default();
    if ext == "py" || ext == "pyi" {
        return py_defs(text);
    }
    let Some(rx) = pattern(&ext) else { return Vec::new() };
    let mut out = Vec::new();
    for (i, ln) in text.lines().enumerate() {
        if let Some(c) = rx.captures(ln) {
            if let Some(m) = c.iter().skip(1).flatten().next() {
                if !NOT_NAMES.contains(&m.as_str()) {
                    out.push((m.as_str().to_string(), i + 1, utf16(&ln[..m.start()]), "definition", String::new()));
                }
            }
        }
    }
    out
}

/// Python by indentation: functions, classes (with their methods' class as
/// container) and module-level assignments.
fn py_defs(text: &str) -> Vec<(String, usize, usize, &'static str, String)> {
    static D: OnceLock<Regex> = OnceLock::new();
    static A: OnceLock<Regex> = OnceLock::new();
    let d = D.get_or_init(|| re(r"^([ \t]*)(?:async\s+)?(def|class)\s+([A-Za-z_]\w*)"));
    let a = A.get_or_init(|| re(r"^([A-Za-z_]\w*)\s*(?::[^=]*)?=[^=]"));
    let mut out = Vec::new();
    // what we are inside: (indent, Some(class) | None for a function)
    let mut stack: Vec<(usize, Option<String>)> = Vec::new();
    let mut in_string: Option<&str> = None;
    for (i, ln) in text.lines().enumerate() {
        // skip the inside of triple-quoted strings (docstrings full of "def")
        if let Some(q) = in_string {
            if ln.contains(q) {
                in_string = None;
            }
            continue;
        }
        let t = ln.trim_start();
        if t.is_empty() || t.starts_with('#') {
            continue;
        }
        let indent = ln.len() - t.len();
        while stack.last().is_some_and(|(ind, _)| *ind >= indent) {
            stack.pop();
        }
        for q in ["\"\"\"", "'''"] {
            if t.matches(q).count() % 2 == 1 {
                in_string = Some(q);
            }
        }
        if let Some(c) = d.captures(ln) {
            let name = c[3].to_string();
            // directly in a class body: a method of it; in a function: a plain function
            let container = stack.last().and_then(|(_, c)| c.clone()).unwrap_or_default();
            let kind = if &c[2] == "class" { "class" } else if container.is_empty() { "function" } else { "method" };
            out.push((name.clone(), i + 1, utf16(&c[1]), kind, container));
            stack.push((indent, (&c[2] == "class").then_some(name)));
        } else if indent == 0 {
            if let Some(c) = a.captures(ln) {
                out.push((c[1].to_string(), i + 1, 0, "variable", String::new()));
            }
        }
    }
    out
}

/// Length in UTF-16 code units (what LSP columns count).
pub fn utf16(s: &str) -> usize {
    s.chars().map(char::len_utf16).sum()
}

/// "Store.save" -> (Some("Store"), "save"); "save" -> (None, "save").
pub fn split(name: &str) -> (Option<&str>, &str) {
    let n = name.trim();
    match n.trim_matches('.').rsplit_once('.') {
        Some((cont, leaf)) if !n.starts_with('.') => (Some(cont.rsplit('.').next().unwrap_or(cont)), leaf),
        _ => (None, n),
    }
}

pub fn valid_name(s: &str) -> bool {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| re(r"^[A-Za-z_$][\w$]*$")).is_match(s)
}

/// The text of line `n` (1-based) of a file.
pub fn line_of(root: &Path, rel: &str, n: usize) -> String {
    std::fs::read_to_string(root.join(rel)).ok()
        .and_then(|t| t.lines().nth(n.saturating_sub(1)).map(|l| l.trim_end().to_string())).unwrap_or_default()
}

/// Definitions of `name` by syntax.
pub fn defs(root: &Path, name: &str) -> Vec<Def> {
    let (cont, leaf) = split(name);
    let mut out = Vec::new();
    for rel in project_files(root, 5000) {
        let Some(text) = source(root, &rel) else { continue };
        if !text.contains(leaf) {
            continue;
        }
        for (n, line, col, kind, container) in defs_in(&rel, &text) {
            if n == leaf && cont.is_none_or(|c| c == container) {
                let t = text.lines().nth(line - 1).unwrap_or("").trim().to_string();
                out.push(Def { path: rel.clone(), line, col, kind: kind.into(), container, text: t });
            }
        }
    }
    out
}

fn word(name: &str) -> Regex {
    re(&format!(r"(?:^|[^\w$]){}(?:[^\w$]|$)", regex::escape(name)))
}

/// Files that mention `word` as a whole word (optionally only these extensions).
pub fn mentions(root: &Path, w: &str, exts: Option<&[&str]>) -> Vec<String> {
    let rx = word(w);
    project_files(root, 5000).into_iter().filter(|rel| {
        exts.is_none_or(|e| Path::new(rel).extension().is_some_and(|x| e.contains(&x.to_string_lossy().to_lowercase().as_str())))
    }).filter(|rel| source(root, rel).is_some_and(|t| rx.is_match(&t))).collect()
}

/// Whole-word uses of `w`, definitions left out.
pub fn text_refs(root: &Path, w: &str, defs: &[Def], limit: usize) -> Vec<Ref> {
    let rx = word(w);
    let mut out = Vec::new();
    for rel in project_files(root, 5000) {
        let Some(text) = source(root, &rel) else { continue };
        for (i, ln) in text.lines().enumerate() {
            if rx.is_match(ln) && !defs.iter().any(|d| d.path == rel && d.line == i + 1) {
                out.push(Ref { path: rel.clone(), line: i + 1, text: ln.trim().to_string() });
                if out.len() >= limit {
                    return out;
                }
            }
        }
    }
    out
}

/// "path (N lines): defs…" for every source file, at most `max_chars`.
pub fn repo_map(root: &Path, max_chars: usize) -> String {
    let files = project_files(root, 5000);
    let mut rows = Vec::new();
    let mut total = 0;
    for (k, rel) in files.iter().enumerate() {
        let Some(text) = source(root, rel) else { continue };
        let n = text.lines().count();
        let ds = defs_in(rel, &text);
        let ext = Path::new(rel).extension().map(|e| e.to_string_lossy().to_lowercase()).unwrap_or_default();
        let mut names: Vec<String> = Vec::new();
        if ext == "py" || ext == "pyi" {
            // classes with their methods: Store[__init__, save, load]
            for (name, _, _, kind, container) in &ds {
                match *kind {
                    "class" if container.is_empty() => names.push(name.clone()),
                    "function" => names.push(format!("{name}()")),
                    "method" => {
                        if let Some(last) = names.iter_mut().rev().find(|x| x.trim_end_matches(']').split('[').next() == Some(container)) {
                            if !(name.starts_with("__") && name != "__init__") {
                                if last.ends_with(']') {
                                    last.pop();
                                    last.push_str(&format!(", {name}]"));
                                } else {
                                    last.push_str(&format!("[{name}]"));
                                }
                            }
                        }
                    }
                    _ => {}
                }
            }
        } else {
            for (name, ..) in &ds {
                if !names.contains(name) {
                    names.push(name.clone());
                }
            }
        }
        let mut row = format!("{rel} ({n} lines)");
        if !names.is_empty() {
            let shown: Vec<&str> = names.iter().take(30).map(String::as_str).collect();
            row.push_str(&format!(": {}{}", shown.join(", "), if names.len() > 30 { format!(", … +{}", names.len() - 30) } else { String::new() }));
        }
        if total + row.len() > max_chars {
            rows.push(format!("… {} more files — use glob/grep to narrow", files.len() - k));
            break;
        }
        total += row.len() + 1;
        rows.push(row);
    }
    if rows.is_empty() { "(no readable source files)".into() } else { rows.join("\n") }
}

/// An absolute path inside `root`, as a relative one (None: outside it).
pub fn rel_in(root: &Path, p: &Path) -> Option<String> {
    let root = root.canonicalize().ok()?;
    let p = p.canonicalize().unwrap_or_else(|_| p.to_path_buf());
    p.strip_prefix(&root).ok().map(|r| r.to_string_lossy().into_owned())
}

pub fn abs(root: &Path, rel: &str) -> PathBuf {
    root.join(rel)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_methods_know_their_class() {
        let src = "import os\nLIMIT: int = 3\nclass Store:\n    \"\"\"def fake(): in a docstring\n    \"\"\"\n    def save(self):\n        def helper():\n            pass\n    async def load(self):\n        pass\n\ndef save(x):\n    return x == 1\n";
        let d = defs_in("a.py", src);
        let got: Vec<(&str, usize, &str, &str)> = d.iter().map(|(n, l, _, k, c)| (n.as_str(), *l, *k, c.as_str())).collect();
        assert_eq!(got, vec![("LIMIT", 2, "variable", ""), ("Store", 3, "class", ""), ("save", 6, "method", "Store"),
                             ("helper", 7, "function", ""), ("load", 9, "method", "Store"), ("save", 12, "function", "")]);
    }

    #[test]
    fn other_languages_by_pattern() {
        let rs = "pub(crate) async fn run() {}\nstruct Kernel;\n    pub const fn new() -> Self {}\n";
        assert_eq!(defs_in("k.rs", rs).iter().map(|d| d.0.as_str()).collect::<Vec<_>>(), ["run", "Kernel", "new"]);
        let ts = "export default async function load() {}\nexport const save = async (x) => x\ninterface Opts {}\nif (x) {}\n";
        assert_eq!(defs_in("a.ts", ts).iter().map(|d| d.0.as_str()).collect::<Vec<_>>(), ["load", "save", "Opts"]);
        assert_eq!(defs_in("x.go", "func (s *S) Save() {}\ntype S struct{}\n").iter().map(|d| d.0.as_str()).collect::<Vec<_>>(), ["Save", "S"]);
        assert!(defs_in("notes.txt", "fn x").is_empty());
        assert_eq!(utf16("a😀b"), 4);
        assert_eq!(split("Store.save"), (Some("Store"), "save"));
        assert_eq!(split("pkg.Store.save"), (Some("Store"), "save"));
        assert_eq!(split("save"), (None, "save"));
    }

    #[test]
    fn a_project_by_shape() {
        let d = std::env::temp_dir().join(format!("chuck-syntax-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(d.join("src")).unwrap();
        std::fs::create_dir_all(d.join("node_modules/x")).unwrap();
        std::fs::write(d.join("src/store.py"), "class Store:\n    def save(self):\n        pass\n    def __repr__(self):\n        pass\n\ndef make_store():\n    return Store()\n").unwrap();
        std::fs::write(d.join("src/main.py"), "from store import make_store\nmake_store().save()\nsaver = 1\n").unwrap();
        std::fs::write(d.join("node_modules/x/index.js"), "function make_store() {}\n").unwrap();
        std::fs::write(d.join("logo.png"), [0u8, 1, 2]).unwrap();
        let m = repo_map(&d, 10_000);
        assert_eq!(m, "src/main.py (3 lines)\nsrc/store.py (8 lines): Store[save], make_store()");
        let defs = defs(&d, "make_store");
        assert_eq!(defs.len(), 1, "vendored code isn't the project's");
        assert_eq!((defs[0].path.as_str(), defs[0].line, defs[0].text.as_str()), ("src/store.py", 7, "def make_store():"));
        assert_eq!(super::defs(&d, "Store.save").len(), 1);
        assert!(super::defs(&d, "Other.save").is_empty());
        let refs = text_refs(&d, "make_store", &defs, 50);
        assert_eq!(refs.iter().map(|r| (r.path.as_str(), r.line)).collect::<Vec<_>>(), [("src/main.py", 1), ("src/main.py", 2)]);
        assert_eq!(text_refs(&d, "save", &[], 50).len(), 2, "whole words: not `saver`");
        let _ = std::fs::remove_dir_all(&d);
    }
}
