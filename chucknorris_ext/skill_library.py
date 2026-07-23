"""skill_library.py — a curated library of ready-made skills Chuck ships with.

These are real, tested recipes for hard Arch/CachyOS and recon/security tasks —
the kind of thing you'd otherwise have to look up every time. On first run the
app seeds them into the user's skills store (idempotent: won't clobber user
edits or re-add on every launch, tracked by a version marker). Chuck sees them
in his skills index and can run any with ```runskill <name>```.

Every skill is bash (portable, no runtime deps beyond what the recipe uses) and
read-only-or-safe by default; anything that changes the system still lands as an
approve-to-run card in the UI. Destructive steps are commented, never live.
"""

# name -> (description, body). Bodies are bash. Keep them defensive: check for
# the tool, prefer read-only, explain via echo.
LIBRARY = {
    # ── Arch / CachyOS ─────────────────────────────────────────────────────
    "arch-health": (
        "Full system health check: failed units, journal errors, pacman state, disk, mem",
        r"""#!/usr/bin/env bash
# Read-only system health snapshot.
echo "== failed systemd units =="
systemctl --failed --no-legend || true
echo; echo "== recent journal errors (last boot) =="
journalctl -p 3 -b --no-pager 2>/dev/null | tail -15 || true
echo; echo "== pacman: orphans =="
pacman -Qtdq 2>/dev/null | wc -l | xargs echo "orphan count:"
echo "== pacman: foreign/AUR pkgs =="
pacman -Qmq 2>/dev/null | wc -l | xargs echo "foreign count:"
echo; echo "== disk =="
df -h / /home 2>/dev/null | grep -vE '^tmpfs'
echo; echo "== memory =="
free -h
echo; echo "== top 5 RAM hogs =="
ps aux --sort=-%mem | awk 'NR<=6{printf "%-8s %5s%%  %s\n",$1,$4,$11}'
""",
    ),
    "arch-update-safe": (
        "Safe full system update: refresh keyring first, full -Syu, AUR, then orphan report",
        r"""#!/usr/bin/env bash
# Safe update sequence for Arch/CachyOS. Uses sudo — you approve the card.
set -e
echo ">> refreshing keyring (prevents signature errors)"
sudo pacman -Sy --needed --noconfirm archlinux-keyring 2>/dev/null || true
command -v cachyos-keyring >/dev/null 2>&1 && sudo pacman -S --needed --noconfirm cachyos-keyring 2>/dev/null || true
echo ">> full system upgrade"
sudo pacman -Syu --noconfirm
if command -v paru >/dev/null; then echo ">> AUR (paru)"; paru -Sua --noconfirm || true
elif command -v yay >/dev/null; then echo ">> AUR (yay)"; yay -Sua --noconfirm || true; fi
echo ">> orphans (review before removing):"
pacman -Qtdq 2>/dev/null || echo "none"
""",
    ),
    "arch-keyring-fix": (
        "Repair broken pacman keyring / PGP signature errors",
        r"""#!/usr/bin/env bash
set -e
echo ">> reinitialising pacman keyring"
sudo rm -rf /etc/pacman.d/gnupg 2>/dev/null || true
sudo pacman-key --init
sudo pacman-key --populate archlinux
command -v cachyos-keyring >/dev/null 2>&1 && sudo pacman-key --populate cachyos 2>/dev/null || true
sudo pacman -Sy --needed --noconfirm archlinux-keyring
echo ">> keyring rebuilt — retry your install now"
""",
    ),
    "arch-mirror-rank": (
        "Rank the fastest pacman mirrors for your location (CachyOS-aware)",
        r"""#!/usr/bin/env bash
if command -v cachyos-rate-mirrors >/dev/null; then
  echo ">> ranking CachyOS + Arch mirrors"
  sudo cachyos-rate-mirrors
elif command -v rate-mirrors >/dev/null; then
  sudo sh -c 'rate-mirrors --allow-root arch > /etc/pacman.d/mirrorlist'
elif command -v reflector >/dev/null; then
  sudo reflector --latest 20 --sort rate --protocol https --save /etc/pacman.d/mirrorlist
else
  echo "install one first: sudo pacman -S reflector   (or cachyos-rate-mirrors on CachyOS)"
fi
""",
    ),
    "arch-biggest-packages": (
        "List your 15 largest installed packages by disk size",
        r"""#!/usr/bin/env bash
if command -v expac >/dev/null; then
  expac -H M '%m\t%n' | sort -h | tail -15
else
  echo "needs 'expac': sudo pacman -S expac"
  echo "fallback (slower):"
  pacman -Qi | awk '/^Name/{n=$3} /^Installed Size/{print $4$5, n}' | sort -h | tail -15
fi
""",
    ),
    "arch-cleanup": (
        "Reclaim disk: trim pacman cache, drop orphans, vacuum journal (shows sizes first)",
        r"""#!/usr/bin/env bash
echo "== current usage =="
du -sh /var/cache/pacman/pkg 2>/dev/null | awk '{print "pacman cache: "$1}'
journalctl --disk-usage 2>/dev/null
echo "== actions =="
command -v paccache >/dev/null && sudo paccache -rk1 || echo "install pacman-contrib for paccache"
ORPH=$(pacman -Qtdq 2>/dev/null)
[ -n "$ORPH" ] && echo "$ORPH" | sudo pacman -Rns - --noconfirm || echo "no orphans"
sudo journalctl --vacuum-size=200M
""",
    ),
    "arch-boot-repair-notes": (
        "Print the chroot boot-rescue sequence (for a live USB) — reference, does not run it",
        r"""#!/usr/bin/env bash
cat <<'NOTES'
BOOT RESCUE (from a live USB) — adapt device names:
  1. lsblk                                  # find your root + EFI partitions
  2. sudo mount /dev/sdXn /mnt              # root
  3. sudo mount /dev/sdXe /mnt/boot         # EFI/boot
  4. sudo arch-chroot /mnt
  5. inside: mkinitcpio -P                  # rebuild initramfs
  6. inside (GRUB): grub-mkconfig -o /boot/grub/grub.cfg
     inside (systemd-boot): bootctl update
  7. exit; sudo umount -R /mnt; reboot
NOTES
""",
    ),
    "gpu-info": (
        "Show GPU, driver, and whether the right mesa/vulkan/nvidia bits are installed",
        r"""#!/usr/bin/env bash
echo "== GPU hardware =="
lspci | grep -iE 'vga|3d|display' | sed 's/.*: //'
echo; echo "== kernel driver in use =="
lspci -k | grep -A2 -iE 'vga|3d' | grep -i 'kernel driver' || true
echo; echo "== renderer =="
command -v glxinfo >/dev/null && glxinfo 2>/dev/null | grep -i 'OpenGL renderer' || echo "install mesa-utils for glxinfo"
command -v vulkaninfo >/dev/null && vulkaninfo 2>/dev/null | grep -i 'deviceName' | head -1 || echo "install vulkan-tools for vulkaninfo"
""",
    ),
    # ── recon / security (all on targets you're authorised to test) ─────────
    "recon-domain": (
        "Full passive recon of a domain: WHOIS, DNS, TLS cert, HTTP headers. Usage: edit TARGET",
        r"""#!/usr/bin/env bash
TARGET="${1:-example.com}"
echo "== WHOIS =="; whois "$TARGET" 2>/dev/null | grep -iE 'registrar|creation|expir|name server' | head -10
echo; echo "== DNS =="; dig +short "$TARGET" A; dig +short "$TARGET" AAAA; dig +short "$TARGET" MX
echo; echo "== NS =="; dig +short "$TARGET" NS
echo; echo "== TLS cert =="
echo | openssl s_client -connect "$TARGET":443 -servername "$TARGET" 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates 2>/dev/null
echo; echo "== HTTP headers =="; curl -sSIL "https://$TARGET" 2>/dev/null | head -15
""",
    ),
    "recon-ip": (
        "Geolocate + ASN + reverse-DNS an IP (server infra recon). Usage: edit TARGET",
        r"""#!/usr/bin/env bash
IP="${1:-8.8.8.8}"
echo "== geo / ASN (ipinfo) =="; curl -s "ipinfo.io/$IP" 2>/dev/null
echo; echo "== reverse DNS =="; dig -x "$IP" +short
echo; echo "== ASN (team cymru) =="; whois -h whois.cymru.com " -v $IP" 2>/dev/null | tail -1
""",
    ),
    "net-listening": (
        "What's listening on this machine + active connections (local security audit)",
        r"""#!/usr/bin/env bash
echo "== listening sockets =="
ss -tulpn 2>/dev/null || sudo ss -tulpn
echo; echo "== established connections =="
ss -tp state established 2>/dev/null | head -20
""",
    ),
    "http-headers-audit": (
        "Check a site's security headers (HSTS, CSP, X-Frame, etc). Usage: edit URL",
        r"""#!/usr/bin/env bash
URL="${1:-https://example.com}"
H=$(curl -sSIL "$URL" 2>/dev/null)
check(){ echo "$H" | grep -qi "^$1:" && echo "  [+] $1 present" || echo "  [-] $1 MISSING"; }
echo "Security headers for $URL:"
check "Strict-Transport-Security"
check "Content-Security-Policy"
check "X-Frame-Options"
check "X-Content-Type-Options"
check "Referrer-Policy"
check "Permissions-Policy"
""",
    ),
    "wifi-scan": (
        "Scan nearby WiFi networks with signal + security (needs nmcli)",
        r"""#!/usr/bin/env bash
if command -v nmcli >/dev/null; then
  nmcli -f SSID,SIGNAL,SECURITY,CHAN dev wifi list 2>/dev/null | head -25
else
  echo "needs NetworkManager (nmcli). On iw-only systems: sudo iw dev <iface> scan | grep SSID"
fi
""",
    ),
    "port-scan-local": (
        "Quick TCP port check on a host you own. Usage: edit HOST (defaults to localhost)",
        r"""#!/usr/bin/env bash
HOST="${1:-127.0.0.1}"
if command -v nmap >/dev/null; then
  echo ">> nmap top-1000 on $HOST (authorised targets only)"
  nmap -T4 -F "$HOST"
else
  echo ">> nmap not installed; bash fallback on common ports"
  for p in 22 80 443 3306 5432 8080 8443; do
    (echo > "/dev/tcp/$HOST/$p") >/dev/null 2>&1 && echo "  $p open" || true
  done
fi
""",
    ),
}


def seed_into(skills_module, marker_path):
    """Idempotently seed the library into the user's skills store.

    marker_path: a Path the app owns; we write the library VERSION there so we
    don't re-seed every launch. Returns the number of skills newly added.
    Never overwrites a skill the user already has by that name.
    """
    VERSION = "1"
    try:
        if marker_path.exists() and marker_path.read_text().strip() == VERSION:
            return 0
    except Exception:
        pass
    existing = {n for (n, _l, _d) in skills_module.skill_list()}
    added = 0
    for name, (desc, body) in LIBRARY.items():
        if name in existing:
            continue
        ok, _msg, _cmd = skills_module.skill_write(name, "bash", body, desc)
        if ok:
            added += 1
    try:
        marker_path.write_text(VERSION)
    except Exception:
        pass
    return added
