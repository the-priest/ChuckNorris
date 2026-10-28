//! MCP tools in the fleet and in Chat mode, against a real stdio server
//! (chuck-mcp's tests/fake_server.py): what may run, when, and on whose yes.

use chuck_mcp::Manager;
use chuck_model::{ChatRequest, Scripted};
use chuck_runtime::{Event, Events, Job, Outcome, Runtime, RuntimeConfig};
use std::path::{Path, PathBuf};
use std::sync::mpsc::channel;
use std::sync::Arc;
use std::time::Duration;

const FAKE: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../chuck-mcp/tests/fake_server.py");

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("chuck-mcpf-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn last_user(req: &ChatRequest) -> String {
    req.messages.iter().rev().find(|m| m["role"] == "user").and_then(|m| m["content"].as_str()).unwrap_or("").to_string()
}

/// A manager with the fake server as "notes" (and as "mine", trusted).
fn servers(dir: &Path) -> Option<Arc<Manager>> {
    chuck_mcp::which("python3")?;
    let cfg = dir.join("mcp.json");
    std::fs::write(&cfg, format!(r#"{{"mcpServers": {{"notes": {{"command": "python3", "args": ["{FAKE}"]}},
        "mine": {{"command": "python3", "args": ["{FAKE}"], "trust": true}}}}}}"#)).unwrap();
    let m = Arc::new(Manager::new(cfg));
    m.start_all();
    Some(m)
}

fn rt(model: Arc<Scripted>, dir: &Path, mcp: Arc<Manager>, events: Events) -> Runtime {
    Runtime::new(model, RuntimeConfig { store: dir.join("store"), mcp: Some(mcp), lead_steps: 3,
                                        ..RuntimeConfig::default() }, events)
}

fn run_within(secs: u64, rt: Runtime, job: Job, chat: bool) -> chuck_runtime::JobReport {
    let (tx, rx) = channel();
    std::thread::spawn(move || {
        let _ = tx.send(if chat { rt.chat(job) } else { rt.run(job) });
    });
    rx.recv_timeout(Duration::from_secs(secs)).expect("the job hung")
}

#[test]
fn before_the_criteria_only_reading_tools_run() {
    let dir = tmp("before");
    let Some(m) = servers(&dir) else { return };
    let note = dir.join("note.txt");
    let model = Arc::new(Scripted::new().replies([
        format!("```mcp notes.echo\n{{\"text\": \"hello\"}}\n```\n```mcp notes.write_note\n{{\"path\": \"{}\", \"text\": \"x\"}}\n```",
                note.display()),
        "Stopping.".into()]));
    let r = rt(model.clone(), &dir, m.clone(), Events::none());
    run_within(30, r, Job::new("note it", &dir), false);
    let fb = last_user(&model.seen.lock().unwrap()[1]);
    assert!(fb.contains("[mcp notes.echo OK]\necho: hello"), "{fb}");
    assert!(fb.contains("[mcp notes.write_note refused: before the acceptance criteria"), "{fb}");
    assert!(!note.exists());
    let sys = model.seen.lock().unwrap()[0].messages[0]["content"].as_str().unwrap().to_string();
    assert!(sys.contains("- notes.echo(text) [reads]") && sys.contains("- notes.write_note(path, text) [asks the user]")
            && sys.contains("- mine.write_note(path, text): Write"), "{sys}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_tool_that_may_destroy_things_runs_on_the_users_yes_or_a_trusted_server() {
    let dir = tmp("yes");
    let Some(m) = servers(&dir) else { return };
    for (server, answer, runs, asked) in [("notes", "no", false, true), ("notes", "yes", true, true), ("mine", "", true, false)] {
        let note = dir.join(format!("note-{server}-{answer}.txt"));
        let model = Arc::new(Scripted::new().replies([
            format!("```criteria\n- the note exists :: verify: test -f {0}\n```\n```mcp {server}.write_note\n\
                     {{\"path\": \"{0}\", \"text\": \"x\"}}\n```", note.display()),
            "Stopping.".into()]));
        let (tx, rx) = channel();
        let (atx, arx) = channel();
        let mut job = Job::new("write the note", &dir);
        job.answers = Some(arx);
        let confirms = Arc::new(std::sync::Mutex::new(0));
        let c2 = confirms.clone();
        std::thread::spawn(move || {
            for e in rx.iter() {
                if let Event::Confirm { command, .. } = e {
                    assert!(command.starts_with(&format!("mcp {server}.write_note")), "{command}");
                    *c2.lock().unwrap() += 1;
                    let _ = atx.send(answer.to_string());
                }
            }
        });
        run_within(30, rt(model.clone(), &dir, m.clone(), Events(Some(tx))), job, false);
        assert_eq!(note.exists(), runs, "{server} {answer}");
        assert_eq!(*confirms.lock().unwrap() > 0, asked, "{server} {answer}");
        let fb = last_user(&model.seen.lock().unwrap()[1]);
        assert_eq!(fb.contains("didn't approve"), !runs, "{fb}");
    }
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn an_offline_folder_and_chat_mode_keep_mcp_in_bounds() {
    let dir = tmp("bounds");
    let Some(m) = servers(&dir) else { return };
    let model = Arc::new(Scripted::new().replies(["```mcp notes.echo {\"text\": \"x\"}```", "ok"]));
    let r = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), mcp: Some(m.clone()),
                                                        access: chuck_sandbox::Access::Sandbox, ..RuntimeConfig::default() },
                         Events::none());
    run_within(30, r, Job::new("x", &dir), false);
    assert!(last_user(&model.seen.lock().unwrap()[1]).contains("sandboxed offline"));

    // Chat: reading tools yes, anything else no
    let note = dir.join("chat-note.txt");
    let model = Arc::new(Scripted::new().replies([
        format!("```mcp notes.echo\n{{\"text\": \"from chat\"}}\n```\n```mcp mine.write_note\n{{\"path\": \"{}\", \"text\": \"x\"}}\n```",
                note.display()),
        "Done.".into()]));
    let r = rt(model.clone(), &dir, m.clone(), Events::none());
    let rep = run_within(30, r, Job::new("note it", &dir), true);
    assert_eq!(rep.outcome, Outcome::Answered { text: "Done.".into() });
    let seen = model.seen.lock().unwrap();
    let fb = last_user(&seen[1]);
    assert!(fb.contains("echo: from chat"), "{fb}");
    assert!(fb.contains("in Chat mode only MCP tools that only read can run"), "{fb}");
    assert!(!note.exists());
    let sys = seen[0].messages[0]["content"].as_str().unwrap();
    assert!(sys.contains("notes.echo") && !sys.contains("write_note"), "Chat is shown only the reading tools");
    let _ = std::fs::remove_dir_all(&dir);
}
