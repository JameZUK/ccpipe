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
# Units ship as templates (*.service.in) with an @REPO_ROOT@ placeholder
# that we substitute with the absolute path of THIS install. That makes
# the project location-independent: clone to anywhere, run install.sh,
# the rendered unit at ~/.config/systemd/user/ccpipe.service points at
# the right place. The repo never carries a hardcoded user path.
if [[ "$INSTALL_UNITS" -eq 1 ]]; then
  say "rendering + installing systemd --user units (REPO_ROOT=$REPO_ROOT)"
  mkdir -p "$USER_UNITS_DIR"
  render_unit() {
    local template="$1" target="$2"
    if [[ ! -f "$template" ]]; then
      warn "missing template: $template"
      exit 1
    fi
    # Use a delimiter that can't appear in a filesystem path. The
    # placeholder is documented in the .in files so a maintainer
    # editing them knows what gets substituted.
    sed "s|@REPO_ROOT@|$REPO_ROOT|g" "$template" \
      | install -m 0644 /dev/stdin "$target"
  }
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
else
  ok  "skipped systemd unit install (--skip-units)"
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
  echo "  Delete after capturing: shred -u $SIDECAR"
  echo "  Rotate via Settings → Account in the web UI."
elif [[ -f "$CREDS" ]]; then
  echo
  printf '\033[1;36m═══ ccpipe credentials ═══\033[0m\n'
  python3 -c "import json; d=json.load(open(r'$CREDS')); print(f\"  username: {d['username']}\"); print(f\"  totp:     {'enrolled' if d.get('totp_secret') else 'disabled'}\")"
  echo
  echo "  Password is argon2id-hashed in $CREDS (no plaintext stored)."
  echo "  Forgot it? rm $CREDS && systemctl --user restart ccpipe to regenerate."
fi

echo
ok "install complete. Open https://<host>/ in your browser."
