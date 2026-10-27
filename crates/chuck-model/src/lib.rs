//! chuck-model — the only way an agent thinks: SiliconFlow chat completions.
//!
//! One trait, [`ChatModel`], with the real HTTP client ([`SiliconFlow`]) and a
//! scripted stand-in for tests ([`Scripted`]). Every agent in the fleet calls
//! it from its own thread; the client is `Send + Sync` and shares a connection
//! pool.

pub mod pricing;
mod scripted;
mod siliconflow;

pub use pricing::{fmt_tokens, fmt_usd, price_for, sees_images, Usage};
pub use scripted::Scripted;
pub use siliconflow::{is_siliconflow, SiliconFlow, DEFAULT_BASE};

use serde_json::Value;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, RecvTimeoutError};
use std::sync::Arc;
use std::time::Duration;

/// How much a thinking model may think: a token budget sent to the API
/// (0 = thinking off) and a clock (0 = none). Past the clock with no answer
/// yet, the call is stopped and asked again with thinking off.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Reasoning {
    pub budget: u32,
    pub max_think_secs: u32,
}

impl Default for Reasoning {
    fn default() -> Reasoning {
        Reasoning { budget: 4096, max_think_secs: 120 }
    }
}

#[derive(Clone, Debug)]
pub struct ChatRequest {
    pub model: String,
    /// OpenAI-style messages: {"role": ..., "content": string | parts}.
    pub messages: Vec<Value>,
    pub temperature: f32,
    pub max_tokens: u32,
    pub reasoning: Reasoning,
    /// Functions the model may call (OpenAI-style `tools`): what it calls
    /// comes back in [`Outcome::calls`], parsed by the API rather than
    /// written out as text. A server that refuses them gets the request
    /// without them.
    pub tools: Option<Value>,
}

impl ChatRequest {
    pub fn new(model: impl Into<String>, messages: Vec<Value>) -> ChatRequest {
        ChatRequest { model: model.into(), messages, temperature: 0.35, max_tokens: 8192,
                      reasoning: Reasoning::default(), tools: None }
    }

    pub fn sent_chars(&self) -> usize {
        self.messages.iter().map(|m| match m.get("content") {
            Some(Value::String(s)) => s.len(),
            Some(other) => other.to_string().len(),
            None => 0,
        }).sum()
    }
}

/// Things worth telling the user while a call runs.
#[derive(Clone, Debug, PartialEq)]
pub enum Notice {
    /// Thought this long without answering: asked again with thinking off.
    ThinkCut { secs: u64 },
    /// A transient failure; trying again.
    Retrying { attempt: u32, why: String },
    /// The server refused an optional parameter; it was dropped.
    OptionDropped(&'static str),
}

/// Where a streaming reply goes.
pub trait Sink {
    fn think(&mut self, _chunk: &str) {}
    fn delta(&mut self, _chunk: &str) {}
    fn notice(&mut self, _n: Notice) {}
    /// Checked between chunks: true stops the call (Esc, or the kernel's KILL).
    fn should_stop(&self) -> bool {
        false
    }
}

/// A sink that keeps everything (tests, headless runs).
#[derive(Default, Debug)]
pub struct Collect {
    pub text: String,
    pub reasoning: String,
    pub notices: Vec<Notice>,
}

impl Sink for Collect {
    fn think(&mut self, c: &str) {
        self.reasoning.push_str(c);
    }
    fn delta(&mut self, c: &str) {
        self.text.push_str(c);
    }
    fn notice(&mut self, n: Notice) {
        self.notices.push(n);
    }
}

#[derive(Clone, Debug, Default)]
pub struct Outcome {
    pub text: String,
    pub reasoning: String,
    pub usage: Option<Usage>,
    /// "stop", "length", ...: "length" means the reply was cut and should be continued.
    pub finish: Option<String>,
    /// The model that actually answered.
    pub model: String,
    /// Functions it called: (name, arguments as the JSON text it sent).
    pub calls: Vec<(String, String)>,
}

impl Outcome {
    pub fn was_cut(&self) -> bool {
        self.finish.as_deref() == Some("length")
    }
}

#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum ModelError {
    #[error("No SiliconFlow API key. Add one in Settings.")]
    NoKey,
    #[error("{0} isn't a SiliconFlow endpoint: Chuck's fleet talks to SiliconFlow only")]
    NotSiliconFlow(String),
    #[error("API key rejected — check it in Settings.")]
    Unauthorized,
    #[error("Rate limited by the API. Give it a moment.")]
    RateLimited,
    #[error("API error (HTTP {status}): {message}")]
    Http { status: u16, message: String },
    #[error("Can't reach the API ({0}) — check your connection.")]
    Network(String),
    #[error("The API went silent for too long. Try again.")]
    Timeout,
    #[error("stopped")]
    Stopped,
    /// The call's own thread died (a bug, not the network): trying again
    /// would only do it again.
    #[error("the model call crashed: {0}")]
    Crashed(String),
}

impl ModelError {
    /// Worth retrying: a dropped connection, a timeout, 429 or 5xx.
    pub fn transient(&self) -> bool {
        match self {
            ModelError::RateLimited | ModelError::Network(_) | ModelError::Timeout => true,
            ModelError::Http { status, .. } => matches!(status, 408 | 425 | 500 | 502 | 503 | 504),
            _ => false,
        }
    }
}

/// The one way an agent thinks.
pub trait ChatModel: Send + Sync {
    fn chat(&self, req: &ChatRequest, sink: &mut dyn Sink) -> Result<Outcome, ModelError>;

    /// The chat models this key can use (for resolving the fleet's ids).
    fn list_models(&self) -> Result<Vec<String>, ModelError> {
        Ok(Vec::new())
    }
}

enum Piece {
    Think(String),
    Delta(String),
    Notice(Notice),
    End(Result<Outcome, ModelError>),
}

struct Relay {
    tx: std::sync::mpsc::Sender<Piece>,
    stop: Arc<AtomicBool>,
}

impl Sink for Relay {
    fn think(&mut self, c: &str) {
        let _ = self.tx.send(Piece::Think(c.to_string()));
    }
    fn delta(&mut self, c: &str) {
        let _ = self.tx.send(Piece::Delta(c.to_string()));
    }
    fn notice(&mut self, n: Notice) {
        let _ = self.tx.send(Piece::Notice(n));
    }
    fn should_stop(&self) -> bool {
        self.stop.load(Ordering::Relaxed)
    }
}

/// [`ChatModel::chat`], but a Stop is honoured within a tenth of a second
/// even while the connection is silent (connecting, or a server that hasn't
/// sent a byte yet): the call runs on its own thread, and on Stop it is
/// abandoned — it ends by itself at its next chunk or its stall timeout, and
/// nothing it produces afterwards reaches anyone.
pub fn chat_interruptible(model: &Arc<dyn ChatModel>, req: &ChatRequest, sink: &mut dyn Sink)
    -> Result<Outcome, ModelError>
{
    if sink.should_stop() {
        return Err(ModelError::Stopped);
    }
    let (tx, rx) = channel();
    let stop = Arc::new(AtomicBool::new(false));
    let relay_stop = stop.clone();
    let (m, r) = (model.clone(), req.clone());
    std::thread::Builder::new().name("chuck-model-call".into()).spawn(move || {
        let mut relay = Relay { tx: tx.clone(), stop: relay_stop };
        let out = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| m.chat(&r, &mut relay)))
            .unwrap_or_else(|p| Err(ModelError::Crashed(p.downcast_ref::<String>().cloned()
                .or_else(|| p.downcast_ref::<&str>().map(|s| s.to_string())).unwrap_or_else(|| "a panic".into()))));
        let _ = tx.send(Piece::End(out));
    }).map_err(|e| ModelError::Network(format!("couldn't start the call: {e}")))?;
    loop {
        if sink.should_stop() {
            stop.store(true, Ordering::Relaxed);
            return Err(ModelError::Stopped);
        }
        match rx.recv_timeout(Duration::from_millis(100)) {
            Ok(Piece::Think(c)) => sink.think(&c),
            Ok(Piece::Delta(c)) => sink.delta(&c),
            Ok(Piece::Notice(n)) => sink.notice(n),
            Ok(Piece::End(out)) => return out,
            Err(RecvTimeoutError::Timeout) => continue,
            Err(RecvTimeoutError::Disconnected) => {
                return Err(ModelError::Crashed("the call ended without a result".into()));
            }
        }
    }
}

#[cfg(test)]
mod interrupt_tests {
    use super::*;

    struct Hang;
    impl ChatModel for Hang {
        fn chat(&self, _: &ChatRequest, sink: &mut dyn Sink) -> Result<Outcome, ModelError> {
            sink.delta("partial ");
            std::thread::sleep(Duration::from_secs(5)); // a silent server: no chunk to check Stop at
            Ok(Outcome::default())
        }
    }

    struct Watch {
        got: String,
        flag: Arc<AtomicBool>,
    }
    impl Sink for Watch {
        fn delta(&mut self, c: &str) {
            self.got.push_str(c);
        }
        fn should_stop(&self) -> bool {
            self.flag.load(Ordering::Relaxed)
        }
    }

    #[test]
    fn stop_wins_over_a_silent_connection() {
        let m: Arc<dyn ChatModel> = Arc::new(Hang);
        let flag = Arc::new(AtomicBool::new(false));
        let f2 = flag.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(300));
            f2.store(true, Ordering::Relaxed);
        });
        let mut w = Watch { got: String::new(), flag };
        let t0 = std::time::Instant::now();
        let r = chat_interruptible(&m, &ChatRequest::new("m", vec![]), &mut w);
        assert_eq!(r.unwrap_err(), ModelError::Stopped);
        assert!(t0.elapsed() < Duration::from_secs(1), "{:?}", t0.elapsed());
        assert_eq!(w.got, "partial ");
    }

    #[test]
    fn a_normal_call_passes_through() {
        let m: Arc<dyn ChatModel> = Arc::new(Scripted::new().replies(["hello there"]));
        let mut c = Collect::default();
        let out = chat_interruptible(&m, &ChatRequest::new("m", vec![serde_json::json!({"role": "user", "content": "hi"})]), &mut c)
            .unwrap();
        assert_eq!(out.text, "hello there");
        assert_eq!(c.text, "hello there");
    }
}
