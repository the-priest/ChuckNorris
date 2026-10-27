//! Is this the newest Chuck? The installer records the commit it built
//! (~/.local/share/chucknorris/rust-commit); the branch's newest commit is
//! asked of GitHub at most once a day (Settings can turn that off). "Update
//! now" runs the branch's installer without touching system packages or
//! the voice models, which replaces the binary; a restart opens the new one.

use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::Duration;

pub const REPO: &str = "the-priest/ChuckNorris";
pub const BRANCH: &str = "rust-rewrite";
const CHECK_EVERY: f64 = 86_400.0;

fn data_dir() -> PathBuf {
    chuck_store::ledger::data_dir()
}

/// The commit this Chuck was built from (None: built by hand, not installed).
pub fn installed() -> Option<String> {
    std::fs::read_to_string(data_dir().join("rust-commit")).ok().map(|s| s.trim().to_string())
        .filter(|s| s.len() >= 7 && s.chars().all(|c| c.is_ascii_hexdigit()))
}

fn agent() -> ureq::Agent {
    ureq::Agent::config_builder().timeout_global(Some(Duration::from_secs(15))).http_status_as_error(false)
        .tls_config(ureq::tls::TlsConfig::builder().root_certs(ureq::tls::RootCerts::PlatformVerifier).build())
        .build().new_agent()
}

/// The branch's newest commit.
pub fn latest() -> Result<String, String> {
    let mut r = agent().get(&format!("https://api.github.com/repos/{REPO}/commits/{BRANCH}"))
        .header("Accept", "application/vnd.github.sha").header("User-Agent", "chuck").call()
        .map_err(|e| e.to_string())?;
    let status = r.status().as_u16();
    let body = r.body_mut().read_to_string().map_err(|e| e.to_string())?;
    let sha = body.trim().to_string();
    if status != 200 || sha.len() < 7 || !sha.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err(format!("GitHub answered HTTP {status}"));
    }
    Ok(sha)
}

/// What an update check found.
#[derive(Clone, Debug, PartialEq)]
pub enum Found {
    UpToDate(String),
    Newer { installed: String, latest: String },
    /// This binary wasn't installed by the installer: there's nothing to compare.
    Unknown { latest: String },
}

pub fn check() -> Result<Found, String> {
    let latest = latest()?;
    let _ = mark_checked();
    Ok(match installed() {
        Some(i) if latest.starts_with(&i) || i.starts_with(&latest) => Found::UpToDate(i),
        Some(i) => Found::Newer { installed: i, latest },
        None => Found::Unknown { latest },
    })
}

fn state_path() -> PathBuf {
    chuck_store::settings::config_dir().join("rust-update.json")
}

/// More than a day since the last check (and checking is on).
pub fn due() -> bool {
    let s = chuck_store::Settings::load();
    if s.map.get("check_updates").and_then(|v| v.as_bool()) == Some(false) {
        return false;
    }
    let last = std::fs::read_to_string(state_path()).ok().and_then(|t| serde_json::from_str::<serde_json::Value>(&t).ok())
        .and_then(|v| v.get("checked").and_then(|c| c.as_f64())).unwrap_or(0.0);
    chuck_daemon::now() - last >= CHECK_EVERY
}

fn mark_checked() -> std::io::Result<()> {
    let p = state_path();
    if let Some(d) = p.parent() {
        std::fs::create_dir_all(d)?;
    }
    std::fs::write(p, serde_json::json!({"checked": chuck_daemon::now()}).to_string())
}

/// Download the branch's installer and run it (no system packages, no voice
/// download): it builds and installs the new binary. Each line of its
/// output goes to `line`. Ok when it installed the new Chuck.
pub fn run(line: &mut dyn FnMut(&str)) -> Result<(), String> {
    let script = std::env::temp_dir().join(format!("chuck-install-{}.sh", std::process::id()));
    let mut r = agent().get(&format!("https://raw.githubusercontent.com/{REPO}/{BRANCH}/install.sh"))
        .header("User-Agent", "chuck").call().map_err(|e| e.to_string())?;
    if r.status().as_u16() != 200 {
        return Err(format!("couldn't download the installer (HTTP {})", r.status().as_u16()));
    }
    let text = r.body_mut().read_to_string().map_err(|e| e.to_string())?;
    if !text.starts_with("#!") || !text.contains("CHUCK_DEPS") {
        return Err("the downloaded installer doesn't look right; nothing was run".into());
    }
    std::fs::write(&script, text).map_err(|e| e.to_string())?;
    let mut cmd = Command::new("bash");
    chuck_tools::tag(&mut cmd);
    let mut child = cmd.arg(&script)
        .env("CHUCK_DEPS", "0").env("CHUCK_VOICE", "0").env("CHUCK_BRANCH", BRANCH).env("NO_COLOR", "1")
        .stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().map_err(|e| e.to_string())?;
    let err = child.stderr.take();
    let errs = std::thread::spawn(move || err.map(|e| BufReader::new(e).lines().map_while(Result::ok).collect::<Vec<_>>()));
    if let Some(out) = child.stdout.take() {
        for l in BufReader::new(out).lines().map_while(Result::ok) {
            let clean = strip_ansi(&l);
            if !clean.trim().is_empty() {
                line(&clean);
            }
        }
    }
    let status = child.wait().map_err(|e| e.to_string())?;
    let _ = std::fs::remove_file(&script);
    let tail = errs.join().ok().flatten().unwrap_or_default();
    if !status.success() {
        return Err(format!("the installer stopped (exit {}): {}", status.code().unwrap_or(-1),
                           tail.iter().rev().take(3).rev().cloned().collect::<Vec<_>>().join(" / ")));
    }
    Ok(())
}

fn strip_ansi(s: &str) -> String {
    let mut out = String::new();
    let mut it = s.chars().peekable();
    while let Some(c) = it.next() {
        if c == '\u{1b}' {
            if it.peek() == Some(&'[') {
                it.next();
                for d in it.by_ref() {
                    if d.is_ascii_alphabetic() {
                        break;
                    }
                }
            }
            continue;
        }
        if c != '\r' {
            out.push(c);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    #[test]
    fn colours_are_dropped_from_the_installer_output() {
        assert_eq!(super::strip_ansi("\u{1b}[38;5;42m✓ chuck installed\u{1b}[0m"), "✓ chuck installed");
    }
}
