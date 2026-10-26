//! Finding things in a folder: grep, glob, and a cheap fingerprint that says
//! whether anything changed (so a shell command that edited files counts as
//! a change, and earlier verification stops counting).

use crate::cow::SKIP;
use std::hash::{Hash, Hasher};
use std::path::Path;

fn walk(root: &Path) -> impl Iterator<Item = walkdir::DirEntry> {
    walkdir::WalkDir::new(root).follow_links(false).into_iter()
        .filter_entry(|e| e.depth() == 0 || !(e.file_type().is_dir()
            && SKIP.contains(&e.file_name().to_string_lossy().as_ref())))
        .flatten()
        .filter(|e| e.file_type().is_file())
}

const BINARY_EXT: &[&str] = &["png", "jpg", "jpeg", "gif", "webp", "ico", "pdf", "zip", "gz", "xz", "zst", "so",
    "o", "a", "bin", "pyc", "lock", "woff", "woff2", "ttf", "mp3", "mp4", "onnx", "wasm"];

/// `path:line:text` for each match of `pattern`, at most `max_hits`.
pub fn grep(root: &Path, pattern: &str, sub: &str, max_hits: usize) -> Result<String, String> {
    let rx = regex::Regex::new(pattern).map_err(|e| format!("bad regex: {e}"))?;
    let base = if sub.trim().is_empty() { root.to_path_buf() } else { crate::fs::safe(root, sub).map_err(|e| e.to_string())? };
    let mut rows = Vec::new();
    let mut hits = 0;
    for e in walk(&base) {
        let p = e.path();
        if p.extension().and_then(|x| x.to_str()).is_some_and(|x| BINARY_EXT.contains(&x.to_lowercase().as_str())) {
            continue;
        }
        if e.metadata().map(|m| m.len() > crate::fs::MAX_FILE_BYTES as u64).unwrap_or(true) {
            continue;
        }
        let Ok(text) = std::fs::read_to_string(p) else { continue };
        let rel = p.strip_prefix(root).unwrap_or(p).display().to_string();
        for (i, line) in text.lines().enumerate() {
            if rx.is_match(line) {
                hits += 1;
                if rows.len() < max_hits {
                    rows.push(format!("{rel}:{}:{}", i + 1, line.chars().take(220).collect::<String>()));
                }
            }
        }
    }
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
        assert_eq!(glob(&d, "*.py", 10).unwrap(), "src/a.py");
        let f1 = fingerprint(&d);
        assert_eq!(f1, fingerprint(&d));
        std::thread::sleep(std::time::Duration::from_millis(10));
        std::fs::write(d.join("src/b.py"), "x").unwrap();
        assert_ne!(f1, fingerprint(&d));
    }
}
