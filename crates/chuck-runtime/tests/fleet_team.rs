//! The fleet as a team: workers tell each other what they provide and ask
//! the Lead what only it knows; the Lead waits while they build (no turn
//! spent polling), can't change the project under them, answers, takes a
//! task back with `cancel`, and never waits forever on a worker that died.

use chuck_core::WORKER_MODEL;
use chuck_model::{ChatRequest, Scripted};
use chuck_runtime::{Event, Events, Job, Outcome, Runtime, RuntimeConfig};
use std::path::PathBuf;
use std::sync::mpsc::channel;
use std::sync::Arc;
use std::time::Duration;

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("chuck-team-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn text(m: &serde_json::Value) -> String {
    m["content"].as_str().unwrap_or("").to_string()
}

fn last_user(req: &ChatRequest) -> String {
    req.messages.iter().rev().find(|m| m["role"] == "user").map(text).unwrap_or_default()
}

fn heard(req: &ChatRequest, needle: &str) -> bool {
    req.messages.iter().any(|m| m["role"] == "user" && text(m).contains(needle))
}

fn is_lead(req: &ChatRequest) -> bool {
    text(&req.messages[0]).contains("You are the LEAD")
}

fn worker(req: &ChatRequest) -> String {
    req.messages.get(1).map(text).filter(|t| t.starts_with("You are ds-")).unwrap_or_default()
}

fn run_within(secs: u64, rt: Runtime, job: Job) -> (chuck_runtime::JobReport, Runtime) {
    let (tx, rx) = channel();
    std::thread::spawn(move || {
        let r = rt.run(job);
        let _ = tx.send((r, rt));
    });
    rx.recv_timeout(Duration::from_secs(secs)).expect("the job hung")
}

#[test]
fn workers_talk_and_the_lead_waits_answers_and_merges() {
    let dir = tmp("talk");
    std::fs::write(dir.join("README.md"), "a game\n").unwrap();
    let model = Arc::new(Scripted::new().respond(|req| {
        if text(&req.messages[0]) == chuck_runtime::prompts::CRITIC {
            return "ACCEPT".into();
        }
        let me = worker(req);
        if me.contains("TASK t1") {
            assert_eq!(req.model, WORKER_MODEL);
            return if req.messages.len() == 2 {
                // it says what it provides, then builds and hands in
                "```team engine.js exports create(seed) -> game; the server should import it```\n\
                 ```write engine.js\nexport function create(seed) { return { seed }; }\n```\n\
                 ```test grep -q create engine.js```\n```submit\nengine.js: create(seed), checked with grep\n```".into()
            } else { "ok".into() };
        }
        if me.contains("TASK t2") {
            if req.messages.len() == 2 {
                return "```team @lead which port should the server listen on?```\n```read .```".into();
            }
            if heard(req, "lead → ds-2: use 8080") {
                assert!(req.messages.iter().any(|m| text(m).contains("engine.js exports create(seed)")),
                        "what ds-1 said reached ds-2");
                return "```write server.js\nimport { create } from './engine.js';\nlisten(8080);\n```\n\
                    ```test grep -q 8080 server.js```\n```submit\nserver.js on 8080, using create()\n```".into();
            }
            // the answer isn't here yet: it reads on, as a real worker would
            std::thread::sleep(Duration::from_millis(150));
            return "```read README.md```".into();
        }
        if is_lead(req) {
            let last = last_user(req);
            if req.messages.len() == 2 {
                return "```criteria\n- engine.js provides create :: verify: grep -q create engine.js\n\
                    - the server listens on 8080 :: verify: grep -q 8080 server.js\n```\n\
                    ```plan\n- t1: the engine\n- t2: the server\n- verify the whole game\n```\n\
                    ```assign worker\ntitle: The engine\ncriteria:\n- engine.js provides create :: verify: grep -q create \
                    engine.js\nbrief:\nOwns engine.js. Provides create(seed).\n```\n\
                    ```assign worker\ntitle: The server\ncriteria:\n- listens on 8080 :: verify: grep -q 8080 server.js\n\
                    brief:\nOwns server.js. Uses create() from engine.js.\n```".into();
            }
            let mut out = String::new();
            if last.contains("ds-2 → lead: which port") {
                // a change of its own while the workers build is refused; the answer goes out
                out.push_str("```write hack.txt\nmine\n```\n```team @ds-2 use 8080```\n");
            }
            for t in ["t1", "t2"] {
                if last.contains(&format!("SUBMISSION {t}")) {
                    out.push_str(&format!("```review {t} accept\nverified\n```\n"));
                }
            }
            if !out.is_empty() {
                return out;
            }
            if heard(req, "t1 accepted") && heard(req, "t2 accepted") {
                return "```test grep -q create engine.js```\n```test grep -q 8080 server.js```\n\
                    ```justify proportion\ntwo small files are the whole ask\n```\n\
                    ```done\nengine.js and server.js are merged; both greps pass.\n```".into();
            }
            return "Waiting.".into();
        }
        panic!("who is this?")
    }));
    let (tx, rx) = channel();
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() },
                          Events(Some(tx)));
    let (report, _) = run_within(60, rt, Job::new("build the engine and the server", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    let events: Vec<Event> = rx.try_iter().collect();

    // both merged; the Lead's own write under the workers was refused and never landed
    assert!(dir.join("engine.js").exists() && dir.join("server.js").exists());
    assert!(!dir.join("hack.txt").exists());
    let seen = model.seen.lock().unwrap();
    let lead: Vec<&ChatRequest> = seen.iter().filter(|r| is_lead(r)).collect();
    assert!(lead.iter().any(|r| last_user(r).contains("the workers are changing the project now")));
    // it woke for the question, the submissions and the end: never just to say "Waiting."
    assert!(lead.len() <= 6, "the Lead polled: {} turns", lead.len());
    assert!(!lead.iter().any(|r| r.messages.iter().any(|m| m["role"] == "assistant" && text(m) == "Waiting.")),
            "a turn spent waiting");
    // the talk is on the record, the lifecycle notes too
    let talk: Vec<(String, Option<String>, String)> = events.iter().filter_map(|e| match e {
        Event::Team { from, to, text } => Some((from.clone(), to.clone(), text.clone())),
        _ => None,
    }).collect();
    assert!(talk.iter().any(|(f, to, t)| f == "ds-1" && to.is_none() && t.contains("exports create")));
    assert!(talk.iter().any(|(f, to, _)| f == "ds-2" && to.as_deref() == Some("lead")));
    assert!(talk.iter().any(|(f, to, t)| f == "lead" && to.as_deref() == Some("ds-2") && t == "use 8080"));
    assert!(talk.iter().any(|(f, _, t)| f == "lead" && t.starts_with("t2 ") && t.contains("merged")));
    // the plan ticked off with the merges
    let plan = events.iter().rev().find_map(|e| match e { Event::Plan { steps } => Some(steps.clone()), _ => None }).unwrap();
    assert!(plan.iter().all(|(_, d)| *d), "{plan:?}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn the_lead_takes_a_task_back_and_finishes_it_itself() {
    let dir = tmp("cancel");
    std::fs::write(dir.join("v.txt"), "1\n").unwrap();
    let model = Arc::new(Scripted::new().respond(|req| {
        if text(&req.messages[0]) == chuck_runtime::prompts::CRITIC {
            return "ACCEPT".into();
        }
        if !worker(req).is_empty() {
            if req.messages.len() == 2 {
                return "```team @lead v.txt is generated by a script I can't find — is the task right?```\n\
                    ```read v.txt```".into();
            }
            std::thread::sleep(Duration::from_millis(100));
            return "```read v.txt```".into(); // stuck: only the Lead can unblock it
        }
        let last = last_user(req);
        if req.messages.len() == 2 {
            return "```criteria\n- v.txt holds 2 :: verify: grep -qx 2 v.txt\n```\n```plan\n- t1: set v\n```\n\
                ```assign worker\ntitle: Set v\ncriteria:\n- v.txt holds 2 :: verify: grep -qx 2 v.txt\nbrief: set it\n```".into();
        }
        if last.contains("is the task right?") {
            return "```cancel t1 the task was wrong: it's a plain file, I'll do it```".into();
        }
        if last.contains("t1 cancelled") {
            return "```write v.txt\n2\n```\n```test grep -qx 2 v.txt```\n```justify proportion\none value\n```\n\
                ```done\nv.txt holds 2 (grep -qx 2 v.txt passes).\n```".into();
        }
        "Hmm.".into()
    }));
    let (tx, rx) = channel();
    let rt = Runtime::new(model, RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() }, Events(Some(tx)));
    let (report, rt) = run_within(30, rt, Job::new("set v to 2", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    assert_eq!(std::fs::read_to_string(dir.join("v.txt")).unwrap(), "2\n");
    let events: Vec<Event> = rx.try_iter().collect();
    let last_tasks = events.iter().rev().find_map(|e| match e { Event::Tasks(t) => Some(t.clone()), _ => None }).unwrap();
    assert_eq!(last_tasks[0].state, "failed");
    assert!(last_tasks[0].note.contains("cancelled: the task was wrong"), "{:?}", last_tasks[0].note);
    assert!(events.iter().any(|e| matches!(e, Event::AgentEnded { status, .. } if status == "stopped")));
    assert!(rt.kernel.ps(true).iter().filter(|p| p.role.is_worker()).all(|p| !p.state.alive()));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_worker_whose_call_crashes_is_reported_not_retried() {
    let dir = tmp("crash");
    std::fs::write(dir.join("v.txt"), "1\n").unwrap();
    let model = Arc::new(Scripted::new().respond(|req| {
        if text(&req.messages[0]) == chuck_runtime::prompts::CRITIC {
            return "ACCEPT".into();
        }
        if !worker(req).is_empty() {
            panic!("a bug in the call");
        }
        let last = last_user(req);
        if req.messages.len() == 2 {
            return "```criteria\n- v.txt holds 2 :: verify: grep -qx 2 v.txt\n```\n```plan\n- t1: set v\n```\n\
                ```assign worker\ntitle: Set v\ncriteria:\n- v.txt holds 2 :: verify: grep -qx 2 v.txt\nbrief: set it\n```".into();
        }
        if last.contains("crashed") {
            return "```write v.txt\n2\n```\n```test grep -qx 2 v.txt```\n```justify proportion\none value\n```\n\
                ```done\nv.txt holds 2 (grep -qx 2 v.txt passes).\n```".into();
        }
        "Hmm.".into()
    }));
    let rt = Runtime::new(model, RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() }, Events::none());
    let (report, rt) = run_within(30, rt, Job::new("set v to 2", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    // its process ended with it: nothing is left holding a slot
    assert!(rt.kernel.ps(true).iter().filter(|p| p.role.is_worker()).all(|p| !p.state.alive()));
    let _ = std::fs::remove_dir_all(&dir);
}
