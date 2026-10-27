//! The real system, as triggers see it: a folder's fingerprint, the failed
//! units, how full a disk is, what a (read-only) command prints.

use crate::shell::read_only_problem;
use crate::when::Probe;
use std::path::Path;
use std::time::Duration;

pub struct SystemProbe;

/// The newest mtime, file count and size of a file or folder: cheap, and it
/// changes when anything in it does.
pub fn tree_signature(path: &str, limit: usize) -> String {
    let p = crate::when_expand(path);
    let p = Path::new(&p);
    let Ok(meta) = p.metadata() else { return "missing".into() };
    let ns = |m: &std::fs::Metadata| m.modified().ok().and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map_or(0, |d| d.as_nanos());
    if meta.is_file() {
        return format!("{}:{}", ns(&meta), meta.len());
    }
    let (mut newest, mut n, mut size) = (0u128, 0usize, 0u64);
    let walk = walkdir::WalkDir::new(p).follow_links(false).into_iter().filter_entry(|e| e.depth() == 0
        || !(e.file_type().is_dir() && {
            let name = e.file_name().to_string_lossy();
            name.starts_with('.') || ["node_modules", "__pycache__", "target", ".venv"].contains(&name.as_ref())
        }));
    for e in walk.flatten().filter(|e| e.file_type().is_file()) {
        if let Ok(m) = e.metadata() {
            newest = newest.max(ns(&m));
            n += 1;
            size += m.len();
        }
        if n >= limit {
            break;
        }
    }
    format!("{newest}:{n}:{size}")
}

impl Probe for SystemProbe {
    fn signature(&self, path: &str) -> String {
        tree_signature(path, 20_000)
    }

    fn failed_units(&self) -> Vec<String> {
        let mut out = Vec::new();
        for scope in [&["--failed"][..], &["--user", "--failed"][..]] {
            if let Ok(o) = std::process::Command::new("systemctl").args(scope).args(["--plain", "--no-legend", "--no-pager"])
                .stdin(std::process::Stdio::null()).stderr(std::process::Stdio::null()).output()
            {
                out.extend(String::from_utf8_lossy(&o.stdout).lines().filter_map(|l| l.split_whitespace().next())
                    .map(String::from));
            }
        }
        out
    }

    fn disk_percent(&self, path: &str) -> Option<f64> {
        let p = std::ffi::CString::new(crate::when_expand(path)).ok()?;
        let mut s: libc::statvfs = unsafe { std::mem::zeroed() };
        if unsafe { libc::statvfs(p.as_ptr(), &mut s) } != 0 {
            return None;
        }
        // as df counts it: used / (used + available to users)
        let used = (s.f_blocks - s.f_bfree) as f64;
        let avail = s.f_bavail as f64;
        (used + avail > 0.0).then(|| 100.0 * used / (used + avail))
    }

    fn run(&self, cmd: &str) -> String {
        let jail = chuck_sandbox::bwrap().is_some();
        let why = read_only_problem(cmd, !jail);
        if !why.is_empty() {
            return format!("(not run: {why})");
        }
        let home = std::env::var_os("HOME").map(std::path::PathBuf::from).unwrap_or_else(|| "/".into());
        let mut o = chuck_tools::RunOpts::new(&home);
        o.timeout = Duration::from_secs(60);
        // the whole system read-only; the network stays (checkupdates asks the mirrors)
        o.wrap = jail.then(|| chuck_sandbox::Jail::new(&home).read_only().wrap());
        chuck_tools::bash(cmd, o).output
    }
}
