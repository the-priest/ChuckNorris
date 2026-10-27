//! The SiliconFlow client: OpenAI-compatible chat completions, streamed.

use crate::pricing::Usage;
use crate::{ChatModel, ChatRequest, ModelError, Notice, Outcome, Sink};
use serde_json::{json, Value};
use std::collections::HashSet;
use std::io::{BufRead, BufReader, Read};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

pub const DEFAULT_BASE: &str = "https://api.siliconflow.com/v1";
/// No reply is allowed to stream more than this (a runaway server).
const MAX_STREAM_BYTES: usize = 64 << 20;

/// Is this a SiliconFlow endpoint (international or China site)?
pub fn is_siliconflow(base: &str) -> bool {
    let host = base.split("://").nth(1).unwrap_or(base).split(['/', ':']).next().unwrap_or("").to_lowercase();
    host == "siliconflow.com" || host == "siliconflow.cn" || host.ends_with(".siliconflow.com")
        || host.ends_with(".siliconflow.cn")
}

fn is_local(base: &str) -> bool {
    let host = base.split("://").nth(1).unwrap_or(base).split(['/', ':']).next().unwrap_or("");
    matches!(host, "127.0.0.1" | "localhost" | "[::1]")
}

pub struct SiliconFlow {
    base: String,
    key: String,
    agent: ureq::Agent,
    attempts: u32,
    no_stream_opts: AtomicBool,
    no_think_opts: Mutex<HashSet<String>>,
    /// The longest a reply may go without a word before it is tried again.
    stall: Duration,
}

impl SiliconFlow {
    /// A client for `base` with `key`. Refuses anything but SiliconFlow
    /// unless `allow_other` (a local test server, or the user's explicit
    /// override in Settings). `stall` is the longest silence allowed between
    /// bytes before the call is retried.
    pub fn new(base: &str, key: &str, stall: Duration, allow_other: bool) -> Result<SiliconFlow, ModelError> {
        let base = if base.trim().is_empty() { DEFAULT_BASE } else { base.trim() }.trim_end_matches('/');
        if !allow_other && !is_siliconflow(base) {
            return Err(ModelError::NotSiliconFlow(base.to_string()));
        }
        let mut cfg = ureq::Agent::config_builder()
            .http_status_as_error(false)
            .timeout_connect(Some(Duration::from_secs(20)))
            .timeout_per_read(Some(stall.max(Duration::from_secs(1))))
            // the system's trust store (a company's inspecting proxy lives there), not a bundled one
            .tls_config(ureq::tls::TlsConfig::builder().root_certs(ureq::tls::RootCerts::PlatformVerifier).build());
        if is_local(base) {
            cfg = cfg.proxy(None);
        }
        Ok(SiliconFlow {
            base: base.to_string(),
            key: key.trim().to_string(),
            agent: cfg.build().new_agent(),
            attempts: 3,
            no_stream_opts: AtomicBool::new(false),
            no_think_opts: Mutex::new(HashSet::new()),
            stall: stall.max(Duration::from_secs(1)),
        })
    }

    pub fn base(&self) -> &str {
        &self.base
    }

    fn payload(&self, req: &ChatRequest, think_on: bool) -> Value {
        let mut p = json!({
            "model": req.model,
            "messages": req.messages,
            "stream": true,
            "temperature": req.temperature.clamp(0.0, 1.5),
            "max_tokens": req.max_tokens.clamp(256, 32768),
        });
        if !self.no_stream_opts.load(Ordering::Relaxed) {
            p["stream_options"] = json!({"include_usage": true});
        }
        if !self.no_think_opts.lock().unwrap().contains(&req.model) {
            if !think_on || req.reasoning.budget == 0 {
                p["enable_thinking"] = json!(false);
            } else {
                p["thinking_budget"] = json!(req.reasoning.budget.max(128));
            }
        }
        p
    }

    fn post(&self, payload: &Value) -> Result<ureq::http::Response<ureq::Body>, ModelError> {
        let body = serde_json::to_vec(payload).unwrap_or_default();
        self.agent.post(&format!("{}/chat/completions", self.base))
            .header("Authorization", &format!("Bearer {}", self.key))
            .header("Content-Type", "application/json")
            .send(&body[..])
            .map_err(net_err)
    }
}

fn net_err(e: ureq::Error) -> ModelError {
    match e {
        ureq::Error::Timeout(_) => ModelError::Timeout,
        ureq::Error::Io(io) if io.kind() == std::io::ErrorKind::TimedOut
            || io.kind() == std::io::ErrorKind::WouldBlock => ModelError::Timeout,
        other => ModelError::Network(other.to_string()),
    }
}

fn status_err(status: u16, body: &str) -> ModelError {
    let msg = serde_json::from_str::<Value>(body).ok()
        .and_then(|v| v.get("message").or_else(|| v.get("error").and_then(|e| e.get("message"))).cloned())
        .and_then(|m| m.as_str().map(str::to_string))
        .unwrap_or_else(|| body.chars().take(300).collect());
    match status {
        401 | 403 => ModelError::Unauthorized,
        429 => ModelError::RateLimited,
        _ => ModelError::Http { status, message: msg },
    }
}

/// How one streamed attempt ended.
enum Attempt {
    Done(Outcome),
    /// Thinking ran past its clock or budget: ask again with thinking off.
    ThinkCut(u64),
}

impl ChatModel for SiliconFlow {
    fn chat(&self, req: &ChatRequest, sink: &mut dyn Sink) -> Result<Outcome, ModelError> {
        if self.key.is_empty() {
            return Err(ModelError::NoKey);
        }
        let mut think_on = true;
        let mut cut_once = false;
        let mut attempt = 0u32;
        loop {
            if sink.should_stop() {
                return Err(ModelError::Stopped);
            }
            let payload = self.payload(req, think_on);
            let resp = match self.post(&payload) {
                Ok(r) => r,
                Err(e) => {
                    attempt += 1;
                    if e.transient() && attempt < self.attempts {
                        sink.notice(Notice::Retrying { attempt, why: e.to_string() });
                        std::thread::sleep(Duration::from_millis(500 * attempt as u64));
                        continue;
                    }
                    return Err(e);
                }
            };
            let status = resp.status().as_u16();
            if status >= 400 {
                let mut body = String::new();
                let _ = resp.into_body().into_reader().take(8192).read_to_string(&mut body);
                let low = body.to_lowercase();
                if matches!(status, 400 | 422) {
                    if payload.get("stream_options").is_some()
                        && (low.contains("stream_options") || low.contains("include_usage"))
                    {
                        self.no_stream_opts.store(true, Ordering::Relaxed);
                        sink.notice(Notice::OptionDropped("stream_options"));
                        continue;
                    }
                    if (payload.get("thinking_budget").is_some() || payload.get("enable_thinking").is_some())
                        && (low.contains("thinking") || low.contains("reasoning"))
                    {
                        self.no_think_opts.lock().unwrap().insert(req.model.clone());
                        sink.notice(Notice::OptionDropped("thinking"));
                        continue;
                    }
                }
                let e = status_err(status, &body);
                attempt += 1;
                if e.transient() && attempt < self.attempts {
                    sink.notice(Notice::Retrying { attempt, why: e.to_string() });
                    std::thread::sleep(Duration::from_millis(500 * attempt as u64));
                    continue;
                }
                return Err(e);
            }
            match stream(resp, req, sink, cut_once, self.stall) {
                Ok(Attempt::Done(mut out)) => {
                    out.model = req.model.clone();
                    return Ok(out);
                }
                Ok(Attempt::ThinkCut(secs)) => {
                    cut_once = true;
                    think_on = false;
                    sink.notice(Notice::ThinkCut { secs });
                }
                Err(boxed) => {
                    let (e, partial) = *boxed;
                    if let Some(mut out) = partial {
                        // text already reached the user: keep it as a CUT reply
                        // (the agent continues it) rather than replay and duplicate
                        out.finish = Some("length".into());
                        out.model = req.model.clone();
                        return Ok(out);
                    }
                    attempt += 1;
                    if e.transient() && attempt < self.attempts {
                        sink.notice(Notice::Retrying { attempt, why: e.to_string() });
                        std::thread::sleep(Duration::from_millis(500 * attempt as u64));
                        continue;
                    }
                    return Err(e);
                }
            }
        }
    }

    fn list_models(&self) -> Result<Vec<String>, ModelError> {
        let resp = self.agent.get(&format!("{}/models?type=text&sub_type=chat", self.base))
            .header("Authorization", &format!("Bearer {}", self.key))
            .call().map_err(net_err)?;
        let status = resp.status().as_u16();
        let mut body = String::new();
        let _ = resp.into_body().into_reader().take(4 << 20).read_to_string(&mut body);
        if status >= 400 {
            return Err(status_err(status, &body));
        }
        let v: Value = serde_json::from_str(&body).map_err(|e| ModelError::Network(e.to_string()))?;
        let mut ids: Vec<String> = v.get("data").and_then(|d| d.as_array()).map(|a| a.iter()
            .filter_map(|m| m.get("id").and_then(|i| i.as_str()).map(str::to_string)).collect()).unwrap_or_default();
        ids.sort();
        ids.dedup();
        Ok(ids)
    }
}

type StreamErr = Box<(ModelError, Option<Outcome>)>;

fn stream(resp: ureq::http::Response<ureq::Body>, req: &ChatRequest, sink: &mut dyn Sink, cut_once: bool,
          stall: Duration) -> Result<Attempt, StreamErr>
{
    // bytes that carry no words (keep-alive lines while the server queues the request, empty
    // deltas) don't count as an answer: past `stall` without one word, the call is tried again
    let mut heard = Instant::now();
    let reader = resp.into_body().into_reader();
    let mut lines = BufReader::new(reader.take(MAX_STREAM_BYTES as u64));
    let mut out = Outcome::default();
    let mut think_t0: Option<Instant> = None;
    let mut buf = String::new();
    let mut finished = false;
    loop {
        if sink.should_stop() {
            return Err(Box::new((ModelError::Stopped, None)));
        }
        buf.clear();
        let n = match lines.read_line(&mut buf) {
            Ok(n) => n,
            Err(e) => {
                let err = if matches!(e.kind(), std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock) {
                    ModelError::Timeout
                } else {
                    ModelError::Network(e.to_string())
                };
                return Err(Box::new((err, if out.text.is_empty() { None } else { Some(out) })));
            }
        };
        if n == 0 {
            break; // the server closed the stream
        }
        let line = buf.trim();
        if heard.elapsed() > stall {
            let err = ModelError::Network(format!("the API sent nothing to read for {}s (it may be queueing the \
                request)", heard.elapsed().as_secs()));
            return Err(Box::new((err, if out.text.is_empty() { None } else { Some(out) })));
        }
        let Some(data) = line.strip_prefix("data:") else { continue };
        let data = data.trim();
        if data == "[DONE]" {
            finished = true;
            break;
        }
        let Ok(d) = serde_json::from_str::<Value>(data) else { continue };
        if let Some(u) = Usage::from_chunk(&d) {
            out.usage = Some(u);
        }
        let Some(ch) = d.get("choices").and_then(|c| c.get(0)) else { continue };
        if let Some(f) = ch.get("finish_reason").and_then(|f| f.as_str()) {
            out.finish = Some(f.to_string());
            finished = true;
        }
        let delta = ch.get("delta").cloned().unwrap_or(Value::Null);
        if let Some(r) = delta.get("reasoning_content").and_then(|r| r.as_str()) {
            if !r.is_empty() {
                heard = Instant::now();
                sink.think(r);
                out.reasoning.push_str(r);
                if out.text.is_empty() && !cut_once {
                    let t0 = *think_t0.get_or_insert_with(Instant::now);
                    let secs = t0.elapsed().as_secs();
                    let rb = &req.reasoning;
                    if (rb.max_think_secs > 0 && secs >= rb.max_think_secs as u64)
                        || (rb.budget > 0 && out.reasoning.len() > rb.budget as usize * 6)
                    {
                        return Ok(Attempt::ThinkCut(secs));
                    }
                }
            }
        }
        if let Some(c) = delta.get("content").and_then(|c| c.as_str()) {
            if !c.is_empty() {
                heard = Instant::now();
                sink.delta(c);
                out.text.push_str(c);
            }
        }
    }
    if !finished {
        // the connection ended mid-reply: never pass half a reply off as whole
        if out.text.is_empty() {
            return Err(Box::new((ModelError::Network("the stream ended before the reply".into()), None)));
        }
        out.finish = Some("length".into());
    }
    Ok(Attempt::Done(out))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_siliconflow() {
        assert!(is_siliconflow("https://api.siliconflow.com/v1"));
        assert!(is_siliconflow("https://api.siliconflow.cn/v1"));
        assert!(!is_siliconflow("https://api.openai.com/v1"));
        assert!(!is_siliconflow("https://siliconflow.com.evil.io/v1"));
        assert!(matches!(SiliconFlow::new("https://api.openai.com/v1", "k", Duration::from_secs(5), false),
                         Err(ModelError::NotSiliconFlow(_))));
    }
}
