//! chuck-runtime — the fleet at work. One request becomes a job: the Lead
//! (a kernel process) writes criteria, delegates to DeepSeek and GLM workers
//! (child processes, each on its own thread and its own copy of the
//! project), reviews, merges through the gates, verifies, and finishes only
//! through the Done gate and an independent critic pass.

pub mod agent;
pub mod events;
pub mod exec;
pub mod lead;
pub mod prompts;

pub use agent::{Ctx, Halt};
pub use events::{Event, Events, Outcome};

use chuck_core::{AgentId, DoneGate, FleetConfig, Kernel, Quota, Role, SpawnSpec};
use chuck_model::{ChatModel, Reasoning};
use chuck_tools::Wrap;
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;
use std::sync::mpsc::Receiver;
use std::sync::Arc;
use std::time::Duration;

#[derive(Clone)]
pub struct RuntimeConfig {
    pub fleet: FleetConfig,
    pub reasoning: Reasoning,
    pub max_tokens: u32,
    pub lead_steps: usize,
    pub worker_steps: usize,
    pub cmd_timeout: Duration,
    /// A conversation is trimmed past this many characters.
    pub context_chars: usize,
    /// The whole job: the Lead and everything under it.
    pub job_quota: Quota,
    /// Each worker (also clipped to what the job has left).
    pub worker_quota: Quota,
    /// The sandbox around every command (None: run directly).
    pub wrap: Option<Wrap>,
    /// Where workers' copies live.
    pub store: PathBuf,
    /// Model calls in flight at once (1 lead + 4 + 4).
    pub slots: usize,
}

pub fn default_store() -> PathBuf {
    let cache = std::env::var_os("XDG_CACHE_HOME").map(PathBuf::from).filter(|p| p.is_absolute())
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".cache")))
        .unwrap_or_else(std::env::temp_dir);
    cache.join("chuck").join("work")
}

impl Default for RuntimeConfig {
    fn default() -> RuntimeConfig {
        RuntimeConfig {
            fleet: FleetConfig::default(),
            reasoning: Reasoning::default(),
            max_tokens: 8192,
            lead_steps: 80,
            worker_steps: 50,
            cmd_timeout: Duration::from_secs(300),
            context_chars: 360_000,
            job_quota: Quota { tokens: Some(4_000_000), usd: Some(2.0), seconds: None },
            worker_quota: Quota::tokens(900_000),
            wrap: None,
            store: default_store(),
            slots: 9,
        }
    }
}

/// One request from the user.
pub struct Job {
    pub ask: String,
    pub folder: PathBuf,
    /// Earlier turns of this chat, (role, text), oldest first.
    pub history: Vec<(String, String)>,
    /// Answers to the Lead's ```ask``` (None: nobody is there to answer).
    pub answers: Option<Receiver<String>>,
    pub stop: Arc<AtomicBool>,
}

impl Job {
    pub fn new(ask: impl Into<String>, folder: impl AsRef<Path>) -> Job {
        Job { ask: ask.into(), folder: folder.as_ref().to_path_buf(), history: Vec::new(), answers: None,
              stop: Arc::new(AtomicBool::new(false)) }
    }
}

#[derive(Clone, Debug)]
pub struct JobReport {
    pub outcome: Outcome,
    /// The job's gate: criteria, evidence, verdicts (what the UI shows).
    pub gate: DoneGate,
    pub lead: AgentId,
    pub tokens: u64,
    pub usd: f64,
}

pub struct Runtime {
    pub kernel: Arc<Kernel>,
    pub model: Arc<dyn ChatModel>,
    pub cfg: RuntimeConfig,
    pub events: Events,
}

fn first_message(job: &Job) -> String {
    if job.history.is_empty() {
        return job.ask.clone();
    }
    let mut earlier = String::new();
    for (role, text) in job.history.iter().rev() {
        let line = format!("{role}: {}\n\n", chuck_core::evidence::clip(text, 3000));
        if earlier.len() + line.len() > 24_000 {
            break;
        }
        earlier.insert_str(0, &line);
    }
    format!("EARLIER IN THIS CHAT:\n{earlier}NOW:\n{}", job.ask)
}

impl Runtime {
    pub fn new(model: Arc<dyn ChatModel>, cfg: RuntimeConfig, events: Events) -> Runtime {
        let kernel = Arc::new(Kernel::new(cfg.slots));
        Runtime { kernel, model, cfg, events }
    }

    /// Run one job to its end: done, answered, paused (with what's missing)
    /// or failed. Blocks; call it from its own thread in a UI.
    pub fn run(&self, job: Job) -> JobReport {
        let folder = match job.folder.canonicalize() {
            Ok(f) if f.is_dir() => f,
            _ => {
                let outcome = Outcome::Failed { error: format!("no such folder: {}", job.folder.display()) };
                self.events.emit(Event::Finished(outcome.clone()));
                return JobReport { outcome, gate: DoneGate::new(job.ask, chuck_core::Scale::Small), lead: AgentId(0),
                                   tokens: 0, usd: 0.0 };
            }
        };
        let ctx = Ctx { kernel: self.kernel.clone(), model: self.model.clone(), cfg: self.cfg.clone(),
                        events: self.events.clone(), stop: job.stop.clone() };
        let lead = self.kernel.spawn(SpawnSpec::new("lead", Role::Lead).quota(self.cfg.job_quota)
            .brief(chuck_core::evidence::clip(&job.ask, 160)));
        self.events.emit(Event::AgentStarted { id: lead, role: Role::Lead, name: "lead".into(),
                                               model: self.cfg.fleet.deepseek_model.clone(), task: None });
        let first = first_message(&job);
        let mut run = lead::LeadRun::new(&ctx, lead, &folder, &job.ask, job.answers);
        let outcome = run.run(&first);
        let status = match &outcome {
            Outcome::Done { .. } | Outcome::Answered { .. } => 0,
            _ => 1,
        };
        let label = match &outcome {
            Outcome::Done { .. } => "done",
            Outcome::Answered { .. } => "answered",
            Outcome::Paused { .. } => "INCOMPLETE",
            Outcome::Failed { .. } => "failed",
        };
        self.kernel.exit(lead, status, Some(label.into()));
        self.events.emit(Event::AgentEnded { id: lead, status: label.into() });
        let (tokens, usd) = self.kernel.info(lead).map(|p| (p.tokens, p.usd)).unwrap_or_default();
        let gate = run.gate.lock().unwrap().clone();
        self.events.emit(Event::Finished(outcome.clone()));
        JobReport { outcome, gate, lead, tokens, usd }
    }
}
