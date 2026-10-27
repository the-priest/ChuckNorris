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

struct AgentSink<'a> {
    id: AgentId,
    ctx: &'a Ctx,
}

impl Sink for AgentSink<'_> {
    fn think(&mut self, c: &str) {
        self.ctx.events.emit(Event::Thinking { id: self.id, chunk: c.into() });
    }
    fn delta(&mut self, c: &str) {
        self.ctx.events.emit(Event::Delta { id: self.id, chunk: c.into() });
    }
    fn notice(&mut self, n: Notice) {
        let text = match n {
            Notice::ThinkCut { secs } => format!("thought for {secs}s without answering — asked again with thinking off"),
            Notice::Retrying { attempt, why } => format!("retrying ({attempt}): {why}"),
            Notice::OptionDropped(o) => format!("the API refused `{o}`; sent without it"),
        };
        self.ctx.events.emit(Event::Notice { id: self.id, text });
    }
    fn should_stop(&self) -> bool {
        self.ctx.stop.load(Ordering::Relaxed) || self.ctx.kernel.should_stop(self.id)
    }
}

/// One model call, scheduled and metered by the kernel. A reply the API cut
/// at its length limit is continued (twice at most) so a half-written block
/// is never executed.
pub fn think(ctx: &Ctx, id: AgentId, model: &str, convo: &Convo) -> Result<String, Halt> {
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
        ctx.kernel.note(id, "thinking");
        let mut req = ChatRequest::new(model, msgs.clone());
        req.reasoning = ctx.cfg.reasoning;
        req.max_tokens = ctx.cfg.max_tokens;
        let mut sink = AgentSink { id, ctx };
        let r = chuck_model::chat_interruptible(&ctx.model, &req, &mut sink);
        ctx.kernel.release(id);
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
        if !out.was_cut() || round == 2 {
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
}

fn describe(gaps: &[Unmet]) -> Vec<String> {
    gaps.iter().map(Unmet::describe).collect()
}

/// A worker's whole life, on its own thread.
pub fn run_worker(ctx: Ctx, w: WorkerSpec) {
    let model = ctx.cfg.fleet.model_for(w.role).to_string();
    let root = w.ws.work.display().to_string();
    // the project's own rules (its CHUCK.md / AGENTS.md is in the copy too)
    let system = prompts::with_context(prompts::worker(w.role, &w.name, &model, &w.task, &root),
                                       &prompts::instructions(Some(&w.ws.work)), "");
    let mut convo = Convo::new(&system,
                               &format!("{}\n\nYour copy of the project: {root}", prompts::task_brief(&w.task)));
    let mut progress = Progress::default();
    let mut warned_term = false;
    let (status, ended) = 'life: {
        for _step in 0..ctx.cfg.worker_steps {
            if ctx.kernel.terminating(w.id) && !warned_term {
                warned_term = true;
                convo.user("[the budget is nearly spent: this is your LAST turn. Submit what is verified and say \
                    plainly what isn't]");
            }
            convo.fit(ctx.cfg.context_chars);
            let reply = match think(&ctx, w.id, &model, &convo) {
                Ok(r) => r,
                Err(h) => break 'life ("paused", Some(h.describe())),
            };
            convo.assistant(&reply);
            let said = chuck_tools::prose(&reply, WORKER_TAGS);
            if !said.trim().is_empty() {
                ctx.events.emit(Event::Said { id: w.id, text: said });
            }
            let exec = Exec { id: w.id, role: w.role, kernel: &ctx.kernel, root: &w.ws.work, gate: &w.gate,
                              sandbox: Sandbox { access: ctx.cfg.access, bwrap: ctx.cfg.bwrap, worker: true },
                              approve: None, audit: ctx.cfg.audit.clone().map(|l| (l, w.name.clone())),
                              stop: w.stop.clone(), events: &ctx.events,
                              cmd_timeout: ctx.cfg.cmd_timeout,
                              web: ctx.cfg.web.as_deref(), sources: ctx.cfg.sources,
                              mcp: ctx.cfg.mcp.as_deref(), lsp: ctx.cfg.lsp.as_deref(),
                              memory: ctx.cfg.memory.as_deref(), allowed_outside: None };
            let r = exec.run(&reply);
            let mut fb = r.feedback.clone();
            if let Some(report) = &r.submit {
                // judged on the copy as it really is, not on the history of edits
                let (diff, lines) = w.ws.diff(400_000).unwrap_or_else(|e| (format!("(diff failed: {e})"), 0));
                let sub = {
                    let mut g = w.gate.lock().unwrap();
                    g.observe(diff.clone(), lines);
                    g.submit()
                };
                match sub {
                    Err(gaps) => fb.push(format!("[submission REFUSED by the gate — still missing:\n- {}\nFinish \
                        these, then submit again]", describe(&gaps).join("\n- "))),
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
                fb.push("[no tool calls in that reply — keep working with the tools, or ```submit``` when every \
                    criterion has evidence]".into());
            }
            if let Stall::Replan(why) = progress.turn(r.mutated, r.verified, &r.actions) {
                if progress.stalls() > crate::lead::MAX_STALLS {
                    break 'life ("paused", Some(format!("stuck: {} replans in a row without progress ({why})",
                                                        progress.stalls())));
                }
                fb.push(format!("[you are going in circles ({why}). Stop and re-think: what exactly is still \
                    missing for each criterion, and what is the next concrete change or check?]"));
            }
            if ctx.kernel.terminating(w.id) && warned_term && r.submit.is_none() {
                break 'life ("paused", Some("budget spent".into()));
            }
            fb.push(exec.gate_line());
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
