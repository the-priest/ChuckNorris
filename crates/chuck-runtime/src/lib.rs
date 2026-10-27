//! chuck-runtime — the fleet at work. One request becomes a job: the Lead
//! (a kernel process) writes criteria, delegates to DeepSeek and GLM workers
//! (child processes, each on its own thread and its own copy of the
//! project), reviews, merges through the gates, verifies, and finishes only
//! through the Done gate and an independent critic pass.

pub mod agent;
pub mod chat;
pub mod events;
pub mod exec;
pub mod lead;
pub mod prompts;
pub mod setup;

pub use agent::{Ctx, Halt};
pub use events::{Event, Events, Outcome, TaskView};

use chuck_core::{AgentId, DoneGate, FleetConfig, Kernel, Quota, Role, SpawnSpec};
use chuck_model::{ChatModel, Reasoning};
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
    /// The project folder's access (settings `folder_access`): the lead's
    /// commands follow it. Workers always work jailed in their own copy.
    pub access: chuck_sandbox::Access,
    /// Use bubblewrap (when installed).
    pub bwrap: bool,
    /// Where workers' copies live.
    pub store: PathBuf,
    /// Model calls in flight at once (1 lead + 4 + 4).
    pub slots: usize,
    /// The evidence ledger every command is recorded in (the window's).
    pub audit: Option<Arc<chuck_store::Ledger>>,
    /// The day/month spend every model call is booked in, and its limits
    /// (0 = none): over a limit, no agent gets another call.
    pub spend: Option<Arc<chuck_store::Spend>>,
    pub day_limit: f64,
    pub month_limit: f64,
    /// Where the folder's checkpoints go (the window's store): the lead
    /// snapshots before every step that acts. None: no checkpoints.
    pub checkpoints: Option<PathBuf>,
    /// Web search and page reads for every agent (None: no web this run).
    pub web: Option<Arc<chuck_web::Web>>,
    /// Results per search.
    pub sources: usize,
    /// MCP servers (the app's, running for as long as it does).
    pub mcp: Option<Arc<chuck_mcp::Manager>>,
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
            // generous: work ends when it's done (or out of budget), not on a step count
            lead_steps: 200,
            worker_steps: 120,
            cmd_timeout: Duration::from_secs(300),
            context_chars: 360_000,
            job_quota: Quota { tokens: Some(4_000_000), usd: Some(2.0), seconds: None },
            worker_quota: Quota::tokens(900_000),
            access: chuck_sandbox::Access::Full,
            bwrap: chuck_sandbox::bwrap().is_some(),
            store: default_store(),
            slots: 9,
            audit: None,
            spend: None,
            day_limit: 0.0,
            month_limit: 0.0,
            checkpoints: None,
            web: None,
            sources: 6,
            mcp: None,
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
    /// What the user types while the fleet works: the Lead reads it before
    /// its next step (and stops waiting on workers to read it).
    pub steer: Option<Receiver<String>>,
    pub stop: Arc<AtomicBool>,
}

impl Job {
    pub fn new(ask: impl Into<String>, folder: impl AsRef<Path>) -> Job {
        Job { ask: ask.into(), folder: folder.as_ref().to_path_buf(), history: Vec::new(), answers: None, steer: None,
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
    /// The checkpoint taken before the job touched anything: `rewind` to it
    /// undoes the whole job.
    pub checkpoint: Option<String>,
}

pub struct Runtime {
    pub kernel: Arc<Kernel>,
    pub model: Arc<dyn ChatModel>,
    pub cfg: RuntimeConfig,
    pub events: Events,
}

pub(crate) fn first_message(job: &Job) -> String {
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

/// Copies left behind by a job whose process is gone (a crash, a power cut):
/// nothing will ever merge them, so they only take space.
fn sweep_stale(store: &Path) {
    let Ok(rd) = std::fs::read_dir(store) else { return };
    let me = std::process::id();
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        let Some(pid) = name.strip_prefix("job-").and_then(|r| r.split('-').next()).and_then(|p| p.parse::<u32>().ok())
        else { continue };
        if pid != me && !Path::new(&format!("/proc/{pid}")).exists() {
            let _ = std::fs::remove_dir_all(e.path());
        }
    }
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
                                   tokens: 0, usd: 0.0, checkpoint: None };
            }
        };
        sweep_stale(&self.cfg.store);
        let ctx = Ctx { kernel: self.kernel.clone(), model: self.model.clone(), cfg: self.cfg.clone(),
                        events: self.events.clone(), stop: job.stop.clone() };
        let lead = self.kernel.spawn(SpawnSpec::new("lead", Role::Lead).quota(self.cfg.job_quota)
            .brief(chuck_core::evidence::clip(&job.ask, 160)));
        self.events.emit(Event::AgentStarted { id: lead, role: Role::Lead, name: "lead".into(),
                                               model: self.cfg.fleet.deepseek_model.clone(), task: None });
        let first = first_message(&job);
        let mut run = lead::LeadRun::new(&ctx, lead, &folder, &job.ask, job.answers);
        run.steer = job.steer;
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
        let checkpoint = run.first_checkpoint.clone();
        self.events.emit(Event::Finished(outcome.clone()));
        JobReport { outcome, gate, lead, tokens, usd, checkpoint }
    }
}
