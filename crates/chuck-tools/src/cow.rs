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
/// Dependencies and history a worker needs but must not change: never
/// copied, lent instead (see [`Workspace::lend`]).
pub const LENT: &[&str] = &["node_modules", ".venv", "venv", ".git"];
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
            if LENT.contains(&e.file_name().to_string_lossy().as_ref()) {
                continue; // a lent folder: the project's own, never a change of the worker's
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

/// Give `p` the modified time `t` (its access time untouched).
fn set_mtime(p: &Path, t: std::time::SystemTime) {
    use std::os::unix::ffi::OsStrExt;
    let Ok(d) = t.duration_since(std::time::UNIX_EPOCH) else { return };
    let Ok(c) = std::ffi::CString::new(p.as_os_str().as_bytes()) else { return };
    let times = [libc::timespec { tv_sec: 0, tv_nsec: libc::UTIME_OMIT },
                 libc::timespec { tv_sec: d.as_secs() as libc::time_t, tv_nsec: d.subsec_nanos() as _ }];
    unsafe { libc::utimensat(libc::AT_FDCWD, c.as_ptr(), times.as_ptr(), 0) };
}

/// One file copied with its modified time kept, so an untouched copy is
/// told apart from a changed one without reading either (see `same_file`).
/// Not for a file changed in the last moments: the clock that stamps files
/// is coarse, so a second change within the same tick would keep the same
/// time — such a copy keeps its own time, and is compared by its contents.
fn copy_file(s: &Path, d: &Path) -> std::io::Result<u64> {
    let n = fs::copy(s, d)?;
    if let Ok(t) = fs::metadata(s).and_then(|m| m.modified()) {
        if t.elapsed().is_ok_and(|age| age > std::time::Duration::from_secs(2)) {
            set_mtime(d, t);
        }
    }
    Ok(n)
}

fn copy_tree(src: &Path, dst: &Path) -> Result<(), CowError> {
    fs::create_dir_all(dst).map_err(|e| CowError::Io(e.to_string()))?;
    let list = files(src)?;
    // too big is said before anything is copied
    let size: u64 = list.iter().filter_map(|r| fs::symlink_metadata(src.join(r)).ok())
        .filter(|m| m.is_file()).map(|m| m.len()).sum();
    if size > MAX_BYTES {
        return Err(CowError::Refused(format!("the folder is over {} MB (not counting dependencies) — too big to copy",
                                             MAX_BYTES >> 20)));
    }
    let dirs: BTreeSet<PathBuf> = list.iter().filter_map(|r| dst.join(r).parent().map(Path::to_path_buf)).collect();
    for d in dirs {
        let _ = fs::create_dir_all(d);
    }
    // the top folder's time marks when copying began (see `kept_before`): set again once it's done, as
    // files going into it move it on
    let began = std::time::SystemTime::now();
    // the copying on every core: a worker starts in a fraction of the time
    let next = std::sync::atomic::AtomicUsize::new(0);
    let threads = std::thread::available_parallelism().map_or(4, |n| n.get()).clamp(1, 8).min(list.len().max(1));
    std::thread::scope(|sc| {
        for _ in 0..threads {
            sc.spawn(|| loop {
                let i = next.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                let Some(rel) = list.get(i) else { break };
                let (s, d) = (src.join(rel), dst.join(rel));
                if s.is_symlink() {
                    if let Ok(t) = fs::read_link(&s) {
                        let _ = std::os::unix::fs::symlink(t, &d);
                    }
                } else {
                    let _ = copy_file(&s, &d); // unreadable: the worker won't have it
                }
            });
        }
    });
    set_mtime(dst, began);
    Ok(())
}

/// Before when a modified time is one a copy kept from its original (see
/// `copy_file`): copying began when the copy's folders were made, so its
/// top folder's time; a kept time is older than that by the margin.
fn kept_before(copy: &Path) -> Option<std::time::SystemTime> {
    fs::metadata(copy).and_then(|m| m.modified()).ok()?.checked_sub(std::time::Duration::from_secs(2))
}

/// The same contents? A copy whose size and kept modified time are still
/// its original's is (every edit moves the time on past the copy); anything
/// else is read and compared.
fn same_file(a: &Path, b: &Path, kept: Option<std::time::SystemTime>) -> bool {
    if let (Ok(x), Ok(y)) = (fs::metadata(a), fs::metadata(b)) {
        if x.len() != y.len() {
            return false; // cheap, and the common case for a changed file
        }
        if let (Ok(p), Ok(q), Some(k)) = (x.modified(), y.modified(), kept) {
            if p == q && p < k {
                return true;
            }
        }
    }
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

    /// Lend the copy what it can't work without and must not change: the
    /// project's dependencies (`node_modules`, `.venv`, `venv`, at the top
    /// and one folder down) and, when `git` (only where a jail keeps it
    /// read-only), its `.git`. Each appears in the copy as a link to the
    /// real one; diffs and merges pass the links by. Returns the real
    /// folders, for the jail to show read-only.
    pub fn lend(&self, git: bool) -> Vec<PathBuf> {
        let mut out = Vec::new();
        let mut places = vec![PathBuf::new()];
        if let Ok(rd) = fs::read_dir(&self.source) {
            let mut subs: Vec<PathBuf> = rd.flatten()
                .filter(|e| e.file_type().is_ok_and(|t| t.is_dir()) && !SKIP.contains(&e.file_name().to_string_lossy().as_ref())
                        && !e.file_name().to_string_lossy().starts_with('.'))
                .map(|e| PathBuf::from(e.file_name())).collect();
            subs.sort();
            places.extend(subs);
        }
        for place in places {
            for name in LENT {
                if *name == ".git" && (!git || !place.as_os_str().is_empty()) {
                    continue;
                }
                let real = self.source.join(&place).join(name);
                let link = self.work.join(&place).join(name);
                if !real.is_dir() || real.is_symlink() || !link.parent().is_some_and(Path::is_dir)
                    || link.symlink_metadata().is_ok()
                {
                    continue;
                }
                if std::os::unix::fs::symlink(&real, &link).is_ok() {
                    out.push(real);
                }
            }
        }
        out
    }

    /// What the worker changed.
    pub fn changes(&self) -> Result<BTreeMap<String, ChangeKind>, CowError> {
        changes_between(&self.base, &self.work)
    }

    /// A unified diff of the worker's changes, and the lines changed.
    pub fn diff(&self, limit: usize) -> Result<(String, usize), CowError> {
        diff_between(&self.base, &self.work, limit)
    }

    /// Bring the changes home, three-way (only through the Lead — see
    /// chuck-runtime). `only`: just these files.
    pub fn merge(&self, only: Option<&[String]>) -> Result<MergeResult, CowError> {
        let mut res = MergeResult::default();
        let cdir = self.source.join(".chuck-conflicts");
        let kept = kept_before(&self.base);
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
            // never write through a link of yours that leads out of the project
            if self.leads_out(&mine) {
                res.refused.push(rel);
                continue;
            }
            let mine_now = mine.exists();
            let mine_same_as_base = mine_now && base.exists() && same_file(&mine, &base, kept);
            let io = |e: std::io::Error| CowError::Io(format!("{rel}: {e}"));
            match kind {
                ChangeKind::Added => {
                    if !mine_now {
                        if let Some(p) = mine.parent() {
                            fs::create_dir_all(p).map_err(io)?;
                        }
                        fs::copy(&theirs, &mine).map_err(io)?;
                        res.applied.push(rel);
                    } else if same_file(&mine, &theirs, kept) {
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
                    } else if same_file(&mine, &theirs, kept) {
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

    /// The user's path would put the write outside the project: a link to
    /// elsewhere on the file itself or on a folder above it.
    fn leads_out(&self, mine: &Path) -> bool {
        if mine.is_symlink() {
            // the worker's version is a plain file: writing it would follow the link
            return mine.canonicalize().map_or(true, |c| !c.starts_with(&self.source));
        }
        let mut dir = mine.parent();
        while let Some(d) = dir {
            if d == self.source {
                return false;
            }
            if d.exists() {
                return d.canonicalize().map_or(true, |c| !c.starts_with(&self.source));
            }
            dir = d.parent();
        }
        true
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

    /// Bring files merged into the project since this copy was made into
    /// it — the ones the worker hasn't changed itself (its own stay its own,
    /// for its merge to bring together). Taken into the base too, so they
    /// aren't counted as the worker's changes. -> the files taken in.
    pub fn take_in(&self, rels: &[String]) -> Vec<String> {
        let mine = self.changes().unwrap_or_default();
        let mut out = Vec::new();
        for rel in rels {
            if mine.contains_key(rel) || crate::fs::safe(&self.work, rel).is_err() {
                continue;
            }
            let src = self.source.join(rel);
            let ok = if src.is_file() && !src.is_symlink() {
                [&self.base, &self.work].iter().all(|d| {
                    let to = d.join(rel);
                    to.parent().is_some_and(|p| fs::create_dir_all(p).is_ok()) && fs::copy(&src, &to).is_ok()
                })
            } else if !src.exists() {
                // deleted in the project: gone here too
                [&self.base, &self.work].iter().for_each(|d| {
                    let _ = fs::remove_file(d.join(rel));
                });
                true
            } else {
                false
            };
            if ok {
                out.push(rel.clone());
            }
        }
        out
    }

    pub fn remove(&self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

/// What changed from `base` to `other`.
pub fn changes_between(base_dir: &Path, other: &Path) -> Result<BTreeMap<String, ChangeKind>, CowError> {
    let kept = kept_before(base_dir);
    let base: BTreeSet<String> = files(base_dir)?.into_iter().collect();
    let work: BTreeSet<String> = files(other)?.into_iter().collect();
    let mut out = BTreeMap::new();
    for r in work.difference(&base) {
        out.insert(r.clone(), ChangeKind::Added);
    }
    for r in base.difference(&work) {
        out.insert(r.clone(), ChangeKind::Deleted);
    }
    for r in base.intersection(&work) {
        let (b, w) = (base_dir.join(r), other.join(r));
        let changed = if b.is_symlink() || w.is_symlink() {
            b.is_symlink() != w.is_symlink() || fs::read_link(&b).ok() != fs::read_link(&w).ok()
        } else {
            !same_file(&b, &w, kept)
        };
        if changed {
            out.insert(r.clone(), ChangeKind::Modified);
        }
    }
    Ok(out)
}

/// A unified diff from `base` to `other` (cut past `limit` bytes), and the
/// number of lines added plus removed (always the full count).
pub fn diff_between(base: &Path, other: &Path, limit: usize) -> Result<(String, usize), CowError> {
    let mut text = String::new();
    let mut lines = 0;
    let mut cut = false;
    for (rel, kind) in changes_between(base, other)? {
        let read = |p: &Path| if p.is_symlink() {
            fs::read_link(p).ok().map(|t| format!("-> {}\n", t.display()))
        } else {
            fs::read(p).ok().map(|b| String::from_utf8_lossy(&b).into_owned())
        };
        let before = if kind == ChangeKind::Added { None } else { read(&base.join(&rel)) };
        let after = if kind == ChangeKind::Deleted { None } else { read(&other.join(&rel)) };
        let (d, a, r) = crate::fs::unified(&rel, before.as_deref(), after.as_deref());
        lines += a + r;
        if !cut {
            text.push_str(&d);
            if text.len() > limit {
                let mut at = limit;
                while !text.is_char_boundary(at) {
                    at -= 1;
                }
                text.truncate(at);
                text.push_str("\n… (diff cut)\n");
                cut = true;
            }
        }
    }
    Ok((text, lines))
}

/// The folder as it was when a job started, to diff the real result against
/// (shell commands change files too, not only the agents' edits).
#[derive(Clone, Debug)]
pub struct Snapshot {
    pub source: PathBuf,
    pub base: PathBuf,
}

impl Snapshot {
    pub fn take(source: &Path, dir: &Path) -> Result<Snapshot, CowError> {
        let source = source.canonicalize().map_err(|e| CowError::Io(format!("no such folder {}: {e}", source.display())))?;
        if dir.exists() {
            let _ = fs::remove_dir_all(dir);
        }
        if let Err(e) = copy_tree(&source, dir) {
            let _ = fs::remove_dir_all(dir);
            return Err(e);
        }
        Ok(Snapshot { source, base: dir.to_path_buf() })
    }

    /// Everything that changed in the folder since the snapshot.
    pub fn diff(&self, limit: usize) -> Result<(String, usize), CowError> {
        diff_between(&self.base, &self.source, limit)
    }

    pub fn remove(&self) {
        let _ = fs::remove_dir_all(&self.base);
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
    fn a_merge_never_writes_through_your_links_out_of_the_project() {
        let outside = tmp("outside");
        fs::write(outside.join("secret.txt"), "keep\n").unwrap();
        let src = tmp("src-links");
        let store = tmp("store-links");
        std::os::unix::fs::symlink(&outside, src.join("ext")).unwrap();
        std::os::unix::fs::symlink(outside.join("secret.txt"), src.join("s.txt")).unwrap();
        let ws = Workspace::create(&src, &store, "t1").unwrap();
        // the worker replaces the file link with a plain file, and adds a file "under" the folder link
        fs::remove_file(ws.work.join("s.txt")).unwrap();
        fs::write(ws.work.join("s.txt"), "pwned\n").unwrap();
        fs::remove_file(ws.work.join("ext")).unwrap();
        fs::create_dir_all(ws.work.join("ext")).unwrap();
        fs::write(ws.work.join("ext/new.txt"), "pwned\n").unwrap();
        let r = ws.merge(None).unwrap();
        assert_eq!(fs::read_to_string(outside.join("secret.txt")).unwrap(), "keep\n");
        assert!(!outside.join("new.txt").exists());
        assert!(r.refused.contains(&"s.txt".to_string()) && r.refused.contains(&"ext/new.txt".to_string()), "{r:?}");
        for d in [outside, src, store] {
            let _ = fs::remove_dir_all(d);
        }
    }

    #[test]
    fn a_running_copy_takes_in_what_was_merged_but_keeps_its_own() {
        let src = tmp("takein-src");
        let store = tmp("takein-store");
        fs::write(src.join("mine.js"), "1\n").unwrap();
        let ws = Workspace::create(&src, &store, "t2").unwrap();
        fs::write(ws.work.join("mine.js"), "worker\n").unwrap();
        // meanwhile another task merged: a new file, a change to mine.js too
        fs::write(src.join("index.html"), "<game>\n").unwrap();
        fs::write(src.join("mine.js"), "other\n").unwrap();
        let got = ws.take_in(&["index.html".into(), "mine.js".into(), "../escape".into()]);
        assert_eq!(got, vec!["index.html"]);
        assert_eq!(fs::read_to_string(ws.work.join("index.html")).unwrap(), "<game>\n");
        assert_eq!(fs::read_to_string(ws.work.join("mine.js")).unwrap(), "worker\n", "its own change stays");
        let ch = ws.changes().unwrap();
        assert!(!ch.contains_key("index.html"), "what it was given isn't its change");
        assert!(ch.contains_key("mine.js"));
        for d in [src, store] {
            let _ = fs::remove_dir_all(d);
        }
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
