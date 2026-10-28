//! The folder as a job found it: a copy taken before anything changes, so
//! the job's real result (shell commands change files too, not only the
//! agent's edits) is a diff against it. Links are copied as links, never
//! followed; pipes, sockets and devices are left out.

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
    // the copying on every core
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
                    let _ = copy_file(&s, &d); // unreadable: left out
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
    fn a_snapshot_shows_what_changed_and_never_follows_links_out() {
        let src = tmp("src");
        let secret = tmp("secret").join("key");
        fs::write(&secret, "PRIVATE").unwrap();
        fs::write(src.join("a.py"), "a\n").unwrap();
        fs::write(src.join("gone.py"), "x\n").unwrap();
        let snap = Snapshot::take(&src, &tmp("snap").join("s")).unwrap();
        fs::write(src.join("a.py"), "b\n").unwrap();
        fs::remove_file(src.join("gone.py")).unwrap();
        fs::write(src.join("new.py"), "n\n").unwrap();
        std::os::unix::fs::symlink(&secret, src.join("leak.txt")).unwrap();
        let fifo = src.join("pipe");
        let c = std::ffi::CString::new(fifo.to_str().unwrap()).unwrap();
        unsafe { libc::mkfifo(c.as_ptr(), 0o644) };
        let (d, lines) = snap.diff(100_000).unwrap();
        assert!(d.contains("-a") && d.contains("+b") && d.contains("+n") && d.contains("-x"), "{d}");
        assert!(!d.contains("PRIVATE"), "a link is shown as a link, never read through");
        assert!(!d.contains("pipe"));
        assert!(lines >= 4);
        snap.remove();
    }
}
