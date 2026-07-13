#!/usr/bin/env bash
# ============================================================================
#  Chuck Norris installer  —  Arch / CachyOS grandmaster assistant
#
#  One line:
#    curl -fsSL https://raw.githubusercontent.com/the-priest/ChuckNorris/main/install.sh | bash
#
#  Installs GTK4 + libadwaita, screenshot tool (eyes), polkit (approved fixes),
#  pacman-contrib (cleanup), pciutils (GPU detection) and pkgfile (tool lookup),
#  then the app + button art + Tux background + launcher + .desktop.
#  Talks to SiliconFlow (reuses Basilisk's key if present). Non-autonomous:
#  you approve every command. Fixes, installs, researches, verifies news, reads
#  files — but never tracks people.
# ============================================================================
set -euo pipefail

REPO="${CHUCK_REPO:-the-priest/ChuckNorris}"
BRANCH="${CHUCK_BRANCH:-main}"
DATA_DIR="${HOME}/.local/share/chucknorris"
APP_DIR="${DATA_DIR}/app"
ASSET_DIR="${DATA_DIR}/assets"
LAUNCH="${HOME}/.local/bin/chucknorris"
DESKTOP="${HOME}/.local/share/applications/org.thepriest.chucknorris.desktop"

c1=$'\033[38;5;44m'; c2=$'\033[38;5;42m'; cd=$'\033[38;5;245m'; c0=$'\033[0m'
step(){ printf '%s\u25b8 %s%s\n' "$c1" "$1" "$c0"; }
ok(){   printf '%s\u2713 %s%s\n' "$c2" "$1" "$c0"; }
info(){ printf '%s  %s%s\n' "$cd" "$1" "$c0"; }
die(){  printf '\033[38;5;196m\u2717 %s\033[0m\n' "$1" >&2; exit 1; }

mkdir -p "$APP_DIR" "$ASSET_DIR" "${HOME}/.local/bin" "${HOME}/.local/share/applications"

step "installing GTK4 + libadwaita + tools (screenshot, polkit, cleanup, GPU, pkgfile)"
SESS="${XDG_SESSION_TYPE:-}"; DE="${XDG_CURRENT_DESKTOP:-}"
if command -v pacman >/dev/null; then
  PKGS="python-gobject python-cairo gtk4 libadwaita polkit pciutils pacman-contrib pkgfile"
  if [ "$SESS" = "wayland" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then PKGS="$PKGS grim"; else PKGS="$PKGS scrot"; fi
  case "$DE" in *KDE*|*kde*|*plasma*|*Plasma*) PKGS="$PKGS spectacle" ;; esac
  # shellcheck disable=SC2086
  sudo pacman -Sy --needed --noconfirm $PKGS || die "pacman install failed"
  sudo pkgfile --update 2>/dev/null || true
elif command -v apt-get >/dev/null; then
  sudo apt-get update
  PKGS="python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 libgtk-4-1 libadwaita-1-0 policykit-1 pciutils"
  if [ "$SESS" = "wayland" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then PKGS="$PKGS grim"; else PKGS="$PKGS scrot"; fi
  # shellcheck disable=SC2086
  sudo apt-get install -y $PKGS || die "apt install failed"
elif command -v dnf >/dev/null; then
  PKGS="python3-gobject gtk4 libadwaita polkit pciutils"
  if [ "$SESS" = "wayland" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then PKGS="$PKGS grim"; else PKGS="$PKGS scrot"; fi
  # shellcheck disable=SC2086
  sudo dnf install -y $PKGS || die "dnf install failed"
else
  die "unknown package manager — install python3-gi, GTK4, libadwaita, polkit, pciutils, grim/scrot"
fi
ok "dependencies ready"

step "installing Chuck Norris + assets"
if [ -f chucknorris.py ]; then
  cp chucknorris.py "${APP_DIR}/chucknorris.py"
  cp -r assets/* "${ASSET_DIR}/" 2>/dev/null || true
else
  curl -fsSL "https://raw.githubusercontent.com/${REPO}/${BRANCH}/chucknorris.py" \
    -o "${APP_DIR}/chucknorris.py" || die "could not fetch chucknorris.py"
  for a in chucknorris-bg chucknorris-icon basilisk-btn-close basilisk-btn-minimise \
           basilisk-btn-expand basilisk-btn-settings basilisk-btn-camera basilisk-btn-attach; do
    curl -fsSL "https://raw.githubusercontent.com/${REPO}/${BRANCH}/assets/${a}.png" \
      -o "${ASSET_DIR}/${a}.png" 2>/dev/null || true
  done
fi
ok "app + assets installed"

cat > "$LAUNCH" <<EOF
#!/usr/bin/env bash
exec python3 "${APP_DIR}/chucknorris.py" "\$@"
EOF
chmod +x "$LAUNCH"
cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Chuck Norris
GenericName=Arch/CachyOS Assistant
Comment=Arch/CachyOS grandmaster: fixes, installs, researches, verifies news, reads files
Exec=${LAUNCH}
Icon=org.thepriest.chucknorris
Terminal=false
Categories=Utility;System;
Keywords=arch;cachyos;assistant;fixer;research;news;drivers;pacman;systemd;
StartupNotify=true
EOF
update-desktop-database "${HOME}/.local/share/applications" 2>/dev/null || true
if [ -f "${ASSET_DIR}/chucknorris-icon.png" ]; then
  ICON="${HOME}/.local/share/icons/hicolor/512x512/apps"
  mkdir -p "$ICON"; cp "${ASSET_DIR}/chucknorris-icon.png" "${ICON}/org.thepriest.chucknorris.png" 2>/dev/null || true
fi

case ":$PATH:" in *":${HOME}/.local/bin:"*) : ;; *)
  info "add to PATH:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;; esac

printf '\n%s\U0001F94B Chuck Norris installed.%s  Launch:  %schucknorris%s\n' "$c1" "$c0" "$c2" "$c0"
info "add a SiliconFlow key in Settings (or it reuses Basilisk's), then ask Chuck anything."
