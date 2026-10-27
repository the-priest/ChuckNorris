//! Every error Chuck hits on your machine, written down: panics (in the
//! window or any thread) and failed jobs go to
//! ~/.local/state/chucknorris/chuck.log (XDG_STATE_HOME honoured), the same
//! file the Python app writes, with a timestamp, the version and what
//! happened. It turns over at 1 MB (one old copy kept), is readable only by
//! you, and anything shaped like a key is blanked first. /doctor shows the
//! latest entries.

use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::PathBuf;
use std::sync::Mutex;

const MAX_BYTES: u64 = 1_000_000;
static LOCK: Mutex<()> = Mutex::new(());

pub fn log_path() -> PathBuf {
    let base = std::env::var_os("XDG_STATE_HOME").map(PathBuf::from).filter(|p| p.is_absolute())
        .unwrap_or_else(|| std::env::var_os("HOME").map(PathBuf::from).unwrap_or_default().join(".local/state"));
    base.join("chucknorris").join("chuck.log")
}

/// Append one entry. Never fails: logging must not become the crash.
pub fn record(kind: &str, text: &str) {
    let _g = LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let p = log_path();
    let _ = (|| -> std::io::Result<()> {
        std::fs::create_dir_all(p.parent().unwrap_or(&p))?;
        if p.metadata().is_ok_and(|m| m.len() > MAX_BYTES) {
            std::fs::rename(&p, p.with_extension("log.1"))?;
        }
        let mut f = std::fs::OpenOptions::new().create(true).append(true).mode(0o600).open(&p)?;
        let stamp = crate::ledger::local_stamp("%Y-%m-%d %H:%M:%S");
        write!(f, "=== {stamp}  v{}  {}\n{}\n\n", env!("CARGO_PKG_VERSION"), crate::redact(kind),
               crate::redact(text.trim_end()))
    })();
}

/// Panics anywhere go to the log (and still to stderr, as before).
pub fn install() {
    let prev = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let thread = std::thread::current().name().unwrap_or("?").to_string();
        let msg = info.payload().downcast_ref::<&str>().map(|s| s.to_string())
            .or_else(|| info.payload().downcast_ref::<String>().cloned()).unwrap_or_else(|| "panic".into());
        let at = info.location().map(|l| format!(" at {}:{}", l.file(), l.line())).unwrap_or_default();
        let bt = std::backtrace::Backtrace::force_capture();
        record(&format!("panic in thread {thread}"), &format!("{msg}{at}\n{bt}"));
        prev(info);
    }));
}

/// (when, what, its first line) of entries from the last `days` days,
/// newest first.
pub fn recent(limit: usize, days: u64) -> Vec<(String, String, String)> {
    let Ok(text) = std::fs::read_to_string(log_path()) else { return Vec::new() };
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    let cutoff = crate::ledger::stamp_at(now.saturating_sub(days * 86_400) as i64, "%Y-%m-%d %H:%M:%S");
    let mut out: Vec<(String, String, String)> = text.split("=== ").skip(1).filter_map(|block| {
        let (head, body) = block.split_once('\n').unwrap_or((block, ""));
        let parts: Vec<&str> = head.split("  ").collect();
        let when = parts.first()?.trim().to_string();
        if when.len() != 19 || when < cutoff {
            return None;
        }
        let what = if parts.len() > 2 { parts[2..].join("  ").trim().to_string() } else { String::new() };
        let first = body.lines().find(|l| !l.trim().is_empty()).unwrap_or("").trim().to_string();
        Some((when, what, first))
    }).collect();
    out.reverse();
    out.truncate(limit);
    out
}

#[cfg(test)]
mod tests {
    #[test]
    fn entries_are_written_redacted_and_read_back() {
        let d = std::env::temp_dir().join(format!("chuck-crash-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        // the only test in this crate that sets it
        unsafe { std::env::set_var("XDG_STATE_HOME", &d) };
        super::record("job failed", "the API refused key sk-abcdefghijklmnop1234\nmore");
        super::record("panic in thread main", "index out of bounds at src/x.rs:3");
        let text = std::fs::read_to_string(super::log_path()).unwrap();
        assert!(!text.contains("sk-abcdefghijklmnop1234"), "{text}");
        let r = super::recent(5, 7);
        assert_eq!(r.len(), 2);
        assert_eq!(r[0].1, "panic in thread main");
        assert_eq!(r[0].2, "index out of bounds at src/x.rs:3");
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(std::fs::metadata(super::log_path()).unwrap().permissions().mode() & 0o777, 0o600);
        let _ = std::fs::remove_dir_all(&d);
    }
}
