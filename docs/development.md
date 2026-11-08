# Development

## Architecture

ccpipe runs as a `systemd --user` service on the host. There's no
Docker: the backend is tightly coupled to host state (tmux, `~/.claude`,
project files, PulseAudio, the mic pipe), and containerising it created
more friction than it solved.

```
[Browser] ──WSS── [nginx] ── [uvicorn (user service)]
                                   │
                                   ├── tmux client (talks to user's tmux server)
                                   ├── /tmp/ccpipe_mic.pipe (Pulse pipe-source)
                                   ├── watches ~/.claude/projects/*.jsonl → Kokoro
                                   ├── reads ~/.claude/sessions/<pid>.json (Claude's busy / idle)
                                   └── reads /proc (system + per-session CPU, memory)
```

The terminal stack is unchanged: tmux owns the session, xterm.js renders
it, and the unmodified `claude` CLI does the API work. ccpipe is the
bridge in the middle.

State it keeps (`~/.local/state/ccpipe`): credentials and passkeys,
settings, the record of every session (directory, Claude conversation,
times), sticky sessions, prompt-box drafts, saved prompts and tile
layouts. See
[Configuration → Where state lives](configuration.md#where-state-lives).

## Running without the systemd unit

```bash
# Backend (auto-reload on changes)
cd backend && . .venv/bin/activate
uvicorn ccpipe.main:app --reload --port 8080

# Frontend (Vite dev server with proxy to backend)
cd frontend && npm run dev
# Visit http://localhost:5173
```

## Tests

```bash
cd backend && . .venv/bin/activate
pip install -e ".[dev]"
pytest -v
```

The suite is ~310 cases; about 95 are skipped by default because they
require live credentials or external services. Set
`CCPIPE_TEST_PASSWORD`, `CCPIPE_EXTERNAL_BASE`, etc. to enable them
(see `tests/test_external_security.py` for the env-var list).

## Layout

```
backend/
  ccpipe/
    main.py            App factory: middleware, lifespan, /ws, router wiring
    auth.py            Argon2id passwords, TOTP, passkey store, sign-in
                       methods (which factor combinations sign in),
                       session middleware
    passkeys.py        WebAuthn ceremonies (py_webauthn): one-use
                       challenges, relying party from the page's origin
    routes/
      auth.py          /api/auth/* (login, logout, TOTP, credentials,
                       passkey register/delete/options, sign-in methods)
      sessions.py      /api/sessions/*, /api/claude-sessions/*,
                       /api/sessions/{name}/sticky|stop|resume|forget,
                       /api/sessions/remembered, /api/sessions/previews,
                       /api/session-config,
                       /api/sessions/{name}/history (transcript blocks,
                       before/after paged, for the /history view)
      fs.py            /api/fs/* (list, read, write, upload, download,
                       download-zip, dir-summary, ...)
      prompts.py       /api/prompts (saved prompts: list, save, delete)
      layouts.py       /api/layouts/{session} (tile layouts, layouts.json)
      tts.py           /api/tts/* (voices, config, speak, preview)
      mic.py           /api/mic/config (voice-input timing knobs)
      system.py        /api/system (host + per-session resource use)
      static.py        /, /view (Markdown viewer), /history (conversation
                       view), /manifest.webmanifest, /sw.js, icons
    fs_zip.py          Folder → streamed .zip: fd-based no-symlink walk,
                       denylist + hidden filtering, size pre-flight
    drafts.py          Per-session composer drafts (drafts.json), relayed
                       live between devices by ws.py
    safe_write.py      Atomic saves that keep the file's mode and write
                       through symlinks (file panel + settings patch)
    paths.py           state_dir() shared by auth/config/sticky;
                       claude_project_dir() — Claude Code's
                       ~/.claude/projects/<encoded cwd>
    tmux.py            libtmux wrapper for one-shot ops
    tmux_control.py    Long-lived `tmux -C` listener; pushes events
    tmux_setup.py      Sets server-wide default-shell etc. at startup
    tmux_ui.py         tmux toolbar: fixed action table (windows, panes,
                       copy-mode) behind /api/sessions/{name}/tmux, plus
                       window/size queries for watch tiles
    ws.py              WebSocket handler, tagged binary frame protocol;
                       owns backend-orchestrated mic release-PTT timing
    pty_relay.py       PTY spawn + async read/write
    mic.py             Mic pipe writer (PCM → /tmp/ccpipe_mic.pipe);
                       tracks bytes/drops and estimates drain time
    tts.py             JSONL tail + Kokoro client + audio chunk fan-out;
                       which records are Claude's reply, markdown → speech
    attention.py       What needs you in every session: Claude waiting on
                       a question / permission prompt, or a finished turn
                       no page saw (Claude's session files, once a second)
    claude_asks.py     Reads a question / permission dialog off the pane
    notify.py          Push subscriptions, and which devices get a push
    webpush.py         Web Push: RFC 8291 encryption, VAPID (RFC 8292)
    sticky.py          {name: {cwd}} JSON store for sticky sessions
    session_meta.py    Every remembered session: times, cwd, Claude
                       conversation id, stopped / why
    lifecycle.py       Resume / stop / forget, which sessions start at
                       boot, and the careful idle auto-stop
    sysstats.py        Host CPU/mem/disk sampler + per-session PSS/CPU,
                       project sizes (du at idle priority)
    proctree.py        /proc process-table snapshot (busy detection,
                       per-session memory); run_argv, the one helper
                       for short subprocess queries (tmux, du)
    settings_patch.py  Idempotently adds voice keys to ~/.claude/settings.json
  tests/               pytest suite
  pyproject.toml

frontend/
  src/
    main.ts            Entry: session picker → terminal; lazy-loads heavy UI
    api.ts             Shared fetch helper (CSRF header + same-origin + JSON)
    session-picker.ts
    terminal.ts        xterm.js setup, resize, input wiring; mobile
                       touch-scroll → SGR wheel forwarding when the app
                       owns the mouse (so Claude scrolls its own view)
    mobile.ts          Composer bar (draft sync, saved prompts) on every
                       layout + modifier-key row for phone/tablet
    layout.ts          Tile layouts: split tree around the main terminal,
                       dividers, presets, saved per session on the server
    tile.ts            One tile: its own socket + xterm (DOM renderer);
                       watch (read-only, ignore-size, scaled) or live
    tiles-menu.ts      Toolbar tiles menu (add session, presets, clear)
    tmuxbar.ts         tmux controls row for the focused session
    share.ts           PWA share-target capture + review helpers
    inline-prompt.ts   In-app prompt/confirm dialogs (PWAs suppress the
                       native ones)
    file-panel.ts      Adaptive file browser + the editor (its footer:
                       indent style, tab width, whitespace, wrap)
    file-viewer.ts     Full-screen viewer: text, Markdown, images, PDF,
                       audio, video
    code-view.ts       CodeMirror 6 for the viewer (read-only) and the
                       editor: language by name/#!, theme, indentation
                       detection, CRLF + BOM kept; loaded on demand,
                       each language its own chunk
    hljs-extra.ts      highlight.js languages beyond "common" (PowerShell,
                       batch, …) for Markdown code blocks; the rarer
                       ones in hljs-extra-more.ts, for /view only
    viewer.ts          /view page: markdown-it + highlight.js + KaTeX +
                       Mermaid, DOMPurify-sanitised, live file reload
    docs-drawer.ts     Viewer's document drawer: search, tree, recents
    doc-recents.ts     Recent / recently-modified docs (viewer + main)
    view-url.ts        The one /view URL builder
    close-page.ts      ✕ close for /view and /history (installed app)
    history.ts         /history page: console-style conversation review,
                       lazy older paging + live tail (after cursor)
    md-chat.ts         Lean markdown renderer (markdown-it + highlight.js
                       + DOMPurify) shared by the /history prose blocks
    settings.ts        Tabbed settings dialog (Display / Sessions /
                       Notifications / Voice / Account / Debug)
    settings-passkeys.ts  Account → passkeys and ways to sign in
    settings-notify.ts Notifications → chime, spoken name, push per device
    audio-session.ts   Safari's audio session: "ambient" (mixes with other
                       apps' audio) at rest and for chimes, "auto" for
                       Claude's voice and the microphone
    attention.ts       What needs you on the page: the top bar's bell and
                       its drop-down, banners, the chime (made in code)
                       and the spoken name (Web Audio, which stays off
                       the lock screen's player), the tab title count,
                       per-device choices
    push.ts            Push on this device: permission, push-sw.js,
                       subscribe; a tapped notification opens its session
    passkey.ts         WebAuthn in the browser (base64url ⇄ ArrayBuffer,
                       options fetched before the tap for Safari)
    mic.ts             Mic capture: getUserMedia + AudioWorkletNode +
                       config-driven VAD + max-record cap
    tts.ts             Chunked audio player + per-session mute
    ws.ts              WS client; JSON control (input/resize/ping/
                       tts_mute/mic_stop/draft) + tagged binary frames;
                       auto-reconnect (backoff ≤ 8 s, 10 s connect
                       timeout, redial on resume / online)
    styles.css
  public/
    manifest.webmanifest
    sw.js              Self-retiring worker (retires any an old install
                       registered; nothing registers it now)
    push-sw.js         Push notifications' worker: shows them, opens the
                       session tapped, renews the subscription; registered
                       only when a device turns push on. No fetch handler
    mic-worklet.js     AudioWorkletProcessor: 48k→16k mono Int16 PCM
  index.html
  vite.config.ts

nginx/ccpipe.conf      Sample server block
scripts/
  setup-virtual-mic.sh Load/unload Pulse module-pipe-source on host (up/down)
  install.sh           Bundled installer (venv + frontend build + systemd)
systemd/
  ccpipe.service              Web service (FastAPI + uvicorn)
  ccpipe-virtual-mic.service  Loads the Pulse pipe-source at login
docs/
  deployment.md       Reverse proxy + TLS + firewall + troubleshooting
  configuration.md    Env vars + voice setup + login + state files
  debugging.md        PTY → WS stream debug playbook (byte accounting,
                      /api/debug/sessions, doctor regression modes)
  development.md      This file
  threat-model.md     Design-level threat model (post-pen-test)
```

## WebSocket protocol

Client → server text frames (JSON):
- `{"type":"input","data":"…"}` — raw bytes to the PTY
- `{"type":"resize","cols":N,"rows":N}` — terminal size
- `{"type":"ping","id":N}` — keepalive, and the round trip the ping
  shows: every 30 s, and every 2 s while the ping's drop-down is open
- `{"type":"tts_mute","value":bool}` — mirror of client mute state so
  the server can skip Kokoro round-trips while the user isn't listening
- `{"type":"mic_stop"}` — the client tore down its mic; server
  computes drain + pad and writes release-PTT to the PTY itself
- `{"type":"draft","text":"…"}` — the prompt box changed; the server
  stores it as the session's draft and relays it to the session's other
  connections (last write wins). Routed before the control-frame sniffs.
- `{"type":"attention",…}` — sent after `stream_ready` and on every
  visibility change: `visible` (the page is on screen: a page showing a
  session has seen its finished turn, and a device with one on screen
  isn't pushed to; the dial URL carries `&visible=0|1` too, so a hidden
  page reconnecting doesn't count as looking). Also `dismiss` (clear
  finished turns: a session name, or `"*"`). The name of another session
  that needs you isn't said over the socket: the page fetches it from
  `POST /api/attention/say` and plays it with the chime, through Web
  Audio, so it stays off the lock screen's player.

Client → server binary frames are prefixed with a 1-byte channel tag:
- `0x01` — Int16 PCM mic audio (16 kHz mono)

Server → client text frames (JSON):
- `hello` — session name, cwd, TTS + voice availability, and the
  session's current `draft`
- `draft` — the draft was changed on another device
- `session_event` — tmux control-mode forwarded events
- `session_gone` — tmux session closed
- `tts_start` / `tts_end` — frame the audio that follows; a reply is
  spoken a sentence at a time, and `tts_start` carries the sentence
  (`text`), the whole reply (`utterance`) and its number (`id`)
- `claude_ask` — Claude is waiting on a question or permission prompt
  in this session: `kind`, `title`, `text`, and `id` of its spoken copy
  (or null)
- `attention` — what needs you in every session, after `stream_ready`
  and on each change: `items` (`id`, `session`, `level` "ask" | "done",
  `kind`, `title`, `body`, `since`) and `new` (the ids that just
  started, for the page to ring)
- `pong` — keepalive reply, echoing the ping's `id` so each pong is
  timed against its own ping (an id-less ping gets an id-less pong)
- `pty_exited` — PTY child closed

Server → client binary frames (1-byte prefix):
- `0x00` — raw PTY output
- `0x02` — encoded TTS audio chunk

## Debugging the PTY → WebSocket stream

If you see (or a user reports) terminal content disappearing until a
page refresh restores it, the diagnostic playbook lives in
[`docs/debugging.md`](debugging.md). Short version:

1. `journalctl --user -u ccpipe.service -f | grep "ws closed\|send_bytes(pty) failed"`
   — every WS close logs flow stats; `bytes_lost > 0` is the smoking
   gun.
2. `curl -b cookies https://<host>/api/debug/sessions | jq` — live
   per-WS byte counters for in-flight connections.
3. `python scripts/scrollback-doctor.py --realistic` — comprehensive
   byte-pattern regression test (SGR, cursor, UTF-8, burst, etc.).
4. `pytest backend/tests/test_ws_byte_accounting.py` — pins the
   `read == sent + lost` invariant and no-silent-drop contract.

The full debugging guide covers the reproducer (briefly DROP `:8080`
via iptables to induce a forced drop and verify recovery), how to
read the counters, and what to look at when `bytes_lost == 0` but
the symptom persists.

## Source of truth for the voice-input pipeline

The release-PTT timing lives in `backend/ccpipe/ws.py` —
`_release_ptt_after()` and the `mic_stop` handler. The drain estimate
math is in `backend/ccpipe/mic.py::MicWriter.estimate_drain_seconds()`.
The four user-tunable knobs are defined in
`backend/ccpipe/config.py::MicConfig`. If you're touching any of those,
read all three.

## Temporary workarounds

Things in the code that exist only to dodge an upstream bug, and how to
take each one out once the fix ships.

### WebGL glyph-atlas merge guard (added 2026-11-02)

**Bug:** `@xterm/addon-webgl` ≤ 0.19 corrupts glyphs after its texture
atlas merges four pages into one: the merged page can reuse an index and
version number, so the GPU copy isn't re-uploaded and glyphs are drawn
from the old page. Seen on the Firefox Android PWA as garbled or blank
letters in coloured/bold text while the buffer text was correct. Fixed
upstream in [xtermjs/xterm.js#5883](https://github.com/xtermjs/xterm.js/pull/5883),
shipped only in `@xterm/addon-webgl` 0.20 betas (needs `@xterm/xterm`
6.1).

**Workaround:** `frontend/src/terminal.ts`, the block marked
`WORKAROUND (temporary): addon-webgl atlas page-merge bug`. After each
frame it reads the atlas page count (private internals) and, three
pages short of the merge point, rebuilds the WebGL addon so a fresh
atlas starts; no merge ever happens. The debug snapshot reports it as
`terminal.webglAtlas` (`pages`, `limit`, `merged`, `rebuilds`,
`guardBroken`). `merged > 0` means a merge got through anyway (one frame
drew more new glyphs than the headroom).

**Roll back when** `@xterm/xterm` 6.1 and `@xterm/addon-webgl` 0.20 are
released as stable (`npm view @xterm/addon-webgl dist-tags` shows
`latest` ≥ 0.20):

1. Upgrade `@xterm/xterm` and every `@xterm/addon-*` together, and
   re-check the private-internals patches in `terminal.ts` (the
   scroll-up patch reports `suPatchInstalled` in the debug snapshot).
2. Delete the `WORKAROUND` block and the `webglAtlas` entry in
   `getDebugState()`.
3. Delete this section.

Keep **Settings → Display → WebGL renderer** (the per-device off switch):
it's a useful escape hatch for any future GPU problem, not part of the
workaround.
