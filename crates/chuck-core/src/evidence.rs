//! Evidence: what an agent can show for a claim. Chat text is not evidence;
//! a command with its exit code and output, a diff, a test run, a look at a
//! screenshot, or the Lead's verdict are.

use crate::types::AgentId;
use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub struct CriterionId(pub u16);

impl std::fmt::Display for CriterionId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "c{}", self.0)
    }
}

/// One acceptance criterion: what must be true, and (optionally) the command
/// that proves it.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Criterion {
    pub id: CriterionId,
    pub text: String,
    pub verify: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Verdict {
    Accepted,
    /// Rejected with concrete defects: these become the worker's next turn.
    Rejected(Vec<String>),
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum EvidenceKind {
    /// Any command that ran: what, exit code, output (clipped).
    Command { cmd: String, exit: i32, output: String },
    /// A verification run: the project's tests, build or linters.
    Test { cmd: String, exit: i32, passed: Option<u32>, failed: Option<u32>, output: String },
    /// A change to files.
    Diff { files: Vec<String>, added: usize, removed: usize, patch: String },
    /// A look at a rendered result (a screenshot), with what was seen.
    Look { path: String, note: String },
    /// The Lead's review of a submission.
    Review { verdict: Verdict, notes: String },
    /// A written justification (for a tiny diff, a flagged line, a skipped
    /// criterion). Only counts once the Lead accepts it.
    Justification { about: String, text: String },
}

impl EvidenceKind {
    /// Kinds that can back a criterion by themselves.
    pub fn is_proof(&self) -> bool {
        matches!(self, EvidenceKind::Command { .. } | EvidenceKind::Test { .. }
                 | EvidenceKind::Diff { .. } | EvidenceKind::Look { .. })
    }

    pub fn label(&self) -> String {
        match self {
            EvidenceKind::Command { cmd, exit, .. } => format!("$ {} → exit {exit}", clip(cmd, 80)),
            EvidenceKind::Test { cmd, exit, passed, failed, .. } => {
                let counts = match (passed, failed) {
                    (Some(p), Some(f)) => format!(" ({p} passed, {f} failed)"),
                    _ => String::new(),
                };
                format!("test {} → {}{counts}", clip(cmd, 70), if *exit == 0 { "pass" } else { "FAIL" })
            }
            EvidenceKind::Diff { files, added, removed, .. } => {
                format!("diff {} file(s) +{added} −{removed}", files.len())
            }
            EvidenceKind::Look { path, .. } => format!("looked at {path}"),
            EvidenceKind::Review { verdict: Verdict::Accepted, .. } => "lead: accepted".into(),
            EvidenceKind::Review { verdict: Verdict::Rejected(d), .. } => {
                format!("lead: rejected ({} defect(s))", d.len())
            }
            EvidenceKind::Justification { about, .. } => format!("justified: {}", clip(about, 60)),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Evidence {
    /// Order in the job's log. Mutations and verifications are compared by it.
    pub seq: u64,
    pub by: AgentId,
    /// Which criteria this backs (empty: general).
    pub criteria: Vec<CriterionId>,
    pub kind: EvidenceKind,
    /// The agent said which criteria this run checks (`for c1,c2`), rather
    /// than a test run counting for every criterion by default.
    #[serde(default)]
    pub explicit: bool,
}

pub fn clip(s: &str, n: usize) -> String {
    if s.chars().count() <= n {
        s.to_string()
    } else {
        let mut t: String = s.chars().take(n.saturating_sub(1)).collect();
        t.push('…');
        t
    }
}

/// Command output at most `n` characters, keeping its end: what failed (a
/// compiler error, a failing test, the summary) is at the end of a long log.
pub fn clip_ends(s: &str, n: usize) -> String {
    let total = s.chars().count();
    if total <= n {
        return s.to_string();
    }
    let head = n / 4;
    let tail = n - head;
    let a: String = s.chars().take(head).collect();
    let b: String = s.chars().skip(total - tail).collect();
    format!("{a}\n… ({} characters left out) …\n{b}", total - head - tail)
}

/// Pull "N passed, M failed" out of common test runners' output.
pub fn test_counts(output: &str) -> (Option<u32>, Option<u32>) {
    let num = |pat: &str| -> Option<u32> {
        let re = regex::Regex::new(pat).ok()?;
        re.captures_iter(output).last()?.get(1)?.as_str().parse().ok()
    };
    let passed = num(r"(\d+) passed").or_else(|| num(r"test result: ok\. (\d+) passed"))
        .or_else(|| num(r"Tests:\s+(?:\d+ failed, )?(\d+) passed"));
    let failed = num(r"(\d+) failed").or_else(|| num(r"FAILED \(failures=(\d+)"));
    (passed, failed.or(passed.map(|_| 0)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_long_log_keeps_its_end() {
        let log = format!("{}error[E0308]: mismatched types at src/main.rs:12", "Compiling crate…\n".repeat(2000));
        let c = clip_ends(&log, 6000);
        assert!(c.chars().count() < 6100 && c.ends_with("src/main.rs:12") && c.contains("characters left out"));
        assert_eq!(clip_ends("short", 6000), "short");
    }

    #[test]
    fn counts_from_runners() {
        assert_eq!(test_counts("===== 12 passed in 0.3s ====="), (Some(12), Some(0)));
        assert_eq!(test_counts("== 3 failed, 9 passed =="), (Some(9), Some(3)));
        assert_eq!(test_counts("test result: ok. 41 passed; 0 failed;"), (Some(41), Some(0)));
        assert_eq!(test_counts("nothing here"), (None, None));
    }
}
