# Configuration

## Environment variables

All env vars are typically set via a systemd drop-in at
`~/.config/systemd/user/ccpipe.service.d/*.conf`.

| Variable                  | Default                            | Notes |
| ------------------------- | ---------------------------------- | ----- |
| `CCPIPE_FRONTEND_DIST`    | `/app/frontend`                    | Where to serve the Vite build from. The systemd unit points this at the in-repo `frontend/dist`. |
| `CCPIPE_AUTH_USERNAME`    | system user                        | Login username. **Bootstrap-only:** consulted only when no credentials file exists yet. Once the file is written, changes happen in Settings → Account or by deleting the file. |
| `CCPIPE_AUTH_PASSWORD`    | auto-generated                     | Bootstrap password. **Only consulted when no credentials file exists** — first start hashes it and persists it; from then on the file wins and env is ignored (with a one-shot log line). To re-seed, delete the credentials file and restart. Deleting also wipes any enrolled TOTP secret. Once the credentials file exists, ccpipe removes this variable from its own process environment, and no `CCPIPE_*` variable is passed on to tmux panes. |
| `CCPIPE_CREDENTIALS_FILE` | `~/.local/state/ccpipe/credentials`| JSON credential store (`0600`). |
| `CCPIPE_SESSION_SECRET_FILE` | `~/.local/state/ccpipe/session_secret` | Random secret used to sign session cookies. Auto-generated on first run. |
| `CCPIPE_BEHIND_TLS`       | (unset)                            | When `1`/`true`/`on`, cookies get `Secure` + `__Host-` prefix, HSTS is sent, `TrustedHostMiddleware` enables, and a startup banner reminds the operator to firewall `:8080` to the proxy IP. |
| `CCPIPE_TRUSTED_HOSTS`    | (none)                             | Comma-separated allow-list for `Host`. **Required under `CCPIPE_BEHIND_TLS=1`:** if unset or `*`, ccpipe **refuses to start** (a wildcard Host accepts `Host: anything.attacker.example`, enabling Host-header / DNS-rebinding attacks). Set it to your public hostname, e.g. `ccpipe.example.com`. Ignored without TLS. |
| `CCPIPE_ALLOW_WILDCARD_HOST` | (unset)                         | Escape hatch: set to `1`/`true`/`yes`/`on` to allow the wildcard `Host` allow-list under TLS instead of refusing to start. Only for deployments that genuinely need it. |
| `CCPIPE_ALLOWED_ORIGINS`  | derived from `Host`                | Comma-separated WebSocket origin allow-list. Set explicitly under TLS, e.g. `https://ccpipe.example.com`. On plain HTTP it falls back to the request `Host` (a one-time log notes this). |
| `CCPIPE_FS_ROOT`          | `$HOME`                            | Root directory the file panel + editor are scoped to. Denylisted subpaths under it are refused (read **and** write): `.local/state/ccpipe`, `.config/ccpipe` (ccpipe's own credentials store), `.claude` (Claude Code transcripts/settings/live OAuth token), and `.claude.json` (Claude Code global config + `oauthAccount` identity). |
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
Aegis...), then enter the 6-digit code to confirm. After enrolment,
enter the current code on the login form's second screen (the form
always shows that screen; without TOTP, leave it blank). Password and
code are checked together in one request, and any wrong part gets the
same "invalid credentials" error.

Lost your authenticator? With shell access to the host, delete the
`totp_secret` field from `~/.local/state/ccpipe/credentials` and restart
ccpipe — the secret is wiped, two-factor is disabled, and password-only
login works again.

### Passkeys

Optional. Open Settings → Account → **Add a passkey**, give it a name,
confirm with your password (and code, with two-factor on), tap
**Create passkey**, and your device asks for your face, fingerprint or
PIN. Then use **Sign in with a passkey** on the login page, or pick the
passkey from the username field's suggestions where the browser offers
them. A passkey synced through iCloud Keychain, Google Password Manager
or a password manager works on your other devices too.

- **Ways to sign in.** Under the passkey list, tick which combinations
  may sign in: *Passkey*, *Password + code*, *Password*, *Passkey +
  password*, *Passkey + code*, *Passkey + password + code*. Your first
  passkey turns on *Passkey* alongside what you had. For a combination
  that starts with a passkey, use the passkey button: once the passkey
  is accepted, the page asks for the rest.
- **Changes need your password**, and the code when two-factor is on:
  adding or removing a passkey, and changing the ways to sign in. A
  stolen session alone can't add its own passkey or loosen sign-in.
  These changes don't sign other devices out; use *sign out everywhere*
  for a lost device after removing its passkey.
- **Where they work.** Browsers only allow passkeys over HTTPS (or on
  `localhost`), and never at an IP address. A passkey belongs to the
  host it was made at: one made at `ccpipe.example.org` doesn't work at
  `192.168.1.5:8080`. Under `CCPIPE_BEHIND_TLS=1` that host is the one
  in `CCPIPE_ALLOWED_ORIGINS`.
- **What's checked.** Every sign-in requires user verification (the
  biometric or PIN). Each challenge is single-use and expires after
  5 minutes. Sign-in options name no credentials, so the login page
  reveals nothing about the account.
- **Locked out?** The password always works when none of the chosen ways
  can be used, for example after removing the last passkey. But if every
  ticked way needs a passkey and you lose every device holding one, the
  password doesn't stand in (Settings warns before saving such a
  choice). With shell access to the host, delete the `signin` field (and
  `passkeys`, to drop them all) from `~/.local/state/ccpipe/credentials`
  and restart ccpipe: sign-in is back to password (+ code).

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

### Text-to-speech

ccpipe POSTs each utterance to an **OpenAI-compatible
`/v1/audio/speech` endpoint** and streams the audio back to the
browser; the voice list in Settings → Voice is fetched from
`/v1/audio/voices` on the same endpoint. Any TTS service that
implements this shape works — OpenAI's actual API, a self-hosted
compatible proxy, or one of several open-source TTS wrappers.
ccpipe doesn't care which you pick.

The default and recommended option is **Kokoro-FastAPI** —
<https://github.com/remsky/Kokoro-FastAPI> — which wraps the
[Kokoro](https://huggingface.co/hexgrad/Kokoro-82M) TTS model and
ships several install paths (GPU Docker image, CPU Docker image,
bare-metal Python). Follow the upstream README for the path that
fits your hardware.

Once your TTS service is reachable, point ccpipe at it and enable TTS
in your ccpipe systemd drop-in:

```ini
Environment=CCPIPE_TTS=kokoro
Environment=CCPIPE_KOKORO_URL=http://localhost:8880
Environment=CCPIPE_TTS_VOICE=bf_emma         # optional initial default
```

The env-var name is historical — `CCPIPE_KOKORO_URL` can point at any
OpenAI-compatible audio endpoint, not just Kokoro-FastAPI. TTS-related
Settings (voice, speech rate, "what to read aloud", progress notes,
per-session mute) are then editable from Settings → Voice and persist in
`~/.local/state/ccpipe/config.json` under `tts`:

| Key | Default | |
|---|---|---|
| `voice` | `bf_emma` | Kokoro voice id |
| `speech_rate` | `1.0` | 0.5–2.0 |
| `scope` | `last_paragraph` | `full`, `first_paragraph`, `first_sentence`, `last_paragraph`, `last_sentence`, `last_question` or `off` |
| `progress` | `false` | Also read the notes Claude writes between tool calls, not only the reply that ends its turn |

Questions and permission prompts are read out whatever the scope,
unless it is `off`.

## Voice-input behaviour (Settings → Voice → "voice input")

ccpipe orchestrates the "release push-to-talk" signal from the backend
based on actual pipeline state, not a fixed client-side delay. The
client sends a `mic_stop` message when the browser mic stops; the
backend then waits for the in-flight audio to drain through Pulse and
adds a configurable safety pad before writing the release keystroke
itself.

Five settings in Settings → Voice → "voice input":

- **Auto-stop on silence** — when on, the client-side VAD ends the
  recording after sustained silence. When off, the mic is strictly
  tap-to-stop. (Claude Code also ends a recording by itself after
  15 s without new words, and caps one at 2 minutes; the mic button
  doesn't follow that yet, so it shows recording until you tap it.)
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
- **Voice processing on this device** (default on) — echo
  cancellation, noise suppression and auto gain on the browser's mic.
  On Firefox for Android any of them makes Firefox treat the mic as a
  phone call, so Android switches into call mode for each recording
  and back after it; a start that races the switch can leave the mic
  silent (`MicBlocked` below). Off avoids call mode, but without auto
  gain Claude may miss quiet speech. Stored in the browser, so it
  applies to that device only, from its next recording.

The first four persist in `~/.local/state/ccpipe/config.json` under the
`mic` key.

### How ccpipe drives `/voice`

ccpipe runs `/voice` in tap mode and presses its key (Alt+K) on the
server, never blind. Claude ignores the key while it's transcribing,
and while idle with text in its prompt box, so every press is checked
against Claude's footer: **REC · tap to send** while recording,
**processing…** while transcribing, nothing when idle.

- **Start:** wait out *processing…*, press only if Claude isn't
  already recording, then wait for *tap to send* before the browser
  streams any audio. If Claude won't start, the mic says why.
- **Stop:** after the drain and the submit pad (above), press only if
  Claude is still recording.
- After each of its own presses, ccpipe waits (up to 1 s) for Claude
  to redraw before trusting the screen again. Without that, a start
  right after a stop read the old **REC** as "already recording" and
  the new recording was lost.

### Dictation in the journal

`journalctl --user -u ccpipe | grep -E 'mic_|ptt'` shows each
recording:

| Line | Meaning |
|---|---|
| `mic_start: armed` | Claude started recording. `already recording` means it was recording already; anything else is the reason it didn't start. |
| `mic_stop: bytes=… drops=… level speech=… floor=… peak=… dBFS` | The recording: bytes sent (32000 per second), audio lost because the pipe was full, and how loud it was. *speech* is the loudest tenth of the recording, *floor* the quietest. Speech around -15 dBFS is healthy. |
| `ptt-release skipped: Claude isn't recording` | Claude had already stopped by itself, so no stop key was sent. |
| `mic_error from client: <name>: …` | The browser's mic failed. `NotAllowedError`: access refused. `MicBlocked`: the browser's audio engine never started. `MicSlowStart`: it started, but took over 0.3 s. These also give the time since the last recording stopped, and whether voice processing was on. |

## Session lifecycle

### New sessions drop to a shell when claude exits

Every new tmux session is launched with `claude; exec $SHELL -i`
(`$SHELL` falls back to `/bin/bash`). When the user exits claude
the pane doesn't die — it lands at an interactive shell prompt in
the session's original working directory. Re-attaching from the
picker drops you back at that prompt; typing `claude` relaunches it
in the right cwd. Without this wrapper the only-pane closed when
claude exited, the session was destroyed, and the next attach
auto-created a fresh session in `$HOME`.

The `--resume <uuid>` path goes through the same wrapper, so resumed
conversations also leave you at a shell when claude exits.

### Remembered, stopped and sticky sessions

Every session is remembered (`session_meta.json`): its directory, the
Claude conversation it was on, and when it was created and last opened.
A session that isn't running — after a reboot, an idle stop, or
"stop" from its kebab (`⋮`) menu — stays on the landing page under
**stopped**, and opening it resumes it: a tmux session of the same name
in the same directory, running `claude --resume <its conversation>` (or
`claude --continue` when the conversation or its transcript isn't
there), then `exec $SHELL -i`. A session that was never seen running
Claude (a plain shell started over ssh, say) comes back as a shell in
its directory instead. "Forget" removes it for good.

If it can't be resumed — its directory was deleted or moved outside the
allowed root — opening it says why and leaves its record alone; it is
not replaced by a fresh Claude in `$HOME`. A terminal that was open on
a session when it was stopped, forgotten or `exit`ed goes back to the
landing page with a note saying so: its reconnect never starts the
session again (the WebSocket's `reattach=1`).

**Settings → Sessions** (server-side, `config.json` → `sessions`):

| setting | default | |
|---|---|---|
| Start at boot · sticky | 5 | the most recently used sticky sessions started when the machine boots |
| Start at boot · other | 0 | the same for non-sticky sessions |
| Stop sticky sessions idle for | 1 day | 0 = never |
| Stop other sessions idle for | 2 h | 0 = never |

"At boot" means the tmux server started with the backend — every session
died since ccpipe last ran (a reboot, or tmux itself was killed). A
backend restart (a deploy) finds the tmux server older than itself and
starts nothing, even when every session is stopped, so stopped sessions
stay stopped.

**Idle** is deliberately hard to reach (`backend/ccpipe/lifecycle.py`).
A session counts as in use — and its idle clock restarts — while a
terminal or tile is attached, its pane produces output or input, Claude
Code reports itself busy or running a background shell task (its own
status in `~/.claude/sessions/<pid>.json`), its footer shows a background
MCP task (Claude's status says idle while one runs, so the footer is read), a Claude tool command runs
(any process carrying `~/.claude/shell-snapshots/`, even a `sleep` loop
at 0 % CPU), the pane's shell runs anything besides Claude (a server, a
build, an editor, in any window or pane), its processes use CPU, or a
question / permission prompt waits on screen. Claude's own helpers (MCP
servers, the Playwright browser, language servers) don't count. Only
sessions with Claude running in them are idle-stopped: a plain shell
costs next to nothing and is left alone. A session whose directory
isn't known is never stopped, and every auto-stop is logged. The landing page shows the countdown ("auto-stop
in 1h 20m").

Mark a session sticky from its kebab menu; a pin glyph shows it.
Renaming a session keeps its record, sticky flag, draft and layout. Note
that a stopped Claude's in-memory state is not kept — only the
conversation history Claude itself persists.

### System status

The landing page shows CPU and memory with ten minutes of history, the
root filesystem's free space, how much memory the sessions use, and per
session its CPU, its memory (PSS of its whole process tree) and its
project directory's size. Session CPU is read like `top` (100% = one core
busy) every 15 s over the whole process tree, and includes the time of
children that have already exited (`cutime`/`cstime`), so a build or test
run made of many short processes doesn't read as idle. Inside a session
the same figures sit beside the ping (a bar meter on a phone — tap it for
details). Per device, Settings
→ Display → *status & graphs* turns each part off and sets the refresh
interval — switched off, a part stops asking the server too. Session
memory is re-read at most once a minute per process; project sizes are
measured with `du` at idle CPU / IO priority every 15 minutes. Both only
while someone is looking.

### Claude plan usage

How much of the Claude subscription's usage limits is used, in the top
bar, its pop-up and the landing page (`ccpipe/plan_usage.py`). Two
sources, both `claude` reporting on itself; ccpipe never reads the login
token:

- **The status line.** ccpipe starts Claude as `claude --settings
  '{"statusLine": …}'`, naming `ccpipe/statusline_record.py` as the
  status-line command. After each reply Claude hands it JSON with
  `rate_limits` (the 5-hour and weekly windows' used % and reset time)
  and the context window's fill. The recorder saves that to
  `~/.local/state/ccpipe/statusline/<tmux server pid>_<pane>.json`, then
  runs your own status-line command with the same input. It looks in the
  project's `.claude/settings.local.json`, then its `.claude/settings.json`,
  then `~/.claude/settings.json`, the order Claude reads them. Your
  `padding` and `refreshInterval` carry over. With no status line of your
  own it prints nothing, and Claude draws no status row. Claude's docs say
  any configured status line hides most of the footer's key hints, such
  as *esc to interrupt*. Sessions started before the change, and a
  `claude` you type yourself in a session's shell, don't report.
- **`/usage`, headless.** `claude -p /usage --output-format stream-json`
  with MCP servers and hooks off and no saved session, run in
  `~/.local/state/ccpipe/plan-usage/`. Its answer carries every meter
  the server reports, including per-model weekly limits, plus the usage
  credits. It makes no model call and uses no tokens. Measured
  2026-11-03: 13 s, 13.6 s of CPU, 391 MB peak. It runs every
  `poll_minutes` while someone has ccpipe open, when the pop-up opens on
  figures more than 5 minutes old, and never twice within 2 minutes. The
  endpoint behind `/usage` is itself rate limited, and `/usage` in your
  own sessions then shows *rate limited until …*, so don't set the
  interval low. After a failure (including that rate limit) it waits the
  interval, then doubles the wait up to an hour. The last reading is
  kept in `plan-usage/usage.json`, so a restart doesn't start from
  nothing.

The readout is the latest `/usage` report, with the status line's
figures laid over the meters they cover when they're newer. A window
whose reset time has passed reads 0 % until the next reading.

In `config.json` (restart to apply, like the rest of the file):

```json
"plan_usage": {
  "enabled": true,
  "statusline": true,
  "poll_minutes": 15
}
```

- `enabled`: `false` turns the readout and the background check off.
- `statusline`: `false` starts new sessions without the recorder. You
  then get the `/usage` figures only, and no context fill per session.
- `poll_minutes`: 5 to 240.

Each check logs one line, such as `plan usage: week · all models 40%,
week · Fable 5% (13.2 s)`, or why it failed and when it tries again.

## Notifications

What needs you (`backend/ccpipe/attention.py`): every second the backend
reads each running Claude's `~/.claude/sessions/<pid>.json`. A status of
`waiting` is a question or permission prompt, read off the pane. A
`busy` → `idle` change held for 3 s while no page showed the session is
a finished turn. Pages get the list over their WebSocket. The session list
polls `GET /api/attention` every 3 s while it's on screen. See
[When Claude needs you](guide/notifications.md).

Push notifications (`notify.py`, `webpush.py`) are off until a device
turns them on in Settings → Notifications. Then:

- The backend makes a P-256 VAPID key (in `push.json`, `0600`) and POSTs
  each notification to that device's push service, encrypted for it
  (RFC 8291) and signed (RFC 8292). These are its only outbound
  connections apart from Kokoro.
- Push endpoints are limited to the browsers' push services:
  `*.push.services.mozilla.com`, `*.push.apple.com`,
  `fcm.googleapis.com`, `android.googleapis.com`, `*.notify.windows.com`.
  Anything else is refused at subscribe time.
- The signature's contact (`sub`) is the page's own `https://` origin, or
  the project's URL when ccpipe is reached over plain HTTP.
- A device the push service reports gone (404 / 410) is dropped, and so
  is one it has refused (400 / 401 / 403) for a week. Network errors,
  429 and 5xx don't count against a device. **Send a test** shows the
  last error.
- A device that had ccpipe on screen when a question came up is checked
  again every 5 s while the question waits (for up to 30 minutes), and is
  pushed to once ccpipe is no longer on its screen.

| Setting | Default | |
|---|---|---|
| `notify.push_wait_s` | `60` | While a ccpipe page is on screen on another device, wait this long before pushing (0–900 s). Settings → Notifications. |

Per device, and kept in that browser: the chime and the spoken name,
each for questions and for finished turns. Per device but kept on the
server with its subscription: whether it's pushed questions, finished
turns, and the question or reply text.

## Where state lives

- `~/.local/state/ccpipe/credentials` — argon2id hash, TOTP secret,
  passkeys (public keys only), the chosen ways to sign in, `0600`.
- `~/.local/state/ccpipe/session_secret` — used to sign session
  cookies. Rotating it invalidates all sessions. (Settings → Account →
  "sign out everywhere" does the same without touching files.)
- `~/.local/state/ccpipe/revoked_sessions.json` — ids of logins that
  were signed out, kept until their cookies would have expired anyway
  (beside the credentials file if `CCPIPE_CREDENTIALS_FILE` moves it).
- `~/.local/state/ccpipe/drafts.json` — each session's prompt-box draft
  (what's typed but not yet sent), shared across devices; capped per
  draft, drafts untouched for 30 days are dropped. `0600`.
- `~/.local/state/ccpipe/prompts.json` — the saved-prompts library
  (name → text), shared by every session and device. `0600`.
- `~/.local/state/ccpipe/layouts.json` — each session's tile layout
  (which other sessions are tiled beside it, and how). `0600`.
- `~/.local/state/ccpipe/push.json` — the server's push-notification
  (VAPID) key, made on first use, and each device's push subscription
  with its choices. `0600`. Deleting it turns push off everywhere; each
  device then turns it on again.
- `~/.local/state/ccpipe/initial_password.txt` — read-once `0400` file
  with the auto-generated password on first run. **Delete after
  reading.**
- `~/.local/state/ccpipe/config.json` — Settings-modal values.
- `~/.local/state/ccpipe/statusline/` — each session's last status-line
  reading from Claude (plan limits, context fill), one file per tmux
  pane; files untouched for 8 days are removed. `0700`.
- `~/.local/state/ccpipe/plan-usage/` — where `claude -p /usage` runs,
  and `usage.json`, its last reading and the retry state. `0700`.
- `~/.local/state/ccpipe/sticky_sessions.json` — `{name: {cwd}}` map
  of sticky sessions. Override the path with `CCPIPE_STICKY_FILE`.
- `~/.local/state/ccpipe/session_meta.json` — every remembered session:
  created / last-opened times, directory, Claude conversation id, and
  when and why it stopped. Override with `CCPIPE_SESSION_META_FILE`.
- `~/.claude/keybindings.json` — ccpipe idempotently adds the meta+k
  voice push-to-talk binding here on startup (see
  `ccpipe/settings_patch.py`).
