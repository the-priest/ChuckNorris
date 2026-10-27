//! A paused job carries on exactly where it stopped: the same criteria and
//! evidence, the Lead's conversation, task numbers after the old ones, and a
//! job that finishes clears what was kept.

use chuck_model::{ChatRequest, Scripted};
use chuck_runtime::{resume, Events, Job, Outcome, Runtime, RuntimeConfig};
use std::path::PathBuf;
use std::sync::Arc;

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("chuck-resume-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn text(m: &serde_json::Value) -> String {
    m["content"].as_str().unwrap_or("").to_string()
}

#[test]
fn a_paused_job_carries_on_where_it_stopped() {
    let dir = tmp("job");
    let keep = dir.join(".resume");
    // run 1: criteria and a change, then out of steps
    let first = Arc::new(Scripted::new().replies([
        "```criteria\n- hello.txt says hi :: verify: grep -q hi hello.txt\n```\n```write hello.txt\nhi\n```",
        "Writing more next.\n```read hello.txt```",
    ]));
    let cfg = RuntimeConfig { store: dir.join(".s"), lead_steps: 2, resume_dir: Some(keep.clone()), ..RuntimeConfig::default() };
    let r1 = Runtime::new(first, cfg.clone(), Events::none()).run(Job::new("make hello.txt say hi", &dir));
    assert!(matches!(&r1.outcome, Outcome::Paused { gaps } if gaps[0].contains("used all 2 steps")), "{:?}", r1.outcome);
    let saved = resume::load_in(&keep, &dir).expect("kept on disk");
    assert_eq!(saved.ask, "make hello.txt say hi");
    assert!(saved.mutated && saved.gate.criteria.len() == 1 && !saved.gate.evidence.is_empty());
    assert!(saved.convo.len() >= 4);

    // run 2: carries on — no new criteria needed, the verify runs, done
    let second = Arc::new(Scripted::new().respond(|r: &ChatRequest| {
        let sys = r.messages[0]["content"].as_str().unwrap_or("");
        if sys.starts_with("You are an independent") {
            return "ACCEPT".into();
        }
        "```test grep -q hi hello.txt```\n```done\nhello.txt says hi; verified with grep.\n```".into()
    }));
    let cfg2 = RuntimeConfig { lead_steps: 20, ..cfg };
    let mut job = Job::new("and keep it short", &dir);
    job.resume = Some(saved);
    let r2 = Runtime::new(second.clone(), cfg2, Events::none()).run(job);
    assert!(matches!(r2.outcome, Outcome::Done { .. }), "{:?}", r2.outcome);
    let seen = second.seen.lock().unwrap();
    let lead_first = &seen[0].messages;
    // the old conversation is there, with the note about carrying on and the user's words
    assert!(lead_first.iter().any(|m| text(m).contains("```write hello.txt")), "the earlier turns are kept");
    let last = text(lead_first.last().unwrap());
    assert!(last.contains("CONTINUING this job") && last.contains("The user adds: and keep it short"), "{last}");
    assert_eq!(r2.gate.criteria.len(), 1, "the same criteria");
    assert!(resume::load_in(&keep, &dir).is_none(), "a finished job leaves nothing to continue");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_plain_answer_keeps_nothing() {
    let dir = tmp("answer");
    let keep = dir.join(".resume");
    let m = Arc::new(Scripted::new().replies(["It's a parser."]));
    let cfg = RuntimeConfig { store: dir.join(".s"), resume_dir: Some(keep.clone()), ..RuntimeConfig::default() };
    let r = Runtime::new(m, cfg, Events::none()).run(Job::new("what is this?", &dir));
    assert!(r.resume.is_none() && resume::load_in(&keep, &dir).is_none());
    let _ = std::fs::remove_dir_all(&dir);
}

/// Paused in a conversation that had history (a chat first, then the job),
/// then carried on: the task is found where it was, the earlier turns are
/// kept once, and every request alternates as a strict API needs.
#[test]
fn a_job_that_followed_a_chat_carries_on_with_its_history_intact() {
    let dir = tmp("history");
    let keep = dir.join(".resume");
    let first = Arc::new(Scripted::new().replies([
        "```criteria\n- hello.txt says hi :: verify: grep -q hi hello.txt\n```\n```write hello.txt\nhi\n```",
        "More next.\n```read hello.txt```",
    ]));
    let cfg = RuntimeConfig { store: dir.join(".s"), lead_steps: 2, resume_dir: Some(keep.clone()), ..RuntimeConfig::default() };
    let mut job = Job::new("make hello.txt say hi", &dir);
    job.history = vec![("user".into(), "what's in this folder?".into()), ("lead".into(), "Nothing yet.".into())];
    let r1 = Runtime::new(first, cfg.clone(), Events::none()).run(job);
    assert!(matches!(r1.outcome, Outcome::Paused { .. }), "{:?}", r1.outcome);
    let saved = resume::load_in(&keep, &dir).unwrap();
    assert_eq!(saved.head, 3, "the task comes after the two earlier turns");
    assert_eq!(text(&saved.convo[3]), "make hello.txt say hi");

    let second = Arc::new(Scripted::new().respond(|r: &ChatRequest| {
        if r.messages[0]["content"].as_str().unwrap_or("").starts_with("You are an independent") {
            return "ACCEPT".into();
        }
        "```test grep -q hi hello.txt```\n```done\nhello.txt says hi; verified with grep.\n```".into()
    }));
    let mut job = Job::new("", &dir);
    job.history = vec![("user".into(), "should not be added again".into()), ("lead".into(), "x".into())];
    job.resume = Some(saved);
    let r2 = Runtime::new(second.clone(), RuntimeConfig { lead_steps: 20, ..cfg }, Events::none()).run(job);
    assert!(matches!(r2.outcome, Outcome::Done { .. }), "{:?}", r2.outcome);
    let seen = second.seen.lock().unwrap();
    let all: Vec<String> = seen[0].messages.iter().map(text).collect();
    assert_eq!(all.iter().filter(|t| t.as_str() == "what's in this folder?").count(), 1, "{all:#?}");
    assert!(all.iter().all(|t| !t.contains("should not be added again")));
    let _ = std::fs::remove_dir_all(&dir);
}
