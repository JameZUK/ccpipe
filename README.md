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

The repo is location-independent — clone it wherever you want it to
live long-term. A common pattern is to keep one clone for development
under `~/Projects/ccpipe/` and a separate "live install" under
`~/.local/share/ccpipe/` that the systemd unit actually runs from;
the install script picks up the path of whichever checkout you run
it from and bakes that into the rendered systemd unit.

The recommended path is the bundled installer — it creates a
self-contained Python venv under `backend/.venv`, installs the
frontend deps, builds the bundle, renders the systemd unit templates
with the current install location, and wires up the two `systemd
--user` units in one command. Idempotent: re-run to upgrade.

```bash
# Long-term install location:
git clone https://github.com/<you>/ccpipe ~/.local/share/ccpipe
cd ~/.local/share/ccpipe
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

# 3. Render + install the systemd user units. The repo ships templates
#    (.service.in) with an @REPO_ROOT@ placeholder; substitute the
#    absolute path of THIS checkout so the unit points at the right
#    venv and frontend bundle.
REPO_ROOT="$(pwd)"
mkdir -p ~/.config/systemd/user
sed "s|@REPO_ROOT@|$REPO_ROOT|g" systemd/ccpipe.service.in \
    > ~/.config/systemd/user/ccpipe.service
sed "s|@REPO_ROOT@|$REPO_ROOT|g" systemd/ccpipe-virtual-mic.service.in \
    > ~/.config/systemd/user/ccpipe-virtual-mic.service
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

ccpipe runs HTTP only — it does not terminate TLS itself. The
recommended deployment is **nginx in front, terminating TLS**, with
ccpipe's uvicorn bound to `0.0.0.0:8080` so the proxy can reach it
regardless of where it runs.

The bundled `nginx/ccpipe.conf` is a complete, production-shaped
sample (HTTPS server block + HTTP-to-HTTPS redirect + WS tuning +
defence-in-depth headers). Three sections work together; all three
must agree on which host is the proxy:

1. **nginx** — `server_name`, cert paths, and `proxy_pass` target
2. **ccpipe backend** — runs with `--proxy-headers
   --forwarded-allow-ips=<nginx-host-IP>` and `CCPIPE_BEHIND_TLS=1`
3. **firewall** — :8080 reachable only from the nginx host

### Topology: same-host vs off-host

| | nginx **on the same host** as ccpipe | nginx **on a different LAN host** |
|---|---|---|
| `proxy_pass` | `http://127.0.0.1:8080` | `http://<ccpipe-host>:8080` |
| ccpipe `--host` | tighten to `127.0.0.1` if you want | leave `0.0.0.0` |
| `--forwarded-allow-ips` | `127.0.0.1` | the nginx host's LAN IP |
| Firewall on :8080 | deny all external | allow from nginx host only |

Off-host is the documented default; the systemd unit ships with
`--host 0.0.0.0` so it works out of the box for that case.

### Step 1: install the nginx config

```bash
# Edit the four marked spots (server_name, cert paths, proxy_pass)
sudo cp nginx/ccpipe.conf /etc/nginx/sites-available/ccpipe
sudo $EDITOR /etc/nginx/sites-available/ccpipe
sudo ln -sf /etc/nginx/sites-available/ccpipe /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

### Step 2: wire the ccpipe TLS drop-in

Replace `10.0.0.5` with the actual IP of your nginx host. **The
allow-ips list MUST be tight** — every IP in it can spoof
`X-Forwarded-For` to bypass the per-IP login throttle.

```bash
mkdir -p ~/.config/systemd/user/ccpipe.service.d/
cat > ~/.config/systemd/user/ccpipe.service.d/tls.conf <<'EOF'
[Service]
Environment=CCPIPE_BEHIND_TLS=1
Environment=CCPIPE_TRUSTED_HOSTS=ccpipe.example.com
Environment=CCPIPE_ALLOWED_ORIGINS=https://ccpipe.example.com

# Reset ExecStart and re-run uvicorn with proxy-headers honoured.
# Replace the path below if you installed to somewhere other than
# the recommended ~/.local/share/ccpipe (the install script bakes
# the right path into the base unit; this drop-in is just adding
# the proxy-headers flag).
ExecStart=
ExecStart=%h/.local/share/ccpipe/backend/.venv/bin/uvicorn ccpipe.main:app \
    --host 0.0.0.0 --port 8080 \
    --proxy-headers --forwarded-allow-ips=10.0.0.5 \
    --timeout-keep-alive 5 --limit-concurrency 200
EOF
systemctl --user daemon-reload && systemctl --user restart ccpipe
```

What `CCPIPE_BEHIND_TLS=1` flips on:
- Session cookie gets `Secure` + the `__Host-` prefix so it refuses
  to travel over plain HTTP.
- `TrustedHostMiddleware` binds to your hostname so HTTP `Host`
  header spoofing is rejected.
- WebSocket Origin checks restricted to the HTTPS origin so a page
  loaded over HTTP can't hijack the WS upgrade.
- `Strict-Transport-Security` is sent on every response.
- A startup banner reminds you to firewall :8080 to the proxy IP.

What `--proxy-headers --forwarded-allow-ips=…` flips on:
- `request.client.host` (used by the per-IP login throttle) reads
  `X-Forwarded-For` from the proxy instead of always showing
  nginx's IP.
- The throttle log line (`login throttle tripped for ip=…`) shows
  the real client IP, so you can see who's hammering you.
- Combined with the firewall rule below, makes the per-IP cap
  meaningful per-real-client.

### Step 3: firewall :8080 to the proxy

If nginx is off-host, only the nginx host should be allowed to reach
ccpipe's backend port. Otherwise a LAN attacker can hit :8080
directly over plaintext HTTP, bypassing both TLS and (in the
spoofable case) the per-IP throttle.

```bash
# ufw example — replace 10.0.0.5 with the nginx host's IP
sudo ufw deny  in on <iface> to any port 8080
sudo ufw allow from 10.0.0.5 to any port 8080
sudo ufw reload
```

iptables, nftables, your router ACL, or binding to a specific LAN
interface (`--host 192.168.1.50`) all achieve the same thing — pick
whatever fits your environment.

### Verifying the wiring

After `systemctl --user restart ccpipe` and `systemctl reload nginx`:

```bash
# 1. ccpipe's startup banner should warn about :8080:
journalctl --user -u ccpipe -b | grep -A 6 BEHIND_TLS

# 2. Hit the site over HTTPS — should return JSON, not an error:
curl -sS https://ccpipe.example.com/api/health

# 3. Deliberately fail a login and watch the journal for the real IP:
curl -sS -X POST https://ccpipe.example.com/api/auth/login \
     -H 'Content-Type: application/json' \
     -H 'X-Requested-By: ccpipe' \
     -d '{"username":"bad","password":"bad"}' &
journalctl --user -u ccpipe -f | grep "login throttle"
# If the logged IP is the nginx host, --proxy-headers isn't wired.
# If it's your laptop, you're good.

# 4. Backend should NOT be reachable directly from anywhere except
#    the nginx host:
curl -sS --max-time 3 http://<ccpipe-host>:8080/api/health
# expected: timeout from a LAN host that isn't the proxy
```

### Common gotchas

- **WebSocket disconnects after ~60 s of idle.** `proxy_read_timeout`
  defaults to 60 s on nginx. The sample sets it to 1 day; tune
  shorter if you want.
- **Login throttle locks out everyone after 5 attempts.** Without
  `--proxy-headers --forwarded-allow-ips=<nginx-IP>` every request
  shows up as the same source IP and the per-IP cap is effectively
  global. The fix is the systemd drop-in above.
- **Cookie not set on first login.** `__Host-` cookies require both
  `Secure` and `Path=/`; if you forgot `CCPIPE_BEHIND_TLS=1`, the
  Secure flag isn't applied and the browser silently drops the
  cookie under HTTPS. Look for `Set-Cookie: __Host-ccpipe_session`
  in the response headers to confirm.
- **`502 Bad Gateway` on first request.** Either nginx is pointing
  at the wrong `proxy_pass` host/port, or the firewall rule blocks
  the nginx host. `curl http://<ccpipe-host>:8080/api/health` from
  the nginx box should return JSON.

### Alternatives

Caddy can replace the nginx server block in 5 lines and handles
TLS issuance automatically:

```caddy
ccpipe.example.com {
    reverse_proxy <ccpipe-host>:8080 {
        # Trust X-Forwarded-* from Caddy. Caddy sets these by default.
        header_up Host {host}
    }
}
```

Pair with the same `tls.conf` systemd drop-in, swapping
`--forwarded-allow-ips=` for the Caddy host's IP.

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
