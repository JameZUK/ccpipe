"""Background sessions: work that carries on, but gives way.

A session marked "background" (session picker → ⋮) keeps running while
you work in another one, and yields to everything else:

* **CPU.** A small share (``BG_WEIGHT`` against the default 100) whenever
  something else wants the processor; all of it when nothing does.
* **Memory.** It may only grow into memory that is spare. ``RESERVE`` of
  the machine's RAM is kept available for everything else: as available
  memory falls towards that, the session's soft limit (``memory.high``)
  closes in on what it already uses, and below it the kernel pushes the
  session's pages out to swap. That slows it down; the limit never kills.
* **If the machine runs out anyway**, its processes are the ones the
  kernel picks first (``oom_score_adj``), not tmux, ccpipe or the session
  you're working in.

How: tmux (3.4+) starts every pane in its own systemd scope
(``tmux-spawn-<uuid>.scope``), a cgroup holding the pane's whole process
tree, including whatever it starts later. The CPU share is a unit
property (``systemctl --user set-property --runtime``, which also turns
the CPU controller on for the slice); the memory limit is written to the
cgroup file directly, since it changes every few seconds.

Why not ``nice``: measured 2026-11-12 on the machine this was written
for. The nice limit is 0, so a lowered priority can't be raised again;
and with kernel autogrouping each pane is its own group, so a nice value
doesn't rank one session against another at all. ``memory.low`` (protect
the normal sessions instead) needs the cgroups above the user manager,
which are root's.

Nothing is remembered here. The level lives in session_meta.py; the loop
compares what each pane's scope has with what it should have and fixes
the difference, because scopes come and go (a new pane, a resumed
session and ``respawn-pane`` each get a fresh one), and a pane spends its
first moments in the tmux server's own scope, which is never touched.
A scope ccpipe changed carries ``cpu.weight == BG_WEIGHT``: that marker
is how a restarted ccpipe finds what to put back.

Linux with cgroup v2 and systemd only; elsewhere ``supported()`` is
False and the picker doesn't offer it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import proctree, session_meta, tmux

log = logging.getLogger(__name__)

LEVELS = ("normal", "background")
BG_WEIGHT = 5                 # of a default 100: measured ~1/6 of a normal
                              # session's CPU with both flat out on 8 cores
BG_OOM_ADJ = 800              # (the idle class got 3 %: too close to stopped)
TICK_S = 5
RESERVE_FRACTION = 0.15       # of RAM kept available for everything else
RESERVE_MIN = 1 << 30
LIMIT_FLOOR = 256 << 20       # never squeeze a session below this
STEP = 0.25                   # at most this much of its memory taken back per tick
REWRITE_STEP = 8 << 20        # don't rewrite a limit for less than this

CGROUP_ROOT = Path("/sys/fs/cgroup")
_SCOPE_RE = re.compile(r"^tmux-spawn-[0-9a-f-]{8,64}\.scope$")

_wake: asyncio.Event | None = None
_dirty = True                 # a pass is owed even with no background session
_usable: bool | None = None   # panes really are in scopes of their own
_ours: set[str] = set()       # cgroup paths this process has changed
_held: set[str] = set()       # sessions whose memory is being held back now
_warned: dict[str, float] = {}


def _warn(key: str, msg: str, *args: Any) -> None:
    now = time.monotonic()
    if now - _warned.get(key, -1e9) >= 60:
        _warned[key] = now
        log.warning(msg, *args)


def supported() -> bool:
    """This host can do it at all (says nothing about tmux's version)."""
    return (sys.platform == "linux" and (CGROUP_ROOT / "cgroup.controllers").is_file()
            and shutil.which("systemctl") is not None)


def available() -> bool:
    """Offer it: supported, and not known to be without per-pane scopes."""
    return supported() and _usable is not False


def level_of(entry: dict[str, Any] | None) -> str:
    return "background" if (entry or {}).get("priority") == "background" else "normal"


def held(name: str) -> bool:
    """Its memory is being held back (or pushed to swap) right now."""
    return name in _held


async def set_level(name: str, level: str) -> None:
    if level not in LEVELS:
        raise ValueError("unknown level")
    await asyncio.to_thread(session_meta.set_priority, name, level)
    poke()


def poke() -> None:
    """Something changed (a level, a resumed session): look now."""
    global _dirty
    _dirty = True
    if _wake is not None:
        _wake.set()


# ─── reading the cgroups ──────────────────────────────────────────────────

def _cgroup_path(pid: int) -> str | None:
    """The process's cgroup v2 path ("/user.slice/…"), or None."""
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            for line in f:
                if line.startswith("0::"):
                    return line[3:].strip()
    except OSError:
        pass
    return None


def _scope_dir(pid: int, never: set[str]) -> Path | None:
    """The pane's own scope, as a directory under /sys/fs/cgroup. None when
    it isn't in one, or is (still) in the tmux server's or ccpipe's."""
    path = _cgroup_path(pid)
    if not path or path in never or ".." in path.split("/"):
        return None
    if not _SCOPE_RE.match(path.rsplit("/", 1)[-1]):
        return None
    return CGROUP_ROOT / path.lstrip("/")


def _read_int(path: Path) -> int | None:
    """A cgroup file holding one number; "max" and a missing file are None."""
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _write(path: Path, value: str) -> bool:
    try:
        with open(path, "w") as f:
            f.write(value)
        return True
    except OSError as exc:
        _warn(f"write:{path.name}", "could not set %s to %s: %s", path, value, exc)
        return False


def _pids(scope: Path) -> list[int]:
    try:
        return [int(p) for p in (scope / "cgroup.procs").read_text().split()]
    except (OSError, ValueError):
        return []


def _meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    out[key] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return out


# ─── what a scope should have ─────────────────────────────────────────────

def _survey(panes: dict[str, list[int]], wanted: set[str],
            never: set[str]) -> tuple[dict[str, list[Path]], list[Path], bool]:
    """(background session → its scopes, scopes to put back, any scope seen).

    To put back: a scope of a session that isn't background but carries
    our marker, or that this process changed."""
    background: dict[str, list[Path]] = {}
    restore: list[Path] = []
    seen = False
    for name, pids in panes.items():
        scopes = {s for s in (_scope_dir(p, never) for p in pids) if s is not None}
        seen = seen or bool(scopes)
        if name in wanted:
            background[name] = sorted(scopes)
        else:
            restore.extend(s for s in sorted(scopes)
                           if str(s) in _ours or _read_int(s / "cpu.weight") == BG_WEIGHT)
    return background, restore, seen


def memory_limits(usage: dict[str, int], available: int, total: int) -> dict[str, int]:
    """The soft limit for each background scope, from what each uses now.

    With more than the reserve available, the spare part is shared out on
    top of what they use (so none can outgrow it before the next tick).
    With less, the shortfall is taken back from them in proportion, at
    most STEP of each per tick and never below LIMIT_FLOOR."""
    if not usage:
        return {}
    reserve = max(RESERVE_MIN, int(total * RESERVE_FRACTION))
    spare = available - reserve
    if spare >= 0:
        each = spare // len(usage)
        return {k: max(LIMIT_FLOOR, cur + each) for k, cur in usage.items()}
    in_use = sum(usage.values()) or 1
    out: dict[str, int] = {}
    for k, cur in usage.items():
        take = min(int(-spare * cur / in_use), int(cur * STEP))
        out[k] = max(LIMIT_FLOOR, cur - take)
    return out


def _apply_memory(background: dict[str, list[Path]]) -> set[str]:
    """Set every background scope's soft limit; the sessions held back."""
    mem = _meminfo()
    if "MemTotal" not in mem or "MemAvailable" not in mem:
        return set()
    usage: dict[str, int] = {}
    owner: dict[str, str] = {}
    for name, scopes in background.items():
        for scope in scopes:
            cur = _read_int(scope / "memory.current")
            if cur is not None:
                usage[str(scope)] = cur
                owner[str(scope)] = name
    held_now: set[str] = set()
    for key, limit in memory_limits(usage, mem["MemAvailable"], mem["MemTotal"]).items():
        limit -= limit % 4096
        if limit <= usage[key]:
            held_now.add(owner[key])
        path = Path(key) / "memory.high"
        old = _read_int(path)
        if old is not None and abs(old - limit) < REWRITE_STEP:
            continue
        # Lowering it below the usage reclaims before the write returns
        # (1 GB to swap took well under a second), hence the worker thread.
        if _write(path, str(limit)):
            _ours.add(key)
    return held_now


def _own_oom_adj() -> int | None:
    try:
        return int(Path("/proc/self/oom_score_adj").read_text())
    except (OSError, ValueError):
        return None


def _apply_oom(scope: Path, background: bool, base: int | None) -> None:
    """Prefer (or stop preferring) this scope's processes when the kernel
    has to kill for memory. New processes inherit it; the ones already
    running are set here. Raising is always allowed; putting it back to
    ccpipe's own value is too, since that is what they started with."""
    if base is None or base >= BG_OOM_ADJ:
        return
    for pid in _pids(scope):
        path = Path(f"/proc/{pid}/oom_score_adj")
        try:
            cur = int(path.read_text())
            if background and cur < BG_OOM_ADJ:
                path.write_text(str(BG_OOM_ADJ))
            elif not background and cur == BG_OOM_ADJ:
                path.write_text(str(base))
        except (OSError, ValueError):
            continue                       # exited, or not ours to change


def _apply_files(background: dict[str, list[Path]], restore: list[Path]) -> set[str]:
    base = _own_oom_adj()
    for scopes in background.values():
        for scope in scopes:
            _apply_oom(scope, True, base)
    for scope in restore:
        _apply_oom(scope, False, base)
        if _read_int(scope / "memory.high") is not None:
            _write(scope / "memory.high", "max")
    return _apply_memory(background)


# ─── the CPU share (a unit property) ──────────────────────────────────────

def _systemctl_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return env


async def _set_weight(scope: Path, weight: int | None) -> bool:
    """CPUWeight for the scope's unit; None puts the default back."""
    value = "" if weight is None else str(weight)
    out = await proctree.run_argv(
        ["systemctl", "--user", "set-property", "--runtime", scope.name, f"CPUWeight={value}"],
        10.0, env=_systemctl_env(), check=True)
    if out is None:
        _warn("systemctl", "could not set the CPU share of %s", scope.name)
    return out is not None


async def _never_touch() -> set[str] | None:
    """The cgroups of the tmux server and of ccpipe itself; None when the
    server can't be asked (then nothing is changed this tick)."""
    out = await tmux._tmux_out("display-message", "-p", "#{pid}")
    if not out or not out.strip().isdigit():
        return None
    return {p for p in (_cgroup_path(int(out.strip())), _cgroup_path(os.getpid())) if p}


async def _tick() -> None:
    global _dirty, _usable
    if not supported():
        return
    meta = await asyncio.to_thread(session_meta.load)
    wanted = {name for name, entry in meta.items() if level_of(entry) == "background"}
    if not wanted and not _dirty:
        _held.clear()
        return
    panes = await tmux.all_panes()
    never = await _never_touch()
    if never is None:
        return                              # no tmux server: nothing to set
    background, restore, seen = await asyncio.to_thread(_survey, panes, wanted, never)
    if panes:
        _usable = seen
    ok = True
    for scopes in background.values():
        for scope in scopes:
            if _read_int(scope / "cpu.weight") != BG_WEIGHT:
                if await _set_weight(scope, BG_WEIGHT):
                    _ours.add(str(scope))
                else:
                    ok = False
    held_now = await asyncio.to_thread(_apply_files, background, restore)
    # The marker goes last, so a failure before it leaves the scope findable.
    for scope in restore:
        if _read_int(scope / "cpu.weight") == BG_WEIGHT and not await _set_weight(scope, None):
            ok = False
            continue
        _ours.discard(str(scope))
    for gone in [p for p in _ours if not os.path.isdir(p)]:
        _ours.discard(gone)
    _held.clear()
    _held.update(held_now)
    _dirty = not ok


async def run() -> None:
    """Keep every session's scopes at its level (started by the app)."""
    global _wake
    _wake = asyncio.Event()
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("priority pass failed")
        _wake.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_wake.wait(), timeout=TICK_S)
