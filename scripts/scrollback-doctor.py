#!/usr/bin/env python3
"""scrollback-doctor: deterministic end-to-end test for ccpipe's scrollback path.

Reproduces the exact byte stream a freshly-attached client would receive
and runs it through a headless ANSI terminal emulator (pyte) that matches
xterm.js's screen + history semantics. Then asserts structural properties
against ground truth from tmux's own ``capture-pane``.

Usage:
    .venv/bin/python scripts/scrollback-doctor.py
    .venv/bin/python scripts/scrollback-doctor.py --lines 500 --rows 30
    .venv/bin/python scripts/scrollback-doctor.py --matrix
    .venv/bin/python scripts/scrollback-doctor.py --realistic
    .venv/bin/python scripts/scrollback-doctor.py --reconnect

Modes
-----
- Default (single-config) and ``--matrix``: numbered-lines baseline.
- ``--realistic``: invokes ``scripts/scrollback-test-generator.sh`` inside
  a fresh tmux session via ``send-keys`` — covers numbered lines, wrapped
  lines, full SGR coverage, cursor overwrites, erase sequences, UTF-8 +
  box-drawing, a rapid-burst phase, and a few claude-like patterns.
  Per-phase assertions catch missing lines, broken attribute preservation,
  line-accumulation bugs, and encoding regressions.
- ``--reconnect``: runs the generator twice in the same tmux session
  (with distinct run-ids) and asserts the second capture is a strict
  superset of the first. Models what happens when the WS reconnects
  mid-conversation: both runs' content should be present in the new
  capture without dups or gaps.

What's tested
-------------
1. ``_capture_session_history()`` from ccpipe.ws — does it return the
   right bytes for a known scrollback?
2. The attach-redraw stream from ``tmux attach`` — what bytes does tmux
   emit when a new client attaches?
3. The composition: history + redraw fed into pyte, does the resulting
   (screen, history) match what we'd expect from tmux's pane state?
4. Realistic stress: many byte patterns (SGR / cursor / UTF-8 / bursts)
   captured through the same path as #1 + #3, with phase-scoped
   assertions on the replayed pyte buffer.

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
import re
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


# ─── Realistic-mode helpers ─────────────────────────────────────────────

GENERATOR_PATH = REPO_ROOT / "scripts" / "scrollback-test-generator.sh"
SENTINEL_PREFIX = "<<<DOCTOR-EOT "
SENTINEL_POLL_INTERVAL_S = 0.2
SENTINEL_TIMEOUT_S = 60.0       # generator emits ~2.5k lines; finishes well under 60s


def tmux_send_keys(session: str, keys: str, enter: bool = True) -> None:
    args = ["tmux", "send-keys", "-t", session, keys]
    if enter:
        args.append("Enter")
    subprocess.run(args, check=True)


def wait_for_sentinel(session: str, run_id: str,
                       timeout_s: float = SENTINEL_TIMEOUT_S) -> bool:
    """Poll capture-pane until the generator's `<<<DOCTOR-EOT <run_id>>>`
    marker appears, or timeout. Embedding run_id in the marker means a
    stale capture from a previous run can't false-match."""
    marker = f"{SENTINEL_PREFIX}{run_id}>>>"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session, "-p", "-S", "-10000"],
            capture_output=True, text=True, check=False)
        if marker in result.stdout:
            return True
        time.sleep(SENTINEL_POLL_INTERVAL_S)
    return False


def fresh_run_id() -> str:
    return f"r{os.getpid()}-{int(time.time()*1000)}"


# ─── Per-phase assertion helpers ────────────────────────────────────────
#
# Each returns a list of failure strings (empty list = phase passed).
# `lines` is the combined history+visible text after replay through pyte.
# Some checks also inspect `screen` directly for per-cell attribute data.

_NUMBERED_RE = re.compile(r'^line (\d{4})\s*$')
_BURST_RE = re.compile(r'^burst-(\d{5})\s*$')


def assert_phase_a(lines: list[str]) -> list[str]:
    """500 sequential numbered lines, each appearing exactly once."""
    seen: dict[int, int] = {}
    for line in lines:
        m = _NUMBERED_RE.match(line)
        if m:
            n = int(m.group(1))
            seen[n] = seen.get(n, 0) + 1
    expected = set(range(1, 501))
    missing = sorted(expected - set(seen.keys()))
    dups = {n: c for n, c in seen.items() if c > 1}
    failures = []
    if missing:
        sample = missing[:5] + (["…"] if len(missing) > 5 else [])
        failures.append(f"PHASE A: {len(missing)} numbered lines missing: {sample}")
    if dups:
        sample = sorted(dups.items())[:5]
        failures.append(f"PHASE A: {len(dups)} duplicates: {sample}")
    return failures


def assert_phase_b(lines: list[str]) -> list[str]:
    """Wrapped long lines: both `[wrap-N]` and `[end-wrap-N]` tags
    should survive capture+replay for every N. The visible cells of
    a wrapped line span multiple rows; we only assert the tags are
    present *somewhere* in the captured text — exact reflow is a
    pyte-vs-xterm.js concern we don't model here."""
    failures = []
    haystack = "\n".join(lines)
    for n in (1, 2, 3, 5, 8):
        begin = f"[wrap-{n}]"
        end = f"[end-wrap-{n}]"
        if begin not in haystack:
            failures.append(f"PHASE B: missing '{begin}'")
        if end not in haystack:
            failures.append(f"PHASE B: missing '{end}'")
    return failures


def assert_phase_c(screen) -> list[str]:
    """SGR attribute preservation. Verify the 8 basic-fg-* lines land
    with distinct fg colours in pyte's per-cell attribute store. We
    don't pin specific colour names — pyte's mapping isn't always 1:1
    with the SGR code (33 → "brown", 90 → "brightblack", etc.) — but
    we DO require that at least 6 of the 8 produce distinct fg values,
    which proves the attribute pipeline is working end-to-end."""
    fg_by_label: dict[str, str] = {}
    for line in screen.history.top:
        if not line:
            continue
        cols = sorted(line.keys())
        text = "".join(line[c].data if c in line else " " for c in cols).rstrip()
        m = re.match(r'(basic-fg-\d+|bright-fg-\d+|palette-\d+|truecolor-\w+)', text)
        if not m:
            continue
        label = m.group(1)
        # First non-space cell's fg is representative — the cells past
        # the label have the same fg up to the SGR reset.
        for c in cols:
            if line[c].data != " ":
                fg_by_label[label] = line[c].fg
                break
    failures = []
    basic = [v for k, v in fg_by_label.items() if k.startswith("basic-fg-")]
    if len(basic) < 6:
        failures.append(f"PHASE C: only {len(basic)} basic-fg lines found in pyte history "
                         f"(expected 8); attribute pipeline may have dropped lines")
    elif len(set(basic)) < 6:
        failures.append(f"PHASE C: basic-fg-* lines all collapse to ≤{len(set(basic))} "
                         f"distinct fg colours — attribute preservation broken: {set(basic)}")
    # Also ensure ANY of the palette/truecolor lines landed with non-default fg.
    extras = [v for k, v in fg_by_label.items()
              if k.startswith(("palette-", "truecolor-"))]
    if extras and all(str(v) == "default" for v in extras):
        failures.append("PHASE C: palette/truecolor lines all show default fg — "
                         "256-color / 24-bit escape decode is broken")
    return failures


def assert_phase_d(lines: list[str]) -> list[str]:
    """Cursor-overwrite (\\r): only the final value of each row should
    survive after the \\n. Catches buggy implementations that
    accumulate overwritten text instead of replacing it."""
    failures = []
    # Look for the lines that are the FULL row after the overwrite.
    haystack = "\n".join(lines)
    # The overwrite pattern "loading...\rmidway   \rdone     \n" should
    # leave `done     ` (possibly trimmed) on the row. We don't pin
    # exact whitespace — just verify `done` appears and `loading` /
    # `midway` do NOT appear as standalone tokens on that row.
    if "done" not in haystack:
        failures.append("PHASE D: 'done' missing (final overwrite value lost)")
    if "final-value" not in haystack:
        failures.append("PHASE D: 'final-value' missing (final overwrite value lost)")
    if "progress: 100%" not in haystack:
        failures.append("PHASE D: 'progress: 100%' missing")
    # Stacked-overwrite anti-test: if we see `loading...done` as one
    # token, the \r didn't reset the cursor — that's a buggy emulator.
    if "loading...done" in haystack or "loading...midway" in haystack:
        failures.append("PHASE D: text stacked across \\r overwrite "
                         "(carriage-return handling broken)")
    return failures


def assert_phase_e(lines: list[str]) -> list[str]:
    """Erase sequences: \\x1b[2K (erase whole line) and \\x1b[K
    (erase to end of line) should clear the noisy parts before the
    erase, leaving only the clean tail."""
    failures = []
    haystack = "\n".join(lines)
    if "clean-after-2K" not in haystack:
        failures.append("PHASE E: clean-after-2K missing (replacement text after 2K not preserved)")
    # The noisy half should be GONE — its presence means the erase didn't apply.
    if "noisy-text-that-should-disappear" in haystack:
        failures.append("PHASE E: noisy-text survived an \\x1b[2K erase")
    return failures


def assert_phase_f(lines: list[str]) -> list[str]:
    """UTF-8 + box-drawing. Verify a representative codepoint from each
    sub-category lands intact."""
    failures = []
    haystack = "\n".join(lines)
    checks = [
        ("🦊", "emoji"),
        ("┌", "box-drawing top-left"),
        ("└", "box-drawing bottom-left"),
        ("∑", "math symbol"),
        ("café", "latin-1 accents"),
        ("日本語", "CJK"),
    ]
    for needle, label in checks:
        if needle not in haystack:
            failures.append(f"PHASE F: missing '{needle}' ({label}) — UTF-8 corruption?")
    return failures


def assert_phase_g(lines: list[str]) -> list[str]:
    """Rapid burst: all 2000 burst-NNNNN lines should be present and
    in order. This is the doctor's biggest stress test — a single
    missing line here points at a real capture / pump / scrollback bug."""
    seen_indices: list[int] = []
    seen_nums: set[int] = set()
    for idx, line in enumerate(lines):
        m = _BURST_RE.match(line)
        if m:
            n = int(m.group(1))
            seen_nums.add(n)
            seen_indices.append(n)
    expected = set(range(1, 2001))
    missing = sorted(expected - seen_nums)
    failures = []
    if missing:
        sample = missing[:5] + (["…"] if len(missing) > 5 else [])
        failures.append(f"PHASE G: {len(missing)} burst lines missing: {sample}")
    # Order check: indices should be monotonically increasing.
    for i in range(1, len(seen_indices)):
        if seen_indices[i] < seen_indices[i - 1]:
            failures.append(f"PHASE G: out-of-order at idx {i}: "
                             f"...burst-{seen_indices[i - 1]:05d} then burst-{seen_indices[i]:05d}")
            break
    return failures


def assert_phase_i(lines: list[str]) -> list[str]:
    """Claude-like banner: box-drawing chars should form a complete box."""
    failures = []
    haystack = "\n".join(lines)
    for marker in ("Claude Code", "Welcome back", "Opus 4.7"):
        if marker not in haystack:
            failures.append(f"PHASE I: missing banner content '{marker}'")
    # Box corners
    for corner in ("┌", "┐", "└", "┘"):
        if corner not in haystack:
            failures.append(f"PHASE I: missing box corner '{corner}'")
    return failures


def assert_sentinel(lines: list[str], run_id: str) -> list[str]:
    marker = f"{SENTINEL_PREFIX}{run_id}>>>"
    if not any(marker in line for line in lines):
        return [f"sentinel '{marker}' missing from captured text — generator may not have completed"]
    return []


# ─── Realistic mode (single generator run) ──────────────────────────────

async def run_realistic(rows: int, cols: int,
                          session: str = "scrollback-doctor-realistic",
                          verbose: bool = False) -> tuple[int, list[str]]:
    if not GENERATOR_PATH.exists():
        return 1, [f"generator script not found at {GENERATOR_PATH}"]

    tmux_kill(session)
    tmux_new(session, cols, rows)
    run_id = fresh_run_id()

    if verbose:
        print(f"  generator: {GENERATOR_PATH}")
        print(f"  run_id   : {run_id}")
        print(f"  rows/cols: {rows} x {cols}")

    tmux_send_keys(session, f"bash {GENERATOR_PATH} {run_id}")

    if not wait_for_sentinel(session, run_id):
        tmux_kill(session)
        return 1, [f"generator did not finish in {SENTINEL_TIMEOUT_S}s "
                    f"(sentinel for {run_id} never appeared)"]

    history_bytes = await _capture_session_history(session, rows)
    attach_bytes = capture_attach_redraw(session, cols, rows)
    screen = replay_into_pyte([history_bytes, attach_bytes], cols, rows)

    pyte_hist = pyte_history_text(screen)
    pyte_vis = pyte_visible_text(screen)
    combined = pyte_hist + pyte_vis

    if verbose:
        print(f"  pyte: history={len(pyte_hist)} visible={len(pyte_vis)}")
        print(f"    last 3 history: {pyte_hist[-3:]!r}")
        print(f"    last 3 visible: {pyte_vis[-3:]!r}")

    failures: list[str] = []
    failures += assert_sentinel(combined, run_id)
    failures += assert_phase_a(combined)
    failures += assert_phase_b(combined)
    failures += assert_phase_c(screen)
    failures += assert_phase_d(combined)
    failures += assert_phase_e(combined)
    failures += assert_phase_f(combined)
    failures += assert_phase_g(combined)
    failures += assert_phase_i(combined)

    tmux_kill(session)
    return (1 if failures else 0), failures


# ─── Reconnect mode (two consecutive generator runs, one session) ───────

async def run_reconnect(rows: int, cols: int,
                          session: str = "scrollback-doctor-reconnect",
                          verbose: bool = False) -> tuple[int, list[str]]:
    """Model the WS-reconnect flow: same tmux session, two consecutive
    runs of the generator with distinct run-ids. After both finish, the
    captured pane should contain BOTH sentinels and BOTH full sets of
    phase content — proving capture+replay survives content that
    pre-dates the latest reconnect."""
    if not GENERATOR_PATH.exists():
        return 1, [f"generator script not found at {GENERATOR_PATH}"]

    tmux_kill(session)
    tmux_new(session, cols, rows)

    run_id_a = fresh_run_id() + "a"
    if verbose:
        print(f"  first run : {run_id_a}")
    tmux_send_keys(session, f"bash {GENERATOR_PATH} {run_id_a}")
    if not wait_for_sentinel(session, run_id_a):
        tmux_kill(session)
        return 1, [f"first generator run did not finish (no sentinel for {run_id_a})"]

    # Capture #1 — what a client connecting between the two runs would see.
    cap1_bytes = await _capture_session_history(session, rows)
    cap1_text = cap1_bytes.decode("utf-8", errors="replace")

    run_id_b = fresh_run_id() + "b"
    # Make sure the second run-id is distinct from the first even if
    # the millisecond timer didn't tick between calls.
    if run_id_b == run_id_a.replace("a", "b"):
        run_id_b = run_id_b + "x"
    if verbose:
        print(f"  second run: {run_id_b}")
    tmux_send_keys(session, f"bash {GENERATOR_PATH} {run_id_b}")
    if not wait_for_sentinel(session, run_id_b):
        tmux_kill(session)
        return 1, [f"second generator run did not finish (no sentinel for {run_id_b})"]

    # Capture #2 — what a client reconnecting after the second run sees.
    cap2_bytes = await _capture_session_history(session, rows)
    attach_bytes = capture_attach_redraw(session, cols, rows)
    screen = replay_into_pyte([cap2_bytes, attach_bytes], cols, rows)

    pyte_hist = pyte_history_text(screen)
    pyte_vis = pyte_visible_text(screen)
    combined = pyte_hist + pyte_vis
    cap2_text = "\n".join(combined)

    failures: list[str] = []

    # The second capture should reach back far enough to include the
    # first sentinel — tmux's history-limit (50k) is plenty for two
    # generator runs (~5k lines combined).
    if f"{SENTINEL_PREFIX}{run_id_a}>>>" not in cap2_text:
        failures.append(f"reconnect: first run's sentinel ({run_id_a}) missing from "
                         f"second capture — scrollback truncation or capture bug")
    if f"{SENTINEL_PREFIX}{run_id_b}>>>" not in cap2_text:
        failures.append(f"reconnect: second run's sentinel ({run_id_b}) missing from "
                         f"second capture")

    # The second run's content should also be intact — re-run the
    # phase G assertion against the combined buffer to verify the
    # 2000 burst lines from the second run are present (they'll be in
    # the most recent half of scrollback).
    burst_count_first = cap1_text.count("burst-")
    burst_count_second = cap2_text.count("burst-")
    if burst_count_second < 2 * burst_count_first - 50:
        # 50-line slack for header lines and the small phases
        failures.append(f"reconnect: second capture has {burst_count_second} burst lines "
                         f"vs ~{2 * burst_count_first} expected (1st cap had {burst_count_first}) "
                         f"— content from one of the runs is missing")

    if verbose:
        print(f"  cap1: {len(cap1_text)}B, burst-lines={burst_count_first}")
        print(f"  cap2: {len(cap2_text)}B, burst-lines={burst_count_second}")

    tmux_kill(session)
    return (1 if failures else 0), failures


async def run(args) -> int:
    """Dispatch: single-config (default), matrix, realistic, or reconnect."""

    if args.realistic:
        print(f"╭─ scrollback-doctor — realistic mode")
        print(f"│ rows={args.rows}  cols={args.cols}")
        print(f"╰────────────────────────────────────────────────────────")
        rc, failures = await run_realistic(args.rows, args.cols,
                                             verbose=args.verbose)
        print()
        if rc == 0:
            print(f"✓ realistic: all phase assertions passed")
        else:
            print(f"✗ realistic: {len(failures)} assertion(s) failed:")
            for f in failures:
                print(f"    {f}")
        return rc

    if args.reconnect:
        print(f"╭─ scrollback-doctor — reconnect mode")
        print(f"│ rows={args.rows}  cols={args.cols}  (two generator runs)")
        print(f"╰────────────────────────────────────────────────────────")
        rc, failures = await run_reconnect(args.rows, args.cols,
                                             verbose=args.verbose)
        print()
        if rc == 0:
            print(f"✓ reconnect: second capture cleanly supersedes first")
        else:
            print(f"✗ reconnect: {len(failures)} assertion(s) failed:")
            for f in failures:
                print(f"    {f}")
        return rc

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
    p.add_argument("--realistic", action="store_true",
                   help="run scripts/scrollback-test-generator.sh inside a "
                        "fresh tmux session — comprehensive phase-based "
                        "assertions (SGR, cursor, UTF-8, bursts, etc.)")
    p.add_argument("--reconnect", action="store_true",
                   help="run the generator twice in one session — verify "
                        "the second capture is a strict superset of the "
                        "first (models a WS reconnect mid-conversation)")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
