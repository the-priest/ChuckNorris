//! Quitting ends everything an agent started, even what it left running in
//! the background or what detached itself (nohup, setsid, a daemon).

use chuck_tools::{bash, kill_all, session_pids, RunOpts};
use std::time::Duration;

fn alive(marker: &str) -> usize {
    std::fs::read_dir("/proc").unwrap().flatten().filter(|e| {
        std::fs::read(e.path().join("cmdline")).is_ok_and(|c| String::from_utf8_lossy(&c).replace('\0', " ").contains(marker))
    }).count()
}

#[test]
fn nothing_survives_the_quit() {
    let d = std::env::temp_dir();
    let m = format!("{}", 7_000 + std::process::id() % 1000);
    // three ways to outlive the command that started them
    for cmd in [format!("nohup sleep {m}1 >/dev/null 2>&1 &"),
                format!("setsid sleep {m}2 >/dev/null 2>&1 < /dev/null &"),
                format!("(sleep {m}3 >/dev/null 2>&1 &) ; disown 2>/dev/null; true")] {
        let r = bash(&cmd, RunOpts::new(&d));
        assert_eq!(r.exit, 0, "{cmd}: {}", r.output);
    }
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(alive(&format!("sleep {m}")), 3, "they outlived their commands");
    assert!(session_pids().len() >= 3);
    kill_all();
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(alive(&format!("sleep {m}")), 0, "and are gone after the quit");
    // something not started by Chuck is untouched
    let mut outside = std::process::Command::new("sleep").arg(format!("{m}9")).spawn().unwrap();
    kill_all();
    assert!(outside.try_wait().unwrap().is_none(), "not Chuck's: still running");
    let _ = outside.kill();
    let _ = outside.wait();
}
