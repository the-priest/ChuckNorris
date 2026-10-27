//! Snapshots of the whole working folder, so anything can be undone — the
//! window's shadow-git checkpoints, same store and same keys
//! (~/.local/share/chucknorris/checkpoints/<sha1(folder)[:16]>).
//!
//! Before a step that acts, the folder is committed into a private git
//! repository in Chuck's data folder, with the folder as its work tree. Your
//! own .git is never read or written; a folder that isn't a repo works the
//! same. `restore` puts every file back as it was (changed ones restored,
//! new ones removed, deleted ones back) after first snapshotting the
//! present, so a rewind can itself be undone. .gitignore is honoured;
//! dependency and build folders are left out.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

const EXCLUDE: &str = "node_modules/\n.venv/\nvenv/\n__pycache__/\n*.pyc\n.mypy_cache/\n.pytest_cache/\n.ruff_cache/\n\
.cache/\ntarget/\n.next/\n.nuxt/\n.gradle/\n*.iso\n*.img\n*.qcow2\n";

#[derive(Debug, Clone, PartialEq, thiserror::Error)]
#[error("{0}")]
pub struct CheckpointError(pub String);

/// SHA-1, for the store's folder keys (the window's: sha1 of the path).
pub fn sha1_hex(data: &[u8]) -> String {
    let mut h: [u32; 5] = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0];
    let mut msg = data.to_vec();
    let bits = (data.len() as u64).wrapping_mul(8);
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bits.to_be_bytes());
    for chunk in msg.chunks(64) {
        let mut w = [0u32; 80];
        for i in 0..16 {
            w[i] = u32::from_be_bytes([chunk[4 * i], chunk[4 * i + 1], chunk[4 * i + 2], chunk[4 * i + 3]]);
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }
        let (mut a, mut b, mut c, mut d, mut e) = (h[0], h[1], h[2], h[3], h[4]);
        for (i, wi) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | (!b & d), 0x5A827999),
                20..=39 => (b ^ c ^ d, 0x6ED9EBA1),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1BBCDC),
                _ => (b ^ c ^ d, 0xCA62C1D6),
            };
            let t = a.rotate_left(5).wrapping_add(f).wrapping_add(e).wrapping_add(k).wrapping_add(*wi);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = t;
        }
        for (x, y) in h.iter_mut().zip([a, b, c, d, e]) {
            *x = x.wrapping_add(y);
        }
    }
    h.iter().map(|x| format!("{x:08x}")).collect()
}

pub fn default_store() -> PathBuf {
    crate::ledger::data_dir().join("checkpoints")
}

/// One checkpoint: its commit, when, and what it was taken before.
#[derive(Clone, Debug, PartialEq)]
pub struct Point {
    pub sha: String,
    pub at: i64,
    pub label: String,
}

pub struct Shadow {
    pub work: PathBuf,
    pub git_dir: PathBuf,
    timeout: Duration,
}

impl Shadow {
    pub fn new(work: &Path, store: &Path) -> Result<Shadow, CheckpointError> {
        let work = work.canonicalize().map_err(|e| CheckpointError(format!("{}: {e}", work.display())))?;
        let home = std::env::var_os("HOME").map(PathBuf::from).and_then(|h| h.canonicalize().ok());
        if work == Path::new("/") || Some(&work) == home.as_ref() {
            return Err(CheckpointError("checkpoints need a project folder, not / or your home".into()));
        }
        let key = sha1_hex(work.to_string_lossy().as_bytes())[..16].to_string();
        Ok(Shadow { git_dir: store.join(key), work, timeout: Duration::from_secs(20) })
    }

    fn git_raw(&self, args: &[&str], check: bool) -> Result<Vec<u8>, CheckpointError> {
        let mut c = Command::new("git");
        for (k, _) in std::env::vars_os() {
            if k.to_string_lossy().starts_with("GIT_") {
                c.env_remove(k);
            }
        }
        c.env("GIT_DIR", &self.git_dir).env("GIT_WORK_TREE", &self.work)
            .env("GIT_AUTHOR_NAME", "Chuck").env("GIT_AUTHOR_EMAIL", "chuck@localhost")
            .env("GIT_COMMITTER_NAME", "Chuck").env("GIT_COMMITTER_EMAIL", "chuck@localhost")
            .env("GIT_CONFIG_NOSYSTEM", "1").env("GIT_TERMINAL_PROMPT", "0")
            .args(["-c", "core.autocrlf=false", "-c", "core.quotepath=off", "-c", "gc.auto=200",
                   "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"])
            .args(args).current_dir(&self.work)
            .stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
        let child = c.spawn().map_err(|e| CheckpointError(if e.kind() == std::io::ErrorKind::NotFound {
            "git isn't installed".into() } else { e.to_string() }))?;
        let pid = child.id();
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let _ = tx.send(child.wait_with_output());
        });
        let out = match rx.recv_timeout(self.timeout) {
            Ok(Ok(o)) => o,
            Ok(Err(e)) => return Err(CheckpointError(e.to_string())),
            Err(_) => {
                unsafe {
                    libc::kill(pid as i32, libc::SIGKILL);
                }
                return Err(CheckpointError(format!("the folder is too big to snapshot in {}s", self.timeout.as_secs())));
            }
        };
        if check && !out.status.success() {
            let msg = String::from_utf8_lossy(if out.stderr.is_empty() { &out.stdout } else { &out.stderr });
            let msg = msg.trim();
            let tail: String = msg.chars().rev().take(300).collect::<Vec<_>>().into_iter().rev().collect();
            return Err(CheckpointError(if tail.is_empty() { format!("git {} failed", args[0]) } else { tail }));
        }
        Ok(out.stdout)
    }

    fn git(&self, args: &[&str], check: bool) -> Result<String, CheckpointError> {
        self.git_raw(args, check).map(|b| String::from_utf8_lossy(&b).into_owned())
    }

    fn names(&self, args: &[&str]) -> Vec<String> {
        let mut a = args.to_vec();
        a.push("-z");
        self.git(&a, false).map(|o| o.split('\0').filter(|p| !p.is_empty()).map(String::from).collect())
            .unwrap_or_default()
    }

    /// A git killed by a timeout leaves index.lock behind; only Chuck uses
    /// this repository, one thing at a time, so a lock found here is stale.
    fn unlock(&self) {
        let _ = std::fs::remove_file(self.git_dir.join("index.lock"));
    }

    fn ensure(&self) -> Result<(), CheckpointError> {
        if self.git_dir.join("HEAD").exists() {
            return Ok(());
        }
        std::fs::create_dir_all(&self.git_dir).map_err(|e| CheckpointError(e.to_string()))?;
        self.git(&["init", "--quiet"], true)?;
        let _ = std::fs::create_dir_all(self.git_dir.join("info"));
        let _ = std::fs::write(self.git_dir.join("info").join("exclude"), EXCLUDE);
        let _ = std::fs::write(self.git_dir.join("chuck-folder"), format!("{}\n", self.work.display()));
        Ok(())
    }

    fn manifest_path(&self, sha: &str) -> PathBuf {
        self.git_dir.join("chuck-manifest").join(format!("{sha}.json"))
    }

    /// What a snapshot can't hold: the ignored files and the empty folders
    /// that existed then. A restore never deletes or removes those.
    fn record_manifest(&self, sha: &str) {
        let ignored = self.names(&["ls-files", "--others", "--ignored", "--exclude-standard", "--directory"]);
        let mut empty = Vec::new();
        let mut it = walkdir::WalkDir::new(&self.work).follow_links(false).into_iter();
        let mut n = 0;
        while let Some(Ok(e)) = it.next() {
            if !e.file_type().is_dir() {
                continue;
            }
            if e.file_name() == ".git" {
                it.skip_current_dir();
                continue;
            }
            n += 1;
            if n > 20_000 {
                break;
            }
            if std::fs::read_dir(e.path()).map(|mut d| d.next().is_none()).unwrap_or(false) {
                let rel = e.path().strip_prefix(&self.work).unwrap_or(e.path()).display().to_string();
                empty.push(if rel.is_empty() { ".".into() } else { rel });
            }
        }
        let p = self.manifest_path(sha);
        if let Some(d) = p.parent() {
            let _ = std::fs::create_dir_all(d);
        }
        let _ = std::fs::write(p, serde_json::json!({"ignored": ignored, "empty": empty}).to_string());
    }

    fn manifest(&self, sha: &str) -> (Vec<String>, Vec<String>) {
        let v: serde_json::Value = std::fs::read_to_string(self.manifest_path(sha)).ok()
            .and_then(|t| serde_json::from_str(&t).ok()).unwrap_or(serde_json::Value::Null);
        let list = |k: &str| v.get(k).and_then(|x| x.as_array()).map(|a| a.iter()
            .filter_map(|s| s.as_str().map(String::from)).collect()).unwrap_or_default();
        (list("ignored"), list("empty"))
    }

    /// Folders that are git repositories of their own: their insides aren't
    /// snapshotted (git keeps only a pointer to them).
    pub fn nested(&self) -> Vec<String> {
        self.git(&["ls-files", "-s", "-z"], false).unwrap_or_default().split('\0')
            .filter(|l| l.starts_with("160000 ")).filter_map(|l| l.split_once('\t').map(|(_, p)| p.to_string()))
            .collect()
    }

    /// Commit the folder as it is now → the commit id.
    pub fn snapshot(&self, label: &str) -> Result<String, CheckpointError> {
        self.ensure()?;
        self.unlock();
        let _ = self.git(&["add", "-A", "--ignore-errors"], false);
        let label: String = label.chars().take(200).collect();
        self.git(&["commit", "--quiet", "--allow-empty", "--no-verify", "-m",
                   if label.trim().is_empty() { "step" } else { &label }], true)?;
        let sha = self.git(&["rev-parse", "HEAD"], true)?.trim().to_string();
        self.record_manifest(&sha);
        let _ = std::fs::write(self.git_dir.join("chuck-folder"), format!("{}\n", self.work.display())); // last used
        Ok(sha)
    }

    /// Paths that differ between a snapshot and the folder now.
    pub fn changed_since(&self, sha: &str) -> Vec<String> {
        self.unlock();
        let _ = self.git(&["add", "-A", "--ignore-errors"], false);
        self.names(&["diff", "--cached", "--name-only", "--no-renames", sha])
    }

    /// What changed since a snapshot, as a unified diff (at most `max`
    /// bytes; binary files are named, not shown).
    pub fn diff_since(&self, sha: &str, max: usize) -> String {
        self.unlock();
        let _ = self.git(&["add", "-A", "--ignore-errors"], false);
        let out = self.git_raw(&["diff", "--cached", "--no-renames", "--no-color", "--no-ext-diff", sha], false)
            .unwrap_or_default();
        let text = String::from_utf8_lossy(&out);
        if text.len() <= max {
            return text.into_owned();
        }
        let mut cut = max;
        while !text.is_char_boundary(cut) {
            cut -= 1;
        }
        format!("{}\n… ({} more bytes of diff)", &text[..cut], text.len() - cut)
    }

    /// The checkpoints, newest first.
    pub fn points(&self, n: usize) -> Vec<Point> {
        if !self.git_dir.join("HEAD").exists() {
            return Vec::new();
        }
        let n = n.to_string();
        self.git(&["log", "-n", &n, "--format=%H%x09%ct%x09%s"], false).unwrap_or_default().lines()
            .filter_map(|l| {
                let mut p = l.splitn(3, '\t');
                Some(Point { sha: p.next()?.to_string(), at: p.next()?.parse().ok()?, label: p.next().unwrap_or("").into() })
            }).collect()
    }

    /// Make the folder what it was at `sha` → (the snapshot taken just
    /// before, so the rewind can be undone; the paths it changed). Files that
    /// were ignored then (a .env, a data folder) are never deleted; folders
    /// that existed then are never removed.
    pub fn restore(&self, sha: &str) -> Result<(String, Vec<String>), CheckpointError> {
        self.git(&["cat-file", "-e", &format!("{sha}^{{commit}}")], true)
            .map_err(|_| CheckpointError(format!("no checkpoint {sha}")))?;
        let before = self.snapshot("before rewind")?;
        let changed = self.names(&["diff", "--name-only", "--no-renames", sha, &before]);
        let added = self.names(&["diff", "--name-only", "--no-renames", "--diff-filter=A", sha, &before]);
        let (ignored, keep_dirs) = self.manifest(sha);
        let ign: Vec<String> = ignored.iter().map(|p| p.trim_end_matches('/').to_string()).collect();
        let was_ignored = |rel: &str| ign.iter().any(|p| rel == p || rel.starts_with(&format!("{p}/")));
        let part = |e: CheckpointError| CheckpointError(format!("{e} (the folder may be part-way back; rewind to \
            {before} to return to how it was)"));
        // put back what changed or was deleted, then remove what's new
        if !self.git(&["ls-tree", "-r", "--name-only", sha], false).unwrap_or_default().trim().is_empty() {
            self.unlock();
            self.git(&["checkout", sha, "--", "."], true).map_err(part)?;
        }
        for rel in &added {
            if was_ignored(rel) {
                continue;
            }
            let p = self.work.join(rel);
            if (p.is_file() || p.is_symlink()) && std::fs::remove_file(&p).is_err() {
                continue;
            }
            let mut d = p.parent().map(Path::to_path_buf);
            while let Some(dir) = d {
                if dir == self.work || !dir.is_dir() {
                    break;
                }
                let rel_dir = dir.strip_prefix(&self.work).unwrap_or(&dir).display().to_string();
                if keep_dirs.contains(&rel_dir) || std::fs::remove_dir(&dir).is_err() {
                    break; // kept, or not empty
                }
                d = dir.parent().map(Path::to_path_buf);
            }
        }
        let _ = self.git(&["reset", "--quiet", "--soft", &before], false);
        Ok((before, changed.into_iter().filter(|c| !added.contains(c) || !was_ignored(c)).collect()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sha1_is_sha1_and_keys_match_the_window() {
        assert_eq!(sha1_hex(b"abc"), "a9993e364706816aba3e25717850c26c9cd0d89d");
        assert_eq!(sha1_hex(b""), "da39a3ee5e6b4b0d3255bfef95601890afd80709");
        assert_eq!(&sha1_hex(b"/home/x")[..16], "3c4d7c3751574d3e"); // hashlib.sha1('/home/x')
        assert_eq!(sha1_hex(&[b'a'; 1000]).len(), 40);
    }

    #[test]
    fn rewind_puts_everything_back_and_is_itself_undoable() {
        if Command::new("git").arg("--version").output().is_err() {
            return;
        }
        let d = std::env::temp_dir().join(format!("chuck-ckpt-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        let (proj, store) = (d.join("proj"), d.join("store"));
        std::fs::create_dir_all(proj.join("src")).unwrap();
        std::fs::create_dir_all(proj.join("keep-empty")).unwrap();
        std::fs::write(proj.join("src/a.rs"), "one\n").unwrap();
        std::fs::write(proj.join("del.txt"), "bye\n").unwrap();
        std::fs::write(proj.join(".gitignore"), ".env\n").unwrap();
        std::fs::write(proj.join(".env"), "SECRET=1\n").unwrap();
        let sh = Shadow::new(&proj, &store).unwrap();
        let cp = sh.snapshot("before the job").unwrap();
        // the job: an edit, a deletion, new files in a new folder, a changed .env
        std::fs::write(proj.join("src/a.rs"), "two\n").unwrap();
        std::fs::remove_file(proj.join("del.txt")).unwrap();
        std::fs::create_dir_all(proj.join("gen/deep")).unwrap();
        std::fs::write(proj.join("gen/deep/x.rs"), "new\n").unwrap();
        std::fs::write(proj.join(".env"), "SECRET=2\n").unwrap();
        assert_eq!(sh.changed_since(&cp).len(), 3);
        let (undo, changed) = sh.restore(&cp).unwrap();
        assert_eq!(std::fs::read_to_string(proj.join("src/a.rs")).unwrap(), "one\n");
        assert_eq!(std::fs::read_to_string(proj.join("del.txt")).unwrap(), "bye\n");
        assert!(!proj.join("gen").exists(), "new files and the folders they made are gone");
        assert!(proj.join("keep-empty").is_dir(), "a folder that existed then is kept");
        assert_eq!(std::fs::read_to_string(proj.join(".env")).unwrap(), "SECRET=2\n", "ignored files are never touched");
        assert!(changed.contains(&"src/a.rs".to_string()));
        // and the rewind itself can be undone
        sh.restore(&undo).unwrap();
        assert_eq!(std::fs::read_to_string(proj.join("src/a.rs")).unwrap(), "two\n");
        assert!(proj.join("gen/deep/x.rs").exists());
        let pts = sh.points(10);
        assert!(pts.iter().any(|p| p.label == "before the job"));
        assert!(!proj.join(".git").exists(), "your own folder never gets a .git from this");
        let _ = std::fs::remove_dir_all(&d);
    }
}
