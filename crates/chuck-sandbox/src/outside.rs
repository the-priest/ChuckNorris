//! Does a command reach outside the working folder? The paths it names —
//! absolute, `~/…`, `../…`, a `cd` out of the folder — that aren't the
//! system (read-only to every command anyway), the temporary folders, your
//! toolchains or build caches. In "ask outside" mode such a command waits
//! for your yes; the jail hides the rest of your home either way, so a path
//! this misses is still out of reach.

use crate::jail::{home, CACHE_DIRS, TOOLCHAIN_DIRS};
use std::path::{Component, Path, PathBuf};

/// Places any command may name without asking.
const SYSTEM: &[&str] = &["/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc", "/opt", "/proc", "/sys",
    "/dev", "/tmp", "/var/tmp", "/run", "/snap", "/nix", "/var/lib/flatpak", "/var/cache", "/var/log", "/srv/http"];

/// Words of a shell command, quotes and escapes resolved; operators split off.
pub fn words(cmd: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut have = false;
    let mut it = cmd.chars().peekable();
    let push = |cur: &mut String, have: &mut bool, out: &mut Vec<String>| {
        if *have {
            out.push(std::mem::take(cur));
            *have = false;
        }
    };
    while let Some(c) = it.next() {
        match c {
            '\'' => {
                have = true;
                for d in it.by_ref() {
                    if d == '\'' {
                        break;
                    }
                    cur.push(d);
                }
            }
            '"' => {
                have = true;
                while let Some(d) = it.next() {
                    match d {
                        '"' => break,
                        '\\' => {
                            if let Some(e) = it.next() {
                                cur.push(e);
                            }
                        }
                        _ => cur.push(d),
                    }
                }
            }
            '\\' => {
                have = true;
                if let Some(e) = it.next() {
                    if e != '\n' {
                        cur.push(e);
                    }
                }
            }
            c if c.is_whitespace() || matches!(c, ';' | '|' | '&' | '(' | ')' | '<' | '>' | '`') => {
                push(&mut cur, &mut have, &mut out);
            }
            _ => {
                have = true;
                cur.push(c);
            }
        }
    }
    push(&mut cur, &mut have, &mut out);
    out
}

/// `a/./b/../c` → `a/c`, without touching the disk.
fn tidy(p: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for c in p.components() {
        match c {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => out.push(other.as_os_str()),
        }
    }
    out
}

fn allowed(p: &Path, root: &Path, home: &Path) -> bool {
    if p.starts_with(root) {
        return true;
    }
    if SYSTEM.iter().any(|s| p.starts_with(s)) || p == Path::new("/dev/null") {
        // /home lives under none of these
        return true;
    }
    TOOLCHAIN_DIRS.iter().chain(CACHE_DIRS).any(|d| p.starts_with(home.join(d)))
        || p.starts_with(home.join(".local/share/chucknorris/sandbox-cache"))
}

/// The paths outside `root` that `cmd` names (each once), resolved against
/// `root`. Empty: it stays in the folder.
pub fn outside_paths(cmd: &str, root: &Path) -> Vec<PathBuf> {
    let root = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());
    let h = home();
    let mut out: Vec<PathBuf> = Vec::new();
    for w in words(cmd) {
        // NAME=/path and --opt=/path name a path too
        let w = match w.split_once('=') {
            Some((k, v)) if !k.is_empty() && (k.starts_with('-') || k.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')) => v.to_string(),
            _ => w,
        };
        if w.contains("://") || w.starts_with('-') || w.contains('$') || w.contains('*') && !w.starts_with('/') {
            continue;
        }
        let p = if let Some(rest) = w.strip_prefix("~/") {
            h.join(rest)
        } else if w == "~" {
            h.clone()
        } else if w.starts_with('/') {
            PathBuf::from(&w)
        } else if w == ".." || w.starts_with("../") || w.contains("/../") {
            root.join(&w)
        } else {
            continue;
        };
        // a glob's fixed part
        let p = PathBuf::from(p.to_string_lossy().split(['*', '?', '[']).next().unwrap_or(""));
        let p = tidy(&p);
        if p.as_os_str().is_empty() || allowed(&p, &root, &h) {
            continue;
        }
        if !out.contains(&p) {
            out.push(p);
        }
    }
    out
}

/// A path an agent wrote (absolute, `~/…`, or relative to `root`), resolved
/// without touching the disk. None: it doesn't leave the folder by its
/// spelling (a plain relative path).
pub fn resolve(raw: &str, root: &Path) -> Option<PathBuf> {
    let w = raw.trim().trim_matches(['"', '\'', '`']);
    let p = if let Some(rest) = w.strip_prefix("~/") {
        home().join(rest)
    } else if w == "~" {
        home()
    } else if w.starts_with('/') {
        PathBuf::from(w)
    } else if w == ".." || w.starts_with("../") || w.contains("/../") {
        root.join(w)
    } else {
        return None;
    };
    Some(tidy(&p))
}

/// Somewhere any command may look without asking (the system, temporary
/// files, toolchains), or inside `root`.
pub fn free_to_reach(p: &Path, root: &Path) -> bool {
    allowed(p, &root.canonicalize().unwrap_or_else(|_| root.to_path_buf()), &home())
}

/// What to make writable in the jail so `p` can be used: the folder it's in
/// (or the nearest one that exists). None when that would be all of home or
/// the whole system (then the command runs outside the jail, once).
pub fn mount_point(p: &Path) -> Option<PathBuf> {
    let mut q = p.to_path_buf();
    while !q.is_dir() {
        if !q.pop() {
            return None;
        }
    }
    let h = home();
    (q != Path::new("/") && q != h && q != Path::new("/home") && !h.starts_with(&q)).then_some(q)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shell_words() {
        assert_eq!(words(r#"cd "/a b/c" && ls -la 'x y'|grep z\ w"#), vec!["cd", "/a b/c", "ls", "-la", "x y", "grep", "z w"]);
    }

    #[test]
    fn what_reaches_outside() {
        // under home: /tmp is fine to name, so a root there would hide what this checks
        let root = home().join(format!("chuck-outside-test-{}/proj", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        let root = root.canonicalize().unwrap();
        let h = home();
        let r = root.display().to_string();
        // inside, the system, temporary files, toolchains: nothing to ask
        for ok in [format!("cd \"{r}\" && ls -la src"), "cargo build --release".into(), "cat /etc/os-release > /tmp/x".into(),
                   "ls /usr/lib/jvm".into(), "echo hi > /dev/null".into(), format!("{}/.cargo/bin/cargo test", h.display()),
                   "curl -s https://example.org/a/../b".into(), "find . -name '*.py'".into(), "sed -i s/a/b/ src/x.rs".into(),
                   "export ANDROID_HOME=/opt/android-sdk".into(), format!("ls {r}/../{}/src", root.file_name().unwrap().to_string_lossy())] {
            assert!(outside_paths(&ok, &root).is_empty(), "{ok}: {:?}", outside_paths(&ok, &root));
        }
        // outside: asked
        let up = root.parent().unwrap().join(".tools/gradle-8.11.1/bin/gradle");
        assert_eq!(outside_paths("../.tools/gradle-8.11.1/bin/gradle build", &root), vec![up]);
        assert_eq!(outside_paths("cat ~/Documents/notes.txt", &root), vec![h.join("Documents/notes.txt")]);
        assert_eq!(outside_paths("rm -rf /home/someone/x", &root), vec![PathBuf::from("/home/someone/x")]);
        assert_eq!(outside_paths("cp a.txt --target-directory=/srv/data", &root), vec![PathBuf::from("/srv/data")]);
        assert_eq!(outside_paths("ls /", &root), vec![PathBuf::from("/")]);
        assert!(mount_point(Path::new("/")).is_none());
        assert!(mount_point(&h.join("no-such-dir-xyz/file")).is_none(), "would be all of home");
        let _ = std::fs::remove_dir_all(root.parent().unwrap());
    }
}
