//! A stand-in model for tests: replies come from a script, per model id or
//! from a shared queue, and every request is recorded.

use crate::{ChatModel, ChatRequest, ModelError, Outcome, Sink, Usage};
use std::collections::{HashMap, VecDeque};
use std::sync::Mutex;

/// A reply function gets the request and returns the reply text.
type Responder = Box<dyn Fn(&ChatRequest) -> String + Send + Sync>;

#[derive(Default)]
pub struct Scripted {
    queues: Mutex<HashMap<String, VecDeque<String>>>,
    shared: Mutex<VecDeque<String>>,
    responder: Option<Responder>,
    pub seen: Mutex<Vec<ChatRequest>>,
}

impl Scripted {
    pub fn new() -> Scripted {
        Scripted::default()
    }

    /// Replies for any model, in order.
    pub fn replies(self, rs: impl IntoIterator<Item = impl Into<String>>) -> Scripted {
        self.shared.lock().unwrap().extend(rs.into_iter().map(Into::into));
        self
    }

    /// Replies for one model id (checked before the shared queue).
    pub fn for_model(self, model: &str, rs: impl IntoIterator<Item = impl Into<String>>) -> Scripted {
        self.queues.lock().unwrap().entry(model.to_string()).or_default()
            .extend(rs.into_iter().map(Into::into));
        self
    }

    /// Compute replies from the request (when order across threads varies).
    pub fn respond(mut self, f: impl Fn(&ChatRequest) -> String + Send + Sync + 'static) -> Scripted {
        self.responder = Some(Box::new(f));
        self
    }

    pub fn calls(&self) -> usize {
        self.seen.lock().unwrap().len()
    }
}

/// What a strict chat API refuses: after the system prompt, turns must
/// alternate user / assistant, start with the user, end with the user (it
/// is asked to reply), and none may be empty. A fake that accepts what the
/// real one wouldn't hides bugs.
pub fn shape_problem(req: &ChatRequest) -> Option<String> {
    let turns: Vec<&serde_json::Value> = req.messages.iter().skip_while(|m| m["role"] == "system").collect();
    if turns.iter().any(|m| m["role"] == "system") {
        return Some("a system message after the conversation started".into());
    }
    let mut want = "user";
    for (i, m) in turns.iter().enumerate() {
        if m["role"] != want {
            return Some(format!("message {i} is {} where a {want} turn belongs (roles must alternate)", m["role"]));
        }
        let empty = match &m["content"] {
            serde_json::Value::String(t) => t.trim().is_empty(),
            serde_json::Value::Array(a) => a.is_empty(),
            _ => true,
        };
        if empty {
            return Some(format!("message {i} ({want}) is empty"));
        }
        want = if want == "user" { "assistant" } else { "user" };
    }
    (want != "assistant").then(|| "the last message isn't the user's".into())
}

impl ChatModel for Scripted {
    fn chat(&self, req: &ChatRequest, sink: &mut dyn Sink) -> Result<Outcome, ModelError> {
        self.seen.lock().unwrap_or_else(|e| e.into_inner()).push(req.clone());
        if let Some(p) = shape_problem(req) {
            return Err(ModelError::Http { status: 400, message: format!("invalid messages: {p}") });
        }
        if sink.should_stop() {
            return Err(ModelError::Stopped);
        }
        // no lock is held while the responder runs: agents' calls go on in parallel, as on the real API
        let queued = self.queues.lock().unwrap_or_else(|e| e.into_inner()).get_mut(&req.model).and_then(|q| q.pop_front());
        let queued = queued.or_else(|| self.shared.lock().unwrap_or_else(|e| e.into_inner()).pop_front());
        let text = queued.or_else(|| self.responder.as_ref().map(|f| f(req))).unwrap_or_else(|| "Done.".to_string());
        // `@call name {json}` lines are function calls, as the API returns them when tools were offered
        let mut calls = Vec::new();
        let text = if req.tools.is_some() && text.contains("@call ") {
            let mut kept = Vec::new();
            for line in text.lines() {
                match line.trim_start().strip_prefix("@call ").and_then(|r| r.split_once(' ')) {
                    Some((name, args)) => calls.push((name.to_string(), args.trim().to_string())),
                    None => kept.push(line),
                }
            }
            kept.join("\n")
        } else {
            text
        };
        let chars: Vec<char> = text.chars().collect();
        for chunk in chars.chunks(40) {
            sink.delta(&chunk.iter().collect::<String>());
        }
        Ok(Outcome {
            usage: Some(Usage { prompt: (req.sent_chars() / 4) as u64 + 50, completion: (text.len() / 4) as u64 + 5,
                                cached: 0, reasoning: 0 }),
            text, reasoning: String::new(), finish: Some(if calls.is_empty() { "stop" } else { "tool_calls" }.into()),
            model: req.model.clone(), calls,
        })
    }
}
