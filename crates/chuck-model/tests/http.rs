//! The real client against a local fake SiliconFlow server.

use chuck_model::{ChatModel, ChatRequest, Collect, ModelError, Notice, Reasoning, SiliconFlow};
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

type Seen = Arc<Mutex<Vec<Value>>>;

fn serve() -> (String, Seen) {
    let l = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = format!("http://{}", l.local_addr().unwrap());
    let seen: Seen = Arc::new(Mutex::new(Vec::new()));
    let s2 = seen.clone();
    std::thread::spawn(move || {
        for conn in l.incoming().flatten() {
            let s3 = s2.clone();
            std::thread::spawn(move || handle(conn, s3));
        }
    });
    (addr, seen)
}

fn handle(mut c: TcpStream, seen: Seen) {
    let mut r = BufReader::new(c.try_clone().unwrap());
    let mut len = 0usize;
    let mut first = String::new();
    r.read_line(&mut first).unwrap();
    loop {
        let mut h = String::new();
        if r.read_line(&mut h).unwrap() == 0 || h == "\r\n" {
            break;
        }
        if let Some(v) = h.to_lowercase().strip_prefix("content-length:") {
            len = v.trim().parse().unwrap();
        }
    }
    let mut body = vec![0; len];
    r.read_exact(&mut body).unwrap();
    if first.starts_with("GET /v1/models") {
        let b = r#"{"data":[{"id":"deepseek-ai/DeepSeek-V4.1-Flash"},{"id":"zai-org/GLM-5.3-Flash"}]}"#;
        let _ = write!(c, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{b}", b.len());
        return;
    }
    let v: Value = serde_json::from_slice(&body).unwrap();
    let n = {
        let mut s = seen.lock().unwrap();
        s.push(v.clone());
        s.len()
    };
    let mode = v["messages"][0]["content"].as_str().unwrap_or("").to_string();
    let plain = |c: &mut TcpStream, status: &str, b: &str| {
        let _ = write!(c, "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{b}", b.len());
    };
    match mode.as_str() {
        "badkey" => return plain(&mut c, "401 Unauthorized", r#"{"message":"invalid key"}"#),
        "picky" if v.get("thinking_budget").is_some() || v.get("enable_thinking").is_some() => {
            return plain(&mut c, "400 Bad Request", r#"{"message":"thinking_budget is not supported"}"#)
        }
        "nousage" if v.get("stream_options").is_some() => {
            return plain(&mut c, "400 Bad Request", r#"{"message":"unknown field stream_options"}"#)
        }
        "stall" if n % 2 == 1 => {
            let _ = write!(c, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n");
            std::thread::sleep(Duration::from_secs(4)); // then nothing at all
            return;
        }
        _ => {}
    }
    let _ = write!(c, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n");
    let ev = |c: &mut TcpStream, d: Value| {
        let _ = write!(c, "data: {}\n\n", json!({"choices": [{"delta": d}]}));
        let _ = c.flush();
    };
    if mode == "ponder" && v.get("enable_thinking") != Some(&json!(false)) {
        for _ in 0..600 {
            ev(&mut c, json!({"reasoning_content": "let me reconsider that once more. "}));
            std::thread::sleep(Duration::from_millis(5));
        }
        return;
    }
    if mode == "drop" {
        ev(&mut c, json!({"content": "half an ans"}));
        return; // connection closes mid-answer, no [DONE]
    }
    ev(&mut c, json!({"reasoning_content": "thinking briefly. "}));
    ev(&mut c, json!({"content": "the "}));
    ev(&mut c, json!({"content": "answer"}));
    let _ = write!(c, "data: {}\n\n", json!({"choices": [{"delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 60}}));
    let _ = write!(c, "data: [DONE]\n\n");
}

fn client(base: &str, stall: u64) -> SiliconFlow {
    SiliconFlow::new(&format!("{base}/v1"), "k", Duration::from_secs(stall), true).unwrap()
}

fn ask(m: &SiliconFlow, mode: &str, reasoning: Reasoning) -> (Result<chuck_model::Outcome, ModelError>, Collect) {
    let mut req = ChatRequest::new("deepseek-ai/DeepSeek-V4.1-Flash-1017", vec![json!({"role": "user", "content": mode})]);
    req.reasoning = reasoning;
    let mut sink = Collect::default();
    let r = m.chat(&req, &mut sink);
    (r, sink)
}

#[test]
fn streams_reasoning_apart_from_the_answer_with_real_usage() {
    let (base, seen) = serve();
    let m = client(&base, 10);
    let (r, sink) = ask(&m, "hi", Reasoning { budget: 8192, max_think_secs: 120 });
    let out = r.unwrap();
    assert_eq!(out.text, "the answer");
    assert_eq!(sink.reasoning, "thinking briefly. ");
    assert_eq!(out.finish.as_deref(), Some("stop"));
    let u = out.usage.unwrap();
    assert_eq!((u.prompt, u.completion, u.cached), (100, 20, 60));
    let p = &seen.lock().unwrap()[0];
    assert_eq!(p["thinking_budget"], 8192);
    assert_eq!(p["stream_options"]["include_usage"], true);
}

#[test]
fn reasoning_off_asks_for_the_answer_directly() {
    let (base, seen) = serve();
    let (r, _) = ask(&client(&base, 10), "hi", Reasoning { budget: 0, max_think_secs: 0 });
    assert_eq!(r.unwrap().text, "the answer");
    let p = &seen.lock().unwrap()[0];
    assert_eq!(p["enable_thinking"], false);
    assert!(p.get("thinking_budget").is_none());
}

#[test]
fn an_endless_thinker_is_cut_by_the_clock_and_answers_directly() {
    let (base, seen) = serve();
    let t0 = Instant::now();
    let (r, sink) = ask(&client(&base, 10), "ponder", Reasoning { budget: 32768, max_think_secs: 1 });
    assert_eq!(r.unwrap().text, "the answer");
    assert!(t0.elapsed() < Duration::from_secs(10));
    assert!(sink.notices.iter().any(|n| matches!(n, Notice::ThinkCut { .. })));
    assert_eq!(seen.lock().unwrap().last().unwrap()["enable_thinking"], false);
}

#[test]
fn options_a_server_refuses_are_dropped_and_the_question_answered() {
    let (base, seen) = serve();
    let m = client(&base, 10);
    let (r, sink) = ask(&m, "picky", Reasoning::default());
    assert_eq!(r.unwrap().text, "the answer");
    assert!(sink.notices.contains(&Notice::OptionDropped("thinking")));
    let (r2, _) = ask(&m, "nousage", Reasoning::default());
    assert_eq!(r2.unwrap().text, "the answer");
    let s = seen.lock().unwrap();
    assert!(s.last().unwrap().get("stream_options").is_none());
}

#[test]
fn a_silent_connection_is_retried_not_waited_on() {
    let (base, _) = serve();
    let t0 = Instant::now();
    let (r, sink) = ask(&client(&base, 1), "stall", Reasoning::default());
    assert_eq!(r.unwrap().text, "the answer");
    assert!(t0.elapsed() < Duration::from_secs(4), "{:?}", t0.elapsed());
    assert!(sink.notices.iter().any(|n| matches!(n, Notice::Retrying { .. })));
}

#[test]
fn a_drop_mid_answer_keeps_the_text_as_a_cut_reply() {
    let (base, _) = serve();
    let (r, _) = ask(&client(&base, 10), "drop", Reasoning::default());
    let out = r.unwrap();
    assert_eq!(out.text, "half an ans");
    assert!(out.was_cut() || out.finish.is_none());
}

#[test]
fn a_rejected_key_is_not_retried() {
    let (base, seen) = serve();
    let (r, _) = ask(&client(&base, 10), "badkey", Reasoning::default());
    assert_eq!(r.unwrap_err(), ModelError::Unauthorized);
    assert_eq!(seen.lock().unwrap().len(), 1);
}

#[test]
fn lists_the_accounts_models() {
    let (base, _) = serve();
    let ids = client(&base, 10).list_models().unwrap();
    assert_eq!(ids, vec!["deepseek-ai/DeepSeek-V4.1-Flash", "zai-org/GLM-5.3-Flash"]);
}
