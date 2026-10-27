//! ```find <what the code does>```: code found by what it does, not by an
//! exact string. "where do we retry failed requests?" in a project you've
//! never seen: grep needs the word the author happened to use; this ranks
//! every function, class and doc section by how well its name, docstring,
//! comments and identifiers match the question (BM25), with camelCase and
//! snake_case split into words and a light stemmer so "retrying", "retries"
//! and "retry" meet. The index is kept per folder and only changed files are
//! read again. A port of the Python app's codesearch.py.

use crate::syntax::{defs_in, project_files, source};
use regex::Regex;
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::SystemTime;

const TEXT_EXT: &[&str] = &["py", "pyi", "js", "jsx", "mjs", "cjs", "ts", "tsx", "go", "rs", "c", "h", "cc", "cpp", "hpp",
    "java", "kt", "cs", "swift", "rb", "php", "lua", "sh", "bash", "zsh", "md", "rst", "txt", "toml", "yaml", "yml", "json",
    "html", "css", "scss", "vue", "svelte", "sql", "ex", "exs", "scala", "dart"];
const STOP: &[&str] = &["a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "has", "have", "if", "in",
    "into", "is", "it", "its", "of", "on", "or", "that", "the", "their", "then", "there", "these", "this", "to", "was",
    "were", "will", "with", "without", "you", "your", "we", "our", "not", "no", "self", "cls", "def", "return", "import",
    "class", "function", "const", "let", "var", "new", "none", "null", "true", "false", "else", "elif", "try", "except",
    "finally", "pass", "do", "done", "fi", "end"];
/// Lines per piece of loose code (and twice that per piece of a long function).
const WINDOW: usize = 60;
const MAX_FILES: usize = 4000;

fn re(p: &str) -> Regex {
    Regex::new(p).expect("valid regex")
}

/// Send a word's forms to one stem (parse, parses, parsed, parsing, parser →
/// pars); it needn't be a real word.
pub fn stem(w: &str) -> String {
    let mut w = w.to_string();
    for suf in ["ization", "ations", "ation", "ings", "ing", "edly", "ers", "ies", "ied", "ure", "ed", "es", "er", "s"] {
        if !w.ends_with(suf) || w.len() < suf.len() + 3 {
            continue;
        }
        let mut base = w[..w.len() - suf.len()].to_string();
        if suf == "ies" || suf == "ied" {
            return base + "y";
        }
        if suf == "es" && !["s", "x", "z", "ch", "sh"].iter().any(|e| base.ends_with(e)) {
            base = w[..w.len() - 1].to_string(); // files -> file, not fil·es
        }
        if suf == "s" && ["s", "u", "i"].iter().any(|e| base.ends_with(e)) {
            return w; // process, status, analysis keep their s
        }
        let b = base.as_bytes();
        if ["ing", "ings", "ed", "er", "ers"].contains(&suf) && b.len() > 3 && b[b.len() - 1] == b[b.len() - 2]
            && !b"lsz".contains(&b[b.len() - 1])
        {
            base.pop(); // running -> run, stopped -> stop
        }
        w = base;
        break;
    }
    if w.len() > 3 && w.ends_with('e') {
        w.pop(); // parse and parsing meet at pars
    }
    w
}

/// The words of a text: identifiers split at case and underscores, short
/// and common words dropped, stemmed.
pub fn words(text: &str) -> Vec<String> {
    static IDENT: OnceLock<Regex> = OnceLock::new();
    static PART: OnceLock<fancy_regex::Regex> = OnceLock::new();
    let ident = IDENT.get_or_init(|| re(r"[A-Za-z_][A-Za-z0-9_]*"));
    let part = PART.get_or_init(|| fancy_regex::Regex::new(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+").unwrap());
    let mut out = Vec::new();
    for id in ident.find_iter(text) {
        for p in part.find_iter(id.as_str()).flatten() {
            let w = p.as_str().to_lowercase();
            if w.len() > 1 && !STOP.contains(&w.as_str()) {
                let s = stem(&w);
                if !STOP.contains(&s.as_str()) {
                    out.push(s);
                }
            }
        }
    }
    out
}

/// (start, end, name, doc) — 1-based, inclusive.
type Span = (usize, usize, String, String);

fn indent_of(l: &str) -> usize {
    l.len() - l.trim_start().len()
}

/// A Python docstring right under the line `at` (0-based), if any.
fn py_doc(lines: &[&str], at: usize) -> String {
    let Some(first) = lines.get(at + 1).map(|l| l.trim()) else { return String::new() };
    let q = if first.starts_with("\"\"\"") || first.starts_with("r\"\"\"") { "\"\"\"" } else if first.starts_with("'''") { "'''" }
        else { return String::new() };
    let body = first.trim_start_matches('r').trim_start_matches(q);
    if let Some(end) = body.find(q) {
        return body[..end].to_string();
    }
    let mut doc = vec![body.to_string()];
    for l in lines.iter().skip(at + 2).take(80) {
        if let Some(end) = l.find(q) {
            doc.push(l[..end].to_string());
            break;
        }
        doc.push(l.to_string());
    }
    doc.join("\n")
}

/// Python by indentation: each function and method, each class's head (its
/// methods are found on their own), and module-level code in windows.
fn py_spans(text: &str) -> Vec<Span> {
    let lines: Vec<&str> = text.lines().collect();
    let defs: Vec<_> = defs_in("x.py", text).into_iter().filter(|d| d.3 != "variable").collect();
    let mut out: Vec<Span> = Vec::new();
    let real = |l: &str| !l.trim().is_empty() && !l.trim_start().starts_with('#');
    for (k, (name, line, _col, kind, container)) in defs.iter().enumerate() {
        let i = line - 1;
        let ind = indent_of(lines[i]);
        // decorators above it belong to it
        let mut start = i;
        while start > 0 && lines[start - 1].trim_start().starts_with('@') && indent_of(lines[start - 1]) == ind {
            start -= 1;
        }
        // to the last line before code at its indent or less
        let mut end = i;
        for (j, l) in lines.iter().enumerate().skip(i + 1) {
            if real(l) && indent_of(l) <= ind && !l.trim_start().starts_with(')') {
                break;
            }
            if real(l) {
                end = j;
            }
        }
        if *kind == "class" {
            // the head: up to the first nested def
            if let Some(next) = defs.get(k + 1).filter(|d| d.1 - 1 <= end) {
                let mut s = next.1 - 1;
                while s > i && lines[s - 1].trim_start().starts_with('@') {
                    s -= 1;
                }
                end = s.saturating_sub(1).max(i);
            }
        }
        let full = if container.is_empty() { name.clone() } else { format!("{container}.{name}") };
        out.push((start + 1, end + 1, full, py_doc(&lines, i)));
    }
    let mut covered = vec![false; lines.len() + 1];
    for (s, e, _, _) in &out {
        for c in covered.iter_mut().take(*e + 1).skip(*s) {
            *c = true;
        }
    }
    let loose: Vec<usize> = (1..=lines.len()).filter(|&i| !covered[i]).collect();
    for seg in loose.chunks(WINDOW) {
        if seg.iter().any(|&i| !lines[i - 1].trim().is_empty()) {
            out.push((seg[0], seg[seg.len() - 1], String::new(), String::new()));
        }
    }
    out
}

/// Other languages: from each definition to the next (long ones in pieces);
/// Markdown by its headings; anything else in windows.
fn pattern_spans(rel: &str, text: &str, ext: &str) -> Vec<Span> {
    let lines: Vec<&str> = text.lines().collect();
    let starts: Vec<(usize, String)> = if ext == "md" || ext == "rst" {
        lines.iter().enumerate().filter(|(_, l)| l.starts_with('#')).map(|(i, l)| (i + 1, l.trim_start_matches('#').trim().to_string()))
            .collect()
    } else {
        defs_in(rel, text).into_iter().map(|d| (d.1, d.0)).collect()
    };
    let mut out = Vec::new();
    if starts.is_empty() {
        for a in (1..=lines.len()).step_by(WINDOW) {
            out.push((a, (a + WINDOW - 1).min(lines.len()), String::new(), String::new()));
        }
        return out;
    }
    if starts[0].0 > 1 {
        out.push((1, starts[0].0 - 1, String::new(), String::new()));
    }
    for (k, (i, name)) in starts.iter().enumerate() {
        let end = starts.get(k + 1).map_or(lines.len(), |n| n.0 - 1).max(*i);
        for a in (*i..=end).step_by(WINDOW * 2) {
            out.push((a, (a + WINDOW * 2 - 1).min(end), name.clone(), String::new()));
        }
    }
    out
}

#[derive(Clone, Debug)]
struct Chunk {
    rel: String,
    start: usize,
    end: usize,
    name: String,
    tf: HashMap<String, u32>,
    len: usize,
}

fn chunk(rel: &str, text: &str, ext: &str) -> Vec<Chunk> {
    let lines: Vec<&str> = text.lines().collect();
    let spans = if ext == "py" || ext == "pyi" { py_spans(text) } else { pattern_spans(rel, text, ext) };
    let path_words = words(&rel.replace('/', " "));
    spans.into_iter().filter_map(|(start, end, name, doc)| {
        let body = lines.get(start.saturating_sub(1)..end.min(lines.len()))?.join("\n");
        let mut terms = words(&body);
        terms.extend(path_words.iter().cloned());
        // the name and docstring say what it IS: they count three times
        let said: Vec<String> = words(&name).into_iter().chain(words(&doc)).collect();
        terms.extend(said.iter().cloned());
        terms.extend(said);
        if terms.is_empty() {
            return None;
        }
        let mut tf = HashMap::new();
        for t in &terms {
            *tf.entry(t.clone()).or_insert(0) += 1;
        }
        Some(Chunk { rel: rel.into(), start, end, name, len: terms.len(), tf })
    }).collect()
}

/// One folder's index.
#[derive(Default)]
struct Index {
    files: HashMap<String, (SystemTime, u64, Vec<Chunk>)>,
}

impl Index {
    fn refresh(&mut self, root: &Path) {
        let mut seen = HashSet::new();
        for rel in project_files(root, MAX_FILES) {
            let ext = Path::new(&rel).extension().map(|e| e.to_string_lossy().to_lowercase()).unwrap_or_default();
            if !TEXT_EXT.contains(&ext.as_str()) {
                continue;
            }
            let Ok(m) = root.join(&rel).metadata() else { continue };
            let (mtime, size) = (m.modified().unwrap_or(SystemTime::UNIX_EPOCH), m.len());
            seen.insert(rel.clone());
            if self.files.get(&rel).is_some_and(|f| f.0 == mtime && f.1 == size) {
                continue;
            }
            let Some(text) = source(root, &rel) else { continue };
            let chunks = chunk(&rel, &text, &ext);
            self.files.insert(rel, (mtime, size, chunks));
        }
        self.files.retain(|rel, _| seen.contains(rel));
    }

    fn search(&self, query: &str, k: usize) -> Vec<(f64, Chunk)> {
        let mut q: Vec<String> = Vec::new();
        for w in words(query) {
            if !q.contains(&w) {
                q.push(w);
            }
        }
        let chunks: Vec<&Chunk> = self.files.values().flat_map(|f| f.2.iter()).collect();
        if q.is_empty() || chunks.is_empty() {
            return Vec::new();
        }
        let n = chunks.len() as f64;
        let avg = chunks.iter().map(|c| c.len).sum::<usize>() as f64 / n;
        let df: HashMap<&str, f64> = q.iter().map(|t| (t.as_str(), chunks.iter().filter(|c| c.tf.contains_key(t)).count() as f64))
            .collect();
        let (k1, b) = (1.2, 0.75);
        let want_tests = q.iter().any(|t| t == "test" || t == "spec");
        let want_docs = q.iter().any(|t| ["doc", "readm", "changelog", "roadmap"].contains(&t.as_str()));
        static TESTS: OnceLock<Regex> = OnceLock::new();
        let tests = TESTS.get_or_init(|| re(r"(^|/)(tests?|spec|__tests__)/|(^|/)test_|_test\.|\.spec\."));
        let mut scored: Vec<(f64, Chunk)> = chunks.into_iter().filter_map(|c| {
            let mut s = 0.0;
            let mut hit = 0;
            for t in &q {
                let Some(&f) = c.tf.get(t) else { continue };
                hit += 1;
                let d = df[t.as_str()];
                let idf = (1.0 + (n - d + 0.5) / (d + 0.5)).ln();
                let f = f as f64;
                s += idf * f * (k1 + 1.0) / (f + k1 * (1.0 - b + b * c.len as f64 / avg));
            }
            if s <= 0.0 {
                return None;
            }
            s *= 1.0 + 0.5 * (hit - 1) as f64 / q.len().max(1) as f64; // matching more of the question
            let rel = c.rel.to_lowercase();
            if !want_tests && tests.is_match(&rel) {
                s *= 0.6; // the code, before the code that tests it
            }
            if !want_docs && (rel.ends_with(".md") || rel.ends_with(".rst") || rel.ends_with(".txt")) {
                s *= 0.75;
            }
            Some((s, c.clone()))
        }).collect();
        scored.sort_by(|a, b| b.0.total_cmp(&a.0).then_with(|| a.1.rel.cmp(&b.1.rel)).then(a.1.start.cmp(&b.1.start)));
        scored.truncate(k);
        scored
    }
}

fn indexes() -> &'static Mutex<HashMap<PathBuf, Index>> {
    static I: OnceLock<Mutex<HashMap<PathBuf, Index>>> = OnceLock::new();
    I.get_or_init(Default::default)
}

/// A match: where, what it's called, how well it matched.
#[derive(Clone, Debug, PartialEq)]
pub struct Found {
    pub path: String,
    pub start: usize,
    pub end: usize,
    pub name: String,
    pub score: f64,
}

/// The `k` best matches for `query` in `root`.
pub fn find(root: &Path, query: &str, k: usize) -> Vec<Found> {
    let key = std::fs::canonicalize(root).unwrap_or_else(|_| root.to_path_buf());
    let mut all = indexes().lock().unwrap_or_else(|e| e.into_inner());
    // a few folders at most: an index per folder ever opened would only grow
    if all.len() > 8 && !all.contains_key(&key) {
        all.clear();
    }
    let idx = all.entry(key.clone()).or_default();
    idx.refresh(&key);
    idx.search(query, k).into_iter().map(|(score, c)| Found { path: c.rel, start: c.start, end: c.end, name: c.name, score })
        .collect()
}

/// What the agent reads: each match with its first line and, when another
/// line matches the question better, that one too.
pub fn report(root: &Path, query: &str, found: &[Found]) -> String {
    if found.is_empty() {
        return format!("FIND \"{query}\": nothing matched. Try other words for it, or grep for a name you expect.");
    }
    let q: HashSet<String> = words(query).into_iter().collect();
    let mut rows = Vec::new();
    for f in found {
        let Some(text) = source(root, &f.path) else { continue };
        let lines: Vec<&str> = text.lines().collect();
        let seg = &lines[(f.start - 1).min(lines.len())..f.end.min(lines.len())];
        let best = (0..seg.len()).max_by_key(|&i| {
            let ws: HashSet<String> = words(seg[i]).into_iter().collect();
            (q.intersection(&ws).count(), std::cmp::Reverse(i))
        }).unwrap_or(0);
        let head = seg.first().map(|l| l.trim()).unwrap_or("");
        let cut = |s: &str| s.chars().take(150).collect::<String>();
        let mut row = format!("{}:{}-{}{}  (score {:.1})\n    {}", f.path, f.start, f.end,
                              if f.name.is_empty() { String::new() } else { format!("  {}", f.name) }, f.score, cut(head));
        if best > 0 && !seg[best].trim().is_empty() {
            row.push_str(&format!("\n    … {}", cut(seg[best].trim())));
        }
        rows.push(row);
    }
    format!("FIND \"{query}\" — best matches by meaning (read them to confirm):\n{}", rows.join("\n"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stems_meet() {
        for (a, b) in [("retrying", "retry"), ("retries", "retry"), ("parsing", "parse"), ("parser", "parse"),
                       ("files", "file"), ("running", "run"), ("stopped", "stop")] {
            assert_eq!(stem(a), stem(b), "{a} / {b}");
        }
        assert_eq!(stem("process"), "process");
        assert_eq!(stem("status"), "status");
    }

    #[test]
    fn stems_match_the_python_app() {
        let cases: &[(&str, &str)] = &[("retrying", "retry"), ("retries", "retry"), ("parsing", "pars"), ("parser", "pars"), ("files", "fil"), ("running", "run"), ("stopped", "stop"), ("process", "process"), ("status", "status"), ("analysis", "analysis"), ("classes", "class"), ("configuration", "configur"), ("caching", "cach"), ("boxes", "box"), ("matches", "match"), ("wishes", "wish"), ("buses", "bus"), ("quizzes", "quizz"), ("studied", "study"), ("tried", "tri"), ("flies", "fli"), ("cities", "city"), ("timer", "tim"), ("timers", "tim"), ("created", "creat"), ("creating", "creat"), ("hoped", "hop"), ("hopping", "hop"), ("filled", "fill"), ("passed", "pass"), ("buzzing", "buzz"), ("settings", "set"), ("optimization", "optim"), ("organizations", "organiz"), ("ations", "ation"), ("rendering", "render"), ("renderer", "render"), ("rendered", "render"), ("measure", "meas"), ("measured", "measur"), ("nature", "nat"), ("structure", "struct"), ("feature", "feat"), ("handles", "handl"), ("handled", "handl"), ("handler", "handl"), ("callbacks", "callback"), ("fetching", "fetch"), ("fetched", "fetch"), ("queued", "queu"), ("queue", "queu"), ("queues", "queu"), ("tokens", "token"), ("tokenizer", "tokeniz"), ("serialize", "serializ"), ("serialization", "serial"), ("serialized", "serializ")];
        for (w, want) in cases {
            assert_eq!(stem(w), *want, "{w}");
        }
        assert_eq!(words("getHTTPResponseCode parseJSONFile __init__ XMLHttpRequest2 snake_case_name"),
                   ["get", "http", "respons", "cod", "pars", "json", "fil", "init", "xml", "http", "request", "snak", "cas", "nam"]);
    }

    #[test]
    fn identifiers_split_into_words() {
        assert_eq!(words("retryFailedRequests HTTPServer max_retries"),
                   vec!["retry", "fail", "request", "http", "serv", "max", "retry"]);
        assert!(words("the self return").is_empty());
    }

    #[test]
    fn python_spans() {
        let src = "import os\n\n@cache\ndef load(path):\n    \"\"\"Read the settings file.\"\"\"\n    return open(path)\n\n\
                   class Store:\n    \"\"\"Keeps things.\"\"\"\n    size = 3\n\n    def save(self):\n        pass\n\nx = 1\n";
        let s = py_spans(src);
        assert!(s.contains(&(3, 6, "load".into(), "Read the settings file.".into())), "{s:?}");
        assert!(s.iter().any(|x| x.2 == "Store" && x.0 == 8 && x.1 == 11 && x.3 == "Keeps things."), "{s:?}");
        assert!(s.iter().any(|x| x.2 == "Store.save" && x.0 == 12 && x.1 == 13), "{s:?}");
        assert!(s.iter().any(|x| x.2.is_empty() && x.0 == 1), "loose code: {s:?}");
    }

    #[test]
    fn finds_by_meaning() {
        let d = std::env::temp_dir().join(format!("chuck-find-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(d.join("tests")).unwrap();
        std::fs::write(d.join("net.py"), "def fetch_with_backoff(url, attempts=3):\n    \"\"\"Retry failed HTTP requests with \
            exponential backoff.\"\"\"\n    for i in range(attempts):\n        pass\n\ndef render(page):\n    return page\n").unwrap();
        std::fs::write(d.join("tests/test_net.py"), "def test_retrying():\n    fetch_with_backoff('x')\n").unwrap();
        std::fs::write(d.join("ui.ts"), "export function drawButton(label: string) {\n  return label;\n}\n").unwrap();
        let f = find(&d, "where do we retry requests that failed?", 5);
        assert_eq!(f[0].path, "net.py");
        assert_eq!((f[0].start, f[0].name.as_str()), (1, "fetch_with_backoff"));
        assert!(f.iter().position(|x| x.path.starts_with("tests/")).unwrap() > 0, "the code before its test");
        let r = report(&d, "retry failed requests", &f);
        assert!(r.contains("net.py:1-4  fetch_with_backoff") && r.contains("… \"\"\"Retry failed HTTP"), "{r}");
        assert_eq!(find(&d, "draw a button", 1)[0].path, "ui.ts");
        // a changed file is read again
        std::fs::write(d.join("ui.ts"), "export function paintWidget() {}\n").unwrap();
        let later = std::time::SystemTime::now() + std::time::Duration::from_secs(2);
        let _ = std::fs::File::options().write(true).open(d.join("ui.ts")).and_then(|f| f.set_modified(later));
        assert!(find(&d, "draw a button", 3).iter().all(|x| x.path != "ui.ts"));
        assert!(report(&d, "zebra", &find(&d, "zebra", 3)).contains("nothing matched"));
        let _ = std::fs::remove_dir_all(&d);
    }
}
