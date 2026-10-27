//! ```patch``` and ```find``` through the Lead: a diff applied whole (or
//! not at all), recorded as evidence; code found by what it does.

use chuck_model::{ChatRequest, Scripted};
use chuck_runtime::{Events, Job, Runtime, RuntimeConfig};
use std::path::PathBuf;
use std::sync::Arc;

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("chuck-patchfind-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn feedback(seen: &[ChatRequest], i: usize) -> String {
    seen[i].messages.iter().rev().find(|m| m["role"] == "user").and_then(|m| m["content"].as_str()).unwrap_or("").to_string()
}

#[test]
fn the_lead_finds_code_and_patches_it_whole_or_not_at_all() {
    let dir = tmp("lead");
    std::fs::write(dir.join("net.py"), "def fetch_with_backoff(url):\n    \"\"\"Retry failed requests.\"\"\"\n    return 1\n").unwrap();
    std::fs::write(dir.join("util.py"), "def add(a, b):\n    return a + b\n").unwrap();
    let good = "```patch\n--- a/net.py\n+++ b/net.py\n@@ -3 +3 @@\n-    return 1\n+    return 2\n--- /dev/null\n+++ b/notes.md\n@@ -0,0 +1 @@\n+retry is in net.py\n```";
    let bad = "```patch\n--- a/util.py\n+++ b/util.py\n@@ -2 +2 @@\n-    return a + b\n+    return a - b\n--- a/net.py\n+++ b/net.py\n@@ -1 +1 @@\n-def nothing_like_this():\n+x\n```";
    let model = Arc::new(Scripted::new().replies([
        "```find where are failed requests retried```".to_string(),
        format!("```criteria\n- net returns 2 :: verify: grep -q 'return 2' net.py\n```\n{good}"),
        bad.to_string(),
        "Done looking.".to_string(),
    ]));
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join(".store"), ..RuntimeConfig::default() }, Events::none());
    rt.run(Job::new("change the retry", &dir));
    let seen = model.seen.lock().unwrap();
    let f = feedback(&seen, 1);
    assert!(f.contains("FIND \"where are failed requests retried\"") && f.contains("net.py:1-3  fetch_with_backoff"), "{f}");
    let p = feedback(&seen, 2);
    assert!(p.contains("patched 2 file(s) (+2 −1): net.py, notes.md (new)"), "{p}");
    assert_eq!(std::fs::read_to_string(dir.join("net.py")).unwrap(), "def fetch_with_backoff(url):\n    \"\"\"Retry failed requests.\"\"\"\n    return 2\n");
    assert_eq!(std::fs::read_to_string(dir.join("notes.md")).unwrap(), "retry is in net.py\n");
    let p = feedback(&seen, 3);
    assert!(p.contains("patch NOT applied, nothing was changed: net.py: hunk 1 doesn't match"), "{p}");
    assert_eq!(std::fs::read_to_string(dir.join("util.py")).unwrap(), "def add(a, b):\n    return a + b\n", "all or nothing");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_patch_before_the_criteria_changes_nothing() {
    let dir = tmp("gate");
    std::fs::write(dir.join("a.txt"), "x\n").unwrap();
    let model = Arc::new(Scripted::new().replies(["```patch\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-x\n+y\n```", "ok"]));
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join(".store"), ..RuntimeConfig::default() }, Events::none());
    rt.run(Job::new("change a", &dir));
    assert!(feedback(&model.seen.lock().unwrap(), 1).contains("nothing may change yet"));
    assert_eq!(std::fs::read_to_string(dir.join("a.txt")).unwrap(), "x\n");
    let _ = std::fs::remove_dir_all(&dir);
}
