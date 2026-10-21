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
    r"|\bpacman\s+-R[a-z]*\s+[^\n|;]*(?<![\w-])(?:systemd|glibc|linux|bash|coreutils|pacman)(?![\w-])"
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
    r"|\bpacman\s+-R"
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
# `paru -S pkg` is the same partial-upgrade footgun as `pacman -S pkg`. Anchoring
# this on "pacman" alone let every AUR install through un-rewritten.
_PAC_TOOLS = r"(?:pacman|paru|yay)"
_PAC_INSTALL = re.compile(
    r"\b(?P<tool>" + _PAC_TOOLS + r")\s+(?P<flags>-{1,2}[A-Za-z-]+(?:\s+-{1,2}[A-Za-z-]+)*)",
    re.IGNORECASE)


def enforce_syu(cmd):
    """Rewrite `pacman -S pkg` into `pacman -Syu pkg` (also paru / yay).

    On Arch a plain -S installs against a stale local database, which is the
    classic partial-upgrade footgun: you end up with a package built for a newer
    libc than the one you have, and things break in ways that are miserable to
    unpick. Syncing first is the only supported way to install. This is enforced
    here rather than merely requested in the prompt, because a rule the model
    can forget isn't a rule.
    """
    if not cmd:
        return cmd
    if not re.search(r"\b" + _PAC_TOOLS + r"\b", cmd, re.IGNORECASE):
        return cmd

    # Sync sub-operations that only READ (search/info/list/clean/groups/print/
    # download-only) must be left completely alone — turning `-Ss` into `-Syus`
    # would break a harmless package search.
    READONLY = set("silcgpw")
    OTHER_OPS = set("RQUDFT")

    def fix(m):
        tool = m.group("tool")
        parts = m.group("flags").split()
        out, changed = [], False
        for p_ in parts:
            if p_.startswith("--"):
                out.append(p_); continue
            body = p_[1:]
            if "S" not in body or OTHER_OPS & set(body) or READONLY & set(body):
                out.append(p_); continue
            if "y" in body and "u" in body:
                out.append(p_); continue      # already correct (incl. -Syyu)
            # a real install: make sure it syncs AND upgrades first
            rest = "".join(c for c in body if c not in "Syu")
            out.append("-Syu" + rest); changed = True
        return f"{tool} " + " ".join(out) if changed else m.group(0)

    return _PAC_INSTALL.sub(fix, cmd)
