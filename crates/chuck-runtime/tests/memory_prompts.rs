//! What the agents are told: the user's and the project's standing rules,
//! and what's remembered (only what's relevant); and remember/forget.

use chuck_model::{ChatRequest, Scripted};
use chuck_runtime::{Events, Job, Runtime, RuntimeConfig};
use chuck_store::memory::Memory;
use std::sync::Arc;

fn system(req: &ChatRequest) -> String {
    req.messages[0]["content"].as_str().unwrap_or("").to_string()
}

fn last_user(req: &ChatRequest) -> String {
    req.messages.iter().rev().find(|m| m["role"] == "user").and_then(|m| m["content"].as_str()).unwrap_or("").to_string()
}

#[test]
fn rules_and_memories_reach_the_prompt_and_facts_are_kept() {
    let dir = std::env::temp_dir().join(format!("chuck-memprompt-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(dir.join("proj")).unwrap();
    let proj = dir.join("proj").canonicalize().unwrap();
    std::fs::write(proj.join("AGENTS.md"), "Use tabs. Never touch vendor/.").unwrap();
    let mem = Arc::new(Memory::at(&dir.join("facts.jsonl")));
    mem.remember("User runs CachyOS on a ThinkPad X395", "fact", 1.0, "");
    mem.remember("User's favourite pizza is margherita", "fact", 1.0, "");
    let cfg = || RuntimeConfig { store: dir.join("store"), memory: Some(mem.clone()), ..RuntimeConfig::default() };

    // Chat: the project's rules and the relevant memory, not the irrelevant one
    let model = Arc::new(Scripted::new().replies([
        "```remember User prefers fish as their shell```\n```remember project: the API lives in src/api```\n```forget pizza```",
        "Noted."]));
    Runtime::new(model.clone(), cfg(), Events::none()).chat(Job::new("which distro am I on?", &proj));
    let seen = model.seen.lock().unwrap();
    let sys = system(&seen[0]);
    assert!(sys.contains("PROJECT INSTRUCTIONS (AGENTS.md in the working folder)") && sys.contains("Use tabs."), "{sys}");
    assert!(sys.contains("- User runs CachyOS on a ThinkPad X395") && !sys.contains("pizza"), "only what's relevant");
    let fb = last_user(&seen[1]);
    assert!(fb.contains("[remember: noted]") && fb.contains("[remember: noted (for this folder)]") && fb.contains("[forget: forgot 1 fact(s)]"), "{fb}");
    drop(seen);
    let facts: Vec<(String, String)> = mem.all().iter().map(|f| (f.text().to_string(), f.scope().to_string())).collect();
    assert!(facts.contains(&("the API lives in src/api".into(), proj.display().to_string())), "{facts:?}");
    assert!(facts.iter().any(|(t, s)| t == "User prefers fish as their shell" && s.is_empty()));
    assert!(!facts.iter().any(|(t, _)| t.contains("pizza")));

    // the fleet's lead: the same rules, and the project fact only in its project
    let model = Arc::new(Scripted::new().replies(["Where is the API? It's in src/api."]));
    Runtime::new(model.clone(), cfg(), Events::none()).run(Job::new("where does the api live?", &proj));
    let sys = system(&model.seen.lock().unwrap()[0]);
    assert!(sys.contains("Use tabs.") && sys.contains("the API lives in src/api") && sys.contains("```remember"), "{sys}");
    std::fs::create_dir_all(dir.join("other")).unwrap();
    let model = Arc::new(Scripted::new().replies(["ok"]));
    Runtime::new(model.clone(), cfg(), Events::none()).run(Job::new("where does the api live?", dir.join("other")));
    assert!(!system(&model.seen.lock().unwrap()[0]).contains("src/api"), "a project's facts stay in that project");
    let _ = std::fs::remove_dir_all(&dir);
}
