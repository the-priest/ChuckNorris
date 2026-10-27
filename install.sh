#!/usr/bin/env bash
# ============================================================================
#  Chuck installer  —  desktop coding & system agent (Arch / CachyOS first)
#
#  One line:
#    curl -fsSL https://raw.githubusercontent.com/the-priest/ChuckNorris/main/install.sh | bash
#
#  Chuck IS the Rust app: it is built from the branch into
#  ~/.local/bin/chuck, and the menu entry and the `chucknorris` command open
#  it, replacing whatever Chuck was installed before. The Python app is still
#  installed alongside (the voice setup uses it); it is what opens only if the
#  Rust build fails, and the installer says so loudly. CHUCK_RUST=0 skips it.
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
# the Rust rewrite lived on its own branch until it became main: installs
# made from it (their launcher says so) now follow main
[ "$BRANCH" = "rust-rewrite" ] && BRANCH="main"
DATA_DIR="${HOME}/.local/share/chucknorris"
APP_DIR="${DATA_DIR}/app"
ASSET_DIR="${DATA_DIR}/assets"
LAUNCH="${HOME}/.local/bin/chucknorris"
DESKTOP="${HOME}/.local/share/applications/org.thepriest.chucknorris.desktop"

# colours only on a terminal, and never with NO_COLOR set (the in-app updater reads this output)
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  c1=$'\033[38;5;178m'; c2=$'\033[38;5;42m'; cd=$'\033[38;5;245m'; cr=$'\033[38;5;196m'; c0=$'\033[0m'
else
  c1=""; c2=""; cd=""; cr=""; c0=""
fi
step(){ printf '%s▸ %s%s\n' "$c1" "$1" "$c0"; }
ok(){   printf '%s✓ %s%s\n' "$c2" "$1" "$c0"; }
info(){ printf '%s  %s%s\n' "$cd" "$1" "$c0"; }
die(){  printf '%s✗ %s%s\n' "$cr" "$1" "$c0" >&2; exit 1; }

mkdir -p "$APP_DIR" "$ASSET_DIR" "${HOME}/.local/bin" "${HOME}/.local/share/applications"

if [ "${CHUCK_DEPS:-1}" = "0" ]; then
  info "system packages skipped (CHUCK_DEPS=0: an update from inside Chuck)"
else
step "installing GTK4 + tools (screenshot, polkit, cleanup, GPU, pkgfile, voice, video)"
SESS="${XDG_SESSION_TYPE:-}"; DE="${XDG_CURRENT_DESKTOP:-}"
if command -v pacman >/dev/null; then
  PKGS="gcc pkgconf python-gobject python-cairo gtk4 libadwaita adwaita-icon-theme polkit pciutils pacman-contrib pkgfile espeak-ng yt-dlp alsa-utils python-pip whois bind traceroute wget openbsd-netcat nodejs shellcheck ruff bubblewrap gtk3 dbus-glib libxt nss"
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
  PKGS="build-essential pkg-config libgtk-4-dev libadwaita-1-dev python3-venv python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 adwaita-icon-theme libgtk-4-1 libadwaita-1-0 policykit-1 pciutils espeak-ng yt-dlp whois dnsutils traceroute wget netcat-openbsd bubblewrap"
  if [ "$SESS" = "wayland" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then PKGS="$PKGS grim"; else PKGS="$PKGS scrot"; fi
  # shellcheck disable=SC2086
  sudo apt-get install -y $PKGS || die "apt install failed"
  # what Camoufox (Firefox) needs; a name a release doesn't have must not stop the install
  sudo apt-get install -y libgtk-3-0 libdbus-glib-1-2 libxt6 libasound2 libnss3 >/dev/null 2>&1 \
    || info "some of Camoufox's libraries didn't install (libgtk-3-0 libdbus-glib-1-2 libxt6 libasound2) — it may not start"
elif command -v dnf >/dev/null; then
  PKGS="gcc pkgconf-pkg-config gtk4-devel libadwaita-devel python3-gobject python3-cairo gtk4 libadwaita adwaita-icon-theme polkit pciutils espeak-ng yt-dlp whois bind-utils traceroute wget nmap-ncat bubblewrap"
  if [ "$SESS" = "wayland" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then PKGS="$PKGS grim"; else PKGS="$PKGS scrot"; fi
  # shellcheck disable=SC2086
  sudo dnf install -y $PKGS || die "dnf install failed"
  sudo dnf install -y gtk3 dbus-glib libXt alsa-lib nss >/dev/null 2>&1 \
    || info "some of Camoufox's libraries didn't install (gtk3 dbus-glib libXt alsa-lib) — it may not start"
else
  die "unknown package manager — install python3-gi, GTK4, libadwaita, polkit, pciutils, espeak-ng, yt-dlp, grim/scrot"
fi
ok "dependencies ready"
fi

# code verifier linters (best-effort — the verifier degrades gracefully without them)
if ! command -v ruff >/dev/null; then
  pip install --user --break-system-packages ruff >/dev/null 2>&1 \
    || pip install --user pyflakes >/dev/null 2>&1 \
    || info "ruff/pyflakes not installed — Python lint will use the stdlib compiler only"
fi
command -v shellcheck >/dev/null || info "shellcheck not found — shell lint falls back to 'bash -n' (syntax only)"
mkdir -p "${DATA_DIR}/voices"

# Camoufox: a Firefox build sites can't tell from a person, the browser Chuck
# reads blocked pages with. In an environment of its own (no system Python is
# touched). Set up once: an install or update finds it ready and leaves it
# alone (upgrading the package would make it want a different browser build,
# which it would then try to download on every page Chuck reads).
# CHUCK_CAMOUFOX=0 skips it; CHUCK_CAMOUFOX_UPGRADE=1 moves it to the newest release.
CF_VENV="${DATA_DIR}/camoufox"
CF_LOG="${DATA_DIR}/camoufox-setup.log"
# is the package there with the browser build it pairs with? (looks only: downloads nothing)
cf_ready() {
  [ -x "${CF_VENV}/bin/python" ] && "${CF_VENV}/bin/python" - >/dev/null 2>&1 <<'PY'
from camoufox.pkgman import camoufox_path, launch_path
path = camoufox_path(download_if_missing=False)
try:
    launch_path(path)
except TypeError:
    launch_path()
PY
}
if [ "${CHUCK_CAMOUFOX:-1}" = "1" ]; then
  if cf_ready && [ "${CHUCK_CAMOUFOX_UPGRADE:-0}" != "1" ]; then
    info "Camoufox: already set up"
  else
    step "setting up Camoufox (the browser for pages that turn bots away; about 300 MB, once)"
    : >"$CF_LOG"
    cf_ok=1
    if [ ! -x "${CF_VENV}/bin/python" ]; then
      { python3 -m venv "$CF_VENV" && "${CF_VENV}/bin/python" -m pip install -q -U pip; } >>"$CF_LOG" 2>&1 || cf_ok=0
    fi
    if [ "$cf_ok" = "1" ]; then
      if [ "${CHUCK_CAMOUFOX_UPGRADE:-0}" = "1" ]; then
        "${CF_VENV}/bin/python" -m pip install -q -U 'camoufox[geoip]' >>"$CF_LOG" 2>&1 || cf_ok=0
      elif ! "${CF_VENV}/bin/python" -c "import camoufox" >/dev/null 2>&1; then
        "${CF_VENV}/bin/python" -m pip install -q 'camoufox[geoip]' >>"$CF_LOG" 2>&1 || cf_ok=0
      fi
    fi
    # the browser build this package pairs with (and its add-on): only when it isn't there
    if [ "$cf_ok" = "1" ] && ! cf_ready; then
      "${CF_VENV}/bin/python" -m camoufox fetch >>"$CF_LOG" 2>&1 || true
    fi
    if cf_ready; then
      ok "Camoufox ready"
    elif [ "$cf_ok" = "1" ]; then
      info "Camoufox is installed, but its browser didn't download (see ${CF_LOG}) — run: ${CF_VENV}/bin/python -m camoufox fetch"
    else
      info "Camoufox couldn't be set up (see ${CF_LOG}) — Chuck uses Brave/Chromium/Chrome instead"
    fi
  fi
fi
BROWSER_FOUND=""
for b in brave brave-browser chromium chromium-browser google-chrome google-chrome-stable microsoft-edge vivaldi; do
  if command -v "$b" >/dev/null 2>&1; then BROWSER_FOUND="$b"; break; fi
done
if [ -n "$BROWSER_FOUND" ]; then
  info "browser: $BROWSER_FOUND — the Lead's page checks and script-only pages will use it"
else
  info "no Brave/Chromium/Chrome found — page checks and pages that need JavaScript stay off until one is installed (e.g. pacman -Syu chromium)"
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
RUST_SRC=""
RUST_OK=0
COMMIT=""
# the branch's newest commit, asked first so the archive is exactly that commit
COMMIT="$(curl -fsSL -H 'Accept: application/vnd.github.sha' \
          "https://api.github.com/repos/${REPO}/commits/${BRANCH}" 2>/dev/null | tr -cd '0-9a-f' | head -c 40 || true)"
[ "${#COMMIT}" = "40" ] || COMMIT=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/chucknorris.py" ] && [ -d "$SELF_DIR/chucknorris_ext" ]; then
  install_tree "$SELF_DIR"                           # running from a clone
  RUST_SRC="$SELF_DIR"
  COMMIT="$(git -C "$SELF_DIR" rev-parse HEAD 2>/dev/null || true)"
elif curl -fsSL "https://codeload.github.com/${REPO}/tar.gz/${COMMIT:-refs/heads/${BRANCH}}" \
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
  RUST_SRC="$SRC_DIR"
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
           ledger compress agents browser diagnose mcp crashlog update pysession lsp codesearch checkpoint listen kokoro_server stt_server kernel cow worker daemons governor programs hibernate missions budget palette aiwatch; do
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


# Chuck, the Rust app.
RUST_MIN="1.88.0"     # what its dependencies need (Cargo.toml's rust-version)
RUST_WHY=""
if [ "${CHUCK_RUST:-1}" = "1" ]; then
  step "building Chuck (Rust) from the ${BRANCH} branch (a few minutes the first time)"
  cargo_ok() {
    command -v cargo >/dev/null || return 1
    v="$(cargo --version 2>/dev/null | awk '{print $2}')"
    [ -n "$v" ] && [ "$(printf '%s\n%s\n' "$v" "$RUST_MIN" | sort -V | head -n 1)" = "$RUST_MIN" ]
  }
  [ -f "${HOME}/.cargo/env" ] && . "${HOME}/.cargo/env"
  # rustup with no toolchain, or one that's too old: bring stable up to date
  # (installing the distro's `rust` next to rustup would conflict)
  if ! cargo_ok && command -v rustup >/dev/null; then
    info "updating Rust with rustup (Chuck needs ${RUST_MIN} or newer)"
    rustup toolchain install stable --profile minimal >/dev/null 2>&1 || rustup update stable >/dev/null 2>&1 || true
    rustup default stable >/dev/null 2>&1 || true
  fi
  if ! cargo_ok && ! command -v rustup >/dev/null; then
    if command -v pacman >/dev/null; then sudo pacman -Syu --needed --noconfirm rust >/dev/null 2>&1 || true
    elif command -v dnf >/dev/null; then sudo dnf install -y rust cargo >/dev/null 2>&1 || true
    fi
  fi
  if ! cargo_ok; then
    # the distro's Rust is missing or too old: the official rustup,
    # downloaded to a file first (never piped into a shell), user-local only
    info "Rust ${RUST_MIN}+ not found — installing it with rustup into ~/.cargo"
    RUSTUP="${SRC_TMP}/rustup-init.sh"
    if curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs -o "$RUSTUP" \
       && sh "$RUSTUP" -y --profile minimal --no-modify-path >/dev/null; then
      . "${HOME}/.cargo/env"
    fi
  fi
  if ! cargo_ok; then
    RUST_WHY="Rust ${RUST_MIN} or newer couldn't be installed (cargo: $(cargo --version 2>/dev/null || echo none))"
  elif [ -z "$RUST_SRC" ] || [ ! -f "${RUST_SRC}/Cargo.toml" ]; then
    RUST_WHY="the source archive of the ${BRANCH} branch couldn't be downloaded"
  elif ! pkg-config --atleast-version=4.14 gtk4 2>/dev/null || ! pkg-config --atleast-version=1.5 libadwaita-1 2>/dev/null; then
    RUST_WHY="Chuck needs GTK 4.14+ and libadwaita 1.5+ (found GTK $(pkg-config --modversion gtk4 2>/dev/null || echo none), \
libadwaita $(pkg-config --modversion libadwaita-1 2>/dev/null || echo none)) — a newer distribution release has them"
  else
    TARGET="${HOME}/.cache/chuck/cargo-target"
    BUILD_LOG="${DATA_DIR}/rust-build.log"
    if ( cd "$RUST_SRC" && CARGO_NET_RETRY=5 CARGO_TARGET_DIR="$TARGET" cargo build --release --locked -p chuck ) \
         2>&1 | tee "$BUILD_LOG" | grep -E '^\s*(Compiling chuck|Finished|error)' ; [ "${PIPESTATUS[0]}" = "0" ] \
       && "${TARGET}/release/chuck" version >/dev/null 2>&1; then
      # replace, never swap under a running copy: a new file, then a rename
      install -m 755 "${TARGET}/release/chuck" "${HOME}/.local/bin/.chuck.new"
      mv -f "${HOME}/.local/bin/.chuck.new" "${HOME}/.local/bin/chuck"
      # which commit this is, so Chuck can tell when a newer one is out
      if [ -n "$COMMIT" ]; then printf '%s\n' "$COMMIT" > "${DATA_DIR}/rust-commit"; else rm -f "${DATA_DIR}/rust-commit"; fi
      RUST_OK=1
      ok "$("${HOME}/.local/bin/chuck" version) installed"
    else
      RUST_WHY="the build failed — the last lines of ${BUILD_LOG}:
$(grep -E '^error' -A6 "$BUILD_LOG" 2>/dev/null | head -n 20 || tail -n 15 "$BUILD_LOG")"
    fi
  fi
else
  RUST_WHY="skipped (CHUCK_RUST=0)"
fi

# What "Chuck" opens: the Rust app when it built, the
# Python window only when it couldn't be built. The menu entry and the
# `chucknorris` command are rewritten on every install, so nothing is left
# pointing at an old build.
if [ "$RUST_OK" = "1" ]; then
  cat > "$LAUNCH" <<EOF
#!/usr/bin/env bash
# Chuck (Rust), installed from the ${BRANCH} branch. Re-run the installer to update.
exec "${HOME}/.local/bin/chuck" "\$@"
EOF
  COMMENT="One lead and eight workers on SiliconFlow; work is done only when it is verified"
else
  cat > "$LAUNCH" <<EOF
#!/usr/bin/env bash
# installed from the ${BRANCH} branch: the in-app updater stays on it
export CHUCK_BRANCH="\${CHUCK_BRANCH:-${BRANCH}}"
exec python3 "${APP_DIR}/chucknorris.py" "\$@"
EOF
  COMMENT="A coding and system agent for your desktop: runs commands, edits code, researches, sees screenshots"
fi
chmod +x "$LAUNCH"
cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Chuck
GenericName=AI Agent
Comment=${COMMENT}
Exec=${LAUNCH}
Icon=org.thepriest.chucknorris
StartupWMClass=org.thepriest.chucknorris
Terminal=false
Categories=Utility;System;Development;
Keywords=arch;cachyos;assistant;agent;code;fleet;
StartupNotify=true
EOF
# the separate entry an earlier build of this branch added
rm -f "${HOME}/.local/share/applications/org.thepriest.chuck.fleet.desktop"
update-desktop-database "${HOME}/.local/share/applications" 2>/dev/null || true
# KDE keeps its own menu cache: refresh it so the menu opens the new Chuck now
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true
command -v kbuildsycoca5 >/dev/null && kbuildsycoca5 >/dev/null 2>&1 || true
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

# A Chuck window still open from before is the OLD version. A Rust one
# saves and stops cleanly on SIGTERM, so it is closed; an old Python one is
# asked about (its unsaved text could be lost), never killed unasked.
OLD_RUST=""
for pid in $(pgrep -u "$(id -u)" -x chuck 2>/dev/null); do
  # a window is `chuck` with no arguments; `chuck run …` in a terminal is someone's job: left alone
  [ "$(tr '\0' '\n' < "/proc/${pid}/cmdline" 2>/dev/null | wc -l)" -le 1 ] && OLD_RUST="${OLD_RUST} ${pid}"
done
OLD_PY=""
for pid in $(pgrep -u "$(id -u)" -f 'chucknorris\.py' 2>/dev/null); do
  # only a Python running chucknorris.py, not anything that mentions it
  argv0="$(tr '\0' '\n' < "/proc/${pid}/cmdline" 2>/dev/null | head -n 1)"
  argv1="$(tr '\0' '\n' < "/proc/${pid}/cmdline" 2>/dev/null | sed -n 2p)"
  case "${argv0##*/}" in python*) case "$argv1" in *chucknorris.py) OLD_PY="${OLD_PY} ${pid}" ;; esac ;; esac
done
if [ "$RUST_OK" = "1" ] && [ -n "${OLD_RUST// /}" ]; then
  # shellcheck disable=SC2086
  kill -TERM $OLD_RUST 2>/dev/null && info "closed the Chuck window that was open (the previous build): open Chuck again"
fi
if [ "$RUST_OK" = "1" ] && [ -n "${OLD_PY// /}" ]; then
  ans="n"
  # only with a terminal to ask on (opening /dev/tty fails without one)
  if { exec 3<>/dev/tty; } 2>/dev/null; then
    printf '%s  The old Chuck window (Python) is still open. Close it now? [Y/n] %s' "$c1" "$c0" >&3
    read -r ans <&3 || ans="n"
    exec 3>&-
    [ -z "$ans" ] && ans="y"
  fi
  case "$ans" in
    y|Y|yes|YES) # shellcheck disable=SC2086
      kill -TERM $OLD_PY 2>/dev/null && info "closed the old Chuck window" ;;
    *) info "the old Chuck window (Python) is still open: close it, then open Chuck — it will be the new one" ;;
  esac
fi

if [ "$RUST_OK" = "1" ]; then
  # what opens, checked: the menu entry and `chucknorris` both reach the new binary
  grep -q "${HOME}/.local/bin/chuck\"" "$LAUNCH" && grep -q "^Exec=${LAUNCH}$" "$DESKTOP" \
    || die "the launcher wasn't written as expected (${LAUNCH}, ${DESKTOP})"
  printf '\n%s%s installed (%s branch) — this is the Rust Chuck.%s  Open Chuck from your app menu, or run:  %schuck%s\n' \
    "$c1" "$("${HOME}/.local/bin/chuck" version)" "$BRANCH" "$c0" "$c2" "$c0"
  info "the app menu's Chuck and the chucknorris command both open it"
  info "add a SiliconFlow key in Settings (gear icon), or: chuck key"
else
  printf '\n%s!! The Rust Chuck was NOT installed: %s%s\n' "$cr" "$RUST_WHY" "$c0"
  printf '%s   Chuck opens the older Python window until this is fixed. Re-run the installer afterwards.%s\n' "$c1" "$c0"
  info "the Python window: chucknorris (or Chuck in the app menu)"
fi
