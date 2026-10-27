//! The gate counts a criterion verified when its check really passed after
//! the last change: its verify command (a redundant `cd <folder> &&` aside),
//! or a run the agent linked to it with `for cN` — and a piped test can't
//! hide a failure.

use chuck_model::Scripted;
use chuck_runtime::{Events, Job, Runtime, RuntimeConfig};
use std::sync::Arc;

#[test]
fn equivalent_and_linked_runs_count_failures_in_pipes_dont() {
    let dir = std::env::temp_dir().join(format!("chuck-vmatch-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let dir = dir.canonicalize().unwrap();
    let r = dir.display().to_string();
    let model = Arc::new(Scripted::new().replies([
        format!("```criteria\n- a :: verify: cd \"{r}\" && test -f a.txt\n- b :: verify: test -f b.txt\n```\n\
                 ```write a.txt\nA\n```\n```write b.txt\nB\n```"),
        // a's verify without the cd; b checked another way, linked explicitly
        "```bash\ntest -f a.txt\n```\n```test grep -q B b.txt for c2```".into(),
        // a failure piped through tail still fails
        "```test false | tail -1```".into(),
        "Checked.".into(),
    ]));
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join(".s"), ..RuntimeConfig::default() }, Events::none());
    let rep = rt.run(Job::new("make a and b", &dir));
    let seen = model.seen.lock().unwrap();
    let after2 = seen[2].messages.last().unwrap()["content"].as_str().unwrap().to_string();
    assert!(!after2.contains("c1's verification") && !after2.contains("c2's verification"), "{after2}");
    let after3 = seen[3].messages.last().unwrap()["content"].as_str().unwrap().to_string();
    assert!(after3.contains("TEST `false | tail -1` → exit 1"), "{after3}");
    drop(seen);
    assert_eq!(rep.gate.criteria[0].verify.as_deref(), Some("test -f a.txt"), "kept without the folder cd");
    let _ = std::fs::remove_dir_all(&dir);
}
