//! The window (Python) and `chuck` (Rust) write ONE evidence chain: each
//! must extend the other's entries and verify the whole chain. Skipped when
//! python3 isn't installed.

use chuck_store::{redact, Ledger};
use std::path::{Path, PathBuf};
use std::process::Command;

fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..").canonicalize().unwrap()
}

fn python(ledger: &Path, code: &str) -> Option<String> {
    let prelude = format!("import sys; sys.path.insert(0, {:?})\nfrom chucknorris_ext import ledger as L\n\
        from pathlib import Path\nL.LEDGER = Path({:?}); L.ANCHOR = Path({:?})\n",
        repo().display().to_string(), ledger.display().to_string(),
        ledger.with_file_name("ledger.anchor").display().to_string());
    let home = ledger.parent().unwrap();
    let out = Command::new("python3").arg("-c").arg(prelude + code).env("HOME", home).output().ok()?;
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    Some(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

#[test]
fn one_chain_written_by_both() {
    let d = std::env::temp_dir().join(format!("chuck-interop-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    let path = d.join("ledger.jsonl");
    if python(&path, "L.record('echo from the window — é', 0, 'out 😀', kind='shell', chat_id='w1')").is_none() {
        eprintln!("python3 not available: skipped");
        return;
    }
    let l = Ledger::at(&path);
    l.record("echo from chuck — ü", 3, "tab\tand \u{1} control", "shell", "chuck-1", "").unwrap();
    python(&path, "L.record('ls', 0, 'a\\nb', kind='shell', chat_id='w1')").unwrap();
    l.record("cargo test", 0, "ok", "shell", "chuck-1", "").unwrap();
    let py = python(&path, "ok, n, bad, msg = L.verify(); print(ok, n, bad)").unwrap();
    assert_eq!(py, "True 4 None", "the window must verify chuck's entries");
    let v = l.verify();
    assert!(v.ok && v.count == 4, "chuck must verify the window's entries: {v:?}");
    let _ = std::fs::remove_dir_all(&d);
}

#[test]
fn redaction_matches_the_window() {
    let d = std::env::temp_dir().join(format!("chuck-redact-{}", std::process::id()));
    std::fs::create_dir_all(&d).unwrap();
    let samples = ["export API_KEY=abcd1234efgh", "Authorization: Bearer abc.def", "password: \"hunter22\"",
                   "token='xyz12345'", "sk-abcdefghijklmnopqrstuvwx", "ghp_abcdefghijklmnopqrstuvwxyz123",
                   "AKIAABCDEFGHIJKLMNOP", "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----",
                   "plain text, nothing secret", "my_secret_value = s3cr3t!"];
    for s in samples {
        let Some(py) = python(&d.join("ledger.jsonl"), &format!("print(L.redact({s:?}), end='')")) else { return };
        assert_eq!(redact(s), py, "{s}");
    }
    let _ = std::fs::remove_dir_all(&d);
}
