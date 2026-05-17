#!/usr/bin/env python3
"""scrollback-doctor: deterministic end-to-end test for ccpipe's scrollback path.

Reproduces the exact byte stream a freshly-attached client would receive
and runs it through a headless ANSI terminal emulator (pyte) that matches
xterm.js's screen + history semantics. Then asserts structural properties
against ground truth from tmux's own ``capture-pane``.

Usage:
    .venv/bin/python scripts/scrollback-doctor.py
    .venv/bin/python scripts/scrollback-doctor.py --lines 500 --rows 30
    .venv/bin/python scripts/scrollback-doctor.py --verbose

What's tested
-------------
1. ``_capture_session_history()`` from ccpipe.ws — does it return the
   right bytes for a known scrollback?
2. The attach-redraw stream from ``tmux attach`` — what bytes does tmux
   emit when a new client attaches?
3. The composition: history + redraw fed into pyte, does the resulting
   (screen, history) match what we'd expect from tmux's pane state?

What's NOT tested
-----------------
- The WebSocket transport layer (we exercise the byte-producing code
  paths directly to keep this isolatable). If a WS-specific bug
  appears, a separate harness can wrap this.

Dependencies
------------
- pyte (``pip install pyte``) — pure-Python ANSI emulator
- A running tmux on PATH
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

# Make ccpipe importable when run from repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

import pyte  # noqa: E402
import pyte.screens  # noqa: E402

# pyte's HistoryScreen doesn't accept `private=True` on a few CSI
# dispatch methods (DSR private queries — \x1b[?6n etc.); tmux sends
# these on startup. Drop the kwarg so pyte's no-op handlers don't
# blow up. Doesn't affect screen state.
_orig_report_device_status = pyte.screens.Screen.report_device_status
def _patched_report_device_status(self, *args, **kwargs):
    kwargs.pop("private", None)
    return _orig_report_device_status(self, *args, **kwargs)
pyte.screens.Screen.report_device_status = _patched_report_device_status

from ccpipe import tmux  # noqa: E402
from ccpipe.ws import _capture_session_history  # noqa: E402


# ─── Test fixture: tmux session with deterministic content ──────────────

def tmux_kill(session: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", session],
                   stderr=subprocess.DEVNULL, check=False)


def tmux_new(session: str, cols: int, rows: int) -> None:
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session,
         "-x", str(cols), "-y", str(rows)],
        check=True)


def tmux_write_lines(session: str, lines: int) -> None:
    """Populate the test session with `lines` numbered lines via printf.

    A single shell command produces all lines, so timing isn't a factor.
    Lines look like ``line 0001``, ``line 0002``, …
    """
    cmd = (
        f"for i in $(seq 1 {lines}); do "
        f"printf 'line %04d\\n' $i; done"
    )
    subprocess.run(
        ["tmux", "send-keys", "-t", session, cmd, "Enter"], check=True)
    # Wait for the prompt to settle. printf is fast; 200ms is generous.
    time.sleep(0.25)


def tmux_capture_full(session: str, max_lines: int = 10000) -> list[str]:
    """Ground truth: tmux's own view of the WHOLE pane (scrollback + visible).

    This matches what ccpipe's new ``_capture_session_history`` produces —
    a single contiguous view from N lines into history down through the
    current visible pane."""
    r = subprocess.run(
        ["tmux", "capture-pane", "-t", session, "-p",
         "-S", f"-{max_lines}"],
        capture_output=True, text=True, check=True)
    return r.stdout.splitlines()


def tmux_capture_visible(session: str) -> list[str]:
    """Ground truth: tmux's view of the currently-visible pane only."""
    r = subprocess.run(
        ["tmux", "capture-pane", "-t", session, "-p"],
        capture_output=True, text=True, check=True)
    return r.stdout.splitlines()


# ─── Capture what `tmux attach` emits when a client connects ────────────

def capture_attach_redraw(session: str, cols: int, rows: int,
                           settle_s: float = 0.6) -> bytes:
    """Spawn a tmux attach inside a fresh PTY, read everything it emits
    until quiescent, then detach. Returns the raw byte stream the
    real ccpipe client would see (without ccpipe-added prefixes)."""
    pid, master_fd = pty.fork()
    if pid == 0:
        # Child: become the tmux attach client.
        os.environ["TERM"] = "xterm-256color"
        argv = tmux.attach_argv(session)
        try:
            os.execvp(argv[0], argv)
        except Exception:
            os._exit(127)

    # Parent: set window size and read.
    import struct
    import termios
    ws = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, ws)
    fcntl.fcntl(master_fd, fcntl.F_SETFL,
                 fcntl.fcntl(master_fd, fcntl.F_GETFL) | os.O_NONBLOCK)

    buf = bytearray()
    deadline = time.monotonic() + settle_s
    while time.monotonic() < deadline:
        r, _, _ = select.select([master_fd], [], [], 0.05)
        if master_fd in r:
            try:
                chunk = os.read(master_fd, 65536)
                if chunk:
                    buf.extend(chunk)
                    # Extend the deadline a bit on data — tmux can send
                    # the redraw in bursts.
                    deadline = time.monotonic() + 0.15
            except OSError:
                break

    # Send Ctrl-B d (detach). Then close.
    try:
        os.write(master_fd, b"\x02d")
        time.sleep(0.1)
    except OSError:
        pass
    try:
        os.close(master_fd)
    except OSError:
        pass
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    return bytes(buf)


# ─── pyte simulation helpers ────────────────────────────────────────────

def replay_into_pyte(bytestreams: list[bytes], cols: int, rows: int,
                      history_size: int = 10000) -> pyte.HistoryScreen:
    """Feed byte streams sequentially into a fresh pyte HistoryScreen
    configured to match xterm.js semantics on the client.

    Notes:
    - HistoryScreen.history.top is the "above visible" scrollback (deque
      of Line objects). When the user scrolls up in the browser, they
      see top first, then visible.
    - We DO NOT install an alt-screen handler here — pyte's HistoryScreen
      does push lines into history on LF when in normal mode, which is
      exactly what xterm.js does after our suppress-CSI handlers drop
      \\x1b[?1049h on the client. The frontend behaviour is reproduced
      by simply… not switching pyte to alt screen (which it doesn't by
      default).
    """
    screen = pyte.HistoryScreen(cols, rows, history=history_size, ratio=0.5)
    stream = pyte.Stream(screen)
    for chunk in bytestreams:
        # pyte's Stream.feed wants str on Py3 (it iterates codepoints).
        stream.feed(chunk.decode("utf-8", errors="replace"))
    return screen


def pyte_history_text(screen: pyte.HistoryScreen) -> list[str]:
    """Return the scrollback above the visible region, oldest first."""
    out: list[str] = []
    for line in screen.history.top:
        # Line is a dict-like: keys are column indices, values are Char.
        # Reconstruct the row as a string, stripping trailing spaces.
        # An empty line yields "".
        if not line:
            out.append("")
            continue
        max_col = max(line.keys()) if line else -1
        row = "".join(line[c].data if c in line else " "
                       for c in range(max_col + 1)).rstrip()
        out.append(row)
    return out


def pyte_visible_text(screen: pyte.HistoryScreen) -> list[str]:
    """Return the visible region as plain text, top to bottom."""
    return [row.rstrip() for row in screen.display]


# ─── The actual diagnostic ──────────────────────────────────────────────

def numbered_lines_in(seq: list[str]) -> dict[int, list[int]]:
    """Map line-number → indices where it appears in *seq*. Used to
    detect duplicates + gaps in the numbered-lines reconstruction."""
    out: dict[int, list[int]] = {}
    for idx, line in enumerate(seq):
        stripped = line.strip()
        if stripped.startswith("line "):
            try:
                num = int(stripped.split()[1])
            except (ValueError, IndexError):
                continue
            out.setdefault(num, []).append(idx)
    return out


async def run_one(rows: int, cols: int, lines: int,
                    session: str = "scrollback-doctor",
                    verbose: bool = False) -> tuple[int, list[str]]:
    """Run one configuration. Returns (rc, list_of_failure_messages).
    rc=0 means all assertions passed; rc=1 means one or more failed."""

    failures: list[str] = []
    rc = 0

    # ── Set up ──
    tmux_kill(session)
    tmux_new(session, cols, rows)
    tmux_write_lines(session, lines)

    # ── Ground truth ──
    gt_full = tmux_capture_full(session)
    gt_visible = tmux_capture_visible(session)
    if verbose:
        print(f"  ground truth: full={len(gt_full)} visible={len(gt_visible)}")
        print(f"    full last 2: {gt_full[-2:]!r}")
        print(f"    vis  last 2: {gt_visible[-2:]!r}")

    # ── ccpipe capture ──
    history_bytes = await _capture_session_history(session, rows)
    decoded = history_bytes.decode("utf-8", errors="replace")
    ccpipe_lines = decoded.replace("\r\n", "\n").split("\n")
    if ccpipe_lines and ccpipe_lines[-1] == "":
        ccpipe_lines = ccpipe_lines[:-1]
    if verbose:
        print(f"  ccpipe capture: {len(history_bytes)}B → {len(ccpipe_lines)} lines")
        print(f"    last 2: {ccpipe_lines[-2:]!r}")

    # ── Sanity: ccpipe's capture matches tmux's full ground truth ──
    if ccpipe_lines != gt_full:
        rc = 1
        diff_idx = next((i for i, (a, b) in enumerate(zip(ccpipe_lines, gt_full)) if a != b), None)
        if diff_idx is not None:
            failures.append(f"ccpipe capture != tmux ground truth at row {diff_idx} "
                             f"(ccpipe={ccpipe_lines[diff_idx]!r} tmux={gt_full[diff_idx]!r})")
        else:
            failures.append(f"ccpipe capture length differs: ccpipe={len(ccpipe_lines)} "
                             f"tmux={len(gt_full)}")

    # ── tmux attach redraw ──
    attach_bytes = capture_attach_redraw(session, cols, rows)
    if verbose:
        print(f"  tmux attach: {len(attach_bytes)}B of redraw")

    # ── Replay through pyte (matches xterm.js's scrollback semantics) ──
    screen = replay_into_pyte([history_bytes, attach_bytes], cols, rows)
    pyte_hist = pyte_history_text(screen)
    pyte_vis = pyte_visible_text(screen)
    if verbose:
        print(f"  pyte: history={len(pyte_hist)} visible={len(pyte_vis)}")
        print(f"    hist last 2:    {pyte_hist[-2:]!r}")
        print(f"    visible last 2: {pyte_vis[-2:]!r}")

    combined = pyte_hist + pyte_vis

    # ── Assertion 1: every numbered line present ──
    found = numbered_lines_in(combined)
    expected = set(range(1, lines + 1))
    missing = sorted(expected - set(found))
    if missing:
        rc = 1
        sample = missing[:8] + (["…"] if len(missing) > 8 else [])
        failures.append(f"{len(missing)} numbered lines missing: {sample}")

    # ── Assertion 2: no duplicate numbered lines ──
    dups = {n: idxs for n, idxs in found.items() if len(idxs) > 1}
    if dups:
        rc = 1
        sample = sorted(dups)[:5]
        failures.append(f"{len(dups)} duplicate(s): " +
                         ", ".join(f"line {n}@{dups[n]}" for n in sample))

    # ── Assertion 3: chronological order ──
    order = [int(l.strip().split()[1]) for l in combined
              if l.strip().startswith("line ")]
    if order != sorted(order):
        rc = 1
        first_ooo = next(i for i, (a, b) in enumerate(zip(order, sorted(order)))
                          if a != b)
        failures.append(f"out of order at idx {first_ooo}: "
                         f"...{order[max(0, first_ooo-2):first_ooo+3]}")

    # ── Cleanup ──
    tmux_kill(session)
    return rc, failures


async def run(args) -> int:
    """Dispatch: single-config (default) or matrix mode."""

    if args.matrix:
        # Multi-config sweep over rows × lines to catch resolution-
        # dependent regressions. The original bug only appeared at
        # certain row/line combinations; a matrix run gives us a
        # deterministic regression net.
        configs = [
            (rows, cols, lines)
            for rows in (24, 30, 50, 80)
            for cols in (80, 120)
            for lines in (10, 100, 200, 1000)
        ]
        print(f"╭─ scrollback-doctor — matrix run ({len(configs)} configs)")
        passed = 0
        all_failures: list[str] = []
        for (rows, cols, lines) in configs:
            label = f"rows={rows:3d} cols={cols:3d} lines={lines:5d}"
            rc, failures = await run_one(rows, cols, lines,
                                          session=args.session,
                                          verbose=args.verbose)
            if rc == 0:
                print(f"│ ✓ {label}")
                passed += 1
            else:
                print(f"│ ✗ {label}")
                for f in failures:
                    print(f"│     {f}")
                    all_failures.append(f"[{label}] {f}")
        print(f"╰─ {passed}/{len(configs)} passed")
        return 0 if passed == len(configs) else 1

    # ── Single-config mode ──
    print(f"╭─ scrollback-doctor ────────────────────────────────────")
    print(f"│ session={args.session!r}  rows={args.rows}  cols={args.cols}")
    print(f"│ lines={args.lines}  (printf 'line %04d')")
    print(f"╰────────────────────────────────────────────────────────")

    rc, failures = await run_one(args.rows, args.cols, args.lines,
                                   session=args.session,
                                   verbose=True)
    print()
    if rc == 0:
        print(f"✓ all assertions passed")
    else:
        print(f"✗ {len(failures)} assertion(s) failed:")
        for f in failures:
            print(f"    {f}")
    return rc


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--session", default="scrollback-doctor",
                   help="tmux session name to use (will be killed if exists)")
    p.add_argument("--lines", type=int, default=200,
                   help="how many numbered lines to populate")
    p.add_argument("--rows", type=int, default=24)
    p.add_argument("--cols", type=int, default=80)
    p.add_argument("--matrix", action="store_true",
                   help="run a matrix of resolutions/lines to catch "
                        "resolution-dependent regressions")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
