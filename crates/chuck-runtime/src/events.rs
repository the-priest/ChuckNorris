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
    /// `outside`: the paths outside the folder it reaches (then the user may
    /// also allow them for the rest of the job).
    Confirm { id: AgentId, command: String, why: String, outside: Vec<String> },
    /// One model call's bill: all tokens, the prompt part of them, and how
    /// much of the prompt the API's cache served (billed at a fraction).
    Usage { id: AgentId, tokens: u64, usd: f64, prompt: u64, cached: u64 },
    AgentEnded { id: AgentId, status: String },
    /// The lead's plan as it stands: (step, done).
    Plan { steps: Vec<(String, bool)> },
    /// Every task of the job, whenever one changes.
    Tasks(Vec<TaskView>),
    /// A message on the team's channel (to: one reader, or everyone).
    Team { from: String, to: Option<String>, text: String },
    /// Something got in an agent's way (debug: see `chuck_store::snags`).
    Snag { id: AgentId, who: String, kind: String, text: String },
    /// A command started (it ends with an Action for the same agent).
    Running { id: AgentId, tool: String, cmd: String },
    /// Chat mode: something to be done on the machine, for the fleet — sent
    /// only if the user says so.
    Handoff { request: String },
    /// Chat mode: a reminder ("remind") or a daemon ("report") to keep —
    /// already checked, for whoever holds the daemons to add.
    Schedule { kind: String, when: serde_json::Value, what: String },
    /// The lead looked at a page in the browser.
    Browsed { id: AgentId, url: String, title: String, size: (u32, u32), ms: u64, engine: String, errors: Vec<String>,
              failed: Vec<String>, warnings: Vec<String>, steps: Vec<String>, refused: Vec<String>,
              /// The screenshot, saved.
              shot: Option<std::path::PathBuf>,
              /// What the vision model saw in it.
              look: Option<String> },
    /// Chat mode: pictures found for the user (downloaded and checked).
    Gallery { query: String, items: Vec<Picture> },
    /// Chat mode: videos found for the user.
    Videos { query: String, items: Vec<Video> },
    Finished(Outcome),
}

/// A picture on disk, and where it came from.
#[derive(Clone, Debug, PartialEq)]
pub struct Picture {
    pub path: std::path::PathBuf,
    pub src: String,
    pub page: String,
    pub title: String,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Video {
    pub title: String,
    pub url: String,
    /// Its poster, downloaded.
    pub thumb: Option<std::path::PathBuf>,
    pub length: Option<String>,
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
