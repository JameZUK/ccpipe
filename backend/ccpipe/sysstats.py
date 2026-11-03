"""Host and per-session resource use, for the session picker and the
session bar.

* Host: CPU %, memory, swap, load and the root filesystem, sampled every
  SAMPLE_S with HISTORY samples kept for the graphs. Cheap (three small
  /proc reads and a statvfs), so it always runs.
* Per session: CPU (share of the whole machine) every SESSION_S, and
  memory (PSS of the session's process tree — every pane, Claude, its MCP
  helpers and anything they started). A process's PSS is re-read at most
  every PSS_S: smaps_rollup walks the whole address space under the
  process's mmap lock, which is the expensive part of a pass (hundreds of
  processes, several with multi-GB maps), and memory moves slowly.
* Per project: the disk space its directory takes (`du -x`), refreshed
  every DU_REFRESH_S one directory at a time at idle CPU / IO priority.
  All 23 project dirs measured 2026-11-02: 70 GB in 4.6 s.

The per-session and per-project work only happens while someone is looking
(a request within DEMAND_S); `touch()` marks demand and wakes the loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Any

from . import proctree, session_meta, tmux

log = logging.getLogger(__name__)

SAMPLE_S = 5
HISTORY = 120                 # ten minutes of samples
SESSION_S = 15
PSS_S = 60
DEMAND_S = 300
DU_REFRESH_S = 15 * 60
DU_TIMEOUT_S = 180
DISK_PATH = "/"

_history: deque[tuple[float, float | None, float | None]] = deque(maxlen=HISTORY)
_host: dict[str, Any] = {}
_last_cpu: tuple[int, int] | None = None
_sessions: dict[str, dict[str, Any]] = {}
_session_cwds: dict[str, str] = {}
_tree_cpu: dict[str, tuple[float, float]] = {}
_pss: dict[int, tuple[float, int, float]] = {}   # pid → (start, bytes, measured at)
_sessions_at = -1e9
_sizes: dict[str, tuple[int | None, float]] = {}
_demanded_at = -1e9
_wake: asyncio.Event | None = None
_du_task: asyncio.Task[None] | None = None


def touch() -> None:
    """Someone is looking: gather per-session figures for a while."""
    global _demanded_at
    first = time.monotonic() - _demanded_at > DEMAND_S
    _demanded_at = time.monotonic()
    if first and _wake is not None:
        _wake.set()


def _demanded() -> bool:
    return time.monotonic() - _demanded_at <= DEMAND_S


def demanded() -> bool:
    """Someone has had ccpipe's stats on screen within DEMAND_S."""
    return _demanded()


# ─── host ─────────────────────────────────────────────────────────────────

def _read_cpu() -> tuple[int, int] | None:
    """(total, idle) jiffies from the aggregate line of /proc/stat."""
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
    except OSError:
        return None
    if not parts or parts[0] != "cpu":
        return None
    vals = [int(v) for v in parts[1:9]]
    return sum(vals), vals[3] + vals[4]         # idle + iowait


def _read_mem() -> dict[str, int]:
    keys = {"MemTotal": "total", "MemAvailable": "available",
            "SwapTotal": "swap_total", "SwapFree": "swap_free"}
    out: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                if k in keys:
                    out[keys[k]] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return out


def sample_host() -> None:
    global _last_cpu
    now = time.time()
    cpu_pct: float | None = None
    cur = _read_cpu()
    if cur and _last_cpu:
        dt, di = cur[0] - _last_cpu[0], cur[1] - _last_cpu[1]
        if dt > 0:
            cpu_pct = round(max(0.0, min(100.0, 100.0 * (dt - di) / dt)), 1)
    if cur:
        _last_cpu = cur
    mem = _read_mem()
    total = mem.get("total")
    used = total - mem.get("available", 0) if total else None
    mem_pct = round(100.0 * used / total, 1) if total and used is not None else None
    disk: dict[str, Any] = {"path": DISK_PATH}
    try:
        st = os.statvfs(DISK_PATH)
        disk.update(total=st.f_blocks * st.f_frsize, free=st.f_bavail * st.f_frsize,
                    used=(st.f_blocks - st.f_bfree) * st.f_frsize)
    except OSError:
        pass
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        load = []
    swap_total = mem.get("swap_total") or 0
    _host.update(
        at=now,
        cpu={"pct": cpu_pct, "cores": os.cpu_count() or 1, "load": load},
        mem={"total": total, "available": mem.get("available"), "used": used, "pct": mem_pct,
             "swap_total": swap_total,
             "swap_used": swap_total - mem.get("swap_free", 0) if swap_total else 0},
        disk=disk,
    )
    if cpu_pct is not None or mem_pct is not None:
        _history.append((now, cpu_pct, mem_pct))


# ─── per session ──────────────────────────────────────────────────────────

def _pss_of(proc: proctree.Proc, now: float) -> int:
    hit = _pss.get(proc.pid)
    if hit and hit[0] == proc.start and now - hit[2] < PSS_S:
        return hit[1]
    val = proctree.pss_bytes(proc.pid)
    _pss[proc.pid] = (proc.start, val, now)
    return val


def _measure_sessions(panes: dict[str, list[int]]) -> dict[str, dict[str, Any]]:
    table = proctree.snapshot()
    now = time.monotonic()
    for gone in set(_pss) - set(table.procs):
        _pss.pop(gone, None)
    cores = os.cpu_count() or 1
    out: dict[str, dict[str, Any]] = {}
    for name, roots in panes.items():
        pids: set[int] = set()
        for r in roots:
            pids.update(table.subtree(r))
        cpu_s = table.cpu_of(list(pids))
        prev = _tree_cpu.get(name)
        pct = None
        if prev and now > prev[0]:
            pct = round(max(0.0, (cpu_s - prev[1]) / (now - prev[0]) * 100.0 / cores), 1)
        _tree_cpu[name] = (now, cpu_s)
        out[name] = {"cpu": pct, "mem": sum(_pss_of(table.procs[p], now) for p in pids)}
    for gone in set(_tree_cpu) - set(panes):
        _tree_cpu.pop(gone, None)
    return out


async def _session_pass() -> None:
    global _sessions, _sessions_at
    panes = await tmux.all_panes()
    cwds = await tmux.active_panes()
    _sessions = await asyncio.to_thread(_measure_sessions, panes)
    _session_cwds.clear()
    _session_cwds.update({n: c for n, (c, _t) in cwds.items() if c})
    _sessions_at = time.monotonic()


def session_stats() -> dict[str, dict[str, Any]]:
    return _sessions


# ─── per project ──────────────────────────────────────────────────────────

def _measurable(path: str) -> bool:
    # The whole filesystem or the whole home directory would be a huge scan
    # every quarter hour, and isn't "a project".
    p = os.path.realpath(path)
    return p not in ("/", os.path.realpath(str(Path.home()))) and os.path.isdir(p)


def _du_argv(path: str) -> list[str]:
    argv = ["du", "-s", "-B1", "-x", "--", path]
    if shutil.which("nice"):
        argv = ["nice", "-n", "19", *argv]
    if shutil.which("ionice"):
        argv = ["ionice", "-c", "3", *argv]
    return argv


async def _du(path: str) -> int | None:
    # du exits 1 when some entries were unreadable but still prints a total.
    out = await proctree.run_argv(_du_argv(path), DU_TIMEOUT_S)
    try:
        return int(out.split()[0]) if out else None
    except (ValueError, IndexError):
        return None


async def _sizes_pass() -> None:
    paths = set(_session_cwds.values())
    paths.update(e["cwd"] for e in session_meta.load().values() if e.get("cwd"))
    paths = {p for p in paths if _measurable(p)}
    for stale in set(_sizes) - paths:
        _sizes.pop(stale, None)
    # Oldest (or never measured) first.
    for path in sorted(paths, key=lambda p: _sizes.get(p, (None, -1e9))[1]):
        if not _demanded():
            return
        if time.monotonic() - _sizes.get(path, (None, -1e9))[1] < DU_REFRESH_S:
            continue
        _sizes[path] = (await _du(path), time.monotonic())


def project_size(cwd: str | None) -> int | None:
    if not cwd:
        return None
    hit = _sizes.get(cwd)
    return hit[0] if hit else None


# ─── loop + snapshot ──────────────────────────────────────────────────────

async def run() -> None:
    """Background sampler (started by the app lifespan)."""
    global _wake, _du_task
    _wake = asyncio.Event()
    while True:
        try:
            sample_host()
            if _demanded():
                if time.monotonic() - _sessions_at >= SESSION_S:
                    await _session_pass()
                if _du_task is None or _du_task.done():
                    _du_task = asyncio.create_task(_sizes_pass())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("stats sample failed")
        _wake.clear()
        try:
            await asyncio.wait_for(_wake.wait(), timeout=SAMPLE_S)
        except asyncio.TimeoutError:
            pass


def stop() -> None:
    if _du_task is not None:
        _du_task.cancel()


def snapshot(session: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "interval": SAMPLE_S,
        **{k: _host.get(k) for k in ("at", "cpu", "mem", "disk")},
        "history": {
            "cpu": [c for _t, c, _m in _history],
            "mem": [m for _t, _c, m in _history],
        },
    }
    if session:
        cwd = _session_cwds.get(session)
        s = _sessions.get(session, {})
        out["session"] = {"name": session, "cpu": s.get("cpu"), "mem": s.get("mem"),
                          "cwd": cwd, "disk": project_size(cwd)}
    return out
