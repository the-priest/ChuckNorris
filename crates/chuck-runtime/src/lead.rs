//! The Lead: talks to the user, writes the criteria, splits the work across
//! the fleet, reviews every submission, merges only what passes, verifies the
//! whole result, and is the only one who can say "done" — through the gate
//! and an independent critic pass.

use crate::agent::{run_worker, think, Convo, Ctx, Halt, ToLead, ToWorker, WorkerSpec};
use crate::events::{Event, Outcome};
use crate::exec::{parse_criteria, refuse_command, Exec, LeadCall, LEAD_TAGS};
use crate::prompts;
use chuck_core::evidence::{clip, test_counts};
use chuck_core::fleet::better;
use chuck_core::{AgentId, Caps, Criterion, CriterionId, DoneGate, EvidenceKind, Progress, Role, Scale, SpawnSpec,
                 Stall, Submission, Task, Unmet, Verdict};
use chuck_tools::{bash, RunOpts, Workspace};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, Receiver, RecvTimeoutError, Sender};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::Duration;

#[derive(Clone, Debug, PartialEq)]
enum TaskState {
    Queued,
    Running,
    /// A submission is in front of the lead.
    Review,
    Merged,
    Failed(Vec<String>),
}

struct TaskSlot {
    task: Task,
    state: TaskState,
    /// Workers on it now (two in a race).
    workers: Vec<AgentId>,
    /// Race entries that arrived while the other entrant was still working.
    held: Vec<Submission>,
    /// The submission the lead is reviewing.
    pending: Option<Submission>,
}

impl TaskSlot {
    fn open(&self) -> bool {
        matches!(self.state, TaskState::Queued | TaskState::Running | TaskState::Review)
    }
}

struct Worker {
    role: Role,
    name: String,
    ws: Workspace,
    gate: Arc<Mutex<DoneGate>>,
    tx: Sender<ToWorker>,
    stop: Arc<AtomicBool>,
    join: Option<JoinHandle<()>>,
    live: bool,
}

pub struct LeadRun<'a> {
    ctx: &'a Ctx,
    pub id: AgentId,
    root: PathBuf,
    pub gate: Arc<Mutex<DoneGate>>,
    tasks: Vec<TaskSlot>,
    workers: HashMap<AgentId, Worker>,
    tx: Sender<ToLead>,
    rx: Receiver<ToLead>,
    answers: Option<Receiver<String>>,
    store: PathBuf,
    mutated: bool,
    critic_rounds: u32,
}

fn defects_of(body: &str) -> Vec<String> {
    body.lines().map(str::trim).filter(|l| l.starts_with(['-', '*', '•']))
        .map(|l| l.trim_start_matches(['-', '*', '•']).trim().to_string()).filter(|l| !l.is_empty()).collect()
}

fn tail(s: &str, n: usize) -> String {
    let t: Vec<char> = s.chars().collect();
    if t.len() <= n { s.to_string() } else { format!("…{}", t[t.len() - n..].iter().collect::<String>()) }
}

/// "+"/"-" lines of a unified diff, headers excluded.
fn counts(patch: &str) -> (usize, usize) {
    let mut a = 0;
    let mut r = 0;
    for l in patch.lines() {
        if l.starts_with("+++") || l.starts_with("---") {
            continue;
        }
        if l.starts_with('+') {
            a += 1;
        } else if l.starts_with('-') {
            r += 1;
        }
    }
    (a, r)
}

/// The parts of an ```assign``` block.
#[derive(Debug, PartialEq)]
pub struct AssignSpec {
    pub prefer: Role,
    pub race: bool,
    pub after: Vec<String>,
    pub title: String,
    pub scale: Scale,
    pub criteria: Vec<(String, Option<String>)>,
    pub brief: String,
}

pub fn parse_assign(arg: &str, body: &str) -> Result<AssignSpec, String> {
    let a = arg.to_lowercase();
    let mut words = a.split_whitespace();
    let (prefer, race) = match words.next() {
        Some("deepseek" | "ds") => (Role::DeepSeekWorker, false),
        Some("glm") => (Role::GlmWorker, false),
        Some("race") => (Role::DeepSeekWorker, true),
        other => return Err(format!("[assign: say who — ```assign deepseek```, ```assign glm``` or ```assign race```, \
            not `{}`]", other.unwrap_or(""))),
    };
    let rest: Vec<&str> = words.collect();
    let after = match rest.first() {
        Some(&"after") => rest[1..].join(" ").split([',', ' ']).map(str::trim).filter(|s| !s.is_empty())
            .map(String::from).collect(),
        Some(w) => return Err(format!("[assign: unexpected `{w}` — the form is ```assign glm after t1,t2```]")),
        None => Vec::new(),
    };
    let (mut title, mut scale) = (String::new(), Scale::Small);
    let mut crit_lines = String::new();
    let mut brief = Vec::new();
    let mut section = "";
    for line in body.lines() {
        let t = line.trim();
        let low = t.to_lowercase();
        if section != "brief" {
            if let Some(v) = low.strip_prefix("title:") {
                title = t[t.len() - v.len()..].trim().to_string();
                continue;
            }
            if let Some(v) = low.strip_prefix("scale:") {
                scale = if v.trim().starts_with("large") { Scale::Large } else { Scale::Small };
                continue;
            }
            if low.starts_with("criteria:") {
                section = "criteria";
                continue;
            }
            if let Some(v) = low.strip_prefix("brief:") {
                section = "brief";
                let first = t[t.len() - v.len()..].trim();
                if !first.is_empty() {
                    brief.push(first.to_string());
                }
                continue;
            }
            if section == "criteria" && t.starts_with(['-', '*', '•']) {
                crit_lines.push_str(t);
                crit_lines.push('\n');
                continue;
            }
            if section == "criteria" && t.is_empty() {
                continue;
            }
        }
        brief.push(line.to_string());
    }
    let brief = brief.join("\n").trim().to_string();
    let criteria = parse_criteria(&crit_lines);
    if criteria.is_empty() {
        return Err("[assign refused: a task needs acceptance criteria — `criteria:` then lines \
            `- <criterion> :: verify: <command>`]".into());
    }
    if brief.is_empty() && title.is_empty() {
        return Err("[assign refused: say what the work is (`title:` and `brief:`)]".into());
    }
    if title.is_empty() {
        title = brief.lines().next().unwrap_or("").chars().take(80).collect();
    }
    Ok(AssignSpec { prefer, race, after, title, scale, criteria, brief })
}

/// The critic's reply: ACCEPT, or REJECT with its defects. Anything else is
/// a rejection (no verdict is not a pass).
pub fn parse_verdict(reply: &str) -> Verdict {
    let first = reply.lines().map(str::trim).find(|l| !l.is_empty()).unwrap_or("");
    let head = first.trim_start_matches(['*', '#', ' ']).to_uppercase();
    if head.starts_with("ACCEPT") {
        return Verdict::Accepted;
    }
    let d = defects_of(reply);
    if d.is_empty() {
        Verdict::Rejected(vec![if head.starts_with("REJECT") {
            "the reviewer rejected it without listing defects".into()
        } else {
            format!("the review gave no verdict: {}", clip(first, 120))
        }])
    } else {
        Verdict::Rejected(d)
    }
}

impl<'a> LeadRun<'a> {
    pub fn new(ctx: &'a Ctx, id: AgentId, root: &Path, ask: &str, answers: Option<Receiver<String>>) -> LeadRun<'a> {
        let (tx, rx) = channel();
        let store = ctx.cfg.store.join(format!("job-{}-{}", std::process::id(), id));
        LeadRun { ctx, id, root: root.to_path_buf(), gate: Arc::new(Mutex::new(DoneGate::new(ask, Scale::Small))),
                  tasks: Vec::new(), workers: HashMap::new(), tx, rx, answers, store, mutated: false,
                  critic_rounds: 0 }
    }

    fn open_tasks(&self) -> Vec<String> {
        self.tasks.iter().filter(|t| t.open()).map(|t| t.task.id.clone()).collect()
    }

    fn slot(&mut self, id: &str) -> Option<&mut TaskSlot> {
        self.tasks.iter_mut().find(|t| t.task.id == id)
    }

    fn live(&self, role: Role) -> usize {
        self.workers.values().filter(|w| w.live && w.role == role).count()
    }

    fn free_name(&self, role: Role) -> String {
        let prefix = if role == Role::GlmWorker { "glm" } else { "ds" };
        (1..=8).map(|i| format!("{prefix}-{i}"))
            .find(|n| !self.workers.values().any(|w| w.live && &w.name == n))
            .unwrap_or_else(|| format!("{prefix}-x"))
    }

    fn spawn_worker(&mut self, task: &Task, role: Role) -> Result<AgentId, String> {
        let name = self.free_name(role);
        let k = &self.ctx.kernel;
        let id = k.spawn(SpawnSpec::new(&name, role).parent(self.id).quota(self.ctx.cfg.worker_quota)
            .brief(format!("{}: {}", task.id, task.title)));
        let ws = match Workspace::create(&self.root, &self.store, &format!("{}-{name}", task.id)) {
            Ok(ws) => ws,
            Err(e) => {
                k.exit(id, 1, Some("no workspace".into()));
                return Err(format!("couldn't make {name} a copy of the project: {e:?}"));
            }
        };
        let mut g = task.gate();
        if g.plan.is_empty() {
            g.set_plan(vec![task.title.clone()]); // the lead's task graph is the plan
        }
        let gate = Arc::new(Mutex::new(g));
        let (wtx, wrx) = channel();
        let stop = Arc::new(AtomicBool::new(false));
        let spec = WorkerSpec { id, role, name: name.clone(), task: task.clone(), ws: ws.clone(), gate: gate.clone(),
                                stop: stop.clone(), to_lead: self.tx.clone(), inbox: wrx };
        let ctx = self.ctx.clone();
        let join = std::thread::Builder::new().name(format!("chuck-{name}"))
            .spawn(move || run_worker(ctx, spec))
            .map_err(|e| format!("couldn't start {name}: {e}"))?;
        self.ctx.events.emit(Event::Assigned { task: task.id.clone(), title: task.title.clone(), worker: id, role });
        self.workers.insert(id, Worker { role, name, ws, gate, tx: wtx, stop, join: Some(join),
                                         live: true });
        Ok(id)
    }

    /// Start every queued task whose dependencies are merged and whose pool
    /// has room. Returns what to tell the lead.
    fn start_ready(&mut self) -> Vec<String> {
        let mut out = Vec::new();
        for i in 0..self.tasks.len() {
            if self.tasks[i].state != TaskState::Queued {
                continue;
            }
            let deps = self.tasks[i].task.deps.clone();
            let failed: Vec<&String> = deps.iter().filter(|d| self.tasks.iter()
                .any(|t| &t.task.id == *d && matches!(t.state, TaskState::Failed(_)))).collect();
            if !failed.is_empty() {
                let why = format!("dependency {} failed", failed.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(", "));
                out.push(format!("[{} can't start: {why} — reassign it once that is fixed]", self.tasks[i].task.id));
                self.tasks[i].state = TaskState::Failed(vec![why]);
                continue;
            }
            if !deps.iter().all(|d| self.tasks.iter().any(|t| &t.task.id == d && t.state == TaskState::Merged)) {
                continue;
            }
            let task = self.tasks[i].task.clone();
            let roles = if task.race { vec![Role::DeepSeekWorker, Role::GlmWorker] } else { vec![task.prefer] };
            if roles.iter().any(|r| self.live(*r) >= self.ctx.cfg.fleet.max_workers(*r)) {
                continue;
            }
            let mut started = Vec::new();
            for r in roles {
                match self.spawn_worker(&task, r) {
                    Ok(id) => started.push(id),
                    Err(e) => out.push(format!("[{}: {e}]", task.id)),
                }
            }
            let names: Vec<String> = started.iter().filter_map(|id| self.workers.get(id))
                .map(|w| format!("{} ({})", w.name, self.ctx.cfg.fleet.model_for(w.role))).collect();
            let slot = &mut self.tasks[i];
            if started.is_empty() {
                slot.state = TaskState::Failed(vec!["no worker could start".into()]);
            } else {
                slot.state = TaskState::Running;
                slot.workers = started;
                out.push(format!("[{} \"{}\" → {}{}]", task.id, task.title, names.join(" vs "),
                                 if task.race { ": a race; the better verified result comes to you" } else { "" }));
            }
        }
        out
    }

    fn retire(&mut self, id: AgentId) {
        if let Some(w) = self.workers.get_mut(&id) {
            w.live = false;
            w.stop.store(true, Ordering::Relaxed);
            let _ = w.tx.send(ToWorker::Stop);
            if let Some(j) = w.join.take() {
                let _ = j.join();
            }
            w.ws.remove();
        }
    }

    fn present(&mut self, sub: Submission) -> String {
        let fleet = &self.ctx.cfg.fleet;
        let (name, model) = self.workers.get(&sub.worker)
            .map(|w| (w.name.clone(), fleet.model_for(w.role).to_string())).unwrap_or_default();
        let slot = self.tasks.iter().find(|t| t.task.id == sub.task);
        let mut ev = String::new();
        if let Some(slot) = slot {
            for c in &slot.task.criteria {
                let labels: Vec<String> = sub.evidence.iter().filter(|e| e.criteria.contains(&c.id))
                    .map(|e| e.kind.label()).collect();
                ev.push_str(&format!("- {}: {}\n    {}\n", c.id, clip(&c.text, 100),
                                     if labels.is_empty() { "(none)".into() } else { labels.join(" · ") }));
            }
        }
        let text = format!("SUBMISSION {} \"{}\" from {name} ({model}) — {} lines changed\n\nREPORT:\n{}\n\n\
            EVIDENCE per criterion:\n{ev}\nDIFF:\n{}\n\nReview it against the criteria: ```review {} accept``` \
            or ```review {} reject``` + one `- <defect>` line per problem.",
            sub.task, slot.map(|s| s.task.title.as_str()).unwrap_or(""), sub.lines_changed, clip(&sub.report, 4000),
            clip(&sub.diff, 16_000), sub.task, sub.task);
        if let Some(s) = self.slot(&sub.task.clone()) {
            s.state = TaskState::Review;
            s.pending = Some(sub);
        }
        text
    }

    /// A message from a worker; returns what the lead should hear.
    fn on_worker(&mut self, msg: ToLead) -> Vec<String> {
        let mut out = Vec::new();
        match msg {
            ToLead::Submitted(sub) => {
                let sub = *sub;
                let Some(slot) = self.tasks.iter_mut().find(|t| t.task.id == sub.task) else { return out };
                let others_live = slot.workers.iter().filter(|w| **w != sub.worker)
                    .any(|w| self.workers.get(w).is_some_and(|x| x.live)
                         && !slot.held.iter().any(|h| h.worker == *w));
                if slot.task.race && others_live {
                    slot.held.push(sub);
                    return out;
                }
                let mut entries = std::mem::take(&mut slot.held);
                entries.push(sub);
                let win = entries.iter().skip(1).fold(&entries[0], |a, b| better(a, b)).clone();
                if slot.task.race && entries.len() > 1 {
                    for loser in entries.iter().filter(|e| e.worker != win.worker).map(|e| e.worker).collect::<Vec<_>>() {
                        let lname = self.workers.get(&loser).map(|w| w.name.clone()).unwrap_or_default();
                        self.retire(loser);
                        out.push(format!("[race on {}: {} lost (more gaps, fewer passing tests, or a larger diff)]",
                                         win.task, lname));
                    }
                    if let Some(s) = self.slot(&win.task.clone()) {
                        s.workers.retain(|w| *w == win.worker);
                    }
                }
                out.push(self.present(win));
            }
            ToLead::Ended { task, worker, gaps, why } => {
                let name = self.workers.get(&worker).map(|w| w.name.clone()).unwrap_or_default();
                self.retire(worker);
                let Some(slot) = self.tasks.iter_mut().find(|t| t.task.id == task) else { return out };
                slot.workers.retain(|w| *w != worker);
                if !slot.workers.is_empty() {
                    // the race goes on with the other entrant (or its held submission)
                    out.push(format!("[{task}: {name} stopped ({why}); the other entrant continues]"));
                    if let Some(h) = slot.held.pop() {
                        out.push(self.present(h));
                    }
                    return out;
                }
                slot.state = TaskState::Failed(gaps.clone());
                out.push(format!("[{task}: {name} stopped without finished work ({why}). Still missing:\n- {}\n\
                    Reassign it with tighter criteria, do it yourself, or say in your summary that it isn't done]",
                    if gaps.is_empty() { "(nothing listed)".into() } else { gaps.join("\n- ") }));
            }
        }
        out.extend(self.start_ready());
        out
    }

    fn run_check(&self, cmd: &str, cwd: &Path) -> chuck_tools::RunResult {
        let mut o = RunOpts::new(cwd);
        o.timeout = self.ctx.cfg.cmd_timeout;
        o.wrap = self.ctx.cfg.wrap.clone();
        o.stop = Some(self.ctx.stop.clone());
        bash(cmd, o)
    }

    fn review(&mut self, call: &LeadCall) -> String {
        let mut words = call.arg.split_whitespace();
        let (Some(tid), Some(word)) = (words.next(), words.next()) else {
            return "[review: ```review t1 accept``` or ```review t1 reject``` + `- defect` lines]".into();
        };
        let tid = tid.to_string();
        let Some(slot) = self.tasks.iter_mut().find(|t| t.task.id == tid) else {
            return format!("[review: there is no task {tid}]");
        };
        let Some(sub) = slot.pending.take() else {
            return format!("[review: {tid} has no submission waiting (it is {:?})]", slot.state);
        };
        let Some(w) = self.workers.get(&sub.worker) else { return format!("[review: {tid}'s worker is gone]") };
        let (gate, wtx, work) = (w.gate.clone(), w.tx.clone(), w.ws.work.clone());
        let reject = |gate: &Arc<Mutex<DoneGate>>, d: Vec<String>, notes: &str| {
            let _ = gate.lock().unwrap().review(self.id, Role::Lead, Verdict::Rejected(d.clone()), notes);
            let _ = wtx.send(ToWorker::Rejected(d.clone()));
            self.ctx.events.emit(Event::Reviewed { task: tid.clone(), accepted: false, defects: d });
        };
        match word.to_lowercase().as_str() {
            "reject" | "rejected" | "no" => {
                let d = defects_of(&call.body);
                if d.is_empty() {
                    slot.pending = Some(sub);
                    return "[a rejection needs its defects: one `- <defect>` line each, concrete enough to fix]".into();
                }
                slot.state = TaskState::Running;
                reject(&gate, d.clone(), &call.body);
                format!("[{tid} sent back with {} defect(s); the worker continues in its copy]", d.len())
            }
            "accept" | "accepted" | "yes" => {
                slot.state = TaskState::Running;
                // re-verify on the worker's copy: its claims are checked, not trusted
                let crit = gate.lock().unwrap().criteria.clone();
                let mut cmds: Vec<(String, Vec<CriterionId>)> = Vec::new();
                for c in &crit {
                    if let Some(v) = &c.verify {
                        match cmds.iter_mut().find(|(x, _)| x == v) {
                            Some((_, ids)) => ids.push(c.id),
                            None => cmds.push((v.clone(), vec![c.id])),
                        }
                    }
                }
                if cmds.is_empty() {
                    let last = sub.evidence.iter().rev().find_map(|e| match &e.kind {
                        EvidenceKind::Test { cmd, exit: 0, .. } => Some(cmd.clone()),
                        _ => None,
                    });
                    cmds.extend(last.map(|c| (c, crit.iter().map(|c| c.id).collect())));
                }
                let mut failed = Vec::new();
                for (cmd, ids) in &cmds {
                    if let Some(why) = refuse_command(cmd, Role::Lead) {
                        failed.push(format!("`{cmd}` can't be re-run: {why}"));
                        continue;
                    }
                    let r = self.run_check(cmd, &work);
                    let (passed, nfail) = test_counts(&r.output);
                    gate.lock().unwrap().record(self.id, ids.clone(), EvidenceKind::Test {
                        cmd: cmd.clone(), exit: r.exit, passed, failed: nfail, output: clip(&r.output, 6000) }, false);
                    if !r.ok() {
                        failed.push(format!("re-running `{cmd}` on your copy failed (exit {}): {}", r.exit,
                                            tail(&r.output, 600)));
                    }
                }
                if !failed.is_empty() {
                    reject(&gate, failed.clone(), "re-verification failed");
                    return format!("[{tid} NOT accepted: the lead's re-run of its verification failed, so it went \
                        back to the worker:\n- {}]", failed.join("\n- "));
                }
                let done = {
                    let mut g = gate.lock().unwrap();
                    let _ = g.review(self.id, Role::Lead, Verdict::Accepted, &call.body);
                    g.declare_done(Role::Lead)
                };
                if let Err(gaps) = done {
                    let d: Vec<String> = gaps.iter().map(Unmet::describe).collect();
                    reject(&gate, d.clone(), "the task gate refused");
                    return format!("[{tid} can't be accepted — its gate refuses:\n- {}\nIt went back to the worker]",
                                   d.join("\n- "));
                }
                let m = match self.workers.get(&sub.worker).unwrap().ws.merge(None) {
                    Ok(m) => m,
                    Err(e) => {
                        if let Some(s) = self.slot(&tid) {
                            s.pending = Some(sub);
                            s.state = TaskState::Review;
                        }
                        return format!("[{tid}: merging failed ({e:?}) — nothing was brought home; try again or do \
                            it yourself]");
                    }
                };
                self.ctx.events.emit(Event::Merged { task: tid.clone(), applied: m.applied.len(), merged: m.merged.len(),
                                                     conflicts: m.conflicts.clone(), refused: m.refused.clone() });
                let (added, removed) = counts(&sub.diff);
                {
                    let mut g = self.gate.lock().unwrap();
                    let all: Vec<CriterionId> = g.criteria.iter().map(|c| c.id).collect();
                    let mut files = m.applied.clone();
                    files.extend(m.merged.iter().cloned());
                    g.record(self.id, all, EvidenceKind::Diff { files, added, removed, patch: sub.diff.clone() }, true);
                }
                self.mutated = true;
                let _ = wtx.send(ToWorker::Accepted);
                if let Some(s) = self.slot(&tid) {
                    s.state = TaskState::Merged;
                    s.workers.clear();
                }
                self.ctx.events.emit(Event::Reviewed { task: tid.clone(), accepted: true, defects: Vec::new() });
                if let Some(j) = self.workers.get_mut(&sub.worker).and_then(|w| w.join.take()) {
                    let _ = j.join();
                }
                if let Some(w) = self.workers.get_mut(&sub.worker) {
                    w.live = false;
                    w.ws.remove();
                }
                let mut s = format!("[{tid} accepted, re-verified on the worker's copy ({}), and merged into the \
                    project: {} file(s) applied, {} merged three-way]",
                    cmds.iter().map(|(c, _)| format!("`{c}`")).collect::<Vec<_>>().join(", "),
                    m.applied.len(), m.merged.len());
                if !m.conflicts.is_empty() {
                    s.push_str(&format!("\n[CONFLICTS — you changed these too; the worker's versions are in \
                        .chuck-conflicts/, resolve them: {}]", m.conflicts.join(", ")));
                }
                if !m.refused.is_empty() {
                    s.push_str(&format!("\n[not brought home (links outside the project): {}]", m.refused.join(", ")));
                }
                s.push_str("\n[the project changed: verify the whole result before ```done```]");
                for line in self.start_ready() {
                    s.push('\n');
                    s.push_str(&line);
                }
                s
            }
            other => {
                slot.pending = Some(sub);
                format!("[review: `{other}` isn't a verdict — accept or reject]")
            }
        }
    }

    fn assign(&mut self, call: &LeadCall) -> String {
        let spec = match parse_assign(&call.arg, &call.body) {
            Ok(s) => s,
            Err(e) => return e,
        };
        {
            let mut g = self.gate.lock().unwrap();
            g.scale = Scale::Large; // delegated work is large work: a plan, and a result in proportion
            if let Err(unmet) = g.allow_mutation() {
                return format!("[assign refused — first: {}]",
                               unmet.iter().map(Unmet::describe).collect::<Vec<_>>().join("; "));
            }
        }
        let unknown: Vec<&String> = spec.after.iter().filter(|d| !self.tasks.iter().any(|t| &t.task.id == *d)).collect();
        if !unknown.is_empty() {
            return format!("[assign refused: no task {}]", unknown.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(", "));
        }
        let id = format!("t{}", self.tasks.len() + 1);
        let criteria = spec.criteria.into_iter().enumerate()
            .map(|(i, (text, verify))| Criterion { id: CriterionId(i as u16 + 1), text, verify }).collect();
        let task = Task { id: id.clone(), title: spec.title, brief: spec.brief, criteria, scale: spec.scale,
                          prefer: spec.prefer, race: spec.race, deps: spec.after };
        self.tasks.push(TaskSlot { task, state: TaskState::Queued, workers: Vec::new(), held: Vec::new(),
                                   pending: None });
        let mut out = self.start_ready();
        if self.tasks.last().is_some_and(|t| t.state == TaskState::Queued) {
            out.push(format!("[{id} queued: it starts when its dependencies are merged and a worker is free]"));
        }
        out.join("\n")
    }

    fn justify(&mut self, call: &LeadCall) -> String {
        let mut arg = call.arg.trim().to_string();
        let mut target = self.gate.clone();
        if let Some((first, rest)) = arg.split_once(char::is_whitespace) {
            if let Some(w) = self.tasks.iter().find(|t| t.task.id == first).and_then(|t| t.workers.first())
                .and_then(|w| self.workers.get(w)) {
                target = w.gate.clone();
                arg = rest.trim().to_string();
            }
        }
        if arg.is_empty() || call.body.trim().is_empty() {
            return "[justify: ```justify proportion``` (or the flagged line/file) + why, in the body]".into();
        }
        let mut g = target.lock().unwrap();
        g.record(self.id, Vec::new(), EvidenceKind::Justification { about: arg.clone(), text: call.body.trim().into() }, false);
        g.accept_justification(Role::Lead, &arg);
        format!("[justification for `{arg}` recorded]")
    }

    fn ask(&mut self, call: &LeadCall) -> String {
        let q = format!("{} {}", call.arg, call.body).trim().to_string();
        self.ctx.events.emit(Event::Ask { id: self.id, question: q });
        let Some(rx) = &self.answers else {
            return "[nobody can answer right now: make the most reasonable choice, say which, and go on]".into();
        };
        self.ctx.kernel.block(self.id, "waiting for your answer");
        let answer = loop {
            if self.ctx.stop.load(Ordering::Relaxed) || self.ctx.kernel.should_stop(self.id) {
                break None;
            }
            match rx.recv_timeout(Duration::from_millis(200)) {
                Ok(a) => break Some(a),
                Err(RecvTimeoutError::Timeout) => continue,
                Err(RecvTimeoutError::Disconnected) => break None,
            }
        };
        self.ctx.kernel.unblock(self.id);
        match answer {
            Some(a) => format!("[the user answered: {a}]"),
            None => "[no answer came: make the most reasonable choice, say which, and go on]".into(),
        }
    }

    /// The independent pass: a fresh model call that sees only the request,
    /// the criteria, the evidence and the diff.
    fn critic(&mut self, summary: &str) -> Result<Verdict, Halt> {
        let input = {
            let g = self.gate.lock().unwrap();
            let mut s = format!("REQUEST:\n{}\n\nACCEPTANCE CRITERIA:\n", g.ask);
            for c in &g.criteria {
                s.push_str(&format!("- {}: {}{}\n", c.id, c.text,
                                    c.verify.as_ref().map(|v| format!(" (verify: `{v}`)")).unwrap_or_default()));
            }
            s.push_str("\nEVIDENCE (oldest first):\n");
            let mut patches = String::new();
            for e in &g.evidence {
                let ids: Vec<String> = e.criteria.iter().map(|c| c.to_string()).collect();
                s.push_str(&format!("- #{} [{}] {}\n", e.seq, ids.join(","), e.kind.label()));
                match &e.kind {
                    EvidenceKind::Test { output, .. } | EvidenceKind::Command { output, .. } => {
                        s.push_str(&format!("    output: {}\n", tail(output, 900).replace('\n', "\n    ")));
                    }
                    EvidenceKind::Diff { patch, .. } => patches.push_str(patch),
                    EvidenceKind::Review { notes, .. } | EvidenceKind::Justification { text: notes, .. } => {
                        s.push_str(&format!("    {}\n", clip(notes, 400)));
                    }
                    EvidenceKind::Look { note, .. } => s.push_str(&format!("    {}\n", clip(note, 400))),
                }
            }
            s.push_str(&format!("\nFINAL DIFF:\n{}\n\nTHE TEAM'S SUMMARY:\n{}", clip(&patches, 40_000), clip(summary, 4000)));
            s
        };
        let k = &self.ctx.kernel;
        let cid = k.spawn(SpawnSpec::new("critic", Role::Lead).parent(self.id).caps(Caps::READ)
            .brief("independent review of the whole result"));
        self.ctx.events.emit(Event::AgentStarted { id: cid, role: Role::Lead, name: "critic".into(),
                                                   model: self.ctx.cfg.fleet.deepseek_model.clone(), task: None });
        let r = think(self.ctx, cid, &self.ctx.cfg.fleet.deepseek_model.clone(), &Convo::new(prompts::CRITIC, &input));
        k.exit(cid, if r.is_ok() { 0 } else { 1 }, None);
        self.ctx.events.emit(Event::AgentEnded { id: cid, status: if r.is_ok() { "done".into() } else { "failed".into() } });
        Ok(parse_verdict(&r?))
    }

    /// ```done```: through the work gate, then the critic, then declare.
    /// Ok(Err(why)) is a refusal the lead hears.
    fn done(&mut self, call: &LeadCall) -> Result<Result<Outcome, String>, Halt> {
        let summary = format!("{} {}", call.arg, call.body).trim().to_string();
        let open = self.open_tasks();
        if !open.is_empty() {
            return Ok(Err(format!("[done REFUSED: still open: {} — review them or wait for them first]",
                                  open.join(", "))));
        }
        let list = |gaps: &[Unmet]| gaps.iter().map(Unmet::describe).collect::<Vec<_>>().join("\n- ");
        let gaps = self.gate.lock().unwrap().check_work();
        if !gaps.is_empty() {
            return Ok(Err(format!("[done REFUSED by the gate — still missing:\n- {}]", list(&gaps))));
        }
        self.critic_rounds += 1;
        let verdict = self.critic(&summary)?;
        let defects = match &verdict {
            Verdict::Rejected(d) => d.clone(),
            Verdict::Accepted => Vec::new(),
        };
        let mut g = self.gate.lock().unwrap();
        let _ = g.review(self.id, Role::Lead, verdict, "independent critic pass");
        self.ctx.events.emit(Event::Reviewed { task: "job".into(), accepted: defects.is_empty(), defects: defects.clone() });
        if !defects.is_empty() {
            return Ok(Err(format!("[done REFUSED: the independent review (round {}) rejected the result:\n- {}\n\
                Fix each one, verify again, then ```done```]", self.critic_rounds, defects.join("\n- "))));
        }
        match g.declare_done(Role::Lead) {
            Ok(()) => Ok(Ok(Outcome::Done { summary })),
            Err(gaps) => Ok(Err(format!("[done REFUSED by the gate:\n- {}]", list(&gaps)))),
        }
    }

    fn drain(&mut self) -> Vec<String> {
        let mut out = Vec::new();
        while let Ok(m) = self.rx.try_recv() {
            out.extend(self.on_worker(m));
        }
        out
    }

    /// Nothing for the lead to do but wait for a worker.
    fn wait_for_worker(&mut self) -> Result<Vec<String>, Halt> {
        self.ctx.kernel.block(self.id, "waiting for the workers");
        let r = loop {
            if self.ctx.stop.load(Ordering::Relaxed) {
                break Err(Halt::Stopped);
            }
            if self.ctx.kernel.should_stop(self.id) {
                break Err(Halt::Killed("killed".into()));
            }
            match self.rx.recv_timeout(Duration::from_millis(200)) {
                Ok(m) => {
                    let mut out = self.on_worker(m);
                    out.extend(self.drain());
                    if !out.is_empty() {
                        break Ok(out);
                    }
                }
                Err(RecvTimeoutError::Timeout) => continue,
                Err(RecvTimeoutError::Disconnected) => break Ok(vec!["[every worker is gone]".into()]),
            }
        };
        self.ctx.kernel.unblock(self.id);
        r
    }

    fn pause(&mut self, why: &str) -> Outcome {
        let mut gaps = vec![why.to_string()];
        for t in &self.tasks {
            match &t.state {
                TaskState::Queued | TaskState::Running | TaskState::Review => {
                    gaps.push(format!("{} \"{}\" wasn't finished", t.task.id, t.task.title));
                }
                TaskState::Failed(g) => gaps.push(format!("{} \"{}\" failed: {}", t.task.id, t.task.title,
                                                          clip(&g.join("; "), 200))),
                TaskState::Merged => {}
            }
        }
        gaps.extend(self.gate.lock().unwrap().pause());
        Outcome::Paused { gaps }
    }

    /// The lead's whole run. Always tears the fleet down before returning.
    pub fn run(&mut self, first: &str) -> Outcome {
        let out = self.run_inner(first);
        let ids: Vec<AgentId> = self.workers.iter().filter(|(_, w)| w.live).map(|(id, _)| *id).collect();
        for id in ids {
            self.ctx.kernel.signal(id, chuck_core::Signal::Kill);
            self.retire(id);
        }
        for w in self.workers.values_mut() {
            if let Some(j) = w.join.take() {
                let _ = j.join();
            }
        }
        let _ = std::fs::remove_dir_all(&self.store);
        out
    }

    fn run_inner(&mut self, first: &str) -> Outcome {
        let ctx = self.ctx;
        let model = ctx.cfg.fleet.deepseek_model.clone();
        let root = self.root.display().to_string();
        let mut convo = Convo::new(&prompts::lead(&ctx.cfg.fleet, &root), first);
        let mut progress = Progress::default();
        let mut replans = 0;
        let mut warned_term = false;
        for _ in 0..ctx.cfg.lead_steps {
            if ctx.kernel.terminating(self.id) && !warned_term {
                warned_term = true;
                convo.user("[the budget is nearly spent: this is your LAST turn. Say plainly what is finished and \
                    verified and what is not]");
            }
            convo.fit(ctx.cfg.context_chars);
            let reply = match think(ctx, self.id, &model, &convo) {
                Ok(r) => r,
                Err(h) => return self.pause(&h.describe()),
            };
            convo.assistant(&reply);
            let said = chuck_tools::prose(&reply, LEAD_TAGS);
            if !said.trim().is_empty() {
                ctx.events.emit(Event::Said { id: self.id, text: said.clone() });
            }
            let (root_dir, job_gate) = (self.root.clone(), self.gate.clone());
            let exec = Exec { id: self.id, role: Role::Lead, kernel: &ctx.kernel, root: &root_dir, gate: &job_gate,
                              wrap: ctx.cfg.wrap.clone(), stop: ctx.stop.clone(), events: &ctx.events,
                              cmd_timeout: ctx.cfg.cmd_timeout };
            let r = exec.run(&reply);
            self.mutated |= r.mutated;
            let mut fb = r.feedback.clone();
            for call in &r.lead_calls {
                let line = match call.tag.as_str() {
                    "assign" => self.assign(call),
                    "review" => self.review(call),
                    "justify" => self.justify(call),
                    "ask" => self.ask(call),
                    "done" => match self.done(call) {
                        Ok(Ok(o)) => return o,
                        Ok(Err(refused)) => refused,
                        Err(h) => return self.pause(&h.describe()),
                    },
                    other => format!("[{other}: not a lead tool]"),
                };
                fb.push(line);
            }
            let waiting = !self.open_tasks().is_empty();
            if !r.acted() {
                if !self.mutated && self.tasks.is_empty() {
                    return Outcome::Answered { text: said.trim().to_string() };
                }
                if waiting {
                    match self.wait_for_worker() {
                        Ok(m) => fb.extend(m),
                        Err(h) => return self.pause(&h.describe()),
                    }
                } else {
                    fb.push("[no tool calls. Files changed, so only ```done``` finishes this — verify the whole \
                        result, then ```done``` + a summary; the gate tells you what is missing]".into());
                }
            }
            fb.extend(self.drain());
            if !waiting {
                if let Stall::Replan(why) = progress.turn(r.mutated, r.verified, &r.actions) {
                    replans += 1;
                    if replans > 3 {
                        return self.pause(&format!("the lead stopped making progress ({why})"));
                    }
                    fb.push(format!("[you are going in circles ({why}). Replan: list what is still missing for each \
                        criterion and the next concrete step for it]"));
                }
            }
            if ctx.kernel.terminating(self.id) && warned_term {
                return self.pause("the budget ran out");
            }
            {
                let mut g = self.gate.lock().unwrap();
                if g.criteria.len() >= 3 {
                    g.scale = Scale::Large;
                }
                ctx.events.emit(Event::Gate { id: self.id, task: None, phase: g.phase.label().into(),
                                              unmet: g.check().iter().map(Unmet::describe).collect() });
            }
            fb.push(exec.gate_line());
            let open = self.open_tasks();
            if !open.is_empty() {
                fb.push(format!("FLEET: {} open — {}", open.len(), self.tasks.iter().filter(|t| t.open())
                    .map(|t| format!("{} {:?}", t.task.id, t.state).to_lowercase()).collect::<Vec<_>>().join(", ")));
            }
            convo.user(&fb.join("\n\n"));
        }
        self.pause(&format!("used all {} steps", ctx.cfg.lead_steps))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn assign_blocks_parse() {
        let a = parse_assign("race after t1, t2", "title: Retry with backoff\ncriteria:\n- retries 3x :: verify: \
            pytest -q tests/test_retry.py\n- docs mention it\nbrief:\nAdd retry to fetch().\nKeep the API.").unwrap();
        assert!(a.race);
        assert_eq!(a.after, vec!["t1", "t2"]);
        assert_eq!(a.title, "Retry with backoff");
        assert_eq!(a.criteria.len(), 2);
        assert_eq!(a.criteria[0].1.as_deref(), Some("pytest -q tests/test_retry.py"));
        assert_eq!(a.brief, "Add retry to fetch().\nKeep the API.");
        assert!(parse_assign("glm", "title: x\nbrief: do it").is_err(), "criteria are required");
        assert!(parse_assign("someone", "criteria:\n- a").is_err());
    }

    #[test]
    fn critic_verdicts() {
        assert_eq!(parse_verdict("ACCEPT\n"), Verdict::Accepted);
        assert_eq!(parse_verdict("**ACCEPT**"), Verdict::Accepted);
        assert_eq!(parse_verdict("REJECT\n- no test for empty input\n- TODO left"),
                   Verdict::Rejected(vec!["no test for empty input".into(), "TODO left".into()]));
        assert!(matches!(parse_verdict("Looks great to me"), Verdict::Rejected(_)));
    }
}
