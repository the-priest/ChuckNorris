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
# a "root-ish" target: / itself, /*, ~, $HOME, a bare wildcard, or cwd
_NUKE_TARGET = r"(?:/|/\*|~|~/\*|\$HOME(?:/\*)?|\*|\.)"
# rm with recursive intent, short cluster (-rf/-fr/-Rf) or long flags
_RM_REC = r"\brm\s+(?:(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive|--force|-[a-zA-Z]*f[a-zA-Z]*)\s+)+"

CRITICAL = re.compile(
    # rm -rf aimed at root / home / bare wildcard
    _RM_REC + _NUKE_TARGET + r"\s*(?:$|[;&|])"
    r"|--no-preserve-root"
    # filesystem / partition / crypto destruction
    r"|\bmkfs(?:\.[a-z0-9]+)?\b|\bwipefs\b|\bblkdiscard\b|\bshred\b"
    r"|\bcryptsetup\s+(?:luksFormat|erase)\b"
    r"|\b(?:parted|fdisk|sgdisk|cfdisk|gdisk)\b[^|;]*" + _DEV +
    r"|\bsgdisk\b[^|;]*--zap-all"
    # raw writes to a block device
    r"|\bdd\b[^|;]*\bof=" + _DEV +
    r"|>\s*" + _DEV +
    # remote code execution: curl/wget piped into a shell
    r"|\b(?:curl|wget)\b[^|;&]*\|\s*(?:sudo\s+)?(?:ba|z|k)?sh\b"
    # clobbering critical system files
    r"|>\s*/etc/(?:passwd|shadow|sudoers|fstab)\b"
    r"|\btruncate\s+-s\s*0\s+/(?:etc|boot)/"
    r"|\bmv\s+(?:/|/etc|/boot|/home|~)\S*\s+/dev/null\b"
    # mass delete via find on a broad path
    r"|\bfind\s+(?:/|~|\$HOME)\S*[^|;]*-delete\b"
    r"|\bfind\s+(?:/|~|\$HOME)\S*[^|;]*-exec\s+rm\b"
    # fork bomb
    r"|:\(\)\s*\{"
    # ripping out core packages
    r"|\bpacman\s+-R[a-z]*\s+[^|;]*\b(?:systemd|glibc|linux|bash|coreutils|pacman)\b"
    # recursive permission/ownership destruction from root
    r"|\bchmod\s+-R\s+[0-7]{3,4}\s+/\s*(?:$|[;&|])"
    r"|\bchown\s+-R\s+\S+\s+/\s*(?:$|[;&|])"
    # account destruction
    r"|\buserdel\b|\bpasswd\s+-d\b",
    re.IGNORECASE)

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
    re.IGNORECASE)


def classify_command(cmd):
    """Return 'critical', 'danger', or '' for a shell command string.
    Static and fast — this runs on every card, so it must never be slow."""
    if not cmd:
        return ""
    if CRITICAL.search(cmd):
        return "critical"
    if DANGER.search(cmd):
        return "danger"
    return ""


_PAC_INSTALL = re.compile(
    r"\bpacman\s+(?P<flags>-{1,2}[A-Za-z-]+(?:\s+-{1,2}[A-Za-z-]+)*)", re.IGNORECASE)


def enforce_syu(cmd):
    """Rewrite `pacman -S pkg` into `pacman -Syu pkg`.

    On Arch a plain -S installs against a stale local database, which is the
    classic partial-upgrade footgun: you end up with a package built for a newer
    libc than the one you have, and things break in ways that are miserable to
    unpick. Syncing first is the only supported way to install. This is enforced
    here rather than merely requested in the prompt, because a rule the model
    can forget isn't a rule.
    """
    if not cmd or "pacman" not in cmd:
        return cmd

    # Sync sub-operations that only READ (search/info/list/clean/groups/print/
    # download-only) must be left completely alone — turning `-Ss` into `-Syus`
    # would break a harmless package search.
    READONLY = set("silcgpw")
    OTHER_OPS = set("RQUDFT")

    def fix(m):
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
        return "pacman " + " ".join(out) if changed else m.group(0)

    return _PAC_INSTALL.sub(fix, cmd)
