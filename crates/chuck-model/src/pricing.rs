//! What a call costs, from the usage the API actually reports.
//!
//! List prices in USD per 1M tokens on SiliconFlow's international site
//! (October 2026). Any of them can be overridden in Settings; an unknown model
//! shows tokens without inventing a dollar figure.

use serde::{Deserialize, Serialize};

/// (input, cached input, output) per 1M tokens. Cached `None`: billed as input.
pub type Price = (f64, Option<f64>, f64);

const PRICES: &[(&str, Price)] = &[
    ("deepseek-ai/deepseek-v4.1-flash", (0.158, Some(0.01), 0.633)),
    ("deepseek-ai/deepseek-v4-flash", (0.14, Some(0.028), 0.28)),
    ("deepseek-ai/deepseek-v4-flash-0731", (0.22, Some(0.014), 0.66)),
    ("zai-org/glm-5.3-flash", (0.15, None, 0.50)),
    ("zai-org/glm-5.3", (1.40, Some(0.26), 4.40)),
    ("zai-org/glm-5.1", (1.40, None, 4.40)),
    ("deepseek-ai/deepseek-v3.2", (0.27, Some(0.135), 0.42)),
    ("deepseek-ai/deepseek-v3", (0.25, None, 1.00)),
    ("qwen/qwen3-8b", (0.0, None, 0.0)),
    ("thudm/glm-4-9b-0414", (0.0, None, 0.0)),
    ("deepseek-ai/deepseek-r1-0528-qwen3-8b", (0.0, None, 0.0)),
];

/// The price of a model: exact id first, then the longest listed id it
/// extends (`deepseek-v4.1-flash-1017` is priced as `deepseek-v4.1-flash`).
pub fn price_for(model: &str) -> Option<Price> {
    let m = model.trim().to_lowercase();
    if let Some((_, p)) = PRICES.iter().find(|(k, _)| *k == m) {
        return Some(*p);
    }
    PRICES.iter().filter(|(k, _)| m.starts_with(&format!("{k}-")))
        .max_by_key(|(k, _)| k.len()).map(|(_, p)| *p)
}

/// Models that read images natively.
pub fn sees_images(model: &str) -> bool {
    let m = model.trim().to_lowercase();
    ["zai-org/glm-5.3-flash", "zai-org/glm-5.3", "qwen/qwen3.6-35b-a3b", "qwen/qwen3.6-27b"]
        .iter().any(|k| m == *k || m.starts_with(&format!("{k}-")))
}

/// Token counts as the API billed them.
#[derive(Clone, Copy, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Usage {
    pub prompt: u64,
    pub completion: u64,
    pub cached: u64,
    pub reasoning: u64,
}

impl Usage {
    /// Read a response's usage block. SiliconFlow/DeepSeek put cache hits at
    /// the top level, OpenAI-style servers under prompt_tokens_details.
    pub fn from_chunk(d: &serde_json::Value) -> Option<Usage> {
        let u = d.get("usage")?.as_object()?;
        let n = |v: Option<&serde_json::Value>| v.and_then(|x| x.as_u64()).unwrap_or(0);
        let prompt = n(u.get("prompt_tokens"));
        let completion = n(u.get("completion_tokens"));
        if prompt == 0 && completion == 0 {
            return None;
        }
        let cached = u.get("prompt_cache_hit_tokens").and_then(|x| x.as_u64())
            .or_else(|| u.get("prompt_tokens_details").and_then(|d| d.get("cached_tokens")).and_then(|x| x.as_u64()))
            .unwrap_or(0).min(prompt);
        let reasoning = n(u.get("completion_tokens_details").and_then(|d| d.get("reasoning_tokens")));
        Some(Usage { prompt, completion, cached, reasoning })
    }

    /// Dollars, or None when the price is unknown. Reasoning tokens are
    /// already inside completion_tokens: never billed twice.
    pub fn cost(&self, price: Option<Price>) -> Option<f64> {
        let (p_in, p_cache, p_out) = price?;
        let fresh = self.prompt.saturating_sub(self.cached) as f64;
        Some((fresh * p_in + self.cached as f64 * p_cache.unwrap_or(p_in) + self.completion as f64 * p_out)
            / 1_000_000.0)
    }

    /// chars/4 when a response carried no usage block (labelled an estimate).
    pub fn estimate(sent_chars: usize, got_chars: usize) -> Usage {
        Usage { prompt: (sent_chars / 4) as u64, completion: (got_chars / 4) as u64, cached: 0, reasoning: 0 }
    }
}

pub fn fmt_usd(x: f64) -> String {
    // -0.0 (an empty sum) is zero dollars, not "$-0.0000"
    let x = if x == 0.0 { 0.0 } else { x };
    if x < 0.01 {
        format!("${x:.4}")
    } else if x < 0.1 {
        format!("${x:.3}")
    } else {
        format!("${x:.2}")
    }
}

pub fn fmt_tokens(n: u64) -> String {
    if n >= 1_000_000 {
        format!("{:.1}M", n as f64 / 1e6)
    } else if n >= 1000 {
        format!("{:.1}k", n as f64 / 1e3)
    } else {
        n.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fleet_models_are_priced() {
        assert_eq!(price_for("deepseek-ai/DeepSeek-V4.1-Flash-1017"), Some((0.158, Some(0.01), 0.633)));
        assert_eq!(price_for("zai-org/GLM-5.3-Flash"), Some((0.15, None, 0.50)));
        assert!(price_for("someone/unknown").is_none());
        assert!(sees_images("zai-org/GLM-5.3-Flash"));
        assert!(!sees_images("deepseek-ai/DeepSeek-V4.1-Flash-1017"));
    }

    #[test]
    fn usage_and_cost() {
        let d: serde_json::Value = serde_json::json!({"usage": {"prompt_tokens": 1000, "completion_tokens": 200,
            "prompt_cache_hit_tokens": 800, "completion_tokens_details": {"reasoning_tokens": 50}}});
        let u = Usage::from_chunk(&d).unwrap();
        assert_eq!((u.cached, u.reasoning), (800, 50));
        let c = u.cost(price_for("deepseek-ai/deepseek-v4.1-flash")).unwrap();
        assert!((c - (200.0 * 0.158 + 800.0 * 0.01 + 200.0 * 0.633) / 1e6).abs() < 1e-12);
        assert_eq!(fmt_usd(0.0042), "$0.0042");
        assert_eq!(fmt_usd(-0.0), "$0.0000");
        assert_eq!(fmt_usd([0.0f64; 0].iter().sum::<f64>()), "$0.0000");
        assert_eq!(fmt_tokens(12_500), "12.5k");
    }
}
