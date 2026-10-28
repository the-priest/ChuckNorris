//! Finding things in a folder: grep, glob, and a cheap fingerprint that says
//! whether anything changed (so a shell command that edited files counts as
//! a change, and earlier verification stops counting).

use crate::snapshot::SKIP;
use std::hash::{Hash, Hasher};
use std::path::Path;

fn walk(root: &Path) -> impl Iterator<Item = walkdir::DirEntry> {
    walkdir::WalkDir::new(root).follow_links(false).into_iter()
        .filter_entry(|e| e.depth() == 0 || !(e.file_type().is_dir()
            && SKIP.contains(&e.file_name().to_string_lossy().as_ref())))
        .flatten()
        .filter(|e| e.file_type().is_file())
}

/// The folder's files at a glance, shallowest first (at most `max`, three
/// folders deep; dependencies and build output left out), and how many more
/// there are past those. Cheap on any folder: it stops looking at `max * 20`.
pub fn listing(root: &Path, max: usize) -> (Vec<String>, usize) {
    let seen: Vec<(usize, String)> = walkdir::WalkDir::new(root).follow_links(false).max_depth(3)
        .sort_by_file_name().into_iter()
        .filter_entry(|e| e.depth() == 0 || !(e.file_type().is_dir()
            && SKIP.contains(&e.file_name().to_string_lossy().as_ref())))
        .flatten().filter(|e| e.file_type().is_file())
        .filter_map(|e| Some((e.depth(), e.path().strip_prefix(root).ok()?.to_string_lossy().into_owned())))
        .take(max * 20).collect();
    let mut files = seen;
    files.sort();
    let more = files.len().saturating_sub(max);
    let mut shown: Vec<String> = files.into_iter().take(max).map(|(_, p)| p).collect();
    shown.sort();
    (shown, more)
}

const BINARY_EXT: &[&str] = &["png", "jpg", "jpeg", "gif", "webp", "ico", "pdf", "zip", "gz", "xz", "zst", "so",
    "o", "a", "bin", "pyc", "lock", "woff", "woff2", "ttf", "mp3", "mp4", "onnx", "wasm"];

/// One file's matching lines (all of them: the caller keeps the first few).
fn grep_file(rx: &regex::Regex, whole: &regex::Regex, root: &Path, p: &Path) -> Vec<String> {
    let Ok(text) = std::fs::read_to_string(p) else { return Vec::new() };
    // most files don't match at all: one pass over the whole text says so, far faster than line by line
    // (`whole` is the pattern with ^ and $ at every line, as the line-by-line match reads them)
    if !whole.is_match(&text) {
        return Vec::new();
    }
    let rel = p.strip_prefix(root).unwrap_or(p).display().to_string();
    text.lines().enumerate().filter(|(_, l)| rx.is_match(l))
        .map(|(i, line)| format!("{rel}:{}:{}", i + 1, line.chars().take(220).collect::<String>())).collect()
}

/// `path:line:text` for each match of `pattern`, at most `max_hits`. Files
/// are searched on every core; the results come in the walk's order.
pub fn grep(root: &Path, pattern: &str, sub: &str, max_hits: usize) -> Result<String, String> {
    let rx = regex::Regex::new(pattern).map_err(|e| format!("bad regex: {e}"))?;
    let whole = regex::RegexBuilder::new(pattern).multi_line(true).build().map_err(|e| format!("bad regex: {e}"))?;
    let base = if sub.trim().is_empty() { root.to_path_buf() } else { crate::fs::safe(root, sub).map_err(|e| e.to_string())? };
    let files: Vec<std::path::PathBuf> = walk(&base).filter(|e| {
        !e.path().extension().and_then(|x| x.to_str()).is_some_and(|x| BINARY_EXT.contains(&x.to_lowercase().as_str()))
            && e.metadata().is_ok_and(|m| m.len() <= crate::fs::MAX_FILE_BYTES as u64)
    }).map(|e| e.into_path()).collect();
    let threads = std::thread::available_parallelism().map_or(4, |n| n.get()).clamp(1, 16).min(files.len().max(1));
    let next = std::sync::atomic::AtomicUsize::new(0);
    let mut found: Vec<(usize, Vec<String>)> = std::thread::scope(|s| {
        let workers: Vec<_> = (0..threads).map(|_| s.spawn(|| {
            let mut mine = Vec::new();
            loop {
                let i = next.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                let Some(p) = files.get(i) else { break };
                let rows = grep_file(&rx, &whole, root, p);
                if !rows.is_empty() {
                    mine.push((i, rows));
                }
            }
            mine
        })).collect();
        workers.into_iter().flat_map(|w| w.join().unwrap_or_default()).collect()
    });
    found.sort_by_key(|(i, _)| *i);
    let hits: usize = found.iter().map(|(_, r)| r.len()).sum();
    let mut rows: Vec<String> = found.into_iter().flat_map(|(_, r)| r).take(max_hits).collect();
    if rows.is_empty() {
        return Ok(format!("no matches for /{pattern}/"));
    }
    if hits > rows.len() {
        rows.push(format!("… {} more matches (narrow the pattern or the path)", hits - rows.len()));
    }
    Ok(rows.join("\n"))
}

/// Files matching a glob, relative to root.
pub fn glob(root: &Path, pattern: &str, max: usize) -> Result<String, String> {
    let pat = if pattern.contains('/') || pattern.starts_with("**") { pattern.to_string() } else { format!("**/{pattern}") };
    let g = globset::Glob::new(&pat).map_err(|e| format!("bad glob: {e}"))?.compile_matcher();
    let mut out: Vec<String> = walk(root).filter_map(|e| {
        let rel = e.path().strip_prefix(root).ok()?.to_string_lossy().into_owned();
        g.is_match(&rel).then_some(rel)
    }).collect();
    out.sort();
    let n = out.len();
    out.truncate(max);
    if n == 0 {
        return Ok(format!("no files match {pattern}"));
    }
    if n > max {
        out.push(format!("… {} more", n - max));
    }
    Ok(out.join("\n"))
}

/// A hash of every file's path, size and mtime: equal before and after a
/// command means it changed nothing.
pub fn fingerprint(root: &Path) -> u64 {
    let mut h = std::collections::hash_map::DefaultHasher::new();
    let mut rows: Vec<(String, u64, i128)> = walk(root).filter_map(|e| {
        let m = e.metadata().ok()?;
        let mt = m.modified().ok()?.duration_since(std::time::UNIX_EPOCH).ok()?.as_nanos() as i128;
        Some((e.path().strip_prefix(root).ok()?.to_string_lossy().into_owned(), m.len(), mt))
    }).collect();
    rows.sort();
    rows.hash(&mut h);
    h.finish()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grep_glob_fingerprint() {
        let d = std::env::temp_dir().join(format!("chuck-search-{}", std::process::id()));
        std::fs::create_dir_all(d.join("src")).unwrap();
        std::fs::write(d.join("src/a.py"), "def retry():\n    pass\n").unwrap();
        std::fs::create_dir_all(d.join("node_modules")).unwrap();
        std::fs::write(d.join("node_modules/x.js"), "retry").unwrap();
        let g = grep(&d, "retry", "", 10).unwrap();
        assert_eq!(g, "src/a.py:1:def retry():");
        // anchors are per line, as ever (the whole-file pre-check reads them so too)
        std::fs::write(d.join("src/c.py"), "x = 1\n    pass_through()\n").unwrap();
        assert_eq!(grep(&d, r"^\s+pass_", "", 10).unwrap(), "src/c.py:2:    pass_through()");
        assert_eq!(grep(&d, r"retry\(\):$", "", 10).unwrap(), "src/a.py:1:def retry():");
        assert_eq!(glob(&d, "*.py", 10).unwrap(), "src/a.py\nsrc/c.py");
        // the listing: shallowest first, dependencies left out, the rest counted
        let (files, more) = listing(&d, 2);
        assert_eq!((files, more), (vec!["src/a.py".to_string(), "src/c.py".to_string()], 0));
        let (files, more) = listing(&d, 1);
        assert_eq!((files.len(), more), (1, 1));
        let f1 = fingerprint(&d);
        assert_eq!(f1, fingerprint(&d));
        std::thread::sleep(std::time::Duration::from_millis(10));
        std::fs::write(d.join("src/b.py"), "x").unwrap();
        assert_ne!(f1, fingerprint(&d));
    }
}
