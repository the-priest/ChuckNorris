//! map, def, refs and rename in the fleet: by meaning through a language
//! server for the lead (chuck-code's fake one), by syntax for a worker, and
//! rename only through the gate.

use chuck_code::Manager;
use chuck_core::{DoneGate, EvidenceKind, Kernel, Role, Scale, SpawnSpec};
use chuck_model::{ChatRequest, Scripted};
use chuck_runtime::exec::{Exec, Sandbox};
use chuck_runtime::{Events, Job, Runtime, RuntimeConfig};
use std::path::PathBuf;
use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Mutex};
use std::time::Duration;

const FAKE: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../chuck-code/tests/fake_lsp.py");

fn project(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("chuck-codef-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(d.join("pkg")).unwrap();
    std::fs::write(d.join("pkg/store.py"), "class Store:\n    def save(self):\n        pass\n\ndef make_store():\n    return Store()\n").unwrap();
    std::fs::write(d.join("pkg/app.py"), "from pkg.store import make_store\ns = make_store()\n").unwrap();
    d.canonicalize().unwrap()
}

fn last_user(req: &ChatRequest) -> String {
    req.messages.iter().rev().find(|m| m["role"] == "user").and_then(|m| m["content"].as_str()).unwrap_or("").to_string()
}

fn servers() -> Option<Arc<Manager>> {
    std::env::split_paths(&std::env::var_os("PATH")?).map(|d| d.join("python3")).find(|p| p.is_file())?;
    let m = Arc::new(Manager::new());
    m.set_server("python", vec!["python3".into(), FAKE.into()]);
    Some(m)
}

fn run(dir: &std::path::Path, m: Arc<Manager>, replies: Vec<String>) -> (Arc<Scripted>, chuck_runtime::JobReport) {
    let model = Arc::new(Scripted::new().replies(replies));
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join(".store"), lsp: Some(m), lead_steps: 3,
                                                         ..RuntimeConfig::default() }, Events::none());
    let (tx, rx) = std::sync::mpsc::channel();
    let d = dir.to_path_buf();
    std::thread::spawn(move || {
        let _ = tx.send(rt.run(Job::new("rename make_store", &d)));
    });
    let r = rx.recv_timeout(Duration::from_secs(60)).expect("the job hung");
    (model, r)
}

#[test]
fn the_lead_asks_by_meaning_and_renames_only_through_the_gate() {
    let dir = project("lead");
    let Some(m) = servers() else { return };
    let (model, _) = run(&dir, m.clone(), vec![
        "```map```\n```def make_store```\n```refs make_store```\n```rename make_store build_store```".into(),
        "```criteria\n- make_store is called build_store everywhere :: verify: grep -rq build_store pkg\n```\n\
         ```rename make_store build_store```".into(),
        "Stopping here.".into()]);
    let seen = model.seen.lock().unwrap();
    let fb = last_user(&seen[1]);
    assert!(fb.contains("pkg/store.py (6 lines): Store[save], make_store()"), "{fb}");
    assert!(fb.contains("DEFINITIONS of make_store (1, via python3):\npkg/store.py:5  def make_store():"), "{fb}");
    assert!(fb.contains("REFERENCES to make_store (2, via python3); defined at pkg/store.py:5:\npkg/app.py:1"), "{fb}");
    assert!(fb.contains("[rename NOT done, nothing was changed: [refused — nothing may change yet"), "{fb}");
    let fb = last_user(&seen[2]);
    assert!(fb.contains("[renamed make_store → build_store via python3: 3 occurrence(s) in 2 file(s): pkg/app.py, pkg/store.py]"),
            "{fb}");
    assert_eq!(std::fs::read_to_string(dir.join("pkg/app.py")).unwrap(), "from pkg.store import build_store\ns = build_store()\n");
    m.stop_all();
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_worker_gets_syntax_answers_and_no_rename() {
    let dir = project("worker");
    let Some(m) = servers() else { return };
    let kernel = Kernel::new(4);
    let id = kernel.spawn(SpawnSpec::new("glm-1", Role::GlmWorker));
    let gate = Mutex::new(DoneGate::new("x", Scale::Small));
    gate.lock().unwrap().set_criteria(Role::GlmWorker, vec![("renamed".into(), None)]);
    let ev = Events::none();
    let exec = Exec { id, role: Role::GlmWorker, kernel: &kernel, root: &dir, gate: &gate,
                      sandbox: Sandbox { access: chuck_sandbox::Access::Full, bwrap: false, worker: true }, approve: None,
                      audit: None, stop: Arc::new(AtomicBool::new(false)), events: &ev,
                      cmd_timeout: Duration::from_secs(10), web: None, sources: 6, mcp: None, lsp: Some(&m), memory: None,
                      allowed_outside: None, team: None, hold: None };
    let r = exec.run("```def make_store```\n```rename make_store build_store```");
    assert!(r.feedback[0].contains("via syntax (no language server)"), "{:?}", r.feedback);
    assert!(r.feedback[1].contains("workers don't get"), "{:?}", r.feedback);
    assert!(!gate.lock().unwrap().evidence.iter().any(|e| matches!(e.kind, EvidenceKind::Diff { .. })));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn in_a_sandboxed_folder_the_server_runs_in_its_jail() {
    if chuck_sandbox::bwrap().is_none() {
        return;
    }
    let dir = project("jailed");
    let Some(m) = servers() else { return };
    let model = Arc::new(Scripted::new().replies(["```def make_store```", "ok"]));
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join(".store"), lsp: Some(m.clone()),
                                                         access: chuck_sandbox::Access::Sandbox, lead_steps: 2,
                                                         ..RuntimeConfig::default() }, Events::none());
    rt.run(Job::new("where is make_store", &dir));
    let fb = last_user(&model.seen.lock().unwrap()[1]);
    assert!(fb.contains("via python3"), "the server answered from inside the jail: {fb}");
    // it really is jailed: its process runs under bwrap
    let jailed = std::fs::read_dir("/proc").unwrap().flatten().any(|e| {
        let cmd = String::from_utf8_lossy(&std::fs::read(e.path().join("cmdline")).unwrap_or_default()).replace('\0', " ");
        cmd.contains("bwrap") && cmd.contains("fake_lsp.py") && cmd.contains("--unshare-net")
    });
    assert!(jailed, "an offline folder's server runs in an offline jail");
    m.stop_all();
    let _ = std::fs::remove_dir_all(&dir);
}
