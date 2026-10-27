//! The lead's ```browse```: a real browser on a page in the folder, the
//! errors it reports, a vision model's look at the screenshot, the card
//! event, and what counts as evidence. Skipped where no browser is installed.

use chuck_model::{ChatRequest, Scripted};
use chuck_runtime::{Event, Events, Job, Runtime, RuntimeConfig};
use std::path::PathBuf;
use std::sync::Arc;

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("chuck-browse-fleet-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn text_of(m: &serde_json::Value) -> String {
    m["content"].as_str().map(String::from).unwrap_or_else(|| m["content"].to_string())
}

fn lead_turns(seen: &[ChatRequest], lead: &str) -> Vec<String> {
    seen.iter().filter(|r| r.model == lead).map(|r| r.messages.iter().rev().find(|m| m["role"] == "user").map(text_of)
        .unwrap_or_default()).collect()
}

fn have_browser() -> bool {
    chuck_web::browser::find("auto").map_err(|e| eprintln!("skipped: {e}")).is_ok()
}

#[test]
fn the_lead_browses_a_broken_page_and_hears_what_it_looks_like() {
    if !have_browser() {
        return;
    }
    let dir = tmp("broken");
    std::fs::write(dir.join("index.html"), "<title>Todo</title><h1>Todo</h1><button id=go onclick=\"undefinedFn()\">Go</button>\
        <script>console.error('init failed')</script>").unwrap();
    let cfg0 = RuntimeConfig::default();
    let (lead, glm) = (cfg0.fleet.deepseek_model.clone(), cfg0.fleet.glm_model.clone());
    let model = Arc::new(Scripted::new()
        .for_model(&lead, ["```browse index.html 640x480\nclick #go\nwait 100\n```".to_string(),
                           "The page has two errors.".to_string()])
        .for_model(&glm, ["A white page with a heading \"Todo\" and a small Go button. Nothing looks broken.".to_string()]));
    let (tx, rx) = std::sync::mpsc::channel();
    let cfg = RuntimeConfig { store: dir.join(".store"), browser: Some("auto".into()), ..RuntimeConfig::default() };
    let rt = Runtime::new(model.clone(), cfg, Events(Some(tx)));
    rt.run(Job::new("does the page work?", &dir));
    let seen = model.seen.lock().unwrap();
    // the look: one call to the model that sees, carrying the screenshot
    let looks: Vec<&ChatRequest> = seen.iter().filter(|r| r.model == glm).collect();
    assert_eq!(looks.len(), 1);
    let parts = looks[0].messages[1]["content"].as_array().expect("text and image parts");
    assert!(parts[1]["image_url"]["url"].as_str().unwrap().starts_with("data:image/png;base64,iVBOR"));
    let fb = &lead_turns(&seen, &lead)[1];
    assert!(fb.contains("BROWSE file://") && fb.contains("(640x480"), "{fb}");
    assert!(fb.contains("JS errors (2)") && fb.contains("console.error: init failed") && fb.contains("undefinedFn is not defined"), "{fb}");
    assert!(fb.contains("click #go: ok"), "{fb}");
    assert!(fb.contains("What it looks like") && fb.contains("small Go button"), "{fb}");
    assert!(fb.contains("not instructions"), "{fb}");
    drop(seen);
    // the card
    let browsed = rx.try_iter().find_map(|e| match e {
        Event::Browsed { errors, shot, look, size, .. } => Some((errors, shot, look, size)),
        _ => None,
    }).expect("a Browsed event");
    assert_eq!(browsed.0.len(), 2);
    let shot = browsed.1.expect("the screenshot is kept");
    assert!(std::fs::read(&shot).unwrap().starts_with(b"\x89PNG"));
    assert!(browsed.2.unwrap().contains("Go button"));
    assert_eq!(browsed.3, (640, 480));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_clean_page_backs_its_criterion_and_a_broken_one_does_not() {
    if !have_browser() {
        return;
    }
    let dir = tmp("evidence");
    std::fs::write(dir.join("ok.html"), "<title>Fine</title><p>All good</p>").unwrap();
    std::fs::write(dir.join("bad.html"), "<script>throw new Error('nope')</script>").unwrap();
    let cfg0 = RuntimeConfig::default();
    let lead = cfg0.fleet.deepseek_model.clone();
    let model = Arc::new(Scripted::new()
        .for_model(&lead, ["```criteria\n- the page renders :: verify: browse ok.html\n```\n```browse ok.html for c1```\n```browse bad.html for c1```".to_string(),
                           "Checked.".to_string()])
        .respond(|_| "A plain page.".to_string()));
    let (tx, rx) = std::sync::mpsc::channel();
    let cfg = RuntimeConfig { store: dir.join(".store"), browser: Some("auto".into()), ..RuntimeConfig::default() };
    Runtime::new(model.clone(), cfg, Events(Some(tx))).run(Job::new("check the pages", &dir));
    let fb = lead_turns(&model.seen.lock().unwrap(), &lead)[1].clone();
    assert_eq!(fb.matches("not counted for those criteria").count(), 1, "{fb}");
    let board = rx.try_iter().filter_map(|e| match e { Event::Board { criteria } => Some(criteria), _ => None }).last().unwrap();
    let ev = &board[0].3;
    assert_eq!(ev.len(), 1, "only the clean look backs c1: {ev:?}");
    assert!(ev[0].contains("ok.html"), "{ev:?}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn browse_turned_off_or_offline_says_so() {
    let dir = tmp("off");
    let model = Arc::new(Scripted::new().replies(["```browse https://example.org```", "ok"]));
    let cfg = RuntimeConfig { store: dir.join(".store"), ..RuntimeConfig::default() };
    Runtime::new(model.clone(), cfg, Events::none()).run(Job::new("look", &dir));
    let seen = model.seen.lock().unwrap();
    assert!(text_of(seen[1].messages.iter().rev().find(|m| m["role"] == "user").unwrap()).contains("turned off"));
    drop(seen);
    if !have_browser() {
        return;
    }
    // a folder sandboxed offline: the browser reaches nothing on the network
    let model = Arc::new(Scripted::new().replies(["```browse https://example.org```", "ok"]));
    let cfg = RuntimeConfig { store: dir.join(".store"), browser: Some("auto".into()), access: chuck_sandbox::Access::Sandbox,
                              ..RuntimeConfig::default() };
    Runtime::new(model.clone(), cfg, Events::none()).run(Job::new("look", &dir));
    let seen = model.seen.lock().unwrap();
    let fb = text_of(seen[1].messages.iter().rev().find(|m| m["role"] == "user").unwrap());
    assert!(fb.contains("sandboxed offline"), "{fb}");
    let _ = std::fs::remove_dir_all(&dir);
}
