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
        "bigask" if v["max_tokens"].as_u64().unwrap_or(0) > 16384 => {
            return plain(&mut c, "400 Bad Request", r#"{"message":"max_tokens must be less than or equal to 16384"}"#)
        }
        "notools" if v.get("tools").is_some() => {
            return plain(&mut c, "400 Bad Request", r#"{"message":"tools is not supported for this model"}"#)
        }
        "calls" => {
            assert!(v.get("tools").is_some() && v["tool_choice"] == "auto");
            let _ = write!(c, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n");
            let pieces = [
                json!({"content": "Checking."}),
                json!({"tool_calls": [{"index": 0, "id": "a", "type": "function", "function": {"name": "bash", "arguments": ""}}]}),
                json!({"tool_calls": [{"index": 0, "function": {"arguments": "{\"command\": \"ls"}}]}),
                json!({"tool_calls": [{"index": 0, "function": {"arguments": " -la\"}"}}]}),
                json!({"tool_calls": [{"index": 1, "id": "b", "type": "function", "function": {"name": "read", "arguments": "{\"path\": \"a.rs\"}"}}]}),
            ];
            for d in pieces {
                let _ = write!(c, "data: {}\n\n", json!({"choices": [{"delta": d}]}));
            }
            let _ = write!(c, "data: {}\n\ndata: [DONE]\n\n", json!({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}));
            return;
        }
        "queued" if n % 2 == 1 => {
            // the connection stays busy, but with nothing to read: keep-alives and empty deltas
            let _ = write!(c, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n");
            for i in 0..50 {
                let line = if i % 2 == 0 { ": keep-alive\n\n".to_string() } else {
                    format!("data: {}\n\n", json!({"choices": [{"delta": {"role": "assistant", "content": ""}}]})) };
                if write!(c, "{line}").and_then(|_| c.flush()).is_err() {
                    return;
                }
                std::thread::sleep(Duration::from_millis(200));
            }
            return;
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
    if mode == "empty" {
        return; // headers, then the connection closes: no reply at all
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
fn function_calls_come_back_whole_from_their_pieces() {
    let (base, _) = serve();
    let m = client(&base, 5);
    let mut req = ChatRequest::new("deepseek-ai/DeepSeek-V4-Flash", vec![json!({"role": "user", "content": "calls"})]);
    req.tools = Some(json!([{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}]));
    let out = m.chat(&req, &mut Collect::default()).unwrap();
    assert_eq!(out.text, "Checking.");
    assert_eq!(out.calls, vec![("bash".to_string(), "{\"command\": \"ls -la\"}".to_string()),
                               ("read".to_string(), "{\"path\": \"a.rs\"}".to_string())]);
}

#[test]
fn a_reply_ceiling_the_server_refuses_is_halved_and_remembered() {
    let (base, seen) = serve();
    let m = client(&base, 5);
    let mut req = ChatRequest::new("deepseek-ai/DeepSeek-V4-Flash", vec![json!({"role": "user", "content": "bigask"})]);
    req.max_tokens = 32768;
    assert_eq!(m.chat(&req, &mut Collect::default()).unwrap().text, "the answer");
    m.chat(&req, &mut Collect::default()).unwrap();
    let s = seen.lock().unwrap();
    assert_eq!(s.iter().map(|v| v["max_tokens"].as_u64().unwrap()).collect::<Vec<_>>(), vec![32768, 16384, 16384]);
}

#[test]
fn a_server_that_refuses_tools_is_asked_without_them() {
    let (base, seen) = serve();
    let m = client(&base, 5);
    let mut req = ChatRequest::new("deepseek-ai/DeepSeek-V4-Flash", vec![json!({"role": "user", "content": "notools"})]);
    req.tools = Some(json!([{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}]));
    let mut sink = Collect::default();
    let out = m.chat(&req, &mut sink).unwrap();
    assert_eq!(out.text, "the answer");
    assert!(sink.notices.contains(&Notice::OptionDropped("tools")));
    // remembered: the next call goes without them straight away
    let before = seen.lock().unwrap().len();
    m.chat(&req, &mut Collect::default()).unwrap();
    let s = seen.lock().unwrap();
    assert_eq!(s.len(), before + 1);
    assert!(s.last().unwrap().get("tools").is_none());
}

#[test]
fn a_connection_kept_alive_without_a_word_is_retried_not_waited_on() {
    let (base, _) = serve();
    let t0 = Instant::now();
    let (r, sink) = ask(&client(&base, 1), "queued", Reasoning::default());
    assert_eq!(r.unwrap().text, "the answer");
    assert!(t0.elapsed() < Duration::from_secs(5), "{:?}", t0.elapsed());
    assert!(sink.notices.iter().any(|n| matches!(n, Notice::Retrying { why, .. } if why.contains("nothing to read"))),
            "{:?}", sink.notices);
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
    assert!(out.was_cut(), "half a reply is never passed off as whole: {:?}", out.finish);
}

#[test]
fn an_empty_stream_is_retried_then_reported() {
    let (base, seen) = serve();
    let (r, sink) = ask(&client(&base, 10), "empty", Reasoning::default());
    assert!(matches!(r, Err(ModelError::Network(_))), "{r:?}");
    assert_eq!(seen.lock().unwrap().len(), 3, "three attempts");
    assert!(sink.notices.iter().any(|n| matches!(n, Notice::Retrying { .. })));
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

/// One request's head and body off a kept-alive connection (None: it closed).
fn read_request(r: &mut BufReader<TcpStream>) -> Option<Vec<u8>> {
    let mut first = String::new();
    if r.read_line(&mut first).ok()? == 0 {
        return None;
    }
    let mut len = 0usize;
    loop {
        let mut h = String::new();
        if r.read_line(&mut h).ok()? == 0 || h == "\r\n" {
            break;
        }
        if let Some(v) = h.to_lowercase().strip_prefix("content-length:") {
            len = v.trim().parse().ok()?;
        }
    }
    let mut body = vec![0; len];
    r.read_exact(&mut body).ok()?;
    Some(body)
}

#[test]
fn calls_one_after_another_share_one_connection() {
    // a streamed reply read to its end goes back to the pool: the next call skips a new connection and handshake
    let l = TcpListener::bind("127.0.0.1:0").unwrap();
    let base = format!("http://{}/v1", l.local_addr().unwrap());
    let conns = Arc::new(Mutex::new(0usize));
    let c2 = conns.clone();
    std::thread::spawn(move || {
        for conn in l.incoming().flatten() {
            *c2.lock().unwrap() += 1;
            std::thread::spawn(move || {
                let mut w = conn.try_clone().unwrap();
                let mut r = BufReader::new(conn);
                while read_request(&mut r).is_some() {
                    let mut body = String::new();
                    for piece in ["{\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}",
                                  "{\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}"] {
                        body.push_str(&format!("data: {piece}\n\n"));
                    }
                    body.push_str("data: [DONE]\n\n");
                    let _ = write!(w, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n\
                                       {:x}\r\n{body}\r\n0\r\n\r\n", body.len());
                }
            });
        }
    });
    let m = SiliconFlow::new(&base, "sk-test", Duration::from_secs(10), true).unwrap();
    for _ in 0..3 {
        let mut c = Collect::default();
        let out = m.chat(&ChatRequest::new("m", vec![json!({"role": "user", "content": "hello"})]), &mut c).unwrap();
        assert_eq!(out.text, "hi");
        std::thread::sleep(Duration::from_millis(100)); // the rest of the response is drained on its own thread
    }
    assert_eq!(*conns.lock().unwrap(), 1, "three calls, one connection");
}
