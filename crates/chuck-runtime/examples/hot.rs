//! The hot paths, timed one by one on a realistic project (2,000 source
//! files, a node_modules of 6,000, a .git), and a whole job with a big file
//! streamed in small pieces. Development only:
//!
//!   cargo run --release -p chuck-runtime --example hot

use chuck_model::Scripted;
use chuck_runtime::{Events, Job, Runtime, RuntimeConfig};
use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, Instant};

fn time<T>(name: &str, runs: u32, mut f: impl FnMut() -> T) -> T {
    let mut out = None;
    let t = Instant::now();
    for _ in 0..runs {
        out = Some(f());
    }
    let each = t.elapsed() / runs;
    println!("{name:<44} {:>10.2?}", each);
    out.unwrap()
}

fn project(dir: &Path) {
    for i in 0..2000 {
        let d = dir.join(format!("src/m{}/sub{}", i % 40, i % 7));
        std::fs::create_dir_all(&d).unwrap();
        std::fs::write(d.join(format!("f{i}.js")), format!("export const v{i} = {i}; // a line of code\n").repeat(60))
            .unwrap();
    }
    for i in 0..6000 {
        let d = dir.join(format!("node_modules/pkg{}/lib", i % 300));
        std::fs::create_dir_all(&d).unwrap();
        std::fs::write(d.join(format!("x{i}.js")), "module.exports = 1;\n").unwrap();
    }
    std::fs::create_dir_all(dir.join(".git/objects")).unwrap();
    std::fs::write(dir.join(".git/HEAD"), "ref: refs/heads/main\n").unwrap();
}

fn main() {
    let outer = std::env::temp_dir().join(format!("chuck-hot-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&outer);
    let dir = outer.join("proj");
    project(&dir);

    println!("— per command / per turn —");
    time("fingerprint (before+after every command)", 10, || chuck_tools::fingerprint(&dir));
    time("listing (first message)", 10, || chuck_tools::listing(&dir, 120));
    let _ = time("grep across the project", 5, || chuck_tools::grep(&dir, "v1999", "", 80));
    let _ = time("glob **/*.js", 5, || chuck_tools::glob(&dir, "**/*.js", 200));
    println!("— per job —");
    let snap = time("snapshot of the folder (job start)", 1, || {
        chuck_tools::Snapshot::take(&dir, &outer.join("snap")).unwrap()
    });
    std::fs::write(dir.join("src/m1/sub1/f1.js"), "changed\n").unwrap();
    time("diff against the snapshot (done)", 3, || snap.diff(400_000).unwrap());
    let sh = chuck_store::Shadow::new(&dir, &outer.join("cp")).unwrap();
    time("first checkpoint", 1, || sh.snapshot("first").unwrap());
    std::fs::write(dir.join("src/m2/sub2/f2.js"), "changed too\n").unwrap();
    time("next checkpoint (one file changed)", 3, || sh.snapshot("next").unwrap());

    println!("— per reply —");
    let body: String = (0..3000).map(|i| format!("  const row{i} = Array(10).fill({i}); // row {i}\n")).collect();
    let reply = format!("Here it is.\n```criteria\n- ok :: verify: test -f g.js\n```\n```write g.js\n{body}```\n\
                         ```test test -f g.js```\n```done\nmade g.js\n```");
    println!("(a reply of {} KB)", reply.len() / 1024);
    let tags = chuck_runtime::exec::LEAD_TAGS;
    time("normalize_calls", 20, || chuck_tools::normalize_calls(&reply, tags));
    time("file_blocks + tool_blocks", 20, || (chuck_tools::file_blocks(&reply).len(), chuck_tools::tool_blocks(&reply, tags).len()));
    time("prose", 20, || chuck_tools::prose(&reply, tags));
    time("runaway_at (every 512 bytes while streaming)", 200, || chuck_runtime::agent::runaway_at(&reply));

    println!("— a whole job —");
    // the big file streamed in 8-character pieces, as a model streams it
    let r2 = reply.clone();
    let model = Arc::new(Scripted::new().respond(move |req| {
        if req.messages.iter().any(|m| m["role"] == "assistant") { "ok".into() } else { r2.clone() }
    }).chunk(8));
    let cfg = RuntimeConfig { store: outer.join("store"), checkpoints: Some(outer.join("cp2")), ..RuntimeConfig::default() };
    let (tx, rx) = std::sync::mpsc::channel();
    let drain = std::thread::spawn(move || rx.iter().count());
    let rt = Runtime::new(model, cfg, Events(Some(tx)));
    let t = Instant::now();
    let r = rt.run(Job::new("make g.js", &dir));
    let took = t.elapsed();
    drop(rt);
    let events = drain.join().unwrap();
    println!("{:<44} {:>10.2?}  ({} events, {})", "job: one 130 KB reply streamed in 8-byte pieces", took, events,
             if matches!(r.outcome, chuck_runtime::Outcome::Done { .. }) { "done" } else { "NOT done" });
    let _ = Duration::ZERO;
    let _ = std::fs::remove_dir_all(&outer);
}
