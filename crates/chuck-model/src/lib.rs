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
}

impl ChatRequest {
    pub fn new(model: impl Into<String>, messages: Vec<Value>) -> ChatRequest {
        ChatRequest { model: model.into(), messages, temperature: 0.35, max_tokens: 8192,
                      reasoning: Reasoning::default() }
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
