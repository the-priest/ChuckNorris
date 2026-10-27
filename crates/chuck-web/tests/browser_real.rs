//! The browser for real: the user's Chromium, a page served here, the
//! errors a broken page makes, actions, a bot wall that clears itself, a
//! page that is empty until its scripts run, and what the page may reach.
//! Skipped (with a note) where no Chromium-family browser is installed.

use chuck_web::browser::{self, Files, Net, Options};
use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::time::Duration;

fn serve() -> u16 {
    let l = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = l.local_addr().unwrap().port();
    std::thread::spawn(move || {
        for s in l.incoming().flatten() {
            std::thread::spawn(move || {
                let mut r = BufReader::new(s.try_clone().unwrap());
                let mut line = String::new();
                if r.read_line(&mut line).is_err() {
                    return;
                }
                loop {
                    let mut h = String::new();
                    if r.read_line(&mut h).is_err() || h.trim().is_empty() {
                        break;
                    }
                }
                let path = line.split_whitespace().nth(1).unwrap_or("/").to_string();
                let (status, ctype, body) = match path.as_str() {
                    "/" => ("200 OK", "text/html", r#"<!doctype html><title>Broken app</title>
                        <body><h1>Counter</h1><button id="b" onclick="document.getElementById('n').textContent=+document.getElementById('n').textContent+1">add</button>
                        <span id="n">0</span><input id="i">
                        <script src="/missing.js"></script>
                        <script>console.error("boom", 42); console.warn("careful"); console.log("hello log");
                        document.getElementById('i').addEventListener('keydown', e => { if (e.key === 'Enter') document.title = 'entered ' + e.target.value; });
                        setTimeout(() => { null.x }, 10);</script></body>"#.to_string()),
                    "/spa" => ("200 OK", "text/html", r#"<!doctype html><title>App</title><div id="root"></div>
                        <script>document.getElementById('root').innerHTML = '<main><p>Rendered by script: the answer is forty-two.</p></main>';</script>"#.to_string()),
                    "/wall" => ("200 OK", "text/html", r#"<!doctype html><title>Just a moment...</title><body>Checking your browser before accessing.
                        <script>setTimeout(() => { document.title = 'Real page'; document.body.innerHTML = '<article>The wall cleared: forty-two.</article>'; }, 1500);</script></body>"#.to_string()),
                    _ => ("404 Not Found", "text/plain", "nope".to_string()),
                };
                let mut s = s;
                let _ = write!(s, "HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
            });
        }
    });
    port
}

/// One browser test at a time: they count browser processes.
static ONE: std::sync::Mutex<()> = std::sync::Mutex::new(());

fn exe() -> Option<std::path::PathBuf> {
    match browser::find("auto") {
        Ok(p) => Some(p),
        Err(e) => {
            eprintln!("skipped: {e}");
            None
        }
    }
}

#[test]
fn a_broken_page_reports_its_errors_and_acts() {
    let _one = ONE.lock().unwrap_or_else(|e| e.into_inner());
    let Some(exe) = exe() else { return };
    let port = serve();
    let mut o = Options::new(exe);
    o.size = (900, 600);
    let actions: Vec<String> = ["click #b", "click #b", "click #i", "type hi", "press Enter", "eval document.getElementById('n').textContent",
        "click #nothing-here", "dance"].iter().map(|s| s.to_string()).collect();
    let r = browser::browse(&format!("localhost:{port}/"), &actions, &o).unwrap();
    let rep = browser::report(&r, 1500);
    eprintln!("{rep}");
    assert!(r.errors.iter().any(|e| e.contains("console.error: boom 42")), "{rep}");
    assert!(r.errors.iter().any(|e| e.starts_with("Uncaught TypeError")), "{rep}");
    assert!(r.failed.iter().any(|f| f.contains("/missing.js") && f.contains("404")), "{rep}");
    assert!(r.warnings.iter().any(|w| w.contains("careful")), "{rep}");
    assert!(r.logs.iter().any(|l| l == "hello log"), "{rep}");
    assert!(r.steps[5].ends_with("→ \"2\""), "{rep}");
    assert!(r.steps[6].contains("FAILED: no element matches"), "{rep}");
    assert!(r.steps[7].contains("unknown action"), "{rep}");
    assert_eq!(r.title, "entered hi", "{rep}");
    let png = r.shot.as_ref().expect("a screenshot");
    assert!(png.starts_with(b"\x89PNG"));
    // the width is in the PNG header
    assert_eq!(u32::from_be_bytes(png[16..20].try_into().unwrap()), 900);
    assert!(rep.contains("BROWSE http://localhost") && rep.contains("(900x600"), "{rep}");
}

#[test]
fn script_pages_and_bot_walls_are_read() {
    let _one = ONE.lock().unwrap_or_else(|e| e.into_inner());
    let Some(exe) = exe() else { return };
    let port = serve();
    let o = Options::new(exe);
    let (r, blocked) = browser::read_page(&format!("http://127.0.0.1:{port}/spa"), &o).unwrap();
    assert!(!blocked && r.text.contains("forty-two"), "{}", r.text);
    let t = std::time::Instant::now();
    let (r, blocked) = browser::read_page(&format!("http://127.0.0.1:{port}/wall"), &o).unwrap();
    assert!(!blocked && r.text == "The wall cleared: forty-two." && r.title == "Real page", "{r:?}");
    assert!(t.elapsed() < Duration::from_secs(15));
}

#[test]
fn what_the_page_may_reach() {
    let _one = ONE.lock().unwrap_or_else(|e| e.into_inner());
    let Some(exe) = exe() else { return };
    let port = serve();
    // research: never a local address, not even the first one
    let mut o = Options::new(exe.clone());
    o.net = Net::Public(chuck_web::Proxy::Env);
    let e = browser::browse(&format!("http://127.0.0.1:{port}/"), &[], &o).unwrap_err();
    assert!(e.contains("may not be reached"), "{e}");
    // an offline folder: its own page loads, nothing on the network does
    let d = std::env::temp_dir().join(format!("chuck-browse-off-{}", std::process::id()));
    std::fs::create_dir_all(&d).unwrap();
    std::fs::write(d.join("index.html"), format!(r#"<title>Offline</title><img src="http://127.0.0.1:{port}/px.png">
        <script>fetch("http://127.0.0.1:{port}/x").catch(e => console.log("fetch refused"))</script>"#)).unwrap();
    let mut o = Options::new(exe.clone());
    o.net = Net::Offline;
    o.base = Some(d.clone());
    o.files = Files { root: Some(d.clone()), hidden: vec![] };
    let r = browser::browse(".", &["wait 300".into()], &o).unwrap();
    assert_eq!(r.title, "Offline");
    assert!(r.refused.iter().any(|u| u.contains("/px.png")) && r.refused.iter().any(|u| u.ends_with("/x")), "{r:?}");
    assert!(r.logs.iter().any(|l| l == "fetch refused"), "{r:?}");
    // and a restricted folder's page can't open a file outside it
    let e = browser::browse("/etc/hostname", &[], &o).unwrap_err();
    assert!(e.contains("outside this folder"), "{e}");
    let r = browser::browse(".", &["goto /etc/passwd".into()], &o).unwrap();
    assert!(r.steps[0].contains("FAILED") && r.steps[0].contains("outside"), "{:?}", r.steps);
    let _ = std::fs::remove_dir_all(&d);
}

#[test]
fn nothing_is_left_running() {
    let _one = ONE.lock().unwrap_or_else(|e| e.into_inner());
    let Some(exe) = exe() else { return };
    let mut o = Options::new(exe);
    o.screenshot = false;
    let before = chrome_count();
    for _ in 0..3 {
        browser::browse("about:blank", &[], &o).unwrap();
    }
    std::thread::sleep(Duration::from_millis(300));
    assert!(chrome_count() <= before, "browsers left behind");
    // a stop flag ends a long wait at once
    let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    o.stop = Some(stop.clone());
    let s2 = stop.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(1500));
        s2.store(true, std::sync::atomic::Ordering::SeqCst);
    });
    let t = std::time::Instant::now();
    let e = browser::browse("about:blank", &["wait 10000".into()], &o).unwrap_err();
    assert_eq!(e, "stopped");
    assert!(t.elapsed() < Duration::from_secs(5));
    std::thread::sleep(Duration::from_millis(300));
    assert!(chrome_count() <= before, "browsers left behind after a stop");
}

fn chrome_count() -> usize {
    std::fs::read_dir("/proc").unwrap().flatten().filter(|e| {
        std::fs::read_to_string(e.path().join("cmdline")).is_ok_and(|c| c.contains("chuck-browse-"))
    }).count()
}
