//! One model doing the whole job, against a model that behaves the way real
//! ones do: it calls its tools as functions or writes them as blocks, runs
//! past the length limit, loops, asks the user, writes files before its
//! criteria. Every job here ends through the Done gate.

use chuck_model::{ChatRequest, Scripted};
use chuck_runtime::{Event, Events, Job, Outcome, Runtime, RuntimeConfig};
use std::path::PathBuf;
use std::sync::mpsc::channel;
use std::sync::Arc;
use std::time::Duration;

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("chuck-solo-{}-{name}", std::process::id()));
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

fn run_within(secs: u64, rt: Runtime, job: Job) -> (chuck_runtime::JobReport, Runtime) {
    let (tx, rx) = channel();
    std::thread::spawn(move || {
        let r = rt.run(job);
        let _ = tx.send((r, rt));
    });
    rx.recv_timeout(Duration::from_secs(secs)).expect("the job hung")
}

fn solo_rt(model: Arc<Scripted>, dir: &std::path::Path) -> Runtime {
    Runtime::new(model, RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() }, Events(None))
}

#[test]
fn a_whole_job_done_through_function_calls_alone() {
    // as V4-Flash answers when it is offered tools: no fenced block anywhere, only calls the API parsed
    let dir = tmp("native");
    let call = |name: &str, args: serde_json::Value| format!("@call {name} {args}");
    let model = Arc::new(Scripted::new().respond(move |req| {
        let names: Vec<String> = req.tools.as_ref().expect("Chuck is offered its tools").as_array().unwrap()
            .iter().map(|t| t["function"]["name"].as_str().unwrap().to_string()).collect();
        for gone in ["assign", "review", "team", "submit", "plan", "cancel", "justify"] {
            assert!(!names.contains(&gone.to_string()), "no fleet tools: {names:?}");
        }
        assert!(names.contains(&"done".into()) && names.contains(&"ask".into()), "{names:?}");
        if req.messages.len() == 2 {
            return ["Writing it.".to_string(),
                call("criteria", serde_json::json!({"body": "- game.js exports start :: verify: grep -q start game.js"})),
                call("write", serde_json::json!({"path": "game.js", "content": "export function start() {}\n"})),
                call("test", serde_json::json!({"command": "grep -q start game.js"})),
                call("done", serde_json::json!({"summary": "game.js exports start(); grep -q start game.js passes."}))]
                .join("\n");
        }
        "Hmm.".into()
    }));
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() },
                          Events::none());
    let (report, _) = run_within(30, rt, Job::new("make the game module", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    assert_eq!(std::fs::read_to_string(dir.join("game.js")).unwrap(), "export function start() {}\n");
    assert_eq!(model.calls(), 1, "one call, start to finish");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn finished_work_said_in_prose_is_done_not_lost() {
    // it builds and checks, then just says it's done (no ```done```): that is its done, the gate still judges it
    let dir = tmp("prose-done");
    let model = Arc::new(Scripted::new().respond(|req| {
        if req.messages.len() == 2 {
            return "```criteria\n- a holds A :: verify: grep -qx A a.txt\n```\n```write a.txt\nA\n```\n\
                ```test grep -qx A a.txt```".into();
        }
        "Everything is in place: a.txt holds A, and grep -qx A a.txt passes in the folder.".into()
    }));
    let (report, _) = run_within(30, solo_rt(model.clone(), &dir), Job::new("write a", &dir));
    assert!(matches!(&report.outcome, Outcome::Done { summary } if summary.contains("a.txt holds A")), "{:?}", report.outcome);
    assert_eq!(model.calls(), 2);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn every_turn_may_write_a_whole_file() {
    // a turn after one that only looked must not be capped small: a file written then would be cut off and
    // written again (the slowness seen in a real run)
    let dir = tmp("budget");
    std::fs::write(dir.join("notes.txt"), "the port is 9090\n").unwrap();
    let model = Arc::new(Scripted::new().respond(|req| {
        match req.messages.iter().filter(|m| m["role"] == "assistant").count() {
            0 => "```read notes.txt```\n```criteria\n- a.txt holds A :: verify: grep -qx A a.txt\n```".into(),
            1 => "```write a.txt\nA\n```\n```test grep -qx A a.txt```\n```done\na.txt holds A.\n```".into(),
            _ => "Hmm.".into(),
        }
    }));
    let cfg = RuntimeConfig { store: dir.join("store"), max_tokens: 32768, ..RuntimeConfig::default() };
    let rt = Runtime::new(model.clone(), cfg, Events(None));
    let (report, _) = run_within(20, rt, Job::new("write a", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    let budgets: Vec<u32> = model.seen.lock().unwrap().iter().map(|r| r.max_tokens).collect();
    assert!(budgets.iter().all(|b| *b == 32768), "{budgets:?}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_file_too_big_for_one_reply_is_written_whole() {
    // the length limit cuts a write call mid-file: what arrived is kept, and the model continues it as text
    let dir = tmp("bigfile");
    let model = Arc::new(Scripted::new().respond(|req| {
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
fn what_gets_in_an_agents_way_is_written_down() {
    let dir = tmp("snags");
    std::fs::write(dir.join("a.txt"), "old\n").unwrap();
    let log = dir.join("snags.jsonl");
    chuck_store::snags::set_path(Some(log.clone()));
    let model = Arc::new(Scripted::new().respond(|req| {
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
fn alone_one_model_builds_it_in_one_reply() {
    // no workers: criteria, the whole file, its checks and done, all in one reply — no plan required, no review
    // call, nothing else
    let dir = tmp("solo");
    let model = Arc::new(Scripted::new().respond(|req| {
        assert!(!text(&req.messages[0]).contains("independent reviewer"), "no review call alone");
        assert!(text(&req.messages[0]).starts_with("You are Chuck"), "the solo prompt");
        "```criteria\n- tetris.html exists :: verify: test -f tetris.html\n- it has a canvas :: verify: grep -q canvas tetris.html\n\
            - it has the pieces :: verify: grep -q TETROMINOES tetris.html\n```\n\
            ```write tetris.html\n<canvas id=c></canvas><script>const TETROMINOES = [];</script>\n```\n\
            ```test test -f tetris.html```\n```test grep -q canvas tetris.html```\n```test grep -q TETROMINOES tetris.html```\n\
            ```done\ntetris.html: a canvas and the seven pieces; all three checks pass.\n```".into()
    }));
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() },
                          Events(None));
    let (report, _) = run_within(20, rt, Job::new("make me a tetris in tetris.html", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    assert!(dir.join("tetris.html").exists());
    assert_eq!(model.seen.lock().unwrap().len(), 1, "one call, start to finish");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn alone_it_thinks_to_plan_and_to_debug_not_to_carry_on() {
    let dir = tmp("solo-think");
    let model = Arc::new(Scripted::new().respond(|req| {
        match req.messages.iter().filter(|m| m["role"] == "assistant").count() {
            0 => "```criteria\n- a holds A :: verify: grep -qx A a.txt\n```\n```write a.txt\nB\n```\n```test grep -qx A a.txt```".into(),
            1 => "```write a.txt\nA\n```".into(), // after a failure
            2 => "```read a.txt```".into(),         // after a fine turn
            _ => "```test grep -qx A a.txt```\n```done\na.txt holds A.\n```".into(),
        }
    }));
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() },
                          Events(None));
    let (report, _) = run_within(20, rt, Job::new("a holds A", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    let budgets: Vec<u32> = model.seen.lock().unwrap().iter().map(|r| r.reasoning.budget).collect();
    assert!(budgets[0] > 0, "it plans with thinking: {budgets:?}");
    assert!(budgets[1] > 0, "it debugs a failed check with thinking: {budgets:?}");
    assert_eq!(budgets[2], 0, "a fine turn is carried on without: {budgets:?}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_file_is_shown_as_it_is_written_by_call_or_by_block() {
    let dir = tmp("live-write");
    let big: String = (0..120).map(|i| format!("const row{i} = {i};\\n")).collect();
    // (a reply's function calls run after its text: the file by call first, the rest next)
    let first = format!("```criteria\n- both exist :: verify: test -f a.js && test -f b.js\n```\n\
        @call write {{\"path\": \"a.js\", \"content\": \"{big}\"}}");
    let second = "```write b.js\nlet b = 1;\nlet c = 2;\n```\n```test test -f a.js && test -f b.js```\n```done\na.js and b.js.\n```";
    let model = Arc::new(Scripted::new().replies([first, second.to_string()]));
    let (tx, rx) = channel();
    let rt = Runtime::new(model, RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() }, Events(Some(tx)));
    let (report, _) = run_within(20, rt, Job::new("write a and b", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    assert_eq!(std::fs::read_to_string(dir.join("a.js")).unwrap().lines().count(), 120);
    let writes: Vec<(String, usize, bool)> = rx.try_iter().filter_map(|e| match e {
        Event::Writing { path, lines, done, .. } => Some((path, lines, done)),
        _ => None,
    }).collect();
    assert!(writes.iter().any(|(p, n, _)| p == "b.js" && *n == 2), "the fenced block is shown: {writes:?}");
    assert!(writes.iter().any(|(p, n, done)| p == "a.js" && *n == 120 && *done), "the call's file, whole, at the end: {writes:?}");
    assert!(writes.iter().filter(|(p, _, d)| p == "a.js" && !d).count() >= 1, "and while it grew: {writes:?}");
    let _ = std::fs::remove_dir_all(&dir);
}

fn asking_model() -> Arc<Scripted> {
    Arc::new(Scripted::new().respond(|req| {
        let last = last_user(req);
        if req.messages.len() == 2 {
            return "```ask\nHow should the board be drawn?\n- Plain DOM (recommended) :: simplest, easy to style\n\
                - Canvas :: smoother animation for many pieces\n- WebGL :: only worth it for 3D\n```".into();
        }
        let pick = if last.contains("[the user picked: Canvas]") { "canvas" }
            else if last.contains("go with \"Plain DOM\"") { "dom" } else { return "Hmm.".into() };
        format!("```criteria\n- v holds {pick} :: verify: grep -qx {pick} v.txt\n```\n```write v.txt\n{pick}\n```\n\
            ```test grep -qx {pick} v.txt```\n```justify proportion\none word\n```\n```done\nv.txt holds {pick} (grep).\n```")
    }))
}

#[test]
fn the_lead_offers_explained_options_and_a_number_picks_one() {
    let dir = tmp("ask-pick");
    let (atx, arx) = channel::<String>();
    let (etx, erx) = channel();
    let mut job = Job::new("draw the board", &dir);
    job.answers = Some(arx);
    let rt = Runtime::new(asking_model(), RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() },
                          Events(Some(etx)));
    let asked = std::thread::spawn(move || {
        for e in erx.iter() {
            if let Event::Ask { question, options, .. } = e {
                let _ = atx.send("2".into()); // the user clicks / types the second option
                return Some((question, options));
            }
        }
        None
    });
    let (report, _) = run_within(30, rt, job);
    let (question, options) = asked.join().unwrap().expect("the Lead asked");
    assert_eq!(question, "How should the board be drawn?");
    let labels: Vec<_> = options.iter().map(|o| (o.label.as_str(), o.recommended)).collect();
    assert_eq!(labels, [("Plain DOM", true), ("Canvas", false), ("WebGL", false)]);
    assert_eq!(options[1].why, "smoother animation for many pieces");
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    assert_eq!(std::fs::read_to_string(dir.join("v.txt")).unwrap().trim(), "canvas");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn with_nobody_answering_the_lead_goes_with_its_recommended_option() {
    let dir = tmp("ask-default");
    let (_atx, arx) = channel::<String>();
    let mut job = Job::new("draw the board", &dir);
    job.answers = Some(arx);
    let cfg = RuntimeConfig { store: dir.join("store"), ask_wait: Duration::from_millis(300), ..RuntimeConfig::default() };
    let rt = Runtime::new(asking_model(), cfg, Events(None));
    let (report, _) = run_within(30, rt, job);
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    assert_eq!(std::fs::read_to_string(dir.join("v.txt")).unwrap().trim(), "dom");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn criteria_written_below_the_files_still_come_first() {
    // the model writes the file, then remembers its criteria: one reply still finishes the job
    let dir = tmp("criteria-last");
    let model = Arc::new(Scripted::new().respond(|_| {
        "```write app.py\nprint('hi')\n```\n\
            ```criteria\n- app.py prints hi :: verify: grep -q hi app.py\n```\n\
            ```test grep -q hi app.py```\n```done\napp.py prints hi (grep).\n```".into()
    }));
    let (report, _) = run_within(20, solo_rt(model.clone(), &dir), Job::new("app.py that prints hi", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    assert_eq!(model.seen.lock().unwrap().len(), 1, "no reply wasted on a refusal");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_file_written_before_any_criteria_is_kept_not_sent_again() {
    // reply 1: the whole file, no criteria. Reply 2: only the criteria and the check — the file is there
    let dir = tmp("held");
    let model = Arc::new(Scripted::new().respond(|req| {
        let last = last_user(req);
        if req.messages.len() == 2 {
            return "```write big.js\nconst board = [];\n```".into();
        }
        assert!(last.contains("big.js KEPT"), "{last}");
        "```criteria\n- big.js has the board :: verify: grep -q board big.js\n```\n\
            ```test grep -q board big.js```\n```done\nbig.js has the board (grep).\n```".into()
    }));
    let (report, _) = run_within(20, solo_rt(model.clone(), &dir), Job::new("a board in big.js", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    assert_eq!(std::fs::read_to_string(dir.join("big.js")).unwrap(), "const board = [];\n");
    assert_eq!(model.seen.lock().unwrap().len(), 2);
    let _ = std::fs::remove_dir_all(&dir);
}
