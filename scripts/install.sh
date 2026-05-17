#!/usr/bin/env bash
# ccpipe one-shot installer.
#
# Runs end-to-end so a fresh clone becomes a running, enabled
# `systemd --user` service in a single command. Idempotent — re-running
# it just upgrades the venv + rebuilds the frontend.
#
# Usage:
#   scripts/install.sh              # full install
#   scripts/install.sh --skip-units # don't touch systemd, just venv+build
#
# Requires (on PATH): python3 (≥3.11), node (≥20), npm, tmux, systemctl.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$REPO_ROOT/backend"
FRONTEND="$REPO_ROOT/frontend"
UNITS_DIR="$REPO_ROOT/systemd"
USER_UNITS_DIR="$HOME/.config/systemd/user"
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

need python3
need node
need npm
need tmux
need systemctl

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

# ─── 3. systemd user units ────────────────────────────────────────────
if [[ "$INSTALL_UNITS" -eq 1 ]]; then
  say "installing systemd --user units"
  mkdir -p "$USER_UNITS_DIR"
  install -m 0644 "$UNITS_DIR/ccpipe.service"             "$USER_UNITS_DIR/"
  install -m 0644 "$UNITS_DIR/ccpipe-virtual-mic.service" "$USER_UNITS_DIR/"
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
else
  ok  "skipped systemd unit install (--skip-units)"
fi

# ─── 4. show credentials banner (if present) ──────────────────────────
CREDS="${XDG_STATE_HOME:-$HOME/.local/state}/ccpipe/credentials"
if [[ -f "$CREDS" ]]; then
  echo
  printf '\033[1;36m═══ ccpipe login credentials ═══\033[0m\n'
  python3 -c "import json,sys; d=json.load(open(r'$CREDS')); print(f\"  username: {d['username']}\"); print(f\"  password: {d['password']}\"); print(f\"  totp:     {'enrolled' if d.get('totp_secret') else 'disabled'}\")"
  echo
  echo "  (edit / rotate via Settings → Account in the web UI)"
fi

echo
ok "install complete. Open https://<host>/ in your browser."
