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

impl ChatModel for Scripted {
    fn chat(&self, req: &ChatRequest, sink: &mut dyn Sink) -> Result<Outcome, ModelError> {
        self.seen.lock().unwrap().push(req.clone());
        if sink.should_stop() {
            return Err(ModelError::Stopped);
        }
        let text = self.queues.lock().unwrap().get_mut(&req.model).and_then(|q| q.pop_front())
            .or_else(|| self.shared.lock().unwrap().pop_front())
            .or_else(|| self.responder.as_ref().map(|f| f(req)))
            .unwrap_or_else(|| "Done.".to_string());
        for chunk in text.as_bytes().chunks(40) {
            sink.delta(&String::from_utf8_lossy(chunk));
        }
        Ok(Outcome {
            usage: Some(Usage { prompt: (req.sent_chars() / 4) as u64 + 50, completion: (text.len() / 4) as u64 + 5,
                                cached: 0, reasoning: 0 }),
            text, reasoning: String::new(), finish: Some("stop".into()), model: req.model.clone(),
        })
    }
}
