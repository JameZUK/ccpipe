# Configuration

## Environment variables

All env vars are typically set via a systemd drop-in at
`~/.config/systemd/user/ccpipe.service.d/*.conf`.

| Variable                  | Default                            | Notes |
| ------------------------- | ---------------------------------- | ----- |
| `CCPIPE_FRONTEND_DIST`    | `/app/frontend`                    | Where to serve the Vite build from. The systemd unit points this at the in-repo `frontend/dist`. |
| `CCPIPE_AUTH_USERNAME`    | system user                        | Login user. Overrides the credentials file when set. |
| `CCPIPE_AUTH_PASSWORD`    | auto-generated                     | Login password. Set explicitly to skip the auto-generated random one. |
| `CCPIPE_CREDENTIALS_FILE` | `~/.local/state/ccpipe/credentials`| JSON credential store (`0600`). |
| `CCPIPE_SESSION_SECRET_FILE` | `~/.local/state/ccpipe/session_secret` | Random secret used to sign session cookies. Auto-generated on first run. |
| `CCPIPE_BEHIND_TLS`       | (unset)                            | When `1`/`true`/`on`, cookies get `Secure` + `__Host-` prefix, HSTS is sent, `TrustedHostMiddleware` enables, and a startup banner reminds the operator to firewall `:8080` to the proxy IP. |
| `CCPIPE_TRUSTED_HOSTS`    | `*`                                | Comma-separated allow-list for `Host`. Only meaningful with `CCPIPE_BEHIND_TLS=1`. |
| `CCPIPE_ALLOWED_ORIGINS`  | derived from `Host`                | Comma-separated WebSocket origin allow-list. Set explicitly under TLS, e.g. `https://ccpipe.example.com`. |
| `CCPIPE_FS_ROOT`          | `$HOME`                            | Root directory the file panel + editor are scoped to. Two subpaths under it (`.local/state/ccpipe`, `.config/ccpipe`) are denylisted to protect ccpipe's own credentials store. |
| `CCPIPE_TTS`              | `off`                              | `kokoro`/`on`/`1`/`true` to enable TTS playback. Anything else disables. |
| `CCPIPE_KOKORO_URL`       | `http://localhost:8880`            | URL of your running Kokoro-FastAPI instance. |
| `CCPIPE_TTS_VOICE`        | `bf_emma`                          | Initial voice; user-overridable in Settings. |
| `CCPIPE_CLAUDE_PROJECTS`  | `~/.claude/projects`               | Where to tail claude transcripts for TTS. |
| `CCPIPE_CONFIG_FILE`      | `~/.local/state/ccpipe/config.json`| Persistent app settings: TTS voice + speech rate + scope, FS upload limit, voice-input timings. Edited in-app via Settings. |
| `CCPIPE_LOG_LEVEL`        | `INFO`                             | Logger level for `ccpipe.*`. |
| `XDG_STATE_HOME`          | `~/.local/state`                   | Standard XDG variable; ccpipe puts its state dir under here. |

## First-time login

On first start, ccpipe generates a random password, argon2id-hashes it
into the credentials file, and writes the plaintext into a read-once
sidecar file (`0400`). The plaintext is never logged.

```bash
cat ~/.local/state/ccpipe/initial_password.txt
# username + password + a reminder to delete this file once read

# After you've recorded the password, delete the sidecar:
shred -u ~/.local/state/ccpipe/initial_password.txt
```

Existing installs with an old plaintext credentials file are migrated
to argon2id automatically on next start.

Username defaults to your system username. Both can be changed from the
Settings modal at any time; ccpipe re-hashes the new password before
persisting it.

### Two-factor (TOTP)

Optional. Open Settings → Account → "Set up two-factor", scan the QR
code with any TOTP app (Google Authenticator, 1Password, Authy,
Aegis...), then enter the 6-digit code to confirm. After enrolment, the
login form gains a second step that asks for the current code.

Lost your authenticator? With shell access to the host, delete the
`totp_secret` field from `~/.local/state/ccpipe/credentials` and restart
ccpipe — the secret is wiped, two-factor is disabled, and password-only
login works again.

## Voice setup

The virtual mic runs as a systemd user service
(`ccpipe-virtual-mic.service`) that starts at login and tears down on
stop:

```bash
systemctl --user status ccpipe-virtual-mic
journalctl --user -u ccpipe-virtual-mic -n 30
pactl list short modules | grep ccpipe_mic     # verify the Pulse module is loaded
ls -la /tmp/ccpipe_mic.pipe                    # verify the FIFO is present
```

You can also drive the underlying script manually:

```bash
./scripts/setup-virtual-mic.sh up      # load
./scripts/setup-virtual-mic.sh down    # unload
./scripts/setup-virtual-mic.sh         # (defaults to up)
```

### Coexistence with a real microphone

Loading the virtual mic is purely additive — it appears in `pactl list
short sources` alongside any real (USB / built-in / Bluetooth) inputs
you already have. The unload path matches on `source_name=ccpipe_mic`
specifically, so it can only ever touch its own module.

If you want `claude /voice` to pick up the virtual mic automatically,
the simplest path is:

```bash
pactl set-default-source ccpipe_mic
```

**Heads up — this flips the system-wide default input.** Every app
that grabs "the default mic" without explicitly selecting a device
(video calls, voice-memo apps, browser `getUserMedia` calls without a
`deviceId`, etc.) will then read silence from the pipe whenever ccpipe
isn't actively streaming. If you already rely on a real mic for other
apps, leave the default alone and instead pick `ccpipe_browser_mic`
(the friendly device description) in claude's audio source picker
when you start a `/voice` session — or flip the default only for the
duration of dictation and flip it back.

Kokoro-FastAPI lives outside ccpipe; point `CCPIPE_KOKORO_URL` at your
running instance.

## Voice-input behaviour (Settings → Voice → "voice input")

ccpipe orchestrates the "release push-to-talk" signal from the backend
based on actual pipeline state, not a fixed client-side delay. The
client sends a `mic_stop` message when the browser mic stops; the
backend then waits for the in-flight audio to drain through Pulse and
adds a configurable safety pad before writing the release keystroke
itself.

Four knobs in Settings → Voice → "voice input":

- **Auto-stop on silence** — when on, the client-side VAD ends the
  recording after sustained silence. When off, the mic is strictly
  tap-to-stop.
- **Silence before stop** (200-15000 ms, default 2500) — how long
  silence must run before the VAD trips. Only matters when auto-stop is
  on.
- **Submit pad** (0-10000 ms, default 1500) — extra wall-time the
  backend waits *after* the drain estimate completes before submitting.
  Buys time for Pulse's internal buffer and claude's STT to finalise.
  **The key knob if tail words are still being cut on submission** —
  bump it 500 ms at a time.
- **Max recording length** (5-600 s, default 60) — safety cap. The mic
  force-stops after this many seconds even with continuous voice.

Settings persist in `~/.local/state/ccpipe/config.json` under the `mic`
key.

## Where state lives

- `~/.local/state/ccpipe/credentials` — argon2id hash, TOTP secret,
  `0600`.
- `~/.local/state/ccpipe/session_secret` — used to sign session
  cookies. Rotating it invalidates all sessions.
- `~/.local/state/ccpipe/initial_password.txt` — read-once `0400` file
  with the auto-generated password on first run. **Delete after
  reading.**
- `~/.local/state/ccpipe/config.json` — Settings-modal values.
- `~/.claude/keybindings.json` — ccpipe idempotently adds the meta+k
  voice push-to-talk binding here on startup (see
  `ccpipe/settings_patch.py`).
