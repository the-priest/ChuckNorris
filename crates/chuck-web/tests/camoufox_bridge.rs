//! Camoufox through its bridge, against a stand-in for the camoufox package
//! (tests/fake_camoufox) that behaves the way Playwright does: every request
//! is asked of Chuck's judge, a restricted folder is served by Chuck's own
//! file server (fetched for real), errors and responses come back as
//! events, and the "browser" runs in a session of its own, so ending it
//! takes more than killing the bridge.

use chuck_web::browser::{self, Engine, Files, Net, Options};
use chuck_web::camoufox::Install;
use chuck_web::Proxy;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;
use std::sync::Arc;
use std::time::{Duration, Instant};

struct Fake {
    dir: PathBuf,
    install: Install,
}

impl Fake {
    /// A Python that imports the stand-in, with `site` as the web.
    fn new(name: &str, site: Value) -> Option<Fake> {
        let py = std::env::split_paths(&std::env::var_os("PATH")?).map(|d| d.join("python3")).find(|p| p.is_file())?;
        let dir = std::env::temp_dir().join(format!("chuck-fox-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("site.json"), site.to_string()).unwrap();
        let pkg = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fake_camoufox");
        let wrapper = dir.join("python");
        std::fs::write(&wrapper, format!("#!/bin/sh\nexport PYTHONPATH='{}' FAKE_SITE_FILE='{}' FAKE_LOG='{}'\nexec '{}' \"$@\"\n",
                                         pkg.display(), dir.join("site.json").display(), dir.join("log").display(),
                                         py.display())).unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&wrapper, std::fs::Permissions::from_mode(0o755)).unwrap();
        Some(Fake { install: Install { python: wrapper, exe: None }, dir })
    }

    fn options(&self) -> Options {
        let mut o = Options::new(Engine::Camoufox(self.install.clone()));
        o.timeout = Duration::from_secs(20);
        o
    }

    fn log(&self) -> Vec<Value> {
        std::fs::read_to_string(self.dir.join("log")).unwrap_or_default().lines()
            .filter_map(|l| serde_json::from_str(l).ok()).collect()
    }
}

impl Drop for Fake {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn alive(pid: i64) -> bool {
    // a zombie isn't running: look at its state
    std::fs::read_to_string(format!("/proc/{pid}/stat")).is_ok_and(|s| {
        s.rsplit(')').next().and_then(|r| r.split_whitespace().next()).is_some_and(|st| st != "Z")
    })
}

#[test]
fn a_page_read_for_research_reaches_only_the_public_internet() {
    let page = "http://93.184.216.34/story";
    let Some(f) = Fake::new("public", json!({"remote": {page: "<title>Story</title><p>A two-year-old died near the farm.</p>\
        <script src=\"http://127.0.0.1:1/track.js\"></script><script src=\"http://10.0.0.1/x.js\"></script>\
        <script src=\"https://93.184.216.34/ok.js\"></script>"}})) else { return };
    let mut o = f.options();
    o.net = Net::Public(Proxy::None);
    o.files = Files { root: Some(PathBuf::from("/nonexistent")), hidden: Vec::new() };
    let (r, blocked) = browser::read_page(page, &o).unwrap();
    assert!(!blocked);
    assert_eq!((r.engine.as_str(), r.title.as_str()), ("Camoufox", "Story"));
    assert!(r.text.contains("A two-year-old died near the farm."), "{}", r.text);
    assert_eq!(r.refused, vec!["http://127.0.0.1:1/track.js".to_string(), "http://10.0.0.1/x.js".to_string()]);
    assert!(r.failed.iter().any(|x| x.starts_with("http://10.0.0.1/x.js")), "{:?}", r.failed);
    assert_eq!(r.status, Some(200));
    let log = f.log();
    let launch = &log.iter().find(|l| l.get("launch").is_some()).unwrap()["launch"];
    assert_eq!(launch["headless"], true);
    assert_eq!(launch["block_webrtc"], true, "WebRTC can't leak the machine's addresses");
    assert_eq!(launch["firefox_user_prefs"]["network.dns.disablePrefetch"], true);
    let ctx = &log.iter().find(|l| l.get("context").is_some()).unwrap()["context"];
    assert_eq!((ctx["accept_downloads"].as_bool(), ctx["service_workers"].as_str()), (Some(false), Some("block")));
    assert!(ctx.get("viewport").is_none(), "a page read keeps the window its fingerprint says");
}

#[test]
fn a_research_page_that_is_a_private_address_is_refused_outright() {
    let Some(f) = Fake::new("refused", json!({})) else { return };
    let mut o = f.options();
    o.net = Net::Public(Proxy::None);
    let e = browser::browse("http://127.0.0.1:9/admin", &[], &o).unwrap_err();
    assert!(e.contains("may not be reached") && e.contains("public internet"), "{e}");
}

#[test]
fn a_restricted_folder_is_served_with_nothing_beside_it() {
    let Some(f) = Fake::new("folder", json!({})) else { return };
    let site = f.dir.join("site");
    std::fs::create_dir_all(&site).unwrap();
    std::fs::write(site.join("index.html"), "<title>Todo</title><h1 id=\"add\">Todo</h1><script src=\"app.js\"></script>\
        <script src=\"../outside.js\"></script><script src=\"file:///etc/passwd\"></script>\
        <!-- error: addTask is not defined --><!-- console: init failed -->").unwrap();
    std::fs::write(site.join("app.js"), "x").unwrap();
    std::fs::write(f.dir.join("outside.js"), "secret").unwrap();
    let mut o = f.options();
    o.base = Some(site.clone());
    o.files = Files { root: Some(site.clone()), hidden: Vec::new() };
    let actions: Vec<String> = ["click #add", "type Buy milk", "press Enter", "size 800x600", "goto ../outside.js", "wait soon"]
        .iter().map(|s| s.to_string()).collect();
    let r = browser::browse(".", &actions, &o).unwrap();
    let real = std::fs::canonicalize(&site).unwrap();
    assert_eq!(r.url, format!("file://{}/index.html", real.display()));
    assert_eq!(r.final_url, r.url, "the server's address never shows");
    assert_eq!(r.title, "Todo");
    assert!(r.errors.iter().any(|e| e.starts_with("Uncaught Error: addTask is not defined (index.html:3)")), "{:?}", r.errors);
    assert!(r.errors.iter().any(|e| e == "console.error: init failed (index.html:7)"), "{:?}", r.errors);
    // beside the folder: not served; outside the web: never loaded
    assert!(r.failed.iter().any(|x| x.ends_with("outside.js: HTTP 404")), "{:?}", r.failed);
    assert!(r.failed.iter().any(|x| x.starts_with("file:///etc/passwd")), "{:?}", r.failed);
    assert!(r.steps[4].contains("FAILED") && r.steps[4].contains("outside this folder"), "{:?}", r.steps);
    assert!(r.steps[5].contains("FAILED: `soon` isn't a number"), "{:?}", r.steps);
    assert_eq!(r.size, (800, 600));
    assert!(r.shot.as_deref().is_some_and(|p| p.starts_with(b"\x89PNG")));
    let did = f.log().into_iter().find_map(|l| l.get("did").cloned()).unwrap();
    for want in ["click #add", "type Buy milk", "press Enter", "size 800x600"] {
        assert!(did.as_array().unwrap().iter().any(|d| d == want), "{want}: {did}");
    }
}

#[test]
fn an_offline_folder_reaches_its_own_files_and_nothing_else() {
    let Some(f) = Fake::new("offline", json!({})) else { return };
    let site = f.dir.join("site");
    std::fs::create_dir_all(&site).unwrap();
    std::fs::write(site.join("index.html"), "<title>Off</title><script src=\"app.js\"></script>\
        <script src=\"https://93.184.216.34/cdn.js\"></script>").unwrap();
    std::fs::write(site.join("app.js"), "x").unwrap();
    let mut o = f.options();
    o.base = Some(site.clone());
    o.net = Net::Offline;
    o.files = Files { root: Some(site.clone()), hidden: Vec::new() };
    let r = browser::browse("index.html", &[], &o).unwrap();
    assert_eq!(r.refused, vec!["https://93.184.216.34/cdn.js".to_string()]);
    assert!(r.failed.iter().any(|x| x.starts_with("https://93.184.216.34/cdn.js")), "it never loaded: {:?}", r.failed);
    assert!(!r.failed.iter().any(|x| x.contains("app.js")), "its own file loads: {:?}", r.failed);
}

#[test]
fn stopping_ends_the_bridge_and_the_browser_it_started() {
    let Some(f) = Fake::new("stop", json!({"hang": true, "leave_browser": true})) else { return };
    let mut o = f.options();
    let stop = Arc::new(AtomicBool::new(false));
    o.stop = Some(stop.clone());
    let s2 = stop.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(1500));
        s2.store(true, std::sync::atomic::Ordering::SeqCst);
    });
    let t0 = Instant::now();
    assert_eq!(browser::browse("https://93.184.216.34/", &[], &o).unwrap_err(), "stopped");
    assert!(t0.elapsed() < Duration::from_secs(5));
    let pid = f.log().iter().find_map(|l| l.get("browser_pid").and_then(Value::as_i64)).expect("the browser started");
    let end = Instant::now() + Duration::from_secs(3);
    while alive(pid) && Instant::now() < end {
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(!alive(pid), "the browser, in a session of its own, ended too");
}

#[test]
fn a_camoufox_that_was_never_downloaded_says_how_to_get_it() {
    let Some(f) = Fake::new("missing", json!({"not_installed": true})) else { return };
    let e = browser::browse("https://93.184.216.34/", &[], &f.options()).unwrap_err();
    assert!(e.starts_with("Camoufox: ") && e.contains("camoufox fetch"), "{e}");
}

#[test]
fn without_the_geoip_extra_it_still_launches_privately() {
    let Some(f) = Fake::new("geoip", json!({"no_geoip": true})) else { return };
    let mut o = f.options();
    o.proxy = Some(Proxy::Url("socks5://127.0.0.1:9050".into()));
    browser::browse("https://93.184.216.34/", &[], &o).unwrap();
    let launch = f.log().into_iter().find_map(|l| l.get("launch").cloned()).unwrap();
    assert_eq!(launch["proxy"]["server"], "socks5://127.0.0.1:9050");
    assert_eq!(launch["block_webrtc"], true);
    assert!(launch.get("geoip").is_none());
}
