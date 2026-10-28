//! What the harness itself costs, with a model that answers at once: a solo
//! job of seven turns (a write, reads, greps, commands, a check, done) on a
//! project of 300 files. Development only:
//!
//!   cargo run --release -p chuck-runtime --example overhead

use chuck_model::Scripted;
use chuck_runtime::{Events, Job, Runtime, RuntimeConfig};
use std::sync::Arc;
use std::time::Instant;

fn main() {
    let outer = std::env::temp_dir().join(format!("chuck-overhead-{}", std::process::id()));
    let dir = outer.join("proj");
    let _ = std::fs::remove_dir_all(&outer);
    for i in 0..300 {
        let d = dir.join(format!("src/m{}", i % 20));
        std::fs::create_dir_all(&d).unwrap();
        std::fs::write(d.join(format!("f{i}.js")), format!("export const v{i} = {i};\n").repeat(40)).unwrap();
    }
    let model = Arc::new(Scripted::new().respond(|req| {
        match req.messages.iter().filter(|m| m["role"] == "assistant").count() {
            0 => "```criteria\n- t.html exists :: verify: test -f t.html\n```\n```write t.html\n<canvas></canvas>\n```".into(),
            n @ 1..=5 => format!("```read src/m1/f1.js```\n```grep v{n} src```\n```bash\nls src | wc -l\n```"),
            _ => "```test test -f t.html```\n```done\nmade t.html\n```".into(),
        }
    }));
    let cfg = RuntimeConfig { store: outer.join("store"), checkpoints: Some(outer.join("cp")), ..RuntimeConfig::default() };
    let rt = Runtime::new(model.clone(), cfg, Events(None));
    let t = Instant::now();
    let r = rt.run(Job::new("make t.html", &dir));
    let total = t.elapsed();
    let calls = model.seen.lock().unwrap().len() as u32;
    println!("{} in {total:?}: {calls} turns, {:?} of harness per turn",
             if matches!(r.outcome, chuck_runtime::Outcome::Done { .. }) { "done" } else { "NOT done" }, total / calls.max(1));
    let _ = std::fs::remove_dir_all(&outer);
}
