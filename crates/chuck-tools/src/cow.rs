//! Copy-on-write workspaces: each worker gets two copies of the folder —
//! `base` (as it was when the worker started, never touched) and `work` (where
//! it builds and tests). Only the Lead brings work home, three-way:
//!
//! * a file only the worker changed  -> takes the worker's version
//! * a file only the user changed    -> stays the user's
//! * a file both changed             -> `git merge-file`; overlapping edits
//!   are never forced: the worker's version goes to .chuck-conflicts/
//! * links are merged as links, never followed; a link pointing out of the
//!   copy is refused; pipes, sockets and devices are ignored.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

pub const SKIP: &[&str] = &[".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".cache", "target", ".next", ".nuxt", ".gradle", "dist", "build", ".tox",
    ".chuck-conflicts"];
pub const MAX_BYTES: u64 = 400 << 20;
pub const MAX_FILES: usize = 50_000;

#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum CowError {
    #[error("{0}")]
    Refused(String),
    #[error("{0}")]
    Io(String),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChangeKind {
    Added,
    Modified,
    Deleted,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct MergeResult {
    pub applied: Vec<String>,
    pub merged: Vec<String>,
    pub conflicts: Vec<String>,
    pub kept: Vec<String>,
    pub skipped: Vec<String>,
    /// Links pointing outside the copy: never brought home.
    pub refused: Vec<String>,
}

/// Regular files and symlinks worth copying (relative paths, sorted).
fn files(root: &Path) -> Result<Vec<String>, CowError> {
    let mut out = Vec::new();
    let walker = walkdir::WalkDir::new(root).follow_links(false).into_iter()
        .filter_entry(|e| e.depth() == 0 || !(e.file_type().is_dir()
            && SKIP.contains(&e.file_name().to_string_lossy().as_ref())));
    for e in walker.flatten() {
        let ft = e.file_type();
        if ft.is_dir() {
            continue;
        }
        if ft.is_symlink() {
            if !e.path().exists() {
                continue; // dangling
            }
        } else if !ft.is_file() {
            continue; // a pipe, socket or device: reading it could hang
        }
        out.push(e.path().strip_prefix(root).unwrap().to_string_lossy().into_owned());
        if out.len() > MAX_FILES {
            return Err(CowError::Refused(format!("the folder has more than {MAX_FILES} files to copy")));
        }
    }
    out.sort();
    Ok(out)
}

fn copy_tree(src: &Path, dst: &Path) -> Result<(), CowError> {
    let mut size = 0u64;
    fs::create_dir_all(dst).map_err(|e| CowError::Io(e.to_string()))?;
    for rel in files(src)? {
        let (s, d) = (src.join(&rel), dst.join(&rel));
        if let Some(p) = d.parent() {
            let _ = fs::create_dir_all(p);
        }
        if s.is_symlink() {
            if let Ok(t) = fs::read_link(&s) {
                let _ = std::os::unix::fs::symlink(t, &d);
            }
            continue;
        }
        size += fs::metadata(&s).map(|m| m.len()).unwrap_or(0);
        if size > MAX_BYTES {
            return Err(CowError::Refused(format!("the folder is over {} MB (not counting dependencies) — too big to copy",
                                                 MAX_BYTES >> 20)));
        }
        let _ = fs::copy(&s, &d); // unreadable: the worker won't have it
    }
    Ok(())
}

fn same_file(a: &Path, b: &Path) -> bool {
    match (fs::read(a), fs::read(b)) {
        (Ok(x), Ok(y)) => x == y,
        _ => false,
    }
}

#[derive(Clone, Debug)]
pub struct Workspace {
    pub source: PathBuf,
    pub dir: PathBuf,
    pub base: PathBuf,
    pub work: PathBuf,
}

impl Workspace {
    pub fn create(source: &Path, store: &Path, name: &str) -> Result<Workspace, CowError> {
        let source = source.canonicalize().map_err(|e| CowError::Io(format!("no such folder {}: {e}", source.display())))?;
        if !source.is_dir() {
            return Err(CowError::Refused(format!("not a folder: {}", source.display())));
        }
        let home = std::env::var("HOME").map(PathBuf::from).ok().and_then(|h| h.canonicalize().ok());
        if source == Path::new("/") || Some(&source) == home.as_ref() {
            return Err(CowError::Refused("a worker needs a project folder, not / or your home".into()));
        }
        let dir = store.join(name);
        if dir.exists() {
            let _ = fs::remove_dir_all(&dir);
        }
        let ws = Workspace { base: dir.join("base"), work: dir.join("work"), source, dir };
        let r = copy_tree(&ws.source, &ws.base).and_then(|_| copy_tree(&ws.base, &ws.work));
        if let Err(e) = r {
            let _ = fs::remove_dir_all(&ws.dir);
            return Err(e);
        }
        Ok(ws)
    }

    /// What the worker changed.
    pub fn changes(&self) -> Result<BTreeMap<String, ChangeKind>, CowError> {
        let base: BTreeSet<String> = files(&self.base)?.into_iter().collect();
        let work: BTreeSet<String> = files(&self.work)?.into_iter().collect();
        let mut out = BTreeMap::new();
        for r in work.difference(&base) {
            out.insert(r.clone(), ChangeKind::Added);
        }
        for r in base.difference(&work) {
            out.insert(r.clone(), ChangeKind::Deleted);
        }
        for r in base.intersection(&work) {
            let (b, w) = (self.base.join(r), self.work.join(r));
            let changed = if b.is_symlink() || w.is_symlink() {
                b.is_symlink() != w.is_symlink() || fs::read_link(&b).ok() != fs::read_link(&w).ok()
            } else {
                !same_file(&b, &w)
            };
            if changed {
                out.insert(r.clone(), ChangeKind::Modified);
            }
        }
        Ok(out)
    }

    /// A unified diff of the worker's changes, and the lines changed.
    pub fn diff(&self, limit: usize) -> Result<(String, usize), CowError> {
        let mut text = String::new();
        let mut lines = 0;
        for (rel, kind) in self.changes()? {
            let read = |p: &Path| if p.is_symlink() {
                fs::read_link(p).ok().map(|t| format!("-> {}\n", t.display()))
            } else {
                fs::read(p).ok().map(|b| String::from_utf8_lossy(&b).into_owned())
            };
            let before = if kind == ChangeKind::Added { None } else { read(&self.base.join(&rel)) };
            let after = if kind == ChangeKind::Deleted { None } else { read(&self.work.join(&rel)) };
            let (d, a, r) = crate::fs::unified(&rel, before.as_deref(), after.as_deref());
            lines += a + r;
            text.push_str(&d);
            if text.len() > limit {
                text.truncate(limit);
                text.push_str("\n… (diff cut)\n");
                break;
            }
        }
        Ok((text, lines))
    }

    /// Bring the changes home, three-way (only through the Lead — see
    /// chuck-runtime). `only`: just these files.
    pub fn merge(&self, only: Option<&[String]>) -> Result<MergeResult, CowError> {
        let mut res = MergeResult::default();
        let cdir = self.source.join(".chuck-conflicts");
        for (rel, kind) in self.changes()? {
            if only.is_some_and(|o| !o.contains(&rel)) {
                res.skipped.push(rel);
                continue;
            }
            let mine = self.source.join(&rel);
            let (base, theirs) = (self.base.join(&rel), self.work.join(&rel));
            if theirs.is_symlink() {
                self.merge_link(&rel, &mine, &theirs, &mut res, &cdir);
                continue;
            }
            let mine_now = mine.exists();
            let mine_same_as_base = mine_now && base.exists() && same_file(&mine, &base);
            let io = |e: std::io::Error| CowError::Io(format!("{rel}: {e}"));
            match kind {
                ChangeKind::Added => {
                    if !mine_now {
                        if let Some(p) = mine.parent() {
                            fs::create_dir_all(p).map_err(io)?;
                        }
                        fs::copy(&theirs, &mine).map_err(io)?;
                        res.applied.push(rel);
                    } else if same_file(&mine, &theirs) {
                        res.kept.push(rel);
                    } else {
                        park(&cdir, &rel, &theirs);
                        res.conflicts.push(rel);
                    }
                }
                ChangeKind::Deleted => {
                    if !mine_now {
                        res.kept.push(rel);
                    } else if mine_same_as_base {
                        fs::remove_file(&mine).map_err(io)?;
                        res.applied.push(rel);
                    } else {
                        res.conflicts.push(rel);
                    }
                }
                ChangeKind::Modified => {
                    if !mine_now {
                        park(&cdir, &rel, &theirs);
                        res.conflicts.push(rel);
                    } else if mine_same_as_base {
                        fs::copy(&theirs, &mine).map_err(io)?;
                        res.applied.push(rel);
                    } else if same_file(&mine, &theirs) {
                        res.kept.push(rel);
                    } else if let Some(merged) = merge3(&mine, &base, &theirs) {
                        fs::write(&mine, merged).map_err(io)?;
                        res.merged.push(rel);
                    } else {
                        park(&cdir, &rel, &theirs);
                        res.conflicts.push(rel);
                    }
                }
            }
        }
        Ok(res)
    }

    fn merge_link(&self, rel: &str, mine: &Path, theirs: &Path, res: &mut MergeResult, cdir: &Path) {
        let Ok(target) = fs::read_link(theirs) else { return };
        let work = self.work.canonicalize().unwrap_or_else(|_| self.work.clone());
        let dest = if target.is_absolute() { None } else { theirs.parent().map(|p| p.join(&target)) }
            .and_then(|p| p.canonicalize().ok());
        let inside = dest.as_ref().is_some_and(|d| d == &work || d.starts_with(&work));
        if !inside {
            res.refused.push(rel.to_string());
            return;
        }
        if mine.is_symlink() && fs::read_link(mine).ok().as_ref() == Some(&target) {
            res.kept.push(rel.to_string());
        } else if !mine.exists() && !mine.is_symlink() {
            if let Some(p) = mine.parent() {
                let _ = fs::create_dir_all(p);
            }
            let _ = std::os::unix::fs::symlink(&target, mine);
            res.applied.push(rel.to_string());
        } else {
            park(cdir, rel, theirs);
            res.conflicts.push(rel.to_string());
        }
    }

    pub fn remove(&self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

fn park(cdir: &Path, rel: &str, theirs: &Path) {
    let dest = cdir.join(rel);
    if let Some(p) = dest.parent() {
        let _ = fs::create_dir_all(p);
    }
    let _ = fs::remove_file(&dest);
    if theirs.is_symlink() {
        if let Ok(t) = fs::read_link(theirs) {
            let _ = std::os::unix::fs::symlink(t, &dest);
        }
    } else {
        let _ = fs::copy(theirs, &dest);
    }
}

fn merge3(mine: &Path, base: &Path, theirs: &Path) -> Option<Vec<u8>> {
    let out = std::process::Command::new("git").args(["merge-file", "-p", "-q"]).arg(mine).arg(base).arg(theirs)
        .output().ok()?;
    if out.status.success() { Some(out.stdout) } else { None }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("chuck-cow-{tag}-{}-{}", std::process::id(),
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()));
        fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn three_way_merge_and_conflicts() {
        let src = tmp("src");
        let store = tmp("store");
        fs::write(src.join("a.py"), "one\ntwo\nthree\nfour\nfive\n").unwrap();
        fs::write(src.join("c.py"), "c1\nc2\nc3\n").unwrap();
        fs::write(src.join("del.py"), "bye\n").unwrap();
        fs::create_dir_all(src.join("node_modules")).unwrap();
        fs::write(src.join("node_modules/big.js"), "x").unwrap();
        let ws = Workspace::create(&src, &store, "t1").unwrap();
        assert!(!ws.work.join("node_modules").exists());
        fs::write(ws.work.join("a.py"), "ONE\ntwo\nthree\nfour\nfive\n").unwrap();
        fs::write(ws.work.join("c.py"), "c1\nAGENT\nc3\n").unwrap();
        fs::write(ws.work.join("new.py"), "new\n").unwrap();
        fs::remove_file(ws.work.join("del.py")).unwrap();
        fs::write(src.join("a.py"), "one\ntwo\nthree\nfour\nFIVE\n").unwrap();
        fs::write(src.join("c.py"), "c1\nYOURS\nc3\n").unwrap();
        let ch = ws.changes().unwrap();
        assert_eq!(ch.len(), 4);
        let r = ws.merge(None).unwrap();
        assert_eq!(fs::read_to_string(src.join("a.py")).unwrap(), "ONE\ntwo\nthree\nfour\nFIVE\n");
        assert_eq!(r.merged, vec!["a.py"]);
        assert_eq!(r.conflicts, vec!["c.py"]);
        assert_eq!(fs::read_to_string(src.join(".chuck-conflicts/c.py")).unwrap(), "c1\nAGENT\nc3\n");
        assert!(src.join("new.py").exists() && !src.join("del.py").exists());
    }

    #[test]
    fn links_are_never_followed_out_and_pipes_are_ignored() {
        let src = tmp("src2");
        let store = tmp("store2");
        let secret = tmp("secret").join("key");
        fs::write(&secret, "PRIVATE").unwrap();
        fs::write(src.join("a.py"), "a\n").unwrap();
        let ws = Workspace::create(&src, &store, "t2").unwrap();
        std::os::unix::fs::symlink(&secret, ws.work.join("leak.txt")).unwrap();
        std::os::unix::fs::symlink("a.py", ws.work.join("alias.py")).unwrap();
        let fifo = ws.work.join("pipe");
        let c = std::ffi::CString::new(fifo.to_str().unwrap()).unwrap();
        unsafe { libc::mkfifo(c.as_ptr(), 0o644) };
        assert!(!ws.changes().unwrap().contains_key("pipe"));
        let r = ws.merge(None).unwrap();
        assert_eq!(r.refused, vec!["leak.txt"]);
        assert!(!src.join("leak.txt").exists());
        assert_eq!(fs::read_link(src.join("alias.py")).unwrap(), PathBuf::from("a.py"));
    }

    #[test]
    fn refuses_home_and_root() {
        let store = tmp("store3");
        assert!(Workspace::create(Path::new("/"), &store, "x").is_err());
    }
}
