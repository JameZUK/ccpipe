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
  a firewall rule). The README's "Reverse proxy" section is the
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

## Known limitations

These are accepted trade-offs, documented here so you don't need to
report them as findings:

- **0.0.0.0 bind by default.** Required so an off-host reverse proxy
  can reach the backend. The README emphasises firewalling :8080 to
  the proxy host; a startup banner reminds the operator when
  `CCPIPE_BEHIND_TLS=1` is set.
- **No persistent login banning.** The login throttle is in-memory
  sliding-window only. fail2ban reading
  `journalctl --user -u ccpipe` is the recommended add-on if you
  need persistent IP banning.
- **Operator privileges.** The file-panel jail blocks ccpipe's own
  state dir but does not block `.ssh`, `.aws`, `.gnupg`, `.kube`,
  etc. — by design, because ccpipe is an admin tool for the operator's
  own machine. An attacker with a valid session cookie has the same
  filesystem reach the operator does (within the jail). Defence is
  the auth gate + TOTP, not the file ACL.
- **TOTP burn-list is in-memory.** Survives the verify window but not
  a process restart. With uvicorn `--reload` (dev) the same code can
  be replayed; production restarts are rare enough that the trade-off
  is acceptable.
