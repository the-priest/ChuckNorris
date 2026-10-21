#!/usr/bin/env bash
# ============================================================================
#  Chuck installer  —  desktop coding & system agent (Arch / CachyOS first)
#
#  One line:
#    curl -fsSL https://raw.githubusercontent.com/the-priest/ChuckNorris/main/install.sh | bash
#
#  Installs GTK4 + libadwaita (+ the PyGObject cairo bridge), screenshot tool,
#  polkit, pacman-contrib, pciutils, pkgfile, espeak-ng (voice) and yt-dlp
#  (video), then the app, its icon, a launcher and a .desktop entry.
#  SiliconFlow backend (reuses Basilisk's key). Commands run on their own;
#  CRITICAL ones (disk wipes, rm -rf /, curl|sh) wait for your tick.
# ============================================================================
set -euo pipefail

REPO="${CHUCK_REPO:-the-priest/ChuckNorris}"
BRANCH="${CHUCK_BRANCH:-main}"
DATA_DIR="${HOME}/.local/share/chucknorris"
APP_DIR="${DATA_DIR}/app"
ASSET_DIR="${DATA_DIR}/assets"
LAUNCH="${HOME}/.local/bin/chucknorris"
DESKTOP="${HOME}/.local/share/applications/org.thepriest.chucknorris.desktop"

c1=$'\033[38;5;178m'; c2=$'\033[38;5;42m'; cd=$'\033[38;5;245m'; c0=$'\033[0m'
step(){ printf '%s\u25b8 %s%s\n' "$c1" "$1" "$c0"; }
ok(){   printf '%s\u2713 %s%s\n' "$c2" "$1" "$c0"; }
info(){ printf '%s  %s%s\n' "$cd" "$1" "$c0"; }
die(){  printf '\033[38;5;196m\u2717 %s\033[0m\n' "$1" >&2; exit 1; }

mkdir -p "$APP_DIR" "$ASSET_DIR" "${HOME}/.local/bin" "${HOME}/.local/share/applications"

step "installing GTK4 + tools (screenshot, polkit, cleanup, GPU, pkgfile, voice, video)"
SESS="${XDG_SESSION_TYPE:-}"; DE="${XDG_CURRENT_DESKTOP:-}"
if command -v pacman >/dev/null; then
  PKGS="python-gobject python-cairo gtk4 libadwaita adwaita-icon-theme polkit pciutils pacman-contrib pkgfile espeak-ng yt-dlp alsa-utils python-pip whois bind traceroute wget openbsd-netcat nodejs shellcheck ruff bubblewrap"
  if [ "$SESS" = "wayland" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then PKGS="$PKGS grim"; else PKGS="$PKGS scrot"; fi
  case "$DE" in *KDE*|*kde*|*plasma*|*Plasma*) PKGS="$PKGS spectacle" ;; esac
  # shellcheck disable=SC2086
  # -Syu, never -Sy: a bare -Sy installs against a freshly-synced database
  # without upgrading, which is the partial-upgrade footgun the app itself
  # rewrites away. The installer must not do what the app forbids.
  sudo pacman -Syu --needed --noconfirm $PKGS || die "pacman install failed"
  sudo pkgfile --update 2>/dev/null || true
elif command -v apt-get >/dev/null; then
  sudo apt-get update
  PKGS="python3-venv python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 adwaita-icon-theme libgtk-4-1 libadwaita-1-0 policykit-1 pciutils espeak-ng yt-dlp whois dnsutils traceroute wget netcat-openbsd bubblewrap"
  if [ "$SESS" = "wayland" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then PKGS="$PKGS grim"; else PKGS="$PKGS scrot"; fi
  # shellcheck disable=SC2086
  sudo apt-get install -y $PKGS || die "apt install failed"
elif command -v dnf >/dev/null; then
  PKGS="python3-gobject python3-cairo gtk4 libadwaita adwaita-icon-theme polkit pciutils espeak-ng yt-dlp whois bind-utils traceroute wget nmap-ncat bubblewrap"
  if [ "$SESS" = "wayland" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then PKGS="$PKGS grim"; else PKGS="$PKGS scrot"; fi
  # shellcheck disable=SC2086
  sudo dnf install -y $PKGS || die "dnf install failed"
else
  die "unknown package manager — install python3-gi, GTK4, libadwaita, polkit, pciutils, espeak-ng, yt-dlp, grim/scrot"
fi
ok "dependencies ready"

# code verifier linters (best-effort — the verifier degrades gracefully without them)
if ! command -v ruff >/dev/null; then
  pip install --user --break-system-packages ruff >/dev/null 2>&1 \
    || pip install --user pyflakes >/dev/null 2>&1 \
    || info "ruff/pyflakes not installed — Python lint will use the stdlib compiler only"
fi
command -v shellcheck >/dev/null || info "shellcheck not found — shell lint falls back to 'bash -n' (syntax only)"
mkdir -p "${DATA_DIR}/voices"

# Optional: Camoufox, a Firefox build sites can't fingerprint, for ```browse```.
# CHUCK_CAMOUFOX=1 ./install.sh   (then pick Camoufox in Settings > Agent > Browser)
if [ "${CHUCK_CAMOUFOX:-0}" = "1" ]; then
  step "installing Camoufox (optional browser)"
  if pip install --user --break-system-packages -U 'camoufox[geoip]' >/dev/null 2>&1 \
     || pip install --user -U 'camoufox[geoip]' >/dev/null 2>&1; then
    python3 -m camoufox fetch >/dev/null 2>&1 && ok "Camoufox ready" \
      || info "Camoufox installed, but its browser download failed — run: python3 -m camoufox fetch"
  else
    info "Camoufox install skipped (pip failed) — browse will use Brave/Chromium"
  fi
fi
if command -v brave >/dev/null || command -v brave-browser >/dev/null; then
  info "Brave found — browse will use it"
fi

step "installing Chuck"
# Copy a whole source tree into place: the app, its package, its art.
install_tree() {
  cp "$1/chucknorris.py" "${APP_DIR}/chucknorris.py"
  rm -rf "${APP_DIR}/chucknorris_ext"
  cp -r "$1/chucknorris_ext" "${APP_DIR}/chucknorris_ext"
  rm -rf "${APP_DIR}/chucknorris_ext/__pycache__"
  cp -r "$1"/assets/* "${ASSET_DIR}/" 2>/dev/null || true
}
SRC_TMP="$(mktemp -d)"
trap 'rm -rf "$SRC_TMP"' EXIT
# A local tree only when THIS script was run from a clone (./install.sh):
# piped from curl, BASH_SOURCE is empty, and a chucknorris.py that merely
# happens to be in the current folder (an old clone) must not be installed
# over the latest release.
SELF_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/chucknorris.py" ] && [ -d "$SELF_DIR/chucknorris_ext" ]; then
  install_tree "$SELF_DIR"                           # running from a clone
elif curl -fsSL "https://codeload.github.com/${REPO}/tar.gz/refs/heads/${BRANCH}" \
        -o "${SRC_TMP}/src.tar.gz" \
     && tar -xzf "${SRC_TMP}/src.tar.gz" -C "$SRC_TMP" \
     && SRC_DIR="$(find "$SRC_TMP" -mindepth 1 -maxdepth 1 -type d | head -n 1)" \
     && [ -f "${SRC_DIR}/chucknorris.py" ] && [ -d "${SRC_DIR}/chucknorris_ext" ]; then
  # ONE archive of the branch, so every file comes from the same commit.
  # Fetching files one by one from raw.githubusercontent.com could mix
  # versions: its CDN caches each file separately for minutes, so right after
  # a release you could get the new app with an old module list — and an app
  # that would not start.
  install_tree "$SRC_DIR"
else
  info "archive download failed — falling back to fetching files one by one"
  curl -fsSL "https://raw.githubusercontent.com/${REPO}/${BRANCH}/chucknorris.py" \
    -o "${APP_DIR}/chucknorris.py" || die "could not fetch chucknorris.py"
  mkdir -p "${APP_DIR}/chucknorris_ext"
  # config/net/safety/web/voice/chats are REQUIRED — the app won't import
  # without them, so a missing one must be a hard error, not a quiet "skipped".
  #
  # This list is hand-maintained and it has drifted before: ledger.py and
  # compress.py both shipped in 12.0.3 and were never added here, so every
  # curl-installed copy silently ran with no evidence ledger and no context
  # compression while the git-cloned copy had both. If you add a module, add it
  # HERE as well — the check below exists because that is easy to forget.
  for f in __init__ config net safety web voice chats pricing \
           skills specs memory codecheck skill_library builder \
           ledger compress agents browser diagnose mcp crashlog update pysession lsp codesearch checkpoint listen kokoro_server stt_server kernel cow worker daemons governor programs hibernate missions budget; do
    curl -fsSL "https://raw.githubusercontent.com/${REPO}/${BRANCH}/chucknorris_ext/${f}.py" \
      -o "${APP_DIR}/chucknorris_ext/${f}.py" \
      || case "$f" in
           __init__|config|net|safety|web|voice|chats|pricing)
             die "could not fetch required module chucknorris_ext/${f}.py" ;;
           *) info "could not fetch chucknorris_ext/${f}.py (that feature will be limited)" ;;
         esac
  done
  for a in chucknorris-icon gi-mark \
           chucknorris-icon-256 chucknorris-icon-128 chucknorris-icon-64 \
           chucknorris-icon-48 chucknorris-icon-32; do
    curl -fsSL "https://raw.githubusercontent.com/${REPO}/${BRANCH}/assets/${a}.png" \
      -o "${ASSET_DIR}/${a}.png" 2>/dev/null || true
  done
fi
# Every module the app imports must be here. A partial install is caught NOW,
# with a clear message, instead of as a traceback the first time you launch.
missing=""
for m in $(grep -oE '(from chucknorris_ext import [a-z_]+|_optional\("[a-z_]+"\))' \
             "${APP_DIR}/chucknorris.py" | grep -oE '[a-z_]+("?\))?$' | tr -d '")' | sort -u); do
  [ -f "${APP_DIR}/chucknorris_ext/${m}.py" ] || missing="${missing} ${m}"
done
[ -z "$missing" ] || die "install incomplete — missing modules:${missing}. Re-run the installer."
python3 -m py_compile "${APP_DIR}/chucknorris.py" "${APP_DIR}"/chucknorris_ext/*.py \
  || die "a downloaded file is corrupt (did not compile). Re-run the installer."
ok "app installed"

# The natural voice (Kokoro) and voice input (Moonshine): both run on this
# machine. Installed by the app's own, tested code, in a private venv.
if [ "${CHUCK_VOICE:-1}" = "1" ]; then
  step "setting up the natural voice + voice input (Kokoro + Moonshine, about 600 MB, all local)"
  info "this is the big download; CHUCK_VOICE=0 skips it (then: /voice in the app)"
  ( cd "${APP_DIR}" && python3 - <<'PYEOF'
import sys
from chucknorris_ext import voice, listen
def say(m):
    end = "\r" if m.startswith("downloading") else "\n"
    sys.stdout.write("\r\033[K  " + m + end)
    sys.stdout.flush()
try:
    ok1, m1 = voice.install_kokoro(say)
    say(("✓ " if ok1 else "✗ ") + m1)
    ok2, m2 = listen.install(say)
    say(("✓ " if ok2 else "✗ ") + m2)
finally:
    voice.KOKORO.stop()
    listen.EARS.stop()
PYEOF
  ) || info "voice setup didn't finish — run /voice in the app to retry"
else
  info "voice setup skipped (CHUCK_VOICE=0) — /voice in the app installs it"
fi

cat > "$LAUNCH" <<EOF
#!/usr/bin/env bash
exec python3 "${APP_DIR}/chucknorris.py" "\$@"
EOF
chmod +x "$LAUNCH"
cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Chuck
GenericName=AI Agent
Comment=A coding and system agent for your desktop: runs commands, edits code, researches, sees screenshots
Exec=${LAUNCH}
Icon=org.thepriest.chucknorris
Terminal=false
Categories=Utility;System;
Keywords=arch;cachyos;assistant;fixer;research;news;images;video;code;pacman;searxng;
StartupNotify=true
EOF
update-desktop-database "${HOME}/.local/share/applications" 2>/dev/null || true
if [ -f "${ASSET_DIR}/chucknorris-icon.png" ]; then
  # install every generated size so the taskbar/dock picks a crisp one
  for SZ in 512 256 128 64 48 32; do
    IDIR="${HOME}/.local/share/icons/hicolor/${SZ}x${SZ}/apps"
    mkdir -p "$IDIR"
    if [ "$SZ" = "512" ]; then
      SRC="${ASSET_DIR}/chucknorris-icon.png"
    else
      SRC="${ASSET_DIR}/chucknorris-icon-${SZ}.png"
    fi
    [ -f "$SRC" ] && cp "$SRC" "${IDIR}/org.thepriest.chucknorris.png"
  done
  gtk-update-icon-cache -f "${HOME}/.local/share/icons/hicolor" 2>/dev/null || true
fi

case ":$PATH:" in *":${HOME}/.local/bin:"*) : ;; *)
  info "add to PATH:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;; esac

printf '\n%sChuck installed.%s  Launch:  %schucknorris%s\n' "$c1" "$c0" "$c2" "$c0"
info "add a SiliconFlow key in Settings (or it reuses Basilisk's). Tab switches Agent / Chat."
