//! The evidence ledger: a tamper-evident record of every command Chuck ran,
//! the same file the window writes (~/.local/share/chucknorris/ledger.jsonl).
//!
//! Append-only JSONL, 0600; each entry carries the SHA-256 of the previous
//! one. Editing or removing an entry breaks every link after it, and
//! `verify` names the first place the chain parts. Honestly: tamper-EVIDENT,
//! not tamper-proof — whoever can write the file can rewrite the whole chain.
//! The hash is over Python's canonical JSON (sorted keys, compact, ASCII
//! escapes), byte for byte, so the window and `chuck` extend one chain.

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

pub const GENESIS: &str = "0000000000000000000000000000000000000000000000000000000000000000";
const MAX_OUTPUT_KEPT: usize = 4000;
const MAX_LEDGER_BYTES: u64 = 8_000_000;
const KEEP_ROTATED: usize = 3;

pub fn data_dir() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"))
        .join(".local").join("share").join("chucknorris")
}

/// Python's `json.dumps(v, sort_keys=True, separators=(",", ":"))`.
pub fn canonical(v: &Value) -> String {
    let mut out = String::new();
    write_canon(v, &mut out);
    out
}

fn write_canon(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => {
            out.push('"');
            for c in s.chars() {
                match c {
                    '"' => out.push_str("\\\""),
                    '\\' => out.push_str("\\\\"),
                    '\n' => out.push_str("\\n"),
                    '\r' => out.push_str("\\r"),
                    '\t' => out.push_str("\\t"),
                    '\u{8}' => out.push_str("\\b"),
                    '\u{c}' => out.push_str("\\f"),
                    c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                        let mut buf = [0u16; 2];
                        for u in c.encode_utf16(&mut buf) {
                            out.push_str(&format!("\\u{u:04x}"));
                        }
                    }
                    c => out.push(c),
                }
            }
            out.push('"');
        }
        Value::Array(a) => {
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_canon(x, out);
            }
            out.push(']');
        }
        Value::Object(m) => {
            out.push('{');
            let mut keys: Vec<&String> = m.keys().collect();
            keys.sort_by(|a, b| a.encode_utf16().cmp(b.encode_utf16())); // Python sorts by code points
            for (i, k) in keys.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_canon(&Value::String((*k).clone()), out);
                out.push(':');
                write_canon(&m[*k], out);
            }
            out.push('}');
        }
    }
}

/// The hash of an entry's canonical form, without its own hash field.
pub fn digest(entry: &Map<String, Value>) -> String {
    let mut m = entry.clone();
    m.remove("hash");
    hex::encode(Sha256::digest(canonical(&Value::Object(m)).as_bytes()))
}

/// What must never be kept: keys, tokens, passwords, private keys. The
/// output's hash is still of the real output, so the record still proves
/// what ran.
pub fn redact(text: &str) -> String {
    static RX: OnceLock<Vec<(fancy_regex::Regex, &'static str)>> = OnceLock::new();
    let rules = RX.get_or_init(|| {
        [
            (r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\z)", "[private key removed]"),
            (r#"(?i)(authorization:\s*(?:bearer|basic|token)\s+)[^\s'"]+"#, "${1}[removed]"),
            (r#"(?i)\b([\w.-]*(?:api[_-]?key|token|secret|passw(?:or)?d|pwd|credential)s?[\w.-]*\s*[=:]\s*)(['"]?)[^\s'"]{4,}\2"#,
             "${1}${2}[removed]${2}"),
            (r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[abposr]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})\b",
             "[removed]"),
        ].into_iter().map(|(p, r)| (fancy_regex::Regex::new(p).expect("redaction pattern"), r)).collect()
    });
    let mut t = text.to_string();
    for (re, rep) in rules {
        t = re.try_replacen(&t, 0, *rep).map(|c| c.into_owned()).unwrap_or(t.clone());
    }
    t
}

/// "%Y-%m-%dT%H:%M:%S%z" in local time, as the window writes it.
pub fn local_stamp(fmt: &str) -> String {
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    stamp_at(now as i64, fmt)
}

pub fn stamp_at(secs: i64, fmt: &str) -> String {
    let t: libc::time_t = secs as libc::time_t;
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    let mut buf = [0u8; 64];
    let Ok(f) = std::ffi::CString::new(fmt) else { return String::new() };
    let n = unsafe {
        libc::localtime_r(&t, &mut tm);
        libc::strftime(buf.as_mut_ptr() as *mut libc::c_char, buf.len(), f.as_ptr(), &tm)
    };
    String::from_utf8_lossy(&buf[..n]).into_owned()
}

fn clip_chars(s: &str, n: usize) -> &str {
    match s.char_indices().nth(n) {
        Some((i, _)) => &s[..i],
        None => s,
    }
}

pub struct Ledger {
    pub path: PathBuf,
    anchor: PathBuf,
    lock: Mutex<Option<String>>, // the chain head, once read
}

#[derive(Debug, PartialEq)]
pub struct Verified {
    pub ok: bool,
    pub count: usize,
    pub first_bad: Option<usize>,
    pub message: String,
}

impl Ledger {
    pub fn default_path() -> PathBuf {
        data_dir().join("ledger.jsonl")
    }

    pub fn at(path: &Path) -> Ledger {
        Ledger { path: path.to_path_buf(), anchor: path.with_file_name("ledger.anchor"), lock: Mutex::new(None) }
    }

    /// The process-wide ledger at the window's path.
    pub fn global() -> &'static Ledger {
        static L: OnceLock<Ledger> = OnceLock::new();
        L.get_or_init(|| Ledger::at(&Ledger::default_path()))
    }

    fn anchor_hash(&self) -> String {
        std::fs::read_to_string(&self.anchor).ok().map(|s| s.trim().to_string()).filter(|s| s.len() == 64)
            .unwrap_or_else(|| GENESIS.to_string())
    }

    /// The newest entry's hash: read from the file's tail every time (the
    /// window may have appended since), so two writers keep one chain.
    fn head(&self) -> String {
        let mut head = self.anchor_hash();
        let Ok(mut f) = std::fs::File::open(&self.path) else { return head };
        let size = f.metadata().map(|m| m.len()).unwrap_or(0);
        let start = size.saturating_sub(65_536);
        if f.seek(SeekFrom::Start(start)).is_err() {
            return head;
        }
        let mut tail = Vec::new();
        let _ = f.read_to_end(&mut tail);
        let text = String::from_utf8_lossy(&tail);
        let mut lines: Vec<&str> = text.lines().collect();
        if start > 0 && !lines.is_empty() {
            lines.remove(0); // a partial first line
        }
        for l in lines.iter().rev() {
            if let Ok(Value::Object(e)) = serde_json::from_str::<Value>(l.trim()) {
                if let Some(h) = e.get("hash").and_then(Value::as_str) {
                    head = h.to_string();
                    break;
                }
            }
        }
        head
    }

    fn rotate(&self) {
        let big = std::fs::metadata(&self.path).map(|m| m.len() >= MAX_LEDGER_BYTES).unwrap_or(false);
        if !big {
            return;
        }
        let head = self.head();
        let rolled = self.path.with_file_name(format!("{}.{}",
            self.path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default(),
            local_stamp("%Y%m%d-%H%M%S")));
        if std::fs::rename(&self.path, &rolled).is_ok() {
            let _ = crate::settings::write_private(&self.anchor, head.as_bytes());
            let name = self.path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
            let mut olds: Vec<PathBuf> = std::fs::read_dir(self.path.parent().unwrap_or(Path::new("."))).into_iter()
                .flatten().flatten().map(|e| e.path())
                .filter(|p| p.file_name().is_some_and(|n| n.to_string_lossy().starts_with(&format!("{name}.2"))))
                .collect();
            olds.sort();
            let n = olds.len();
            for f in olds.into_iter().take(n.saturating_sub(KEEP_ROTATED)) {
                let _ = std::fs::remove_file(f);
            }
        }
    }

    /// Append one command. Never fails the run it records: errors are
    /// swallowed, and the hash (or None) returned.
    pub fn record(&self, command: &str, rc: i32, output: &str, kind: &str, chat: &str, started: &str) -> Option<String> {
        let _guard = self.lock.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(p) = self.path.parent() {
            let _ = std::fs::create_dir_all(p);
        }
        self.rotate();
        let mut e = Map::new();
        e.insert("ts".into(), json!(local_stamp("%Y-%m-%dT%H:%M:%S%z")));
        e.insert("started".into(), json!(if started.is_empty() { local_stamp("%Y-%m-%dT%H:%M:%S%z") } else { started.into() }));
        e.insert("kind".into(), json!(kind));
        e.insert("chat".into(), json!(chat));
        e.insert("command".into(), json!(redact(clip_chars(command, 2000))));
        e.insert("rc".into(), json!(rc));
        e.insert("output_sha256".into(), json!(hex::encode(Sha256::digest(output.as_bytes()))));
        e.insert("output_bytes".into(), json!(output.len()));
        e.insert("output_head".into(), json!(redact(clip_chars(output, MAX_OUTPUT_KEPT))));
        e.insert("prev".into(), json!(self.head()));
        let h = digest(&e);
        e.insert("hash".into(), json!(h));
        // a line cut off mid-write (a kill) must not glue onto this one
        let lead = std::fs::File::open(&self.path).ok().and_then(|mut f| {
            let len = f.metadata().ok()?.len();
            if len == 0 {
                return Some(false);
            }
            f.seek(SeekFrom::End(-1)).ok()?;
            let mut b = [0u8; 1];
            f.read_exact(&mut b).ok()?;
            Some(b[0] != b'\n')
        }).unwrap_or(false);
        let mut o = std::fs::OpenOptions::new();
        o.create(true).append(true);
        {
            use std::os::unix::fs::OpenOptionsExt;
            o.mode(0o600);
        }
        let mut f = o.open(&self.path).ok()?;
        let line = format!("{}{}\n", if lead { "\n" } else { "" }, serde_json::to_string(&Value::Object(e)).ok()?);
        f.write_all(line.as_bytes()).ok()?;
        Some(h)
    }

    pub fn read_all(&self) -> Vec<Map<String, Value>> {
        let Ok(text) = std::fs::read_to_string(&self.path) else { return Vec::new() };
        text.lines().filter_map(|l| match serde_json::from_str::<Value>(l.trim()) {
            Ok(Value::Object(m)) => Some(m),
            _ => None,
        }).collect()
    }

    /// Check the chain from the anchor to the newest entry.
    pub fn verify(&self) -> Verified {
        let entries = self.read_all();
        if entries.is_empty() {
            return Verified { ok: true, count: 0, first_bad: None, message: "ledger is empty".into() };
        }
        let mut prev = self.anchor_hash();
        for (i, e) in entries.iter().enumerate() {
            if e.get("prev").and_then(Value::as_str) != Some(prev.as_str()) {
                return Verified { ok: false, count: entries.len(), first_bad: Some(i), message: format!(
                    "entry {i} doesn't follow the one before it — an entry was edited or removed at or before here") };
            }
            if e.get("hash").and_then(Value::as_str) != Some(digest(e).as_str()) {
                return Verified { ok: false, count: entries.len(), first_bad: Some(i),
                                  message: format!("entry {i} was modified: its contents no longer match its hash") };
            }
            prev = e["hash"].as_str().unwrap_or("").to_string();
        }
        Verified { ok: true, count: entries.len(), first_bad: None,
                   message: format!("chain intact across {} entries", entries.len()) }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("chuck-ledger-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn canonical_json_is_pythons() {
        let v = json!({"b": "é\u{2028}\u{1}\"\\", "a": 1, "c": null, "d": "😀"});
        assert_eq!(canonical(&v), r#"{"a":1,"b":"\u00e9\u2028\u0001\"\\","c":null,"d":"\ud83d\ude00"}"#);
    }

    #[test]
    fn a_chain_that_verifies_and_breaks_where_edited() {
        let d = tmp("chain");
        let l = Ledger::at(&d.join("ledger.jsonl"));
        for i in 0..4 {
            l.record(&format!("echo {i} — ünïcode"), i, &format!("out {i} 😀"), "shell", "c1", "").unwrap();
        }
        let v = l.verify();
        assert!(v.ok && v.count == 4, "{v:?}");
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(std::fs::metadata(&l.path).unwrap().permissions().mode() & 0o777, 0o600);
        let text = std::fs::read_to_string(&l.path).unwrap().replace("echo 2", "echo 9");
        std::fs::write(&l.path, text).unwrap();
        let v = l.verify();
        assert!(!v.ok && v.first_bad == Some(2), "{v:?}");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn secrets_are_never_kept() {
        let r = redact("export API_KEY=abcd1234efgh\nAuthorization: Bearer xyz.123\nsk-abcdefghijklmnopqrstuv\n\
            -----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----");
        assert!(!r.contains("abcd1234") && !r.contains("xyz.123") && !r.contains("sk-abc") && !r.contains("MIIE"), "{r}");
        assert!(r.contains("API_KEY=[removed]"));
    }
}
