//! Long jobs: an API outage is waited out, exploring a big project for
//! many turns is work (not "going in circles"), and only being stuck — not
//! a job's length — pauses it.

use chuck_model::{ChatModel, ChatRequest, ModelError, Outcome as ModelOutcome, Scripted, Sink};
use chuck_runtime::{Event, Events, Job, Outcome, Runtime, RuntimeConfig};
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("chuck-endure-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

/// The network is down for the first `down` calls.
struct Flaky {
    down: usize,
    calls: AtomicUsize,
    inner: Scripted,
}

impl ChatModel for Flaky {
    fn chat(&self, req: &ChatRequest, sink: &mut dyn Sink) -> Result<ModelOutcome, ModelError> {
        if self.calls.fetch_add(1, Ordering::SeqCst) < self.down {
            return Err(ModelError::Network("connection refused".into()));
        }
        self.inner.chat(req, sink)
    }
}

#[test]
fn an_outage_is_waited_out() {
    let dir = tmp("outage");
    let model = Arc::new(Flaky { down: 4, calls: AtomicUsize::new(0), inner: Scripted::new().replies(["Back, and here's the answer."]) });
    let (tx, rx) = std::sync::mpsc::channel();
    let cfg = RuntimeConfig { store: dir.join(".s"), outage_wait: Duration::from_secs(2), ..RuntimeConfig::default() };
    let r = Runtime::new(model, cfg, Events(Some(tx))).run(Job::new("hi", &dir));
    assert_eq!(r.outcome, Outcome::Answered { text: "Back, and here's the answer.".into() });
    let waits = rx.try_iter().filter(|e| matches!(e, Event::Notice { text, .. } if text.contains("the API isn't answering"))).count();
    assert_eq!(waits, 4);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn an_outage_longer_than_the_wait_pauses_the_job() {
    let dir = tmp("long-outage");
    let model = Arc::new(Flaky { down: 1000, calls: AtomicUsize::new(0), inner: Scripted::new() });
    let cfg = RuntimeConfig { store: dir.join(".s"), outage_wait: Duration::from_millis(500), ..RuntimeConfig::default() };
    let t = std::time::Instant::now();
    let r = Runtime::new(model, cfg, Events::none()).run(Job::new("hi", &dir));
    assert!(matches!(&r.outcome, Outcome::Paused { gaps } if gaps[0].contains("connection refused")), "{:?}", r.outcome);
    assert!(t.elapsed() < Duration::from_secs(5));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn exploring_for_many_turns_is_work_and_spinning_is_stopped() {
    let dir = tmp("explore");
    for i in 0..40 {
        std::fs::write(dir.join(format!("m{i}.py")), format!("x = {i}\n")).unwrap();
    }
    // forty turns of reading different files, then the answer: never "going in circles"
    let mut script: Vec<String> = (0..40).map(|i| format!("```read m{i}.py```")).collect();
    script.push("Every module sets x to its number.".into());
    let model = Arc::new(Scripted::new().replies(script));
    let r = Runtime::new(model.clone(), RuntimeConfig { store: dir.join(".s"), ..RuntimeConfig::default() }, Events::none())
        .run(Job::new("what do the modules do?", &dir));
    assert_eq!(r.outcome, Outcome::Answered { text: "Every module sets x to its number.".into() });
    assert!(!model.seen.lock().unwrap().iter().any(|q| q.messages.iter().any(|m| m["content"].as_str()
        .is_some_and(|c| c.contains("going in circles")))));
    // reading the same file forever: told to replan, then paused as stuck — it never runs on forever
    let model = Arc::new(Scripted::new().respond(|_| "```read m1.py```".into()));
    let r = Runtime::new(model.clone(), RuntimeConfig { store: dir.join(".s"), ..RuntimeConfig::default() }, Events::none())
        .run(Job::new("loop", &dir));
    assert!(matches!(&r.outcome, Outcome::Paused { gaps } if gaps[0].contains("stuck")), "{:?}", r.outcome);
    assert!(model.calls() < 40, "{} calls", model.calls());
    let _ = std::fs::remove_dir_all(&dir);
}
