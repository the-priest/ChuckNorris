//! From the user's settings to a ready job: the SiliconFlow client, the
//! team, reasoning, budgets, the folder's access, the ledgers — the same for
//! `chuck run` and the window, so the two can never disagree.

use crate::RuntimeConfig;
use chuck_core::{Models, Quota};
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
        return Err(format!("{base} isn't a SiliconFlow endpoint: Chuck talks to SiliconFlow only \
            (clear \"API base URL\" in Settings)"));
    }
    let stall = Duration::from_secs(s.num("stall_seconds", 90.0, 20.0, 600.0) as u64);
    SiliconFlow::new(&base, key, stall, is_local(&base)).map_err(|e| e.to_string())
}

/// The client the session's jobs share, and the account's model list: one
/// connection (kept alive between jobs, so no new handshake each time) and one
/// `/models` call an hour, not one of each per job.
struct Warm {
    base: String,
    key: String,
    stall: Duration,
    client: Arc<SiliconFlow>,
    live: Option<(std::time::Instant, Vec<String>)>,
}

static WARM: std::sync::Mutex<Option<Warm>> = std::sync::Mutex::new(None);

/// How long the account's model list is trusted.
const LIVE_FOR: Duration = Duration::from_secs(3600);

/// The shared client for these settings (made again when the key, the base
/// URL or the stall limit changed).
pub fn shared_client(s: &Settings) -> Result<Arc<SiliconFlow>, String> {
    let fresh = client(s)?;
    let mut w = WARM.lock().unwrap_or_else(|e| e.into_inner());
    if let Some(warm) = w.as_ref() {
        if warm.base == fresh.base() && warm.key == s.api_key().unwrap_or("").trim() && warm.stall == fresh.stall() {
            return Ok(warm.client.clone());
        }
    }
    let client = Arc::new(fresh);
    *w = Some(Warm { base: client.base().to_string(), key: s.api_key().unwrap_or("").trim().to_string(),
                     stall: client.stall(), client: client.clone(), live: None });
    Ok(client)
}

/// The account's models (asked at most once an hour; None: the API didn't say).
fn live_models(client: &Arc<SiliconFlow>) -> Option<Vec<String>> {
    {
        let w = WARM.lock().unwrap_or_else(|e| e.into_inner());
        if let Some((at, live)) = w.as_ref().filter(|w| Arc::ptr_eq(&w.client, client)).and_then(|w| w.live.as_ref()) {
            if at.elapsed() < LIVE_FOR {
                return Some(live.clone());
            }
        }
    }
    // the list is kept on disk too (an hour), so `chuck run` — one job per process — skips the call as well
    let file = crate::default_store().with_file_name("models.json");
    // (for this endpoint and this key: another account may have other models; the key itself isn't written)
    let tag = format!("{} {}\n", client.base(), &chuck_store::checkpoint::sha1_hex(client.key_id().as_bytes())[..12]);
    let on_disk = std::fs::metadata(&file).ok().and_then(|m| m.modified().ok())
        .filter(|t| t.elapsed().is_ok_and(|age| age < LIVE_FOR))
        .and_then(|_| std::fs::read_to_string(&file).ok())
        .and_then(|t| t.strip_prefix(&tag).map(|rest| rest.lines().map(String::from).collect::<Vec<_>>()))
        .filter(|l| !l.is_empty());
    let live = match on_disk {
        Some(l) => l,
        None => {
            let l = client.list_models().ok()?;
            if let Some(d) = file.parent() {
                let _ = std::fs::create_dir_all(d);
            }
            let _ = std::fs::write(&file, format!("{tag}{}", l.join("\n")));
            l
        }
    };
    let mut w = WARM.lock().unwrap_or_else(|e| e.into_inner());
    if let Some(warm) = w.as_mut().filter(|w| Arc::ptr_eq(&w.client, client)) {
        warm.live = Some((std::time::Instant::now(), live.clone()));
    }
    Some(live)
}

/// Get the client and the model list ready in the background (the window,
/// at start): the first job then starts at once.
pub fn warm_up(s: &Settings) {
    let Ok(client) = shared_client(s) else { return };
    let _ = std::thread::Builder::new().name("chuck-warm".into()).spawn(move || {
        let _ = live_models(&client);
    });
}

/// The folder's access as the window stores it (`folder_access`: path → mode).
pub fn access_of(s: &Settings, folder: &Path) -> Access {
    let Some(map) = s.map.get("folder_access").and_then(|v| v.as_object()) else { return Access::Guarded };
    map.iter().find(|(k, _)| {
        let p = Path::new(k.as_str());
        p == folder || p.canonicalize().is_ok_and(|c| c == folder)
    }).and_then(|(_, v)| v.as_str()).map_or(Access::Guarded, Access::parse)
}

/// Once: thinking off and calls as text — the settings a faster Chuck starts
/// from (a reasoning level saved before was the old default for most). Turning
/// either back on in Settings sticks; this never runs twice.
pub fn migrate() {
    let mut s = Settings::load();
    if s.map.contains_key("fast_v2") {
        return;
    }
    s.set("reasoning_budget", 0.into());
    s.set("native_tools", false.into());
    s.set("fast_v2", true.into());
    let _ = s.save();
}

/// The models, from the settings (`fleet_deepseek_model` / `fleet_glm_model`:
/// the names older versions saved them under).
pub fn models(s: &Settings) -> Models {
    let mut m = Models::default();
    if let Some(v) = s.str("fleet_deepseek_model") {
        m.main = v.to_string();
    }
    if let Some(v) = s.str("fleet_glm_model") {
        m.vision = v.to_string();
    }
    m
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
        browser: (s.map.get("stealth_fetch").and_then(|v| v.as_bool()) != Some(false)).then(|| browser_pref(s)),
        ..chuck_web::Config::default()
    })
}

/// Which browser drives ```browse``` and reads the pages a plain fetch can't
/// (Settings › Research; the Python app's `browser` setting): "auto",
/// "camoufox", "brave", "chromium", "chrome", or a path.
pub fn browser_pref(s: &Settings) -> String {
    s.str("browser").map(str::trim).filter(|b| !b.is_empty()).unwrap_or("auto").to_string()
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
    let model = shared_client(s)?;
    let mut cfg = RuntimeConfig { models: models(s), ..RuntimeConfig::default() };
    if let Some(live) = live_models(&model) {
        for (from, to) in cfg.models.resolve(&live) {
            notes.push(format!("{from} isn't on your account; using {to}"));
        }
    }
    // the tools as functions the API parses (off: every call written as a fenced block)
    cfg.native_tools = s.map.get("native_tools").and_then(|v| v.as_bool()).unwrap_or(false);
    cfg.debug = s.map.get("debug").and_then(|v| v.as_bool()).unwrap_or(false);
    // what gets in the agents' way is written down (always: it costs nothing), for it to be fixed
    chuck_store::snags::set_path(Some(chuck_store::snags::default_path()));
    cfg.reasoning = Reasoning {
        budget: s.num("reasoning_budget", 0.0, 0.0, 32768.0) as u32,
        max_think_secs: s.num("max_think_seconds", 90.0, 0.0, 1800.0) as u32,
    };
    let mut usd = max_usd.unwrap_or_else(|| s.num("fleet_job_usd", 2.0, 0.01, 1000.0));
    // the window's evidence chain and day/month spend: one history, one budget
    let spend = Arc::new(Spend::at(&Spend::default_path()));
    cfg.day_limit = s.num("budget_day_usd", 0.0, 0.0, 100_000.0);
    cfg.month_limit = s.num("budget_month_usd", 0.0, 0.0, 1_000_000.0);
    cfg.live_limits = true;
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
    cfg.cmd_timeout = std::time::Duration::from_secs_f64(s.num("command_timeout", 1800.0, 30.0, 86_400.0));
    cfg.web = Some(Arc::new(web(s)));
    cfg.browser = Some(browser_pref(s));
    cfg.resume_dir = Some(crate::resume::default_dir());
    cfg.memory = Some(Arc::new(chuck_store::memory::Memory::at(&chuck_store::memory::Memory::default_path())));
    cfg.sources = s.num("research_sources", 6.0, 1.0, 10.0) as usize;
    cfg.access = access_of(s, folder);
    if cfg.access == Access::Guarded && !cfg.bwrap {
        notes.push("bubblewrap isn't installed, so commands can't be kept inside this folder: they still ask before \
            naming anything outside it, but a command could reach further without saying so — install it (pacman -Syu \
            bubblewrap / apt install bubblewrap)".into());
    } else if cfg.access != Access::Full && !cfg.bwrap {
        notes.push(format!("this folder is {}, which needs bubblewrap to run commands — install it (pacman -Syu \
            bubblewrap / apt install bubblewrap); until then commands are refused", cfg.access.label()));
    }
    Ok(Setup { model, cfg, usd, notes })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_accounts_model_list_is_asked_for_once_an_hour_not_once_a_job() {
        use std::io::{BufRead, BufReader, Write};
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}/v1", l.local_addr().unwrap());
        let asked = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let a2 = asked.clone();
        std::thread::spawn(move || {
            for c in l.incoming().flatten() {
                let mut r = BufReader::new(c.try_clone().unwrap());
                let mut line = String::new();
                let _ = r.read_line(&mut line);
                loop {
                    let mut h = String::new();
                    if r.read_line(&mut h).unwrap_or(0) == 0 || h == "\r\n" {
                        break;
                    }
                }
                a2.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                let b = r#"{"data":[{"id":"deepseek-ai/DeepSeek-V4.1-Flash-1017"}]}"#;
                let mut c = c;
                let _ = write!(c, "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{b}", b.len());
            }
        });
        let cache = std::env::temp_dir().join(format!("chuck-models-cache-{}", std::process::id()));
        std::env::set_var("XDG_CACHE_HOME", &cache);
        // two processes' worth: neither shares the other's client, so only the file can save the second call
        for _ in 0..2 {
            let c = Arc::new(SiliconFlow::new(&base, "sk-a", Duration::from_secs(5), true).unwrap());
            assert_eq!(live_models(&c).unwrap(), vec!["deepseek-ai/DeepSeek-V4.1-Flash-1017".to_string()]);
        }
        assert_eq!(asked.load(std::sync::atomic::Ordering::SeqCst), 1);
        // another key is another account: asked again
        let c = Arc::new(SiliconFlow::new(&base, "sk-b", Duration::from_secs(5), true).unwrap());
        live_models(&c).unwrap();
        assert_eq!(asked.load(std::sync::atomic::Ordering::SeqCst), 2);
        std::env::remove_var("XDG_CACHE_HOME");
        let _ = std::fs::remove_dir_all(&cache);
    }

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
