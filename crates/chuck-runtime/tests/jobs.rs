//! Jobs end to end, on a real folder, with a scripted model standing in for
//! SiliconFlow: the gates (criteria first, done only when verified after the
//! last change), money, the user's yes, the folder's access, the evidence
//! chain, rewinding.

use chuck_core::EvidenceKind;
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
fn hollow_tests_and_shell_edits_dont_fool_the_gate() {
    let dir = tmp("hollow");
    std::fs::write(dir.join("app.py"), "def f():\n    return 1\n").unwrap();
    let model = Arc::new(Scripted::new().respond(|req| {
        let last = last_user(req);
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
        .filter(|r| text(&r.messages[0]).contains("You are Chuck")).map(last_user).collect();
    assert!(lead_inputs.iter().any(|t| t.contains("proves nothing")));
    assert!(lead_inputs.iter().any(|t| t.contains("done REFUSED") && t.contains("NotImplementedError")));
    assert_eq!(std::fs::read_to_string(dir.join("app.py")).unwrap(), "def f():\n    return 2\n");
    drop(rx);
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
fn a_read_only_folder_is_never_changed() {
    if !has_bwrap() {
        return;
    }
    let dir = tmp("readonly");
    std::fs::write(dir.join("a.txt"), "old\n").unwrap();
    let model = Arc::new(Scripted::new().replies([
        "```criteria\n- a says new :: verify: grep -qx new a.txt\n```\n```write a.txt\nnew\n```\n\
         ```bash\necho new > a.txt; cat a.txt\n```",
        "I can't change a read-only folder.",
    ]));
    let cfg = RuntimeConfig { store: dir.join("store"), access: chuck_sandbox::Access::ReadOnly, lead_steps: 3,
                              ..RuntimeConfig::default() };
    let rt = Runtime::new(model.clone(), cfg, Events::none());
    let _ = run_within(20, rt, Job::new("make a say new", &dir));
    assert_eq!(std::fs::read_to_string(dir.join("a.txt")).unwrap(), "old\n");
    let fb = last_user(&model.seen.lock().unwrap()[1]);
    assert!(fb.contains("read-only (its access setting)"), "{fb}");
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
        if req.messages.len() == 2 {
            return "```criteria\n- v.txt holds 2 :: verify: grep -qx 2 v.txt\n```\n```write v.txt\n2\n```\n\
                ```bash\ncat v.txt\n```\n```test grep -qx 2 v.txt```\n```done\nv.txt holds 2 (checked).\n```".into();
        }
        "ok".into()
    }));
    let cfg = RuntimeConfig { store: dir.join("store"), audit: Some(ledger.clone()), ..RuntimeConfig::default() };
    let rt = Runtime::new(model, cfg, Events::none());
    let (report, _) = run_within(20, rt, Job::new("set v to 2", &dir));
    assert!(matches!(report.outcome, Outcome::Done { .. }), "{:?}", report.outcome);
    let v = ledger.verify();
    assert!(v.ok, "{v:?}");
    let kinds: Vec<(String, String)> = ledger.read_all().iter().map(|e| (e["kind"].as_str().unwrap().to_string(),
        e["command"].as_str().unwrap().to_string())).collect();
    assert!(kinds.contains(&("shell".into(), "cat v.txt".into())), "{kinds:?}");
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
