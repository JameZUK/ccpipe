# macOS

**Status: experimental.** The macOS port was written without a Mac to
test on, so paths, argument quoting, and launchd-specific quirks may
need adjustment. Voice, terminal handling, TTS, sessions, and auth use
the same code as Linux except for the mic stack (see [Voice on
macOS](#voice-on-macos) below). If you run this on a real Mac, please
report what works and what doesn't.

## Install

```bash
# Prereqs
brew install python@3.11 node tmux whisper-cpp

# Claude Code CLI, logged in once interactively
brew install --cask claude-code
claude   # complete OAuth, exit

# ccpipe
git clone https://github.com/JameZUK/ccpipe ~/Developer/ccpipe
cd ~/Developer/ccpipe
scripts/install.sh
```

`scripts/install.sh` detects macOS, renders the launchd plist into
`~/Library/LaunchAgents/com.ccpipe.app.plist`, boots it via `launchctl`,
and on first run downloads the whisper `base.en` model (~148 MB,
sha256-pinned) into `~/Library/Application Support/ccpipe/whisper-models/`.
Idempotent — re-run to upgrade.

Check it's running:

```bash
launchctl print "gui/$UID/com.ccpipe.app" | grep -E "state|pid"
tail -F ~/Library/Logs/ccpipe/ccpipe.{log,err.log}
curl http://127.0.0.1:8080/api/health    # → {"status":"ok"}
```

Grab the initial password the same way as on Linux, but use macOS's
secure-delete primitive:

```bash
cat ~/.local/state/ccpipe/initial_password.txt
rm -P ~/.local/state/ccpipe/initial_password.txt   # macOS: rm -P, not shred
```

## Differences from Linux

|                  | Linux                                         | macOS                                                                   |
| ---------------- | --------------------------------------------- | ----------------------------------------------------------------------- |
| Supervisor       | `systemd --user`                              | `launchd` LaunchAgent                                                   |
| Unit / plist     | `~/.config/systemd/user/ccpipe.service`       | `~/Library/LaunchAgents/com.ccpipe.app.plist`                           |
| Logs             | `journalctl --user -u ccpipe`                 | `~/Library/Logs/ccpipe/ccpipe.{log,err.log}`                            |
| Voice backend    | claude `/voice` reads a Pulse pipe-source     | ccpipe transcribes locally via whisper-cpp and types the text into PTY |
| Voice prereqs    | `tmux`, `pactl`, Pulse/PipeWire               | `tmux`, `whisper-cpp`, ~148 MB model                                    |
| Auto-start       | `systemctl --user enable --now` + linger      | LaunchAgent in `~/Library/LaunchAgents` auto-loads at GUI login         |
| Secure-delete    | `shred -u`                                    | `rm -P`                                                                 |

## Voice on macOS

The Linux design routes mic audio from the browser through a FIFO,
exposes the FIFO as a virtual microphone via PulseAudio's
`module-pipe-source`, and lets `claude`'s built-in `/voice`
push-to-talk read from that mic and call the Anthropic transcription
API.

That chain doesn't work on macOS today. The obvious port is to replace
`module-pipe-source` with [BlackHole](https://github.com/ExistentialAudio/BlackHole)
plus a small bridge daemon that streams PCM from a FIFO to BlackHole's
output. The audio path is solid — real speech reaches BlackHole's
input cleanly, recoverable by other macOS audio apps. But claude's
`/voice` keybinding handler has had a regression since v2.1.83 (see
[anthropics/claude-code#38690](https://github.com/anthropics/claude-code/issues/38690),
still open as of v2.1.144) where the meta+K keystroke is consumed but
recording never starts. The Linux flow can't work on macOS until
Anthropic fixes that — verifiable independently: open Terminal.app,
run `claude`, type `/voice`, press Option+K, speak; nothing transcribes.

ccpipe sidesteps the upstream bug entirely on macOS by running
transcription itself. The flow:

```
[Browser mic] → PCM via WebSocket → ccpipe backend
        ↓
        MicTranscriber buffers in memory (60 s cap)
        ↓
        on mic_stop → whisper-cli transcribes locally
        ↓
        result text injected straight into the PTY (as if typed)
        ↓
        claude sees a normal text input — no /voice involvement
```

Consequences:

- **No BlackHole, no virtual mic.** The mic stays your real Mac mic
  (or whatever you've set in System Settings → Sound → Input).
- **Transcription is offline.** Audio never leaves your Mac.
  whisper-cpp runs locally on Metal / CPU.
- **Latency lives in the model.** Apple Silicon + `base.en` is
  ~real-time (sub-second for short utterances). Linux's path benefits
  from streaming transcription via the cloud API; ours runs after
  `mic_stop`. If end-of-utterance latency matters, swap the model —
  see below.

### Swapping the model

Set `CCPIPE_WHISPER_MODEL` to any ggml-format whisper model file.
Larger = more accurate, slower; smaller = faster, less accurate.
`base.en` is a good default for English dictation.

```bash
# Faster, slightly less accurate
curl -fL --progress-bar \
  -o "$HOME/Library/Application Support/ccpipe/whisper-models/ggml-tiny.en.bin" \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.en.bin

# Point the LaunchAgent at the new model and restart
plutil -insert EnvironmentVariables.CCPIPE_WHISPER_MODEL \
  -string "$HOME/Library/Application Support/ccpipe/whisper-models/ggml-tiny.en.bin" \
  ~/Library/LaunchAgents/com.ccpipe.app.plist
launchctl kickstart -k "gui/$UID/com.ccpipe.app"
```

Or use a non-Homebrew `whisper-cli` binary via `CCPIPE_WHISPER_BIN`.

### What if Anthropic fixes the upstream bug?

The local-transcribe path doesn't depend on claude `/voice` and won't
benefit from the fix on its own — it just won't get worse either.
If the fix ships and you'd rather route through claude (e.g. for
streaming transcription, or to avoid hosting whisper), point your
install at a branch that re-enables a BlackHole bridge. The infra is
straightforward; the FIFO + Pulse parts of the Linux flow translate
almost directly.

## Reverse proxy / TLS / port forwarding

Same shape as Linux. The backend binds `0.0.0.0` by default;
firewall `:8080` to your reverse proxy host. macOS's built-in
firewall (System Settings → Network → Firewall → "Block all
incoming connections") works at a coarse grain; for per-port rules,
use `pf` directly or a tool like Little Snitch.

## Known limitations / caveats

- **Untested on real Apple hardware.** The port was written without a
  Mac to hand. The first user gets to find the launchd / curl / sha256
  / homebrew-path quirks the testing pass would have caught.
- **Same-host browser caveat.** If you open ccpipe in a browser on the
  same Mac that runs the backend, make sure System Settings → Sound →
  Input is **not** set to a virtual loopback device (BlackHole,
  Loopback, Audio Hijack, etc.). The browser captures whatever is set
  there — a loopback records silence. The real Mac mic is fine.
- **No native streaming transcription.** ccpipe transcribes after
  `mic_stop`, not while you're talking. For 5–10 s utterances the wait
  is imperceptible; for very long monologues you'll feel it.
- **Voice depends on whisper-cpp.** If `brew install whisper-cpp`
  fails or the model download is interrupted, the mic FAB is hidden
  (same fail-soft behaviour as Linux when Pulse can't load
  `module-pipe-source`). Re-run `scripts/install.sh` to fix.
- **No virtual-mic service unit.** The Linux build has a separate
  `ccpipe-virtual-mic.service` that creates the FIFO. macOS doesn't
  need it — there's no FIFO. If you see references to it in the
  Linux docs, mentally skip them.

## Troubleshooting

```bash
# Backend up?
launchctl print "gui/$UID/com.ccpipe.app" | grep -E "state|pid"
curl http://127.0.0.1:8080/api/health

# Logs (stderr has the interesting bits; stdout has access logs)
tail -F ~/Library/Logs/ccpipe/ccpipe.{log,err.log}

# whisper-cli + model
command -v whisper-cli
ls -lh ~/Library/Application\ Support/ccpipe/whisper-models/

# Restart after a code edit
launchctl kickstart -k "gui/$UID/com.ccpipe.app"
```

Specific voice issues:

- **"mic FAB doesn't appear"** — the WS-hello sends `voice: false`
  when `whisper-cli` or the model is missing. Check `ccpipe.err.log`
  for `transcriber unavailable` near startup.
- **"recording but no text appears"** — check the backend log for
  `whisper-cli exited <n>` lines. Most failures are model-path issues
  (env var pointed somewhere that doesn't exist) or a corrupt model
  file (re-run the installer; it sha256-verifies on the way in).
- **"text appears but is garbled"** — the model probably picked up
  background noise or the wrong language. `base.en` is English-only;
  use one of the multilingual `ggml-base.bin`/`ggml-small.bin`
  variants if you dictate in other languages.
- **"every utterance has a leading space"** — by design after the
  first utterance, so consecutive dictations don't collide
  (`hello`+`world` → `hello world`, not `helloworld`). The first
  utterance in a session has no leading space.
