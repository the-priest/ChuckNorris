//! /doctor and `chuck doctor`: every part Chuck depends on, checked for
//! real (the API answers, the sandbox runs, a search comes back), each with
//! what to do when it doesn't.

use chuck_model::ChatModel;
use chuck_runtime::setup;
use chuck_store::Settings;
use std::path::Path;

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum State {
    Ok,
    Warn,
    Fail,
}

#[derive(Clone, Debug)]
pub struct Check {
    pub name: &'static str,
    pub state: State,
    pub detail: String,
}

fn c(name: &'static str, state: State, detail: impl Into<String>) -> Check {
    Check { name, state, detail: detail.into() }
}

fn free_gb(p: &Path) -> Option<f64> {
    let s = std::ffi::CString::new(p.display().to_string()).ok()?;
    let mut v: libc::statvfs = unsafe { std::mem::zeroed() };
    (unsafe { libc::statvfs(s.as_ptr(), &mut v) } == 0).then(|| v.f_bavail as f64 * v.f_frsize as f64 / 1e9)
}

/// Every check, in order. Takes a few seconds (it talks to the network);
/// run it off the main thread.
pub fn run(folder: Option<&Path>, mcp: Option<&chuck_mcp::Manager>) -> Vec<Check> {
    let s = Settings::load();
    let mut out = Vec::new();
    out.push(c("Chuck", State::Ok, match crate::update::installed() {
        Some(h) => format!("{} · built from {} ({})", env!("CARGO_PKG_VERSION"), &h[..10.min(h.len())], crate::update::BRANCH),
        None => format!("{} · built from source", env!("CARGO_PKG_VERSION")),
    }));
    match crate::update::check() {
        Ok(crate::update::Found::UpToDate(_)) => out.push(c("Updates", State::Ok, "this is the newest version")),
        Ok(crate::update::Found::Newer { latest, .. }) => out.push(c("Updates", State::Warn,
            format!("a newer Chuck is out ({}): /update installs it", &latest[..10.min(latest.len())]))),
        Ok(crate::update::Found::Unknown { .. }) => out.push(c("Updates", State::Ok, "built from source: update with git pull")),
        Err(e) => out.push(c("Updates", State::Warn, format!("couldn't ask GitHub: {e}"))),
    }
    // the model API, for real
    let probe = folder.map(Path::to_path_buf).unwrap_or_else(std::env::temp_dir);
    match setup::client(&s) {
        Err(e) => out.push(c("SiliconFlow", State::Fail, e)),
        Ok(m) => match m.list_models() {
            Ok(models) => {
                out.push(c("SiliconFlow", State::Ok, format!("the key works · {} models on your account", models.len())));
                let mut cfg = setup::fleet(&s);
                let notes = cfg.resolve(&models);
                out.push(c("Fleet models", if notes.is_empty() { State::Ok } else { State::Warn },
                           if notes.is_empty() { format!("lead {}, workers {}, eyes {}", cfg.deepseek_model, cfg.worker_model, cfg.glm_model) } else {
                               notes.iter().map(|(a, b)| format!("{a} isn't on your account: {b} is used")).collect::<Vec<_>>().join("; ")
                           }));
            }
            Err(e) => out.push(c("SiliconFlow", State::Fail, format!("the API didn't answer: {e}"))),
        },
    }
    let spend = chuck_store::Spend::at(&chuck_store::Spend::default_path());
    let now = chuck_sandbox::now_secs();
    let (dl, ml) = (s.num("budget_day_usd", 0.0, 0.0, 1e5), s.num("budget_month_usd", 0.0, 0.0, 1e6));
    let lim = |v: f64| if v > 0.0 { chuck_model::fmt_usd(v) } else { "no limit".into() };
    out.push(c("Spending", if spend.over(dl, ml).is_some() { State::Fail } else { State::Ok },
               format!("today {} of {} · this month {} of {}", chuck_model::fmt_usd(spend.today(now)), lim(dl),
                       chuck_model::fmt_usd(spend.month(now)), lim(ml))));
    // the sandbox, for real
    match chuck_sandbox::bwrap() {
        None => out.push(c("Sandbox", State::Warn, "bubblewrap isn't installed: workers' copies aren't jailed and \
            sandboxed folders can't run commands (pacman -Syu bubblewrap / apt install bubblewrap)")),
        Some(b) => {
            let ok = std::process::Command::new(&b).args(["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "true"])
                .stdin(std::process::Stdio::null()).stderr(std::process::Stdio::null()).status().is_ok_and(|st| st.success());
            out.push(if ok { c("Sandbox", State::Ok, "bubblewrap works") } else {
                c("Sandbox", State::Fail, "bubblewrap is installed but can't start a sandbox here (user namespaces off?)")
            });
        }
    }
    if let Some(f) = folder {
        out.push(c("This folder", State::Ok, format!("{} · {}", f.display(), setup::access_of(&s, f).label())));
    }
    // research, for real
    let web = setup::web(&s);
    match web.search("linux kernel", 3) {
        Ok(h) => out.push(c("Web search", State::Ok, format!("{} results", h.len()))),
        Err(e) => out.push(c("Web search", State::Warn, format!("{e} — set your own SearXNG in Settings › Research"))),
    }
    let pref = setup::browser_pref(&s);
    use chuck_web::browser::{pick, Purpose};
    match pick(&pref, Purpose::Check) {
        Ok(e) => out.push(c("Browser", State::Ok, format!("{} — the Lead's page checks", e.describe()))),
        Err(e) => out.push(c("Browser", State::Warn, format!("{e}: the Lead can't check web pages"))),
    }
    match pick(&pref, Purpose::Read) {
        Ok(e) => out.push(c("Blocked pages", if matches!(e, chuck_web::browser::Engine::Camoufox(_)) { State::Ok } else {
                State::Warn }, format!("read with {}{}", e.describe(), if matches!(e, chuck_web::browser::Engine::Camoufox(_)) {
                String::new() } else {
                    // why not Camoufox, exactly (not installed, or its browser not downloaded)
                    format!(" — Camoufox gets past more bot checks. {}",
                            chuck_web::camoufox::find(None).err().unwrap_or_else(|| chuck_web::camoufox::INSTALL.into()))
                }))),
        Err(e) => out.push(c("Blocked pages", State::Warn, format!("{e}: pages that need JavaScript or turn bots away \
            can't be read"))),
    }
    // voice
    let vp = chuck_voice::Prefs::from(&s);
    let voice = chuck_voice::Voice::new();
    out.push(c("Voice", match voice.engine_for(&vp) { Some("kokoro") | Some("piper") => State::Ok, _ => State::Warn },
               voice.status(&vp)));
    out.push(c("Voice input", if chuck_voice::listen::ready() { State::Ok } else { State::Warn },
               chuck_voice::listen::status(s.str("mic_command").unwrap_or(""))));
    // MCP and language servers
    if let Some(m) = mcp {
        let st = m.status();
        if st.is_empty() {
            out.push(c("MCP servers", State::Ok, "none set up"));
        }
        for x in st {
            out.push(Check { name: "MCP server", state: if x.state == "running" { State::Ok } else if x.state == "failed" {
                State::Fail } else { State::Warn }, detail: format!("{}: {} {}", x.name, x.state, x.detail) });
        }
    }
    let langs: Vec<String> = chuck_code::lsp::LANGS.iter().map(|(l, _, _)| match chuck_code::lsp::server_for(l, &probe) {
        Some(argv) => format!("{l}: {}", Path::new(&argv[0]).file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()),
        None => format!("{l}: none (install {})", chuck_code::lsp::install_hint(l)),
    }).collect();
    out.push(c("Language servers", State::Ok, langs.join(" · ")));
    let reg = chuck_daemon::Registry::load(&chuck_daemon::Registry::default_path());
    out.push(c("Daemons", State::Ok, format!("{} set up, {} on (they run while Chuck is open)", reg.items.len(),
                                             reg.items.iter().filter(|d| d.enabled()).count())));
    let mem = chuck_store::memory::Memory::at(&chuck_store::memory::Memory::default_path());
    out.push(c("Memory", State::Ok, format!("{} facts", mem.all().len())));
    let data = chuck_store::ledger::data_dir();
    let _ = std::fs::create_dir_all(&data);
    match free_gb(&data) {
        Some(g) if g < 1.0 => out.push(c("Disk", State::Fail, format!("{g:.1} GB free where Chuck keeps its data: workers' copies need room"))),
        Some(g) if g < 5.0 => out.push(c("Disk", State::Warn, format!("{g:.1} GB free"))),
        Some(g) => out.push(c("Disk", State::Ok, format!("{g:.0} GB free"))),
        None => {}
    }
    let errs = chuck_store::crashlog::recent(3, 7);
    out.push(if errs.is_empty() { c("Errors", State::Ok, "none in the last 7 days") } else {
        c("Errors", State::Warn, format!("{} in the last 7 days, latest {} {}: {} — the log is {}", errs.len(), errs[0].0,
            errs[0].1, errs[0].2.chars().take(160).collect::<String>(), chuck_store::crashlog::log_path().display()))
    });
    let ledger = chuck_store::Ledger::at(&chuck_store::Ledger::default_path());
    let v = ledger.verify();
    out.push(if v.ok { c("Evidence ledger", State::Ok, format!("{} commands recorded; the chain checks out", v.count)) } else {
        c("Evidence ledger", State::Fail, format!("the chain is broken: {}", v.message))
    });
    out
}

/// As text (for the terminal and for the chat card).
pub fn report(checks: &[Check]) -> String {
    checks.iter().map(|k| format!("{} **{}** — {}", match k.state { State::Ok => "✓", State::Warn => "!", State::Fail => "✗" },
                                  k.name, k.detail)).collect::<Vec<_>>().join("\n")
}
