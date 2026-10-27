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
            // it builds once it has both the Lead's answer and what ds-1 provides (they arrive in either order)
            if heard(req, "lead → ds-2: use 8080") && req.messages.iter().any(|m| text(m).contains("engine.js exports create(seed)")) {
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

    // both merged; a file no worker touches was the Lead's to write while they built
    assert!(dir.join("engine.js").exists() && dir.join("server.js").exists());
    assert_eq!(std::fs::read_to_string(dir.join("hack.txt")).unwrap(), "mine\n");
    let seen = model.seen.lock().unwrap();
    let lead: Vec<&ChatRequest> = seen.iter().filter(|r| is_lead(r)).collect();
    // it woke for the question, the submissions and the end, and heard back at once on its own write
    // (one turn that ends its own work): never polling
    assert!(lead.len() <= 7, "the Lead polled: {} turns", lead.len());
    let waits = lead.last().map_or(0, |r| r.messages.iter().filter(|m| m["role"] == "assistant" && text(m) == "Waiting.").count());
    assert!(waits <= 1, "{waits} turns spent waiting");
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

#[test]
fn a_file_too_big_for_one_reply_is_written_whole() {
    // the length limit cuts a write call mid-file: what arrived is kept, and the model continues it as text
    let dir = tmp("bigfile");
    let model = Arc::new(Scripted::new().respond(|req| {
        if text(&req.messages[0]) == chuck_runtime::prompts::CRITIC {
            return "ACCEPT".into();
        }
        let last = last_user(req);
        if last.contains("cut off by the length limit") {
            assert!(req.tools.is_none(), "a continuation is text");
            let so_far = req.messages.iter().rev().find(|m| m["role"] == "assistant").map(text).unwrap_or_default();
            assert!(so_far.ends_with("```write big.js\nline 1\nline 2 \"q\"\n"), "{so_far}");
            return "line 3\n```\n```test grep -q 'line 3' big.js```\n```done\nbig.js has all 3 lines (grep passes).\n```".into();
        }
        if req.messages.len() == 2 {
            return "```criteria\n- big.js is whole :: verify: grep -q 'line 3' big.js\n```\n\
                    @call write {\"path\": \"big.js\", \"content\": \"line 1\\nline 2 \\\"q\\\"\\n@cut".into();
        }
        "```done\nbig.js has all 3 lines (grep passes).\n```".into()
    }));
    let rt = Runtime::new(model, RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() }, Events::none());
    let (report, _) = run_within(30, rt, Job::new("write big.js", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    assert_eq!(std::fs::read_to_string(dir.join("big.js")).unwrap(), "line 1\nline 2 \"q\"\nline 3\n");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn the_lead_is_the_boss_its_change_stands_and_the_worker_is_told() {
    let dir = tmp("claims");
    std::fs::write(dir.join("a.txt"), "a0\n").unwrap();
    std::fs::write(dir.join("b.txt"), "b0\n").unwrap();
    let model = Arc::new(Scripted::new().respond(|req| {
        if text(&req.messages[0]) == chuck_runtime::prompts::CRITIC {
            return "ACCEPT".into();
        }
        if !worker(req).is_empty() {
            if req.messages.len() == 2 {
                return "```write a.txt\na1\n```\n```team @lead a.txt is written; checking it next```".into();
            }
            if heard(req, "the Lead changed a.txt") && !heard(req, "submission") {
                return "```test grep -qx a1 a.txt```\n```submit\na.txt says a1\n```".into();
            }
            std::thread::sleep(Duration::from_millis(100));
            return "```read a.txt```".into();
        }
        let last = last_user(req);
        if req.messages.len() == 2 {
            return "```criteria\n- b says b1 :: verify: grep -qx b1 b.txt\n```\n\
                ```plan\n- t1: a\n- b\n```\n```assign worker\ntitle: a\ncriteria:\n- a says a1 :: verify: grep -qx a1 \
                a.txt\nbrief: a.txt only\n```".into();
        }
        if last.contains("ds-1 → lead: a.txt is written") {
            // a.txt is ds-1's file too: the Lead changes it anyway, and b.txt
            return "```write a.txt\nmine\n```\n```write b.txt\nb1\n```".into();
        }
        if last.contains("SUBMISSION t1") {
            return "```review t1 accept\nok\n```".into();
        }
        if heard(req, "t1 accepted") {
            return "```test grep -qx b1 b.txt```\n```justify proportion\none line\n```\n\
                ```done\nb says b1 (grep passes); a.txt kept the Lead's version.\n```".into();
        }
        "Waiting.".into()
    }));
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() },
                          Events::none());
    let (report, _) = run_within(30, rt, Job::new("set a and b", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    assert_eq!(std::fs::read_to_string(dir.join("a.txt")).unwrap(), "mine\n", "the boss's version stands");
    assert_eq!(std::fs::read_to_string(dir.join("b.txt")).unwrap(), "b1\n");
    assert!(dir.join(".chuck-conflicts").exists(), "the worker's overlapping version is set aside, not lost");
    let seen = model.seen.lock().unwrap();
    assert!(seen.iter().filter(|r| is_lead(r)).any(|r| last_user(r).contains("ds-1 is changing a.txt too (t1)")));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_stuck_worker_is_warned_then_replaced_and_a_step_that_stalls_twice_comes_back() {
    // ds-1 only ever reads: warned, then replaced; t1's second worker gets it done. t2's both workers loop:
    // the step comes back to the Lead, who does it itself
    let dir = tmp("supervisor");
    let model = Arc::new(Scripted::new().respond(|req| {
        if text(&req.messages[0]) == chuck_runtime::prompts::CRITIC {
            return "ACCEPT".into();
        }
        let me = worker(req);
        if !me.is_empty() {
            if me.contains("TASK t1") && me.contains("was stopped for being stuck") {
                return if req.messages.len() == 2 {
                    "```write one.txt\n1\n```\n```test grep -qx 1 one.txt```\n```submit\none.txt says 1\n```".into()
                } else { "ok".into() };
            }
            return "```read .```".into(); // going nowhere
        }
        let last = last_user(req);
        if req.messages.len() == 2 {
            return "```criteria\n- one says 1 :: verify: grep -qx 1 one.txt\n- two says 2 :: verify: grep -qx 2 two.txt\n```\n\
                ```plan\n- t1: one\n- t2: two\n```\n\
                ```assign worker\ntitle: one\ncriteria:\n- one says 1 :: verify: grep -qx 1 one.txt\nbrief: write one.txt\n```\n\
                ```assign worker\ntitle: two\ncriteria:\n- two says 2 :: verify: grep -qx 2 two.txt\nbrief: write two.txt\n```".into();
        }
        let mut out = String::new();
        if last.contains("SUBMISSION t1") {
            out.push_str("```review t1 accept\nok\n```\n");
        }
        if last.contains("t2 stalled twice") {
            out.push_str("```write two.txt\n2\n```\n```test grep -qx 2 two.txt```\n");
        }
        if !out.is_empty() {
            return out;
        }
        if heard(req, "t1 accepted") && heard(req, "t2 stalled twice") {
            return "```test grep -qx 1 one.txt```\n```test grep -qx 2 two.txt```\n```justify proportion\ntwo lines\n```\n\
                ```done\none says 1 and two says 2 (both greps pass).\n```".into();
        }
        "Waiting.".into()
    }));
    let (tx, rx) = channel();
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() },
                          Events(Some(tx)));
    let (report, _) = run_within(60, rt, Job::new("write one and two", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    assert_eq!(std::fs::read_to_string(dir.join("one.txt")).unwrap(), "1\n");
    assert_eq!(std::fs::read_to_string(dir.join("two.txt")).unwrap(), "2\n");
    let seen = model.seen.lock().unwrap();
    assert!(seen.iter().any(|r| !worker(r).is_empty() && last_user(r).contains("[supervisor: 8 turns")), "warned first");
    // before that, the Lead pushed it, on the team channel, once per idle streak
    assert!(seen.iter().any(|r| !worker(r).is_empty() && last_user(r).contains("lead → ds-1: @ds-1 hurry up on t1")),
            "pushed at 4 turns");
    let events: Vec<Event> = rx.try_iter().collect();
    let pushes = events.iter().filter(|e| matches!(e, Event::Team { from, to: Some(to), text }
        if from == "lead" && to == "ds-1" && text.contains("hurry up"))).count();
    assert_eq!(pushes, 1, "one push per idle streak");
    let lead: Vec<String> = seen.iter().filter(|r| is_lead(r)).map(last_user).collect();
    assert!(lead.iter().any(|l| l.contains("the supervisor replaced it with")), "{lead:?}");
    // stopped workers never got past the limit: none read more than 12 times in a row
    assert!(events.iter().filter(|e| matches!(e, Event::Notice { text, .. } if text.starts_with("stopped by the supervisor"))).count() >= 3);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn what_gets_in_an_agents_way_is_written_down() {
    let dir = tmp("snags");
    std::fs::write(dir.join("a.txt"), "old\n").unwrap();
    let log = dir.join("snags.jsonl");
    chuck_store::snags::set_path(Some(log.clone()));
    let model = Arc::new(Scripted::new().respond(|req| {
        if text(&req.messages[0]) == chuck_runtime::prompts::CRITIC {
            return "ACCEPT".into();
        }
        assert!(text(&req.messages[0]).contains("DEBUG MODE"), "debug mode asks every agent for snags");
        if req.messages.len() == 2 {
            // an edit that misses, a tool call written wrong, and the agent's own word
            return "```criteria\n- a says new :: verify: grep -qx new a.txt\n```\n\
                    ```edit a.txt\n<<<<<<< SEARCH\nolder\n=======\nnew\n>>>>>>> REPLACE\n```\n\
                    ```snag the edit tool's miss message doesn't show the file's lines\n```".into();
        }
        "```write a.txt\nnew\n```\n```test grep -qx new a.txt```\n```done\na says new (grep passes).\n```".into()
    }));
    let (tx, rx) = channel();
    let cfg = RuntimeConfig { store: dir.join("store"), debug: true, ..RuntimeConfig::default() };
    let rt = Runtime::new(model, cfg, Events(Some(tx)));
    let (report, _) = run_within(30, rt, Job::new("make a say new", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    let kinds: Vec<String> = rx.try_iter().filter_map(|e| match e { Event::Snag { kind, .. } => Some(kind), _ => None }).collect();
    assert!(kinds.contains(&"edit missed".to_string()) && kinds.contains(&"said".to_string()), "{kinds:?}");
    let written = chuck_store::snags::read(&log, 100);
    assert!(written.iter().any(|s| s.kind == "said" && s.who == "lead" && s.text.contains("miss message")), "{written:?}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_reply_stuck_repeating_itself_is_cut_and_the_job_goes_on() {
    let dir = tmp("runaway");
    let model = Arc::new(Scripted::new().respond(|req| {
        if text(&req.messages[0]) == chuck_runtime::prompts::CRITIC {
            return "ACCEPT".into();
        }
        if req.messages.len() == 2 {
            // what a model in a loop writes: the same call, over and over
            return format!("```criteria\n- a says 1 :: verify: grep -qx 1 a.txt\n```\n{}",
                           "```read index.html:70300000```\n".repeat(600));
        }
        let last = last_user(req);
        assert!(req.messages.iter().any(|m| text(m).contains("[cut: this reply began repeating")), "it is told");
        assert!(!req.messages.iter().any(|m| text(m).matches("70300000").count() > 1), "the loop isn't kept: {last}");
        "```write a.txt\n1\n```\n```test grep -qx 1 a.txt```\n```done\na says 1 (grep passes).\n```".into()
    }));
    let (tx, rx) = channel();
    let rt = Runtime::new(model, RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() }, Events(Some(tx)));
    let (report, _) = run_within(30, rt, Job::new("make a say 1", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    assert!(rx.try_iter().any(|e| matches!(e, Event::Snag { kind, .. } if kind == "runaway reply")));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn what_the_lead_reads_while_workers_build_comes_back_at_once() {
    let dir = tmp("look");
    std::fs::write(dir.join("notes.txt"), "the port is 9090\n").unwrap();
    let saw_early = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let early = saw_early.clone();
    let model = Arc::new(Scripted::new().respond(move |req| {
        if text(&req.messages[0]) == chuck_runtime::prompts::CRITIC {
            return "ACCEPT".into();
        }
        if !worker(req).is_empty() {
            // a slow worker: the Lead's read must not wait for it
            std::thread::sleep(Duration::from_millis(1500));
            return "```write port.txt\n9090\n```\n```test grep -q 9090 port.txt```\n```submit\nport.txt holds 9090\n```".into();
        }
        let last = last_user(req);
        if req.messages.len() == 2 {
            return "```criteria\n- port.txt holds 9090 :: verify: grep -q 9090 port.txt\n```\n```plan\n- t1: write it\n```\n\
                ```assign worker\ntitle: Port file\ncriteria:\n- port.txt holds 9090 :: verify: grep -q 9090 port.txt\n\
                brief: write port.txt\n```\n```read notes.txt```".into();
        }
        if last.contains("the port is 9090") && !last.contains("SUBMISSION") {
            early.store(true, std::sync::atomic::Ordering::SeqCst);
        }
        if last.contains("SUBMISSION t1") {
            return "```review t1 accept\nok\n```".into();
        }
        if heard(req, "t1 accepted") {
            return "```test grep -q 9090 port.txt```\n```justify proportion\none value\n```\n\
                ```done\nport.txt holds 9090.\n```".into();
        }
        "Waiting.".into()
    }));
    let rt = Runtime::new(model, RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() }, Events(None));
    let (report, _) = run_within(30, rt, Job::new("write the port", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    assert!(saw_early.load(std::sync::atomic::Ordering::SeqCst), "the Lead's read waited for the worker's hand-in");
    let _ = std::fs::remove_dir_all(&dir);
}
