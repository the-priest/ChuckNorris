//! ```patch``` with a unified diff (git diff style), any number of files,
//! applied all or nothing. Hunks are placed by their context, so slightly
//! wrong line numbers don't matter; a hunk that doesn't match changes
//! nothing anywhere. New files (--- /dev/null), deletions (+++ /dev/null)
//! and renames work. A port of the Python app's builder.plan_patch.

use crate::fs::{eol_of, read_keep, safe, FsError};
use regex::Regex;
use std::collections::HashMap;
use std::path::Path;
use std::sync::OnceLock;

/// One file of a diff: its old and new paths (None: /dev/null) and hunks
/// (the old start line, the body lines).
#[derive(Clone, Debug, PartialEq)]
pub struct FileDiff {
    pub old: Option<String>,
    pub new: Option<String>,
    pub hunks: Vec<(usize, Vec<String>)>,
}

fn diff_path(p: &str) -> Option<String> {
    let p = p.split('\t').next().unwrap_or("").trim();
    if p == "/dev/null" {
        return None;
    }
    Some(p.strip_prefix("a/").or_else(|| p.strip_prefix("b/")).unwrap_or(p).to_string())
}

/// The files of a unified diff. Err: a diff it can't read.
pub fn parse(text: &str) -> Result<Vec<FileDiff>, String> {
    static HUNK: OnceLock<Regex> = OnceLock::new();
    let hunk_rx = HUNK.get_or_init(|| Regex::new(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@").unwrap());
    // split on \n only: a diff of a file with form feeds or U+2028 must carry them through
    let t = text.replace("\r\n", "\n");
    let mut lines: Vec<&str> = t.split('\n').collect();
    if lines.last() == Some(&"") {
        lines.pop();
    }
    let mut files: Vec<FileDiff> = Vec::new();
    let mut in_hunk = false;
    let mut i = 0;
    while i < lines.len() {
        let ln = lines[i];
        if ln.starts_with("--- ") && lines.get(i + 1).is_some_and(|n| n.starts_with("+++ ")) {
            files.push(FileDiff { old: diff_path(&ln[4..]), new: diff_path(&lines[i + 1][4..]), hunks: Vec::new() });
            in_hunk = false;
            i += 2;
            continue;
        }
        if let Some(c) = hunk_rx.captures(ln) {
            let f = files.last_mut().ok_or("a hunk before any ---/+++ file header")?;
            f.hunks.push((c[1].parse().unwrap_or(1), Vec::new()));
            in_hunk = true;
        } else if in_hunk && (ln.starts_with([' ', '+', '-']) || ln.is_empty()) {
            let f = files.last_mut().expect("in a hunk, so in a file");
            f.hunks.last_mut().expect("in a hunk").1.push(if ln.is_empty() { " ".into() } else { ln.to_string() });
        }
        // "\ No newline at end of file", "diff --git", "index …", modes: nothing to do
        i += 1;
    }
    if files.is_empty() {
        return Err("no ---/+++ file headers found".into());
    }
    Ok(files)
}

/// Where `needle` sits in `hay`, nearest to `near`. Err(true): nowhere;
/// Err(false): in two places equally near (ambiguous).
fn locate(hay: &[String], needle: &[String], near: usize, loose: bool) -> Result<usize, bool> {
    if needle.is_empty() {
        return Ok(near.min(hay.len()));
    }
    let norm = |s: &str| if loose { s.split_whitespace().collect::<Vec<_>>().join(" ") } else { s.trim_end().to_string() };
    let n: Vec<String> = needle.iter().map(|x| norm(x)).collect();
    if hay.len() < n.len() {
        return Err(true);
    }
    let mut hits: Vec<usize> = (0..=hay.len() - n.len())
        .filter(|&i| norm(&hay[i]) == n[0] && hay[i..i + n.len()].iter().zip(&n).all(|(h, w)| norm(h) == *w)).collect();
    if hits.is_empty() {
        return Err(true);
    }
    hits.sort_by_key(|&i| i.abs_diff(near));
    if hits.len() > 1 && hits[0].abs_diff(near) == hits[1].abs_diff(near) {
        return Err(false);
    }
    Ok(hits[0])
}

/// Apply hunks to text (LF line endings). Err names the hunk.
pub fn apply_hunks(content: &str, hunks: &[(usize, Vec<String>)], label: &str) -> Result<String, String> {
    let had_nl = content.is_empty() || content.ends_with('\n');
    let mut lines: Vec<String> = if content.is_empty() { Vec::new() } else { content.split('\n').map(String::from).collect() };
    if lines.last().is_some_and(|l| l.is_empty()) && had_nl {
        lines.pop();
    }
    let mut offset: isize = 0;
    for (k, (start, body)) in hunks.iter().enumerate() {
        let old: Vec<String> = body.iter().filter(|x| x.starts_with([' ', '-'])).map(|x| x[1..].to_string()).collect();
        let new: Vec<String> = body.iter().filter(|x| x.starts_with([' ', '+'])).map(|x| x[1..].to_string()).collect();
        let near = (*start as isize - 1 + offset).max(0) as usize;
        let at = match locate(&lines, &old, near, false).or_else(|e| if e { locate(&lines, &old, near, true) } else { Err(e) }) {
            Ok(a) => a,
            Err(true) => return Err(format!("{label}: hunk {} doesn't match the file (its context and removed lines must \
                be the file's current lines — read it again)", k + 1)),
            Err(false) => return Err(format!("{label}: hunk {} matches more than one place — add context", k + 1)),
        };
        // context lines stay as the file has them (a loose match forgave their whitespace)
        let mut repl = Vec::with_capacity(new.len());
        let mut j = at;
        for x in body {
            match x.as_bytes()[0] {
                b' ' => {
                    repl.push(lines[j].clone());
                    j += 1;
                }
                b'-' => j += 1,
                _ => repl.push(x[1..].to_string()),
            }
        }
        offset += new.len() as isize - old.len() as isize;
        lines.splice(at..at + old.len(), repl);
    }
    let mut out = lines.join("\n");
    if had_nl && !lines.is_empty() {
        out.push('\n');
    }
    Ok(out)
}

/// One file's change: (path, before, after); None before: a new file;
/// None after: deleted.
pub type Planned = (String, Option<String>, Option<String>);

/// A whole diff worked out without touching the disk: one entry per file
/// (what is on disk now, and what it becomes). Err: nothing may be applied.
pub fn plan(root: &Path, text: &str) -> Result<Vec<Planned>, FsError> {
    let bad = FsError::Refused;
    let mut pending: HashMap<String, Option<String>> = HashMap::new();
    let mut out: Vec<Planned> = Vec::new();
    let current = |pending: &HashMap<String, Option<String>>, rel: &str| -> Result<Option<String>, FsError> {
        if let Some(c) = pending.get(rel) {
            return Ok(c.clone());
        }
        let p = safe(root, rel)?;
        Ok(if p.is_file() { Some(read_keep(&p).map_err(|e| bad(format!("{rel}: {e}")))?) } else { None })
    };
    for f in parse(text).map_err(bad)? {
        let rel = f.new.clone().or(f.old.clone()).ok_or_else(|| bad("a file with /dev/null on both sides".into()))?;
        for r in [&f.old, &f.new].into_iter().flatten() {
            let p = safe(root, r)?;
            let base = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());
            if p.strip_prefix(&base).unwrap_or(&p).components().any(|c| c.as_os_str() == ".git") {
                return Err(bad(format!("{r}: git's own files aren't patched")));
            }
        }
        let before = current(&pending, &rel)?;
        if f.new.is_none() {
            if before.is_none() {
                return Err(bad(format!("{rel}: can't delete — it doesn't exist")));
            }
            out.push((rel.clone(), before, None));
            pending.insert(rel, None);
            continue;
        }
        if f.old.is_none() && before.is_some() && !f.hunks.is_empty() {
            return Err(bad(format!("{rel}: already exists — diff it against its current content")));
        }
        let renamed = matches!((&f.old, &f.new), (Some(o), Some(n)) if o != n);
        if renamed && before.is_some() {
            return Err(bad(format!("{rel}: can't rename onto a file that exists")));
        }
        // a rename's hunks apply to the old file's text
        let src = if renamed { current(&pending, f.old.as_deref().unwrap_or(""))? } else { before.clone() };
        if renamed && src.is_none() {
            return Err(bad(format!("{}: can't rename — it doesn't exist", f.old.as_deref().unwrap_or(""))));
        }
        let b = src.unwrap_or_default();
        let eol = eol_of(&b);
        let lf = if eol == "\r\n" { b.replace("\r\n", "\n") } else { b };
        let mut after = apply_hunks(&lf, &f.hunks, &rel).map_err(bad)?;
        if eol == "\r\n" {
            after = after.replace('\n', "\r\n");
        }
        if let (Some(o), Some(n)) = (&f.old, &f.new) {
            if o != n {
                // a rename: the old file goes
                let ob = current(&pending, o)?;
                out.push((o.clone(), ob, None));
                pending.insert(o.clone(), None);
            }
        }
        out.push((rel.clone(), before, Some(after.clone())));
        pending.insert(rel, Some(after));
    }
    // one entry per file: the first 'before' (what's on disk), the last 'after'
    let mut merged: Vec<Planned> = Vec::new();
    for (rel, before, after) in out {
        match merged.iter_mut().find(|m| m.0 == rel) {
            Some(m) => m.2 = after,
            None => merged.push((rel, before, after)),
        }
    }
    Ok(merged)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dir(name: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("chuck-patch-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn hunks_are_placed_by_context() {
        let src = "a\nb\nc\nd\ne\n";
        // the line number is wrong by two: the context still finds it
        let h = vec![(5, vec![" b".into(), "-c".into(), "+C".into(), " d".into()])];
        assert_eq!(apply_hunks(src, &h, "f").unwrap(), "a\nb\nC\nd\ne\n");
        // whitespace differences are forgiven only when nothing matches exactly
        let h = vec![(1, vec!["  b".into(), "-c".into()])];
        assert_eq!(apply_hunks(src, &h, "f").unwrap(), "a\nb\nd\ne\n");
        let h = vec![(1, vec![" zzz".into(), "-c".into()])];
        assert!(apply_hunks(src, &h, "f").unwrap_err().contains("hunk 1 doesn't match"));
        let h = vec![(2, vec!["-x".into()])];
        assert!(apply_hunks("x\ny\nx\ny\nx\n", &h, "f").unwrap_err().contains("more than one place"));
        assert_eq!(apply_hunks("no newline", &[(1, vec!["-no newline".into(), "+fixed".into()])], "f").unwrap(), "fixed");
    }

    #[test]
    fn a_whole_diff_is_planned_or_refused_whole() {
        let d = dir("plan");
        std::fs::write(d.join("a.py"), "def f():\n    return 1\n").unwrap();
        std::fs::write(d.join("old.txt"), "bye\n").unwrap();
        std::fs::write(d.join("win.txt"), "one\r\ntwo\r\n").unwrap();
        let diff = "diff --git a/a.py b/a.py\nindex 1..2 100644\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n def f():\n-    return 1\n+    return 2\n\
                    --- /dev/null\n+++ b/new/n.txt\n@@ -0,0 +1,2 @@\n+hello\n+world\n\
                    --- a/old.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n\
                    --- a/win.txt\n+++ b/win.txt\n@@ -1,2 +1,2 @@\n one\n-two\n+TWO\n";
        let p = plan(&d, diff).unwrap();
        assert_eq!(p.len(), 4);
        assert_eq!(p[0], ("a.py".into(), Some("def f():\n    return 1\n".into()), Some("def f():\n    return 2\n".into())));
        assert_eq!(p[1], ("new/n.txt".into(), None, Some("hello\nworld\n".into())));
        assert_eq!(p[2], ("old.txt".into(), Some("bye\n".into()), None));
        assert_eq!(p[3].2.as_deref(), Some("one\r\nTWO\r\n"), "a Windows file stays one");
        // one bad hunk anywhere: the whole patch is refused
        let bad = format!("{diff}--- a/a.py\n+++ b/a.py\n@@ -9 +9 @@\n-nothing like this\n+x\n");
        assert!(plan(&d, &bad).unwrap_err().to_string().contains("hunk 1 doesn't match"));
        assert!(plan(&d, "--- a/../../etc/passwd\n+++ b/../../etc/passwd\n@@ -1 +1 @@\n-x\n+y\n").is_err());
        assert!(plan(&d, "--- a/.git/config\n+++ b/.git/config\n@@ -0,0 +1 @@\n+x\n").unwrap_err().to_string().contains("git"));
        assert!(plan(&d, "just words").unwrap_err().to_string().contains("no ---/+++"));
        assert!(plan(&d, "--- /dev/null\n+++ b/a.py\n@@ -0,0 +1 @@\n+x\n").unwrap_err().to_string().contains("already exists"));
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn renames_and_two_sections_on_one_file() {
        let d = dir("rename");
        std::fs::write(d.join("x.txt"), "1\n2\n3\n").unwrap();
        let diff = "--- a/x.txt\n+++ b/y.txt\n@@ -1,3 +1,3 @@\n 1\n-2\n+two\n 3\n--- a/y.txt\n+++ b/y.txt\n@@ -3 +3 @@\n-3\n+three\n";
        let p = plan(&d, diff).unwrap();
        assert_eq!(p, vec![("x.txt".into(), Some("1\n2\n3\n".into()), None),
                           ("y.txt".into(), None, Some("1\ntwo\nthree\n".into()))]);
        let _ = std::fs::remove_dir_all(&d);
    }
}
