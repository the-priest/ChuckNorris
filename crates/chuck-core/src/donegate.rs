//! Genuine Done. The default behaviour of a model is to do the minimum and
//! say "done". This gate makes that fail: completion is refused — with the
//! exact list of what's missing — unless the work is backed by evidence,
//! verified after its last change, and accepted by the Lead.
//!
//! ```text
//!  Intake ─criteria─► Criteria ─plan─► Planned ─change─► Working ─verify─► Verifying
//!                                                          ▲                  │ green + every
//!                                                          └── reject ── Review ◄─┘ criterion backed
//!                                                                         │ accept + gates
//!                                      Paused(gaps) ◄─ budget gone ─      Done
//! ```

use crate::evidence::{clip, Criterion, CriterionId, Evidence, EvidenceKind, Verdict};
use crate::types::{AgentId, Role};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum Phase {
    /// The task arrived; nothing may change yet.
    Intake,
    /// Acceptance criteria are written and accepted by the Lead.
    Criteria,
    /// A plan exists (required for non-trivial work).
    Planned,
    /// Files are being changed.
    Working,
    /// The last change has been verified green.
    Verifying,
    /// Submitted for the Lead's review.
    Review,
    /// Through every gate.
    Done,
    /// Out of budget (or stopped): honest about what is missing.
    Paused { gaps: Vec<String> },
}

impl Phase {
    pub fn label(&self) -> &'static str {
        match self {
            Phase::Intake => "intake",
            Phase::Criteria => "criteria",
            Phase::Planned => "planned",
            Phase::Working => "working",
            Phase::Verifying => "verified",
            Phase::Review => "in review",
            Phase::Done => "done",
            Phase::Paused { .. } => "INCOMPLETE",
        }
    }
}

/// How big the ask is: decides whether a plan is required and what diff is
/// "in proportion".
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Scale {
    /// A one-liner, a question answered by running something.
    Tiny,
    Small,
    /// A feature, a refactor, a game, an app.
    Large,
}

/// Why a mutation or a completion was refused, item by item.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum Unmet {
    NoCriteria,
    CriteriaNotAccepted,
    NoPlan,
    NoEvidence(CriterionId, String),
    NotVerifiedSinceLastChange,
    VerifyCommandNotRun(CriterionId, String),
    VerificationFailing(String),
    Craftsmanship(String),
    OutOfProportion { lines: usize },
    NotReviewed,
    Rejected(Vec<String>),
    NotTheLead(Role),
}

impl Unmet {
    pub fn describe(&self) -> String {
        match self {
            Unmet::NoCriteria => "no acceptance criteria were written".into(),
            Unmet::CriteriaNotAccepted => "the acceptance criteria haven't been accepted by the lead".into(),
            Unmet::NoPlan => "non-trivial work needs a plan before changes".into(),
            Unmet::NoEvidence(c, t) => format!("{c} has no evidence: {}", clip(t, 90)),
            Unmet::NotVerifiedSinceLastChange => {
                "nothing was verified (tests/build/run) after the last change".into()
            }
            Unmet::VerifyCommandNotRun(c, cmd) => {
                format!("{c}'s verification `{}` hasn't passed since the last change", clip(cmd, 70))
            }
            Unmet::VerificationFailing(cmd) => format!("verification is failing: {}", clip(cmd, 80)),
            Unmet::Craftsmanship(why) => format!("unfinished code: {why}"),
            Unmet::OutOfProportion { lines } => {
                format!("a large ask with a {lines}-line change — finish it or justify it to the lead")
            }
            Unmet::NotReviewed => "the current version hasn't been reviewed and accepted".into(),
            Unmet::Rejected(d) => format!("rejected by the lead: {}", clip(&d.join("; "), 200)),
            Unmet::NotTheLead(r) => format!("a {} can't declare the job done; submit it to the lead", r.label()),
        }
    }
}

/// A finding in changed code that a careful engineer wouldn't ship.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Flag {
    pub file: String,
    pub line: String,
    pub why: &'static str,
}

/// Unfinished-code markers in ADDED lines of a unified diff.
pub fn scan_diff(patch: &str) -> Vec<Flag> {
    let rules: &[(&str, &'static str)] = &[
        (r"\b(TODO|FIXME|XXX|HACK)\b", "a TODO/FIXME left in"),
        (r"\b(unimplemented|todo)!\s*\(", "unimplemented!/todo!() in shipped code"),
        (r"raise\s+NotImplementedError", "raise NotImplementedError"),
        (r"(?i)not\s+implemented(\s+yet)?['\x22]", "a 'not implemented' placeholder"),
        (r"^\s*pass\s*#\s*(stub|placeholder|todo)", "a stub body"),
        (r"(?i)lorem ipsum", "lorem ipsum placeholder text"),
        (r"(?i)\b(your|insert)[ _-](code|logic|implementation)[ _-]here\b", "a 'code goes here' placeholder"),
        (r"(?i)//\s*\.\.\.\s*(rest|remaining)", "elided code ('... rest')"),
        (r"(?i)#\s*\.\.\.\s*(rest|remaining)", "elided code ('... rest')"),
    ];
    let compiled: Vec<(regex::Regex, &'static str)> = rules.iter()
        .filter_map(|(p, w)| regex::Regex::new(p).ok().map(|r| (r, *w))).collect();
    let mut out = Vec::new();
    let mut file = String::new();
    for ln in patch.lines() {
        if let Some(f) = ln.strip_prefix("+++ ") {
            file = f.trim_start_matches("b/").to_string();
            continue;
        }
        let Some(added) = ln.strip_prefix('+') else { continue };
        for (re, why) in &compiled {
            if re.is_match(added) {
                out.push(Flag { file: file.clone(), line: clip(added.trim(), 120), why });
                break;
            }
        }
    }
    out
}

/// The gate for one job (a task, or the whole user request).
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct DoneGate {
    pub ask: String,
    pub scale: Scale,
    pub phase: Phase,
    pub criteria: Vec<Criterion>,
    pub criteria_accepted: bool,
    pub plan: Vec<String>,
    /// Which plan steps are done (as long as `plan`).
    #[serde(default)]
    pub plan_done: Vec<bool>,
    pub evidence: Vec<Evidence>,
    seq: u64,
    last_mutation: u64,
    lines_changed: usize,
    /// Flag lines (file:line) the Lead accepted a justification for.
    accepted_justifications: Vec<String>,
    /// The real current diff (the copy, or the folder against its snapshot),
    /// when the runtime has measured it: what the craftsmanship and
    /// proportion checks judge. Without it they judge the recorded diffs,
    /// the latest per file.
    #[serde(default)]
    observed: Option<(String, usize)>,
}

/// A unified diff split per file: (file, section), in order.
fn sections(patch: &str) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    let lines: Vec<&str> = patch.lines().collect();
    let mut i = 0;
    while i < lines.len() {
        let ln = lines[i];
        let header = ln.starts_with("--- ") && lines.get(i + 1).is_some_and(|n| n.starts_with("+++ "));
        if header {
            let file = lines[i + 1][4..].trim().trim_start_matches("b/").to_string();
            let file = if file == "/dev/null" { ln[4..].trim().trim_start_matches("a/").to_string() } else { file };
            out.push((file, format!("{ln}\n{}\n", lines[i + 1])));
            i += 2;
            continue;
        }
        match out.last_mut() {
            Some((_, s)) => {
                s.push_str(ln);
                s.push('\n');
            }
            None => out.push((String::new(), format!("{ln}\n"))),
        }
        i += 1;
    }
    out
}


impl DoneGate {
    pub fn new(ask: impl Into<String>, scale: Scale) -> DoneGate {
        DoneGate {
            ask: ask.into(), scale, phase: Phase::Intake, criteria: Vec::new(),
            criteria_accepted: false, plan: Vec::new(), plan_done: Vec::new(), evidence: Vec::new(), seq: 0,
            last_mutation: 0, lines_changed: 0, accepted_justifications: Vec::new(), observed: None,
        }
    }

    fn next_seq(&mut self) -> u64 {
        self.seq += 1;
        self.seq
    }

    /// Write the acceptance criteria (each "text" or "text ⟶ verify: cmd").
    /// Workers receive theirs from the Lead, already accepted.
    pub fn set_criteria(&mut self, by: Role, items: Vec<(String, Option<String>)>) {
        self.criteria = items.into_iter().enumerate().filter(|(_, (t, _))| !t.trim().is_empty())
            .map(|(i, (text, verify))| Criterion {
                id: CriterionId(i as u16 + 1), text: text.trim().to_string(),
                verify: verify.filter(|v| !v.trim().is_empty()),
            }).collect();
        self.criteria_accepted = by == Role::Lead && !self.criteria.is_empty();
        if !self.criteria.is_empty() && self.phase == Phase::Intake {
            self.phase = Phase::Criteria;
        }
    }

    pub fn accept_criteria(&mut self, by: Role) -> bool {
        if by == Role::Lead && !self.criteria.is_empty() {
            self.criteria_accepted = true;
            true
        } else {
            false
        }
    }

    pub fn set_plan(&mut self, steps: Vec<String>) {
        self.set_plan_steps(steps.into_iter().map(|s| (s, false)).collect());
    }

    /// The plan, with what's done. A step sent again unmarked but done
    /// before stays done (re-sending the plan never loses progress).
    pub fn set_plan_steps(&mut self, steps: Vec<(String, bool)>) {
        let before: Vec<(String, bool)> = self.plan_view();
        let steps: Vec<(String, bool)> = steps.into_iter().filter(|(s, _)| !s.trim().is_empty()).map(|(s, d)| {
            let was = before.iter().any(|(b, bd)| *bd && b.trim() == s.trim());
            (s, d || was)
        }).collect();
        self.plan_done = steps.iter().map(|(_, d)| *d).collect();
        self.plan = steps.into_iter().map(|(s, _)| s).collect();
        if !self.plan.is_empty() && self.phase == Phase::Criteria {
            self.phase = Phase::Planned;
        }
    }

    /// Mark steps (1-based) done, or not done again. Err names a number
    /// that isn't a step.
    pub fn mark_plan(&mut self, steps: &[usize], done: bool) -> Result<(), String> {
        self.plan_done.resize(self.plan.len(), false);
        if let Some(bad) = steps.iter().find(|&&n| n == 0 || n > self.plan.len()) {
            return Err(format!("the plan has {} steps; there's no step {bad}", self.plan.len()));
        }
        for &n in steps {
            self.plan_done[n - 1] = done;
        }
        Ok(())
    }

    /// (step, done) for each step.
    pub fn plan_view(&self) -> Vec<(String, bool)> {
        self.plan.iter().enumerate().map(|(i, s)| (s.clone(), self.plan_done.get(i).copied().unwrap_or(false))).collect()
    }

    fn needs_plan(&self) -> bool {
        self.scale == Scale::Large || self.criteria.len() > 2
    }

    /// May this job change files now? Criteria come first; a large job also
    /// needs a plan. Checked by the tool guard before every mutating call.
    pub fn allow_mutation(&self) -> Result<(), Vec<Unmet>> {
        let mut why = Vec::new();
        if self.criteria.is_empty() {
            why.push(Unmet::NoCriteria);
        } else if !self.criteria_accepted {
            why.push(Unmet::CriteriaNotAccepted);
        }
        if self.needs_plan() && self.plan.is_empty() {
            why.push(Unmet::NoPlan);
        }
        if why.is_empty() { Ok(()) } else { Err(why) }
    }

    /// Record evidence. A Diff (or a command that changed files) is a
    /// mutation: everything verified before it no longer counts.
    pub fn record(&mut self, by: AgentId, criteria: Vec<CriterionId>, kind: EvidenceKind, mutates: bool) -> u64 {
        let seq = self.next_seq();
        if mutates {
            self.last_mutation = seq;
            self.observed = None; // measured before this change: no longer the truth
            if let EvidenceKind::Diff { added, removed, .. } = &kind {
                self.lines_changed += added + removed;
            }
            if !matches!(self.phase, Phase::Done | Phase::Paused { .. }) {
                self.phase = Phase::Working;
            }
        }
        let verified_now = matches!(&kind, EvidenceKind::Test { exit: 0, .. });
        self.evidence.push(Evidence { seq, by, criteria, kind });
        if verified_now && self.phase == Phase::Working && self.verified_since_change() {
            self.phase = Phase::Verifying;
        }
        seq
    }

    /// The Lead accepts a justification (for a flagged line, or a tiny diff).
    pub fn accept_justification(&mut self, by: Role, about: &str) -> bool {
        if by != Role::Lead {
            return false;
        }
        self.accepted_justifications.push(about.to_string());
        true
    }

    fn verified_since_change(&self) -> bool {
        self.evidence.iter().any(|e| e.seq > self.last_mutation
            && matches!(e.kind, EvidenceKind::Test { exit: 0, .. }))
    }

    fn last_verification_failed(&self) -> Option<String> {
        self.evidence.iter().rev().find_map(|e| match &e.kind {
            EvidenceKind::Test { cmd, exit, .. } if e.seq > self.last_mutation => Some((cmd, *exit)),
            _ => None,
        }).and_then(|(cmd, exit)| if exit != 0 { Some(cmd.clone()) } else { None })
    }

    fn latest_review(&self) -> Option<(&Verdict, u64)> {
        self.evidence.iter().rev().find_map(|e| match &e.kind {
            EvidenceKind::Review { verdict, .. } => Some((verdict, e.seq)),
            _ => None,
        })
    }

    /// The runtime measured the real diff of the work (see `observed`).
    pub fn observe(&mut self, patch: String, lines: usize) {
        self.observed = Some((patch, lines));
    }

    /// The latest recorded diff of each file. Each recorded diff is from the
    /// file's state before that change, so this is what the most recent
    /// change of each file looked like.
    fn latest_sections(&self) -> Vec<(String, String)> {
        let mut by_file: Vec<(String, String)> = Vec::new();
        for e in &self.evidence {
            if let EvidenceKind::Diff { patch, .. } = &e.kind {
                for (f, s) in sections(patch) {
                    match by_file.iter_mut().find(|(g, _)| *g == f) {
                        Some(slot) => slot.1 = s,
                        None => by_file.push((f, s)),
                    }
                }
            }
        }
        by_file
    }

    /// Lines changed by the work: measured when the runtime observed the
    /// real diff, else summed over every recorded change.
    pub fn lines_changed(&self) -> usize {
        match &self.observed {
            Some((_, n)) => *n,
            None => self.lines_changed,
        }
    }

    /// Unfinished-code flags in the work as it stands, minus the ones the
    /// Lead accepted a justification for. A TODO written and then removed
    /// doesn't count; one still in the result does.
    pub fn flags(&self) -> Vec<Flag> {
        let found = match &self.observed {
            Some((patch, _)) => scan_diff(patch),
            None => self.latest_sections().iter().flat_map(|(_, s)| scan_diff(s)).collect(),
        };
        found.into_iter().filter(|f| !self.accepted_justifications.iter().any(|a| a == &f.line || a == &f.file))
            .collect()
    }

    /// Everything that stands between this job and "done" — before review.
    pub fn check_work(&self) -> Vec<Unmet> {
        let mut out = Vec::new();
        if self.criteria.is_empty() {
            return vec![Unmet::NoCriteria];
        }
        if !self.criteria_accepted {
            out.push(Unmet::CriteriaNotAccepted);
        }
        if self.needs_plan() && self.plan.is_empty() {
            out.push(Unmet::NoPlan);
        }
        for c in &self.criteria {
            let backed = self.evidence.iter().any(|e| e.kind.is_proof() && e.criteria.contains(&c.id));
            if !backed {
                out.push(Unmet::NoEvidence(c.id, c.text.clone()));
            }
            if let Some(cmd) = &c.verify {
                let ran = self.evidence.iter().any(|e| e.seq > self.last_mutation && match &e.kind {
                    EvidenceKind::Test { cmd: c2, exit: 0, .. } | EvidenceKind::Command { cmd: c2, exit: 0, .. } => {
                        norm(c2) == norm(cmd)
                    }
                    _ => false,
                });
                if !ran {
                    out.push(Unmet::VerifyCommandNotRun(c.id, cmd.clone()));
                }
            }
        }
        if let Some(cmd) = self.last_verification_failed() {
            out.push(Unmet::VerificationFailing(cmd));
        } else if self.last_mutation > 0 && !self.verified_since_change() {
            out.push(Unmet::NotVerifiedSinceLastChange);
        }
        for f in self.flags() {
            out.push(Unmet::Craftsmanship(format!("{}: {} — `{}`", f.file, f.why, f.line)));
        }
        let lines = self.lines_changed();
        if self.scale == Scale::Large && lines < 20
            && !self.accepted_justifications.iter().any(|a| a == "proportion")
        {
            out.push(Unmet::OutOfProportion { lines });
        }
        out
    }

    /// A worker hands its work to the Lead. Refused (with the gaps) unless the
    /// work itself is complete; the Lead's review is what's left.
    pub fn submit(&mut self) -> Result<(), Vec<Unmet>> {
        let gaps = self.check_work();
        if !gaps.is_empty() {
            return Err(gaps);
        }
        self.phase = Phase::Review;
        Ok(())
    }

    /// The Lead's verdict on the current version.
    pub fn review(&mut self, by: AgentId, role: Role, verdict: Verdict, notes: &str) -> Result<(), Unmet> {
        if role != Role::Lead {
            return Err(Unmet::NotTheLead(role));
        }
        let rejected = matches!(verdict, Verdict::Rejected(_));
        self.record(by, Vec::new(), EvidenceKind::Review { verdict, notes: notes.to_string() }, false);
        if rejected {
            self.phase = Phase::Working;
        }
        Ok(())
    }

    /// Everything, review included.
    pub fn check(&self) -> Vec<Unmet> {
        let mut out = self.check_work();
        if out == [Unmet::NoCriteria] {
            return out; // nothing to review yet
        }
        match self.latest_review() {
            None => out.push(Unmet::NotReviewed),
            Some((_, seq)) if seq < self.last_mutation => out.push(Unmet::NotReviewed),
            Some((Verdict::Rejected(d), _)) => out.push(Unmet::Rejected(d.clone())),
            Some((Verdict::Accepted, _)) => {}
        }
        out
    }

    /// Declare the job done. Only the Lead, only through every gate.
    pub fn declare_done(&mut self, role: Role) -> Result<(), Vec<Unmet>> {
        if !role.may_declare_done() {
            return Err(vec![Unmet::NotTheLead(role)]);
        }
        let gaps = self.check();
        if !gaps.is_empty() {
            return Err(gaps);
        }
        self.phase = Phase::Done;
        Ok(())
    }

    /// Out of budget (or stopped): never "done" — an honest list of what's left.
    pub fn pause(&mut self) -> Vec<String> {
        let gaps: Vec<String> = self.check().iter().map(Unmet::describe).collect();
        self.phase = Phase::Paused { gaps: gaps.clone() };
        gaps
    }

    pub fn is_done(&self) -> bool {
        self.phase == Phase::Done
    }

    /// Per criterion: (criterion, evidence labels) for the UI.
    pub fn board(&self) -> Vec<(Criterion, Vec<String>)> {
        self.criteria.iter().map(|c| {
            let ev = self.evidence.iter().filter(|e| e.criteria.contains(&c.id))
                .map(|e| e.kind.label()).collect();
            (c.clone(), ev)
        }).collect()
    }
}

fn norm(cmd: &str) -> String {
    cmd.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Shallow-loop detection. A turn makes progress when it changes or verifies
/// something, or looks at something it hadn't looked at before (a new file,
/// search or command): exploring a big project is work. Turns that only
/// repeat what was already done, or do nothing, are idle; a few idle turns
/// in a row mean the agent is spinning. Progress resets the count, so a
/// job that runs for days is judged on its last few turns, not its whole life.
#[derive(Clone, Debug, Default)]
pub struct Progress {
    idle_turns: u32,
    last_actions: Vec<String>,
    seen: std::collections::HashSet<String>,
    /// Replans asked for since the last turn that made progress.
    stalls: u32,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Stall {
    /// Keep going.
    None,
    /// Several turns without a change or a verification: replan, with
    /// tighter criteria.
    Replan(String),
}

impl Progress {
    pub fn turn(&mut self, mutated: bool, verified: bool, actions: &[String]) -> Stall {
        let repeat = !actions.is_empty() && actions == self.last_actions.as_slice();
        self.last_actions = actions.to_vec();
        let mut fresh = false;
        for a in actions {
            fresh |= self.seen.insert(norm(a));
        }
        if ((mutated || verified) && !repeat) || (fresh && !repeat) {
            self.idle_turns = 0;
            self.stalls = 0;
            return Stall::None;
        }
        self.idle_turns += 1;
        let why = if repeat && self.idle_turns >= 2 {
            format!("the same actions twice in a row ({})", actions.join(", "))
        } else if self.idle_turns >= 3 {
            format!("{} turns without changing, verifying or looking at anything new", self.idle_turns)
        } else {
            return Stall::None;
        };
        self.idle_turns = 0;
        self.stalls += 1;
        Stall::Replan(why)
    }

    /// Replans in a row with no progress between them: past a few, the
    /// agent is stuck and more turns only spend money.
    pub fn stalls(&self) -> u32 {
        self.stalls
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const LEAD: AgentId = AgentId(1);
    const W: AgentId = AgentId(2);

    fn diff(lines: usize, body: &str) -> EvidenceKind {
        let patch = format!("--- a/x.py\n+++ b/x.py\n{}", (0..lines).map(|i| format!("+{body}{i}\n")).collect::<String>());
        EvidenceKind::Diff { files: vec!["x.py".into()], added: lines, removed: 0, patch }
    }

    fn test(exit: i32) -> EvidenceKind {
        EvidenceKind::Test { cmd: "pytest -q".into(), exit, passed: Some(3), failed: Some(0), output: String::new() }
    }

    #[test]
    fn nothing_changes_before_criteria() {
        let g = DoneGate::new("add a parser", Scale::Small);
        assert_eq!(g.allow_mutation(), Err(vec![Unmet::NoCriteria]));
    }

    #[test]
    fn large_work_needs_a_plan_first() {
        let mut g = DoneGate::new("build tetris", Scale::Large);
        g.set_criteria(Role::Lead, vec![("it plays".into(), None)]);
        assert_eq!(g.allow_mutation(), Err(vec![Unmet::NoPlan]));
        g.set_plan(vec!["board".into(), "pieces".into()]);
        assert!(g.allow_mutation().is_ok());
    }

    #[test]
    fn a_worker_cannot_declare_done_and_cannot_submit_unverified_work() {
        let mut g = DoneGate::new("fix it", Scale::Small);
        g.set_criteria(Role::Lead, vec![("parser handles empty input".into(), None)]);
        g.record(W, vec![CriterionId(1)], diff(5, "x = "), true);
        assert!(matches!(g.declare_done(Role::DeepSeekWorker), Err(e) if e == vec![Unmet::NotTheLead(Role::DeepSeekWorker)]));
        let gaps = g.submit().unwrap_err();
        assert!(gaps.contains(&Unmet::NotVerifiedSinceLastChange));
    }

    #[test]
    fn a_change_after_a_green_run_needs_verifying_again() {
        let mut g = DoneGate::new("fix it", Scale::Small);
        g.set_criteria(Role::Lead, vec![("works".into(), None)]);
        g.record(W, vec![CriterionId(1)], diff(5, "y = "), true);
        g.record(W, vec![CriterionId(1)], test(0), false);
        assert!(g.check_work().is_empty());
        g.record(W, vec![], diff(1, "z = "), true);
        assert_eq!(g.check_work(), vec![Unmet::NotVerifiedSinceLastChange]);
    }

    #[test]
    fn failing_verification_and_todos_block() {
        let mut g = DoneGate::new("fix it", Scale::Small);
        g.set_criteria(Role::Lead, vec![("works".into(), None)]);
        g.record(W, vec![CriterionId(1)], diff(2, "# TODO handle "), true);
        g.record(W, vec![CriterionId(1)], test(1), false);
        let gaps = g.check_work();
        assert!(gaps.iter().any(|u| matches!(u, Unmet::VerificationFailing(_))));
        assert!(gaps.iter().any(|u| matches!(u, Unmet::Craftsmanship(_))));
    }

    #[test]
    fn a_verify_command_must_pass_after_the_last_change() {
        let mut g = DoneGate::new("fix it", Scale::Small);
        g.set_criteria(Role::Lead, vec![("cli prints help".into(), Some("./app --help".into()))]);
        g.record(W, vec![CriterionId(1)], diff(4, "a = "), true);
        g.record(W, vec![CriterionId(1)], test(0), false);
        assert!(matches!(g.check_work()[0], Unmet::VerifyCommandNotRun(..)));
        g.record(W, vec![CriterionId(1)], EvidenceKind::Command { cmd: "./app  --help".into(), exit: 0, output: "usage".into() }, false);
        assert!(g.check_work().is_empty());
    }

    #[test]
    fn a_tiny_diff_for_a_large_ask_is_out_of_proportion_unless_justified() {
        let mut g = DoneGate::new("build an editor", Scale::Large);
        g.set_criteria(Role::Lead, vec![("opens files".into(), None)]);
        g.set_plan(vec!["one".into()]);
        g.record(W, vec![CriterionId(1)], diff(3, "q = "), true);
        g.record(W, vec![CriterionId(1)], test(0), false);
        assert_eq!(g.check_work(), vec![Unmet::OutOfProportion { lines: 3 }]);
        assert!(!g.accept_justification(Role::GlmWorker, "proportion"));
        assert!(g.accept_justification(Role::Lead, "proportion"));
        assert!(g.check_work().is_empty());
    }

    #[test]
    fn done_only_through_the_leads_review_of_the_current_version() {
        let mut g = DoneGate::new("fix it", Scale::Small);
        g.set_criteria(Role::Lead, vec![("works".into(), None)]);
        g.record(W, vec![CriterionId(1)], diff(5, "b = "), true);
        g.record(W, vec![CriterionId(1)], test(0), false);
        g.submit().unwrap();
        assert_eq!(g.check(), vec![Unmet::NotReviewed]);
        assert!(g.review(W, Role::GlmWorker, Verdict::Accepted, "").is_err());
        g.review(LEAD, Role::Lead, Verdict::Rejected(vec!["no error handling".into()]), "").unwrap();
        assert!(matches!(g.declare_done(Role::Lead), Err(e) if matches!(e[0], Unmet::Rejected(_))));
        g.record(W, vec![CriterionId(1)], diff(3, "c = "), true);
        g.record(W, vec![CriterionId(1)], test(0), false);
        g.review(LEAD, Role::Lead, Verdict::Accepted, "good").unwrap();
        assert!(g.declare_done(Role::Lead).is_ok());
        assert!(g.is_done());
    }

    #[test]
    fn out_of_budget_is_paused_with_gaps_never_done() {
        let mut g = DoneGate::new("fix it", Scale::Small);
        g.set_criteria(Role::Lead, vec![("works".into(), None)]);
        let gaps = g.pause();
        assert!(!g.is_done());
        assert!(gaps.iter().any(|s| s.contains("no evidence")));
        assert_eq!(g.phase.label(), "INCOMPLETE");
    }

    fn edit(file: &str, minus: &str, plus: &str) -> EvidenceKind {
        let patch = format!("--- a/{file}\n+++ b/{file}\n@@ -1 +1 @@\n-{minus}\n+{plus}\n");
        EvidenceKind::Diff { files: vec![file.into()], added: 1, removed: 1, patch }
    }

    #[test]
    fn a_todo_that_was_removed_no_longer_counts() {
        let mut g = DoneGate::new("x", Scale::Small);
        g.set_criteria(Role::Lead, vec![("works".into(), None)]);
        g.record(W, vec![CriterionId(1)], edit("a.py", "pass", "# TODO: handle errors"), true);
        g.record(W, vec![CriterionId(1)], edit("a.py", "# TODO: handle errors", "raise ValueError(x)"), true);
        g.record(W, vec![CriterionId(1)], test(0), false);
        assert!(g.check_work().is_empty(), "{:?}", g.check_work());
        // but one left in another file still blocks
        g.record(W, vec![CriterionId(1)], edit("b.py", "x = 1", "x = 1  # FIXME"), true);
        g.record(W, vec![CriterionId(1)], test(0), false);
        assert!(matches!(g.check_work().as_slice(), [Unmet::Craftsmanship(_)]));
    }

    #[test]
    fn the_measured_diff_decides_and_a_later_change_discards_it() {
        let mut g = DoneGate::new("x", Scale::Large);
        g.set_criteria(Role::Lead, vec![("works".into(), None)]);
        g.set_plan(vec!["do it".into()]);
        g.record(W, vec![CriterionId(1)], diff(3, "y = "), true);
        g.record(W, vec![CriterionId(1)], test(0), false);
        // the edits were tiny, but a command also changed 40 lines: measured, it's in proportion
        g.observe(format!("--- a/x.py\n+++ b/x.py\n{}", "+line\n".repeat(40)), 40);
        assert!(g.check_work().is_empty(), "{:?}", g.check_work());
        // the measured diff shows a stub the recorded edits never did
        g.observe("--- a/x.py\n+++ b/x.py\n+    raise NotImplementedError\n".into(), 40);
        assert!(g.check_work().iter().any(|u| matches!(u, Unmet::Craftsmanship(_))));
        // any new change makes the measurement stale: back to the recorded diffs
        g.record(W, vec![CriterionId(1)], diff(30, "z = "), true);
        g.record(W, vec![CriterionId(1)], test(0), false);
        assert!(g.check_work().is_empty(), "{:?}", g.check_work());
    }

    #[test]
    fn nothing_to_review_before_criteria() {
        let g = DoneGate::new("x", Scale::Small);
        assert_eq!(g.check(), vec![Unmet::NoCriteria]);
    }

    #[test]
    fn spinning_is_detected() {
        let mut p = Progress::default();
        // exploring something new is progress, however long it takes
        for i in 0..50 {
            assert_eq!(p.turn(false, false, &[format!("read f{i}.py")]), Stall::None);
        }
        // looking at the same things again is not
        assert_eq!(p.turn(false, false, &["read f1.py".into()]), Stall::None);
        assert_eq!(p.turn(false, false, &["grep x".into(), "read f2.py".into()]), Stall::None, "grep x is new");
        assert_eq!(p.turn(false, false, &["read f3.py".into()]), Stall::None);
        assert_eq!(p.turn(false, false, &[]), Stall::None);
        assert!(matches!(p.turn(false, false, &["read f4.py".into()]), Stall::Replan(_)));
        assert_eq!(p.stalls(), 1);
        let mut q = Progress::default();
        q.turn(true, false, &["bash make".into()]);
        assert!(matches!(q.turn(true, false, &["bash make".into()]), Stall::None));
        assert!(matches!(q.turn(true, false, &["bash make".into()]), Stall::Replan(_)));
    }

    #[test]
    fn progress_forgives_earlier_stalls() {
        let mut p = Progress::default();
        for _ in 0..3 {
            p.turn(false, false, &[]);
        }
        assert_eq!(p.stalls(), 1);
        for _ in 0..3 {
            p.turn(false, false, &[]);
        }
        assert_eq!(p.stalls(), 2);
        p.turn(true, false, &["edit a.py".into()]);
        assert_eq!(p.stalls(), 0, "real work since: the count starts again");
    }
}
