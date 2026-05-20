#!/usr/bin/env bash
# ccpipe one-shot installer.
#
# Runs end-to-end so a fresh clone becomes a running, enabled user-level
# service in a single command (systemd --user on Linux, launchd
# LaunchAgent on macOS). Idempotent — re-running it just upgrades the
# venv + rebuilds the frontend.
#
# Usage:
#   scripts/install.sh              # full install
#   scripts/install.sh --skip-units # skip service install, just venv+build
#
# Requires (on PATH): python3 (≥3.11), node (≥20), npm, tmux, plus
# systemctl (Linux) or launchctl (macOS).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$REPO_ROOT/backend"
FRONTEND="$REPO_ROOT/frontend"
OS="$(uname)"
# Linux: systemd --user unit templates + per-user unit dir
UNITS_DIR="$REPO_ROOT/systemd"
USER_UNITS_DIR="$HOME/.config/systemd/user"
# macOS: launchd plist templates + per-user LaunchAgents dir
AGENTS_DIR="$REPO_ROOT/launchd"
USER_AGENTS_DIR="$HOME/Library/LaunchAgents"
INSTALL_UNITS=1

# ─── arg parsing ──────────────────────────────────────────────────────
for arg in "$@"; do
  case "$arg" in
    --skip-units) INSTALL_UNITS=0 ;;
    -h|--help)
      sed -n '2,16p' "$0"            # print the header docstring
      exit 0
      ;;
    *)
      echo "unknown arg: $arg (use --help)" >&2
      exit 2
      ;;
  esac
done

# ─── helpers ──────────────────────────────────────────────────────────
say()  { printf '\033[1;33m▸ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;31m! %s\033[0m\n' "$*"; }

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    warn "missing required command: $1"
    exit 1
  fi
}

# Substitute @REPO_ROOT@ and @HOME@ in a template and write the result
# at $target. Used for both systemd unit files and launchd plist files —
# placeholders are documented in the corresponding .in files. The pipe
# delimiter in the sed expressions is chosen because it can't appear in
# a filesystem path.
#
# Implementation note: GNU install on Linux accepts `/dev/stdin` as a
# source, but BSD install (macOS) does not. Plain redirect + chmod is
# portable to both, idempotent, and doesn't shell out twice.
render_unit() {
  local template="$1" target="$2"
  if [[ ! -f "$template" ]]; then
    warn "missing template: $template"
    exit 1
  fi
  sed -e "s|@REPO_ROOT@|$REPO_ROOT|g" -e "s|@HOME@|$HOME|g" "$template" > "$target"
  chmod 0644 "$target"
}

# Idempotently install a launchd LaunchAgent: bootout any prior load,
# wait for it to clear (bootout is async — bootstrap too soon races with
# EIO), then bootstrap and kickstart. macOS only.
relaunch_agent() {
  local label="$1" plist="$2"
  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  # 5 s ceiling is generous; bootout settles in 100-300 ms in practice.
  for _ in $(seq 1 50); do
    launchctl print "gui/$UID/$label" >/dev/null 2>&1 || break
    sleep 0.1
  done
  launchctl bootstrap "gui/$UID" "$plist"
  launchctl kickstart -k "gui/$UID/$label" >/dev/null
}

need python3
need node
need npm
need tmux

# Pick the service supervisor for this platform. Linux: systemctl --user.
# macOS: launchctl. Other OSes are unsupported by the service-install step
# but the backend may still build fine with --skip-units.
case "$OS" in
  Linux)  need systemctl ;;
  Darwin) need launchctl ;;
esac

# ─── 1. backend venv + editable install ───────────────────────────────
say "creating / updating backend venv"
python3 -m venv "$BACKEND/.venv"
# shellcheck source=/dev/null
. "$BACKEND/.venv/bin/activate"
python -m pip install --upgrade --quiet pip
pip install --quiet -e "$BACKEND"
deactivate
ok  "backend deps installed into $BACKEND/.venv"

# ─── 2. frontend build ────────────────────────────────────────────────
say "installing frontend deps"
(cd "$FRONTEND" && npm install --silent)
say "building frontend bundle"
(cd "$FRONTEND" && npm run build --silent)
ok  "frontend built to $FRONTEND/dist"

# ─── 3. user-level service install ────────────────────────────────────
# Templates ship with @REPO_ROOT@ / @HOME@ placeholders that we
# substitute at install time. That makes the project location- and
# user-independent: clone to anywhere, run install.sh, the rendered
# unit/plist points at the right place. The repo never carries a
# hardcoded path.
if [[ "$INSTALL_UNITS" -eq 1 ]]; then
  case "$OS" in
    Linux)
      say "rendering + installing systemd --user units (REPO_ROOT=$REPO_ROOT)"
      mkdir -p "$USER_UNITS_DIR"
      render_unit "$UNITS_DIR/ccpipe.service.in"             "$USER_UNITS_DIR/ccpipe.service"
      render_unit "$UNITS_DIR/ccpipe-virtual-mic.service.in" "$USER_UNITS_DIR/ccpipe-virtual-mic.service"
      systemctl --user daemon-reload
      systemctl --user enable --now ccpipe-virtual-mic.service || \
        warn "virtual-mic unit failed to enable — voice/dictation will be unavailable (continuing)"
      systemctl --user enable --now ccpipe.service
      ok  "ccpipe is running. Inspect with: journalctl --user -u ccpipe -f"

      # Linger so the service stays up after the user logs out.
      if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
        warn "user lingering is disabled; ccpipe will stop when you log out."
        warn "to keep it running across logout: sudo loginctl enable-linger $USER"
      fi
      ;;
    Darwin)
      say "rendering + installing launchd LaunchAgents (REPO_ROOT=$REPO_ROOT)"
      mkdir -p "$USER_AGENTS_DIR" "$HOME/Library/Logs/ccpipe"
      render_unit "$AGENTS_DIR/com.ccpipe.app.plist.in"         "$USER_AGENTS_DIR/com.ccpipe.app.plist"
      render_unit "$AGENTS_DIR/com.ccpipe.virtual-mic.plist.in" "$USER_AGENTS_DIR/com.ccpipe.virtual-mic.plist"

      # Bring up the virtual-mic bridge first so the FIFO exists by the
      # time ccpipe.mic tries to open it (matches the Linux unit's
      # `After=ccpipe-virtual-mic.service` ordering). The bridge needs
      # BlackHole's audio device to write into, so soft-fail when it
      # isn't installed — same fallback as the Linux unit's pactl-load.
      if brew list --cask 2>/dev/null | grep -qx 'blackhole-2ch'; then
        relaunch_agent com.ccpipe.virtual-mic "$USER_AGENTS_DIR/com.ccpipe.virtual-mic.plist"
      else
        # Tear down any prior load in case BlackHole was just uninstalled.
        launchctl bootout "gui/$UID/com.ccpipe.virtual-mic" 2>/dev/null || true
        warn "blackhole-2ch not installed; /voice will be unavailable."
        warn "to enable: brew install --cask blackhole-2ch  (reboot required)"
      fi

      relaunch_agent com.ccpipe.app "$USER_AGENTS_DIR/com.ccpipe.app.plist"
      ok  "ccpipe is running. Inspect with: tail -F ~/Library/Logs/ccpipe/ccpipe.{log,err.log}"
      # No `loginctl enable-linger` equivalent needed: LaunchAgents in
      # ~/Library/LaunchAgents auto-load at GUI login on macOS.
      ;;
    *)
      warn "unsupported OS for service install: $OS (use --skip-units to skip)"
      exit 1
      ;;
  esac
else
  ok  "skipped service install (--skip-units)"
fi

# ─── 4. show credentials banner (if present) ──────────────────────────
# Passwords are argon2id-hashed inside the credentials file; the
# plaintext lives ONCE in a sidecar (mode 0400) that the operator is
# expected to `cat` and then `shred -u`. Surface it here if it's still
# present so a fresh install gets a one-shot reveal; otherwise just
# tell them where to look.
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/ccpipe"
SIDECAR="$STATE_DIR/initial_password.txt"
CREDS="$STATE_DIR/credentials"
if [[ -f "$SIDECAR" ]]; then
  echo
  printf '\033[1;36m═══ ccpipe initial credentials (read once) ═══\033[0m\n'
  sed 's/^/  /' "$SIDECAR"
  echo
  echo "  Recover later: cat $SIDECAR"
  if [[ "$OS" == "Darwin" ]]; then
    echo "  Delete after capturing: rm -P $SIDECAR"
  else
    echo "  Delete after capturing: shred -u $SIDECAR"
  fi
  echo "  Rotate via Settings → Account in the web UI."
elif [[ -f "$CREDS" ]]; then
  echo
  printf '\033[1;36m═══ ccpipe credentials ═══\033[0m\n'
  python3 -c "import json; d=json.load(open(r'$CREDS')); print(f\"  username: {d['username']}\"); print(f\"  totp:     {'enrolled' if d.get('totp_secret') else 'disabled'}\")"
  echo
  echo "  Password is argon2id-hashed in $CREDS (no plaintext stored)."
  if [[ "$OS" == "Darwin" ]]; then
    echo "  Forgot it? rm $CREDS && launchctl kickstart -k gui/\$UID/com.ccpipe.app to regenerate."
  else
    echo "  Forgot it? rm $CREDS && systemctl --user restart ccpipe to regenerate."
  fi
fi

echo
ok "install complete. Open https://<host>/ in your browser."
