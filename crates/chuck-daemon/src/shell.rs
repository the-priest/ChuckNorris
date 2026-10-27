//! What a daemon may run. Its commands run with the whole system read-only
//! and without the system bus (no service, power or login control can be
//! reached at all); this check is the second wall, and the only one when
//! there is no bubblewrap. It reads the command as a shell would (quotes,
//! escapes, pipes), refuses anything that would run another command for it
//! (sh -c, xargs, env, nice, timeout…), and checks each command against
//! what it may do.

const RO_CMDS: &[&str] = &["df", "du", "ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "wc", "stat",
    "file", "find", "uname", "uptime", "free", "ps", "journalctl", "systemctl", "pacman", "checkupdates", "git", "ip",
    "ss", "lsblk", "findmnt", "sensors", "date", "who", "w", "last", "id", "whoami", "hostname", "sort", "uniq", "cut",
    "tr", "echo", "printf", "nproc", "lscpu", "lspci", "lsusb", "which", "test", "true", "false", "column", "jq", "diff",
    "md5sum", "sha256sum", "tree", "readlink", "realpath", "basename", "dirname", "nvidia-smi", "paccache", "pactree",
    "expac", "zcat", "printenv", "locale", "lsmod", "dmesg", "inxi", "fastfetch", "pgrep", "vmstat", "iostat", "top",
    "pwd", "seq", "awk"];
const ROOT: &[&str] = &["sudo", "doas", "pkexec", "su", "run0"];
const RUNS_OTHERS: &[&str] = &["sh", "bash", "zsh", "dash", "ksh", "fish", "xargs", "env", "nice", "ionice", "timeout",
    "time", "command", "exec", "stdbuf", "setsid", "nohup", "chrt", "taskset", "flock", "watch", "busybox", "unbuffer",
    "eval", "source", ".", "parallel", "script", "runuser", "systemd-run", "strace", "gdb", "python", "python3", "perl",
    "ruby", "node", "less", "more", "vi", "vim", "nano"];
const NEVER: &[&str] = &["loginctl", "busctl", "dbus-send", "gdbus", "qdbus", "shutdown", "reboot", "poweroff", "halt",
    "kill", "pkill", "killall", "rm", "rmdir", "dd", "chmod", "chown", "chgrp", "mv", "cp", "tee", "truncate", "ln",
    "install", "rsync", "shred", "mount", "umount", "swapoff", "swapon", "wipefs", "fdisk", "parted", "sed", "crontab",
    "at", "curl", "wget", "hostnamectl", "timedatectl", "localectl", "nmcli", "rfkill", "udisksctl"];
const SYSTEMCTL_RO: &[&str] = &["status", "list-units", "list-timers", "list-unit-files", "list-sockets",
    "list-dependencies", "list-jobs", "list-machines", "is-active", "is-failed", "is-enabled", "is-system-running",
    "show", "cat", "help", "get-default"];
const SYSTEMCTL_ARGOPTS: &[&str] = &["-p", "-t", "-H", "-M", "-n", "-o", "-s", "--property", "--type", "--host",
    "--machine", "--lines", "--output", "--state", "--signal", "--kill-whom", "--root", "--what", "--timestamp"];
const GIT_RO: &[&str] = &["status", "log", "diff", "show", "rev-parse", "ls-files", "describe", "blame", "shortlog",
    "rev-list", "cat-file", "ls-tree", "branch"];

const PUNCT: &str = ";&|<>()";

/// Shell words, as POSIX shlex reads them with `;&|<>()` as punctuation
/// (a run of punctuation is one token: `&&`, `2>&1` is `2` `>&` `1`).
/// None: unbalanced quotes.
pub fn tokens(cmd: &str) -> Option<Vec<String>> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut have = false; // a word is open (it may be empty: "")
    let mut punct = String::new();
    let mut it = cmd.chars().peekable();
    let flush_word = |out: &mut Vec<String>, cur: &mut String, have: &mut bool| {
        if *have {
            out.push(std::mem::take(cur));
            *have = false;
        }
    };
    while let Some(c) = it.next() {
        if PUNCT.contains(c) {
            flush_word(&mut out, &mut cur, &mut have);
            punct.push(c);
            continue;
        }
        if !punct.is_empty() {
            out.push(std::mem::take(&mut punct));
        }
        match c {
            c if c.is_whitespace() => flush_word(&mut out, &mut cur, &mut have),
            '\'' => {
                have = true;
                loop {
                    match it.next() {
                        Some('\'') => break,
                        Some(ch) => cur.push(ch),
                        None => return None,
                    }
                }
            }
            '"' => {
                have = true;
                loop {
                    match it.next() {
                        Some('"') => break,
                        Some('\\') => match it.next() {
                            Some(e @ ('"' | '\\')) => cur.push(e),
                            Some(e) => {
                                cur.push('\\');
                                cur.push(e);
                            }
                            None => return None,
                        },
                        Some(ch) => cur.push(ch),
                        None => return None,
                    }
                }
            }
            '\\' => {
                have = true;
                cur.push(it.next()?);
            }
            c => {
                have = true;
                cur.push(c);
            }
        }
    }
    flush_word(&mut out, &mut cur, &mut have);
    if !punct.is_empty() {
        out.push(punct);
    }
    Some(out)
}

/// Each simple command's argv, or why the command can't be read safely.
fn split(cmd: &str) -> Result<Vec<Vec<String>>, String> {
    let toks = tokens(cmd).ok_or("unbalanced quotes")?;
    let mut segs: Vec<Vec<String>> = Vec::new();
    let mut cur: Vec<String> = Vec::new();
    let mut i = 0;
    while i < toks.len() {
        let t = &toks[i];
        let all = |set: &str| !t.is_empty() && t.chars().all(|c| set.contains(c));
        if all(";&|") {
            segs.push(std::mem::take(&mut cur));
        } else if all("()") {
            return Err("no subshells".into());
        } else if all("<>&") {
            if t.contains('>') {
                let tgt = toks.get(i + 1).map(String::as_str).unwrap_or("");
                if !["/dev/null", "1", "2"].contains(&tgt) || (!t.contains('&') && (tgt == "1" || tgt == "2")) {
                    return Err("no writing to files".into());
                }
                if cur.last().is_some_and(|l| l == "1" || l == "2") {
                    cur.pop(); // the fd number of 2>/dev/null
                }
            }
            i += 2;
            continue;
        } else {
            cur.push(t.clone());
        }
        i += 1;
    }
    segs.push(cur);
    Ok(segs.into_iter().filter(|s| !s.is_empty()).collect())
}

fn positional<'a>(args: &'a [String], takes_value: &[&str]) -> Vec<&'a str> {
    let mut out = Vec::new();
    let mut skip = false;
    for a in args {
        if skip {
            skip = false;
            continue;
        }
        if takes_value.contains(&a.as_str()) {
            skip = true;
            continue;
        }
        if !a.starts_with('-') {
            out.push(a.as_str());
        }
    }
    out
}

fn argv_problem(argv: &[String], strict: bool) -> String {
    let assign = regex::Regex::new(r"^[A-Za-z_][A-Za-z0-9_]*=").unwrap();
    let argv: Vec<String> = argv.iter().skip_while(|a| assign.is_match(a)).cloned().collect(); // FOO=1 cmd
    let Some(first) = argv.first() else { return String::new() };
    let prog = first.rsplit('/').next().unwrap_or(first).to_string();
    let args = &argv[1..];
    let has = |opts: &[&str]| args.iter().any(|a| opts.contains(&a.as_str()));
    if ROOT.contains(&prog.as_str()) {
        return "no root for a daemon".into();
    }
    if RUNS_OTHERS.contains(&prog.as_str()) {
        return format!("{prog} runs other commands; run the command itself");
    }
    if NEVER.contains(&prog.as_str()) || prog.starts_with("mkfs") {
        return format!("{prog} isn't read-only");
    }
    if args.iter().any(|a| a.starts_with("--output")) {
        return "no writing to files".into();
    }
    match prog.as_str() {
        "systemctl" => {
            if let Some(bad) = args.iter().find(|a| SYSTEMCTL_ARGOPTS.contains(&a.as_str())) {
                return format!("write it as {bad}=<value>");
            }
            let pos = positional(args, &[]);
            if let Some(verb) = pos.first() {
                if !SYSTEMCTL_RO.contains(verb) {
                    return format!("systemctl {verb} changes the system");
                }
            }
        }
        "journalctl" => {
            if args.iter().any(|a| ["--vacuum", "--rotate", "--flush", "--relinquish", "--sync", "--setup-keys",
                                    "--update-catalog"].iter().any(|p| a.starts_with(p))) {
                return "journalctl maintenance changes the journal".into();
            }
        }
        "find" => {
            let bad = regex::Regex::new(r"^-(?:delete|exec|execdir|ok|okdir|fprint\w*|fls)$").unwrap();
            if args.iter().any(|a| bad.is_match(a)) {
                return "find may only list".into();
            }
        }
        "pacman" => {
            let first = args.iter().find(|a| a.starts_with('-')).map(String::as_str).unwrap_or("");
            let ok = regex::Regex::new(r"^(?:-Q\w*|--query|-S[silg]+|-F[slx]*)$").unwrap();
            if !ok.is_match(first) {
                return "pacman may only query".into();
            }
        }
        "git" => {
            let pos = positional(args, &["-C", "-c"]);
            let verb = pos.first().copied().unwrap_or("");
            if !verb.is_empty() && !GIT_RO.contains(&verb) {
                return format!("git {verb} changes the repository");
            }
            if verb == "branch" && (pos.len() > 1 || has(&["-d", "-D", "-m", "-M", "-c", "-C", "-f"])) {
                return "git branch may only list".into();
            }
            if has(&["-c"]) {
                return "no git -c (it can run commands)".into();
            }
        }
        "sort" if args.iter().any(|a| a.starts_with("-o")) => return "no writing to files".into(),
        "tree" if has(&["-o"]) => return "no writing to files".into(),
        "uniq" if positional(args, &[]).len() > 1 => return "no writing to files".into(),
        "dmesg" if has(&["-c", "-C", "-D", "-E", "-n", "--clear", "--read-clear", "--console-off", "--console-on",
                         "--console-level"]) => return "dmesg may only read".into(),
        "date" if args.iter().any(|a| a == "-s" || a == "--set" || a.starts_with("--set=")) => {
            return "date may only read".into();
        }
        "awk" if args.iter().any(|a| a.contains("system") || a.contains('|') || a.contains('>')) => {
            return "awk may only read".into();
        }
        "top" if !has(&["-b"]) => return "run top as `top -b -n 1`".into(),
        _ => {}
    }
    if strict && !RO_CMDS.contains(&prog.as_str()) {
        return format!("{prog} isn't on the read-only list (install bubblewrap and a daemon may run more)");
    }
    String::new()
}

/// Why a daemon may NOT run `cmd` ("" when it may). `strict`: there is no
/// sandbox, so only commands on the read-only list run.
pub fn read_only_problem(cmd: &str, strict: bool) -> String {
    let c = cmd.trim();
    if c.is_empty() {
        return "an empty command".into();
    }
    let without_arith = c.replace("$((", "");
    if ["`", "$(", "<(", ">(", "${"].iter().any(|p| without_arith.contains(p)) {
        return "no command substitution".into();
    }
    let arith = regex::Regex::new(r"\$\(\([^()]*\)\)").unwrap();
    let c = arith.replace_all(c, "0");
    match split(&c) {
        Err(why) => why,
        Ok(segs) => segs.iter().map(|a| argv_problem(a, strict)).find(|w| !w.is_empty()).unwrap_or_default(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn words_as_a_shell_reads_them() {
        assert_eq!(tokens(r#"echo "a b" 'c d' e\ f 2>&1 | sort && x;y"#).unwrap(),
                   ["echo", "a b", "c d", "e f", "2", ">&", "1", "|", "sort", "&&", "x", ";", "y"]);
        assert_eq!(tokens(r#""" x"#).unwrap(), ["", "x"]);
        assert!(tokens("echo 'open").is_none());
        assert_eq!(tokens(r#""a\"b\$c""#).unwrap(), [r#"a"b\$c"#]);
    }

    #[test]
    fn looking_is_allowed() {
        for c in ["journalctl -u nginx -n 50 --no-pager", "systemctl status nginx", "df -h; du -sh ~/x | sort -h",
                  "git log -5", "pacman -Qu", "echo looked-$((1+1))"] {
            assert_eq!(read_only_problem(c, false), "", "{c}");
        }
        for c in ["systemctl status nginx", "systemctl list-units --state=failed",
                  "journalctl -p 3 -b --no-pager 2>&1 | tail -50", "git log -5 --oneline", "du -sh ~/x | sort -h",
                  "ip -s link"] {
            assert_eq!(read_only_problem(c, true), "", "strict: {c}");
        }
    }

    #[test]
    fn changing_anything_is_not() {
        for c in ["systemctl restart nginx", "sudo journalctl", "git push", "pacman -Syu", "find . -delete",
                  "busctl call org.freedesktop.login1 / x PowerOff", "rm x", "xargs rm", "loginctl poweroff",
                  "dbus-send --system x", "curl x", "echo suspend | xargs systemctl", "systemctl -p status suspend",
                  "nice systemctl suspend", "sh -c 'systemctl suspend'", "\"systemctl\" suspend", "s\\ystemctl suspend",
                  "(systemctl suspend)", "$(printf systemctl) suspend", "git log --output=/tmp/x",
                  "sort -o ~/.bashrc /dev/null", "git stash", "git reflog expire --all", "loginctl list-users",
                  "awk 'BEGIN{system(\"id\")}'", "echo x > f", "echo `id`", "cat <(id)", "echo ${HOME}", "FOO=1 rm x",
                  "top", "git -c core.pager=x log", "git branch -D main", "", "echo 'open"] {
            assert_ne!(read_only_problem(c, false), "", "a daemon may not run: {c}");
            assert_ne!(read_only_problem(c, true), "", "strict: {c}");
        }
        assert_ne!(read_only_problem("vmtouch x", true), "");
        assert_eq!(read_only_problem("vmtouch x", false), "", "with a sandbox the list is not the only wall");
        assert_eq!(read_only_problem("ls 2>/dev/null", true), "");
    }
}
