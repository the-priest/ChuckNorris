//! A server or emulator the Lead starts in the background, jailed, is still
//! there for its next command; quitting ends it.

use chuck_model::Scripted;
use chuck_runtime::{Events, Job, Runtime, RuntimeConfig};
use std::sync::Arc;

#[test]
fn a_background_server_outlives_its_command_until_the_quit() {
    if chuck_sandbox::bwrap().is_none() {
        eprintln!("skipped: no bubblewrap");
        return;
    }
    let dir = chuck_sandbox::jail::home().join(format!("chuck-bg-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let mark = 30_000 + std::process::id() % 1000;
    let model = Arc::new(Scripted::new().replies([
        format!("```criteria\n- up :: verify: true\n```\n```bash\nnohup sleep {mark} >/dev/null 2>&1 &\n```"),
        format!("```bash\nsleep 0.3; ps -eo args | grep -c '^sleep {mark}'\n```"),
        "ok".into(),
    ]));
    let cfg = RuntimeConfig { store: dir.join(".s"), access: chuck_sandbox::Access::Guarded, ..RuntimeConfig::default() };
    Runtime::new(model.clone(), cfg, Events::none()).run(Job::new("start it", &dir));
    let fb = model.seen.lock().unwrap()[2].messages.last().unwrap()["content"].as_str().unwrap().to_string();
    assert!(fb.contains("exit 0") && fb.lines().any(|l| l.trim() == "1"), "still running for the next command: {fb}");
    chuck_tools::kill_all();
    std::thread::sleep(std::time::Duration::from_millis(300));
    let left = std::fs::read_dir("/proc").unwrap().flatten().filter(|e| std::fs::read(e.path().join("cmdline"))
        .is_ok_and(|c| String::from_utf8_lossy(&c).replace('\0', " ").starts_with(&format!("sleep {mark}")))).count();
    assert_eq!(left, 0, "and gone after the quit");
    let _ = std::fs::remove_dir_all(&dir);
}
