//! The fleet: one Lead and up to eight workers, SiliconFlow only.

use crate::donegate::{DoneGate, Scale};
use crate::evidence::{Criterion, Evidence};
use crate::types::{AgentId, ModelFamily, Role};
use serde::{Deserialize, Serialize};

pub const DEEPSEEK_MODEL: &str = "deepseek-ai/DeepSeek-V4.1-Flash-1017";
pub const GLM_MODEL: &str = "zai-org/GLM-5.3-Flash";

/// Who the team is. The default is the full 1 + 4 + 4.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct FleetConfig {
    pub deepseek_model: String,
    pub glm_model: String,
    pub deepseek_workers: u8,
    pub glm_workers: u8,
}

impl Default for FleetConfig {
    fn default() -> FleetConfig {
        FleetConfig {
            deepseek_model: DEEPSEEK_MODEL.into(),
            glm_model: GLM_MODEL.into(),
            deepseek_workers: 4,
            glm_workers: 4,
        }
    }
}

impl FleetConfig {
    pub fn model_for(&self, role: Role) -> &str {
        match role.family() {
            ModelFamily::DeepSeek => &self.deepseek_model,
            ModelFamily::Glm => &self.glm_model,
        }
    }

    pub fn max_workers(&self, role: Role) -> usize {
        match role {
            Role::DeepSeekWorker => self.deepseek_workers.min(4) as usize,
            Role::GlmWorker => self.glm_workers.min(4) as usize,
            _ => 0,
        }
    }

    /// Point the intended ids at the account's live model list: the exact id
    /// when it's there, else the closest of the same family
    /// (`DeepSeek-V4.1-Flash*`, `GLM-5.3-Flash*`). Returns what changed.
    pub fn resolve(&mut self, live: &[String]) -> Vec<(String, String)> {
        let mut changes = Vec::new();
        for (slot, family) in [(&mut self.deepseek_model, "deepseek-v4.1-flash"),
                               (&mut self.glm_model, "glm-5.3-flash")] {
            if live.iter().any(|m| m == slot.as_str()) {
                continue;
            }
            let best = live.iter().filter(|m| m.to_lowercase().contains(family))
                .min_by_key(|m| (strsim(m, slot), m.len()));
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
    fn default_fleet_is_one_plus_four_plus_four_on_siliconflow_ids() {
        let f = FleetConfig::default();
        assert_eq!(f.model_for(Role::Lead), DEEPSEEK_MODEL);
        assert_eq!(f.model_for(Role::DeepSeekWorker), DEEPSEEK_MODEL);
        assert_eq!(f.model_for(Role::GlmWorker), GLM_MODEL);
        assert_eq!(f.max_workers(Role::DeepSeekWorker) + f.max_workers(Role::GlmWorker), 8);
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
    }
}
