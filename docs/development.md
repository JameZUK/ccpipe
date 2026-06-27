# Development

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

The suite is ~270 cases; about 90 are skipped by default because they
require live credentials or external services. Set
`CCPIPE_TEST_PASSWORD`, `CCPIPE_EXTERNAL_BASE`, etc. to enable them
(see `tests/test_external_security.py` for the env-var list).

## Layout

```
backend/
  ccpipe/
    main.py            App factory: middleware, lifespan, /ws, router wiring
    auth.py            Argon2id passwords, TOTP, session middleware
    routes/
      auth.py          /api/auth/* (login, logout, TOTP, credentials)
      sessions.py      /api/sessions/*, /api/claude-sessions/*,
                       /api/sessions/{name}/sticky,
                       /api/sessions/{name}/history (transcript blocks,
                       before/after paged, for the /history view)
      fs.py            /api/fs/* (list, read, write, upload, download, ...)
      tts.py           /api/tts/* (voices, config, speak, preview)
      mic.py           /api/mic/config (voice-input timing knobs)
      static.py        /, /view (Markdown viewer), /history (conversation
                       view), /manifest.webmanifest, /sw.js, icons
    tmux.py            libtmux wrapper for one-shot ops
    tmux_control.py    Long-lived `tmux -C` listener; pushes events
    tmux_setup.py      Sets server-wide default-shell etc. at startup
    ws.py              WebSocket handler, tagged binary frame protocol;
                       owns backend-orchestrated mic release-PTT timing
    pty_relay.py       PTY spawn + async read/write
    mic.py             Mic pipe writer (PCM → /tmp/ccpipe_mic.pipe);
                       tracks bytes/drops and estimates drain time
    tts.py             JSONL tail + Kokoro client + audio chunk fan-out
    sticky.py          {name: {cwd}} JSON store for sticky sessions;
                       lifespan in main.py recreates missing entries
                       on startup using `claude --continue; exec $SHELL -i`
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
    mobile.ts          Composer bar + modifier-key row for phone/tablet
    file-panel.ts      Adaptive file browser + inline editor
    viewer.ts          /view page: markdown-it + highlight.js + KaTeX +
                       Mermaid, DOMPurify-sanitised, live file reload
    history.ts         /history page: console-style conversation review,
                       lazy older paging + live tail (after cursor)
    md-chat.ts         Lean markdown renderer (markdown-it + highlight.js
                       + DOMPurify) shared by the /history prose blocks
    settings.ts        Tabbed settings dialog (Display / Voice / Account)
    mic.ts             Mic capture: getUserMedia + AudioWorkletNode +
                       config-driven VAD + max-record cap
    tts.ts             Chunked audio player + per-session mute
    ws.ts              WS client; JSON control (input/resize/ping/
                       tts_mute/mic_stop) + tagged binary frames
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
- `{"type":"ping"}` — keepalive
- `{"type":"tts_mute","value":bool}` — mirror of client mute state so
  the server can skip Kokoro round-trips while the user isn't listening
- `{"type":"mic_stop"}` — the client tore down its mic; server
  computes drain + pad and writes release-PTT to the PTY itself

Client → server binary frames are prefixed with a 1-byte channel tag:
- `0x01` — Int16 PCM mic audio (16 kHz mono)

Server → client text frames (JSON):
- `hello` — session name + TTS + voice availability
- `session_event` — tmux control-mode forwarded events
- `session_gone` — tmux session closed
- `tts_start` / `tts_end` — frame the audio that follows
- `pong` — keepalive reply
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
