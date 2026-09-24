# Security policy

## Reporting a vulnerability

ccpipe is a personal-use tool, but it carries credentials and gates
access to a `claude` session. If you find a vulnerability that affects
authentication, session handling, the file-panel jail, the WebSocket
upgrade path, or any other security-sensitive surface, please report it
privately rather than opening a public issue.

The preferred channel is **GitHub Security Advisories** for this
repository — Security tab → "Report a vulnerability". That keeps the
report private to the repo maintainers until a fix is ready.

If you don't have a GitHub account, open an issue containing **no
exploit detail** and ask for a private contact channel; the maintainer
will follow up.

Please include:

- Affected file(s) / endpoint(s) / commit SHA.
- A minimal reproduction (curl, screenshot, or a one-paragraph
  description — whatever lets us confirm the issue without ambiguity).
- The impact you believe it has (e.g. "logged-in user can read
  arbitrary files outside the FS jail", "WebSocket survives logout").
- Whether you've already disclosed elsewhere.

You should expect an acknowledgement within a few days. A fix may take
longer depending on complexity; we'll keep you in the loop on the
advisory.

## Scope

In scope:

- Anything under `backend/ccpipe/` and `frontend/src/`.
- The systemd unit templates and the installer (`scripts/install.sh`).
- The bundled `nginx/ccpipe.conf` sample, where the issue is a flaw in
  the sample itself rather than misuse by an operator.

Out of scope:

- Misconfiguration on the operator's side (e.g. running with
  `CCPIPE_BEHIND_TLS=0` over the public internet, putting non-proxy
  IPs in `--forwarded-allow-ips`, exposing `:8080` to the LAN without
  a firewall rule). [`docs/deployment.md`](docs/deployment.md) is the
  authoritative guide; deviations are on the operator.
- The unmodified `claude` CLI itself (report those upstream to
  Anthropic).
- Kokoro-FastAPI, PulseAudio/PipeWire, tmux, nginx — report those
  upstream to their respective maintainers.
- Threat models that assume an attacker with shell access on the
  ccpipe host. If they're already on the box, ccpipe is the wrong
  thing to harden against.

## Supported versions

Only the latest commit on `main` receives security fixes. There are
no version branches.

## Threat model & regression tests

ccpipe has been externally pen-tested in three passes (May 2026). The
deliverables that landed in this repository as a result:

- **[`docs/threat-model.md`](docs/threat-model.md)** — design-level
  threat model for the prompt-injection / remote-shell-for-Claude
  class of risks that can't be tested with curl. Worth re-reading
  whenever you change the WebSocket protocol, terminal rendering,
  share-target handling, or anything Claude touches via tool use.
- **[`backend/tests/test_external_security.py`](backend/tests/test_external_security.py)** —
  live-HTTP regression suite that pins every defensive property
  observed across the three audit passes. Talks to a real running
  instance over HTTP (not the in-process TestClient), so it also
  catches deployment-layer regressions (nginx config, systemd
  drop-ins, reverse-proxy header injection) that
  `test_review_fixes.py` can't see.

  Default `pytest` skips it. To run against a local instance:

  ```bash
  CCPIPE_EXTERNAL_BASE=http://localhost:8080 \
      CCPIPE_EXTERNAL_HOST=ccpipe.example.com \
      pytest -v backend/tests/test_external_security.py
  ```

  Rate-limit tests are further gated by
  `CCPIPE_ALLOW_DESTRUCTIVE_TESTS=1` because they sleep 65 s after
  tripping the limiter — don't enable that flag against production
  unless you're OK locking your own IP out for a minute.

## Hardening summary

What the current code does, so a report can say which property it
breaks. Details live in the linked code and docs.

- **Login.** One `POST /api/auth/login` carrying username, password
  and (when TOTP is enrolled) the code together; any failure is the
  same `401`, so there is no password-correct-but-code-missing signal.
  The two screens in the UI (password, then code) are client-side
  only. Login and the four password re-verify endpoints share a
  per-IP 5/min bucket plus a global 300/min cap.
- **Sessions.** A signed Starlette cookie (`__Host-` + `Secure` under
  `CCPIPE_BEHIND_TLS=1`) with a **30-day idle timeout**: it is
  re-issued at most hourly while in use, and lapses after 30 days
  without use. Each login carries a random session id; **logout
  revokes that id server-side** (persisted in `revoked_sessions.json`
  beside the credentials file, so it survives a restart and a copied
  cookie stops working) and closes that login's open terminal
  sockets. **Settings → Account → "sign out everywhere"** (two taps;
  `POST /api/auth/logout-all`) invalidates every session on every
  device without changing the password; changing the password or
  enrolling/disabling TOTP does the same. Open WebSockets are
  re-checked and closed when their session is no longer valid.
- **tmux.** ccpipe addresses sessions and panes with exact-match
  targets (`=name`), so a name can't prefix-match a different session,
  and session names are restricted to `[A-Za-z0-9_-]`. Processes ccpipe
  spawns through tmux get its environment **minus every `CCPIPE_*`
  variable**, `CCPIPE_*` is unset from the tmux server's global
  environment at startup, and `CCPIPE_AUTH_PASSWORD` is removed from
  ccpipe's own environment once it has been hashed to disk — so a
  bootstrap password set in a drop-in doesn't leak into every pane.
- **Terminal clipboard (OSC 52).** tmux is pinned to
  `set-clipboard external` and `allow-passthrough off`, so programs in
  a pane can't pass clipboard writes through to the browser. As a
  second layer, the browser only honours an OSC 52 write within 2 s of
  the operator's own key or pointer input on the page, caps its size,
  and toasts a preview of what was copied; an unsolicited write is
  refused with a visible "ignored" toast. Clipboard *reads* are never
  answered.
- **File saves.** Saves and uploads over an existing file go through a
  unique `O_EXCL | O_NOFOLLOW` temp file and keep the original file's
  permission bits (a `0600` file stays `0600`, scripts keep `+x`). A
  save to a symlink writes **through** it, keeping the link, but only
  after the resolved target passes the same jail, deny-list and
  no-symlinked-parent checks as any other path — a link pointing out
  of the jail or into a denied directory is still refused (`403`).
- **Dependencies.** `backend/constraints.txt` pins the Python
  dependency set and `scripts/install.sh` installs with
  `pip install -c constraints.txt`; the frontend installs with
  `npm ci` from the committed `package-lock.json`. Pins reduce drift
  and surprise upgrades; they don't make the pinned versions
  vulnerability-free.
- **Cross-site GETs.** Every authenticated `GET` carries one shared
  Fetch-Metadata gate (`auth.require_same_origin`): a request whose
  `Sec-Fetch-Site` is anything other than `same-origin` (another site,
  a typed URL) is refused, so a link or `<img>`/`<audio>` tag elsewhere
  can't ride the session cookie to download files or meter Kokoro. An
  absent header is allowed (non-browser clients have no ambient cookie).
  State-changing methods are covered by the `X-Requested-By` CSRF check.
- **Resource caps.** At most 32 user tmux sessions (each a live
  `claude`) can be created, so a runaway client can't exhaust memory;
  terminal-relay output is backpressured rather than buffered without
  bound; malformed WebSocket frames are dropped (and their log warnings
  rate-limited) instead of tearing down the connection.

## Known limitations

These are accepted trade-offs, documented here so you don't need to
report them as findings:

- **0.0.0.0 bind by default.** Required so an off-host reverse proxy
  can reach the backend. The bind address is not a control — `--host
  ::` would also listen on public IPv6 addresses — so `:8080` must be
  firewalled to the proxy host (IPv4 and IPv6; ufw and nftables
  examples in [`docs/deployment.md`](docs/deployment.md)). A startup
  banner reminds the operator when `CCPIPE_BEHIND_TLS=1` is set.
- **Throttle keys on the client IP it is given.** Behind a CDN such as
  Cloudflare the per-IP login bucket sees edge IPs unless nginx
  restores the visitor IP (`set_real_ip_from` for the CDN's ranges
  only — see the optional block in `nginx/ccpipe.conf`).
- **No persistent login banning.** The login throttle is in-memory
  sliding-window only. fail2ban reading
  `journalctl --user -u ccpipe` is the recommended add-on if you
  need persistent IP banning.
- **Operator privileges.** Within the file-panel jail, the deny-list
  covers only ccpipe's own state (`~/.local/state/ccpipe`,
  `~/.config/ccpipe`) and Claude Code's (`~/.claude`,
  `~/.claude.json`). It does **not** block `.ssh`, `.aws`, `.gnupg`,
  `.kube`, etc. — by design, because ccpipe is an admin tool for the
  operator's own machine. An attacker with a valid session cookie has the same
  filesystem reach the operator does (within the jail). Defence is
  the auth gate + TOTP, not the file ACL.
- **TOTP burn-list is in-memory.** Survives the verify window but not
  a process restart. With uvicorn `--reload` (dev) the same code can
  be replayed; production restarts are rare enough that the trade-off
  is acceptable.
