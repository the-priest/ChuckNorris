//! The memory remembers, recalls and forgets exactly as the Python app's
//! (tests/memory_golden.json was made by running its memory.py).

use chuck_store::memory::Memory;
use serde_json::{json, Value};

#[test]
fn the_same_memory_as_the_python_app() {
    let g: Value = serde_json::from_str(include_str!("memory_golden.json")).unwrap();
    let dir = std::env::temp_dir().join(format!("chuck-memory-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let m = Memory::at(&dir.join("facts.jsonl"));
    let mut bad = Vec::new();
    for s in g["steps"].as_array().unwrap() {
        let (op, text, scope) = (s["op"].as_str().unwrap(), s["text"].as_str().unwrap(), s["scope"].as_str().unwrap_or(""));
        let got = match op {
            "remember" => {
                let (ok, msg) = m.remember(text, s["kind"].as_str().unwrap(), 1.0, scope);
                json!([ok, msg])
            }
            "forget" => {
                let (ok, msg) = m.forget(text);
                json!([ok, msg])
            }
            _ => json!(m.recall(text, scope)),
        };
        if got != s["out"] {
            bad.push(format!("{op} {text:?}: got {got}, want {}", s["out"]));
        }
    }
    let mut finals: Vec<Value> = m.all().iter().map(|f| json!({"text": f.text(), "kind": f.kind(), "scope": f.scope()})).collect();
    let mut want = g["final"].as_array().unwrap().clone();
    let key = |v: &Value| v["text"].as_str().unwrap().to_string();
    finals.sort_by_key(key);
    want.sort_by_key(key);
    if finals != want {
        bad.push(format!("final store: got {finals:?}\nwant {want:?}"));
    }
    // a store from another process: read back as it is
    let again = Memory::at(&dir.join("facts.jsonl"));
    assert_eq!(again.all().len(), want.len());
    let _ = std::fs::remove_dir_all(&dir);
    assert!(bad.is_empty(), "{}", bad.join("\n"));
}
