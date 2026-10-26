//! One agent's life: think (a model call through the kernel's scheduler),
//! act (its blocks through the guard and the gate), hear back, repeat — and
//! for a worker, hand the result to the lead and wait for the verdict.

use crate::events::{Event, Events};
use crate::exec::{Exec, WORKER_TAGS};
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
    trimmed: usize,
}

impl Convo {
    pub fn new(system: &str, first: &str) -> Convo {
        Convo { msgs: vec![json!({"role": "system", "content": system}), json!({"role": "user", "content": first})],
                trimmed: 0 }
    }

    pub fn user(&mut self, text: &str) {
        self.msgs.push(json!({"role": "user", "content": text}));
    }

    pub fn assistant(&mut self, text: &str) {
        self.msgs.push(json!({"role": "assistant", "content": text}));
    }

    fn chars(&self) -> usize {
        self.msgs.iter().map(|m| m["content"].as_str().map_or(0, str::len)).sum()
    }

    /// Drop the oldest assistant/user pairs until it fits.
    pub fn fit(&mut self, max_chars: usize) {
        while self.chars() > max_chars && self.msgs.len() > 4 {
            self.msgs.drain(2..4);
            self.trimmed += 2;
        }
        if self.trimmed > 0 {
            let note = format!("[{} earlier messages were trimmed to fit the context; the gate keeps every \
                piece of evidence, so re-read files rather than relying on memory]", self.trimmed);
            if let Some(first) = self.msgs.get_mut(1) {
                let text = first["content"].as_str().unwrap_or("");
                let base = text.split("\n\n[").next().unwrap_or(text).to_string();
                first["content"] = Value::String(format!("{base}\n\n{note}"));
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
    for round in 0..3 {
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
        let r = ctx.model.chat(&req, &mut sink);
        ctx.kernel.release(id);
        let out = match r {
            Ok(o) => o,
            Err(ModelError::Stopped) => {
                return Err(if ctx.stop.load(Ordering::Relaxed) { Halt::Stopped } else {
                    Halt::Killed("killed".into())
                });
            }
            Err(e) => return Err(Halt::Model(e.to_string())),
        };
        let usage = out.usage.unwrap_or_else(|| Usage::estimate(req.sent_chars(), out.text.len()));
        let usd = usage.cost(price_for(&out.model)).unwrap_or(0.0);
        ctx.kernel.charge(id, usage.prompt, usage.completion, usd);
        ctx.events.emit(Event::Usage { id, tokens: usage.prompt + usage.completion, usd });
        text.push_str(&out.text);
        if !out.was_cut() || round == 2 {
            break;
        }
        msgs.push(json!({"role": "assistant", "content": text.clone()}));
        msgs.push(json!({"role": "user", "content":
            "[your reply was cut off by the length limit — continue EXACTLY where it stopped, no repetition]"}));
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
    ctx.events.emit(Event::AgentStarted { id: w.id, role: w.role, name: w.name.clone(), model: model.clone(),
                                          task: Some(w.task.id.clone()) });
    let mut convo = Convo::new(&prompts::worker(w.role, &w.name, &model, &w.task, &root),
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
                              wrap: ctx.cfg.wrap.clone(), stop: w.stop.clone(), events: &ctx.events,
                              cmd_timeout: ctx.cfg.cmd_timeout };
            let r = exec.run(&reply);
            let mut fb = r.feedback.clone();
            if let Some(report) = &r.submit {
                let sub = w.gate.lock().unwrap().submit();
                match sub {
                    Err(gaps) => fb.push(format!("[submission REFUSED by the gate — still missing:\n- {}\nFinish \
                        these, then submit again]", describe(&gaps).join("\n- "))),
                    Ok(()) => {
                        let (diff, lines) = w.ws.diff(200_000).unwrap_or_else(|e| (format!("(diff failed: {e:?})"), 0));
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
}
