//! The client against a real stdio server (tests/fake_server.py): the
//! handshake, paginated tools, every kind of content, the server's own
//! requests, failures, timeouts, crashes — and nothing left running.

use chuck_mcp::{Manager, Server, Spec};
use serde_json::json;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

fn python() -> bool {
    chuck_mcp::which("python3").is_some()
}

fn spec() -> Spec {
    Spec { command: "python3".into(), args: vec![concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fake_server.py").into()],
           ..Spec::default() }
}

#[test]
fn a_server_is_started_used_and_stopped() {
    if !python() {
        return;
    }
    let roots: Arc<dyn Fn() -> Vec<PathBuf> + Send + Sync> = Arc::new(|| vec![PathBuf::from("/home/me/project")]);
    let s = Server::start("fake", &spec(), roots).unwrap();
    assert_eq!(s.info["version"], "1.2");
    let names: Vec<&str> = s.tools.iter().map(|t| t.name.as_str()).collect();
    assert_eq!(names, ["echo", "write_note", "roots", "image", "fails", "slow", "crash"], "both pages; the bad name dropped");
    assert!(s.tools[0].read_only && s.tools[1].destructive);

    // a call during which the server pings us
    let r = s.call("echo", json!({"text": "hi"}), Duration::from_secs(10)).unwrap();
    assert_eq!((r.ok, r.text.as_str()), (true, "echo: hi"));
    // roots/list answered; anything else the server asks is "method not found"
    let r = s.call("roots", json!({}), Duration::from_secs(10)).unwrap();
    assert_eq!(r.text, r#"["file:///home/me/project"] / -32601"#);
    let r = s.call("image", json!({}), Duration::from_secs(10)).unwrap();
    assert_eq!(r.images, vec![("image/png".to_string(), "iVBORw0KGgo=".to_string())]);
    assert_eq!(r.text, "resource text");
    let r = s.call("fails", json!({}), Duration::from_secs(10)).unwrap();
    assert_eq!((r.ok, r.text.as_str()), (false, "no such repo"));
    let e = s.call("nope", json!({}), Duration::from_secs(10)).unwrap_err();
    assert!(e.contains("unknown tool (-32602)"), "{e}");

    let t0 = Instant::now();
    let e = s.call("slow", json!({}), Duration::from_millis(500)).unwrap_err();
    assert!(e.contains("timed out") && t0.elapsed() < Duration::from_secs(3), "{e}");
    // the late answer to the call that timed out is dropped, not mistaken for the next one's
    let r = s.call("echo", json!({"text": "again"}), Duration::from_secs(10)).unwrap();
    assert_eq!(r.text, "echo: again");

    let e = s.call("crash", json!({}), Duration::from_secs(10)).unwrap_err();
    assert!(e.contains("exited") && e.contains("dying now"), "{e}");
    assert!(!s.alive());
    assert!(s.call("echo", json!({"text": "x"}), Duration::from_secs(1)).unwrap_err().contains("isn't running"));
}

#[test]
fn the_manager_lists_resolves_and_reports() {
    if !python() {
        return;
    }
    let d = std::env::temp_dir().join(format!("chuck-mcp-mgr-{}", std::process::id()));
    std::fs::create_dir_all(&d).unwrap();
    let cfg = d.join("mcp.json");
    std::fs::write(&cfg, format!(r#"{{"mcpServers": {{
        "fake": {{"command": "python3", "args": ["{}", "mgr-test"]}},
        "trusted": {{"command": "python3", "args": ["{}", "mgr-test"], "trust": true}},
        "missing": {{"command": "no-such-program-chuck"}}}}}}"#,
        concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fake_server.py"), concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fake_server.py")))
        .unwrap();
    let m = Manager::new(cfg);
    m.start_all();
    let st = m.status();
    assert_eq!(st.iter().map(|s| (s.name.as_str(), s.state.as_str())).collect::<Vec<_>>(),
               [("fake", "running"), ("missing", "failed"), ("trusted", "running")]);
    assert!(st[0].detail.starts_with("7 tools · v1.2"), "{:?}", st[0]);
    assert!(st[1].detail.contains("command not found"));

    let all = m.index(false, 80);
    assert!(all.contains("- fake.echo(text) [reads]: Echo the text back"));
    assert!(all.contains("- fake.write_note(path, text) [asks the user]: Write a note file"));
    assert!(all.contains("- trusted.write_note(path, text): Write"), "a trusted server's tools don't ask");
    let ro = m.index(true, 80);
    assert!(ro.contains("fake.echo") && !ro.contains("write_note"));

    assert!(m.resolve("fake.echo", &json!({"text": "x"})).is_ok());
    assert!(m.resolve("fake.echo", &json!({})).unwrap_err().contains("missing: text"));
    assert!(m.resolve("fake.nothing", &json!({})).unwrap_err().contains("has no tool 'nothing'"));
    assert!(m.resolve("missing.x", &json!({})).unwrap_err().contains("failed to start"));
    assert!(m.resolve("ghost.x", &json!({})).unwrap_err().contains("running: fake, trusted"));

    // every server ends with the manager
    assert_eq!(fake_servers_of_mine(), 2);
    drop(m);
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(fake_servers_of_mine(), 0);
    let _ = std::fs::remove_dir_all(&d);
}

/// Fake servers (not zombies) whose parent is this test process.
pub fn fake_servers_of_mine() -> usize {
    let me = std::process::id().to_string();
    std::fs::read_dir("/proc").unwrap().flatten().filter(|e| {
        let p = e.path();
        let cmd = std::fs::read(p.join("cmdline")).unwrap_or_default();
        let stat = std::fs::read_to_string(p.join("stat")).unwrap_or_default();
        let rest = stat.rsplit(')').next().unwrap_or("");
        let mut f = rest.split_whitespace();
        let (state, ppid) = (f.next().unwrap_or(""), f.next().unwrap_or(""));
        String::from_utf8_lossy(&cmd).contains("mgr-test") && ppid == me && state != "Z"
    }).count()
}
