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

The recommended path is the bundled installer — it creates a
self-contained Python venv under `backend/.venv`, installs the
frontend deps, builds the bundle, and wires up the two `systemd
--user` units in one command. Idempotent: re-run to upgrade.

```bash
scripts/install.sh
```

If you'd rather just touch the venv + frontend without the
systemd step (e.g. for a dev box where you'll run `uvicorn --reload`
manually):

```bash
scripts/install.sh --skip-units
```

### Manual install (if you don't want to use the script)

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
toggles. Replace `10.0.0.5` with the actual IP of your nginx host:

```bash
mkdir -p ~/.config/systemd/user/ccpipe.service.d/
cat > ~/.config/systemd/user/ccpipe.service.d/tls.conf <<'EOF'
[Service]
Environment=CCPIPE_BEHIND_TLS=1
Environment=CCPIPE_TRUSTED_HOSTS=ccpipe.example.com
Environment=CCPIPE_ALLOWED_ORIGINS=https://ccpipe.example.com

# Reset ExecStart and re-run uvicorn with proxy-headers honoured. The
# allow-ips list MUST be tight — every IP in it can spoof X-Forwarded-
# For to bypass the per-IP login throttle. List only the host(s) where
# nginx actually runs. Without this, request.client.host returns the
# nginx IP for every request and the per-IP throttle is effectively
# global.
ExecStart=
ExecStart=%h/Projects/ccpipe/backend/.venv/bin/uvicorn ccpipe.main:app \
    --host 0.0.0.0 --port 8080 \
    --proxy-headers --forwarded-allow-ips=10.0.0.5
EOF
systemctl --user daemon-reload && systemctl --user restart ccpipe
```

This:
- marks the session cookie `Secure` + uses the `__Host-` prefix so it
  refuses to travel over plain HTTP,
- enables `TrustedHostMiddleware` bound to your hostname,
- restricts WebSocket Origin checks to the HTTPS origin so a browser
  loaded over HTTP can't hijack the WS upgrade,
- sends `Strict-Transport-Security` on every response,
- accepts `X-Real-IP` / `X-Forwarded-For` from the configured proxy
  IP(s) only, so the per-IP login throttle works per-real-client and
  the journal records true source IPs in throttle-tripped warnings.

Confirm the proxy-headers wiring is correct after restart by tailing
the journal during a deliberate wrong-password attempt — the
`login throttle tripped for ip=…` line should show the real client
IP, not the nginx IP.

Then visit `https://<server_name>/`.

## First-time login

On first start, ccpipe generates a random password, **argon2id-hashes
it into the credentials file**, and writes the plaintext into a
read-once sidecar file (`0400`). The plaintext is never logged — the
operator is told once where to look:

```bash
cat ~/.local/state/ccpipe/initial_password.txt
# username + password + a reminder to delete this file once read

# After you've recorded the password, delete the sidecar:
shred -u ~/.local/state/ccpipe/initial_password.txt
```

Existing installs with an old plaintext credentials file are migrated
to argon2id automatically on next start. The journal banner that the
old build produced is now suppressed — the plaintext never lands in
`journalctl`.

Username defaults to your system username. Both can be changed from
the Settings modal at any time; ccpipe re-hashes the new password
before persisting it.

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
| `CCPIPE_BEHIND_TLS`       | (unset)                            | When `1`/`true`/`on`, cookies get `Secure` + `__Host-` prefix, HSTS is sent, TrustedHostMiddleware enables, and a startup banner reminds the operator to firewall :8080 to the proxy IP. |
| `CCPIPE_TRUSTED_HOSTS`    | `*`                                | Comma-separated allow-list for `Host`. Only meaningful with `CCPIPE_BEHIND_TLS=1`. |
| `CCPIPE_ALLOWED_ORIGINS`  | derived from `Host`                | Comma-separated WebSocket origin allow-list. Set explicitly under TLS, e.g. `https://ccpipe.example.com`. |
| `CCPIPE_FS_ROOT`          | `$HOME`                            | Root directory the file panel + editor are scoped to. Two subpaths under it (`.local/state/ccpipe`, `.config/ccpipe`) are denylisted to protect ccpipe's own credentials store. |
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
    main.py            App factory: middleware, lifespan, /ws, router wiring
    auth.py            Argon2id passwords, TOTP, session middleware
    routes/
      auth.py          /api/auth/* (login, logout, TOTP, credentials)
      sessions.py      /api/sessions/*, /api/claude-sessions/*
      fs.py            /api/fs/* (list, read, write, upload, download, ...)
      tts.py           /api/tts/* (voices, config, speak, preview)
      static.py        /, /manifest.webmanifest, /sw.js, icons
    tmux.py            libtmux wrapper for one-shot ops
    tmux_control.py    Long-lived `tmux -C` listener; pushes events
    tmux_setup.py      Sets server-wide default-shell etc. at startup
    ws.py              WebSocket handler, tagged binary frame protocol
    pty_relay.py       PTY spawn + async read/write
    mic.py             Mic pipe writer (browser PCM → /tmp/ccpipe_mic.pipe)
    tts.py             JSONL tail + Kokoro client + audio chunk fan-out
    settings_patch.py  Idempotently adds voice keys to ~/.claude/settings.json
  tests/               pytest suite (~160 tests)
  pyproject.toml

frontend/
  src/
    main.ts            Entry: session picker → terminal; lazy-loads heavy UI
    api.ts             Shared fetch helper (CSRF header + same-origin + JSON)
    session-picker.ts
    terminal.ts        xterm.js setup, resize, input wiring
    mobile.ts          Composer bar + modifier-key row for phone/tablet
    file-panel.ts      Adaptive file browser + inline editor
    settings.ts        Tabbed settings dialog (Display / Voice / Account)
    mic.ts             Mic capture: getUserMedia + AudioWorkletNode + VAD
    tts.ts             Chunked audio player + per-session mute
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

- **Pure PTY relay.** Subscription OAuth (Pro/Max) is fine for
  personal use **only** because the unmodified `claude` binary makes
  the API calls — ccpipe never originates a request to
  `api.anthropic.com`. See
  https://code.claude.com/docs/en/legal-and-compliance.
- **Always-on auth.** Credentials are generated on first boot if none
  are configured. **Passwords are argon2id-hashed on disk**; the
  plaintext only ever exists in the read-once `initial_password.txt`
  sidecar (mode `0400`). Legacy plaintext credential files from older
  builds are migrated to argon2id automatically.
- **Session hardening.** The signed session cookie carries `Secure` +
  the `__Host-` prefix under `CCPIPE_BEHIND_TLS=1`. WebSocket upgrades
  enforce an Origin allow-list. State-changing POSTs require an
  `X-Requested-By: ccpipe` header (CSRF defence). Each WS pong
  re-checks `is_session_authed`, so a credential change closes any
  open sockets on the next heartbeat.
- **Login throttling.** Per-IP 5/min + global 30/min sliding-window
  limit, with a 1-second sleep on every failure. Tripped attempts log
  the resolved client IP. Behind a proxy, enable
  `--proxy-headers --forwarded-allow-ips=<proxy-ip>` (see
  §Reverse proxy) so the per-IP cap counts real clients, not the
  proxy. No persistent banning — fail2ban reading
  `journalctl --user -u ccpipe` is the conventional add-on.
- **Optional TOTP** under Settings → Account. Codes are single-use
  for ~120s after acceptance (in-memory burn-list refuses replay
  even inside pyotp's clock-drift window).
- **File panel scope.** `/api/fs/*` is jailed to `CCPIPE_FS_ROOT`
  (default `$HOME`). The jail enforces `Path.is_relative_to(root)`
  after symlink resolution, refuses non-regular files
  (so `/proc/*`, `/dev/zero` and named pipes can't be read), and
  uses `O_NOFOLLOW` on temp-file opens so a pre-staged symlink at
  the tmp path can't clobber an arbitrary user-writable file. Only
  ccpipe's own state dirs are denylisted; `.ssh`, `.aws`, `.gnupg`,
  `.kube`, etc. remain reachable because this is an admin tool.
- **TTS reads your Claude transcripts**; only run ccpipe on a host
  where you trust everyone with access to those files. The transcript
  watcher caps its in-memory state at 5k files (LRU eviction) and
  drops watchdog events on overflow rather than growing unbounded.
- **0.0.0.0 bind.** ccpipe binds `0.0.0.0:8080` so an off-host proxy
  can reach it. Under `CCPIPE_BEHIND_TLS=1`, a startup banner reminds
  the operator to firewall the port to the proxy IP only — otherwise
  a LAN attacker can hit ccpipe directly over plaintext HTTP.
  Suggested ufw rule:
  ```
  ufw deny  in on <iface> to any port 8080
  ufw allow from <proxy-host>      to any port 8080
  ```

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
- **Lost the generated initial password**: the credentials file
  stores only the hash now. If you didn't capture the password from
  `~/.local/state/ccpipe/initial_password.txt` before deleting it,
  delete the credentials file itself
  (`rm ~/.local/state/ccpipe/credentials`) and restart ccpipe — it
  will regenerate fresh credentials and write a new sidecar.
- **Login throttle keeps you locked out**: rate-limit windows are
  60 s. Just wait. Or `systemctl --user restart ccpipe` to reset the
  in-memory buckets immediately.
- **`open terminal failed: not a terminal` in logs**: this used to
  fire during tmux control-client startup and is now suppressed; if
  you still see it, you're on an older build.
