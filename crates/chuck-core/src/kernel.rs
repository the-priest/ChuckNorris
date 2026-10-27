//! The agent kernel: processes, a token scheduler, quotas, signals and IPC.
//!
//! Tokens are the new CPU time. Every agent is a process with a parent, a
//! priority, a quota of tokens, dollars and seconds, and everything it spends
//! is charged to it and to every ancestor. Before each model call a process
//! asks the scheduler for a slot; slots go to the most important process that
//! has waited longest, never to a stopped one, and never past a quota: there
//! the process is sent TERM (it gets exactly one more call, to write its
//! report) and then KILL.
//!
//! Nothing here knows about GTK, the network or the model.

use crate::types::{AgentId, Caps, Quota, Role, Spend};
use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProcState {
    Ready,
    Running,
    Waiting,
    Stopped,
    Done,
    Failed,
    Killed,
}

impl ProcState {
    pub fn alive(self) -> bool {
        matches!(self, ProcState::Ready | ProcState::Running | ProcState::Waiting | ProcState::Stopped)
    }

    pub fn label(self) -> &'static str {
        match self {
            ProcState::Ready => "ready",
            ProcState::Running => "running",
            ProcState::Waiting => "waiting",
            ProcState::Stopped => "stopped",
            ProcState::Done => "done",
            ProcState::Failed => "failed",
            ProcState::Killed => "killed",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Signal {
    Stop,
    Cont,
    Term,
    Kill,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Killed(pub String);

impl std::fmt::Display for Killed {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "process stopped: {}", self.0)
    }
}

impl std::error::Error for Killed {}

#[derive(Clone, Debug)]
pub struct Syscall {
    pub at: f64,
    pub tool: String,
    pub arg: String,
    pub ms: f64,
    pub size: usize,
    pub denied: bool,
}

#[derive(Clone, Debug)]
pub struct Mail {
    pub from: AgentId,
    pub text: String,
}

#[derive(Debug)]
struct Process {
    pid: AgentId,
    ppid: Option<AgentId>,
    name: String,
    role: Role,
    brief: String,
    nice: i32,
    quota: Quota,
    caps: Caps,
    state: ProcState,
    started: f64,
    ended: Option<f64>,
    own: Spend,
    total: Spend,
    children: Vec<AgentId>,
    activity: String,
    exit_status: Option<i32>,
    result: Option<String>,
    mailbox: VecDeque<Mail>,
    terminating: bool,
    final_granted: bool,
    term_reason: String,
    stop_pending: bool,
    since: f64,
    recent: VecDeque<(f64, u64)>,
    syscalls: VecDeque<Syscall>,
}

/// One row of /top.
#[derive(Clone, Debug)]
pub struct ProcInfo {
    pub pid: AgentId,
    pub ppid: Option<AgentId>,
    pub name: String,
    pub role: Role,
    pub state: ProcState,
    pub nice: i32,
    pub activity: String,
    pub tokens: u64,
    pub usd: f64,
    pub calls: u64,
    pub rate: f64,
    pub seconds: f64,
    pub quota: Quota,
    pub terminating: bool,
    pub caps: Caps,
    pub children: Vec<AgentId>,
    pub brief: String,
    pub exit_status: Option<i32>,
}

/// A clock in seconds. Monotonic in production; settable in tests.
pub type Clock = Arc<dyn Fn() -> f64 + Send + Sync>;

pub fn monotonic() -> Clock {
    let t0 = Instant::now();
    Arc::new(move || t0.elapsed().as_secs_f64())
}

fn wall() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

struct State {
    procs: HashMap<AgentId, Process>,
    next_pid: u32,
    queue: Vec<(u64, AgentId)>,
    seq: u64,
    running: std::collections::HashSet<AgentId>,
    board: HashMap<String, (String, AgentId, f64)>,
    journal: VecDeque<(f64, AgentId, &'static str, String)>,
}

pub struct Kernel {
    slots: usize,
    clock: Clock,
    st: Mutex<State>,
    cv: Condvar,
    on_change: Mutex<Option<Box<dyn Fn() + Send + Sync>>>,
}

/// What to spawn.
#[derive(Clone, Debug)]
pub struct SpawnSpec {
    pub name: String,
    pub role: Role,
    pub brief: String,
    pub parent: Option<AgentId>,
    pub nice: i32,
    pub quota: Quota,
    pub caps: Option<Caps>,
}

impl SpawnSpec {
    pub fn new(name: impl Into<String>, role: Role) -> SpawnSpec {
        SpawnSpec { name: name.into(), role, brief: String::new(), parent: None, nice: 0,
                    quota: Quota::default(), caps: None }
    }
    pub fn parent(mut self, p: AgentId) -> Self {
        self.parent = Some(p);
        self
    }
    pub fn quota(mut self, q: Quota) -> Self {
        self.quota = q;
        self
    }
    pub fn brief(mut self, b: impl Into<String>) -> Self {
        self.brief = b.into();
        self
    }
    pub fn caps(mut self, c: Caps) -> Self {
        self.caps = Some(c);
        self
    }
    pub fn nice(mut self, n: i32) -> Self {
        self.nice = n;
        self
    }
}

impl Kernel {
    pub fn new(slots: usize) -> Kernel {
        Kernel::with_clock(slots, monotonic())
    }

    pub fn with_clock(slots: usize, clock: Clock) -> Kernel {
        Kernel {
            slots: slots.max(1),
            clock,
            st: Mutex::new(State {
                procs: HashMap::new(),
                next_pid: 1,
                queue: Vec::new(),
                seq: 0,
                running: Default::default(),
                board: HashMap::new(),
                journal: VecDeque::new(),
            }),
            cv: Condvar::new(),
            on_change: Mutex::new(None),
        }
    }

    pub fn slots(&self) -> usize {
        self.slots
    }

    /// Called (from any thread) whenever the process table changes.
    pub fn set_on_change(&self, f: impl Fn() + Send + Sync + 'static) {
        *self.on_change.lock().unwrap() = Some(Box::new(f));
    }

    fn lock(&self) -> MutexGuard<'_, State> {
        self.st.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn now(&self) -> f64 {
        (self.clock)()
    }

    fn changed(&self) {
        if let Some(f) = self.on_change.lock().unwrap().as_ref() {
            f();
        }
    }

    // ── lifecycle ──

    /// A new process. Its quota is clipped to what its parent has left, and
    /// its capabilities to its parent's.
    pub fn spawn(&self, spec: SpawnSpec) -> AgentId {
        let now = self.now();
        let pid;
        {
            let mut st = self.lock();
            pid = AgentId(st.next_pid);
            st.next_pid += 1;
            let mut q = spec.quota;
            let mut caps = spec.caps.unwrap_or(spec.role.default_caps());
            if let Some(par) = spec.parent.and_then(|p| st.procs.get(&p)) {
                if let Some(left) = left_tokens(par) {
                    q.tokens = Some(q.tokens.map_or(left, |t| t.min(left)));
                }
                if let Some(left) = left_usd(par) {
                    q.usd = Some(q.usd.map_or(left, |t| t.min(left)));
                }
                if let Some(left) = left_secs(par, now) {
                    q.seconds = Some(q.seconds.map_or(left, |t| t.min(left)));
                }
                caps = caps.clip(par.caps);
            }
            let ppid = spec.parent.filter(|p| st.procs.contains_key(p));
            st.procs.insert(pid, Process {
                pid, ppid, name: spec.name.clone(), role: spec.role, brief: spec.brief,
                nice: spec.nice.clamp(-10, 10), quota: q, caps, state: ProcState::Ready,
                started: now, ended: None, own: Spend::default(), total: Spend::default(),
                children: Vec::new(), activity: String::new(), exit_status: None, result: None,
                mailbox: VecDeque::new(), terminating: false, final_granted: false,
                term_reason: String::new(), stop_pending: false, since: now,
                recent: VecDeque::new(), syscalls: VecDeque::new(),
            });
            if let Some(pp) = ppid {
                if let Some(par) = st.procs.get_mut(&pp) {
                    par.children.push(pid);
                }
            }
            let detail = format!("{} {}", spec.role.label(), spec.name);
            log(&mut st, pid, "spawn", detail);
        }
        self.changed();
        pid
    }

    /// A process finished (status 0 = success). Releases its slot.
    pub fn exit(&self, pid: AgentId, status: i32, result: Option<String>) {
        {
            let mut st = self.lock();
            let now = self.now();
            let Some(p) = st.procs.get_mut(&pid) else { return };
            if !p.state.alive() {
                return;
            }
            p.state = if status == 0 { ProcState::Done } else { ProcState::Failed };
            p.exit_status = Some(status);
            p.result = result;
            p.ended = Some(now);
            p.activity.clear();
            st.running.remove(&pid);
            st.queue.retain(|(_, q)| *q != pid);
            log(&mut st, pid, "exit", format!("status {status}"));
        }
        self.cv.notify_all();
        self.changed();
    }

    /// Block until a process ends -> (status, result), or None on timeout.
    pub fn wait(&self, pid: AgentId, timeout: Option<Duration>) -> Option<(i32, Option<String>)> {
        let end = timeout.map(|t| Instant::now() + t);
        let mut st = self.lock();
        loop {
            match st.procs.get(&pid) {
                None => return None,
                Some(p) if !p.state.alive() => return Some((p.exit_status.unwrap_or(-1), p.result.clone())),
                _ => {}
            }
            let left = match end {
                Some(e) => match e.checked_duration_since(Instant::now()) {
                    Some(d) if !d.is_zero() => d,
                    _ => return None,
                },
                None => Duration::from_millis(250),
            };
            st = self.cv.wait_timeout(st, left.min(Duration::from_millis(250))).unwrap().0;
        }
    }

    // ── the scheduler ──

    /// Wait for a model-call slot. Ok(true) when granted, Ok(false) on
    /// timeout, Err when the process may not go on. A process past its quota
    /// is sent TERM and gets ONE more slot, to write its report; then KILL.
    pub fn acquire(&self, pid: AgentId, timeout: Option<Duration>) -> Result<bool, Killed> {
        let end = timeout.map(|t| Instant::now() + t);
        let mut st = self.lock();
        let now = self.now();
        {
            let why = over(&st, pid, now);
            let p = st.procs.get_mut(&pid).ok_or_else(|| Killed(format!("no process {pid}")))?;
            if !p.state.alive() {
                return Err(Killed(format!("process {pid} is not running")));
            }
            if let Some(why) = why {
                if !p.terminating {
                    p.terminating = true;
                    p.term_reason = why.clone();
                    log(&mut st, pid, "signal", format!("TERM ({why})"));
                } else if p.final_granted {
                    kill(&mut st, pid, &why, now);
                    drop(st);
                    self.cv.notify_all();
                    self.changed();
                    return Err(Killed(why));
                }
            }
        }
        st.seq += 1;
        let entry = (st.seq, pid);
        st.queue.push(entry);
        if let Some(p) = st.procs.get_mut(&pid) {
            p.since = now;
            if p.state != ProcState::Stopped {
                p.state = ProcState::Waiting;
            }
        }
        let granted = loop {
            let alive = st.procs.get(&pid).map(|p| (p.state.alive(), p.state, p.term_reason.clone()));
            match alive {
                Some((false, _, why)) => {
                    st.queue.retain(|e| *e != entry);
                    return Err(Killed(if why.is_empty() { "killed".into() } else { why }));
                }
                None => return Err(Killed("gone".into())),
                Some((true, state, _)) => {
                    if state != ProcState::Stopped && st.running.len() < self.slots
                        && next(&st, self.now()) == Some(pid)
                    {
                        break true;
                    }
                }
            }
            let left = match end {
                Some(e) => match e.checked_duration_since(Instant::now()) {
                    Some(d) if !d.is_zero() => d,
                    _ => break false,
                },
                None => Duration::from_millis(250),
            };
            st = self.cv.wait_timeout(st, left.min(Duration::from_millis(250))).unwrap().0;
        };
        st.queue.retain(|e| *e != entry);
        if granted {
            st.running.insert(pid);
            let n = st.running.len();
            if let Some(p) = st.procs.get_mut(&pid) {
                p.state = ProcState::Running;
                if p.terminating {
                    p.final_granted = true;
                }
            }
            log(&mut st, pid, "slot", format!("{n}/{}", self.slots));
        } else if let Some(p) = st.procs.get_mut(&pid) {
            if p.state == ProcState::Waiting {
                p.state = ProcState::Ready;
            }
        }
        drop(st);
        self.changed();
        Ok(granted)
    }

    pub fn release(&self, pid: AgentId) {
        {
            let mut st = self.lock();
            st.running.remove(&pid);
            if let Some(p) = st.procs.get_mut(&pid) {
                if p.state == ProcState::Running {
                    p.state = if p.stop_pending { ProcState::Stopped } else { ProcState::Ready };
                }
            }
        }
        self.cv.notify_all();
        self.changed();
    }

    // ── accounting ──

    /// Book one model call to the process and every ancestor.
    pub fn charge(&self, pid: AgentId, prompt: u64, completion: u64, usd: f64) {
        let now = self.now();
        {
            let mut st = self.lock();
            let Some(p) = st.procs.get_mut(&pid) else { return };
            p.own.prompt += prompt;
            p.own.completion += completion;
            p.own.usd += usd;
            p.own.calls += 1;
            let mut cur = Some(pid);
            while let Some(c) = cur {
                let Some(q) = st.procs.get_mut(&c) else { break };
                q.total.prompt += prompt;
                q.total.completion += completion;
                q.total.usd += usd;
                q.total.calls += 1;
                q.recent.push_back((now, prompt + completion));
                while q.recent.len() > 64 {
                    q.recent.pop_front();
                }
                cur = q.ppid;
            }
            log(&mut st, pid, "charge", format!("{prompt}+{completion} tok ${usd:.5}"));
        }
        self.changed();
    }

    /// Why this process may not go on ("token quota reached", or a parent's),
    /// or None. A parent's quota caps its whole subtree.
    pub fn over_quota(&self, pid: AgentId) -> Option<String> {
        let st = self.lock();
        over(&st, pid, self.now())
    }

    /// Tokens left before this process (or an ancestor) hits its quota.
    pub fn tokens_left(&self, pid: AgentId) -> Option<u64> {
        let st = self.lock();
        let mut cur = Some(pid);
        let mut best: Option<u64> = None;
        while let Some(c) = cur {
            let Some(p) = st.procs.get(&c) else { break };
            if let Some(l) = left_tokens(p) {
                best = Some(best.map_or(l, |b| b.min(l)));
            }
            cur = p.ppid;
        }
        best
    }

    // ── signals ──

    /// STOP / CONT / TERM / KILL to the process and its descendants.
    pub fn signal(&self, pid: AgentId, sig: Signal) -> bool {
        {
            let mut st = self.lock();
            if !st.procs.contains_key(&pid) {
                return false;
            }
            let now = self.now();
            for q in tree(&st, pid) {
                let Some(p) = st.procs.get_mut(&q) else { continue };
                if !p.state.alive() {
                    continue;
                }
                match sig {
                    Signal::Stop => {
                        if p.state != ProcState::Running {
                            p.state = ProcState::Stopped;
                        }
                        p.stop_pending = true;
                        log(&mut st, q, "signal", "STOP".into());
                    }
                    Signal::Cont => {
                        p.stop_pending = false;
                        if p.state == ProcState::Stopped {
                            let queued = st.queue.iter().any(|e| e.1 == q);
                            let p = st.procs.get_mut(&q).unwrap();
                            p.state = if queued { ProcState::Waiting } else { ProcState::Ready };
                        }
                        log(&mut st, q, "signal", "CONT".into());
                    }
                    Signal::Term => {
                        if !p.terminating {
                            p.terminating = true;
                            p.term_reason = "asked to finish".into();
                            log(&mut st, q, "signal", "TERM (asked to finish)".into());
                        }
                    }
                    Signal::Kill => kill(&mut st, q, "killed", now),
                }
            }
        }
        self.cv.notify_all();
        self.changed();
        true
    }

    /// True once the process must stop working (killed, or ended).
    pub fn should_stop(&self, pid: AgentId) -> bool {
        self.lock().procs.get(&pid).is_none_or(|p| !p.state.alive())
    }

    /// True once the process was asked to finish: its next call is its last.
    pub fn terminating(&self, pid: AgentId) -> bool {
        self.lock().procs.get(&pid).is_some_and(|p| p.terminating)
    }

    pub fn renice(&self, pid: AgentId, nice: i32) -> bool {
        let ok = {
            let mut st = self.lock();
            match st.procs.get_mut(&pid) {
                Some(p) => {
                    p.nice = nice.clamp(-10, 10);
                    true
                }
                None => false,
            }
        };
        self.changed();
        ok
    }

    /// What a call in flight has done so far (shown, not journaled: it
    /// changes every second).
    pub fn progress(&self, pid: AgentId, activity: &str) {
        {
            let mut st = self.lock();
            let Some(p) = st.procs.get_mut(&pid) else { return };
            p.activity = activity.chars().take(160).collect();
        }
        self.changed();
    }

    pub fn note(&self, pid: AgentId, activity: &str) {
        {
            let mut st = self.lock();
            let Some(p) = st.procs.get_mut(&pid) else { return };
            p.activity = activity.chars().take(160).collect();
            if !activity.is_empty() {
                let a = p.activity.clone();
                log(&mut st, pid, "do", a);
            }
        }
        self.changed();
    }

    // ── IPC ──

    pub fn send(&self, to: AgentId, text: &str, from: AgentId) -> bool {
        let ok = {
            let mut st = self.lock();
            match st.procs.get_mut(&to) {
                Some(p) if p.state.alive() => {
                    p.mailbox.push_back(Mail { from, text: text.to_string() });
                    log(&mut st, to, "mail", format!("from {from}"));
                    true
                }
                _ => false,
            }
        };
        self.cv.notify_all();
        ok
    }

    /// Every message waiting for a process, oldest first.
    pub fn drain(&self, pid: AgentId) -> Vec<Mail> {
        let mut st = self.lock();
        st.procs.get_mut(&pid).map(|p| p.mailbox.drain(..).collect()).unwrap_or_default()
    }

    /// The blackboard: findings every process can read.
    pub fn post(&self, key: &str, value: &str, by: AgentId) {
        {
            let mut st = self.lock();
            let k: String = key.chars().take(200).collect();
            st.board.insert(k.clone(), (value.to_string(), by, wall()));
            log(&mut st, by, "post", k.chars().take(80).collect());
        }
        self.changed();
    }

    /// (key, value, by, time), newest first.
    pub fn board(&self) -> Vec<(String, String, AgentId, f64)> {
        let st = self.lock();
        let mut v: Vec<_> = st.board.iter().map(|(k, (v, b, t))| (k.clone(), v.clone(), *b, *t)).collect();
        v.sort_by(|a, b| b.3.partial_cmp(&a.3).unwrap_or(std::cmp::Ordering::Equal));
        v
    }

    /// Waiting on something outside the scheduler (the user's answer).
    pub fn block(&self, pid: AgentId, why: &str) {
        {
            let mut st = self.lock();
            let Some(p) = st.procs.get_mut(&pid) else { return };
            if !p.state.alive() || p.state == ProcState::Stopped {
                return;
            }
            p.state = ProcState::Waiting;
            p.activity = why.chars().take(160).collect();
        }
        self.changed();
    }

    pub fn unblock(&self, pid: AgentId) {
        {
            let mut st = self.lock();
            let queued = st.queue.iter().any(|e| e.1 == pid);
            if let Some(p) = st.procs.get_mut(&pid) {
                if p.state == ProcState::Waiting && !queued {
                    p.state = ProcState::Ready;
                }
            }
        }
        self.changed();
    }

    // ── capabilities and syscalls ──

    pub fn caps(&self, pid: AgentId) -> Caps {
        self.lock().procs.get(&pid).map_or(Caps::NONE, |p| p.caps)
    }

    pub fn can(&self, pid: AgentId, cap: Caps) -> bool {
        self.lock().procs.get(&pid).is_some_and(|p| p.caps.has(cap))
    }

    pub fn role(&self, pid: AgentId) -> Option<Role> {
        self.lock().procs.get(&pid).map(|p| p.role)
    }

    pub fn syscall(&self, pid: AgentId, tool: &str, arg: &str, ms: f64, size: usize, denied: bool) {
        let mut st = self.lock();
        let Some(p) = st.procs.get_mut(&pid) else { return };
        let a: String = arg.split_whitespace().collect::<Vec<_>>().join(" ").chars().take(160).collect();
        p.syscalls.push_back(Syscall { at: wall(), tool: tool.into(), arg: a.clone(), ms, size, denied });
        while p.syscalls.len() > 1000 {
            p.syscalls.pop_front();
        }
        log(&mut st, pid, if denied { "denied" } else { "syscall" },
            format!("{tool} {}", a.chars().take(80).collect::<String>()));
    }

    pub fn strace(&self, pid: AgentId) -> Vec<Syscall> {
        self.lock().procs.get(&pid).map(|p| p.syscalls.iter().cloned().collect()).unwrap_or_default()
    }

    // ── views ──

    pub fn ps(&self, all: bool) -> Vec<ProcInfo> {
        let st = self.lock();
        let now = self.now();
        let mut rows: Vec<ProcInfo> = st.procs.values().filter(|p| all || p.state.alive()).map(|p| {
            let cutoff = now - 10.0;
            let rate = p.recent.iter().filter(|(t, _)| *t >= cutoff).map(|(_, n)| *n as f64).sum::<f64>() / 10.0;
            ProcInfo {
                pid: p.pid, ppid: p.ppid, name: p.name.clone(), role: p.role, state: p.state,
                nice: p.nice, activity: p.activity.clone(), tokens: p.total.tokens(), usd: p.total.usd,
                calls: p.total.calls, rate, seconds: p.ended.unwrap_or(now) - p.started,
                quota: p.quota, terminating: p.terminating, caps: p.caps, children: p.children.clone(),
                brief: p.brief.chars().take(200).collect(), exit_status: p.exit_status,
            }
        }).collect();
        rows.sort_by_key(|r| r.pid);
        rows
    }

    pub fn info(&self, pid: AgentId) -> Option<ProcInfo> {
        self.ps(true).into_iter().find(|r| r.pid == pid)
    }

    pub fn journal(&self, pid: Option<AgentId>, limit: usize) -> Vec<(f64, AgentId, String, String)> {
        let st = self.lock();
        let v: Vec<_> = st.journal.iter().filter(|e| pid.is_none_or(|p| e.1 == p))
            .map(|e| (e.0, e.1, e.2.to_string(), e.3.clone())).collect();
        v[v.len().saturating_sub(limit)..].to_vec()
    }
}

// ── internals (called with the lock held) ──

fn log(st: &mut State, pid: AgentId, ev: &'static str, detail: String) {
    st.journal.push_back((wall(), pid, ev, detail));
    while st.journal.len() > 5000 {
        st.journal.pop_front();
    }
}

fn left_tokens(p: &Process) -> Option<u64> {
    p.quota.tokens.map(|l| l.saturating_sub(p.total.tokens()))
}

fn left_usd(p: &Process) -> Option<f64> {
    p.quota.usd.map(|l| (l - p.total.usd).max(0.0))
}

fn left_secs(p: &Process, now: f64) -> Option<f64> {
    p.quota.seconds.map(|l| (l - (now - p.started)).max(0.0))
}

fn over(st: &State, pid: AgentId, now: f64) -> Option<String> {
    let mut cur = Some(pid);
    let mut own = true;
    while let Some(c) = cur {
        let p = st.procs.get(&c)?;
        let hits = [
            ("token", left_tokens(p).map(|l| l == 0)),
            ("spending", left_usd(p).map(|l| l <= 0.0)),
            ("time", left_secs(p, now).map(|l| l <= 0.0)),
        ];
        for (label, hit) in hits {
            if hit == Some(true) {
                return Some(if own {
                    format!("{label} quota reached")
                } else {
                    format!("{label} quota of pid {} ({}) reached", p.pid, p.name)
                });
            }
        }
        cur = p.ppid;
        own = false;
    }
    None
}

/// The waiting process that gets the next free slot: lowest nice, aged by how
/// long it has waited (a nice-10 process still runs eventually), then first
/// come first served.
fn next(st: &State, now: f64) -> Option<AgentId> {
    let mut best: Option<(f64, u64, AgentId)> = None;
    for &(seq, pid) in &st.queue {
        let Some(p) = st.procs.get(&pid) else { continue };
        if p.state == ProcState::Stopped || !p.state.alive() {
            continue;
        }
        let k = (p.nice as f64 - (now - p.since) / 30.0, seq, pid);
        if best.as_ref().is_none_or(|b| (k.0, k.1) < (b.0, b.1)) {
            best = Some(k);
        }
    }
    best.map(|b| b.2)
}

fn tree(st: &State, pid: AgentId) -> Vec<AgentId> {
    let mut out = Vec::new();
    let mut todo = vec![pid];
    while let Some(q) = todo.pop() {
        out.push(q);
        if let Some(p) = st.procs.get(&q) {
            todo.extend(p.children.iter().copied());
        }
    }
    out
}

fn kill(st: &mut State, pid: AgentId, why: &str, now: f64) {
    let Some(p) = st.procs.get_mut(&pid) else { return };
    if !p.state.alive() {
        return;
    }
    p.state = ProcState::Killed;
    p.term_reason = why.to_string();
    p.exit_status = Some(-9);
    p.ended = Some(now);
    p.activity.clear();
    st.running.remove(&pid);
    log(st, pid, "signal", format!("KILL ({why})"));
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    fn clock() -> (Clock, Arc<AtomicU64>) {
        let t = Arc::new(AtomicU64::new(0));
        let t2 = t.clone();
        (Arc::new(move || t2.load(Ordering::SeqCst) as f64 / 1000.0), t)
    }

    #[test]
    fn child_quota_and_caps_are_clipped_to_the_parent() {
        let k = Kernel::new(2);
        let lead = k.spawn(SpawnSpec::new("lead", Role::Lead).quota(Quota::tokens(1000)));
        let w = k.spawn(SpawnSpec::new("w", Role::GlmWorker).parent(lead).quota(Quota::tokens(5000))
            .caps(Caps::ALL));
        let info = k.info(w).unwrap();
        assert_eq!(info.quota.tokens, Some(1000));
        assert!(info.caps.has(Caps::SPAWN)); // the lead has all, so the worker may too
        let d = k.spawn(SpawnSpec::new("d", Role::Daemon).parent(w).caps(Caps::ALL));
        assert!(!k.caps(d).has(Caps::SPAWN) || k.caps(w).has(Caps::SPAWN));
        let r = k.spawn(SpawnSpec::new("r", Role::GlmWorker).parent(lead).caps(Caps::READ));
        let rr = k.spawn(SpawnSpec::new("rr", Role::GlmWorker).parent(r).caps(Caps::ALL));
        assert_eq!(k.caps(rr), Caps::READ, "a child never does more than its parent");
    }

    #[test]
    fn spend_is_charged_up_the_tree_and_a_parent_quota_caps_its_subtree() {
        let k = Kernel::new(4);
        let lead = k.spawn(SpawnSpec::new("lead", Role::Lead).quota(Quota::tokens(1000)));
        let a = k.spawn(SpawnSpec::new("a", Role::DeepSeekWorker).parent(lead));
        let b = k.spawn(SpawnSpec::new("b", Role::GlmWorker).parent(lead));
        k.charge(a, 400, 100, 0.01);
        k.charge(b, 400, 100, 0.01);
        assert_eq!(k.info(lead).unwrap().tokens, 1000);
        assert!(k.over_quota(a).unwrap().contains("of pid 1"));
    }

    #[test]
    fn past_quota_a_process_gets_one_last_call_then_is_killed() {
        let k = Kernel::new(1);
        let p = k.spawn(SpawnSpec::new("w", Role::DeepSeekWorker).quota(Quota::tokens(10)));
        assert_eq!(k.acquire(p, None), Ok(true));
        k.charge(p, 10, 0, 0.0);
        k.release(p);
        assert_eq!(k.acquire(p, None), Ok(true), "TERM: one more call to report");
        assert!(k.terminating(p));
        k.release(p);
        assert!(k.acquire(p, None).is_err(), "then KILL");
        assert_eq!(k.info(p).unwrap().state, ProcState::Killed);
    }

    #[test]
    fn slots_go_to_the_lowest_nice_and_waiting_ages() {
        let (clk, t) = clock();
        let k = Arc::new(Kernel::with_clock(1, clk));
        let a = k.spawn(SpawnSpec::new("a", Role::GlmWorker));
        let hi = k.spawn(SpawnSpec::new("hi", Role::DeepSeekWorker).nice(-5));
        let lo = k.spawn(SpawnSpec::new("lo", Role::GlmWorker).nice(5));
        assert_eq!(k.acquire(a, None), Ok(true));
        let order = Arc::new(Mutex::new(Vec::new()));
        let mut hs = vec![];
        for pid in [lo, hi] {
            let (k, o) = (k.clone(), order.clone());
            hs.push(std::thread::spawn(move || {
                k.acquire(pid, None).unwrap();
                o.lock().unwrap().push(pid);
                std::thread::sleep(Duration::from_millis(30));
                k.release(pid);
            }));
            std::thread::sleep(Duration::from_millis(50));
        }
        t.store(1000, Ordering::SeqCst);
        k.release(a);
        for h in hs {
            h.join().unwrap();
        }
        assert_eq!(*order.lock().unwrap(), vec![hi, lo]);
    }

    #[test]
    fn stop_parks_and_kill_reaches_children() {
        let k = Kernel::new(2);
        let lead = k.spawn(SpawnSpec::new("lead", Role::Lead));
        let w = k.spawn(SpawnSpec::new("w", Role::GlmWorker).parent(lead));
        k.signal(w, Signal::Stop);
        assert_eq!(k.acquire(w, Some(Duration::from_millis(300))), Ok(false));
        k.signal(w, Signal::Cont);
        assert_eq!(k.acquire(w, Some(Duration::from_millis(300))), Ok(true));
        k.release(w);
        k.signal(lead, Signal::Kill);
        assert!(k.should_stop(w));
        assert!(k.acquire(w, None).is_err());
    }

    #[test]
    fn mail_and_board() {
        let k = Kernel::new(1);
        let a = k.spawn(SpawnSpec::new("a", Role::Lead));
        let b = k.spawn(SpawnSpec::new("b", Role::GlmWorker).parent(a));
        assert!(k.send(b, "also keep port 80", a));
        let m = k.drain(b);
        assert_eq!(m.len(), 1);
        assert_eq!(m[0].from, a);
        k.post("db", "postgres on :5432", b);
        assert_eq!(k.board()[0].1, "postgres on :5432");
    }

    #[test]
    fn wait_returns_the_result() {
        let k = Arc::new(Kernel::new(1));
        let p = k.spawn(SpawnSpec::new("w", Role::GlmWorker));
        let k2 = k.clone();
        let h = std::thread::spawn(move || k2.wait(p, Some(Duration::from_secs(5))));
        std::thread::sleep(Duration::from_millis(50));
        k.exit(p, 0, Some("REPORT".into()));
        assert_eq!(h.join().unwrap(), Some((0, Some("REPORT".into()))));
    }
}
