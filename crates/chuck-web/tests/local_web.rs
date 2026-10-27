//! Search and fetch against a real HTTP server on loopback, so every path
//! (redirects, walls, script shells, refusals, size caps, engines that fail
//! or stall) runs over a real socket.

use chuck_web::{Config, Proxy, Web, Why};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

struct Server {
    base: String,
    hits: Arc<AtomicUsize>,
}

fn reply(status: &str, ctype: &str, extra: &str, body: &[u8]) -> Vec<u8> {
    let mut r = format!("HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {}\r\n{extra}Connection: close\r\n\r\n",
                        body.len()).into_bytes();
    r.extend_from_slice(body);
    r
}

fn serve() -> Server {
    let l = TcpListener::bind("127.0.0.1:0").unwrap();
    let base = format!("http://{}", l.local_addr().unwrap());
    let hits = Arc::new(AtomicUsize::new(0));
    let h = hits.clone();
    std::thread::spawn(move || {
        for s in l.incoming() {
            let Ok(mut s) = s else { continue };
            let h = h.clone();
            std::thread::spawn(move || {
                let mut r = BufReader::new(s.try_clone().unwrap());
                let mut line = String::new();
                r.read_line(&mut line).unwrap();
                let mut len = 0;
                loop {
                    let mut hd = String::new();
                    r.read_line(&mut hd).unwrap();
                    if hd.trim().is_empty() {
                        break;
                    }
                    if let Some(v) = hd.to_ascii_lowercase().strip_prefix("content-length:") {
                        len = v.trim().parse().unwrap_or(0);
                    }
                }
                let mut body = vec![0; len];
                r.read_exact(&mut body).unwrap();
                let path = line.split_whitespace().nth(1).unwrap_or("/").to_string();
                h.fetch_add(1, Ordering::SeqCst);
                let story = "<p>".to_string() + &"Rust ownership makes data races a compile error. ".repeat(20) + "</p>";
                let out = match path.split('?').next().unwrap() {
                    "/article" => reply("200 OK", "text/html; charset=utf-8", "", format!(
                        "<html><head><title>Ownership</title></head><body><nav>menu</nav><article>{story}</article></body></html>").as_bytes()),
                    "/latin1" => reply("200 OK", "text/html; charset=iso-8859-1", "", b"<p>caf\xe9 cr\xe8me</p>"),
                    "/plain" => reply("200 OK", "text/plain", "", b"just <text>, not markup"),
                    "/redir" => reply("302 Found", "text/html", "Location: /article\r\n", b""),
                    "/loop" => reply("302 Found", "text/html", "Location: /loop\r\n", b""),
                    "/wall" => reply("200 OK", "text/html", "", b"<html><head><title>Just a moment...</title></head>\
                        <body><div id=challenge-platform></div></body></html>"),
                    "/spa" => reply("200 OK", "text/html", "", b"<html><body><div id=\"root\"></div>\
                        <script src=a.js></script></body></html>"),
                    "/pdf" => reply("200 OK", "application/pdf", "", b"%PDF-1.4"),
                    "/forbidden" => reply("403 Forbidden", "text/html", "", b"no"),
                    "/gone" => reply("404 Not Found", "text/html", "", b"gone"),
                    "/huge" => {
                        let mut b = b"<p>".to_vec();
                        b.extend(std::iter::repeat_n(b'x', 6_000_000));
                        reply("200 OK", "text/html", "", &b)
                    }
                    "/search" => {
                        let q = path.split("q=").nth(1).unwrap_or("");
                        let rows: Vec<String> = (0..6).map(|i| format!(
                            r#"{{"url":"https://site{}.org/{q}/{i}","title":"<b>Result</b> {i}","content":"about {q} &amp; more"}}"#, i % 3)).collect();
                        reply("200 OK", "application/json", "", format!(r#"{{"results":[{}]}}"#, rows.join(",")).as_bytes())
                    }
                    "/empty/search" => reply("200 OK", "application/json", "", br#"{"results":[]}"#),
                    "/slow/search" => {
                        std::thread::sleep(Duration::from_secs(30));
                        reply("200 OK", "application/json", "", br#"{"results":[]}"#)
                    }
                    "/ddg" => {
                        let q = String::from_utf8_lossy(&body).to_string();
                        reply("200 OK", "text/html", "", format!(
                            r#"<div class="result"><a class="result__a" href="https://ddg.example/{q}">From DDG</a>
                               <a class="result__snippet">snippet</a></div>
                               <div class="result"><a class="result__a" href="https://other.example/x">Other</a></div>
                               <div class="result"><a class="result__a" href="https://third.example/y">Third</a></div>"#).as_bytes())
                    }
                    _ => reply("404 Not Found", "text/plain", "", b""),
                };
                let _ = s.write_all(&out);
            });
        }
    });
    Server { base, hits }
}

fn web(cfg: Config) -> Web {
    Web::new(Config { proxy: Proxy::None, allow_private: true, timeout: Duration::from_secs(3), ..cfg })
}

#[test]
fn pages_are_read_as_text_and_failures_say_why() {
    let s = serve();
    let w = web(Config::default());
    let p = w.fetch(&format!("{}/article", s.base)).unwrap();
    assert_eq!(p.title.as_deref(), Some("Ownership"));
    assert!(p.text.starts_with("Rust ownership makes data races"), "{}", p.text);
    assert!(!p.text.contains("menu"));

    let p = w.fetch(&format!("{}/redir", s.base)).unwrap();
    assert!(p.url.ends_with("/article"), "the final address is reported: {}", p.url);
    assert_eq!(w.fetch(&format!("{}/latin1", s.base)).unwrap().text, "café crème");
    assert_eq!(w.fetch(&format!("{}/plain", s.base)).unwrap().text, "just <text>, not markup", "plain text isn't stripped");

    assert_eq!(w.fetch(&format!("{}/wall", s.base)), Err(Why::Blocked(0)));
    assert_eq!(w.fetch(&format!("{}/spa", s.base)), Err(Why::Script));
    assert_eq!(w.fetch(&format!("{}/pdf", s.base)), Err(Why::NotText("application/pdf".into())));
    assert_eq!(w.fetch(&format!("{}/forbidden", s.base)), Err(Why::Blocked(403)));
    assert_eq!(w.fetch(&format!("{}/gone", s.base)), Err(Why::Unreachable("HTTP 404".into())));
    assert!(matches!(w.fetch(&format!("{}/loop", s.base)), Err(Why::Unreachable(e)) if e.contains("redirects")));

    let t0 = Instant::now();
    let p = w.fetch(&format!("{}/huge", s.base)).unwrap();
    assert!(p.text.len() <= 400_000 && p.text.starts_with("xxx"), "capped: {}", p.text.len());
    assert!(t0.elapsed() < Duration::from_secs(3));
}

#[test]
fn a_page_is_read_from_the_network_once() {
    let s = serve();
    let w = web(Config::default());
    let url = format!("{}/article", s.base);
    for _ in 0..5 {
        w.fetch(&url).unwrap();
    }
    assert_eq!(s.hits.load(Ordering::SeqCst), 1, "re-reads come from memory");
}

#[test]
fn the_default_never_reaches_this_machine() {
    let s = serve();
    let w = Web::new(Config { proxy: Proxy::None, ..Config::default() });
    assert!(matches!(w.fetch(&format!("{}/article", s.base)), Err(Why::Refused(_))));
    assert_eq!(s.hits.load(Ordering::SeqCst), 0, "not even one request");
}

#[test]
fn search_takes_the_first_engine_that_answers_and_keeps_sources_diverse() {
    let s = serve();
    // one instance answers nothing, one stalls for 30 s, one answers
    let w = web(Config {
        searx: vec![format!("{}/empty", s.base), format!("{}/slow", s.base), s.base.clone()],
        ddg: vec![],
        ..Config::default()
    });
    let t0 = Instant::now();
    let hits = w.search("borrow checker", 8).unwrap();
    assert!(t0.elapsed() < Duration::from_secs(3), "a stalled engine doesn't hold up the answer: {:?}", t0.elapsed());
    assert_eq!(hits.len(), 6, "three sites, two each: {hits:?}");
    assert_eq!(hits[0].title, "Result 0");
    assert_eq!(hits[0].snippet, "about borrow%20checker & more");
}

#[test]
fn the_users_own_instance_goes_first_and_duckduckgo_is_the_fallback() {
    let s = serve();
    let w = web(Config { searx_own: Some(s.base.clone()), searx: vec![format!("{}/slow", s.base)], ddg: vec![],
                         ..Config::default() });
    let t0 = Instant::now();
    assert_eq!(w.search("x", 4).unwrap().len(), 4);
    assert!(t0.elapsed() < Duration::from_secs(2), "the public ones weren't waited on");

    let w = web(Config { searx: vec![format!("{}/empty", s.base)], ddg: vec![format!("{}/ddg", s.base)],
                         ..Config::default() });
    let hits = w.search("q", 8).unwrap();
    assert_eq!(hits.iter().map(|h| h.title.as_str()).collect::<Vec<_>>(), ["From DDG", "Other", "Third"]);
    assert_eq!(hits[0].url, "https://ddg.example/q=q");

    let w = web(Config { searx: vec![], ddg: vec![format!("{}/gone", s.base)], ..Config::default() });
    assert!(w.search("q", 8).is_err());
    assert!(w.search("   ", 8).is_err());
}
