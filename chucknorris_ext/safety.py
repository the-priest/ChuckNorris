"""safety.py — classifying destructive commands, and pacman hygiene.

Part of Chuck Norris. Split out of the original single file so each concern
can be read, tested and changed on its own.
"""
import re

# ── destructive-command classification ──────────────────────────────────────
# Two tiers, both purely static regex (microseconds — no cost to answer speed):
#   CRITICAL  = unrecoverable. Wipes a disk, nukes /, executes remote code,
#               bricks the boot. These need an EXPLICIT second confirmation
#               before the Run button will even arm.
#   DANGER    = destructive but scoped/recoverable. Red warning, single approve.
# DANGER is a superset: anything CRITICAL is also DANGER.

# block devices, incl. NVMe / virtio / SD / loop / device-mapper (not just sdX)
_DEV = r"/dev/(?:sd[a-z]|nvme\d+n\d+|vd[a-z]|hd[a-z]|mmcblk\d+|loop\d+|dm-\d+|disk\d+)"
# where a command starts: line start, after ; & | ( ` or $( , optionally under
# sudo (with its flags), env VAR=..., command or exec
_CMD = (r"(?:^|[\n;&|(`]|\$\()\s*(?:sudo(?:\s+-\S+)*\s+)?(?:env\s+(?:\S+=\S*\s+)*)?"
        r"(?:command\s+|exec\s+|nohup\s+)?\\?(?:/usr)?(?:/s?bin/)?")
_Q = r"""["']?"""
# home, every way it's written; and the top-level system directories
_HOME = r"(?:~|\$HOME\b|\$\{HOME\}|/home/[^/\s'\";&|]+|/home|/root)"
_SYS = r"/(?:etc|usr|boot|var|bin|sbin|lib|lib32|lib64|opt|srv|dev|proc|sys|nix|snap)"
# a "root-ish" target: / itself, /*, home, a system dir, a bare wildcard, or cwd
_NUKE_TARGET = _Q + r"(?:/\*?|" + _HOME + r"(?:/\*?)?|" + _SYS + r"/?\*?|\*|\.)/?" + _Q
# the same without "." and "*": `find . -delete` or `chmod -R 755 .` in a
# project is ordinary work
_BROAD = _Q + r"(?:/\*?|" + _HOME + r"(?:/\*?)?|" + _SYS + r"/?\*?)/?" + _Q
# a target ends at whitespace, a separator, a comment, a redirect or the line end
_TEND = r"(?=\s|$|[;&|)#<>`])"
# rm with recursive intent, short cluster (-rf/-fr/-Rf) or long flags
_RM_REC = r"\brm\s+(?:(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive|--force|-[a-zA-Z]*f[a-zA-Z]*|--)\s+)+"
_ARGS = r"(?:[^\s;&|]+\s+)*?"                     # other arguments before the target
_SHELL = r"(?:sudo(?:\s+-\S+)*\s+)?(?:env\s+(?:\S+=\S*\s+)*)?(?:ba|z|k|da|fi)?sh\b"
_INTERP = r"(?:" + _SHELL + r"|(?:sudo(?:\s+-\S+)*\s+)?(?:python3?|perl|ruby|node)\b)"

CRITICAL = re.compile(
    # rm -rf aimed at root / home / a system dir / a bare wildcard
    _RM_REC + _ARGS + _NUKE_TARGET + _TEND +
    r"|--no-preserve-root"
    # filesystem / partition / crypto destruction (as a command, not a word in
    # a grep or an echo; a version or help query is harmless)
    r"|" + _CMD + r"(?:mkfs(?:\.\w+)?|mke2fs|mkfs\w*|wipefs|blkdiscard|shred|userdel)\b"
    r"(?![^\n;&|]*\s(?:-V|--version|--help|-h)\b)"
    r"|\bcryptsetup\s+(?:luksFormat|erase)\b"
    r"|" + _CMD + r"(?:parted|fdisk|sgdisk|cfdisk|gdisk)\b"
    r"(?![^\n;&|]*\s(?:-l|--list|print|-p|--print)\b)[^\n;&|]*" + _DEV +
    r"|\bsgdisk\b[^\n;&|]*--zap-all"
    # raw writes to a block device
    r"|\bdd\b[^\n;&|]*\bof=" + _DEV +
    r"|>\s*" + _DEV +
    r"|\btee\s+(?:-a\s+)?" + _DEV +
    r"|\bcp\b[^\n;&|]*\s" + _DEV + _TEND +
    # remote code execution: a download piped into a shell or interpreter,
    # or run through process substitution / $(...)
    r"|\b(?:curl|wget)\b[^\n;&|]*\|\s*" + _INTERP +
    r"|(?:<\(|\$\()\s*(?:curl|wget)\b"
    r"|\bbase64\s+(?:-d|--decode)\b[^\n;&]*\|\s*" + _INTERP +
    # clobbering critical system files
    r"|>\s*/etc/(?:passwd|shadow|sudoers|fstab|group)\b"
    r"|\btee\s+(?:-a\s+)?/etc/(?:passwd|shadow|sudoers|fstab|group)\b"
    r"|\btruncate\s+-s\s*0\s+/(?:etc|boot)/"
    r"|\bmv\s+(?:/|/etc|/boot|/home|~)\S*\s+/dev/null\b"
    # mass delete via find on a broad path
    r"|\bfind\s+" + _BROAD + _TEND + r"[^\n;&|]*(?:-delete\b|-exec\s+rm\b)"
    r"|\bgit\s+clean\b[^\n;&|]*\s" + _BROAD + _TEND +
    # fork bomb
    r"|:\(\)\s*\{"
    # ripping out core packages
    r"|\b(?:pacman|paru|yay)\s+(?:--?[\w-]+\s+)*?-R[a-z]*\s+[^\n|;]*(?<![\w-])"
    r"(?:systemd|glibc|linux|bash|coreutils|pacman)(?![\w-])"
    # recursive permission/ownership destruction on root, home or a system dir
    r"|\bchmod\b(?=[^\n;&|]*\s-[a-zA-Z]*R)[^\n;&|]*\s" + _BROAD + _TEND +
    r"|\bchown\b(?=[^\n;&|]*\s-[a-zA-Z]*R)[^\n;&|]*\s" + _BROAD + _TEND +
    r"|\bpasswd\s+-d\b",
    re.IGNORECASE | re.MULTILINE)

DANGER = re.compile(
    # everything critical, plus scoped-but-destructive things worth reading twice
    CRITICAL.pattern +
    r"|\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\b"          # any recursive rm
    r"|\brm\s+--recursive\b"
    r"|\bgit\s+clean\s+-[a-z]*[xd][a-z]*f?\b"     # blows away untracked work
    r"|\bdd\b|\bmkswap\b"
    r"|\bchmod\s+-R\b|\bchown\s+-R\b"
    r"|\b(?:reboot|poweroff|shutdown|halt)\b"
    r"|\bsystemctl\s+(?:mask|disable|stop)\b"
    r"|\biptables\s+-F\b|\bnft\s+flush\b|\bufw\s+--force\s+reset\b"
    r"|\bkillall\b|\bpkill\s+-9\b"
    r"|\b(?:pacman|paru|yay)\s+(?:--?[\w-]+\s+)*?-R"
    r"|\btruncate\s+-s\s*0\b"
    r"|\bfind\b[^|;]*-delete\b|\bfind\b[^|;]*-exec\s+rm\b",
    re.IGNORECASE | re.MULTILINE)

# the command inside bash -c '…', sh -c "…" or eval "…" is a command too
_INNER = re.compile(r"""(?:\b(?:ba|z|k|da)?sh\s+(?:-\w+\s+)*-c|\beval)\s+(['"])(.*?)(?<!\\)\1""",
                    re.DOTALL)


def classify_command(cmd, _depth=0):
    """Return 'critical', 'danger', or '' for a shell command string.
    Static and fast — this runs on every card, so it must never be slow."""
    if not cmd:
        return ""
    cmd = cmd.replace("\\\n", " ")                  # line continuations
    if CRITICAL.search(cmd):
        return "critical"
    worst = "danger" if DANGER.search(cmd) else ""
    if _depth < 3:
        for m in _INNER.finditer(cmd):
            inner = classify_command(m.group(2), _depth + 1)
            if inner == "critical":
                return "critical"
            worst = worst or inner
    return worst


# ── code blocks: Python / JavaScript that destroys the same things ─────────
_PY_HOME = (r"(?:(?:pathlib\.)?Path\.home\(\)|os\.path\.expanduser\(\s*['\"]~/?['\"]\s*\)"
            r"|os\.(?:environ\[['\"]HOME['\"]\]|getenv\(\s*['\"]HOME['\"]\s*\))"
            r"|os\.homedir\(\)|homedir\(\)|process\.env\.HOME"
            r"|(?:Path\()?['\"](?:/|~|~/|/home|/home/[^/'\"]+|/root|" + _SYS[1:] + r")/?['\"]\)?)")
_CODE_CRIT = re.compile(
    r"\b(?:shutil\.)?rmtree\(\s*" + _PY_HOME +
    r"|\b(?:fs\.)?(?:rmSync|rmdirSync|rm|rmdir)\(\s*" + _PY_HOME + r"[^)]*recursive"
    r"|\bopen\(\s*['\"]" + _DEV + r"['\"]\s*,\s*['\"][wa]"
    r"|\b(?:os\.)?(?:remove|unlink)\(\s*['\"]/etc/(?:passwd|shadow|sudoers|fstab)['\"]",
    re.IGNORECASE)
_STR = re.compile(r"""(?:[rbuf]{0,2})(['"])((?:\\.|(?!\1).)*)\1""")
_LIST = re.compile(r"""\[\s*((?:[rbuf]{0,2}(['"])(?:\\.|(?!\2).)*\2\s*,\s*)+[rbuf]{0,2}(['"])(?:\\.|(?!\3).)*\3)\s*,?\s*\]""")


def classify_code(lang, code):
    """'critical', 'danger' or '' for a python/node program: the shell
    commands inside its strings (os.system("rm -rf ~"), ['rm', '-rf', '/'])
    and the library calls that do the same (rmtree(Path.home()))."""
    if not code:
        return ""
    if _CODE_CRIT.search(code):
        return "critical"
    worst = ""
    cmds = [m.group(2) for m in _STR.finditer(code)]
    for m in _LIST.finditer(code):
        cmds.append(" ".join(x.group(2) for x in _STR.finditer(m.group(1))))
    for c in cmds:
        r = classify_command(c)
        if r == "critical":
            return "critical"
        worst = worst or r
    return worst


# pacman AND its AUR wrappers: paru/yay take the same flag grammar, and a bare
# `paru -S pkg` is the same partial-upgrade footgun as `pacman -S pkg`.
_PAC_TOOLS = r"(?:pacman|paru|yay)"
# a package command at COMMAND position (not inside an echo, a commit message
# or a comment): line start or after ; & | ( ` $( , optionally under sudo/env
_PAC_CMD = re.compile(
    r"(?P<pre>(?:^|(?<=[\n;&|(`]))[ \t]*(?:sudo(?:[ \t]+-\S+)*[ \t]+)?"
    r"(?:env[ \t]+(?:\S+=\S*[ \t]+)*)?)(?P<tool>" + _PAC_TOOLS + r"|pamac|makepkg)\b"
    r"(?P<args>[^\n;&|)`]*)", re.IGNORECASE)
# read-only operations, short (in a -S group) and long
_RO_SHORT = set("silcgpw")
_RO_LONG = re.compile(r"--(?:search|info|list|groups|clean|print|downloadonly|query|files|"
                      r"print-format|version|help)\b")
# install-only flags: harmless for the package asked for, damaging when they
# ride along on a whole-system upgrade (-Syu --asdeps marks EVERYTHING a dep)
_INSTALL_ONLY = re.compile(r"--(?:asdeps|asexplicit|overwrite|dbonly|nodeps|assume-installed)\b")


def _flags(args):
    toks = args.split()
    shorts = [t[1:] for t in toks if re.fullmatch(r"-[A-Za-z]+", t)]
    return toks, shorts


def _op(shorts, args):
    """The pacman operation letter (S, R, U, Q, …), or ''."""
    for sh in shorts:
        for c in sh:
            if c in "SRUQFDTV":
                return c
    m = re.search(r"--(sync|remove|upgrade|query|files|database|deptest)\b", args)
    return {"sync": "S", "remove": "R", "upgrade": "U", "query": "Q", "files": "F",
            "database": "D", "deptest": "T"}.get(m.group(1), "") if m else ""


def _read_only(shorts, args, op):
    if op in ("Q", "F", "T", "V", "D"):
        return True
    if op == "S" and any(set(sh) & _RO_SHORT for sh in shorts if "S" in sh):
        return True
    return bool(_RO_LONG.search(args))


def enforce_syu(cmd):
    """Rewrite `pacman -S pkg` into `pacman -Syu pkg` (also paru / yay).

    On Arch a plain -S installs against a stale local database, which is the
    classic partial-upgrade footgun. Syncing first is the only supported way
    to install. This is enforced here rather than merely requested in the
    prompt, because a rule the model can forget isn't a rule. Only a real
    package command is touched (not one quoted in an echo or a commit
    message), read-only operations are left alone, and install-only flags
    (--asdeps, -d, --overwrite) stay on the install instead of riding along
    on the system upgrade.
    """
    if not cmd or not re.search(r"\b" + _PAC_TOOLS + r"\b", cmd, re.IGNORECASE):
        return cmd

    def fix(m):
        tool, args = m.group("tool"), m.group("args")
        if tool.lower() in ("pamac", "makepkg"):
            return m.group(0)
        toks, shorts = _flags(args)
        op = _op(shorts, args)
        if op != "S" or _read_only(shorts, args, op):
            return m.group(0)
        sgroups = [t for t in toks if re.fullmatch(r"-[A-Za-z]*S[A-Za-z]*", t)]
        has_y = any("y" in t for t in toks if re.fullmatch(r"-[A-Za-z]+", t))
        has_u = any("u" in t for t in toks if re.fullmatch(r"-[A-Za-z]+", t)) or "--sysupgrade" in args
        if has_y and has_u:
            return m.group(0)                     # already syncs and upgrades
        pkgs = [t for t in toks if not t.startswith("-")]
        install_only = bool(_INSTALL_ONLY.search(args)) or any(
            "d" in t[1:] for t in sgroups)
        pre = m.group("pre")
        if install_only and pkgs:
            # upgrade the system first, then install with those flags
            lead = re.match(r"[ \t]*", pre).group(0)
            return (f"{pre}{tool} -Syu && {pre.strip() + ' ' if pre.strip() else ''}"
                    f"{tool}{args}").replace(lead + lead, lead)
        if sgroups:
            g = sgroups[0]
            rest = g[1:].replace("S", "", 1)
            ny = max(rest.count("y"), 0 if has_y else 1)
            nu = max(rest.count("u"), 0 if has_u else 1)
            new = "-S" + "y" * ny + "u" * nu + rest.replace("y", "").replace("u", "")
            return f"{pre}{tool}" + re.sub(r"(?<!\S)" + re.escape(g) + r"(?!\S)", new, args, count=1)
        # long form: --sync
        return f"{pre}{tool}" + re.sub(r"--sync\b", "--sync --refresh --sysupgrade", args, count=1)

    return _PAC_CMD.sub(fix, cmd)


def add_noconfirm(cmd):
    """--noconfirm on each package command that would stop and ask (there is
    no keyboard: stdin is /dev/null, so a prompt aborts the operation). Placed
    on THAT command, never on a `which yay` before it, and never on a
    read-only query. `-Scc` is left alone: its "remove ALL files?" question
    defaults to No, so --noconfirm would silently do nothing."""
    if not cmd:
        return cmd

    def fix(m):
        tool, args = m.group("tool"), m.group("args")
        if "--noconfirm" in args:
            return m.group(0)
        if tool.lower() == "makepkg":
            return (m.group("pre") + tool + " --noconfirm" + args
                    if re.search(r"(?<!\S)-[A-Za-z]*[si][A-Za-z]*(?!\S)", args) else m.group(0))
        toks, shorts = _flags(args)
        op = _op(shorts, args)
        if op not in ("S", "R", "U") or _read_only(shorts, args, op):
            return m.group(0)
        if op == "S" and any(sh.count("c") for sh in shorts):
            return m.group(0)
        return m.group("pre") + tool + " --noconfirm" + args

    return _PAC_CMD.sub(fix, cmd)


def needs_noconfirm(cmd):
    return add_noconfirm(cmd) != cmd
