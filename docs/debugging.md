# Debugging the PTY → WebSocket stream

When a user reports a "gap" in the terminal scrollback — content that
was there appearing to disappear, or text being missing until a page
refresh recovers it — this is the playbook. The framework was added
in May 2026 after the silent-drop bug in `forward_pty_to_ws`; the
tools land the moment a regression hits, so the diagnostic loop
shouldn't be longer than a journal grep.

## The invariant

Every PTY byte that leaves tmux must end up in **exactly one** of two
places in the WS counters:

```
bytes_read_pty == bytes_sent_ws + bytes_lost
```

`bytes_read_pty` is what `pump()` pulled out of the PTY.
`bytes_sent_ws` is what made it across `websocket.send_bytes()`.
`bytes_lost` is what `send_bytes` raised on (with a WARNING in the
journal). The invariant is a hard contract — drift means someone
introduced a code path that bypasses the accounting. The unit test
suite in `backend/tests/test_ws_byte_accounting.py` exists to catch
that the moment it happens.

The user-visible symptom of `bytes_lost > 0` is "content I saw is
gone from xterm scrollback until I refresh". The bytes are still in
tmux's pane (claude wrote them and tmux stored them), so a page
refresh / WS reconnect re-captures them via `capture-pane`. After
the May-2026 fix, a send failure also closes the WS so the auto-
reconnect path runs immediately and the user only sees a momentary
flicker rather than persistent gap.

## Step 1 — passive: watch the journal

Every WS handler logs a one-line summary at close:

```
ws closed: session=<name> duration=NNs frames=N bytes_read_pty=N bytes_sent_ws=N bytes_lost=N send_failures=N
```

Stream it while reproducing the symptom:

```bash
journalctl --user -u ccpipe.service -f \
  | grep -E "ws closed|send_bytes\(pty\) failed"
```

You're looking for **either** of these:

- `ws closed: … bytes_lost=N` where `N > 0` — at least one chunk
  failed to land at the client during this connection's lifetime.
- A WARNING line `send_bytes(pty) failed (… bytes lost from this ws…)`
  — the same event in finer detail, with the exception that caused
  it. Always appears immediately before the corresponding close line.

If you see neither while reproducing a "gap", the live forward path
isn't dropping — look at [Step 3](#step-3--regression-the-doctor) and
the [no-loss-but-still-a-gap](#when-the-counters-say-no-loss-but-the-symptom-persists)
section.

## Step 2 — live: query `/api/debug/sessions`

Sometimes you can't wait for a connection to close. The same counters
are available for every open WS via:

```bash
# Log in once, save the cookie jar:
curl -s --cookie-jar /tmp/c.txt -X POST https://<host>/api/auth/login \
     -H 'Content-Type: application/json' \
     -H 'X-Requested-By: ccpipe' \
     -d '{"username":"…","password":"…"}' >/dev/null

# Snapshot:
curl -s -b /tmp/c.txt https://<host>/api/debug/sessions | jq
```

Example response:

```json
{
  "sessions": [
    {
      "session": "ccpipe",
      "duration_s": 142.3,
      "frames_forwarded": 318,
      "bytes_read_pty": 91240,
      "bytes_sent_ws": 91240,
      "bytes_lost": 0,
      "send_failures": 0
    }
  ]
}
```

Refresh the curl while traffic flows; the counters should climb and
the invariant should hold on every snapshot. Drift in `bytes_lost`
between snapshots tells you the *exact* time window the loss
happened. Cross-reference with what was on screen at that moment.

## Step 3 — regression: the doctor

`scripts/scrollback-doctor.py` is the offline regression net. It
doesn't exercise the live WS forward path, but it does pin every
*other* layer — capture-pane, the attach redraw, pyte's replay match
against tmux ground truth. If a "gap" reproduces in real use but
the doctor passes, the issue is in the live forward path (Step 1).

Three modes worth knowing:

```bash
source backend/.venv/bin/activate

# Plain numbered-lines matrix across 32 (rows × cols × line-count) configs.
python scripts/scrollback-doctor.py --matrix

# Comprehensive byte patterns — uses scripts/scrollback-test-generator.sh
# to hammer the session with numbered lines, wrapped lines, full SGR
# coverage (basic + bright + 256-colour + 24-bit), cursor-overwrites,
# erase sequences, UTF-8 + box-drawing, a 2000-line rapid burst, a
# claude-like banner, and edge cases. Each phase has its own
# assertions.
python scripts/scrollback-doctor.py --realistic

# Models a WS reconnect mid-conversation: runs the generator twice in
# the same tmux session and asserts the second capture is a strict
# superset of the first. Catches scrollback-truncation regressions.
python scripts/scrollback-doctor.py --reconnect
```

Add a new phase to the generator (`scripts/scrollback-test-generator.sh`)
+ a corresponding `assert_phase_X` in the doctor whenever you find a
new byte pattern that produces a gap.

The byte-accounting unit suite is a separate net for the forward
path:

```bash
cd backend && pytest tests/test_ws_byte_accounting.py -v
```

Five cases that pin the `read == sent + lost` invariant + the
no-silent-drop contract. If anyone tries to swallow a send exception
again, this is what catches them.

## Reproducer: force a drop

To prove the recovery path end-to-end (and to verify the WARNING
log fires + the client reconnects), induce a brief WS interruption
while traffic is flowing. The simplest way is a one-shot iptables
rule that drops port 8080 for 2 seconds:

```bash
sudo iptables -I INPUT 1 -p tcp --dport 8080 -j DROP
sleep 2
sudo iptables -D INPUT 1
```

During those 2 seconds, claude's PTY output that the pump tries to
forward will hit a stalled WS. The journal should show:

```
WARNING ccpipe.ws: send_bytes(pty) failed (NNN bytes lost from this ws…
INFO    ccpipe.ws: ws closed: session=… bytes_lost=NNN send_failures=1
INFO    uvicorn   : "WebSocket /ws?session=…" [accepted]      ← reconnect
```

The browser should briefly flicker (resetTerminal → capture-pane
replay) and recover the missing bytes from tmux's pane.

## Reading the counters

| State                     | What it means                                             |
| ------------------------- | --------------------------------------------------------- |
| `bytes_lost == 0` everywhere | Live forward path is clean. Any reported gap is elsewhere — see below. |
| `bytes_lost > 0` on close | The fix worked — the user briefly lost content but reconnect recovered it. Investigate *why* the WS stalled (slow client, network blip, nginx timeout). |
| `bytes_read_pty > bytes_sent_ws + bytes_lost` | Accounting drift — bug. The unit suite should have caught this; one of the counter updates was skipped. |
| `frames_forwarded == 0` but `bytes_read_pty > 0` | Every send failed from the first frame. The WS never managed to send anything — usually a client-side bug or an immediate disconnect. |
| `send_failures` high, `bytes_lost / send_failures` small | Many small chunks each lost — typically a slow client that fails one short send at a time rather than a single huge stall. |

## When the counters say "no loss" but the symptom persists

If `bytes_lost == 0` on every connection and a user still reports a
gap, the live forward path is innocent and the bug is downstream.
Order of likelihood:

1. **xterm.js write queue under load.** The client buffers writes
   internally. Under heavy bursts it processes them on microtasks; if
   the page is briefly hidden / throttled, parsing may pause. xterm's
   internal write queue doesn't drop, but if the user is also scrolled
   up, the displayed region may not reflect the latest writes until
   `scrollToBottom` fires. See [`docs/threat-model.md`](threat-model.md)
   for the four xterm scrollback quirks documented in May-2026.

2. **`capture-pane` race during reconnect.** When the WS reconnects,
   `_capture_session_history()` runs at time `T0`, the new PTY pump
   starts at `T1 > T0`. If new content arrives between `T0` and `T1`,
   it lands via the live stream (not the captured history). On
   shorthand reconnects this is invisible. On a reconnect that
   coincides with a heavy claude write, content can briefly appear
   out of order.

3. **Lifecycle-driven reconnects on focus.** The client kicks the WS
   on focus events if no data was received recently. The threshold is
   in `frontend/src/ws.ts::kickStaleSocket` — raise it if you see
   frequent reconnects (`journalctl … | grep -c "WebSocket.*accepted"`)
   during normal tab-switching.

4. **Compositor / OS pausing the page.** Mobile Safari / Chrome
   suspend WebSocket processing on background tabs. Bytes queue at
   the OS level. When the tab comes forward, they're delivered in
   order — no loss, but a perceptible "catch-up flicker". Not a code
   bug; document for users if it's a recurring complaint.

## Where to look in code

| Symptom                                      | Start reading at                                         |
| -------------------------------------------- | -------------------------------------------------------- |
| Loss visible in `bytes_lost`                 | `backend/ccpipe/ws.py::forward_pty_to_ws`                |
| Accounting drift / unit-test failure         | `backend/ccpipe/ws.py::WsCounters` + `tests/test_ws_byte_accounting.py` |
| capture-pane returns wrong content           | `backend/ccpipe/ws.py::_capture_session_history`         |
| Wrong content after reconnect (`--reconnect` fails) | `backend/ccpipe/ws.py::handle_terminal_ws` (hello → history send → pump start ordering) |
| xterm shows stale buffer after reconnect     | `frontend/src/main.ts::onHello` (`resetTerminal` + `bottomOnNextOutput`) |
| Doctor `--realistic` fails on a specific phase | `scripts/scrollback-doctor.py::assert_phase_*` + the matching block in `scripts/scrollback-test-generator.sh` |
| Frequent reconnects on focus                 | `frontend/src/ws.ts::kickStaleSocket` (idle-threshold heuristic) |

## Adding new test coverage

When a new failure mode is identified, the protocol is:

1. **Reproduce it** with `--realistic` or a new doctor phase.
2. **Add a phase** to `scripts/scrollback-test-generator.sh` that
   emits the byte pattern.
3. **Add an `assert_phase_X`** to the doctor with the structural
   property that should hold (content present, attributes preserved,
   order maintained, etc.).
4. **Re-run** — the doctor should fail at the new assertion. *Then*
   fix the code, and verify the doctor goes green again.

For pump / forward bugs that aren't byte-pattern-shaped (e.g., a new
silent-drop branch), add a case to
`backend/tests/test_ws_byte_accounting.py` that drives the `WsCounters`
contract through the failure path you found.
