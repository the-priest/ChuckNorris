//! A daemon woken by its trigger looks, changes nothing, and reports.

use chuck_model::{ChatRequest, Scripted};
use chuck_runtime::daemon::DaemonRun;
use chuck_runtime::{Events, Runtime, RuntimeConfig};
use std::sync::atomic::AtomicBool;
use std::sync::Arc;

fn last_user(req: &ChatRequest) -> String {
    req.messages.iter().rev().find(|m| m["role"] == "user").and_then(|m| m["content"].as_str()).unwrap_or("").to_string()
}

fn run(dir: &std::path::Path, model: Arc<Scripted>, tokens: u64) -> chuck_runtime::daemon::DaemonReport {
    let rt = Runtime::new(model, RuntimeConfig { store: dir.join(".store"), ..RuntimeConfig::default() }, Events::none());
    rt.daemon(DaemonRun { name: "nginx watch".into(), brief: "find out why nginx failed and propose a fix".into(),
                          why: "failed: nginx.service".into(), detail: "Failed units now: nginx.service".into(),
                          folder: dir.to_path_buf(), tokens, minutes: 5, steps: 6, stop: Arc::new(AtomicBool::new(false)) })
}

#[test]
fn a_daemon_looks_and_reports_and_changes_nothing() {
    let dir = std::env::temp_dir().join(format!("chuck-dmn-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("keep.txt"), "x").unwrap();
    let model = Arc::new(Scripted::new().replies([
        "Looking.\n```bash\nsystemctl restart nginx\n```\n```bash\necho looked-$((1+1))\n```\n```bash\nrm keep.txt\n```\n\
         ```write evil.txt\nx\n```\n```read keep.txt```",
        "nginx failed: port 80 in use.\nPropose: sudo systemctl restart nginx",
    ]));
    let r = run(&dir, model.clone(), 100_000);
    assert_eq!(r.status, 0);
    assert_eq!(r.report, "nginx failed: port 80 in use.\nPropose: sudo systemctl restart nginx");
    let seen = model.seen.lock().unwrap();
    let first = last_user(&seen[0]);
    assert!(first.contains("woken because: failed: nginx.service") && first.contains("Failed units now"), "{first}");
    assert!(seen[0].messages[0]["content"].as_str().unwrap().starts_with("You are a daemon"));
    let fb = last_user(&seen[1]);
    assert!(fb.contains("[NOT run: systemctl restart changes the system"), "{fb}");
    assert!(fb.contains("exit 0\nlooked-2"), "{fb}");
    assert!(fb.contains("[NOT run: rm isn't read-only"), "{fb}");
    assert!(fb.contains("[NOT written: evil.txt"), "{fb}");
    assert!(fb.contains("READ keep.txt"), "{fb}");
    assert!(dir.join("keep.txt").exists() && !dir.join("evil.txt").exists());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_daemon_out_of_budget_reports_what_it_has() {
    let dir = std::env::temp_dir().join(format!("chuck-dmn2-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    // a daemon that would look forever: its last turn is announced, and that turn is its report
    let model = Arc::new(Scripted::new().respond(|r| {
        if last_user(r).contains("LAST turn") { "Disk is fine so far; ran out of time.".into() } else { "```bash\ndf -h\n```".into() }
    }));
    let r = run(&dir, model, 100_000);
    assert_eq!((r.status, r.report.as_str()), (0, "Disk is fine so far; ran out of time."));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_daemon_whose_call_is_malformed_is_told_and_its_report_is_the_real_one() {
    let dir = std::env::temp_dir().join(format!("chuck-dmn3-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let model = Arc::new(Scripted::new().replies([
        "Checking.\n```bash systemctl --failed</parameter>",
        "<tool_call>{\"name\": \"bash\", \"arguments\": {\"command\": \"df -h\"}}</tool_call>",
        "```bash\necho disk-ok\n```",
        "Disk is fine: disk-ok.",
    ]));
    let r = run(&dir, model.clone(), 100_000);
    assert_eq!((r.status, r.report.as_str()), (0, "Disk is fine: disk-ok."));
    let seen = model.seen.lock().unwrap();
    assert!(last_user(&seen[1]).contains("NOT run"), "{}", last_user(&seen[1]));
    assert!(last_user(&seen[2]).contains("NOTHING RAN"), "{}", last_user(&seen[2]));
    let _ = std::fs::remove_dir_all(&dir);
}
