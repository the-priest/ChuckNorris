//! The models Chuck thinks with, SiliconFlow only: one model does the whole
//! job (DeepSeek-V4.1-Flash); GLM-5.3-Flash is its eyes (it describes
//! screenshots).

use serde::{Deserialize, Serialize};

/// The model that does the work (and Chat's, and the daemons').
pub const MAIN_MODEL: &str = "deepseek-ai/DeepSeek-V4.1-Flash-1017";
/// The eyes: what a screenshot shows.
pub const VISION_MODEL: &str = "zai-org/GLM-5.3-Flash";

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Models {
    pub main: String,
    pub vision: String,
}

impl Default for Models {
    fn default() -> Models {
        Models { main: MAIN_MODEL.into(), vision: VISION_MODEL.into() }
    }
}

impl Models {
    /// Point the intended ids at the account's live model list: the exact id
    /// when it's there, else the closest of the same family
    /// (`DeepSeek-V4.1-Flash*`, `GLM-5.3-Flash*`). Returns what changed.
    pub fn resolve(&mut self, live: &[String]) -> Vec<(String, String)> {
        let mut changes = Vec::new();
        for (slot, family) in [(&mut self.main, "deepseek-v4.1-flash"), (&mut self.vision, "glm-5.3-flash")] {
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolves_to_the_live_ids() {
        let mut f = Models::default();
        let live = vec!["deepseek-ai/DeepSeek-V4.1-Flash".to_string(), "zai-org/GLM-5.3-Flash".to_string(),
                        "deepseek-ai/DeepSeek-V4.1-Pro".to_string()];
        let ch = f.resolve(&live);
        assert_eq!(f.main, "deepseek-ai/DeepSeek-V4.1-Flash");
        assert_eq!(f.vision, VISION_MODEL);
        assert_eq!(ch.len(), 1);
        let mut g = Models::default();
        g.resolve(&["deepseek-ai/DeepSeek-V4.1-Flash-1017".into()]);
        assert_eq!(g.main, MAIN_MODEL);
        let mut m = Models::default();
        m.resolve(&["deepseek-ai/DeepSeek-V4.1-Flash-Max".into(), "deepseek-ai/DeepSeek-V4.1-Flash-0731".into()]);
        assert_eq!(m.main, "deepseek-ai/DeepSeek-V4.1-Flash-0731", "not the pricier Max");
    }
}
