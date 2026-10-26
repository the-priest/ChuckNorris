//! What the runtime tells the outside world (the window, `chuck run`, logs).

use chuck_core::{AgentId, Role};
use std::sync::mpsc::Sender;

#[derive(Clone, Debug, PartialEq)]
pub enum Outcome {
    /// Through every gate.
    Done { summary: String },
    /// A plain answer to a question: nothing was changed, nothing claimed done.
    Answered { text: String },
    /// Out of budget, stopped, or out of steps: what is still missing.
    Paused { gaps: Vec<String> },
    Failed { error: String },
}

#[derive(Clone, Debug, PartialEq)]
pub enum Event {
    AgentStarted { id: AgentId, role: Role, name: String, model: String, task: Option<String> },
    Thinking { id: AgentId, chunk: String },
    Delta { id: AgentId, chunk: String },
    /// The prose of a reply (tool blocks removed).
    Said { id: AgentId, text: String },
    Action { id: AgentId, tool: String, arg: String, ok: bool, summary: String },
    Output { id: AgentId, chunk: String },
    Gate { id: AgentId, task: Option<String>, phase: String, unmet: Vec<String> },
    Assigned { task: String, title: String, worker: AgentId, role: Role },
    Submitted { task: String, worker: AgentId, lines: usize },
    Reviewed { task: String, accepted: bool, defects: Vec<String> },
    Merged { task: String, applied: usize, merged: usize, conflicts: Vec<String>, refused: Vec<String> },
    Notice { id: AgentId, text: String },
    Ask { id: AgentId, question: String },
    /// A critical command waits for the user's explicit yes.
    Confirm { id: AgentId, command: String, why: String },
    Usage { id: AgentId, tokens: u64, usd: f64 },
    AgentEnded { id: AgentId, status: String },
    Finished(Outcome),
}

#[derive(Clone)]
pub struct Events(pub Option<Sender<Event>>);

impl Events {
    pub fn none() -> Events {
        Events(None)
    }
    pub fn emit(&self, e: Event) {
        if let Some(tx) = &self.0 {
            let _ = tx.send(e);
        }
    }
}
