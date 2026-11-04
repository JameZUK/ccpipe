# Installing ccpipe

[← Documentation](README.md)

- [Requirements](#requirements)
- [Platform support](#platform-support)
- [Install](#install)
- [First sign-in](#first-sign-in)
- [Using it away from home](#using-it-away-from-home)
- [Updating](#updating)

## Requirements

- Python 3.11+
- Node 20+ (to build the frontend only)
- `tmux` on PATH
- The `claude` CLI, installed and logged in (run `claude` once
  interactively to sign in)
- nginx or Caddy for HTTPS (a sample is in `nginx/ccpipe.conf`)
- PulseAudio or PipeWire, for voice only

## Platform support

The server side is built for and tested on **Linux**. The browser side
works in any modern browser (Linux, macOS, Windows, Android, iOS).

| Server | Status |
|---|---|
| Linux | Fully tested; this is what's actively used. |
| Windows via WSL2 | Should work (see below). Not tested by the maintainer; reports welcome. |
| Windows (native) | Not supported. A native port would have to replace systemd, the FIFO mic, PulseAudio and the bash install script; use WSL2. |
| macOS | Experimental: a launchd LaunchAgent and local whisper-cpp transcription. Ported without a Mac to test on. See [macOS](macos.md). |

### Windows (WSL2)

Install WSL2 with a recent Linux distribution from an elevated
PowerShell, reboot, then follow the [install steps](#install) in the Linux
shell:

```powershell
wsl --install                        # default Ubuntu
# or: wsl --install -d Debian
```

WSL2 on current Windows builds supports `systemd --user` (you may need
`systemd=true` under `[boot]` in `/etc/wsl.conf`) and PulseAudio via
WSLg. The browser on the Windows side connects to `http://localhost:8080`
(forwarded from WSL2 by default), or whatever address you put behind a
reverse proxy. Voice across the WSLg audio boundary hasn't been tested
end to end; if you try it, please report back.

### macOS

ccpipe runs as a `launchd` LaunchAgent under `~/Library/LaunchAgents/`.
Voice works differently from Linux: Claude's built-in `/voice` has an
unfixed regression on macOS
([anthropics/claude-code#38690](https://github.com/anthropics/claude-code/issues/38690)),
so ccpipe transcribes locally with
[whisper-cpp](https://github.com/ggerganov/whisper.cpp) and types the
result into the terminal. No virtual audio device is needed.

```bash
brew install python@3.11 node tmux whisper-cpp
brew install --cask claude-code   # if not already
```

Then follow the [install steps](#install). `install.sh` detects macOS,
renders the launchd plist, and downloads the whisper `base.en` model
(~148 MB, sha256-pinned, one time). Design, troubleshooting and settings
are in [macOS](macos.md). The port hasn't been tested on real Apple
hardware, so bug reports are welcome.

## Install

Install into the home directory of the user ccpipe should run as. It runs
as a `systemd --user` service and needs that user's `~/.claude`, tmux
server and Pulse socket. The usual place is `~/.local/share/ccpipe`; the
installer works from wherever you clone it.

```bash
git clone https://github.com/JameZUK/ccpipe ~/.local/share/ccpipe
cd ~/.local/share/ccpipe
scripts/install.sh
```

The installer creates a Python venv under `backend/.venv`, builds the
frontend, renders the systemd unit templates for this location, and
enables the two `systemd --user` units. Re-run it to upgrade.

On a development machine, skip the systemd units with
`scripts/install.sh --skip-units`. [Development](development.md) has a
fully manual setup and the dev workflow.

Check it's running:

```bash
systemctl --user status ccpipe
journalctl --user -u ccpipe -f
curl http://127.0.0.1:8080/api/health    # should print {"status":"ok"}
```

> **If `ccpipe-virtual-mic.service` fails** because `/tmp/ccpipe_mic.pipe`
> exists as a *directory* (left over from a Docker bind-mount in an old
> install), remove it:
> `sudo rmdir /tmp/ccpipe_mic.pipe && systemctl --user restart ccpipe-virtual-mic`.

## First sign-in

On first start ccpipe generates a password and writes it, once, to a
file only you can read. The username is your system username.

```bash
cat ~/.local/state/ccpipe/initial_password.txt
shred -u ~/.local/state/ccpipe/initial_password.txt   # after reading
```

Open `http://<host>:8080` and sign in. Change the username or password,
set up two-factor, or add a passkey under Settings → Account (see
[Signing in](guide/account.md)). To choose the first password yourself,
set `CCPIPE_AUTH_PASSWORD` before the first start (see
[Configuration](configuration.md#environment-variables)).

## Using it away from home

ccpipe speaks plain HTTP; put nginx or Caddy in front of it for HTTPS.
The microphone, notifications and passkeys only work over HTTPS (or on
`localhost`).

The short version: edit `nginx/ccpipe.conf`, install the bundled systemd
drop-in with `CCPIPE_BEHIND_TLS=1` and `--forwarded-allow-ips=<your proxy's
IP>`, and firewall `:8080` to the proxy host.
[Deployment](deployment.md) has the full recipe: same-host and off-host
setups, the drop-in, what `--proxy-headers` does, Cloudflare, and common
gotchas.

## Updating

```bash
cd ~/.local/share/ccpipe
git pull
backend/.venv/bin/pip install -q -c backend/constraints.txt -e backend
(cd frontend && npm ci && npm run build)
systemctl --user restart ccpipe
```

Re-running `scripts/install.sh` does the same. Open pages move to the new
build by themselves. See [Deployment → Updating an existing install](deployment.md#updating-an-existing-install).
