//! Quitting Chuck ends its MCP servers (its own test binary: kill_all ends
//! every server this process started).

use chuck_mcp::{Server, Spec};
use std::sync::Arc;
use std::time::{Duration, Instant};

#[test]
fn kill_all_ends_servers_too() {
    if chuck_mcp::which("python3").is_none() {
        return;
    }
    let spec = Spec { command: "python3".into(), args: vec![concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fake_server.py").into()],
                      ..Spec::default() };
    let s = Server::start("fake", &spec, Arc::new(Vec::new)).unwrap();
    assert!(s.alive());
    chuck_tools::kill_all();
    let t0 = Instant::now();
    while s.alive() && t0.elapsed() < Duration::from_secs(5) {
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(!s.alive(), "Chuck quitting ends its MCP servers");
}
