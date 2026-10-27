//! Pictures and videos against a real HTTP server on loopback: SearXNG's
//! image and video answers, and downloads that must be real pictures.

use chuck_web::{Config, Proxy, Web};
use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::time::Duration;

const PNG: &[u8] = b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR\0\0\0\x01\0\0\0\x01\x08\x06\0\0\0\x1f\x15\xc4\x89\0\0\0\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\0\x05\0\x01\xff\x89\x99=\x1d\0\0\0\0IEND\xaeB`\x82";

fn serve() -> String {
    let l = TcpListener::bind("127.0.0.1:0").unwrap();
    let base = format!("http://{}", l.local_addr().unwrap());
    let b = base.clone();
    std::thread::spawn(move || {
        for s in l.incoming().flatten() {
            let b = b.clone();
            std::thread::spawn(move || {
                let mut r = BufReader::new(s.try_clone().unwrap());
                let mut line = String::new();
                let _ = r.read_line(&mut line);
                loop {
                    let mut h = String::new();
                    if r.read_line(&mut h).is_err() || h.trim().is_empty() {
                        break;
                    }
                }
                let path = line.split_whitespace().nth(1).unwrap_or("/").to_string();
                let (st, ct, body): (&str, &str, Vec<u8>) = if path.starts_with("/search") && path.contains("categories=images") {
                    ("200 OK", "application/json", format!(r#"{{"results":[
                        {{"url":"{b}/page1","title":"Red panda","img_src":"{b}/img.png","thumbnail_src":"{b}/img.png"}},
                        {{"url":"{b}/page2","title":"Fake","img_src":"{b}/fake.png"}},
                        {{"url":"{b}/page3","title":"Moved","img_src":"{b}/moved.png"}}]}}"#).into_bytes())
                } else if path.starts_with("/search") && path.contains("categories=videos") {
                    ("200 OK", "application/json", br#"{"results":[
                        {"url":"https://www.youtube.com/watch?v=abc","title":"Red pandas at play","thumbnail":"https://i.ytimg.com/vi/abc/hq.jpg","length":"3:05"},
                        {"url":"https://vimeo.com/1","title":"Panda doc","length":3725}]}"#.to_vec())
                } else if path == "/img.png" {
                    ("200 OK", "image/png", PNG.to_vec())
                } else if path == "/fake.png" {
                    ("200 OK", "image/png", b"<html>not a picture</html>".to_vec())
                } else if path == "/moved.png" {
                    ("302 Found", "text/plain", Vec::new())
                } else if path == "/huge.png" {
                    let mut v = PNG.to_vec();
                    v.resize(13_000_000, 0);
                    ("200 OK", "image/png", v)
                } else {
                    ("404 Not Found", "text/plain", b"no".to_vec())
                };
                let extra = if path == "/moved.png" { "Location: /img.png\r\n" } else { "" };
                let mut s = s;
                let _ = s.write_all(format!("HTTP/1.1 {st}\r\nContent-Type: {ct}\r\nContent-Length: {}\r\n{extra}Connection: close\r\n\r\n",
                                            body.len()).as_bytes());
                let _ = s.write_all(&body);
            });
        }
    });
    base
}

fn web(base: &str) -> Web {
    Web::new(Config { searx: vec![base.into()], ddg: vec![], proxy: Proxy::None, allow_private: true,
                      timeout: Duration::from_secs(5), browser: None, ..Config::default() })
}

#[test]
fn pictures_are_found_and_only_real_ones_are_kept() {
    let base = serve();
    let w = web(&base);
    let found = w.images("red panda", 6).unwrap();
    assert_eq!(found.len(), 3);
    assert_eq!(found[0].title, "Red panda");
    let dir = std::env::temp_dir().join(format!("chuck-media-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let p = w.download_image(&found[0].src, &dir).unwrap();
    assert_eq!(std::fs::read(&p).unwrap(), PNG);
    assert!(p.extension().unwrap() == "png");
    assert!(w.download_image(&found[1].src, &dir).unwrap_err().contains("not a picture"));
    let moved = w.download_image(&found[2].src, &dir).unwrap();
    assert_eq!(std::fs::read(moved).unwrap(), PNG, "redirects are followed");
    assert!(w.download_image(&format!("{base}/huge.png"), &dir).unwrap_err().contains("12 MB"));
    // the same picture again: from disk
    assert_eq!(w.download_image(&found[0].src, &dir).unwrap(), p);
    // never a private address, by default
    let strict = Web::new(Config { proxy: Proxy::None, ..Config::default() });
    assert!(strict.download_image(&found[0].src, &dir).unwrap_err().contains("private"));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn videos_are_found_with_their_length() {
    let base = serve();
    let v = web(&base).videos("red pandas", 6).unwrap();
    assert_eq!(v.len(), 2);
    assert_eq!(v[0].length.as_deref(), Some("3:05"));
    assert_eq!(v[1].length.as_deref(), Some("62:05"));
    assert!(v[0].thumb.as_deref().unwrap().starts_with("https://i.ytimg.com"));
}
