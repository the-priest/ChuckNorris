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

#[test]
fn four_workers_build_at_the_same_time() {
    // each worker's model call takes a second and its command two: one at a time would be 4 × 4 s
    let dir = tmp("parallel");
    let model = Arc::new(Scripted::new().respond(|req| {
        if text(&req.messages[0]) == chuck_runtime::prompts::CRITIC {
            return "ACCEPT".into();
        }
        let me = worker(req);
        if let Some(t) = ["t1", "t2", "t3", "t4"].iter().find(|t| me.contains(&format!("TASK {t}"))) {
            std::thread::sleep(Duration::from_millis(1000));
            return if req.messages.len() == 2 {
                // half of them check with bash: a criterion's verify command counts whichever tool runs it
                let check = if *t == "t2" || *t == "t4" { format!("```bash\ngrep -q {t} {t}.txt\n```") }
                            else { format!("```test grep -q {t} {t}.txt```") };
                format!("```bash\nsleep 2\n```\n```write {t}.txt\n{t}\n```\n{check}\n```submit\n{t} done\n```")
            } else { "ok".into() };
        }
        let last = last_user(req);
        if req.messages.len() == 2 {
            let mut s = String::from("```criteria\n- every part exists :: verify: ls t1.txt t2.txt t3.txt t4.txt\n```\n\
                ```plan\n- t1\n- t2\n- t3\n- t4\n```\n");
            for t in 1..=4 {
                s.push_str(&format!("```assign worker\ntitle: part {t}\ncriteria:\n- t{t}.txt exists :: verify: grep -q t{t} \
                    t{t}.txt\nbrief: write t{t}.txt\n```\n"));
            }
            return s;
        }
        let mut out = String::new();
        for t in 1..=4 {
            if last.contains(&format!("SUBMISSION t{t} ")) {
                out.push_str(&format!("```review t{t} accept\nok\n```\n"));
            }
        }
        if !out.is_empty() {
            return out;
        }
        if (1..=4).all(|t| heard(req, &format!("t{t} accepted"))) {
            return "```test ls t1.txt t2.txt t3.txt t4.txt```\n```justify proportion\nfour small files\n```\n\
                ```done\nall four parts exist (ls passes).\n```".into();
        }
        "Waiting.".into()
    }));
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() },
                          Events::none());
    let t0 = std::time::Instant::now();
    let (report, _) = run_within(60, rt, Job::new("write the four parts", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    let took = t0.elapsed();
    assert!(took < Duration::from_secs(9), "the workers took turns: {took:?}");
    assert!(!model.seen.lock().unwrap().iter().any(|r| !worker(r).is_empty() && heard(r, "submission REFUSED")),
            "a check run with bash was refused");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_workers_copy_has_the_projects_dependencies_and_history_read_only() {
    if std::process::Command::new("node").arg("--version").output().is_err()
        || std::process::Command::new("git").arg("--version").output().is_err() {
        return;
    }
    let dir = tmp("deps");
    let proj = dir.join("proj");
    std::fs::create_dir_all(proj.join("node_modules/leftpad")).unwrap();
    std::fs::write(proj.join("node_modules/leftpad/index.js"), "module.exports = s => '  ' + s;\n").unwrap();
    std::fs::write(proj.join("check.js"), "const pad = require('leftpad');\nif (pad('x') !== '  x') process.exit(1);\n\
        console.log('pad ok');\n").unwrap();
    let git = |args: &[&str]| assert!(std::process::Command::new("git").args(args).current_dir(&proj).output().unwrap()
        .status.success());
    git(&["init", "-q"]);
    git(&["-c", "user.email=t@t", "-c", "user.name=t", "add", "check.js"]);
    git(&["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "first commit"]);
    let model = Arc::new(Scripted::new().respond(|req| {
        if text(&req.messages[0]) == chuck_runtime::prompts::CRITIC {
            return "ACCEPT".into();
        }
        if !worker(req).is_empty() {
            return if req.messages.len() == 2 {
                "```bash\ngit log --oneline | head -1\ntouch node_modules/leftpad/changed && echo WROTE-DEPS || echo DEPS-READ-ONLY\n\
                 node check.js\n```".into()
            } else if req.messages.len() == 4 {
                "```write notes.txt\nuses leftpad\n```\n```test node check.js```\n```submit\nnotes written; node check.js passes\n```".into()
            } else { "ok".into() };
        }
        let last = last_user(req);
        if req.messages.len() == 2 {
            return "```criteria\n- check.js passes :: verify: node check.js\n```\n```plan\n- t1: notes\n```\n\
                ```assign worker\ntitle: notes\ncriteria:\n- check.js passes :: verify: node check.js\nbrief: write notes.txt\n```".into();
        }
        if last.contains("SUBMISSION t1") {
            return "```review t1 accept\nok\n```".into();
        }
        if heard(req, "t1 accepted") {
            return "```test node check.js```\n```justify proportion\na note\n```\n```done\nnotes.txt added; node check.js \
                passes.\n```".into();
        }
        "Waiting.".into()
    }));
    let (tx, rx) = channel();
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() },
                          Events(Some(tx)));
    let (report, _) = run_within(60, rt, Job::new("add notes", &proj));
    let seen = model.seen.lock().unwrap();
    let fb: Vec<String> = seen.iter().filter(|r| !worker(r).is_empty() && r.messages.len() > 2).map(last_user).collect();
    let fb = fb.join("\n");
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}\n{fb}", report.outcome);
    assert!(fb.contains("first commit"), "git history is there: {fb}");
    assert!(fb.contains("pad ok"), "the dependency is there: {fb}");
    if chuck_sandbox::bwrap().is_some() {
        assert!(fb.contains("DEPS-READ-ONLY") && !proj.join("node_modules/leftpad/changed").exists(), "{fb}");
    }
    // home: the work, and nothing of the links
    assert!(proj.join("notes.txt").exists());
    assert!(!proj.join("node_modules").is_symlink() && proj.join("node_modules/leftpad/index.js").is_file());
    let merged = rx.try_iter().find_map(|e| match e { Event::Merged { refused, applied, .. } => Some((refused, applied)), _ => None });
    let (refused, applied) = merged.unwrap();
    assert!(refused.is_empty() && applied == 1, "{refused:?} {applied}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_whole_job_done_through_function_calls_alone() {
    // as V4-Flash answers when it is offered tools: no fenced block anywhere, only calls the API parsed
    let dir = tmp("native");
    let call = |name: &str, args: serde_json::Value| format!("@call {name} {args}");
    let model = Arc::new(Scripted::new().respond(move |req| {
        if text(&req.messages[0]) == chuck_runtime::prompts::CRITIC {
            assert!(req.tools.is_none(), "the critic has no tools");
            return "ACCEPT".into();
        }
        let names: Vec<String> = req.tools.as_ref().expect("an acting agent is offered its tools").as_array().unwrap()
            .iter().map(|t| t["function"]["name"].as_str().unwrap().to_string()).collect();
        let me = worker(req);
        if !me.is_empty() {
            assert!(names.contains(&"submit".into()) && !names.contains(&"assign".into()), "{names:?}");
            return if req.messages.len() == 2 {
                [call("team", serde_json::json!({"message": "game.js exports start()"})),
                 call("write", serde_json::json!({"path": "game.js", "content": "export function start() {}\n"})),
                 call("test", serde_json::json!({"command": "grep -q start game.js"})),
                 call("submit", serde_json::json!({"report": "game.js exports start(); grep passes"}))].join("\n")
            } else { "ok".into() };
        }
        assert!(names.contains(&"assign".into()) && names.contains(&"review".into()), "{names:?}");
        let last = last_user(req);
        if req.messages.len() == 2 {
            return ["Splitting it.".to_string(),
                call("criteria", serde_json::json!({"body": "- game.js exports start :: verify: grep -q start game.js"})),
                call("plan", serde_json::json!({"body": "- t1: the game module\n- verify"})),
                call("assign", serde_json::json!({"arg": "worker", "body": "title: game module\ncriteria:\n- exports start \
                    :: verify: grep -q start game.js\nbrief: write game.js"}))].join("\n");
        }
        if last.contains("SUBMISSION t1") {
            return call("review", serde_json::json!({"arg": "t1 accept", "body": "verified"}));
        }
        if heard(req, "t1 accepted") {
            return [call("test", serde_json::json!({"command": "grep -q start game.js"})),
                    call("justify", serde_json::json!({"arg": "proportion", "body": "one small module was asked for"})),
                    call("done", serde_json::json!({"summary": "game.js exports start(); grep -q start game.js passes."}))]
                .join("\n");
        }
        "Waiting.".into()
    }));
    let (tx, rx) = channel();
    let rt = Runtime::new(model, RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() }, Events(Some(tx)));
    let (report, _) = run_within(30, rt, Job::new("make the game module", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    assert_eq!(std::fs::read_to_string(dir.join("game.js")).unwrap(), "export function start() {}\n");
    let events: Vec<Event> = rx.try_iter().collect();
    assert!(events.iter().any(|e| matches!(e, Event::Team { from, text, .. } if from == "ds-1" && text.contains("start()"))));
    let plan = events.iter().rev().find_map(|e| match e { Event::Plan { steps } => Some(steps.clone()), _ => None }).unwrap();
    assert!(plan.iter().all(|(_, d)| *d));
    let _ = std::fs::remove_dir_all(&dir);
}
