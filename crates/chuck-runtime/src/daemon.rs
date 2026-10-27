//! A daemon's run: woken by its trigger, an agent LOOKS and REPORTS. It
//! reads, searches, and runs commands that only look — each one checked by
//! the daemon's read-only rules and run with the whole system read-only —
//! and changes nothing. Its report goes to the inbox; what it proposes, the
//! user decides.

use crate::agent::{think_with, Convo, Ctx, Halt};
use crate::events::Event;
use crate::exec::{refuse_command, research};
use crate::Runtime;
use chuck_core::{Caps, Quota, Role, SpawnSpec};
use chuck_sandbox::{classify_command, Level};
use chuck_tools::{file_blocks, prose, tool_blocks};
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;
use std::sync::Arc;
use std::time::{Duration, Instant};

const TAGS: &[&str] = &["read", "grep", "glob", "map", "bash", "sh", "search", "fetch"];

pub struct DaemonRun {
    pub name: String,
    pub brief: String,
    /// Why it was woken (the trigger's words).
    pub why: String,
    /// What the trigger saw (a command's output, the failed units).
    pub detail: String,
    pub folder: PathBuf,
    pub tokens: u64,
    pub minutes: u64,
    pub steps: usize,
    pub stop: Arc<AtomicBool>,
}

/// How a daemon's run ended: 0 a report, 1 stopped or failed.
pub struct DaemonReport {
    pub status: i32,
    pub report: String,
    pub tokens: u64,
    pub usd: f64,
}

/// A daemon's command: only if it only looks, then with the system read-only.
fn look(root: &Path, cmd: &str, stop: &Arc<AtomicBool>) -> String {
    let jail = chuck_sandbox::bwrap().is_some();
    let mut why = chuck_daemon::read_only_problem(cmd, !jail);
    if why.is_empty() && refuse_command(cmd, Role::Daemon).is_some() {
        why = "it needs root".into();
    }
    if why.is_empty() && classify_command(cmd) != Level::Safe {
        why = "it can change or destroy things".into();
    }
    if !why.is_empty() {
        return format!("[NOT run: {why}. A daemon only looks — put the command in your report as a proposal.]");
    }
    let mut o = chuck_tools::RunOpts::new(root);
    o.timeout = Duration::from_secs(120);
    o.wrap = jail.then(|| chuck_sandbox::Jail::new(root).read_only().wrap());
    o.stop = Some(stop.clone());
    let r = chuck_tools::bash(cmd, o);
    format!("exit {}{}\n{}", r.exit, if r.timed_out { " (timed out)" } else { "" }, chuck_core::evidence::clip_ends(&r.output, 12_000))
}

impl Runtime {
    /// Wake a daemon: blocks until its report (run it on its own thread).
    pub fn daemon(&self, d: DaemonRun) -> DaemonReport {
        let ctx = Ctx { kernel: self.kernel.clone(), model: self.model.clone(), cfg: self.cfg.clone(),
                        events: self.events.clone(), stop: d.stop.clone() };
        let model = self.cfg.fleet.model_for(Role::Daemon).to_string();
        let quota = Quota { tokens: Some(d.tokens), usd: self.cfg.job_quota.usd, seconds: Some(d.minutes as f64 * 60.0) };
        let id = self.kernel.spawn(SpawnSpec::new(format!("daemon: {}", d.name).chars().take(60).collect::<String>(),
                                                  Role::Daemon).quota(quota).caps(Caps::READ | Caps::EXEC | Caps::NET)
                                       .nice(5).brief(d.brief.chars().take(160).collect::<String>()));
        self.events.emit(Event::AgentStarted { id, role: Role::Daemon, name: format!("daemon: {}", d.name), model: model.clone(),
                                               task: None });
        let root = if d.folder.is_dir() { d.folder.clone() } else {
            std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| "/".into())
        };
        let first = format!("{}\n\n(You were woken because: {}.){}\n\nNow: {}. Your working directory: {}.",
                            d.brief.trim(), d.why,
                            if d.detail.is_empty() { String::new() } else { format!("\n{}", d.detail) },
                            chuck_store::ledger::local_stamp("%A %d %B %Y, %H:%M"), root.display());
        let mut convo = Convo::new(chuck_daemon::PROMPT, &first);
        let mut warned = false;
        let (status, report) = 'run: {
            for step in 0..d.steps.max(1) {
                let last = step + 1 == d.steps.max(1);
                if !warned && (last || self.kernel.terminating(id) || self.kernel.over_quota(id).is_some()) {
                    warned = true;
                    convo.user("[this is your LAST turn: write the REPORT now, with no tool blocks]");
                }
                convo.fit(self.cfg.context_chars / 2);
                let reply = match think_with(&ctx, id, &model, &convo, TAGS) {
                    Ok(r) => r,
                    Err(Halt::Stopped) => break 'run (1, "(stopped)".to_string()),
                    Err(h) => break 'run (1, format!("(stopped: {})", h.describe())),
                };
                let reply = chuck_tools::normalize_calls(&reply, TAGS).0;
                let reply = chuck_tools::without_made_up_turns(&reply).to_string();
                convo.assistant(&reply);
                let calls = tool_blocks(&reply, TAGS);
                let writes = file_blocks(&reply);
                if calls.is_empty() && writes.is_empty() {
                    // a call written so nothing could run is not the report
                    let open = chuck_tools::unclosed(&reply, TAGS);
                    if !warned && (!open.is_empty() || chuck_tools::native_tool_markup(&reply)) {
                        let mut fb: Vec<String> = open.iter().map(|o| format!("[NOT run: `{o}` was never closed with ```]")).collect();
                        if fb.is_empty() {
                            fb.push(crate::exec::NATIVE_CALLS.to_string());
                        }
                        convo.user(&fb.join("\n"));
                        continue;
                    }
                    break 'run (0, prose(&reply, TAGS));
                }
                if warned {
                    // told to report, it still reached for tools: what it said is the report
                    break 'run (0, prose(&reply, TAGS));
                }
                let mut fb = Vec::new();
                for w in &writes {
                    fb.push(format!("[NOT written: {} — a daemon changes nothing; propose the change in your report]", w.path));
                }
                for c in calls {
                    let what = if c.arg.trim().is_empty() { c.body.trim().to_string() } else { c.arg.trim().to_string() };
                    let t0 = Instant::now();
                    let out = match c.tag.as_str() {
                        "read" => chuck_tools::read_page(&root, &what, 20_000).map_err(|e| e.to_string())
                            .map(|t| format!("READ {what}:\n{t}")).unwrap_or_else(|e| format!("[read {what}: {e}]")),
                        "grep" => {
                            let (pat, sub) = match what.rsplit_once(char::is_whitespace) {
                                Some((p, s)) if root.join(s).exists() => (p.to_string(), s.to_string()),
                                _ => (what.clone(), String::new()),
                            };
                            chuck_tools::grep(&root, pat.trim(), &sub, 80).unwrap_or_else(|e| format!("[grep: {e}]"))
                        }
                        "glob" => chuck_tools::glob(&root, &what, 200).unwrap_or_else(|e| format!("[glob: {e}]")),
                        "map" => chuck_code::repo_map(&root, 12_000),
                        "search" | "fetch" => research(self.cfg.web.as_deref(), self.cfg.sources, &c.tag, &what).2,
                        _ => {
                            let cmd = if c.body.trim().is_empty() { c.arg.trim() } else { c.body.trim() };
                            look(&root, cmd, &d.stop)
                        }
                    };
                    let ok = !out.starts_with("[NOT run") && !out.starts_with('[');
                    self.kernel.syscall(id, &c.tag, &what, t0.elapsed().as_secs_f64() * 1000.0, out.len(), false);
                    self.events.emit(Event::Action { id, tool: c.tag.clone(), arg: what.chars().take(200).collect(), ok,
                                                     summary: out.lines().next().unwrap_or("").chars().take(200).collect() });
                    fb.push(out);
                }
                convo.user(&fb.join("\n\n"));
            }
            (0, "(no report: it ran out of steps)".to_string())
        };
        let report = if report.trim().is_empty() { "(an empty report)".to_string() } else { report };
        self.kernel.exit(id, status, Some(report.lines().next().unwrap_or("").to_string()));
        self.events.emit(Event::AgentEnded { id, status: if status == 0 { "reported".into() } else { "stopped".into() } });
        let (tokens, usd) = self.kernel.info(id).map(|p| (p.tokens, p.usd)).unwrap_or_default();
        DaemonReport { status, report, tokens, usd }
    }
}
