//! Carrying out one reply: every block, in order, through the capability
//! guard and the Done gate, with its evidence recorded.

use crate::events::{Event, Events};
use chuck_core::evidence::test_counts;
use chuck_core::{tool_cap, AgentId, Caps, CriterionId, DoneGate, EvidenceKind, Kernel, Role, Unmet};
use chuck_tools::{bash, file_blocks, fingerprint, tool_blocks, RunOpts, Wrap};
use std::path::Path;
use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

pub const WORKER_TAGS: &[&str] = &["read", "grep", "glob", "bash", "sh", "test", "submit"];
pub const LEAD_TAGS: &[&str] = &["read", "grep", "glob", "bash", "sh", "test", "criteria", "plan", "assign",
    "review", "justify", "done", "ask"];
const CLIP: usize = 20_000;

/// A lead-only call the lead loop handles (needs the fleet).
#[derive(Clone, Debug)]
pub struct LeadCall {
    pub tag: String,
    pub arg: String,
    pub body: String,
}

#[derive(Default, Debug)]
pub struct StepResult {
    pub feedback: Vec<String>,
    pub mutated: bool,
    pub verified: bool,
    pub actions: Vec<String>,
    pub submit: Option<String>,
    pub lead_calls: Vec<LeadCall>,
}

impl StepResult {
    pub fn acted(&self) -> bool {
        !self.actions.is_empty() || self.submit.is_some() || !self.lead_calls.is_empty()
    }
}

pub struct Exec<'a> {
    pub id: AgentId,
    pub role: Role,
    pub kernel: &'a Kernel,
    pub root: &'a Path,
    pub gate: &'a Mutex<DoneGate>,
    pub wrap: Option<Wrap>,
    pub stop: Arc<AtomicBool>,
    pub events: &'a Events,
    pub cmd_timeout: Duration,
}

fn clip(s: &str, n: usize) -> String {
    if s.len() <= n {
        return s.to_string();
    }
    let mut cut = n;
    while !s.is_char_boundary(cut) {
        cut -= 1;
    }
    format!("{}\n… ({} more characters)", &s[..cut], s.len() - cut)
}

/// "pytest -q for c1,c2" -> ("pytest -q", [c1, c2])
pub fn split_for(arg: &str) -> (String, Vec<CriterionId>) {
    let re = regex::Regex::new(r"\s+for\s+((?:c\d+\s*,?\s*)+)$").unwrap();
    match re.captures(arg) {
        Some(c) => {
            let ids = regex::Regex::new(r"c(\d+)").unwrap().captures_iter(&c[1])
                .filter_map(|m| m[1].parse().ok().map(CriterionId)).collect();
            (arg[..c.get(0).unwrap().start()].trim().to_string(), ids)
        }
        None => (arg.trim().to_string(), Vec::new()),
    }
}

/// "- text :: verify: cmd" lines.
pub fn parse_criteria(body: &str) -> Vec<(String, Option<String>)> {
    body.lines().filter_map(|l| {
        let l = l.trim().trim_start_matches(['-', '*', '•']).trim();
        if l.is_empty() || l.to_lowercase().starts_with("criteria") {
            return None;
        }
        Some(match l.split_once("::") {
            Some((t, v)) => {
                let v = v.trim();
                let v = v.strip_prefix("verify:").or_else(|| v.strip_prefix("verify")).unwrap_or(v).trim();
                (t.trim().to_string(), Some(v.trim_matches('`').to_string()).filter(|s| !s.is_empty()))
            }
            None => (l.to_string(), None),
        })
    }).collect()
}

impl<'a> Exec<'a> {
    fn allowed(&self, tool: &str, arg: &str) -> Result<(), String> {
        if let Some(cap) = tool_cap(tool) {
            if !self.kernel.can(self.id, cap) {
                self.kernel.syscall(self.id, tool, arg, 0.0, 0, true);
                let need = cap.names().join(",");
                return Err(format!("[{tool} denied: this agent doesn't have the `{need}` capability]"));
            }
        }
        Ok(())
    }

    fn mutation_ok(&self) -> Result<(), String> {
        self.gate.lock().unwrap().allow_mutation().map_err(|unmet| {
            format!("[refused — nothing may change yet: {}]",
                    unmet.iter().map(Unmet::describe).collect::<Vec<_>>().join("; "))
        })
    }

    fn act(&self, tool: &str, arg: &str, ok: bool, summary: &str) {
        self.events.emit(Event::Action { id: self.id, tool: tool.into(), arg: arg.chars().take(200).collect(),
                                         ok, summary: summary.chars().take(300).collect() });
    }

    fn run_cmd(&self, cmd: &str) -> chuck_tools::RunResult {
        let id = self.id;
        let ev = self.events.clone();
        let mut on = move |c: &str| ev.emit(Event::Output { id, chunk: c.to_string() });
        let mut o = RunOpts::new(self.root);
        o.timeout = self.cmd_timeout;
        o.wrap = self.wrap.clone();
        o.stop = Some(self.stop.clone());
        o.on_chunk = Some(&mut on);
        bash(cmd, o)
    }

    pub fn run(&self, reply: &str) -> StepResult {
        let mut res = StepResult::default();
        let tags: &[&str] = if self.role == Role::Lead { LEAD_TAGS } else { WORKER_TAGS };
        enum Item {
            File(chuck_tools::FileBlock),
            Tool(chuck_tools::ToolBlock),
        }
        let mut items: Vec<(usize, Item)> = file_blocks(reply).into_iter().map(|b| (b.start, Item::File(b))).collect();
        items.extend(tool_blocks(reply, tags).into_iter().map(|b| (b.start, Item::Tool(b))));
        items.sort_by_key(|(s, _)| *s);
        let all_criteria: Vec<CriterionId> = self.gate.lock().unwrap().criteria.iter().map(|c| c.id).collect();
        for (_, item) in items {
            if self.kernel.should_stop(self.id) {
                res.feedback.push("[stopped]".into());
                break;
            }
            match item {
                Item::File(b) => self.file(&b, &all_criteria, &mut res),
                Item::Tool(t) => self.tool(&t, &mut res),
            }
        }
        res
    }

    fn file(&self, b: &chuck_tools::FileBlock, all: &[CriterionId], res: &mut StepResult) {
        let label = format!("{} {}", b.tag, b.path);
        res.actions.push(label.clone());
        if b.ambiguous {
            res.feedback.push(format!("[NOTHING was written: {} contains ``` fences inside a 3-backtick block, so \
                its end is ambiguous. Send it again wrapped in FOUR backticks: ````write {} … ````]", b.path, b.path));
            self.act(&b.tag, &b.path, false, "fences unclear: asked to resend");
            return;
        }
        if let Err(e) = self.allowed("write", &b.path).and_then(|_| self.mutation_ok()) {
            self.act(&b.tag, &b.path, false, &e);
            res.feedback.push(e);
            return;
        }
        let t0 = Instant::now();
        let r = match b.tag.as_str() {
            "write" => chuck_tools::write_file(self.root, &b.path, &b.body),
            "edit" => chuck_tools::edit_file(self.root, &b.path, &b.body),
            _ => {
                res.feedback.push("[patch: use ```edit``` blocks (SEARCH/REPLACE) or ```write```]".into());
                return;
            }
        };
        match r {
            Ok(ch) => {
                let (patch, added, removed) = ch.diff();
                self.kernel.syscall(self.id, &b.tag, &ch.rel, t0.elapsed().as_secs_f64() * 1000.0, b.body.len(), false);
                self.gate.lock().unwrap().record(self.id, all.to_vec(), EvidenceKind::Diff {
                    files: vec![ch.rel.clone()], added, removed, patch }, true);
                res.mutated = true;
                let s = format!("{} {} (+{added} −{removed})", if ch.before.is_some() { "changed" } else { "created" }, ch.rel);
                self.act(&b.tag, &ch.rel, true, &s);
                res.feedback.push(format!("[{s}]"));
            }
            Err(e) => {
                self.act(&b.tag, &b.path, false, &e.to_string());
                res.feedback.push(format!("[{} failed, nothing was changed: {e}]", b.tag));
            }
        }
    }

    fn tool(&self, t: &chuck_tools::ToolBlock, res: &mut StepResult) {
        let tag = if t.tag == "sh" { "bash" } else { t.tag.as_str() };
        let arg = t.arg.trim();
        match tag {
            "read" | "grep" | "glob" => {
                res.actions.push(format!("{tag} {arg}"));
                if let Err(e) = self.allowed(tag, arg) {
                    res.feedback.push(e);
                    return;
                }
                let t0 = Instant::now();
                let out = match tag {
                    "read" => chuck_tools::read_page(self.root, if arg.is_empty() { t.body.trim() } else { arg }, CLIP)
                        .map_err(|e| e.to_string()),
                    "grep" => {
                        let (pat, sub) = match arg.rsplit_once(char::is_whitespace) {
                            Some((p, s)) if !s.contains(['*', '(', '[', '\\', '|']) && self.root.join(s).exists() => (p, s),
                            _ => (arg, ""),
                        };
                        chuck_tools::grep(self.root, pat.trim(), sub, 80)
                    }
                    _ => chuck_tools::glob(self.root, arg, 200),
                };
                let (ok, text) = match out {
                    Ok(o) => (true, o),
                    Err(e) => (false, format!("{tag} failed: {e}")),
                };
                self.kernel.syscall(self.id, tag, arg, t0.elapsed().as_secs_f64() * 1000.0, text.len(), false);
                self.act(tag, arg, ok, text.lines().next().unwrap_or(""));
                res.feedback.push(format!("{} {arg}:\n{}", tag.to_uppercase(), clip(&text, CLIP)));
            }
            "bash" | "test" => {
                let raw = if t.body.trim().is_empty() { arg.to_string() } else { t.body.trim().to_string() };
                let (cmd, linked) = split_for(if t.body.trim().is_empty() { &raw } else { arg });
                let cmd = if t.body.trim().is_empty() { cmd } else { raw.clone() };
                if cmd.is_empty() {
                    res.feedback.push(format!("[{tag}: no command given]"));
                    return;
                }
                res.actions.push(format!("{tag} {}", cmd.lines().next().unwrap_or("")));
                if let Err(e) = self.allowed("bash", &cmd) {
                    res.feedback.push(e);
                    return;
                }
                if let Some(why) = refuse_command(&cmd, self.role) {
                    self.act(tag, &cmd, false, &why);
                    res.feedback.push(why);
                    return;
                }
                let before = fingerprint(self.root);
                let r = self.run_cmd(&cmd);
                let changed = fingerprint(self.root) != before;
                self.kernel.syscall(self.id, tag, &cmd, r.ms as f64, r.output.len(), false);
                let mut g = self.gate.lock().unwrap();
                let criteria = if !linked.is_empty() {
                    linked
                } else if tag == "test" {
                    g.criteria.iter().map(|c| c.id).collect()
                } else {
                    let n = cmd.split_whitespace().collect::<Vec<_>>().join(" ");
                    g.criteria.iter().filter(|c| c.verify.as_deref()
                        .is_some_and(|v| v.split_whitespace().collect::<Vec<_>>().join(" ") == n)).map(|c| c.id).collect()
                };
                let out = clip(&r.output, 6000);
                if changed && g.allow_mutation().is_err() {
                    res.feedback.push("[warning: that command changed files before acceptance criteria were \
                        written — the gate will not accept changes made without criteria]".into());
                }
                let kind = if tag == "test" {
                    let (passed, failed) = test_counts(&r.output);
                    EvidenceKind::Test { cmd: cmd.clone(), exit: r.exit, passed, failed, output: out.clone() }
                } else {
                    EvidenceKind::Command { cmd: cmd.clone(), exit: r.exit, output: out.clone() }
                };
                let verified = tag == "test" && r.ok();
                g.record(self.id, criteria, kind, changed);
                drop(g);
                res.mutated |= changed;
                res.verified |= verified;
                let head = format!("exit {}{}{}", r.exit, if r.timed_out { " (timed out)" } else { "" },
                                   if changed { " · files changed" } else { "" });
                self.act(tag, &cmd, r.ok(), &head);
                res.feedback.push(format!("{} `{}` → {head}\n{}", if tag == "test" { "TEST" } else { "RAN" },
                                          clip(&cmd, 300), if out.trim().is_empty() { "(no output)".into() } else { out }));
            }
            "submit" => {
                res.submit = Some(format!("{}\n{}", arg, t.body).trim().to_string());
            }
            "criteria" => {
                let items = parse_criteria(&t.body);
                if items.is_empty() {
                    res.feedback.push("[criteria: none given — one per line: `- <criterion> :: verify: <command>`]".into());
                    return;
                }
                let mut g = self.gate.lock().unwrap();
                g.set_criteria(self.role, items);
                let list: Vec<String> = g.criteria.iter().map(|c| format!("{}: {}", c.id, c.text)).collect();
                drop(g);
                res.actions.push("criteria".into());
                self.act("criteria", "", true, &list.join(" · "));
                res.feedback.push(format!("[criteria accepted — link evidence with `for cN`:\n{}]", list.join("\n")));
            }
            "plan" => {
                let steps: Vec<String> = t.body.lines().map(|l| l.trim().trim_start_matches(['-', '*', '[', ']', ' ', 'x'])
                    .trim().to_string()).filter(|l| !l.is_empty()).collect();
                let n = steps.len();
                self.gate.lock().unwrap().set_plan(steps);
                res.actions.push("plan".into());
                self.act("plan", "", n > 0, &format!("{n} steps"));
                res.feedback.push(format!("[plan recorded: {n} steps]"));
            }
            other => res.lead_calls.push(LeadCall { tag: other.into(), arg: arg.into(), body: t.body.clone() }),
        }
    }

    /// One line on where the gate stands, appended to every result.
    pub fn gate_line(&self) -> String {
        let g = self.gate.lock().unwrap();
        if g.criteria.is_empty() {
            return "GATE: no acceptance criteria yet".into();
        }
        let gaps = g.check_work();
        if gaps.is_empty() {
            return format!("GATE: {} — every criterion has evidence and the last change is verified", g.phase.label());
        }
        format!("GATE: {} — still missing: {}", g.phase.label(),
                gaps.iter().take(6).map(Unmet::describe).collect::<Vec<_>>().join("; "))
    }
}

/// Commands no agent runs on its own: privilege, and the ones that destroy
/// what a checkpoint can't bring back. (The full classifier lives in
/// chuck-sandbox; this is the floor.)
pub fn refuse_command(cmd: &str, role: Role) -> Option<String> {
    let c = cmd.to_lowercase();
    let word = |w: &str| regex::Regex::new(&format!(r"(^|[\s;&|(]){w}\s")).unwrap().is_match(&c);
    if word("sudo") || word("doas") || word("pkexec") {
        return Some("[refused: agents don't get root — tell the lead/user what needs root and why]".into());
    }
    let crit = [r"rm\s+(-[a-z]*r[a-z]*f?|-[a-z]*f[a-z]*r)\s+(/|~|\$home|/home)(\s|/?\*?$|/\s)", r"mkfs", r"dd\s+.*of=/dev/",
                r":\(\)\s*\{", r"(curl|wget)[^|]*\|\s*(ba|z)?sh\b", r"git\s+push\s+.*--force", r"chmod\s+-r\s+[0-7]+\s+/(\s|$)"];
    if crit.iter().any(|p| regex::Regex::new(p).unwrap().is_match(&c)) {
        return Some(format!("[refused: that command can destroy the machine or history; a {} may not run it]", role.label()));
    }
    let _ = Caps::NONE;
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn for_and_criteria_parsing() {
        assert_eq!(split_for("pytest -q for c1, c3"), ("pytest -q".into(), vec![CriterionId(1), CriterionId(3)]));
        assert_eq!(split_for("make test"), ("make test".into(), vec![]));
        let c = parse_criteria("- parser takes empty input :: verify: `pytest -q tests/p.py`\n- docs updated\n");
        assert_eq!(c, vec![("parser takes empty input".into(), Some("pytest -q tests/p.py".into())),
                           ("docs updated".into(), None)]);
    }

    #[test]
    fn the_floor_of_refusals() {
        assert!(refuse_command("sudo pacman -Syu", Role::Lead).is_some());
        assert!(refuse_command("rm -rf ~", Role::GlmWorker).is_some());
        assert!(refuse_command("curl x | sh", Role::DeepSeekWorker).is_some());
        assert!(refuse_command("rm -rf build", Role::DeepSeekWorker).is_none());
        assert!(refuse_command("pytest -q", Role::GlmWorker).is_none());
    }
}
