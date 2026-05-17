# ccpipe

Personal web interface to Claude Code, via tmux. Text + voice; PWA-installable.

## What it is

- Browser → WSS → FastAPI → `tmux attach -t <session>` → `claude` (the TUI)
- Multiple browsers attach to the same tmux session simultaneously (laptop + phone mirror)
- Picks up any existing tmux session on the host, or creates new ones from the UI
- The unmodified `claude` binary holds the OAuth token; the backend never touches credentials
- Voice in: browser mic → AudioWorklet (16kHz PCM) → backend → Pulse/PipeWire pipe-source → `claude /voice`
- Voice out: tail `~/.claude/projects/**/*.jsonl` → POST to Kokoro-FastAPI → stream MP3 chunks back
- Username + password gate with optional TOTP (Google Authenticator-style)
- Sane for behind-TLS deployment (Caddy / nginx in front)

## Architecture

ccpipe runs as a `systemd --user` service on the host. No Docker — the backend is tightly coupled to host state (tmux, `~/.claude`, project files, PulseAudio, mic pipe) and containerising it created more friction than it solved.

```
[Browser] ──WSS── [nginx] ── [uvicorn (user service)]
                                   │
                                   ├── tmux client (talks to user's tmux server)
                                   ├── /tmp/ccpipe_mic.pipe (Pulse pipe-source)
                                   └── watches ~/.claude/projects/*.jsonl → Kokoro
```

## Prereqs on the host

- Python 3.11+ and `python -m venv` available
- Node 20+ for building the frontend
- `tmux` installed and on PATH (any version — client and server match by definition)
- `claude` CLI installed and logged in (`claude` once interactively to complete OAuth)
- nginx for TLS / reverse proxy (sample config in `nginx/ccpipe.conf`)
- PulseAudio or PipeWire (only needed for voice)

## Install

```bash
# 1. Build the frontend
cd frontend
npm install
npm run build
cd ..

# 2. Set up the backend venv
cd backend
python -m venv .venv
. .venv/bin/activate
pip install -e .
cd ..

# 3. Install the systemd user units
mkdir -p ~/.config/systemd/user
cp systemd/ccpipe.service              ~/.config/systemd/user/
cp systemd/ccpipe-virtual-mic.service  ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ccpipe-virtual-mic   # mic for /voice
systemctl --user enable --now ccpipe               # the web service

# 4. (Optional) start ccpipe automatically at boot, not just at login
loginctl enable-linger "$USER"
```

> Note: the first time `ccpipe-virtual-mic` runs, it may fail if
> `/tmp/ccpipe_mic.pipe` exists as a *directory* (leftover from a
> Docker bind-mount in a previous install). Clean it up:
> `sudo rmdir /tmp/ccpipe_mic.pipe && systemctl --user restart ccpipe-virtual-mic`.

Check it's running:

```bash
systemctl --user status ccpipe
journalctl --user -u ccpipe -f          # follow logs
curl http://127.0.0.1:8080/api/health   # should print {"status":"ok"}
```

## Reverse proxy (TLS termination)

The bundled `nginx/ccpipe.conf` is a sample server block. If nginx
runs **on the same host** as ccpipe, copy and enable it:

```bash
sudo cp nginx/ccpipe.conf /etc/nginx/sites-available/ccpipe
sudo ln -sf /etc/nginx/sites-available/ccpipe /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

If the proxy runs on a **different host** (which is the supported
default — uvicorn binds `0.0.0.0:8080` so the proxy can reach it over
the LAN), point its `proxy_pass` at `http://<ccpipe-host>:8080` and
make sure your firewall only exposes 8080 to the proxy's IP. The
plain-HTTP listener on 8080 will still answer LAN requests; combine
TLS + `CCPIPE_BEHIND_TLS=1` + the optional TOTP (see below) so the
secure path is the only realistic way in.

Once TLS is in front, drop in a service drop-in to flip the security
toggles:

```bash
mkdir -p ~/.config/systemd/user/ccpipe.service.d/
cat > ~/.config/systemd/user/ccpipe.service.d/tls.conf <<'EOF'
[Service]
Environment=CCPIPE_BEHIND_TLS=1
Environment=CCPIPE_TRUSTED_HOSTS=ccpipe.example.com
Environment=CCPIPE_ALLOWED_ORIGINS=https://ccpipe.example.com
EOF
systemctl --user daemon-reload && systemctl --user restart ccpipe
```

This:
- marks the session cookie `Secure` + uses the `__Host-` prefix so it
  refuses to travel over plain HTTP,
- enables `TrustedHostMiddleware` bound to your hostname,
- restricts WebSocket Origin checks to the HTTPS origin so a browser
  loaded over HTTP can't hijack the WS upgrade,
- sends `Strict-Transport-Security` on every response.

Then visit `https://<server_name>/`.

## First-time login

On first start, ccpipe generates a random password and prints it to
the journal alongside a banner. Read it once and then either save it
or rotate it via the Settings → Account UI:

```bash
journalctl --user -u ccpipe -b | grep -A 5 "GENERATED CCPIPE"
# or just cat the credentials file
cat ~/.local/state/ccpipe/credentials
```

Username defaults to your system username. Both can be changed from
the Settings modal, or by editing the credentials file (`0600`) and
restarting ccpipe.

### Two-factor (TOTP)

Optional. Open Settings → Account → "Set up two-factor", scan the QR
code with any TOTP app (Google Authenticator, 1Password, Authy,
Aegis...), then enter the 6-digit code to confirm. After enrollment,
the login form gains a second step that asks for the current code.

Lost your authenticator? With shell access to the host, delete the
`totp_secret` field from `~/.local/state/ccpipe/credentials` and
restart ccpipe — the secret is wiped, two-factor is disabled, and
password-only login works again.

## Configuration reference

All settings come from environment variables, typically set via a
systemd drop-in at `~/.config/systemd/user/ccpipe.service.d/*.conf`.

| Variable                  | Default                            | Notes |
| ------------------------- | ---------------------------------- | ----- |
| `CCPIPE_FRONTEND_DIST`    | `/app/frontend`                    | Where to serve the Vite build from. The systemd unit points this at the in-repo `frontend/dist`. |
| `CCPIPE_AUTH_USERNAME`    | system user                        | Login user. Overrides the credentials file when set. |
| `CCPIPE_AUTH_PASSWORD`    | auto-generated                     | Login password. Set explicitly to skip the auto-generated random one. |
| `CCPIPE_CREDENTIALS_FILE` | `~/.local/state/ccpipe/credentials`| JSON credential store (`0600`). |
| `CCPIPE_SESSION_SECRET_FILE` | `~/.local/state/ccpipe/session_secret` | Random secret used to sign session cookies. Auto-generated on first run. |
| `CCPIPE_BEHIND_TLS`       | (unset)                            | When `1`/`true`/`on`, cookies get `Secure` + `__Host-` prefix, HSTS is sent, TrustedHostMiddleware enables. Required if a TLS proxy is in front. |
| `CCPIPE_TRUSTED_HOSTS`    | `*`                                | Comma-separated allow-list for `Host`. Only meaningful with `CCPIPE_BEHIND_TLS=1`. |
| `CCPIPE_ALLOWED_ORIGINS`  | derived from `Host`                | Comma-separated WebSocket origin allow-list. Set explicitly under TLS, e.g. `https://ccpipe.example.com`. |
| `CCPIPE_TTS`              | `off`                              | `kokoro`/`on`/`1`/`true` to enable TTS playback. Anything else disables. |
| `CCPIPE_KOKORO_URL`       | `http://localhost:8880`            | URL of your running Kokoro-FastAPI instance. |
| `CCPIPE_TTS_VOICE`        | `bf_emma`                          | Initial voice; user-overridable in Settings. |
| `CCPIPE_CLAUDE_PROJECTS`  | `~/.claude/projects`               | Where to tail claude transcripts for TTS. |
| `CCPIPE_CONFIG_FILE`      | `~/.local/state/ccpipe/config.json`| Persistent app settings (voice, scope, speech rate). |
| `CCPIPE_LOG_LEVEL`        | `INFO`                             | Logger level for `ccpipe.*`. |
| `XDG_STATE_HOME`          | `~/.local/state`                   | Standard XDG variable; ccpipe puts its state dir under here. |

## Voice setup

The virtual mic is now a systemd user service (`ccpipe-virtual-mic.service`)
that starts at login and tears down on stop. To control or inspect it:

```bash
systemctl --user status ccpipe-virtual-mic
journalctl --user -u ccpipe-virtual-mic -n 30
pactl list short modules | grep ccpipe_mic     # verify the Pulse module is loaded
ls -la /tmp/ccpipe_mic.pipe                    # verify the FIFO is present
```

You can also drive the underlying script manually (the unit just calls it):

```bash
./scripts/setup-virtual-mic.sh up      # load
./scripts/setup-virtual-mic.sh down    # unload
./scripts/setup-virtual-mic.sh         # (defaults to up)

# (Optional) make it the default input so /voice picks it up without picking.
pactl set-default-source ccpipe_mic
```

Kokoro-FastAPI lives outside ccpipe; point `CCPIPE_KOKORO_URL` in the service file at your running instance.

## Update workflow

```bash
git pull
cd frontend && npm run build && cd ..
# (re-install backend deps only if pyproject.toml changed)
systemctl --user restart ccpipe
```

## Development (without the systemd unit)

```bash
# Backend (auto-reload on changes)
cd backend && . .venv/bin/activate
uvicorn ccpipe.main:app --reload --port 8080

# Frontend (Vite dev server with proxy to backend)
cd frontend && npm run dev
# Visit http://localhost:5173
```

Tests:
```bash
cd backend && . .venv/bin/activate
pip install -e ".[dev]"
pytest -v
```

## Layout

```
backend/
  ccpipe/
    main.py            FastAPI app, /api/sessions, /ws, static frontend
    tmux.py            libtmux wrapper for one-shot ops
    tmux_control.py    Long-lived `tmux -C` listener; pushes events
    tmux_setup.py      Sets server-wide default-shell etc. at startup
    ws.py              WebSocket handler, tagged binary frame protocol
    pty_relay.py       PTY spawn + async read/write
    mic.py             Mic pipe writer (browser PCM → /tmp/ccpipe_mic.pipe)
    tts.py             JSONL tail + Kokoro client + audio chunk fan-out
    settings_patch.py  Idempotently adds voice keys to ~/.claude/settings.json
  tests/               pytest suite (~140 tests)
  pyproject.toml

frontend/
  src/
    main.ts            Entry: session picker → terminal
    session-picker.ts
    terminal.ts        xterm.js setup, resize, input wiring
    mobile.ts          Composer bar + modifier-key row for phone/tablet
    mic.ts             Mic capture: getUserMedia + AudioWorkletNode
    tts.ts             Chunked audio player
    ws.ts              WS client, JSON control + tagged binary frames
    styles.css
  public/
    manifest.webmanifest
    sw.js              Minimal service worker for PWA install
    mic-worklet.js     AudioWorkletProcessor: 48k→16k mono Int16 PCM
  index.html
  vite.config.ts

nginx/ccpipe.conf      Sample server block
scripts/
  setup-virtual-mic.sh Load/unload Pulse module-pipe-source on host (up/down)
systemd/
  ccpipe.service              Web service (FastAPI + uvicorn)
  ccpipe-virtual-mic.service  Loads the Pulse pipe-source at login
```

## Security / ToS notes

- Subscription OAuth (Pro/Max) is fine for personal use **only**
  because the unmodified `claude` binary makes the API calls — ccpipe
  is a pure PTY relay and never originates a request to
  `api.anthropic.com`. See
  https://code.claude.com/docs/en/legal-and-compliance.
- Auth is **always on** — credentials are generated on first boot if
  none are configured. The session cookie is signed and (under
  `CCPIPE_BEHIND_TLS=1`) carries `Secure` + the `__Host-` prefix.
  WebSocket upgrades enforce an Origin allow-list. State-changing
  POSTs require an `X-Requested-By: ccpipe` header (CSRF defense).
- Enable TOTP under Settings → Account if you need a second factor.
- TTS reads from your Claude transcripts; only run ccpipe on a host
  where you trust everyone with access to those files.
- ccpipe binds `0.0.0.0:8080` so a reverse proxy on a different host
  can reach it. If you don't have a firewall in front of that port,
  any LAN device can attempt logins (rate-limited but reachable). If
  the proxy is co-located, you can tighten by editing the unit's
  `--host` to `127.0.0.1`.

## Troubleshooting

- **`/voice` says no audio device**: run `pactl list short sources |
  grep ccpipe_mic` — if missing, the virtual-mic service isn't loaded;
  `systemctl --user restart ccpipe-virtual-mic`. On Wayland sessions
  PulseAudio may need to be replaced by PipeWire's `pulseaudio` shim.
- **WS keeps reconnecting on mobile**: usually fine — the client
  treats the socket as stale after 45s of silence and re-dials.
  Check `journalctl --user -u ccpipe -f` for backend errors.
- **TTS silent on mobile**: tap the page once after attaching to a
  session. Browsers gate `AudioContext` resumption behind a user
  gesture. The voice pill in the statusbar should turn amber once
  it's wired.
- **Login banner not in the journal**: `cat
  ~/.local/state/ccpipe/credentials` always has the current values.
- **`open terminal failed: not a terminal` in logs**: this used to
  fire during tmux control-client startup and is now suppressed; if
  you still see it, you're on an older build.
