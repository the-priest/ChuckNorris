//! The fleet: one Lead and up to eight workers, SiliconFlow only.
//!
//! The Lead (DeepSeek-V4.1-Flash) plans, reviews and merges; the workers
//! (DeepSeek-V4-Flash, the cheaper workhorse) build; GLM-5.3-Flash is the
//! team's eyes (it describes screenshots) and, if asked for, a second pool
//! of workers.

use crate::donegate::{DoneGate, Scale};
use crate::evidence::{Criterion, Evidence};
use crate::types::{AgentId, ModelFamily, Role};
use serde::{Deserialize, Serialize};

/// The Lead's model (and Chat's, the critic's).
pub const DEEPSEEK_MODEL: &str = "deepseek-ai/DeepSeek-V4.1-Flash-1017";
/// The workers' model: the cheap, fast workhorse.
pub const WORKER_MODEL: &str = "deepseek-ai/DeepSeek-V4-Flash";
/// The eyes (and the GLM pool, when there is one).
pub const GLM_MODEL: &str = "zai-org/GLM-5.3-Flash";

fn worker_model() -> String {
    WORKER_MODEL.into()
}

/// Who the team is: the Lead, up to 8 DeepSeek workers, GLM for the eyes.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct FleetConfig {
    /// The Lead's model.
    pub deepseek_model: String,
    /// The DeepSeek workers' model.
    #[serde(default = "worker_model")]
    pub worker_model: String,
    pub glm_model: String,
    pub deepseek_workers: u8,
    pub glm_workers: u8,
}

impl Default for FleetConfig {
    fn default() -> FleetConfig {
        FleetConfig {
            deepseek_model: DEEPSEEK_MODEL.into(),
            worker_model: WORKER_MODEL.into(),
            glm_model: GLM_MODEL.into(),
            deepseek_workers: 4,
            glm_workers: 0,
        }
    }
}

impl FleetConfig {
    pub fn model_for(&self, role: Role) -> &str {
        match role {
            Role::DeepSeekWorker | Role::Daemon => &self.worker_model,
            _ => match role.family() {
                ModelFamily::DeepSeek => &self.deepseek_model,
                ModelFamily::Glm => &self.glm_model,
            },
        }
    }

    pub fn max_workers(&self, role: Role) -> usize {
        match role {
            Role::DeepSeekWorker => self.deepseek_workers.min(8) as usize,
            Role::GlmWorker => self.glm_workers.min(4) as usize,
            _ => 0,
        }
    }

    /// The pool a task goes to: the one asked for when it has room to exist
    /// at all, else the other.
    pub fn pool(&self, asked: Role) -> Role {
        match asked {
            Role::GlmWorker if self.glm_workers == 0 => Role::DeepSeekWorker,
            Role::DeepSeekWorker if self.deepseek_workers == 0 && self.glm_workers > 0 => Role::GlmWorker,
            r => r,
        }
    }

    /// Point the intended ids at the account's live model list: the exact id
    /// when it's there, else the closest of the same family
    /// (`DeepSeek-V4.1-Flash*`, `GLM-5.3-Flash*`). Returns what changed.
    pub fn resolve(&mut self, live: &[String]) -> Vec<(String, String)> {
        let mut changes = Vec::new();
        for (slot, family) in [(&mut self.deepseek_model, "deepseek-v4.1-flash"),
                               (&mut self.worker_model, "deepseek-v4-flash"),
                               (&mut self.glm_model, "glm-5.3-flash")] {
            if live.iter().any(|m| m == slot.as_str()) {
                continue;
            }
            // a bigger sibling (`-Pro`, `-Max`) costs more: only when it's all there is
            let pricier = |m: &str| ["-pro", "-max"].iter().any(|x| m.to_lowercase().contains(x));
            let best = live.iter().filter(|m| m.to_lowercase().contains(family))
                .min_by_key(|m| (pricier(m), strsim(m, slot), m.len()));
            if let Some(b) = best {
                changes.push((slot.clone(), b.clone()));
                *slot = b.clone();
            }
        }
        changes
    }
}

fn strsim(a: &str, b: &str) -> usize {
    // characters not shared at the start: enough to prefer "...Flash-1017" over "...Flash-Pro"
    let common = a.chars().zip(b.chars()).take_while(|(x, y)| x.eq_ignore_ascii_case(y)).count();
    a.len().max(b.len()) - common
}

/// A unit of work the Lead hands to a worker.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Task {
    pub id: String,
    pub title: String,
    pub brief: String,
    pub criteria: Vec<Criterion>,
    pub scale: Scale,
    /// Which pool should take it (the Lead decides).
    pub prefer: Role,
    /// Give it to one worker of EACH pool and keep the better verified result.
    pub race: bool,
    /// Ids of tasks that must be accepted first.
    pub deps: Vec<String>,
}

impl Task {
    /// A gate for this task, with the Lead's criteria already accepted.
    pub fn gate(&self) -> DoneGate {
        let mut g = DoneGate::new(self.brief.clone(), self.scale);
        g.set_criteria(Role::Lead, self.criteria.iter().map(|c| (c.text.clone(), c.verify.clone())).collect());
        if self.scale == Scale::Large {
            g.set_plan(vec![self.title.clone()]); // the Lead's plan is the task graph
        }
        g
    }
}

/// What a worker hands back. Never a merge: the Lead decides.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Submission {
    pub task: String,
    pub worker: AgentId,
    pub role: Role,
    pub report: String,
    pub evidence: Vec<Evidence>,
    /// The unified diff of the worker's copy against its starting point.
    pub diff: String,
    pub lines_changed: usize,
    /// The gate's own verdict on the work (before the Lead's review).
    pub gaps: Vec<String>,
}

/// Pick the better of two verified submissions of a race: fewer gaps, then
/// more passing tests, then the smaller (more focused) diff.
pub fn better<'a>(a: &'a Submission, b: &'a Submission) -> &'a Submission {
    let passed = |s: &Submission| s.evidence.iter().filter(|e| matches!(e.kind,
        crate::evidence::EvidenceKind::Test { exit: 0, .. })).count();
    let key = |s: &'a Submission| (s.gaps.len(), std::cmp::Reverse(passed(s)), s.lines_changed);
    if key(b) < key(a) { b } else { a }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_fleet_is_a_v41_lead_with_v4_flash_workers() {
        let f = FleetConfig::default();
        assert_eq!(f.model_for(Role::Lead), DEEPSEEK_MODEL);
        assert_eq!(f.model_for(Role::DeepSeekWorker), WORKER_MODEL);
        assert_eq!(f.model_for(Role::Daemon), WORKER_MODEL);
        assert_eq!(f.model_for(Role::GlmWorker), GLM_MODEL);
        assert_eq!((f.max_workers(Role::DeepSeekWorker), f.max_workers(Role::GlmWorker)), (4, 0));
        // GLM is the eyes: a task asked of a GLM pool that isn't there goes to the workers
        assert_eq!(f.pool(Role::GlmWorker), Role::DeepSeekWorker);
        let eight = FleetConfig { deepseek_workers: 8, ..FleetConfig::default() };
        assert_eq!(eight.max_workers(Role::DeepSeekWorker), 8);
        // an older saved config (no worker model) gets the workhorse
        let old: FleetConfig = serde_json::from_str(r#"{"deepseek_model":"a","glm_model":"b","deepseek_workers":4,"glm_workers":4}"#).unwrap();
        assert_eq!(old.worker_model, WORKER_MODEL);
    }

    #[test]
    fn resolves_to_the_live_ids() {
        let mut f = FleetConfig::default();
        let live = vec!["deepseek-ai/DeepSeek-V4.1-Flash".to_string(), "zai-org/GLM-5.3-Flash".to_string(),
                        "deepseek-ai/DeepSeek-V4.1-Pro".to_string()];
        let ch = f.resolve(&live);
        assert_eq!(f.deepseek_model, "deepseek-ai/DeepSeek-V4.1-Flash");
        assert_eq!(f.glm_model, GLM_MODEL);
        assert_eq!(ch.len(), 1);
        let mut g = FleetConfig::default();
        g.resolve(&["deepseek-ai/DeepSeek-V4.1-Flash-1017".into()]);
        assert_eq!(g.deepseek_model, DEEPSEEK_MODEL);
        // the workhorse: the account's V4-Flash, never mistaken for V4.1
        let mut h = FleetConfig::default();
        h.resolve(&["deepseek-ai/DeepSeek-V4.1-Flash-1017".into(), "deepseek-ai/DeepSeek-V4-Flash-0738".into()]);
        assert_eq!((h.deepseek_model.as_str(), h.worker_model.as_str()), (DEEPSEEK_MODEL, "deepseek-ai/DeepSeek-V4-Flash-0738"));
        let mut m = FleetConfig::default();
        m.resolve(&["deepseek-ai/DeepSeek-V4-Flash-Max".into(), "deepseek-ai/DeepSeek-V4-Flash-0731".into()]);
        assert_eq!(m.worker_model, "deepseek-ai/DeepSeek-V4-Flash-0731", "not the pricier Max");
    }
}
