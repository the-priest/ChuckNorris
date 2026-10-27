//! Chat mode: answers directly, researches when it should, never acts, and
//! hands work to the fleet only as a proposal.

use chuck_model::{ChatRequest, Scripted};
use chuck_runtime::{Event, Events, Job, Outcome, Runtime, RuntimeConfig};
use std::path::PathBuf;
use std::sync::mpsc::channel;
use std::sync::Arc;

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("chuck-chat-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn last_user(req: &ChatRequest) -> String {
    req.messages.iter().rev().find(|m| m["role"] == "user").and_then(|m| m["content"].as_str()).unwrap_or("").to_string()
}

#[test]
fn a_question_gets_an_answer() {
    let dir = tmp("plain");
    let model = Arc::new(Scripted::new().replies(["`ls -la` lists every file, hidden ones included, with details."]));
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() }, Events::none());
    let r = rt.chat(Job::new("what does ls -la do?", &dir));
    assert_eq!(r.outcome, Outcome::Answered { text: "`ls -la` lists every file, hidden ones included, with details.".into() });
    let sys = model.seen.lock().unwrap()[0].messages[0]["content"].as_str().unwrap().to_string();
    assert!(sys.contains("CHAT MODE") && sys.contains("```agent"));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn chat_mode_never_acts_whatever_the_model_writes() {
    let dir = tmp("noact");
    std::fs::write(dir.join("keep.txt"), "x").unwrap();
    let model = Arc::new(Scripted::new().replies([
        "Here's how:\n```bash\nrm -f keep.txt; touch pwned\n```\n```write pwned2\nhi\n```\nRun it yourself."]));
    let rt = Runtime::new(model, RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() }, Events::none());
    let r = rt.chat(Job::new("how do I delete keep.txt?", &dir));
    let Outcome::Answered { text } = r.outcome else { panic!("{:?}", r.outcome) };
    assert!(text.contains("rm -f keep.txt"), "code is shown to read and copy: {text}");
    assert!(dir.join("keep.txt").exists() && !dir.join("pwned").exists() && !dir.join("pwned2").exists());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn work_on_the_machine_is_handed_to_the_fleet_as_a_proposal() {
    let dir = tmp("handoff");
    let model = Arc::new(Scripted::new().replies([
        "That needs changes on your machine — the fleet can do it.\n```agent\nInstall ripgrep and add an alias rg='rg --smart-case' to ~/.bashrc\n```"]));
    let (tx, rx) = channel();
    let rt = Runtime::new(model, RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() }, Events(Some(tx)));
    let r = rt.chat(Job::new("set up ripgrep for me", &dir));
    assert_eq!(r.outcome, Outcome::Answered { text: "That needs changes on your machine — the fleet can do it.".into() });
    let events: Vec<Event> = rx.try_iter().collect();
    assert!(events.contains(&Event::Handoff {
        request: "Install ripgrep and add an alias rg='rg --smart-case' to ~/.bashrc".into() }), "{events:?}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn research_is_bounded_and_then_the_answer_is_asked_for() {
    let dir = tmp("bounded");
    // a model that would search forever
    let model = Arc::new(Scripted::new().respond(|req| {
        if last_user(req).contains("answer now") { "Best I could find: it depends on the kernel version.".into() }
        else { "```search one more thing```".into() }
    }));
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() }, Events::none());
    let r = rt.chat(Job::new("is this bug fixed?", &dir));
    assert_eq!(r.outcome, Outcome::Answered { text: "Best I could find: it depends on the kernel version.".into() });
    assert_eq!(model.seen.lock().unwrap().len(), 9, "eight rounds of research, then the answer");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn earlier_turns_are_remembered_and_a_promise_is_followed_through() {
    let dir = tmp("history");
    let model = Arc::new(Scripted::new().replies(["Let me look that up.", "Arch ships it in extra."]));
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() }, Events::none());
    let mut job = Job::new("and on Arch?", &dir);
    job.history = vec![("user".into(), "where do I get ripgrep on Debian?".into()), ("lead".into(), "apt install ripgrep".into())];
    let r = rt.chat(job);
    assert_eq!(r.outcome, Outcome::Answered { text: "Arch ships it in extra.".into() });
    let seen = model.seen.lock().unwrap();
    assert!(last_user(&seen[0]).contains("apt install ripgrep") && last_user(&seen[0]).contains("NOW:\nand on Arch?"));
    assert!(last_user(&seen[1]).contains("didn't do it"));
    let _ = std::fs::remove_dir_all(&dir);
}
