//! "This folder freely, ask outside it": inside the folder anything goes;
//! a command or a tool that reaches outside asks the user, who may refuse,
//! allow once, or allow for the rest of the job; the jail hides the rest of
//! home from what a command doesn't name.

use chuck_model::{ChatRequest, Scripted};
use chuck_runtime::{Event, Events, Job, Runtime, RuntimeConfig};
use std::sync::Arc;

fn feedback(seen: &[ChatRequest], i: usize) -> String {
    seen[i].messages.last().unwrap()["content"].as_str().unwrap_or("").to_string()
}

#[test]
fn outside_the_folder_is_asked_inside_is_free() {
    // under home: /tmp is free to reach, so a project there couldn't show this
    let base = chuck_sandbox::jail::home().join(format!("chuck-outside-e2e-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&base);
    let proj = base.join("proj");
    std::fs::create_dir_all(&proj).unwrap();
    std::fs::create_dir_all(base.join("tools")).unwrap();
    std::fs::write(base.join("notes.txt"), "the secret plan\n").unwrap();
    std::fs::write(base.join("tools/ver.txt"), "gradle 8.11\n").unwrap();
    let proj = proj.canonicalize().unwrap();
    let model = Arc::new(Scripted::new().replies([
        "```criteria\n- works :: verify: true\n```\n```bash\necho inside > here.txt && cat here.txt\n```\n```bash\ncat ../notes.txt\n```".to_string(),
        "```bash\ncat ../notes.txt\n```\n```read ../notes.txt```".to_string(),
        "```bash\ncat ../tools/ver.txt\n```".to_string(),
        "```bash\ncat ../tools/ver.txt && ls ../tools\n```\n```bash\ncat \"$HOME/chuck-outside-e2e-PID/notes.txt\"\n```".replace("PID", &std::process::id().to_string()),
        "That's all.".to_string(),
    ]));
    let (tx, rx) = std::sync::mpsc::channel();
    let cfg = RuntimeConfig { store: base.join(".store"), access: chuck_sandbox::Access::Guarded, ..RuntimeConfig::default() };
    let rt = Runtime::new(model.clone(), cfg, Events(Some(tx)));
    // the user's answers as each question arrives: no, once (bash), once (read), for this job
    let mut answers = vec!["always", "yes", "yes", "no"];
    let (atx, arx) = std::sync::mpsc::channel();
    let mut job = Job::new("look around", &proj);
    job.answers = Some(arx);
    let h = std::thread::spawn(move || rt.run(job));
    let mut asked = Vec::new();
    for e in rx.iter() {
        match e {
            Event::Confirm { command, outside, .. } => {
                asked.push((command, outside));
                let a = answers.pop().unwrap_or("no");
                atx.send(a.to_string()).unwrap();
            }
            Event::Finished(_) => break,
            _ => {}
        }
    }
    h.join().unwrap();
    let seen = model.seen.lock().unwrap();
    let f1 = feedback(&seen, 1);
    assert!(f1.contains("RAN `echo inside > here.txt && cat here.txt` → exit 0") && f1.contains("inside"), "{f1}");
    assert!(f1.contains("the user said no"), "{f1}");
    let f2 = feedback(&seen, 2);
    assert!(f2.contains("the secret plan"), "allowed once: {f2}");
    assert!(f2.contains("READ ../notes.txt") && f2.matches("the secret plan").count() == 2, "{f2}");
    let f3 = feedback(&seen, 3);
    assert!(f3.contains("gradle 8.11"), "{f3}");
    let f4 = feedback(&seen, 4);
    assert!(f4.contains("gradle 8.11") && f4.contains("ver.txt"), "allowed for the job: no new question: {f4}");
    // asked exactly four times: no, once, once (read), for the job — the fifth command wasn't asked
    assert_eq!(asked.len(), 4, "{asked:?}");
    assert_eq!(asked[0].1, vec![base.display().to_string()], "the user sees the folder that would open");
    if chuck_sandbox::bwrap().is_some() {
        // a path it didn't spell out is still hidden by the jail
        assert!(f4.contains("No such file") && !f4.split("$HOME").nth(1).unwrap_or("").contains("the secret plan"), "{f4}");
    }
    let _ = std::fs::remove_dir_all(&base);
}
