//! Chat shows pictures and videos: found through SearXNG on a loopback
//! server, the pictures downloaded and checked, the cards as events, and
//! the model told what it showed (it doesn't see them).

use chuck_model::{ChatRequest, Scripted};
use chuck_runtime::{Event, Events, Job, Runtime, RuntimeConfig};
use chuck_web::{Config, Proxy, Web};
use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::sync::Arc;
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
                let (ct, body): (&str, Vec<u8>) = if path.contains("categories=images") {
                    ("application/json", format!(r#"{{"results":[
                        {{"url":"{b}/a","title":"Red panda in a tree","img_src":"{b}/1.png"}},
                        {{"url":"{b}/b","title":"Not really a picture","img_src":"{b}/html.png"}},
                        {{"url":"https://other.example/c","title":"Thumb only","img_src":"{b}/gone.png","thumbnail_src":"{b}/2.png"}}]}}"#).into_bytes())
                } else if path.contains("categories=videos") {
                    ("application/json", format!(r#"{{"results":[
                        {{"url":"https://www.youtube.com/watch?v=x","title":"Red pandas playing","thumbnail":"{b}/1.png","length":"2:10"}}]}}"#).into_bytes())
                } else if path.ends_with(".png") && !path.contains("html") && !path.contains("gone") {
                    ("image/png", PNG.to_vec())
                } else {
                    ("text/html", b"<html>nope</html>".to_vec())
                };
                let mut s = s;
                let st = if path.contains("gone") { "404 Not Found" } else { "200 OK" };
                let _ = s.write_all(format!("HTTP/1.1 {st}\r\nContent-Type: {ct}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                                            body.len()).as_bytes());
                let _ = s.write_all(&body);
            });
        }
    });
    base
}

fn last_user(req: &ChatRequest) -> String {
    req.messages.iter().rev().find(|m| m["role"] == "user").and_then(|m| m["content"].as_str()).unwrap_or("").to_string()
}

#[test]
fn pictures_and_videos_are_shown_and_described_to_the_model() {
    let base = serve();
    let dir = std::env::temp_dir().join(format!("chuck-chat-media-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let web = Arc::new(Web::new(Config { searx: vec![base.clone()], ddg: vec![], proxy: Proxy::None, allow_private: true,
                                         timeout: Duration::from_secs(5), browser: None, ..Config::default() }));
    let model = Arc::new(Scripted::new().replies(["```images red panda```\n```videos red panda```", "Here they are."]));
    let (tx, rx) = std::sync::mpsc::channel();
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), web: Some(web), ..RuntimeConfig::default() },
                          Events(Some(tx)));
    rt.chat(Job::new("show me red pandas", &dir));
    let events: Vec<Event> = rx.try_iter().collect();
    let pics = events.iter().find_map(|e| match e { Event::Gallery { items, .. } => Some(items.clone()), _ => None }).unwrap();
    assert_eq!(pics.len(), 2, "the HTML 'picture' is dropped; the missing one falls back to its thumbnail: {pics:?}");
    assert!(pics.iter().all(|p| std::fs::read(&p.path).unwrap() == PNG));
    assert_eq!(pics[0].title, "Red panda in a tree");
    let vids = events.iter().find_map(|e| match e { Event::Videos { items, .. } => Some(items.clone()), _ => None }).unwrap();
    assert_eq!(vids.len(), 1);
    assert_eq!(vids[0].length.as_deref(), Some("2:10"));
    assert!(vids[0].thumb.as_ref().is_some_and(|t| t.is_file()));
    let fb = last_user(&model.seen.lock().unwrap()[1]);
    assert!(fb.contains("showed the user 2 pictures — you don't see them") && fb.contains("Red panda in a tree"), "{fb}");
    assert!(fb.contains("showed the user 1 video:") && fb.contains("youtube.com/watch?v=x"), "{fb}");
    let _ = std::fs::remove_dir_all(&dir);
}
