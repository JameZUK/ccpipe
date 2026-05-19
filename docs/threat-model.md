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
