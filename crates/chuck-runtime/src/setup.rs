//! From the user's settings to a ready fleet: the SiliconFlow client, the
//! team, reasoning, budgets, the folder's access, the ledgers — the same for
//! `chuck run` and the window, so the two can never disagree.

use crate::RuntimeConfig;
use chuck_core::{FleetConfig, Quota};
use chuck_model::{fmt_usd, is_siliconflow, ChatModel, Reasoning, SiliconFlow, DEFAULT_BASE};
use chuck_sandbox::Access;
use chuck_store::{Ledger, Settings, Spend};
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

/// A server on this machine (for testing): the only exception to SiliconFlow.
pub fn is_local(base: &str) -> bool {
    let rest = base.split("://").nth(1).unwrap_or("");
    let host = rest.split('/').next().unwrap_or("");
    let host = host.rsplit_once(':').map_or(host, |(h, p)| if p.chars().all(|c| c.is_ascii_digit()) { h } else { host });
    matches!(host, "127.0.0.1" | "localhost" | "[::1]")
}

/// The one client every agent shares.
pub fn client(s: &Settings) -> Result<SiliconFlow, String> {
    let key = s.api_key().ok_or("no SiliconFlow API key yet — add it in Settings (or run `chuck key`)")?;
    let base = s.str("siliconflow_base_url").unwrap_or(DEFAULT_BASE).to_string();
    if !is_siliconflow(&base) && !is_local(&base) {
        return Err(format!("{base} isn't a SiliconFlow endpoint: Chuck's fleet talks to SiliconFlow only \
            (clear \"API base URL\" in Settings)"));
    }
    let stall = Duration::from_secs(s.num("stall_seconds", 90.0, 20.0, 600.0) as u64);
    SiliconFlow::new(&base, key, stall, is_local(&base)).map_err(|e| e.to_string())
}

/// The folder's access as the window stores it (`folder_access`: path → mode).
pub fn access_of(s: &Settings, folder: &Path) -> Access {
    let Some(map) = s.map.get("folder_access").and_then(|v| v.as_object()) else { return Access::Full };
    map.iter().find(|(k, _)| {
        let p = Path::new(k.as_str());
        p == folder || p.canonicalize().is_ok_and(|c| c == folder)
    }).and_then(|(_, v)| v.as_str()).map_or(Access::Full, Access::parse)
}

pub fn fleet(s: &Settings) -> FleetConfig {
    let mut f = FleetConfig::default();
    if let Some(m) = s.str("fleet_deepseek_model") {
        f.deepseek_model = m.to_string();
    }
    if let Some(m) = s.str("fleet_glm_model") {
        f.glm_model = m.to_string();
    }
    f.deepseek_workers = s.num("fleet_deepseek_workers", 4.0, 0.0, 4.0) as u8;
    f.glm_workers = s.num("fleet_glm_workers", 4.0, 0.0, 4.0) as u8;
    f
}

/// Everything a job needs, from the settings.
pub struct Setup {
    pub model: Arc<dyn ChatModel>,
    pub cfg: RuntimeConfig,
    /// This job's cap, already clipped to what the day/month limits leave.
    pub usd: f64,
    /// Things worth telling the user before the job starts.
    pub notes: Vec<String>,
}

/// The web as the settings have it: the user's own SearXNG first, their
/// proxy (else the environment's), their fetch timeout.
pub fn web(s: &Settings) -> chuck_web::Web {
    let proxy = s.str("proxy").map(str::trim).filter(|p| !p.is_empty())
        .map_or(chuck_web::Proxy::Env, |p| chuck_web::Proxy::Url(p.to_string()));
    chuck_web::Web::new(chuck_web::Config {
        searx_own: s.str("searx_url").map(|u| u.trim().trim_end_matches('/').to_string()).filter(|u| !u.is_empty()),
        proxy,
        timeout: std::time::Duration::from_secs_f64(s.num("fetch_timeout", 8.0, 3.0, 30.0)),
        ..chuck_web::Config::default()
    })
}

/// Language servers, with the user's choices (settings `lsp_servers`:
/// {"python": "pylsp"} or {"rust": ["rust-analyzer"]}) over the ones found.
pub fn lsp(s: &Settings) -> chuck_code::Manager {
    let m = chuck_code::Manager::new();
    if let Some(map) = s.map.get("lsp_servers").and_then(|v| v.as_object()) {
        for (lang, v) in map {
            let argv: Vec<String> = match v {
                serde_json::Value::String(cmd) => cmd.split_whitespace().map(String::from).collect(),
                serde_json::Value::Array(a) => a.iter().filter_map(|x| x.as_str().map(String::from)).collect(),
                _ => continue,
            };
            if !argv.is_empty() {
                m.set_server(lang, argv);
            }
        }
    }
    m
}

/// Build a job's setup for `folder`. `max_usd`: a cap for this job (else
/// the setting). Err: it can't start (no key, over a limit…).
pub fn from_settings(s: &Settings, folder: &Path, max_usd: Option<f64>) -> Result<Setup, String> {
    let mut notes = Vec::new();
    if let Some(p) = &s.problem {
        notes.push(p.clone());
    }
    let model = client(s)?;
    let mut cfg = RuntimeConfig { fleet: fleet(s), ..RuntimeConfig::default() };
    if let Ok(live) = model.list_models() {
        for (from, to) in cfg.fleet.resolve(&live) {
            notes.push(format!("{from} isn't on your account; using {to}"));
        }
    }
    cfg.reasoning = Reasoning {
        budget: s.num("reasoning_budget", 4096.0, 0.0, 32768.0) as u32,
        max_think_secs: s.num("max_think_seconds", 120.0, 0.0, 1800.0) as u32,
    };
    let mut usd = max_usd.unwrap_or_else(|| s.num("fleet_job_usd", 2.0, 0.01, 1000.0));
    // the window's evidence chain and day/month spend: one history, one budget
    let spend = Arc::new(Spend::at(&Spend::default_path()));
    cfg.day_limit = s.num("budget_day_usd", 0.0, 0.0, 100_000.0);
    cfg.month_limit = s.num("budget_month_usd", 0.0, 0.0, 1_000_000.0);
    if let Some(o) = spend.over(cfg.day_limit, cfg.month_limit) {
        return Err(format!("the {} spending limit ({}) is reached ({} spent) — raise it in Settings to go on",
                           if o.scope == "day" { "daily" } else { "monthly" }, fmt_usd(o.limit), fmt_usd(o.spent)));
    }
    if let Some(left) = spend.left(cfg.day_limit, cfg.month_limit) {
        usd = usd.min(left);
    }
    cfg.spend = Some(spend);
    cfg.audit = Some(Arc::new(Ledger::at(&Ledger::default_path())));
    if s.map.get("checkpoints").and_then(|v| v.as_bool()) != Some(false) {
        cfg.checkpoints = Some(chuck_store::checkpoint::default_store());
    }
    cfg.job_quota = Quota { usd: Some(usd), ..cfg.job_quota };
    cfg.web = Some(Arc::new(web(s)));
    cfg.sources = s.num("research_sources", 6.0, 1.0, 10.0) as usize;
    cfg.access = access_of(s, folder);
    if cfg.access != Access::Full && !cfg.bwrap {
        notes.push(format!("this folder is {}, which needs bubblewrap to run commands — install it (pacman -S \
            bubblewrap / apt install bubblewrap); until then commands are refused", cfg.access.label()));
    }
    Ok(Setup { model: Arc::new(model), cfg, usd, notes })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_only_means_this_machine() {
        assert!(is_local("http://127.0.0.1:8080/v1"));
        assert!(is_local("http://localhost/v1"));
        assert!(!is_local("https://api.openai.com/v1"));
        assert!(!is_local("https://localhost.evil.com/v1"));
    }

    #[test]
    fn no_key_no_job_and_never_another_provider() {
        let mut s = Settings::default();
        assert!(client(&s).err().unwrap().contains("API key"));
        s.set("siliconflow_api_key", "sk-x".into());
        s.set("siliconflow_base_url", "https://api.openai.com/v1".into());
        assert!(client(&s).err().unwrap().contains("SiliconFlow only"));
    }
}
