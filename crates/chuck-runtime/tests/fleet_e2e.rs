//! The fleet end to end, on a real folder, with a scripted model standing in
//! for SiliconFlow: the Lead writes criteria and a plan, delegates to a
//! DeepSeek worker and a GLM worker in parallel, rejects shallow work,
//! re-verifies and merges what passes, is refused "done" until the whole
//! result is verified, and finishes only after the critic accepts.

use chuck_core::{EvidenceKind, Verdict, DEEPSEEK_MODEL, GLM_MODEL};
use chuck_model::{ChatRequest, Scripted};
use chuck_runtime::{Event, Events, Job, Outcome, Runtime, RuntimeConfig};
use std::path::PathBuf;
use std::sync::mpsc::channel;
use std::sync::Arc;

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("chuck-e2e-{}-{name}", std::process::id()));
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

fn said_before(req: &ChatRequest, needle: &str) -> bool {
    req.messages.iter().any(|m| m["role"] == "assistant" && text(m).contains(needle))
}

fn heard(req: &ChatRequest, needle: &str) -> bool {
    req.messages.iter().any(|m| m["role"] == "user" && text(m).contains(needle))
}

const GREET_V1: &str = "greet() { echo \"Hello, $1!\"; }\n";
const GREET_V2: &str = "greet() { echo \"Hello, ${1:-world}!\"; }\n";
const TEST_V2: &str = "#!/bin/bash\n. ./greet.sh\nok=0\n[ \"$(greet Bob)\" = \"Hello, Bob!\" ] && ok=$((ok+1))\n\
[ \"$(greet)\" = \"Hello, world!\" ] && ok=$((ok+1))\nif [ $ok = 2 ]; then echo \"2 passed\"; else echo \"1 failed\"; exit 1; fi\n";

fn lead(req: &ChatRequest) -> String {
    if req.messages.len() == 2 {
        // a change before criteria (refused), then criteria, plan, and two parallel tasks
        return "Let me start.\n```write hack.txt\nnot yet\n```\n\
            ```criteria\n- greet NAME prints `Hello, NAME!` and greet alone prints `Hello, world!` :: verify: bash test.sh\n\
            - the README documents greet :: verify: grep -q greet README.md\n```\n\
            ```plan\n- t1: fix greet and its test (DeepSeek)\n- t2: document it (GLM)\n- verify the whole result\n```\n\
            ```assign deepseek\ntitle: Fix greet\ncriteria:\n- greet NAME prints Hello, NAME! and greet alone prints \
            Hello, world! :: verify: bash test.sh\nbrief:\ngreet.sh prints the wrong greeting; test.sh must cover it.\n```\n\
            ```assign glm\ntitle: Document greet\ncriteria:\n- README.md documents greet :: verify: grep -q greet README.md\n\
            brief:\nWrite README.md describing greet.sh.\n```".into();
    }
    let last = last_user(req);
    let mut out = String::new();
    if last.contains("SUBMISSION t1") {
        if said_before(req, "review t1 reject") {
            out.push_str("```review t1 accept\nThe default is handled and tested.\n```\n");
        } else {
            out.push_str("```review t1 reject\n- greet with no name prints `Hello, !`: give it a default\n\
                - test.sh doesn't cover that case\n```\n");
        }
    }
    if last.contains("SUBMISSION t2") {
        out.push_str("```review t2 accept\nClear.\n```\n");
    }
    if !out.is_empty() {
        return out;
    }
    if heard(req, "t1 accepted") && heard(req, "t2 accepted") {
        if !said_before(req, "```done") {
            return "```done\nFixed greet and documented it.\n```".into();
        }
        if last.contains("done REFUSED by the gate") {
            return "```test bash test.sh```\n```test grep -q greet README.md```\n\
                ```justify proportion\nThe bug was one line; the fix plus a test for both cases is the whole job.\n```\n\
                ```done\nFixed greet.\n```".into();
        }
        if last.contains("independent review") {
            return "```done\ngreet now prints `Hello, NAME!`, defaults to `Hello, world!` with no name; test.sh \
                covers both (2 passed after the merge) and README.md documents it.\n```".into();
        }
    }
    "Waiting for the workers.".into()
}

fn deepseek_worker(req: &ChatRequest) -> String {
    let last = last_user(req);
    if req.messages.len() == 2 {
        return "Looks easy.\n```submit\nAll done.\n```".into(); // nothing done: the gate refuses
    }
    if last.contains("submission REFUSED") {
        return format!("```read greet.sh```\n```write greet.sh\n{GREET_V1}```\n```test bash test.sh```\n\
            ```submit\nFixed the greeting; bash test.sh passes.\n```");
    }
    if last.contains("REJECTED") {
        return format!("```write greet.sh\n{GREET_V2}```\n```write test.sh\n{TEST_V2}```\n```test bash test.sh```\n\
            ```submit\nAdded the default and a test for it: 2 passed.\n```");
    }
    "Thinking about it.".into()
}

fn glm_worker(req: &ChatRequest) -> String {
    if req.messages.len() == 2 {
        return "```write README.md\n# greet\n\n`greet NAME` prints a greeting; with no name it greets the world.\n```\n\
            ```test grep -q greet README.md```\n```submit\nREADME.md documents greet.\n```".into();
    }
    "Nothing left.".into()
}

fn critic(req: &ChatRequest) -> String {
    let input = last_user(req);
    if input.contains("defaults to") { "ACCEPT".into() } else { "REJECT\n- the summary doesn't say how it was verified".into() }
}

#[test]
fn lead_and_two_families_deliver_through_every_gate() {
    let dir = tmp("fleet");
    let proj = dir.join("proj");
    std::fs::create_dir_all(&proj).unwrap();
    std::fs::write(proj.join("greet.sh"), "greet() { echo \"Hi\"; }\n").unwrap();
    std::fs::write(proj.join("test.sh"), "#!/bin/bash\n. ./greet.sh\nif [ \"$(greet Bob)\" = \"Hello, Bob!\" ]; \
        then echo \"1 passed\"; else echo \"1 failed\"; exit 1; fi\n").unwrap();

    let model = Arc::new(Scripted::new().respond(|req| {
        let sys = text(&req.messages[0]);
        if sys == chuck_runtime::prompts::CRITIC {
            critic(req)
        } else if sys.contains("You are the LEAD") {
            lead(req)
        } else if sys.contains("You are ds-") {
            assert_eq!(req.model, DEEPSEEK_MODEL);
            deepseek_worker(req)
        } else if sys.contains("You are glm-") {
            assert_eq!(req.model, GLM_MODEL);
            glm_worker(req)
        } else {
            panic!("who is this? {sys}")
        }
    }));
    let (tx, rx) = channel();
    let cfg = RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() };
    let rt = Runtime::new(model.clone(), cfg, Events(Some(tx)));
    let report = rt.run(Job::new("greet is broken: fix it and document it", &proj));
    let events: Vec<Event> = rx.try_iter().collect();

    assert!(matches!(&report.outcome, Outcome::Done { summary } if summary.contains("defaults to")),
            "{:?}", report.outcome);
    assert!(report.gate.is_done());

    // the user's folder has the merged, verified result, and nothing written before the criteria
    assert_eq!(std::fs::read_to_string(proj.join("greet.sh")).unwrap(), GREET_V2);
    assert_eq!(std::fs::read_to_string(proj.join("test.sh")).unwrap(), TEST_V2);
    assert!(std::fs::read_to_string(proj.join("README.md")).unwrap().contains("greet"));
    assert!(!proj.join("hack.txt").exists());
    assert!(events.iter().any(|e| matches!(e, Event::Action { tool, ok: false, summary, .. }
        if tool == "write" && summary.contains("nothing may change yet"))));

    // both families worked, each in its own copy
    let models: Vec<String> = model.seen.lock().unwrap().iter().map(|r| r.model.clone()).collect();
    assert!(models.iter().any(|m| m == GLM_MODEL) && models.iter().any(|m| m == DEEPSEEK_MODEL));
    let assigned: Vec<(String, chuck_core::Role)> = events.iter().filter_map(|e| match e {
        Event::Assigned { task, role, .. } => Some((task.clone(), *role)),
        _ => None,
    }).collect();
    assert_eq!(assigned, vec![("t1".into(), chuck_core::Role::DeepSeekWorker), ("t2".into(), chuck_core::Role::GlmWorker)]);

    // t1 was submitted twice (rejected once), t2 once; both merged
    let subs: Vec<&str> = events.iter().filter_map(|e| match e {
        Event::Submitted { task, .. } => Some(task.as_str()),
        _ => None,
    }).collect();
    assert_eq!(subs.iter().filter(|t| **t == "t1").count(), 2);
    assert_eq!(subs.iter().filter(|t| **t == "t2").count(), 1);
    assert!(events.iter().any(|e| matches!(e, Event::Reviewed { task, accepted: false, defects }
        if task == "t1" && defects.len() == 2)));
    let merged: Vec<&str> = events.iter().filter_map(|e| match e {
        Event::Merged { task, conflicts, .. } if conflicts.is_empty() => Some(task.as_str()),
        _ => None,
    }).collect();
    assert_eq!(merged.len(), 2);

    // the critic rejected once and then accepted; both verdicts are evidence
    let verdicts: Vec<bool> = report.gate.evidence.iter().filter_map(|e| match &e.kind {
        EvidenceKind::Review { verdict, .. } => Some(*verdict == Verdict::Accepted),
        _ => None,
    }).collect();
    assert_eq!(verdicts, vec![false, true]);
    // the final verification ran after the last merge
    let last_diff = report.gate.evidence.iter().filter(|e| matches!(e.kind, EvidenceKind::Diff { .. }))
        .map(|e| e.seq).max().unwrap();
    assert!(report.gate.evidence.iter().any(|e| e.seq > last_diff
        && matches!(&e.kind, EvidenceKind::Test { cmd, exit: 0, .. } if cmd == "bash test.sh")));

    // every worker was a child of the lead and has exited; the copies are gone
    let ps = rt.kernel.ps(true);
    let workers: Vec<_> = ps.iter().filter(|p| p.role.is_worker()).collect();
    assert_eq!(workers.len(), 2);
    assert!(workers.iter().all(|p| p.ppid == Some(report.lead) && !p.state.alive()));
    assert!(std::fs::read_dir(dir.join("store")).map(|d| d.count() == 0).unwrap_or(true));
    assert!(report.tokens > 0);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn out_of_budget_is_paused_with_gaps_never_done() {
    let dir = tmp("budget");
    std::fs::write(dir.join("a.txt"), "x\n").unwrap();
    let model = Arc::new(Scripted::new().respond(|_| {
        "```criteria\n- a.txt says hello :: verify: grep -q hello a.txt\n```\n```read a.txt```".to_string()
    }));
    let cfg = RuntimeConfig { store: dir.join("store"), job_quota: chuck_core::Quota::tokens(300),
                              ..RuntimeConfig::default() };
    let rt = Runtime::new(model.clone(), cfg, Events::none());
    let report = rt.run(Job::new("make a.txt say hello", &dir));
    match &report.outcome {
        Outcome::Paused { gaps } => {
            assert!(gaps.iter().any(|g| g.contains("quota") || g.contains("budget")), "{gaps:?}");
            assert!(gaps.iter().any(|g| g.contains("grep -q hello a.txt")), "{gaps:?}");
        }
        other => panic!("expected Paused, got {other:?}"),
    }
    assert!(!report.gate.is_done());
    assert!(model.calls() <= 3, "TERM gives one last call, then KILL: {}", model.calls());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_question_is_answered_without_claiming_done() {
    let dir = tmp("question");
    std::fs::write(dir.join("main.rs"), "fn main() {}\n").unwrap();
    let model = Arc::new(Scripted::new().replies(["```read main.rs```", "It's an empty Rust program."]));
    let rt = Runtime::new(model, RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() }, Events::none());
    let report = rt.run(Job::new("what does main.rs do?", &dir));
    assert_eq!(report.outcome, Outcome::Answered { text: "It's an empty Rust program.".into() });
    assert!(!report.gate.is_done());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn done_without_work_is_refused() {
    let dir = tmp("nodone");
    std::fs::write(dir.join("x.txt"), "1\n").unwrap();
    let model = Arc::new(Scripted::new().replies([
        "```criteria\n- x.txt holds 2 :: verify: grep -qx 2 x.txt\n```\n```write x.txt\n2\n```\n```done\nDone!\n```",
    ]).respond(|req| {
        // after the refusal: verify, then done
        if last_user(req).contains("done REFUSED by the gate") {
            "```test grep -qx 2 x.txt```\n```done\nx.txt holds 2; grep -qx 2 x.txt passes.\n```".into()
        } else if text(&req.messages[0]) == chuck_runtime::prompts::CRITIC {
            "ACCEPT".into()
        } else {
            "Hmm.".into()
        }
    }));
    let (tx, rx) = channel();
    let rt = Runtime::new(model, RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() }, Events(Some(tx)));
    let report = rt.run(Job::new("make x.txt hold 2", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    let gates: Vec<Event> = rx.try_iter().filter(|e| matches!(e, Event::Gate { .. })).collect();
    // the first turn ended with the gate open: the done in it was refused
    assert!(matches!(&gates[0], Event::Gate { unmet, .. } if unmet.iter().any(|u| u.contains("after the last change"))));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_race_keeps_the_better_verified_entry() {
    let dir = tmp("race");
    std::fs::write(dir.join("n.txt"), "0\n").unwrap();
    let model = Arc::new(Scripted::new().respond(|req| {
        let sys = text(&req.messages[0]);
        let last = last_user(req);
        if sys == chuck_runtime::prompts::CRITIC {
            return "ACCEPT".into();
        }
        if sys.contains("You are ds-") {
            // two passing verification runs: the stronger entry
            return if req.messages.len() == 2 {
                "```write n.txt\n7\n```\n```test grep -qx 7 n.txt```\n```test grep -qx 7 n.txt```\n```submit\n7, checked twice\n```".into()
            } else { "ok".into() };
        }
        if sys.contains("You are glm-") {
            return if req.messages.len() == 2 {
                "```write n.txt\n7\n```\n```test grep -qx 7 n.txt```\n```submit\n7\n```".into()
            } else { "ok".into() };
        }
        if req.messages.len() == 2 {
            return "```criteria\n- n.txt holds 7 :: verify: grep -qx 7 n.txt\n```\n```plan\n- race it\n```\n\
                ```assign race\ntitle: Set n\ncriteria:\n- n.txt holds 7 :: verify: grep -qx 7 n.txt\nbrief: write 7\n```".into();
        }
        if last.contains("SUBMISSION t1") {
            assert!(last.contains("from ds-1"), "the entry with more passing checks wins: {last}");
            return "```review t1 accept\ngood\n```".into();
        }
        if heard(req, "t1 accepted") {
            return "```test grep -qx 7 n.txt```\n```justify proportion\none value was asked for\n```\n\
                ```done\nn.txt holds 7 (grep -qx 7 n.txt passes).\n```".into();
        }
        "Waiting.".into()
    }));
    let (tx, rx) = channel();
    let rt = Runtime::new(model, RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() }, Events(Some(tx)));
    let report = rt.run(Job::new("set n.txt to 7", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    assert_eq!(std::fs::read_to_string(dir.join("n.txt")).unwrap(), "7\n");
    let events: Vec<Event> = rx.try_iter().collect();
    let roles: Vec<chuck_core::Role> = events.iter().filter_map(|e| match e {
        Event::Assigned { role, .. } => Some(*role),
        _ => None,
    }).collect();
    assert_eq!(roles, vec![chuck_core::Role::DeepSeekWorker, chuck_core::Role::GlmWorker]);
    assert_eq!(events.iter().filter(|e| matches!(e, Event::Merged { .. })).count(), 1);
    let _ = std::fs::remove_dir_all(&dir);
}

/// Run a job on its own thread; a hang is a failure, not a stuck test suite.
fn run_within(secs: u64, rt: Runtime, job: Job) -> (chuck_runtime::JobReport, Runtime) {
    let (tx, rx) = channel();
    std::thread::spawn(move || {
        let r = rt.run(job);
        let _ = tx.send((r, rt));
    });
    rx.recv_timeout(std::time::Duration::from_secs(secs)).expect("the job hung")
}

#[test]
fn a_lead_that_idles_while_a_submission_waits_is_told_not_hung() {
    let dir = tmp("idle");
    std::fs::write(dir.join("v.txt"), "1\n").unwrap();
    let model = Arc::new(Scripted::new().respond(|req| {
        let sys = text(&req.messages[0]);
        let last = last_user(req);
        if sys == chuck_runtime::prompts::CRITIC {
            return "ACCEPT".into();
        }
        if sys.contains("You are glm-") {
            return if req.messages.len() == 2 {
                "```write v.txt\n2\n```\n```test grep -qx 2 v.txt```\n```submit\nv is 2\n```".into()
            } else { "ok".into() };
        }
        if req.messages.len() == 2 {
            return "```criteria\n- v.txt holds 2 :: verify: grep -qx 2 v.txt\n```\n```plan\n- delegate\n```\n\
                ```assign glm\ntitle: v\ncriteria:\n- v.txt holds 2 :: verify: grep -qx 2 v.txt\nbrief: set it\n```".into();
        }
        if last.contains("SUBMISSION t1") {
            return "Let me think about this submission.".into(); // no review: the worker now waits on the lead
        }
        if last.contains("waiting for your review") {
            return "```review t1 accept\nfine\n```".into();
        }
        if heard(req, "t1 accepted") {
            return "```test grep -qx 2 v.txt```\n```justify proportion\none value\n```\n```done\nv.txt holds 2 (checked).\n```".into();
        }
        "Waiting.".into()
    }));
    let rt = Runtime::new(model, RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() }, Events::none());
    let (report, _) = run_within(20, rt, Job::new("set v to 2", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn hollow_tests_and_shell_edits_dont_fool_the_gate() {
    let dir = tmp("hollow");
    std::fs::write(dir.join("app.py"), "def f():\n    return 1\n").unwrap();
    let model = Arc::new(Scripted::new().respond(|req| {
        let sys = text(&req.messages[0]);
        let last = last_user(req);
        if sys == chuck_runtime::prompts::CRITIC {
            return "ACCEPT".into();
        }
        if req.messages.len() == 2 {
            // a stub slipped in through the shell (no write block), then a test that can't fail
            return "```criteria\n- f returns 2\n```\n```bash\nprintf 'def f():\\n    raise NotImplementedError\\n' > app.py\n```\n\
                ```test true```\n```done\nf returns 2.\n```".into();
        }
        if last.contains("NotImplementedError") && last.contains("done REFUSED") {
            return "```write app.py\ndef f():\n    return 2\n```\n```test python3 -c 'import app; assert app.f() == 2'```\n\
                ```done\nf returns 2; checked by importing it.\n```".into();
        }
        "Hmm.".into()
    }));
    let (tx, rx) = channel();
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() },
                          Events(Some(tx)));
    let (report, _) = run_within(20, rt, Job::new("make f return 2", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    // the hollow `test true` never counted as a passing test
    assert!(!report.gate.evidence.iter().any(|e| matches!(&e.kind, EvidenceKind::Test { cmd, .. } if cmd == "true")));
    // the first done was refused for the stub the shell wrote
    let lead_inputs: Vec<String> = model.seen.lock().unwrap().iter()
        .filter(|r| text(&r.messages[0]).contains("You are the LEAD")).map(last_user).collect();
    assert!(lead_inputs.iter().any(|t| t.contains("proves nothing")));
    assert!(lead_inputs.iter().any(|t| t.contains("done REFUSED") && t.contains("NotImplementedError")));
    assert_eq!(std::fs::read_to_string(dir.join("app.py")).unwrap(), "def f():\n    return 2\n");
    let critic_calls = model.seen.lock().unwrap().iter()
        .filter(|r| text(&r.messages[0]) == chuck_runtime::prompts::CRITIC).count();
    assert_eq!(critic_calls, 1, "the critic only sees work that passed the gate");
    drop(rx);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn the_critic_isnt_asked_again_about_unchanged_work() {
    let dir = tmp("critic-again");
    std::fs::write(dir.join("k.txt"), "a\n").unwrap();
    let model = Arc::new(Scripted::new().respond(|req| {
        let sys = text(&req.messages[0]);
        if sys == chuck_runtime::prompts::CRITIC {
            return "REJECT\n- k.txt should end with a newline".into();
        }
        if req.messages.len() == 2 {
            return "```criteria\n- k.txt says b :: verify: grep -qx b k.txt\n```\n```write k.txt\nb\n```\n\
                ```test grep -qx b k.txt```\n```done\nk.txt says b.\n```".into();
        }
        "```done\nk.txt says b.\n```".into() // the same done, again and again
    }));
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), lead_steps: 6,
                                                          ..RuntimeConfig::default() }, Events::none());
    let (report, _) = run_within(20, rt, Job::new("make k say b", &dir));
    assert!(matches!(report.outcome, Outcome::Paused { .. }), "{:?}", report.outcome);
    let critic_calls = model.seen.lock().unwrap().iter()
        .filter(|r| text(&r.messages[0]) == chuck_runtime::prompts::CRITIC).count();
    assert_eq!(critic_calls, 1);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_worker_that_removes_its_own_todo_can_submit() {
    let dir = tmp("todo-fixed");
    std::fs::write(dir.join("m.py"), "X = 0\n").unwrap();
    let model = Arc::new(Scripted::new().respond(|req| {
        let sys = text(&req.messages[0]);
        let last = last_user(req);
        if sys == chuck_runtime::prompts::CRITIC {
            return "ACCEPT".into();
        }
        if sys.contains("You are ds-") {
            if req.messages.len() == 2 {
                return "```write m.py\nX = 1  # TODO: confirm\n```\n```test grep -q 'X = 1' m.py```\n```submit\nX is 1\n```".into();
            }
            if last.contains("submission REFUSED") {
                assert!(last.contains("TODO"), "{last}");
                return "```write m.py\nX = 1\n```\n```test grep -qx 'X = 1' m.py```\n```submit\nX is 1, no TODO left\n```".into();
            }
            return "ok".into();
        }
        if req.messages.len() == 2 {
            return "```criteria\n- X is 1 :: verify: grep -qx 'X = 1' m.py\n```\n```plan\n- delegate\n```\n\
                ```assign deepseek\ntitle: X\ncriteria:\n- X is 1 :: verify: grep -qx 'X = 1' m.py\nbrief: set X\n```".into();
        }
        if last.contains("SUBMISSION t1") {
            return "```review t1 accept\nok\n```".into();
        }
        if heard(req, "t1 accepted") {
            return "```test grep -qx 'X = 1' m.py```\n```justify proportion\none constant\n```\n```done\nX is 1 (grep).\n```".into();
        }
        "Waiting.".into()
    }));
    let rt = Runtime::new(model, RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() }, Events::none());
    let (report, _) = run_within(20, rt, Job::new("set X to 1", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    assert_eq!(std::fs::read_to_string(dir.join("m.py")).unwrap(), "X = 1\n");
    let _ = std::fs::remove_dir_all(&dir);
}

fn has_bwrap() -> bool {
    chuck_sandbox::bwrap().is_some()
}

#[test]
fn before_the_criteria_a_command_can_look_but_not_change() {
    if !has_bwrap() {
        return;
    }
    let dir = tmp("look-only");
    std::fs::write(dir.join("a.txt"), "old\n").unwrap();
    let model = Arc::new(Scripted::new().replies([
        "```bash\ncat a.txt; echo new > a.txt; echo made > b.txt\n```",
        "It says old.",
    ]));
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() },
                          Events::none());
    let (report, _) = run_within(20, rt, Job::new("what's in a.txt?", &dir));
    assert_eq!(report.outcome, Outcome::Answered { text: "It says old.".into() });
    assert_eq!(std::fs::read_to_string(dir.join("a.txt")).unwrap(), "old\n");
    assert!(!dir.join("b.txt").exists());
    let fb = last_user(&model.seen.lock().unwrap()[1]);
    assert!(fb.contains("old") && fb.contains("ran read-only"), "{fb}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_critical_command_runs_only_on_the_users_yes() {
    let dir = tmp("critical");
    // harmless if it runs: decodes `echo ran > proof.txt` and pipes it into bash (curl|sh-shaped: critical)
    let cmd = "echo ZWNobyByYW4gPiBwcm9vZi50eHQK | base64 -d | bash";
    for (answer, runs) in [("no", false), ("yes", true)] {
        let _ = std::fs::remove_file(dir.join("proof.txt"));
        let script = format!("```criteria\n- proof exists :: verify: test -f proof.txt\n```\n```bash\n{cmd}\n```");
        let model = Arc::new(Scripted::new().replies([script.as_str(), "ok, stopping here."]));
        let (tx, rx) = channel();
        let (atx, arx) = channel();
        let mut job = Job::new("make proof", &dir);
        job.answers = Some(arx);
        let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), lead_steps: 2,
                                                              ..RuntimeConfig::default() }, Events(Some(tx)));
        // the user, at the keyboard: answers when asked
        std::thread::spawn(move || {
            for e in rx.iter() {
                if matches!(e, Event::Confirm { .. }) {
                    let _ = atx.send(answer.to_string());
                }
            }
        });
        let _ = run_within(20, rt, job);
        assert_eq!(dir.join("proof.txt").exists(), runs, "answer {answer}");
        let fb = last_user(&model.seen.lock().unwrap()[1]);
        assert_eq!(fb.contains("didn't approve"), !runs, "{fb}");
    }
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn nobody_at_the_keyboard_means_no() {
    let dir = tmp("no-keyboard");
    let model = Arc::new(Scripted::new().replies([
        "```criteria\n- x :: verify: true\n```\n```bash\necho ZWNobyByYW4gPiBwcm9vZi50eHQK | base64 -d | bash\n```",
        "Stopping.",
    ]));
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), lead_steps: 2,
                                                          ..RuntimeConfig::default() }, Events::none());
    let _ = run_within(20, rt, Job::new("make proof", &dir));
    assert!(!dir.join("proof.txt").exists());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_worker_never_runs_a_critical_command() {
    let dir = tmp("worker-critical");
    std::fs::write(dir.join("f.txt"), "1\n").unwrap();
    let model = Arc::new(Scripted::new().respond(|req| {
        let sys = text(&req.messages[0]);
        if sys.contains("You are ds-") {
            return if req.messages.len() == 2 {
                "```bash\necho ZWNobyByYW4gPiBwcm9vZi50eHQK | base64 -d | bash\n```".into()
            } else { "```submit\nblocked\n```".into() };
        }
        if req.messages.len() == 2 {
            return "```criteria\n- f is 2 :: verify: grep -qx 2 f.txt\n```\n```plan\n- delegate\n```\n\
                ```assign deepseek\ntitle: f\ncriteria:\n- f is 2 :: verify: grep -qx 2 f.txt\nbrief: set f\n```".into();
        }
        "Waiting.".into()
    }));
    let (atx, arx) = channel::<String>();
    let mut job = Job::new("set f", &dir);
    job.answers = Some(arx);
    let _keep = atx; // a user is there, but workers don't ask
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), lead_steps: 4, worker_steps: 3,
                                                          ..RuntimeConfig::default() }, Events::none());
    let _ = run_within(30, rt, job);
    let worker_fb: Vec<String> = model.seen.lock().unwrap().iter()
        .filter(|r| text(&r.messages[0]).contains("You are ds-") && r.messages.len() > 2)
        .map(last_user).collect();
    assert!(worker_fb.iter().any(|f| f.contains("background workers never run")), "{worker_fb:?}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_read_only_folder_is_never_changed() {
    if !has_bwrap() {
        return;
    }
    let dir = tmp("readonly");
    std::fs::write(dir.join("a.txt"), "old\n").unwrap();
    let model = Arc::new(Scripted::new().replies([
        "```criteria\n- a says new :: verify: grep -qx new a.txt\n```\n```write a.txt\nnew\n```\n\
         ```bash\necho new > a.txt; cat a.txt\n```\n```plan\n- x\n```\n\
         ```assign glm\ntitle: t\ncriteria:\n- a :: verify: true\nbrief: b\n```",
        "I can't change a read-only folder.",
    ]));
    let cfg = RuntimeConfig { store: dir.join("store"), access: chuck_sandbox::Access::ReadOnly, lead_steps: 3,
                              ..RuntimeConfig::default() };
    let rt = Runtime::new(model.clone(), cfg, Events::none());
    let _ = run_within(20, rt, Job::new("make a say new", &dir));
    assert_eq!(std::fs::read_to_string(dir.join("a.txt")).unwrap(), "old\n");
    let fb = last_user(&model.seen.lock().unwrap()[1]);
    assert!(fb.contains("read-only (its access setting)") && fb.contains("assign refused"), "{fb}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn an_offline_folder_has_no_network() {
    if !has_bwrap() {
        return;
    }
    let dir = tmp("offline");
    let model = Arc::new(Scripted::new().replies([
        "```criteria\n- n :: verify: true\n```\n```bash\ngetent hosts example.com >/dev/null && echo NET-ON || echo NET-OFF\n```",
        "Done looking.",
    ]));
    let cfg = RuntimeConfig { store: dir.join("store"), access: chuck_sandbox::Access::Sandbox, lead_steps: 2,
                              ..RuntimeConfig::default() };
    let rt = Runtime::new(model.clone(), cfg, Events::none());
    let _ = run_within(20, rt, Job::new("check the network", &dir));
    let fb = last_user(&model.seen.lock().unwrap()[1]);
    assert!(fb.contains("NET-OFF"), "{fb}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn past_the_daily_limit_no_agent_gets_a_call() {
    let dir = tmp("limit");
    let spend = Arc::new(chuck_store::Spend::at(&dir.join("spend.json")));
    spend.add(5.0);
    let model = Arc::new(Scripted::new().replies(["```criteria\n- x\n```"]));
    let cfg = RuntimeConfig { store: dir.join("store"), spend: Some(spend), day_limit: 3.0, ..RuntimeConfig::default() };
    let rt = Runtime::new(model.clone(), cfg, Events::none());
    let (report, _) = run_within(20, rt, Job::new("anything", &dir));
    match &report.outcome {
        Outcome::Paused { gaps } => assert!(gaps[0].contains("daily spending limit"), "{gaps:?}"),
        other => panic!("{other:?}"),
    }
    assert_eq!(model.calls(), 0);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn every_command_any_agent_ran_is_in_the_evidence_chain() {
    let dir = tmp("audit");
    std::fs::write(dir.join("v.txt"), "1\n").unwrap();
    let ledger = Arc::new(chuck_store::Ledger::at(&dir.join("ledger.jsonl")));
    let model = Arc::new(Scripted::new().respond(|req| {
        let sys = text(&req.messages[0]);
        let last = last_user(req);
        if sys == chuck_runtime::prompts::CRITIC {
            return "ACCEPT".into();
        }
        if sys.contains("You are glm-") {
            return if req.messages.len() == 2 {
                "```write v.txt\n2\n```\n```test grep -qx 2 v.txt```\n```submit\nv is 2\n```".into()
            } else { "ok".into() };
        }
        if req.messages.len() == 2 {
            return "```criteria\n- v.txt holds 2 :: verify: grep -qx 2 v.txt\n```\n```plan\n- delegate\n```\n\
                ```assign glm\ntitle: v\ncriteria:\n- v.txt holds 2 :: verify: grep -qx 2 v.txt\nbrief: set it\n```".into();
        }
        if last.contains("SUBMISSION t1") {
            return "```review t1 accept\nfine\n```".into();
        }
        if heard(req, "t1 accepted") {
            return "```test grep -qx 2 v.txt```\n```justify proportion\none value\n```\n```done\nv.txt holds 2 (checked).\n```".into();
        }
        "Waiting.".into()
    }));
    let cfg = RuntimeConfig { store: dir.join("store"), audit: Some(ledger.clone()), ..RuntimeConfig::default() };
    let rt = Runtime::new(model, cfg, Events::none());
    let (report, _) = run_within(20, rt, Job::new("set v to 2", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    let v = ledger.verify();
    assert!(v.ok, "{v:?}");
    let kinds: Vec<(String, String)> = ledger.read_all().iter().map(|e| (e["kind"].as_str().unwrap().to_string(),
        e["command"].as_str().unwrap().to_string())).collect();
    assert!(kinds.contains(&("agent".into(), "grep -qx 2 v.txt".into())), "{kinds:?}");
    assert!(kinds.contains(&("shell".into(), "grep -qx 2 v.txt".into())), "{kinds:?}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_whole_job_can_be_rewound() {
    if std::process::Command::new("git").arg("--version").output().is_err() {
        return;
    }
    let dir = tmp("rewind");
    let proj = dir.join("proj");
    std::fs::create_dir_all(&proj).unwrap();
    std::fs::write(proj.join("x.txt"), "1\n").unwrap();
    let model = Arc::new(Scripted::new().respond(|req| {
        if text(&req.messages[0]) == chuck_runtime::prompts::CRITIC {
            return "ACCEPT".into();
        }
        if req.messages.len() == 2 {
            return "```criteria\n- x.txt holds 2 :: verify: grep -qx 2 x.txt\n```\n```write x.txt\n2\n```\n\
                ```bash\necho extra > made-by-shell.txt\n```\n```test grep -qx 2 x.txt```\n```done\nx.txt holds 2 (grep).\n```".into();
        }
        "ok".into()
    }));
    let store = dir.join("checkpoints");
    let cfg = RuntimeConfig { store: dir.join("store"), checkpoints: Some(store.clone()), ..RuntimeConfig::default() };
    let rt = Runtime::new(model, cfg, Events::none());
    let (report, _) = run_within(20, rt, Job::new("make x hold 2", &proj));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    assert_eq!(std::fs::read_to_string(proj.join("x.txt")).unwrap(), "2\n");
    let sh = chuck_store::Shadow::new(&proj, &store).unwrap();
    assert!(sh.points(10).len() >= 2, "one before the job, one before the step");
    sh.restore(report.checkpoint.as_deref().unwrap()).unwrap();
    assert_eq!(std::fs::read_to_string(proj.join("x.txt")).unwrap(), "1\n");
    assert!(!proj.join("made-by-shell.txt").exists(), "what a command made is undone too");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn what_the_user_types_mid_job_reaches_the_lead_next_step() {
    let dir = tmp("steer");
    let model = Arc::new(Scripted::new().respond(|req| {
        if req.messages.len() == 2 {
            return "```criteria\n- n :: verify: true\n```\n```bash\nsleep 0.6\n```".into();
        }
        "Noted.".into()
    }));
    let (stx, srx) = channel();
    let mut job = Job::new("look around", &dir);
    job.steer = Some(srx);
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), lead_steps: 2,
                                                          ..RuntimeConfig::default() }, Events::none());
    std::thread::spawn(move || {
        std::thread::sleep(std::time::Duration::from_millis(200));
        let _ = stx.send("also check the README".to_string());
    });
    let _ = run_within(20, rt, job);
    let second = last_user(&model.seen.lock().unwrap()[1]);
    assert!(second.contains("[the user, while you work") && second.contains("also check the README"), "{second}");
    let _ = std::fs::remove_dir_all(&dir);
}
