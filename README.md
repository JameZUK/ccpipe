# ccpipe

<p align="center">
  <img src="docs/logo.svg" alt="ccpipe" width="160">
</p>

ccpipe puts your [Claude Code](https://claude.com/claude-code) sessions
in a browser. Install it on the machine where you already run `claude`
in tmux, and your phone, tablet or laptop gets the same live terminal,
plus what a terminal on a phone lacks: a prompt box, voice in and out,
a file browser and an overview of every session.

Claude Code itself is unchanged. tmux owns the sessions, the unmodified
`claude` CLI makes every API call, and ccpipe relays the terminal over a
WebSocket. It suits a headless machine well: a homelab VM with no
screen, soundcard or microphone works, because the microphone is a
software one and speech plays in your browser.

<table>
<tr>
<td align="center"><img src="docs/screenshots/Mobile-StartSession.jpg" width="200" alt="The session list on a phone: online status and ping, a summary of running, working and stopped sessions, CPU, memory, disk and Claude plan cards, a filter box, sort and view controls, and session rows showing Claude's task, working or idle state, CPU, memory and disk with bars, and when each was opened and created"></td>
<td align="center"><img src="docs/screenshots/Mobile-CreateSession.jpg" width="200" alt="The new session panel: a working directory with a browse button, three earlier Claude conversations from that directory to resume, each with its first prompt, age and a download button, and the session name"></td>
<td align="center"><img src="docs/screenshots/Mobile-Session.jpg" width="200" alt="A live Claude Code session on a phone: the top bar with the back button, ping, four-bar status meter and toolbar icons, Claude's reply with a diff, the prompt box holding a draft, and the Esc / Tab / / / Ctrl / arrow key row"></td>
<td align="center"><img src="docs/screenshots/Mobile-Status.jpg" width="200" alt="The status pop-up opened from the four-bar meter: a key naming the cpu, mem, disk and plan bars and their colours, CPU and memory graphs, disk use, the Claude plan's 5-hour, weekly and per-model meters with their reset times and pace ticks, usage credits, and this session's memory, CPU, project size and context fill"></td>
</tr>
<tr>
<td align="center"><sub>Every session, running or stopped…</sub></td>
<td align="center"><sub>…start one, or resume an earlier conversation…</sub></td>
<td align="center"><sub>…then drive it by keyboard, prompt box or voice.</sub></td>
<td align="center"><sub>Tap the meter for the machine's status.</sub></td>
</tr>
</table>

<p align="center">
  <img src="docs/screenshots/Desktop-Tiles.png" width="720" alt="Four sessions on one screen: the main session with a git log and a split pane, a live tile showing a test run, and two watching tiles showing a web server's log and a system monitor">
</p>

## Features

- **Every session in one list.** Running and stopped, with what Claude is
  working on, whether it's busy, and each session's CPU, memory and disk.
  A stopped session resumes its conversation with a tap, and idle ones
  can stop themselves to free memory.
  [Sessions →](docs/guide/sessions.md)
- **A terminal that works on a phone.** A key row for Esc, Tab, Ctrl and
  the arrows, a prompt box whose drafts sync between devices, saved
  prompts, copy on select, and reconnecting by itself. Install it to the
  home screen as an app. [Using a session →](docs/guide/terminal.md)
- **Several sessions at once.** Tiles side by side on an iPad or desktop,
  and buttons for tmux windows and panes.
  [Tiles →](docs/guide/sessions.md#tiles)
- **Files.** Browse, upload and download (resumable, with folders as
  .zip); view images, PDFs and media; edit code with colour for about 150
  languages. Markdown renders live, with diagrams and maths.
  [Files →](docs/guide/files.md)
- **Conversation history.** The whole transcript, rendered, with the
  commands Claude ran and the diffs of its edits, updating live.
  [History →](docs/guide/files.md#conversation-history)
- **Voice.** Dictate into Claude's `/voice`, hear replies read aloud, and
  hear when Claude asks a question or needs permission.
  [Voice →](docs/guide/voice.md)
- **Status.** The machine's CPU, memory and disk, how much of your
  Claude plan you've used, and the connection's round trips and
  reconnects. [Status →](docs/guide/status.md)
- **Secure sign-in.** Always on: a password, two-factor codes and
  passkeys, in whatever combination you choose.
  [Signing in →](docs/guide/account.md)

## Getting started

You need a Linux machine (Windows via WSL2 should work; macOS is
experimental) with Python 3.11+, Node 20+, tmux, and the `claude` CLI
installed and logged in.

```bash
git clone https://github.com/JameZUK/ccpipe ~/.local/share/ccpipe
cd ~/.local/share/ccpipe
scripts/install.sh
```

The installer sets ccpipe up as a `systemd --user` service. It then
prints the first password, which is also in
`~/.local/state/ccpipe/initial_password.txt` (delete that file once
you've read it). Open `http://<host>:8080` and sign in with your system
username.

To use ccpipe away from home, put nginx or Caddy in front of it for
HTTPS. The microphone, notifications and passkeys need HTTPS too.

[Install](docs/install.md) has the requirements, platforms and updating;
[Deployment](docs/deployment.md) covers HTTPS and the firewall.

## Can I use it with a Claude subscription?

ccpipe is a pure terminal relay. The unmodified `claude` binary makes
every API call, so a Pro or Max subscription is used exactly as it is in
your own terminal. ccpipe never contacts `api.anthropic.com`, and never
reads your Claude login. Its plan-usage figures come from `claude`
itself. See Anthropic's
[legal and compliance notes](https://code.claude.com/docs/en/legal-and-compliance)
and [SECURITY.md](SECURITY.md#claude-subscription-terms).

## Documentation

- **User guide:** [Sessions](docs/guide/sessions.md) ·
  [Using a session](docs/guide/terminal.md) ·
  [Files](docs/guide/files.md) · [Voice](docs/guide/voice.md) ·
  [Status](docs/guide/status.md) ·
  [Signing in and settings](docs/guide/account.md)
- **Running it:** [Install](docs/install.md) ·
  [Deployment](docs/deployment.md) ·
  [Configuration](docs/configuration.md) · [macOS](docs/macos.md)
- **Security and development:** [Security policy](SECURITY.md) ·
  [Threat model](docs/threat-model.md) ·
  [Development](docs/development.md) · [Debugging](docs/debugging.md)

All of it is indexed in [docs/](docs/README.md).

## Licence

[MIT](LICENSE).
