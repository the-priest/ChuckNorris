//! One agent's life: think (a model call through the kernel's scheduler),
//! act (its blocks through the guard and the gate), hear back, repeat — and
//! for a worker, hand the result to the lead and wait for the verdict.

use crate::events::{Event, Events};
use crate::exec::{Exec, Sandbox, WORKER_TAGS};
use crate::prompts;
use crate::RuntimeConfig;
use chuck_core::{AgentId, DoneGate, Kernel, Progress, Role, Stall, Submission, Task, Unmet};
use chuck_model::{price_for, ChatModel, ChatRequest, ModelError, Notice, Sink, Usage};
use chuck_tools::Workspace;
use serde_json::{json, Value};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{Receiver, RecvTimeoutError, Sender};
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// What every agent of a job shares.
#[derive(Clone)]
pub struct Ctx {
    pub kernel: Arc<Kernel>,
    pub model: Arc<dyn ChatModel>,
    pub cfg: RuntimeConfig,
    pub events: Events,
    /// The user's Stop: every agent of the job, and the commands they run.
    pub stop: Arc<AtomicBool>,
}

/// Why an agent's thinking ended early.
#[derive(Clone, Debug, PartialEq)]
pub enum Halt {
    /// The kernel ended it: a quota (its own or an ancestor's), or a KILL.
    Killed(String),
    /// The user pressed Stop.
    Stopped,
    /// The API failed after its own retries.
    Model(String),
}

impl Halt {
    pub fn describe(&self) -> String {
        match self {
            Halt::Killed(why) => format!("stopped by the kernel: {why}"),
            Halt::Stopped => "stopped by you".into(),
            Halt::Model(e) => e.clone(),
        }
    }
}

/// A conversation that stays inside the context window: the system prompt
/// and the first message (the task) are kept; the oldest exchanges after
/// them go first.
#[derive(Clone, Debug, Default)]
pub struct Convo {
    pub msgs: Vec<Value>,
    /// Characters of old output folded away to keep calls cheap.
    pub elided: usize,
    /// The task as given, kept apart so trimming notes never touch it.
    first: String,
    trimmed: usize,
    /// Where the task is: after the system prompt and any earlier turns of
    /// the conversation it continues. Nothing at or before it is trimmed.
    head: usize,
}

/// Turns kept whole at the end of a conversation.
const KEEP_RECENT: usize = 6;
const ELIDED: &str = "elided to keep the context small";
const TRIM_NOTE_END: &str = "earlier messages were trimmed to fit the context; the gate keeps every piece of evidence, so \
    re-read files rather than relying on memory]";

/// Keep the head and the tail of an over-long message (errors live at the
/// end, what was asked lives at the start).
fn squeeze(s: &str, max: usize) -> String {
    if s.len() <= max {
        return s.to_string();
    }
    let keep = max.saturating_sub(80) / 2;
    let mut a = keep;
    while !s.is_char_boundary(a) {
        a -= 1;
    }
    let mut b = s.len() - keep;
    while !s.is_char_boundary(b) {
        b += 1;
    }
    format!("{}\n… ({} characters left out to fit the context) …\n{}", &s[..a], b - a, &s[b..])
}

impl Convo {
    pub fn new(system: &str, first: &str) -> Convo {
        Convo::with_history(system, &[], first)
    }

    /// The task as the next turn of a conversation: the earlier turns go in
    /// as the real messages they were (a transcript pasted into one message
    /// gets imitated: the model writes the user's next line itself).
    pub fn with_history(system: &str, history: &[(bool, String)], first: &str) -> Convo {
        let mut msgs = vec![json!({"role": "system", "content": system})];
        for (user, text) in history {
            msgs.push(json!({"role": if *user { "user" } else { "assistant" }, "content": text}));
        }
        msgs.push(json!({"role": "user", "content": first}));
        let head = msgs.len() - 1;
        Convo { msgs, elided: 0, first: first.to_string(), trimmed: 0, head }
    }

    /// A conversation as it was saved (a paused job carrying on); `head`:
    /// where its task is.
    pub fn restore(msgs: Vec<Value>, head: usize) -> Convo {
        let head = head.clamp(1, msgs.len().saturating_sub(1).max(1));
        let mut first = msgs.get(head).and_then(|m| m["content"].as_str()).unwrap_or("").to_string();
        // the trimming note is the conversation's, not the task's: taken off,
        // and its count kept, so a later trim says the total once
        let mut trimmed = 0;
        if let Some(at) = first.rfind("\n\n[") {
            let note = &first[at + 3..];
            if let Some(n) = note.strip_suffix(TRIM_NOTE_END).and_then(|n| n.split(' ').next()).and_then(|n| n.parse().ok()) {
                trimmed = n;
                first.truncate(at);
            }
        }
        Convo { msgs, elided: 0, first, trimmed, head }
    }

    /// Where the task is (see `restore`).
    pub fn head(&self) -> usize {
        self.head
    }

    pub fn user(&mut self, text: &str) {
        self.push("user", text);
    }

    pub fn assistant(&mut self, text: &str) {
        self.push("assistant", text);
    }

    /// Turns alternate (a strict API refuses two of one side in a row, and
    /// a model reads them as one anyway): a second note from the same side
    /// joins the one before it; an empty one is left out.
    fn push(&mut self, role: &str, text: &str) {
        let text = if text.trim().is_empty() {
            if role == "user" { return } else { "(no reply)" }
        } else {
            text
        };
        let last = self.msgs.len() - 1;
        if last > 0 && self.msgs[last]["role"] == role {
            if let Some(prev) = self.msgs[last]["content"].as_str() {
                let joined = format!("{prev}\n\n{text}");
                if last == self.head {
                    self.first = format!("{}\n\n{text}", self.first);
                }
                self.msgs[last]["content"] = Value::String(joined);
                return;
            }
        }
        self.msgs.push(json!({"role": role, "content": text}));
    }

    fn chars(&self) -> usize {
        self.msgs.iter().map(|m| m["content"].as_str().map_or(0, str::len)).sum()
    }

    /// Keep the context cheap without losing what matters: past half the
    /// limit, long tool output and long replies older than the last few
    /// turns shrink to their head and a note — all in one go, so the API's
    /// prompt cache (which bills a repeated prefix at a fraction) is broken
    /// only once per compaction, not on every call. Recent turns, the system
    /// prompt and the task are never touched; files are on disk to re-read.
    fn compact(&mut self, soft: usize) -> usize {
        let from = self.head + 1;
        if self.chars() <= soft || self.msgs.len() <= from + KEEP_RECENT {
            return 0;
        }
        let mut saved = 0;
        let end = self.msgs.len() - KEEP_RECENT;
        for m in &mut self.msgs[from..end] {
            let text = m["content"].as_str().unwrap_or("").to_string();
            let limit = if m["role"] == "assistant" { 3000 } else { 1500 };
            if text.len() <= limit || text.contains(ELIDED) {
                continue;
            }
            let mut cut = 600;
            while !text.is_char_boundary(cut) {
                cut -= 1;
            }
            let what = if m["role"] == "assistant" { "your earlier reply (what it wrote is on disk)" } else {
                "tool output — re-read or re-run if you need it again" };
            let short = format!("{}\n[{ELIDED}: {} more characters of {what}]", &text[..cut], text.len() - cut);
            saved += text.len() - short.len();
            m["content"] = Value::String(short);
        }
        self.elided += saved;
        saved
    }

    /// Fit the context: compact old output first (see `compact`); then, if
    /// it's still too big, drop the oldest assistant/user pairs (never the
    /// system prompt, the task, or the latest exchange) down to three
    /// quarters of the limit, so this too happens rarely; if one message is
    /// still too big on its own, keep its head and tail.
    pub fn fit(&mut self, max_chars: usize) {
        self.compact(max_chars / 2);
        // pairs after the task go, oldest first (the roles keep alternating)
        let (a, b) = (self.head + 1, self.head + 3);
        if self.chars() > max_chars {
            while self.chars() > max_chars * 3 / 4 && self.msgs.len() > b {
                self.msgs.drain(a..b);
                self.trimmed += 2;
            }
        }
        while self.chars() > max_chars && self.msgs.len() > b {
            self.msgs.drain(a..b);
            self.trimmed += 2;
        }
        if self.trimmed > 0 {
            let note = format!("[{} {TRIM_NOTE_END}", self.trimmed);
            self.msgs[self.head]["content"] = Value::String(format!("{}\n\n{note}", self.first));
        }
        let total = self.chars();
        if total > max_chars {
            let over = total - max_chars;
            if let Some(big) = self.msgs.iter_mut().skip(1)
                .max_by_key(|m| m["content"].as_str().map_or(0, str::len))
            {
                let text = big["content"].as_str().unwrap_or("").to_string();
                let target = text.len().saturating_sub(over).max(2000);
                big["content"] = Value::String(squeeze(&text, target));
            }
        }
    }
}

/// Where a reply that has started repeating itself (a model stuck in a
/// loop, writing the same line thousands of times) should be cut: after its
/// first copy of what repeats. None: it isn't. A short unit must repeat 30
/// times and a long one 8 (a 22-row board literal is code, not a loop).
pub fn runaway_at(s: &str) -> Option<usize> {
    let n = s.len();
    if n < 2000 {
        return None;
    }
    let base = n.saturating_sub(12_000);
    let b = &s.as_bytes()[base..];
    let m = b.len();
    for l in 1..=400usize {
        if l * 8 > m {
            break;
        }
        let unit = &b[m - l..];
        let mut k = 1;
        while (k + 1) * l <= m && &b[m - (k + 1) * l..m - k * l] == unit {
            k += 1;
        }
        let span = k * l;
        let looping = if l <= 64 { k >= 30 && span >= 2000 } else { k >= 8 && span >= 3000 };
        if looping {
            // where it really began: back through the whole reply, past the window looked at
            let full = s.as_bytes();
            let mut start = n - span;
            while start >= l && &full[start - l..start] == unit {
                start -= l;
            }
            let mut at = start + l;
            while at < n && !s.is_char_boundary(at) {
                at += 1;
            }
            return Some(at);
        }
    }
    None
}

struct AgentSink<'a> {
    id: AgentId,
    ctx: &'a Ctx,
    /// The reply as it streams in, and where it began repeating itself (the
    /// call is stopped there).
    got: String,
    checked: usize,
    runaway: Option<usize>,
    /// When the call began, what has streamed in so far (characters of
    /// reasoning, of reply), and when the agent's line last said so.
    t0: std::time::Instant,
    thought: usize,
    wrote: usize,
    shown: Option<std::time::Instant>,
}

impl AgentSink<'_> {
    /// What the call is doing, on the agent's line, about once a second: a
    /// long reply is seen growing, never a silent "thinking · 0".
    fn progress(&mut self) {
        if self.shown.is_some_and(|t| t.elapsed() < Duration::from_secs(1)) {
            return;
        }
        self.shown = Some(std::time::Instant::now());
        let secs = self.t0.elapsed().as_secs();
        let what = if self.wrote > 0 {
            format!("writing · ~{} tokens · {secs}s", (self.wrote / 4).max(1))
        } else {
            format!("thinking · ~{} tokens · {secs}s", (self.thought / 4).max(1))
        };
        self.ctx.kernel.progress(self.id, &what);
    }
}

impl Sink for AgentSink<'_> {
    fn think(&mut self, c: &str) {
        self.thought += c.len();
        self.progress();
        self.ctx.events.emit(Event::Thinking { id: self.id, chunk: c.into() });
    }
    fn delta(&mut self, c: &str) {
        self.got.push_str(c);
        if self.runaway.is_none() && self.got.len() >= self.checked + 512 {
            self.checked = self.got.len();
            self.runaway = runaway_at(&self.got);
        }
        self.wrote += c.len();
        self.progress();
        self.ctx.events.emit(Event::Delta { id: self.id, chunk: c.into() });
    }
    fn notice(&mut self, n: Notice) {
        let text = match n {
            Notice::ThinkCut { secs } => format!("thought for {secs}s without answering — asked again with thinking off"),
            Notice::Retrying { attempt, why } => format!("retrying ({attempt}): {why}"),
            Notice::OptionDropped(o) => format!("the API refused `{o}`; sent without it"),
        };
        let who = self.ctx.kernel.ps(true).into_iter().find(|p| p.pid == self.id).map(|p| p.name).unwrap_or_default();
        crate::snag(&self.ctx.events, self.id, &who, "api", &text);
        self.ctx.events.emit(Event::Notice { id: self.id, text });
    }
    fn should_stop(&self) -> bool {
        self.runaway.is_some() || self.ctx.stop.load(Ordering::Relaxed) || self.ctx.kernel.should_stop(self.id)
    }
}

/// One model call, scheduled and metered by the kernel. A reply the API cut
/// at its length limit is continued (twice at most) so a half-written block
/// is never executed.
pub fn think(ctx: &Ctx, id: AgentId, model: &str, convo: &Convo) -> Result<String, Halt> {
    think_with(ctx, id, model, convo, &[])
}

/// [`think`], offering the agent's `tags` as functions it can call: a call
/// comes back as the fenced block it means, appended to the reply, so the
/// rest of the loop reads it as if it had been written that way.
pub fn think_with(ctx: &Ctx, id: AgentId, model: &str, convo: &Convo, tags: &[&str]) -> Result<String, Halt> {
    let tools = (ctx.cfg.native_tools && !tags.is_empty()).then(|| crate::schemas::tools(tags));
    let mut msgs = convo.msgs.clone();
    let mut text = String::new();
    // how long the API has been unreachable (reset by any answer)
    let mut outage = Duration::ZERO;
    let mut round = 0;
    while round < 3 {
        // the limits as they are now: one changed in Settings mid-job applies to the next call
        let (day, month) = if ctx.cfg.live_limits {
            let s = chuck_store::Settings::load();
            (s.num("budget_day_usd", 0.0, 0.0, 100_000.0), s.num("budget_month_usd", 0.0, 0.0, 1_000_000.0))
        } else {
            (ctx.cfg.day_limit, ctx.cfg.month_limit)
        };
        if let Some(over) = ctx.cfg.spend.as_ref().and_then(|s| s.over(day, month)) {
            return Err(Halt::Killed(format!("the {} spending limit ({}) is reached — only you can raise it, in \
                Settings", if over.scope == "day" { "daily" } else { "monthly" }, chuck_model::fmt_usd(over.limit))));
        }
        loop {
            if ctx.stop.load(Ordering::Relaxed) {
                return Err(Halt::Stopped);
            }
            match ctx.kernel.acquire(id, Some(Duration::from_millis(300))) {
                Ok(true) => break,
                Ok(false) => continue,
                Err(k) => return Err(Halt::Killed(k.0)),
            }
        }
        ctx.kernel.note(id, "waiting for the model's first word");
        let mut req = ChatRequest::new(model, msgs.clone());
        req.reasoning = if ctx.kernel.role(id).is_some_and(|r| r.is_worker()) { ctx.cfg.worker_reasoning } else {
            ctx.cfg.reasoning };
        req.max_tokens = ctx.cfg.max_tokens;
        // a continuation goes on as text (a cut function call can't be continued as one)
        req.tools = if round == 0 { tools.clone() } else { None };
        let mut sink = AgentSink { id, ctx, got: String::new(), checked: 0, runaway: None,
                                   t0: std::time::Instant::now(), thought: 0, wrote: 0, shown: None };
        let r = chuck_model::chat_interruptible(&ctx.model, &req, &mut sink);
        ctx.kernel.release(id);
        // a reply caught in a loop: stopped at once, what came before the loop kept, the agent told
        let r = match (r, sink.runaway) {
            (Ok(_) | Err(ModelError::Stopped), Some(at)) if !ctx.stop.load(Ordering::Relaxed) && !ctx.kernel.should_stop(id) => {
                let who = ctx.kernel.ps(true).into_iter().find(|p| p.pid == id).map(|p| p.name).unwrap_or_default();
                crate::snag(&ctx.events, id, &who, "runaway reply", &format!("it began repeating itself after {} \
                    characters: {}", at, chuck_core::evidence::clip(&sink.got[at..], 120)));
                let kept = format!("{}\n\n[cut: this reply began repeating the same text over and over, so it was \
                    stopped there]", &sink.got[..at]);
                let usage = Usage::estimate(req.sent_chars(), sink.got.len());
                Ok(chuck_model::Outcome { text: kept, usage: Some(usage), finish: Some("stop".into()),
                                          model: req.model.clone(), ..Default::default() })
            }
            (r, _) => r,
        };
        let out = match r {
            Ok(o) => {
                outage = Duration::ZERO;
                o
            }
            Err(ModelError::Stopped) => {
                return Err(if ctx.stop.load(Ordering::Relaxed) { Halt::Stopped } else {
                    Halt::Killed("killed".into())
                });
            }
            // the network or the API is down: a long job waits it out rather than giving up
            Err(e) if e.transient() && outage < ctx.cfg.outage_wait => {
                let pause = Duration::from_secs(match outage.as_secs() { 0..=59 => 15, 60..=599 => 60, _ => 300 })
                    .min((ctx.cfg.outage_wait / 10).max(Duration::from_millis(100)));
                ctx.events.emit(Event::Notice { id, text: format!("the API isn't answering ({}) — trying again in {:.0}s; \
                    the job waits (Stop ends it)", chuck_core::evidence::clip(&e.to_string(), 120), pause.as_secs_f64()) });
                ctx.kernel.note(id, "waiting for the API");
                let until = std::time::Instant::now() + pause;
                while std::time::Instant::now() < until {
                    if ctx.stop.load(Ordering::Relaxed) {
                        return Err(Halt::Stopped);
                    }
                    if ctx.kernel.should_stop(id) {
                        return Err(Halt::Killed("killed".into()));
                    }
                    std::thread::sleep(Duration::from_millis(200));
                }
                outage += pause;
                continue;
            }
            Err(e) => return Err(Halt::Model(e.to_string())),
        };
        let usage = out.usage.unwrap_or_else(|| Usage::estimate(req.sent_chars(), out.text.len()));
        let usd = usage.cost(price_for(&out.model)).unwrap_or(0.0);
        ctx.kernel.charge(id, usage.prompt, usage.completion, usd);
        if let Some(s) = &ctx.cfg.spend {
            s.add(usd);
        }
        ctx.events.emit(Event::Usage { id, tokens: usage.prompt + usage.completion, usd, prompt: usage.prompt,
                                       cached: usage.cached });
        text.push_str(&out.text);
        // what it called, as the blocks they mean (one a text block already carries isn't run twice)
        let n = out.calls.len();
        for (i, (name, args)) in out.calls.iter().enumerate() {
            if let Some(b) = chuck_tools::native::call_block(name, args, tags) {
                if !text.contains(&b) {
                    text.push_str(&format!("\n{b}\n"));
                }
                continue;
            }
            // the last call, cut by the length limit mid-way (a big file): what arrived stays, as the
            // open block it was becoming, and the next round continues it in text
            if out.was_cut() && i + 1 == n {
                if let Some(open) = chuck_tools::native::partial_block(name, args, tags) {
                    text.push_str(&format!("\n{open}"));
                    continue;
                }
            }
            // a call that can't be made into a block: kept as a call, so the agent hears it didn't run
            text.push_str(&format!("\n<tool_call>{}</tool_call>\n", json!({"name": name, "arguments": args})));
        }
        if !out.was_cut() || round == 2 {
            if out.was_cut() {
                crate::snag(&ctx.events, id, &ctx.kernel.ps(true).into_iter().find(|p| p.pid == id).map(|p| p.name)
                    .unwrap_or_default(), "reply cut", "a reply ran past the length limit three times: what it wrote was \
                    cut short");
            }
            break;
        }
        msgs.push(json!({"role": "assistant", "content": text.clone()}));
        msgs.push(json!({"role": "user", "content":
            "[your reply was cut off by the length limit — continue EXACTLY where it stopped, no repetition]"}));
        round += 1;
    }
    ctx.kernel.note(id, "acting");
    Ok(text)
}

/// `text` with the project's own path, wherever it names it (a whole path
/// component: `/p/proj` never matches inside `/p/proj2`), made the copy's.
pub fn in_copy(text: &str, source: &std::path::Path, copy: &std::path::Path) -> String {
    let src = source.display().to_string();
    if src.len() < 2 || !text.contains(&src) {
        return text.to_string();
    }
    let rx = regex::Regex::new(&format!(r#"{}(/|["'`\s:;)]|$)"#, regex::escape(&src))).expect("valid regex");
    rx.replace_all(text, |c: &regex::Captures| format!("{}{}", copy.display(), &c[1])).into_owned()
}

/// A worker's heartbeat, read by the Lead's supervisor: how many turns it
/// has gone without changing or verifying anything, and what it did last.
#[derive(Clone, Debug, Default)]
pub struct Pulse {
    pub turns: u32,
    /// Turns in a row with no file changed and no check passed.
    pub idle: u32,
    pub last: String,
}

/// The most a worker's conversation holds (characters).
pub const WORKER_CONTEXT: usize = 240_000;

/// Worker → lead.
#[derive(Debug)]
pub enum ToLead {
    Submitted(Box<Submission>),
    /// The worker stopped without a submission the gate accepts.
    Ended { task: String, worker: AgentId, gaps: Vec<String>, why: String },
}

/// Lead → worker.
#[derive(Clone, Debug, PartialEq)]
pub enum ToWorker {
    Accepted,
    Rejected(Vec<String>),
    Stop,
}

pub struct WorkerSpec {
    pub id: AgentId,
    pub role: Role,
    pub name: String,
    pub task: Task,
    pub ws: Workspace,
    pub gate: Arc<Mutex<DoneGate>>,
    /// This worker's own stop (set for a race's loser, and with the job's Stop).
    pub stop: Arc<AtomicBool>,
    pub to_lead: Sender<ToLead>,
    pub inbox: Receiver<ToWorker>,
    /// The job's channel, shared with the Lead and the other workers.
    pub team: Arc<crate::team::Team>,
    /// Who else is working and on what, as the worker first reads it.
    pub roster: String,
    /// The team post it starts reading after (the roster covers what came before).
    pub heard: u64,
    /// The project's dependency (and history) folders its copy borrows.
    pub lent: Vec<std::path::PathBuf>,
    /// Its heartbeat, for the Lead's supervisor.
    pub pulse: Arc<Mutex<Pulse>>,
}

fn describe(gaps: &[Unmet]) -> Vec<String> {
    gaps.iter().map(Unmet::describe).collect()
}

/// A worker's whole life, on its own thread.
pub fn run_worker(ctx: Ctx, w: WorkerSpec) {
    let model = ctx.cfg.fleet.model_for(w.role).to_string();
    let root = w.ws.work.display().to_string();
    // the project's own rules (its CHUCK.md / AGENTS.md is in the copy too)
    let mut system = prompts::with_context(prompts::worker(), &prompts::instructions(Some(&w.ws.work)), "");
    if ctx.cfg.debug {
        system.push_str("\n\n");
        system.push_str(prompts::DEBUG);
    }
    let mut convo = Convo::new(&system, &prompts::worker_first(&w.name, &model, &w.task, &root, &w.roster));
    let mut heard = w.heard;
    let mut progress = Progress::default();
    let mut warned_term = false;
    let (status, ended) = 'life: {
        for _step in 0..ctx.cfg.worker_steps {
            if w.stop.load(Ordering::Relaxed) {
                break 'life ("stopped", None); // the lead took the task back
            }
            if ctx.kernel.terminating(w.id) && !warned_term {
                warned_term = true;
                convo.user("[the budget is nearly spent: this is your LAST turn. Submit what is verified and say \
                    plainly what isn't]");
            }
            // a worker's context is one task's: kept smaller than the Lead's, so each call costs less
            convo.fit(ctx.cfg.context_chars.min(WORKER_CONTEXT));
            let reply = match think_with(&ctx, w.id, &model, &convo, WORKER_TAGS) {
                Ok(r) => r,
                Err(_) if w.stop.load(Ordering::Relaxed) => break 'life ("stopped", None),
                Err(h) => break 'life ("paused", Some(h.describe())),
            };
            // calls in the model's own format (DeepSeek's DSML…) run as the blocks they mean
            let reply = chuck_tools::normalize_calls(&reply, WORKER_TAGS).0;
            let reply = chuck_tools::without_made_up_turns(&reply).to_string();
            // the real project's path (as the Lead's brief names it) means the same place in the copy
            let reply = in_copy(&reply, &w.ws.source, &w.ws.work);
            convo.assistant(&reply);
            let said = chuck_tools::prose(&reply, WORKER_TAGS);
            if !said.trim().is_empty() {
                ctx.events.emit(Event::Said { id: w.id, text: said.clone() });
            }
            let exec = Exec { id: w.id, role: w.role, kernel: &ctx.kernel, root: &w.ws.work, gate: &w.gate,
                              sandbox: Sandbox { access: ctx.cfg.access, bwrap: ctx.cfg.bwrap, worker: true },
                              approve: None, audit: ctx.cfg.audit.clone().map(|l| (l, w.name.clone())),
                              stop: w.stop.clone(), events: &ctx.events,
                              cmd_timeout: ctx.cfg.cmd_timeout,
                              web: ctx.cfg.web.as_deref(), sources: ctx.cfg.sources,
                              mcp: ctx.cfg.mcp.as_deref(), lsp: ctx.cfg.lsp.as_deref(),
                              memory: ctx.cfg.memory.as_deref(), allowed_outside: None,
                              team: Some((&w.team, &w.name)), hold: None, lent: &w.lent, claimed: &[] };
            let r = exec.run(&reply);
            {
                let mut p = w.pulse.lock().unwrap_or_else(|e| e.into_inner());
                p.turns += 1;
                // a change, a passing check or a hand-in is progress; reading and talking alone aren't
                p.idle = if r.mutated || r.verified || r.submit.is_some() { 0 } else { p.idle + 1 };
                p.last = r.actions.last().cloned().unwrap_or_else(|| "no tool call".into());
            }
            let mut fb = r.feedback.clone();
            let idle = w.pulse.lock().unwrap_or_else(|e| e.into_inner()).idle;
            if idle == crate::lead::PUSH_TURNS {
                // the Lead pushes, on the team channel where everyone (the user too) sees it
                let text = format!("@{} hurry up on {}: {idle} turns without a change or a passing check (last: {}). \
                    Stop reading around — make the change now and check it. Blocked? Tell me what you need.",
                    w.name, w.task.id, chuck_core::evidence::clip(r.actions.last().map(String::as_str).unwrap_or("no tool call"), 80));
                w.team.post("lead", Some(&w.name), &text);
                ctx.events.emit(Event::Team { from: "lead".into(), to: Some(w.name.clone()), text });
            }
            if idle == crate::lead::WARN_TURNS {
                crate::snag(&ctx.events, w.id, &w.name, "stuck",
                            &format!("{idle} turns without a change or a passing check (last: {})", r.actions.last()
                                .map(String::as_str).unwrap_or("no tool call")));
                fb.push(format!("[supervisor: {idle} turns without changing or verifying anything. Change the code and \
                    check it now, or ask the Lead (```team @lead```) what you're missing — at {} you are replaced]",
                    crate::lead::STALL_TURNS));
            }
            // "All done, every check passes." with no ```submit```: when its gate agrees (every criterion
            // verified since the last change), that is its hand-in — not a turn lost, nor an idle worker
            let finished = r.submit.is_none() && !r.acted() && !r.malformed && !said.trim().is_empty()
                && w.gate.lock().unwrap().check_work().is_empty();
            let submit = r.submit.clone().or_else(|| finished.then(|| said.trim().to_string()));
            if let Some(report) = &submit {
                // judged on the copy as it really is, not on the history of edits
                let (diff, lines) = w.ws.diff(400_000).unwrap_or_else(|e| (format!("(diff failed: {e})"), 0));
                let sub = {
                    let mut g = w.gate.lock().unwrap();
                    g.observe(diff.clone(), lines);
                    g.submit()
                };
                match sub {
                    Err(gaps) => {
                        crate::snag(&ctx.events, w.id, &w.name, "hand-in refused", &describe(&gaps).join("; "));
                        fb.push(format!("[submission REFUSED by the gate — still missing:\n- {}\nFinish these, then \
                            submit again]", describe(&gaps).join("\n- ")))
                    }
                    Ok(()) => {
                        let evidence = w.gate.lock().unwrap().evidence.clone();
                        ctx.events.emit(Event::Submitted { task: w.task.id.clone(), worker: w.id, lines });
                        let s = Submission { task: w.task.id.clone(), worker: w.id, role: w.role, report: report.clone(),
                                             evidence, diff, lines_changed: lines, gaps: Vec::new() };
                        if w.to_lead.send(ToLead::Submitted(Box::new(s))).is_err() {
                            break 'life ("lead gone", None);
                        }
                        ctx.kernel.block(w.id, "waiting for the lead's review");
                        let verdict = loop {
                            if ctx.stop.load(Ordering::Relaxed) || w.stop.load(Ordering::Relaxed)
                                || ctx.kernel.should_stop(w.id) {
                                break ToWorker::Stop;
                            }
                            match w.inbox.recv_timeout(Duration::from_millis(200)) {
                                Ok(v) => break v,
                                Err(RecvTimeoutError::Timeout) => continue,
                                Err(RecvTimeoutError::Disconnected) => break ToWorker::Stop,
                            }
                        };
                        ctx.kernel.unblock(w.id);
                        match verdict {
                            ToWorker::Accepted => break 'life ("accepted", None),
                            ToWorker::Stop => break 'life ("stopped", None),
                            ToWorker::Rejected(defects) => {
                                warned_term = false;
                                fb = vec![format!("[the lead REJECTED your submission. Fix every defect, verify again \
                                    after your last change, and resubmit:\n- {}]", defects.join("\n- "))];
                            }
                        }
                    }
                }
            } else if !r.acted() {
                crate::snag(&ctx.events, w.id, &w.name, "no tool call", &chuck_core::evidence::clip(&reply, 200));
                fb.push("[no tool calls in that reply — keep working with the tools, or ```submit``` when every \
                    criterion has evidence]".into());
            }
            // going in circles: told so; stopping a stuck worker is the supervisor's call (see lead.rs)
            if let Stall::Replan(why) = progress.turn(r.mutated, r.verified, &r.actions) {
                fb.push(format!("[you are going in circles ({why}). Stop and re-think: what exactly is still \
                    missing for each criterion, and what is the next concrete change or check?]"));
            }
            if ctx.kernel.terminating(w.id) && warned_term && submit.is_none() {
                break 'life ("paused", Some("budget spent".into()));
            }
            fb.push(exec.gate_line());
            let (posts, cursor) = w.team.since(&w.name, heard);
            heard = cursor;
            if !posts.is_empty() {
                fb.push(crate::team::render(&posts));
            }
            {
                let g = w.gate.lock().unwrap();
                ctx.events.emit(Event::Gate { id: w.id, task: Some(w.task.id.clone()), phase: g.phase.label().into(),
                                              unmet: describe(&g.check_work()) });
            }
            convo.user(&fb.join("\n\n"));
        }
        ("paused", Some(format!("used all {} steps", ctx.cfg.worker_steps)))
    };
    if let Some(why) = ended {
        // never a silent exit: the user sees why, as the Lead does
        ctx.events.emit(Event::Notice { id: w.id, text: format!("stopped on {} without finishing: {why}", w.task.id) });
        let gaps = w.gate.lock().unwrap().pause();
        let _ = w.to_lead.send(ToLead::Ended { task: w.task.id.clone(), worker: w.id, gaps, why });
    }
    ctx.kernel.exit(w.id, if status == "accepted" { 0 } else { 1 }, Some(status.into()));
    ctx.events.emit(Event::AgentEnded { id: w.id, status: status.into() });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_reply_caught_in_a_loop_is_cut_and_code_is_not() {
        // the reply in a real job: one call, written thousands of times
        let good = "Let me read the hook section.\n```read index.html:760-800```\n";
        let mut looped = good.to_string();
        for _ in 0..400 {
            looped.push_str("```read index.html:70300000```\n");
        }
        let at = runaway_at(&looped).expect("a loop");
        assert!(looped[..at].starts_with(good) && looped[..at].matches("70300000").count() == 1, "{}", &looped[..at]);
        assert!(runaway_at(&"0".repeat(5000)).is_some());
        // a 22-row board literal and a long ordinary file are code
        let board = format!("const board = [\n{}];\n", "  [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],\n".repeat(22));
        assert!(runaway_at(&board).is_none());
        let code: String = (0..400).map(|i| format!("function f{i}(x) {{ return x + {i}; }}\n")).collect();
        assert!(runaway_at(&code).is_none());
    }

    #[test]
    fn the_projects_path_means_the_copy() {
        let (src, copy) = (std::path::Path::new("/home/u/chuck training"), std::path::Path::new("/c/work"));
        assert_eq!(in_copy("```read /home/u/chuck training/js/a.js```", src, copy), "```read /c/work/js/a.js```");
        assert_eq!(in_copy("cd \"/home/u/chuck training\" && ls", src, copy), "cd \"/c/work\" && ls");
        assert_eq!(in_copy("ls /home/u/chuck training2/x", src, copy), "ls /home/u/chuck training2/x", "another folder");
    }

    #[test]
    fn convo_trims_oldest_pairs_and_keeps_the_task() {
        let mut c = Convo::new("sys", "the task");
        for i in 0..10 {
            c.assistant(&format!("reply {i} {}", "x".repeat(100)));
            c.user(&format!("result {i} {}", "y".repeat(100)));
        }
        c.fit(1000);
        assert!(c.chars() <= 1300);
        assert_eq!(c.msgs[0]["content"], "sys");
        assert!(c.msgs[1]["content"].as_str().unwrap().starts_with("the task\n\n["));
        assert_eq!(c.msgs[2]["role"], "assistant");
        assert!(c.msgs.last().unwrap()["content"].as_str().unwrap().starts_with("result 9"));
        c.fit(1000);
        assert_eq!(c.msgs[1]["content"].as_str().unwrap().matches("trimmed").count(), 1);
    }

    #[test]
    fn a_task_after_earlier_turns_is_kept_and_found_again_after_a_pause() {
        let hist = vec![(true, "earlier question".to_string()), (false, "earlier answer".to_string())];
        let mut c = Convo::with_history("sys", &hist, "the task");
        assert_eq!(c.head(), 3);
        // notes from one side in a row become one turn; an empty one is left out
        c.user("note a");
        c.user("  ");
        assert_eq!(c.msgs.len(), 4);
        assert!(c.msgs[3]["content"].as_str().unwrap().starts_with("the task\n\nnote a"));
        for i in 0..10 {
            c.assistant(&format!("reply {i} {}", "x".repeat(100)));
            c.user(&format!("result {i} {}", "y".repeat(100)));
        }
        c.fit(1200);
        let roles: Vec<&str> = c.msgs.iter().map(|m| m["role"].as_str().unwrap()).collect();
        assert_eq!(&roles[..5], ["system", "user", "assistant", "user", "assistant"], "the earlier turns and the task stay");
        assert!(roles.windows(2).skip(1).all(|w| w[0] != w[1]), "{roles:?}");
        assert!(c.msgs[3]["content"].as_str().unwrap().starts_with("the task\n\nnote a\n\n["));
        // paused and carried on: the task is where it was
        let r = Convo::restore(c.msgs.clone(), c.head());
        assert_eq!((r.head(), r.first.as_str(), r.trimmed), (3, "the task\n\nnote a", 14));
        let mut r = r;
        for i in 0..10 {
            r.assistant(&format!("more {i} {}", "x".repeat(100)));
            r.user(&format!("out {i} {}", "y".repeat(100)));
        }
        r.fit(1200);
        let task = r.msgs[3]["content"].as_str().unwrap();
        assert_eq!(task.matches("trimmed").count(), 1, "{task}");
        assert!(task.contains("[34 earlier messages"), "{task}");
        // a resume saved before there was a head (older files) still works
        assert_eq!(Convo::restore(c.msgs.clone(), 1).head(), 1);
        assert_eq!(Convo::restore(vec![], 9).head(), 1);
    }

    #[test]
    fn the_task_survives_trimming_even_with_brackets_in_it() {
        let task = "fix it\n\n[see the log] below";
        let mut c = Convo::new("sys", task);
        for i in 0..10 {
            c.assistant(&"a".repeat(200));
            c.user(&format!("r{i}"));
        }
        c.fit(1500);
        c.fit(1500);
        let first = c.msgs[1]["content"].as_str().unwrap();
        assert!(first.starts_with(task), "{first}");
        assert_eq!(first.matches("trimmed").count(), 1);
    }

    #[test]
    fn old_output_is_folded_in_one_batch_and_recent_turns_stay_whole() {
        let mut c = Convo::new("sys", "the task");
        for i in 0..10 {
            c.assistant(&format!("reply {i}"));
            c.user(&format!("OUTPUT{i} {}", "o".repeat(5000)));
        }
        let before = c.chars();
        c.fit(60_000); // soft limit 30k: compaction, no dropping
        assert!(c.chars() < before / 2, "{} vs {before}", c.chars()); // the last 3 outputs stay whole
        assert_eq!(c.msgs.len(), 22, "nothing dropped");
        assert!(c.msgs[3]["content"].as_str().unwrap().starts_with("OUTPUT0") &&
                c.msgs[3]["content"].as_str().unwrap().contains("elided"));
        let last = c.msgs.last().unwrap()["content"].as_str().unwrap();
        assert!(last.len() > 5000 && !last.contains("elided"), "the latest output is whole");
        // a second fit with nothing new changes nothing (the cached prefix stays the same)
        let snapshot = c.msgs.clone();
        c.fit(60_000);
        assert_eq!(c.msgs, snapshot);
    }

    #[test]
    fn one_huge_message_is_squeezed_not_sent_whole() {
        let mut c = Convo::new("sys", "task");
        c.assistant("ok");
        c.user(&format!("HEAD{}TAIL", "x".repeat(50_000)));
        c.fit(10_000);
        let last = c.msgs.last().unwrap()["content"].as_str().unwrap();
        assert!(c.chars() <= 10_200, "{}", c.chars());
        assert!(last.starts_with("HEAD") && last.ends_with("TAIL"));
    }
}
