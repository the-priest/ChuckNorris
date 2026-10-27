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
    /// The job's criteria as they stand: (id, text, verify, evidence labels).
    Board { criteria: Vec<(String, String, Option<String>, Vec<String>)> },
    Assigned { task: String, title: String, worker: AgentId, role: Role },
    Submitted { task: String, worker: AgentId, lines: usize },
    Reviewed { task: String, accepted: bool, defects: Vec<String> },
    Merged { task: String, applied: usize, merged: usize, conflicts: Vec<String>, refused: Vec<String> },
    Notice { id: AgentId, text: String },
    Ask { id: AgentId, question: String },
    /// A critical command waits for the user's explicit yes.
    Confirm { id: AgentId, command: String, why: String },
    /// One model call's bill: all tokens, the prompt part of them, and how
    /// much of the prompt the API's cache served (billed at a fraction).
    Usage { id: AgentId, tokens: u64, usd: f64, prompt: u64, cached: u64 },
    AgentEnded { id: AgentId, status: String },
    /// The lead's plan as it stands: (step, done).
    Plan { steps: Vec<(String, bool)> },
    /// Every task of the job, whenever one changes.
    Tasks(Vec<TaskView>),
    /// A command started (it ends with an Action for the same agent).
    Running { id: AgentId, tool: String, cmd: String },
    Finished(Outcome),
}

/// A task as the panel shows it.
#[derive(Clone, Debug, PartialEq)]
pub struct TaskView {
    pub id: String,
    pub title: String,
    /// "queued", "running", "review", "merged" or "failed".
    pub state: String,
    pub workers: Vec<String>,
    pub deps: Vec<String>,
    pub criteria: Vec<String>,
    /// Why it failed, or what was sent back last.
    pub note: String,
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
