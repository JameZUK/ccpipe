# macOS

Status: supported. Same browser UI, terminal handling, TTS, sessions,
and auth as Linux. The mic stack diverges from the Linux design — see
[Voice on macOS](#voice-on-macos) for why and how — but the user-facing
behaviour is the same: click the mic in the header, talk, click to
stop, dictated text lands in claude's prompt.

## Install

```bash
# Prereqs
brew install python@3.11 node tmux whisper-cpp
# Claude Code CLI, logged in once interactively
brew install --cask claude-code
claude   # complete OAuth, exit

# ccpipe
git clone https://github.com/<you>/ccpipe ~/Developer/ccpipe
cd ~/Developer/ccpipe
scripts/install.sh
```

`scripts/install.sh` detects macOS, renders the launchd plist into
`~/Library/LaunchAgents/com.ccpipe.app.plist`, boots it via `launchctl`,
and on first run downloads the whisper base.en model (~148 MB) into
`~/Library/Application Support/ccpipe/whisper-models/`. Idempotent —
re-run to upgrade.

Check it's running:

```bash
launchctl print "gui/$UID/com.ccpipe.app" | grep state
tail -F ~/Library/Logs/ccpipe/ccpipe.{log,err.log}
curl http://127.0.0.1:8080/api/health    # → {"status":"ok"}
```

Grab the initial password the same way as on Linux:

```bash
cat ~/.local/state/ccpipe/initial_password.txt
rm -P ~/.local/state/ccpipe/initial_password.txt   # secure delete
```

## Layout differences vs Linux

| | Linux | macOS |
|---|---|---|
| Supervisor | `systemd --user` | `launchd` LaunchAgent |
| Unit/plist | `~/.config/systemd/user/ccpipe.service` | `~/Library/LaunchAgents/com.ccpipe.app.plist` |
| Logs | `journalctl --user -u ccpipe` | `~/Library/Logs/ccpipe/ccpipe.{log,err.log}` |
| Voice backend | claude `/voice` reads a Pulse pipe-source the backend writes into | ccpipe transcribes locally via whisper-cpp and types the text into the PTY |
| Voice prereqs | `tmux`, `pactl`, Pulse/PipeWire | `tmux`, `whisper-cpp`, ~148 MB model |
| Auto-start | `systemctl --user enable --now` + `loginctl enable-linger` | LaunchAgent in `~/Library/LaunchAgents` auto-loads at GUI login (no linger needed) |

## Voice on macOS

The Linux design routes mic audio from the browser through a FIFO,
exposes the FIFO as a virtual microphone via PulseAudio's
`module-pipe-source`, and lets `claude`'s built-in `/voice` push-to-talk
read from that mic and call the Anthropic transcription API.

That chain doesn't work on macOS today. We tried the obvious
adaptation — replace `module-pipe-source` with [BlackHole](https://github.com/ExistentialAudio/BlackHole)
plus a small Python bridge daemon that streams PCM from the FIFO to
BlackHole's output — and verified the audio path end-to-end: real
speech reaches BlackHole's input cleanly, recoverable by other macOS
audio apps. But claude's `/voice` keybinding handler has had a
regression since v2.1.83 (see [anthropics/claude-code#38690](https://github.com/anthropics/claude-code/issues/38690),
still open as of v2.1.144) where the meta+k keystroke is consumed but
recording never starts. The Linux flow can't work on macOS until
Anthropic fixes that — verifiable independently: open Terminal.app,
run `claude`, type `/voice`, press Option+K, speak; nothing transcribes.

ccpipe sidesteps the upstream bug entirely on macOS by running
transcription itself. The flow:

```
[Browser mic capture] → PCM via WebSocket → ccpipe backend
        ↓
        ccpipe.transcriber_macos.MicTranscriber buffers in memory
        ↓
        on mic_stop → whisper-cli transcribes locally
        ↓
        result text written straight into the PTY (as if typed)
        ↓
        claude sees a normal text input — no /voice involvement
```

This means on macOS:

- **No BlackHole, no virtual mic.** The mic stays your real Mac mic
  (or whatever you've set in System Settings → Sound → Input).
- **Transcription is offline.** Audio never leaves your Mac. whisper-cpp
  runs locally on Metal/CPU.
- **Latency is the model's responsibility.** Apple Silicon + base.en is
  ~real-time (sub-second for short utterances). Linux's path benefits
  from streaming transcription via the cloud API; ours runs after
  mic_stop. If that latency matters to you, swap the model — see below.

### Swapping the model

Set `CCPIPE_WHISPER_MODEL` to any ggml-format whisper model file. Larger
= more accurate, slower; smaller = faster, less accurate. base.en is a
good default for English dictation.

```bash
# Example: faster, slightly less accurate
curl -fL --progress-bar \
  -o "$HOME/Library/Application Support/ccpipe/whisper-models/ggml-tiny.en.bin" \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.en.bin

# Drop-in via launchd plist override
mkdir -p ~/Library/LaunchAgents/com.ccpipe.app.plist.d
# ... edit your plist or use an override mechanism; simplest is to set
# the env var in the plist directly and `launchctl kickstart -k`.
```

Or use a non-Homebrew `whisper-cli` binary via `CCPIPE_WHISPER_BIN`.

### What about claude `/voice` if Anthropic fixes the upstream bug?

The local-transcribe path doesn't depend on claude `/voice` and won't
benefit from the fix on its own — it just won't get worse either. If
the fix ships and you'd rather route through claude (e.g. for streaming
transcription or to avoid hosting whisper), point your install at a
fork that re-enables a BlackHole bridge. The infra is straightforward;
look at the git history of this branch for the bridge prototype if you
want a starting point.

## Reverse proxy / TLS / port forwarding

Same as the Linux instructions in [deployment.md](deployment.md). The
backend binds `0.0.0.0` by default; firewall `:8080` to your reverse
proxy host. macOS has no `pf` analogue of `ufw` baked into the docs —
use Little Snitch, the Apple "Block all incoming" toggle in System
Settings → Network → Firewall, or whatever you already trust.

## Known limitations

- **Same-host browser caveat.** If you open ccpipe in a browser on the
  same Mac that runs the backend, make sure System Settings → Sound →
  Input is *not* set to a virtual loopback device (BlackHole, Loopback,
  Audio Hijack, etc.). The browser captures whatever's set there — a
  loop will record silence. Real Mac mic is fine.
- **No native streaming transcription.** ccpipe transcribes after
  mic_stop, not while you're talking. For 5-10s utterances this is
  imperceptible; for very long monologues you'll feel the wait at the
  end.
- **Voice depends on whisper-cpp.** If `brew install whisper-cpp` fails
  or the model download is interrupted, the mic FAB is hidden (same
  fail-soft behaviour as Linux when Pulse can't load
  `module-pipe-source`). Re-run `scripts/install.sh` to fix.

## Troubleshooting

```bash
# Backend up?
launchctl print "gui/$UID/com.ccpipe.app" | grep -E "state|pid"
curl http://127.0.0.1:8080/api/health

# Logs
tail -F ~/Library/Logs/ccpipe/ccpipe.{log,err.log}

# whisper-cli available?
command -v whisper-cli
ls -lh ~/Library/Application\ Support/ccpipe/whisper-models/

# Restart after a code edit
launchctl kickstart -k "gui/$UID/com.ccpipe.app"
```

Specific voice issues:

- **"mic FAB doesn't appear"** — the WS-hello sends `voice: false` when
  whisper-cli or the model is missing. Check `ccpipe.err.log` for
  `transcriber unavailable` near startup.
- **"recording but no text appears"** — check the backend log for
  `whisper-cli exited <n>` lines, and run whisper manually on a recent
  utterance dumped from the buffer to reproduce. Most failures are
  model-path issues (env var pointed somewhere that doesn't exist).
- **"text appears but is garbled"** — the model probably picked up
  background noise or the wrong language. base.en is English-only; use
  one of the multilingual `ggml-base.bin`/`ggml-small.bin` variants if
  you dictate in other languages.
