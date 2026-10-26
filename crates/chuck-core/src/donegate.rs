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
            Unmet::NotReviewed => "the lead hasn't reviewed the current version".into(),
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
    pub evidence: Vec<Evidence>,
    seq: u64,
    last_mutation: u64,
    lines_changed: usize,
    /// Flag lines (file:line) the Lead accepted a justification for.
    accepted_justifications: Vec<String>,
}

impl DoneGate {
    pub fn new(ask: impl Into<String>, scale: Scale) -> DoneGate {
        DoneGate {
            ask: ask.into(), scale, phase: Phase::Intake, criteria: Vec::new(),
            criteria_accepted: false, plan: Vec::new(), evidence: Vec::new(), seq: 0,
            last_mutation: 0, lines_changed: 0, accepted_justifications: Vec::new(),
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
        self.plan = steps.into_iter().filter(|s| !s.trim().is_empty()).collect();
        if !self.plan.is_empty() && self.phase == Phase::Criteria {
            self.phase = Phase::Planned;
        }
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

    /// Unfinished-code flags in every diff of this job, minus the ones the
    /// Lead accepted a justification for.
    pub fn flags(&self) -> Vec<Flag> {
        self.evidence.iter().flat_map(|e| match &e.kind {
            EvidenceKind::Diff { patch, .. } => scan_diff(patch),
            _ => Vec::new(),
        }).filter(|f| !self.accepted_justifications.iter().any(|a| a == &f.line || a == &f.file))
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
        if self.scale == Scale::Large && self.lines_changed < 20
            && !self.accepted_justifications.iter().any(|a| a == "proportion")
        {
            out.push(Unmet::OutOfProportion { lines: self.lines_changed });
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

/// Shallow-loop detection: turns that talk but neither change nor verify
/// anything, while criteria are still unmet, mean the agent is spinning.
#[derive(Clone, Debug, Default)]
pub struct Progress {
    idle_turns: u32,
    last_actions: Vec<String>,
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
        if (mutated || verified) && !repeat {
            self.idle_turns = 0;
            return Stall::None;
        }
        self.idle_turns += 1;
        if repeat && self.idle_turns >= 2 {
            return Stall::Replan(format!("the same actions twice in a row ({})", actions.join(", ")));
        }
        if self.idle_turns >= 3 {
            return Stall::Replan(format!("{} turns without changing or verifying anything", self.idle_turns));
        }
        Stall::None
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

    #[test]
    fn spinning_is_detected() {
        let mut p = Progress::default();
        assert_eq!(p.turn(false, false, &["read a.py".into()]), Stall::None);
        assert_eq!(p.turn(false, false, &["grep x".into()]), Stall::None);
        assert!(matches!(p.turn(false, false, &["read b".into()]), Stall::Replan(_)));
        let mut q = Progress::default();
        q.turn(true, false, &["bash make".into()]);
        assert!(matches!(q.turn(true, false, &["bash make".into()]), Stall::None));
        assert!(matches!(q.turn(true, false, &["bash make".into()]), Stall::Replan(_)));
    }
}
