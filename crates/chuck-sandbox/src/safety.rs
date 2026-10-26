//! Classifying destructive commands, and pacman hygiene — the same rules as
//! the Python app's safety.py, pattern for pattern (a parity test holds the
//! two to the same verdicts).
//!
//! Two tiers, both static regex (microseconds, so every command is checked):
//!   CRITICAL = unrecoverable: wipes a disk, nukes / or home, runs remote
//!              code, bricks the boot. Needs the user's explicit yes.
//!   DANGER   = destructive but scoped or recoverable: worth reading twice.
//! DANGER is a superset: anything CRITICAL is also DANGER.

use fancy_regex::{Captures, Regex};
use std::sync::OnceLock;

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Level {
    Safe,
    Danger,
    Critical,
}

impl Level {
    pub fn label(self) -> &'static str {
        match self {
            Level::Safe => "",
            Level::Danger => "danger",
            Level::Critical => "critical",
        }
    }
}

// block devices, incl. NVMe / virtio / SD / loop / device-mapper (not just sdX)
const DEV: &str = r"/dev/(?:sd[a-z]|nvme\d+n\d+|vd[a-z]|hd[a-z]|mmcblk\d+|loop\d+|dm-\d+|disk\d+)";
// where a command starts: line start, after ; & | ( ` or $( , optionally under
// sudo (with its flags), env VAR=..., command or exec
const CMD: &str = concat!(r"(?:^|[\n;&|(`]|\$\()\s*(?:sudo(?:\s+-\S+)*\s+)?(?:env\s+(?:\S+=\S*\s+)*)?",
                          r"(?:command\s+|exec\s+|nohup\s+)?\\?(?:/usr)?(?:/s?bin/)?");
const Q: &str = r#"["']?"#;
// home, every way it's written; and the top-level system directories
const HOME: &str = r#"(?:~|\$HOME\b|\$\{HOME\}|/home/[^/\s'";&|]+|/home|/root)"#;
const SYS: &str = r"/(?:etc|usr|boot|var|bin|sbin|lib|lib32|lib64|opt|srv|dev|proc|sys|nix|snap)";
// a target ends at whitespace, a separator, a comment, a redirect or the line end
const TEND: &str = r"(?=\s|$|[;&|)#<>`])";
// rm with recursive intent, short cluster (-rf/-fr/-Rf) or long flags
const RM_REC: &str = r"\brm\s+(?:(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive|--force|-[a-zA-Z]*f[a-zA-Z]*|--)\s+)+";
const ARGS: &str = r"(?:[^\s;&|]+\s+)*?"; // other arguments before the target
const SHELL: &str = r"(?:sudo(?:\s+-\S+)*\s+)?(?:env\s+(?:\S+=\S*\s+)*)?(?:ba|z|k|da|fi)?sh\b";

fn nuke_target() -> String {
    // / itself, /*, home, a system dir, a bare wildcard, or cwd (~/. and ~/.*
    // are home too; .. and ../.. are the folders ABOVE this one; ~name is
    // someone's home)
    format!(r"{Q}(?:/\*?|{HOME}(?:/\.?\*?)?|~[a-z_][\w-]*|{SYS}/?\*?|\*|\.\.?(?:/\.\.)*)/?{Q}")
}

fn broad() -> String {
    // the same without "." and "*": `find . -delete` or `chmod -R 755 .` in a
    // project is ordinary work
    format!(r"{Q}(?:/\*?|{HOME}(?:/\*?)?|{SYS}/?\*?)/?{Q}")
}

fn interp() -> String {
    format!(r"(?:{SHELL}|(?:sudo(?:\s+-\S+)*\s+)?(?:python3?|perl|ruby|node)\b)")
}

fn critical_pattern() -> String {
    let (nuke, broad, interp) = (nuke_target(), broad(), interp());
    [
        // rm -rf aimed at root / home / a system dir / a bare wildcard
        format!("{RM_REC}{ARGS}{nuke}{TEND}"),
        r"--no-preserve-root".into(),
        // filesystem / partition / crypto destruction (as a command, not a word
        // in a grep or an echo; a version or help query is harmless)
        format!(r"{CMD}(?:mkfs(?:\.\w+)?|mke2fs|mkfs\w*|wipefs|blkdiscard|shred|userdel)\b(?![^\n;&|]*\s(?:-V|--version|--help|-h)\b)"),
        r"\bcryptsetup\s+(?:luksFormat|erase)\b".into(),
        format!(r"{CMD}(?:parted|fdisk|sgdisk|cfdisk|gdisk)\b(?![^\n;&|]*\s(?:-l|--list|print|-p|--print)\b)[^\n;&|]*{DEV}"),
        r"\bsgdisk\b[^\n;&|]*--zap-all".into(),
        // raw writes to a block device
        format!(r"\bdd\b[^\n;&|]*\bof={DEV}"),
        format!(r">\s*{DEV}"),
        format!(r"\btee\s+(?:-a\s+)?{DEV}"),
        format!(r"\bcp\b[^\n;&|]*\s{DEV}{TEND}"),
        // remote code execution: a download piped into a shell or interpreter,
        // or run through process substitution / $(...)
        format!(r"\b(?:curl|wget)\b[^\n;&|]*\|\s*{interp}"),
        r"(?:<\(|\$\()\s*(?:curl|wget)\b".into(),
        format!(r"\bbase64\s+(?:-d|--decode)\b[^\n;&]*\|\s*{interp}"),
        // clobbering critical system files
        r">\s*/etc/(?:passwd|shadow|sudoers|fstab|group)\b".into(),
        r"\btee\s+(?:-a\s+)?/etc/(?:passwd|shadow|sudoers|fstab|group)\b".into(),
        r"\btruncate\s+-s\s*0\s+/(?:etc|boot)/".into(),
        r"\bmv\s+(?:/|/etc|/boot|/home|~)\S*\s+/dev/null\b".into(),
        // moving home or a system dir away, or syncing an empty folder over one
        format!(r"\bmv\s+(?:-\S+\s+)*{broad}{TEND}"),
        format!(r"\brsync\b[^\n;&|]*\s--delete[^\n;&|]*\s{broad}\s*(?:$|[;&|)#])"),
        // mass delete via find on a broad path
        format!(r"\bfind\s+{broad}{TEND}[^\n;&|]*(?:-delete\b|-exec\s+rm\b)"),
        format!(r"\bgit\s+clean\b[^\n;&|]*\s{broad}{TEND}"),
        // fork bomb
        r":\(\)\s*\{".into(),
        // ripping out core packages
        r"\b(?:pacman|paru|yay)\s+(?:--?[\w-]+\s+)*?-R[a-z]*\s+[^\n|;]*(?<![\w-])(?:systemd|glibc|linux|bash|coreutils|pacman)(?![\w-])".into(),
        // recursive permission/ownership destruction on root, home or a system dir
        format!(r"\bchmod\b(?=[^\n;&|]*\s-[a-zA-Z]*R)[^\n;&|]*\s{broad}{TEND}"),
        format!(r"\bchown\b(?=[^\n;&|]*\s-[a-zA-Z]*R)[^\n;&|]*\s{broad}{TEND}"),
        r"\bpasswd\s+-d\b".into(),
    ].join("|")
}

const DANGER_EXTRA: &[&str] = &[
    r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\b", // any recursive rm
    r"\brm\s+--recursive\b",
    r"\bgit\s+clean\s+-[a-z]*[xd][a-z]*f?\b", // blows away untracked work
    r"\bdd\b|\bmkswap\b",
    r"\bchmod\s+-R\b|\bchown\s+-R\b",
    r"\b(?:reboot|poweroff|shutdown|halt)\b",
    r"\bsystemctl\s+(?:mask|disable|stop)\b",
    r"\biptables\s+-F\b|\bnft\s+flush\b|\bufw\s+--force\s+reset\b",
    r"\bkillall\b|\bpkill\s+-9\b",
    r"\b(?:pacman|paru|yay)\s+(?:--?[\w-]+\s+)*?-R",
    r"\btruncate\s+-s\s*0\b",
    r"\bfind\b[^|;]*-delete\b|\bfind\b[^|;]*-exec\s+rm\b",
];

struct Rx {
    critical: Regex,
    danger: Regex,
    inner: Regex,
    code_crit: Regex,
    strs: Regex,
    list: Regex,
    pac_cmd: Regex,
    pac_any: Regex,
    short_flag: Regex,
    sgroup: Regex,
    op_long: Regex,
    ro_long: Regex,
    install_only: Regex,
    lead_ws: Regex,
    makepkg_si: Regex,
    sync_long: Regex,
}

fn compile(p: &str) -> Regex {
    let mut b = fancy_regex::RegexBuilder::new(p);
    b.backtrack_limit(2_000_000);
    b.build().unwrap_or_else(|e| panic!("safety pattern doesn't compile: {e}\n{p}"))
}

fn rx() -> &'static Rx {
    static RX: OnceLock<Rx> = OnceLock::new();
    RX.get_or_init(|| {
        let crit = critical_pattern();
        let py_home = format!(concat!(
            r#"(?:(?:pathlib\.)?Path\.home\(\)|os\.path\.expanduser\(\s*['"]~/?['"]\s*\)"#,
            r#"|os\.(?:environ\[['"]HOME['"]\]|getenv\(\s*['"]HOME['"]\s*\))"#,
            r#"|os\.homedir\(\)|homedir\(\)|process\.env\.HOME"#,
            r#"|(?:Path\()?['"](?:/|~|~/|/home|/home/[^/'"]+|/root|{sys})/?['"]\)?)"#), sys = &SYS[1..]);
        Rx {
            critical: compile(&format!("(?im){crit}")),
            danger: compile(&format!("(?im){crit}|{}", DANGER_EXTRA.join("|"))),
            // the command inside bash -c '…', sh -c "…" or eval "…" is a command too
            inner: compile(r#"(?s)(?:\b(?:ba|z|k|da)?sh\s+(?:-\w+\s+)*-c|\beval)\s+(['"])(.*?)(?<!\\)\1"#),
            code_crit: compile(&format!(concat!(r"(?i)\b(?:shutil\.)?rmtree\(\s*{h}",
                r"|\b(?:fs\.)?(?:rmSync|rmdirSync|rm|rmdir)\(\s*{h}[^)]*recursive",
                r#"|\bopen\(\s*['"]{dev}['"]\s*,\s*['"][wa]"#,
                r#"|\b(?:os\.)?(?:remove|unlink)\(\s*['"]/etc/(?:passwd|shadow|sudoers|fstab)['"]"#),
                h = py_home, dev = DEV)),
            strs: compile(r#"(?:[rbuf]{0,2})(['"])((?:\\.|(?!\1).)*)\1"#),
            list: compile(r#"\[\s*((?:[rbuf]{0,2}(['"])(?:\\.|(?!\2).)*\2\s*,\s*)+[rbuf]{0,2}(['"])(?:\\.|(?!\3).)*\3)\s*,?\s*\]"#),
            // a package command at COMMAND position (not inside an echo, a commit
            // message or a comment)
            pac_cmd: compile(concat!(r"(?i)(?P<pre>(?:^|(?<=[\n;&|(`]))[ \t]*(?:sudo(?:[ \t]+-\S+)*[ \t]+)?",
                r"(?:env[ \t]+(?:\S+=\S*[ \t]+)*)?)(?P<tool>(?:pacman|paru|yay)|pamac|makepkg)\b",
                r"(?P<args>[^\n;&|)`]*)")),
            pac_any: compile(r"(?i)\b(?:pacman|paru|yay)\b"),
            short_flag: compile(r"^-[A-Za-z]+$"),
            sgroup: compile(r"^-[A-Za-z]*S[A-Za-z]*$"),
            op_long: compile(r"--(sync|remove|upgrade|query|files|database|deptest)\b"),
            ro_long: compile(concat!(r"--(?:search|info|list|groups|clean|print|downloadonly|query|files|",
                                     r"print-format|version|help)\b")),
            install_only: compile(r"--(?:asdeps|asexplicit|overwrite|dbonly|nodeps|assume-installed)\b"),
            lead_ws: compile(r"^[ \t]*"),
            makepkg_si: compile(r"(?<!\S)-[A-Za-z]*[si][A-Za-z]*(?!\S)"),
            sync_long: compile(r"--sync\b"),
        }
    })
}

/// A pattern that ran out of backtracking budget counts as a match: an
/// input crafted to be slow to check is not given the benefit of the doubt.
fn hit(re: &Regex, s: &str) -> bool {
    re.is_match(s).unwrap_or(true)
}

/// Critical, danger or safe, for a shell command.
pub fn classify_command(cmd: &str) -> Level {
    classify_at(cmd, 0)
}

fn classify_at(cmd: &str, depth: u32) -> Level {
    if cmd.is_empty() {
        return Level::Safe;
    }
    let r = rx();
    let cmd = cmd.replace("\\\n", " "); // line continuations
    if hit(&r.critical, &cmd) {
        return Level::Critical;
    }
    let mut worst = if hit(&r.danger, &cmd) { Level::Danger } else { Level::Safe };
    if depth < 3 {
        for c in r.inner.captures_iter(&cmd) {
            let Ok(c) = c else { return Level::Critical };
            let inner = classify_at(c.get(2).map_or("", |m| m.as_str()), depth + 1);
            if inner == Level::Critical {
                return Level::Critical;
            }
            worst = worst.max(inner);
        }
    }
    worst
}

/// Critical, danger or safe, for a Python / JavaScript program: the shell
/// commands inside its strings (`os.system("rm -rf ~")`, `['rm', '-rf', '/']`)
/// and the library calls that do the same (`rmtree(Path.home())`).
pub fn classify_code(code: &str) -> Level {
    if code.is_empty() {
        return Level::Safe;
    }
    let r = rx();
    if hit(&r.code_crit, code) {
        return Level::Critical;
    }
    let mut cmds: Vec<String> = r.strs.captures_iter(code).flatten()
        .filter_map(|c| c.get(2).map(|m| m.as_str().to_string())).collect();
    for m in r.list.captures_iter(code).flatten() {
        let inside = m.get(1).map_or("", |x| x.as_str());
        let words: Vec<String> = r.strs.captures_iter(inside).flatten()
            .filter_map(|c| c.get(2).map(|x| x.as_str().to_string())).collect();
        cmds.push(words.join(" "));
    }
    let mut worst = Level::Safe;
    for c in cmds {
        let l = classify_command(&c);
        if l == Level::Critical {
            return l;
        }
        worst = worst.max(l);
    }
    worst
}

fn flags(args: &str) -> (Vec<&str>, Vec<&str>) {
    let r = rx();
    let toks: Vec<&str> = args.split_whitespace().collect();
    let shorts = toks.iter().filter(|t| hit_exact(&r.short_flag, t)).map(|t| &t[1..]).collect();
    (toks, shorts)
}

fn hit_exact(re: &Regex, s: &str) -> bool {
    re.is_match(s).unwrap_or(false)
}

/// The pacman operation letter (S, R, U, Q, …), or ' '.
fn op(shorts: &[&str], args: &str) -> char {
    for sh in shorts {
        if let Some(c) = sh.chars().find(|c| "SRUQFDTV".contains(*c)) {
            return c;
        }
    }
    let Ok(Some(c)) = rx().op_long.captures(args) else { return ' ' };
    match c.get(1).map_or("", |m| m.as_str()) {
        "sync" => 'S',
        "remove" => 'R',
        "upgrade" => 'U',
        "query" => 'Q',
        "files" => 'F',
        "database" => 'D',
        "deptest" => 'T',
        _ => ' ',
    }
}

fn read_only(shorts: &[&str], args: &str, op: char) -> bool {
    if "QFTVD".contains(op) && op != ' ' {
        return true;
    }
    if op == 'S' && shorts.iter().any(|sh| sh.contains('S') && sh.chars().any(|c| "silcgpw".contains(c))) {
        return true;
    }
    hit_exact(&rx().ro_long, args)
}

fn group<'a>(c: &'a Captures<'_, str>, name: &str) -> &'a str {
    c.name(name).map_or("", |m| m.as_str())
}

/// Rewrite `pacman -S pkg` into `pacman -Syu pkg` (also paru / yay). On Arch
/// a plain -S installs against a stale database: the partial-upgrade
/// footgun. Only a real package command is touched (not one quoted in an echo
/// or a commit message), read-only operations are left alone, and
/// install-only flags (--asdeps, -d, --overwrite) stay on the install
/// instead of riding along on the system upgrade.
pub fn enforce_syu(cmd: &str) -> String {
    let r = rx();
    if cmd.is_empty() || !hit_exact(&r.pac_any, cmd) {
        return cmd.to_string();
    }
    let fix = |m: &Captures<'_, str>| -> String {
        let whole = m.get(0).map_or("", |x| x.as_str()).to_string();
        let (pre, tool, args) = (group(m, "pre"), group(m, "tool"), group(m, "args"));
        if matches!(tool.to_lowercase().as_str(), "pamac" | "makepkg") {
            return whole;
        }
        let (toks, shorts) = flags(args);
        let o = op(&shorts, args);
        if o != 'S' || read_only(&shorts, args, o) {
            return whole;
        }
        let sgroups: Vec<&str> = toks.iter().copied().filter(|t| hit_exact(&r.sgroup, t)).collect();
        let short: Vec<&str> = toks.iter().copied().filter(|t| hit_exact(&r.short_flag, t)).collect();
        let has_y = short.iter().any(|t| t.contains('y'));
        let has_u = short.iter().any(|t| t.contains('u')) || args.contains("--sysupgrade");
        if has_y && has_u {
            return whole; // already syncs and upgrades
        }
        let pkgs = toks.iter().any(|t| !t.starts_with('-'));
        let install_only = hit_exact(&r.install_only, args) || sgroups.iter().any(|t| t[1..].contains('d'));
        if install_only && pkgs {
            // upgrade the system first, then install with those flags
            let lead = r.lead_ws.find(pre).ok().flatten().map_or("", |x| x.as_str());
            let again = if pre.trim().is_empty() { String::new() } else { format!("{} ", pre.trim()) };
            let mut s = format!("{pre}{tool} -Syu && {again}{tool}{args}");
            if !lead.is_empty() {
                s = s.replace(&format!("{lead}{lead}"), lead);
            }
            return s;
        }
        if let Some(g) = sgroups.first() {
            let rest = g[1..].replacen('S', "", 1);
            let ny = rest.matches('y').count().max(if has_y { 0 } else { 1 });
            let nu = rest.matches('u').count().max(if has_u { 0 } else { 1 });
            let new = format!("-S{}{}{}", "y".repeat(ny), "u".repeat(nu), rest.replace(['y', 'u'], ""));
            let one = compile(&format!(r"(?<!\S){}(?!\S)", fancy_regex::escape(g)));
            return format!("{pre}{tool}{}", one.replacen(args, 1, fancy_regex::NoExpand(&new)));
        }
        // long form: --sync
        format!("{pre}{tool}{}", r.sync_long.replacen(args, 1, fancy_regex::NoExpand("--sync --refresh --sysupgrade")))
    };
    r.pac_cmd.try_replacen(cmd, 0, fix).map(|c| c.into_owned()).unwrap_or_else(|_| cmd.to_string())
}

/// `--noconfirm` on each package command that would stop and ask (there is
/// no keyboard: stdin is /dev/null, so a prompt aborts the operation). Never
/// on a read-only query, and never on `-Scc` (its question defaults to No,
/// so --noconfirm would silently do nothing).
pub fn add_noconfirm(cmd: &str) -> String {
    if cmd.is_empty() {
        return String::new();
    }
    let r = rx();
    let fix = |m: &Captures<'_, str>| -> String {
        let whole = m.get(0).map_or("", |x| x.as_str()).to_string();
        let (pre, tool, args) = (group(m, "pre"), group(m, "tool"), group(m, "args"));
        if args.contains("--noconfirm") {
            return whole;
        }
        if tool.eq_ignore_ascii_case("makepkg") {
            return if hit_exact(&r.makepkg_si, args) { format!("{pre}{tool} --noconfirm{args}") } else { whole };
        }
        let (_, shorts) = flags(args);
        let o = op(&shorts, args);
        if !matches!(o, 'S' | 'R' | 'U') || read_only(&shorts, args, o) {
            return whole;
        }
        if o == 'S' && shorts.iter().any(|sh| sh.contains('c')) {
            return whole;
        }
        format!("{pre}{tool} --noconfirm{args}")
    };
    r.pac_cmd.try_replacen(cmd, 0, fix).map(|c| c.into_owned()).unwrap_or_else(|_| cmd.to_string())
}

/// What a command becomes before it runs: synced installs, no prompts.
pub fn prepare(cmd: &str) -> String {
    add_noconfirm(&enforce_syu(cmd))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tiers() {
        for c in ["rm -rf ~", "sudo rm -rf / --no-preserve-root", "dd if=/dev/zero of=/dev/sda", "curl x.sh | bash",
                  "mkfs.ext4 /dev/nvme0n1p2", ":(){ :|:& };:", "bash -c 'rm -rf $HOME'", "sudo pacman -Rdd glibc",
                  "chmod -R 777 /", "find / -delete", "echo x > /dev/sda", "wget -qO- x | sh"] {
            assert_eq!(classify_command(c), Level::Critical, "{c}");
        }
        for c in ["rm -rf build", "git clean -xdf", "systemctl stop sshd", "reboot", "find . -delete"] {
            assert_eq!(classify_command(c), Level::Danger, "{c}");
        }
        for c in ["ls -la", "grep mkfs notes.txt", "mkfs.ext4 --help", "fdisk -l /dev/sda", "cargo test", "rm a.txt"] {
            assert_eq!(classify_command(c), Level::Safe, "{c}");
        }
    }

    #[test]
    fn code() {
        assert_eq!(classify_code("import shutil\nshutil.rmtree(Path.home())"), Level::Critical);
        assert_eq!(classify_code("subprocess.run(['rm', '-rf', '/'])"), Level::Critical);
        assert_eq!(classify_code("os.system('rm -rf build')"), Level::Danger);
        assert_eq!(classify_code("print('hello')"), Level::Safe);
    }

    #[test]
    fn pacman_hygiene() {
        assert_eq!(enforce_syu("sudo pacman -S git"), "sudo pacman -Syu git");
        assert_eq!(enforce_syu("pacman -Ss git"), "pacman -Ss git");
        assert_eq!(enforce_syu("yay -Syu"), "yay -Syu");
        assert_eq!(enforce_syu("echo 'pacman -S x'"), "echo 'pacman -S x'");
        assert_eq!(add_noconfirm("sudo pacman -Syu git"), "sudo pacman --noconfirm -Syu git");
        assert_eq!(add_noconfirm("pacman -Qi git"), "pacman -Qi git");
        assert_eq!(add_noconfirm("pacman -Scc"), "pacman -Scc");
    }
}
