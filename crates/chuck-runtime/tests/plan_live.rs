//! The plan the user watches moves: ```plan done N``` marks steps, a plan
//! sent again keeps what was done, a stale plan is pointed out, and a job
//! through every gate shows every step done.

use chuck_model::Scripted;
use chuck_runtime::{Event, Events, Job, Runtime, RuntimeConfig};
use std::sync::Arc;

#[test]
fn plan_steps_are_marked_and_kept() {
    let dir = std::env::temp_dir().join(format!("chuck-plan-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let model = Arc::new(Scripted::new().replies([
        "```plan\n- [ ] look\n- [ ] build\n- [ ] check\n```",
        "```plan done 1```",
        "```plan\n- [ ] look\n- [ ] build\n- [ ] check\n```\n```plan done 2 9```",
        "Not changing anything; here's the answer.",
    ]));
    let (tx, rx) = std::sync::mpsc::channel();
    Runtime::new(model.clone(), RuntimeConfig { store: dir.join(".s"), ..RuntimeConfig::default() }, Events(Some(tx)))
        .run(Job::new("q", &dir));
    let plans: Vec<Vec<(String, bool)>> = rx.try_iter().filter_map(|e| match e { Event::Plan { steps } => Some(steps), _ => None }).collect();
    assert_eq!(plans[0].iter().filter(|s| s.1).count(), 0);
    assert_eq!(plans[1], vec![("look".into(), true), ("build".into(), false), ("check".into(), false)]);
    assert!(plans[2][0].1, "sent again unmarked, step 1 stays done");
    let fb = model.seen.lock().unwrap()[3].messages.last().unwrap()["content"].as_str().unwrap().to_string();
    assert!(fb.contains("there's no step 9"), "{fb}");
    let _ = std::fs::remove_dir_all(&dir);
}
