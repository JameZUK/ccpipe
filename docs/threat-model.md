# ccpipe — design notes for the "remote shell for Claude" threat class

Distilled from a three-pass external pen-test (May 2026). Generic
web-app concerns are covered by the in-process and external test
suites; this file captures only the design-level points that aren't
testable with a fuzzer and need to be re-read whenever someone touches
the WebSocket protocol, terminal rendering, share-target handling, or
anything Claude reaches via tool use.

## The right mental model

The operator's text input flows: **composer → WebSocket → tmux →
Claude Code CLI → shell tools**. Claude's tool authority is bounded
only by:

1. The Claude Code permissions model (`/permissions`, settings)
2. The OS user that claude runs as
3. Any sandboxing applied to that user (containers, AppArmor, etc.)

There is no "ccpipe layer" between Claude's chosen commands and the
host. ccpipe is the *delivery mechanism* for prompts; it doesn't
filter, review, or gate what Claude does with them. **The security
boundary is Claude + the host config — not ccpipe.**

That makes the failure modes worth thinking about look like:

- attacker influences the operator into sending malicious text (PWA
  share-target, social engineering)
- attacker plants content that Claude reads mid-session and acts on
  (indirect prompt injection via README/log/PR-body/MOTD/etc.)
- terminal output is crafted to mislead the operator about what just
  happened (OSC-8 link spoofing, title rewrite, screen-clearing
  escapes)
- the WS / file-API channels themselves get abused

The first and last categories are pen-tested and have regression
coverage in `backend/tests/test_external_security.py`. The middle two
are mostly Claude's problem and the operator's problem.

## The one ongoing recommendation: narrow Claude's permissions

The single most useful thing an operator can do is run `claude` with
the **narrowest possible tool permission set** that still gets the
work done. Every `--dangerously-skip-permissions` or broad
`Read`/`Write` allow widens the indirect-prompt-injection blast
radius. The default permission model is the load-bearing defence here
— ccpipe can't substitute for it.

ccpipe operators tend to be auditing other people's code, tailing
logs, summarising PRs, running `gh pr view` against arbitrary repos —
all of which feed attacker-influenced bytes to Claude. Treat any
"summarise this for me" workflow as untrusted input.

If `/api/fs/config.root` (`CCPIPE_FS_ROOT`) is configurable, set it to
the narrowest directory you actually need — not `/` or `~`. Anything
Claude can read could carry an injection.

Within the jail, a denylist refuses both reads and writes to ccpipe's
own state (`.local/state/ccpipe`, `.config/ccpipe`) and to Claude Code's
state — `.claude` (transcripts, settings, and the live OAuth token in
`.claude/.credentials.json`) and the sibling `.claude.json` (global
config + `oauthAccount` identity). The denylist is enforced on the
resolved final target, not just its parent, so a denied leaf can't be
created or written even when its parent dir is allowed. All `/api/fs/*`
operations — including `rename` and `delete` — walk every intermediate
path component with `O_NOFOLLOW` and act via `dir_fd`, so a same-UID
concurrent writer (e.g. `claude` under prompt injection) can't swap an
intermediate directory for an out-of-jail symlink between path
resolution and the syscall.

## Pre-auth request-body DoS

FastAPI reads and parses a route's JSON body *before* its auth/CSRF
dependencies run, so an unauthenticated request could otherwise make the
server ingest up to the proxy's `client_max_body_size` (64 MB) and
`json.loads` it before the 401 — a memory/CPU amplification primitive,
plus a slow-body connection-slot hold. `main.py`'s `_BodyCapMiddleware`
caps every body-taking route (64 KiB default; explicit higher caps for
`/api/fs/write`, `/api/debug/snapshot`, and 16 MiB for one piece of a
resumable upload, `PUT /api/fs/uploads/{id}`; `/api/fs/upload` is exempt and
self-caps while streaming). It counts bytes **as they stream**, so a
chunked or Content-Length-spoofed body can't bypass it. The bundled
nginx sample adds `client_header_timeout`/`client_body_timeout` (and a
commented per-IP `limit_req`) to close the slow-client slot-hold for
nginx-only deployments; an edge proxy that buffers request bodies (e.g.
Cloudflare) already absorbs the slow path. Login additionally costs a
rate-limit bucket slot regardless of body (see `auth.py`).

`POST /api/auth/passkey/options` is the other pre-auth endpoint that
creates state: one 64-byte challenge per call, kept server-side for
5 minutes, at most 8 outstanding per client address and 512 in all (the
oldest is evicted). It
reveals nothing (no credential list), and a challenge only counts once
an assertion signed by a registered passkey comes back for it.

## WebSocket fuzz coverage

T7 of the original pen-test ("WebSocket message-type abuse") is
covered by the credentialed fuzz suite in
`backend/tests/test_external_security.py` (search for `T7`). Tests pin
resize bounds, large-input handling, unknown-type silent-drop, binary
frame prefix handling, and frame-ordering relative to `hello`.

When you add a new client→server message type to `ws.py`, add a
matching fuzz case there too. The pattern is:

```bash
CCPIPE_TEST_PASSWORD=… CCPIPE_EXTERNAL_BASE=http://localhost:8080 \
    pytest backend/tests/test_external_security.py -k T7
```

## tmux controls and tiles

The tmux toolbar (`POST /api/sessions/{name}/tmux`) takes an action
**name** from a fixed table in `backend/ccpipe/tmux_ui.py`, never a tmux
command line. The only free-form value is a window name, which is passed as
a single argv element after `--` (control characters refused), so it can't
reach tmux's command parser. Closing the last pane or the last window is
refused, so the toolbar can't kill a session; that stays with the session
picker. The endpoint has the usual auth + CSRF dependencies.

Tiles add two WebSocket query flags, and both only take capabilities away.
`tile=1` means no TTS subscription, no mic frames, and never auto-creating
the session. `mode=watch` attaches tmux with `-f read-only,ignore-size` and
the server drops the tile's input frames as well. Tile layouts
(`/api/layouts/{name}`) are validated: session names go through
`safe_name`, and there are at most 9 tiles, 4 levels deep.

## Push notifications

Push is off until a signed-in device turns it on (Settings →
Notifications). Then the backend makes outbound HTTPS requests, which
it otherwise doesn't (Kokoro aside):

- **Where to.** A subscription's endpoint comes from the page, so it is
  checked against the browsers' push services (`webpush.endpoint_allowed`:
  `https` on port 443, no userinfo, a Mozilla, Apple, Google or Microsoft
  push host). That keeps an authenticated caller from pointing the server
  at an internal address. Redirects aren't followed.
- **What the push service learns.** The body is encrypted for the device
  (RFC 8291 `aes128gcm`; checked byte for byte against the RFC's worked
  example in `tests/test_push.py`). The service sees the timing, the
  size, the urgency and the `Topic` (a hash of the session name), not the
  question. A device can also leave the text out, for its lock screen.
- **Who can push.** Each request carries a VAPID JWT (RFC 8292) signed
  with the server's P-256 key, which lives only in `push.json` (`0600`).
  The subscriptions are bound to its public key, so without the private
  key nobody else can push to them.
- **The routes** (`/api/push/*`, `/api/attention*`) carry the usual auth
  plus CSRF (writes) or same-origin (reads) dependencies. Endpoints never
  come back out of `GET /api/push`, only their host.
- **The service worker** (`push-sw.js`) has no fetch handler. It never
  sees or serves the app's requests, so it can't pin a stale build. It
  only shows notifications, opens the session tapped and renews its own
  subscription.
- **Rate limits.** At most 20 pushes per device in 10 minutes, and one
  finished turn per session per device a minute, so a runaway loop of
  tiny turns can't buzz a phone all day.

## Rendered markdown (`/history`, `/view`)

Transcripts and files are rendered as markdown with raw HTML allowed, so
the text of anything Claude read (a web page, a cloned repository's
README) ends up on a page of ccpipe's own origin. Script is out twice
over: DOMPurify, and the page's CSP (`script-src 'self'`). On top of that
`frontend/src/md-sanitize.ts` keeps the text from redrawing the page
around it:

- no `<style>`, and inline `style=` only for a short list of properties
  with capped lengths (table alignment, KaTeX's boxes), so nothing can
  cover the page, hide the tool blocks or paint a fake sign-in form;
- no form controls, except a task list's disabled checkbox;
- no `data-*` attributes (the pages keep their own state in them);
- where a link opens is never the document's choice: a link to another
  site opens in a new tab without the opener, written any way
  (`https://…`, `//host/…`). A `target="_top"` link in the file panel's
  preview would otherwise replace ccpipe itself with a lookalike.

When you add a renderer, pass `markdownSanitizeOptions()` to its
`DOMPurify.sanitize` call.

## Review of 2026-11-12

Design points from that review, beyond the above:

- **Login limit.** The per-client budget (5 a minute) counts an IPv6
  client by its /64 (`auth.client_bucket`); counted by single address,
  anyone with an ordinary IPv6 connection had a fresh budget per address
  and only the global cap (300 a minute) applied. The global cap still
  lets a flood from many networks keep you from signing in (open
  sessions are unaffected): a rate rule at the edge proxy on
  `/api/auth/login` is the answer to that.
- **State files** (`statefile.py`). A read that fails is not an empty
  file: the path is blocked for writing until a read works, and a file
  that isn't JSON is moved aside, never overwritten. Use it for any new
  read-change-write state file.
- **The deny-list** also refuses renaming or deleting a folder that
  contains a denied place (`~/.local`), and names the state directory
  wherever `XDG_STATE_HOME` puts it.
- **Status line.** The recorder runs a project's own `statusLine`
  command only when Claude's record says you accepted the trust prompt
  for that folder (`statusline_record.trusted`).
- **Sign out.** Signing out turns that device's push off; "sign out
  everywhere" forgets every push device, so a lost one stops receiving
  Claude's questions. Each device turns push back on after signing in.

