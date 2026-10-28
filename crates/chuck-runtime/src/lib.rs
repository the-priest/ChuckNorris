//! chuck-runtime — one request becomes a job: the Lead (a kernel process,
//! one model) writes criteria, builds, verifies, and finishes only through
//! the Done gate.

pub mod agent;
pub mod browse;
pub mod chat;
pub mod daemon;
pub mod events;
pub mod exec;
pub mod lead;
pub mod prompts;
pub mod resume;
pub mod schemas;

/// Write a snag down and tell whoever watches (the panel's DEBUG section).
pub fn snag(events: &events::Events, id: chuck_core::AgentId, who: &str, kind: &str, text: &str) {
    let text = chuck_core::evidence::clip(text.trim(), 400);
    chuck_store::snags::record(who, kind, &text);
    events.emit(events::Event::Snag { id, who: who.to_string(), kind: kind.to_string(), text });
}
pub mod setup;

pub use agent::{Ctx, Halt};
pub use events::{Choice, Event, Events, Outcome, Picture, Video};

use chuck_core::{AgentId, DoneGate, Kernel, Models, Quota, Role, SpawnSpec};
use chuck_model::{ChatModel, Reasoning};
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;
use std::sync::mpsc::Receiver;
use std::sync::Arc;
use std::time::Duration;

#[derive(Clone)]
pub struct RuntimeConfig {
    pub models: Models,
    pub reasoning: Reasoning,
    /// How long the Lead waits for an answer to its question before going on.
    pub ask_wait: Duration,
    /// Offer agents their tools as functions the API parses (see `schemas`);
    /// off, they write every call as a fenced block.
    pub native_tools: bool,
    /// Debug mode: the agents are asked to note whatever slows them down,
    /// limits them or goes wrong (```snag```), for it to be fixed.
    pub debug: bool,
    /// A reply's ceiling (not its size: only what's written is billed). A
    /// reply cut short is continued.
    pub max_tokens: u32,
    pub lead_steps: usize,
    pub cmd_timeout: Duration,
    /// How long an agent waits out an API or network outage (retrying with
    /// backoff) before the job pauses.
    pub outage_wait: Duration,
    /// A conversation is trimmed past this many characters.
    pub context_chars: usize,
    /// The whole job.
    pub job_quota: Quota,
    /// The project folder's access (settings `folder_access`): the Lead's
    /// commands follow it.
    pub access: chuck_sandbox::Access,
    /// Use bubblewrap (when installed).
    pub bwrap: bool,
    /// Where a job keeps its scratch files (the folder's starting snapshot).
    pub store: PathBuf,
    /// Model calls in flight at once.
    pub slots: usize,
    /// The evidence ledger every command is recorded in (the window's).
    pub audit: Option<Arc<chuck_store::Ledger>>,
    /// The day/month spend every model call is booked in, and its limits
    /// (0 = none): over a limit, no agent gets another call.
    pub spend: Option<Arc<chuck_store::Spend>>,
    pub day_limit: f64,
    pub month_limit: f64,
    /// Read the day/month limits from the settings before every model call
    /// (the window's and `chuck`'s jobs), not once when the job starts.
    pub live_limits: bool,
    /// Where the folder's checkpoints go (the window's store): the lead
    /// snapshots before every step that acts. None: no checkpoints.
    pub checkpoints: Option<PathBuf>,
    /// Web search and page reads for every agent (None: no web this run).
    pub web: Option<Arc<chuck_web::Web>>,
    /// Results per search.
    pub sources: usize,
    /// MCP servers (the app's, running for as long as it does).
    pub mcp: Option<Arc<chuck_mcp::Manager>>,
    /// Language servers for def/refs/rename (the lead's folder only).
    pub lsp: Option<Arc<chuck_code::Manager>>,
    /// Long-term memory: recalled into the lead's and Chat's prompts, kept
    /// with ```remember``` / ```forget```.
    pub memory: Option<Arc<chuck_store::memory::Memory>>,
    /// The lead's ```browse``` drives this browser ("auto", "brave",
    /// "chromium", "chrome" or a path; None: no browse this run).
    pub browser: Option<String>,
    /// Where a paused job is kept so it can carry on later (None: not kept).
    pub resume_dir: Option<PathBuf>,
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
            models: Models::default(),
            reasoning: Reasoning::default(),
            ask_wait: crate::lead::ASK_WAIT,
            native_tools: true,
            debug: false,
            // a reply's ceiling, not its size (only what's written is billed): a big file fits in one
            max_tokens: 32768,
            // no real cap: work ends when it's done, out of budget or stuck — never on a step count
            lead_steps: 1_000_000,
            // long builds and test suites take time; Stop ends any command at once
            cmd_timeout: Duration::from_secs(1800),
            outage_wait: Duration::from_secs(6 * 3600),
            context_chars: 360_000,
            // money is the job's limit (the user sets it)
            job_quota: Quota { tokens: None, usd: Some(2.0), seconds: None },
            access: chuck_sandbox::Access::Full,
            bwrap: chuck_sandbox::bwrap().is_some(),
            store: default_store(),
            slots: 4, // the Lead, the eyes, a daemon
            audit: None,
            spend: None,
            day_limit: 0.0,
            month_limit: 0.0,
            live_limits: false,
            checkpoints: None,
            web: None,
            sources: 6,
            mcp: None,
            lsp: None,
            memory: None,
            browser: None,
            resume_dir: None,
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
    /// What the user types while Chuck works: the Lead reads it before its
    /// next step.
    pub steer: Option<Receiver<String>>,
    pub stop: Arc<AtomicBool>,
    /// Carry on a paused job (`ask` is then what the user adds, if anything).
    pub resume: Option<resume::Resume>,
    /// Screenshots the user pasted with the request (PNG files): the vision
    /// model describes each before the work starts.
    pub images: Vec<PathBuf>,
}

impl Job {
    pub fn new(ask: impl Into<String>, folder: impl AsRef<Path>) -> Job {
        Job { ask: ask.into(), folder: folder.as_ref().to_path_buf(), history: Vec::new(), answers: None, steer: None,
              stop: Arc::new(AtomicBool::new(false)), resume: None, images: Vec::new() }
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
    /// A paused job's state: run it again with `Job::resume` to carry on.
    pub resume: Option<resume::Resume>,
}

pub struct Runtime {
    pub kernel: Arc<Kernel>,
    pub model: Arc<dyn ChatModel>,
    pub cfg: RuntimeConfig,
    pub events: Events,
}

/// What the memory store is asked about: the request with the chat leading
/// up to it.
pub(crate) fn first_message(job: &Job) -> String {
    let mut q: Vec<&str> = job.history.iter().rev().take(2).map(|(_, t)| t.as_str()).collect();
    q.reverse();
    q.push(&job.ask);
    q.join("\n")
}

/// The chat before this request as real turns (true: the user's), newest
/// kept within a budget, each clipped; consecutive turns of one side are
/// joined so the roles alternate, starting with the user and ending with
/// Chuck, so the request is the next user turn.
pub(crate) fn turns(history: &[(String, String)]) -> Vec<(bool, String)> {
    let mut kept: Vec<(bool, String)> = Vec::new();
    let mut size = 0;
    for (role, text) in history.iter().rev() {
        let text = text.trim();
        if text.is_empty() {
            continue;
        }
        let text = chuck_core::evidence::clip(text, 3000);
        if size + text.len() > 24_000 {
            break;
        }
        size += text.len();
        kept.push((matches!(role.as_str(), "user" | "you"), text));
    }
    kept.reverse();
    let mut out: Vec<(bool, String)> = Vec::new();
    for (user, text) in kept {
        match out.last_mut() {
            Some((u, t)) if *u == user => {
                t.push_str("\n\n");
                t.push_str(&text);
            }
            _ => out.push((user, text)),
        }
    }
    while out.first().is_some_and(|(u, _)| !u) {
        out.remove(0);
    }
    if out.last().is_some_and(|(u, _)| *u) {
        // the user spoke and got no answer (stopped, or it failed)
        out.push((false, "(no answer: that turn was stopped before Chuck replied)".into()));
    }
    out
}

/// Scratch left behind by a job whose process is gone (a crash, a power
/// cut): nothing will ever use it, so it only takes space.
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
                                   tokens: 0, usd: 0.0, checkpoint: None, resume: None };
            }
        };
        sweep_stale(&self.cfg.store);
        if let Some(lsp) = &self.cfg.lsp {
            // language servers run as the folder's commands do (some run project code)
            let access = self.cfg.access;
            let wrap: Option<chuck_code::lsp::Wrap> = match access {
                chuck_sandbox::Access::Full => None,
                chuck_sandbox::Access::Guarded if !self.cfg.bwrap => None,
                chuck_sandbox::Access::Guarded => Some(chuck_sandbox::Jail::new(&folder).guarded().wrap()),
                _ if !self.cfg.bwrap => Some(Arc::new(|_| None)),
                chuck_sandbox::Access::SandboxNet => Some(chuck_sandbox::Jail::new(&folder).wrap()),
                chuck_sandbox::Access::Sandbox => Some(chuck_sandbox::Jail::new(&folder).offline().wrap()),
                chuck_sandbox::Access::ReadOnly => Some(chuck_sandbox::Jail::new(&folder).read_only().offline().wrap()),
            };
            lsp.set_wrap(&format!("{}\0{}", folder.display(), access.label()), wrap);
        }
        let ctx = Ctx { kernel: self.kernel.clone(), model: self.model.clone(), cfg: self.cfg.clone(),
                        events: self.events.clone(), stop: job.stop.clone() };
        let lead = self.kernel.spawn(SpawnSpec::new("lead", Role::Lead).quota(self.cfg.job_quota)
            .brief(chuck_core::evidence::clip(&job.ask, 160)));
        self.events.emit(Event::AgentStarted { id: lead, role: Role::Lead, name: "lead".into(),
                                               model: self.cfg.models.main.clone(), task: None });
        let resumed = job.resume.clone();
        // carrying on: the user's words are only what they add now (and what they pasted, described)
        let first = if job.images.is_empty() { job.ask.clone() } else {
            format!("{}\n\n{}", job.ask, browse::describe_pasted(&ctx, lead, &job.images))
        };
        let history = if resumed.is_some() { Vec::new() } else { turns(&job.history) };
        let ask = resumed.as_ref().map_or(job.ask.clone(), |r| r.ask.clone());
        let mut run = lead::LeadRun::new(&ctx, lead, &folder, &ask, job.answers);
        run.steer = job.steer;
        run.resume = job.resume;
        let outcome = run.run(&first, &history);
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
        // a paused job can carry on later, even after Chuck was closed; a finished one is done with
        let resume = match &outcome {
            Outcome::Paused { gaps } if run.mutated() || !gate.criteria.is_empty() => {
                let (convo, head) = if run.last_msgs.len() >= 2 { (run.last_msgs.clone(), run.last_head) } else {
                    (vec![serde_json::json!({"role": "system", "content": ""}), serde_json::json!({"role": "user", "content": first})], 1)
                };
                let r = resume::Resume {
                    ask: ask.clone(), folder: folder.display().to_string(), convo, head, gate: gate.clone(),
                    mutated: run.mutated(), checkpoint: checkpoint.clone(),
                    why: run.paused_why.clone().unwrap_or_else(|| gaps.first().cloned().unwrap_or_default()),
                    gaps: gaps.clone(), spent_usd: resumed.as_ref().map_or(0.0, |r| r.spent_usd) + usd,
                    runs: resumed.as_ref().map_or(0, |r| r.runs + 1), saved_at: chuck_sandbox::now_secs() as f64,
                };
                if let Some(d) = &self.cfg.resume_dir {
                    resume::save_in(d, &r);
                }
                Some(r)
            }
            Outcome::Done { .. } => {
                if let Some(d) = &self.cfg.resume_dir {
                    resume::clear_in(d, &folder);
                }
                None
            }
            // an answer (or a failure to start) leaves an earlier paused job as it was
            _ => None,
        };
        self.events.emit(Event::Finished(outcome.clone()));
        JobReport { outcome, gate, lead, tokens, usd, checkpoint, resume }
    }
}
