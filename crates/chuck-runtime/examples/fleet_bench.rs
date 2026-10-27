//! What the fleet itself costs, with the model taken out of the picture: a
//! scripted job where the Lead fans out N tasks to N workers (each in its own
//! copy, on its own thread), reviews, merges and verifies them, and finishes
//! through the gate and the critic. Prints wall time and peak memory.
//!
//!   cargo run --release -p chuck-runtime --example fleet_bench -- [workers 0..8]

use chuck_model::{ChatRequest, Scripted};
use chuck_runtime::{Events, Job, Outcome, Runtime, RuntimeConfig};
use std::sync::Arc;
use std::time::Instant;

fn text(m: &serde_json::Value) -> String {
    m["content"].as_str().unwrap_or("").to_string()
}

fn peak_kb() -> u64 {
    std::fs::read_to_string("/proc/self/status").ok().and_then(|s| s.lines().find(|l| l.starts_with("VmHWM:"))
        .and_then(|l| l.split_whitespace().nth(1)?.parse().ok())).unwrap_or(0)
}

fn main() {
    let n: usize = std::env::args().nth(1).and_then(|a| a.parse().ok()).unwrap_or(8).min(8);
    let dir = std::env::temp_dir().join(format!("chuck-bench-{}", std::process::id()));
    let proj = dir.join("proj");
    std::fs::create_dir_all(proj.join("src")).unwrap();
    for i in 0..200 {
        std::fs::write(proj.join(format!("src/m{i}.rs")), format!("pub fn f{i}() -> u32 {{ {i} }}\n").repeat(20)).unwrap();
    }
    let model = Arc::new(Scripted::new().respond(move |req: &ChatRequest| {
        let sys = text(&req.messages[0]);
        let me = req.messages.get(1).map(text).unwrap_or_default();
        let last = req.messages.iter().rev().find(|m| m["role"] == "user").map(text).unwrap_or_default();
        if sys.starts_with("You are an independent reviewer") {
            return "ACCEPT".into();
        }
        if me.starts_with("You are ds-") {
            if req.messages.len() > 2 {
                return "ok".into();
            }
            let t = text(&req.messages[1]);
            let id = t.split("TASK ").nth(1).and_then(|r| r.split_whitespace().next()).unwrap_or("t0")
                .trim_end_matches(':').to_string();
            let body: String = (0..30).map(|i| format!("line {i} of {id}\n")).collect();
            return format!("```write out/{id}.txt\n{body}```\n```test test -d out```\n```submit\n{id} written\n```");
        }
        if req.messages.len() == 2 {
            let mut s = String::from("```criteria\n- every part exists :: verify: test -d out\n```\n```plan\n- fan out\n```\n");
            for i in 0..n {
                s.push_str(&format!("```assign worker\ntitle: part {i}\ncriteria:\n- the part exists :: verify: test -d out\nbrief: write it\n```\n"));
            }
            if n == 0 {
                s.push_str("```bash\nmkdir -p out && seq 1 30 > out/solo.txt\n```\n```test test -d out```\n```done\nsolo part written (test -d out).\n```");
            }
            return s;
        }
        let mut out = String::new();
        for i in 1..=n {
            if last.contains(&format!("SUBMISSION t{i} ")) {
                out.push_str(&format!("```review t{i} accept\nok\n```\n"));
            }
        }
        if !out.is_empty() {
            return out;
        }
        let merged = req.messages.iter().filter(|m| m["role"] == "user").map(text)
            .map(|t| t.matches(" accepted, re-verified").count()).sum::<usize>();
        if merged >= n {
            return "```test test -d out```\n```justify proportion\nsmall parts\n```\n```done\nall parts written (test -d out).\n```".into();
        }
        "Waiting.".into()
    }));
    let cfg = RuntimeConfig { store: dir.join("store"), bwrap: false, ..RuntimeConfig::default() };
    let before = peak_kb();
    let t0 = Instant::now();
    let rt = Runtime::new(model.clone(), cfg, Events::none());
    let r = rt.run(Job::new("write the parts", &proj));
    let ms = t0.elapsed().as_millis();
    assert!(matches!(r.outcome, Outcome::Done { .. }), "{:?}", r.outcome);
    println!("workers {n}: {ms} ms wall, {} model calls, peak memory {} MB (start {} MB)",
             model.calls(), peak_kb() / 1024, before / 1024);
    let _ = std::fs::remove_dir_all(&dir);
}
