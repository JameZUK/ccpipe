# ccpipe documentation

[← Back to the project](../README.md)

## Using ccpipe

| Guide | What's in it |
|---|---|
| [Sessions](guide/sessions.md) | The session list, stopped sessions and resuming, starting a session, tiles, tmux controls |
| [Using a session](guide/terminal.md) | The prompt box, drafts and saved prompts, copying, phones and tablets, reconnecting, updates |
| [Files, documents and history](guide/files.md) | The file browser and transfers, the file viewer and editor, rendered Markdown, the conversation history |
| [Voice](guide/voice.md) | Dictation, spoken replies, questions and permission prompts read aloud |
| [Status](guide/status.md) | The machine's CPU, memory and disk; the connection's round trips; your Claude plan usage |
| [Signing in and settings](guide/account.md) | Password, two-factor, passkeys and the ways to sign in; the Settings tabs |

## Running it

| Document | What's in it |
|---|---|
| [Install](install.md) | Requirements, platforms (Linux, WSL2, macOS), installing, first sign-in, updating |
| [Deployment](deployment.md) | HTTPS with nginx or Caddy, the systemd drop-in, firewalling, Cloudflare, troubleshooting |
| [Configuration](configuration.md) | Environment variables, sign-in, voice setup, the session lifecycle, status, where state lives |
| [macOS](macos.md) | The experimental macOS port and its local transcription |

## Security and development

| Document | What's in it |
|---|---|
| [Security policy](../SECURITY.md) | Reporting a vulnerability, the Claude subscription terms, the hardening summary, known limitations |
| [Threat model](threat-model.md) | The design-level threat model |
| [Development](development.md) | Architecture, the dev workflow, code layout, the WebSocket protocol, tests |
| [Debugging](debugging.md) | Diagnosing the PTY → WebSocket byte stream ("a gap until I refresh") |
