//! Snags: everything that slowed an agent down, limited it or went wrong,
//! written down so it can be fixed. The runtime records what it sees itself
//! (a call that didn't run, an edit that missed, a refusal, a stalled
//! API); in debug mode the agents add their own, with the
//! `snag` tool. One JSON line each, in ~/.local/state/chucknorris/snags.jsonl
//! (XDG_STATE_HOME honoured), turned over at 2 MB, readable only by you,
//! anything shaped like a key blanked first.

use serde::{Deserialize, Serialize};
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

const MAX_BYTES: u64 = 2_000_000;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Snag {
    pub at: String,
    /// Who hit it: lead, ds-2, chuck…
    pub who: String,
    /// What sort: "call not run", "edit missed", "refused", "stuck", "api", "said" (an agent's own)…
    pub kind: String,
    pub text: String,
}

/// Where snags go (None: nowhere — tests, or turned off).
static PATH: Mutex<Option<PathBuf>> = Mutex::new(None);

pub fn default_path() -> PathBuf {
    crate::crashlog::log_path().with_file_name("snags.jsonl")
}

/// Start (or stop, with None) writing snags down.
pub fn set_path(p: Option<PathBuf>) {
    *PATH.lock().unwrap_or_else(|e| e.into_inner()) = p;
}

pub fn path() -> Option<PathBuf> {
    PATH.lock().unwrap_or_else(|e| e.into_inner()).clone()
}

/// Write one down. Never fails: noting a problem must not become one.
pub fn record(who: &str, kind: &str, text: &str) {
    let guard = PATH.lock().unwrap_or_else(|e| e.into_inner());
    let Some(p) = guard.as_ref() else { return };
    let s = Snag { at: crate::ledger::local_stamp("%Y-%m-%d %H:%M:%S"), who: who.into(), kind: kind.into(),
                   text: crate::redact(&text.chars().take(600).collect::<String>()) };
    let _ = (|| -> std::io::Result<()> {
        std::fs::create_dir_all(p.parent().unwrap_or(p))?;
        if p.metadata().is_ok_and(|m| m.len() > MAX_BYTES) {
            std::fs::rename(p, p.with_extension("jsonl.1"))?;
        }
        let mut f = std::fs::OpenOptions::new().create(true).append(true).mode(0o600).open(p)?;
        writeln!(f, "{}", serde_json::to_string(&s).unwrap_or_default())
    })();
}

/// The last `n` snags in `p`, oldest first.
pub fn read(p: &Path, n: usize) -> Vec<Snag> {
    let text = std::fs::read_to_string(p).unwrap_or_default();
    let all: Vec<Snag> = text.lines().filter_map(|l| serde_json::from_str(l).ok()).collect();
    all[all.len().saturating_sub(n)..].to_vec()
}

/// What keeps happening: (kind, how often, the latest example), most often first.
pub fn summary(snags: &[Snag]) -> Vec<(String, usize, String)> {
    let mut out: Vec<(String, usize, String)> = Vec::new();
    for s in snags {
        match out.iter_mut().find(|(k, _, _)| *k == s.kind) {
            Some(e) => {
                e.1 += 1;
                e.2 = format!("{}: {}", s.who, s.text);
            }
            None => out.push((s.kind.clone(), 1, format!("{}: {}", s.who, s.text))),
        }
    }
    out.sort_by_key(|e| std::cmp::Reverse(e.1));
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn snags_are_written_read_and_summed_up() {
        let p = std::env::temp_dir().join(format!("chuck-snags-{}.jsonl", std::process::id()));
        let _ = std::fs::remove_file(&p);
        set_path(Some(p.clone()));
        record("ds-1", "edit missed", "SEARCH text not found in a.js");
        record("ds-2", "edit missed", "SEARCH text not found in b.js");
        record("lead", "said", "the test tool can't take a criterion");
        record("ds-1", "said", "key sk-abcdefghijklmnopqrstuvwxyz123456 in the log");
        set_path(None);
        record("ds-9", "lost", "not written: no path");
        let s = read(&p, 10);
        assert_eq!(s.len(), 4);
        assert!(!s[3].text.contains("sk-abcdefghij"), "keys are blanked");
        let sum = summary(&s);
        assert_eq!((sum[0].0.as_str(), sum[0].1), ("edit missed", 2));
        assert!(sum[0].2.starts_with("ds-2:"), "the latest example");
        let _ = std::fs::remove_file(&p);
    }
}
