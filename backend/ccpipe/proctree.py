"""A snapshot of the process table, for per-session resource use and for
telling a busy session from an idle one (lifecycle.py, sysstats.py).

Reads /proc directly (Linux only; everything degrades to "unknown" where
/proc isn't there). One snapshot is a single pass over /proc/*/stat; argv
and memory are read lazily, only for the processes that need them.
"""
from __future__ import annotations

import contextlib
import os
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cached_property

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

# Claude Code runs every Bash-tool command — foreground or background — as
# `bash -c 'source ~/.claude/shell-snapshots/snapshot-… && eval …'`. A
# process carrying this in its argv is a task Claude started.
CLAUDE_TASK_MARKER = "/.claude/shell-snapshots/"


@dataclass
class Proc:
    pid: int
    ppid: int
    comm: str
    start: float          # seconds since boot
    cpu: float            # user + system CPU seconds so far
    # CPU seconds of its children that have exited and been waited for
    # (cutime + cstime). A finished child's time moves here, so a subtree's
    # cpu + reaped keeps counting short-lived processes (compiler runs, a
    # test suite) that start and end between two samples.
    reaped: float = 0.0

    @cached_property
    def argv(self) -> list[str]:
        try:
            with open(f"/proc/{self.pid}/cmdline", "rb") as f:
                raw = f.read()
        except OSError:
            return []
        return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]

    @property
    def is_claude(self) -> bool:
        if self.comm == "claude":
            return True
        head = self.argv[:2]
        return any(os.path.basename(a) in ("claude", "cli.js") and "claude" in a for a in head)

    @property
    def is_claude_task(self) -> bool:
        return any(CLAUDE_TASK_MARKER in a for a in self.argv)


@dataclass
class Table:
    procs: dict[int, Proc]
    kids: dict[int, list[int]] = field(default_factory=dict)

    def subtree(self, pid: int) -> list[int]:
        """pid and all its descendants."""
        out, stack = [], [pid]
        while stack:
            p = stack.pop()
            if p in self.procs:
                out.append(p)
                stack.extend(self.kids.get(p, ()))
        return out

    def cpu_of(self, pids: list[int], reaped: bool = False) -> float:
        """CPU seconds used by pids; reaped=True adds their finished children's."""
        return sum(self.procs[p].cpu + (self.procs[p].reaped if reaped else 0.0)
                   for p in pids if p in self.procs)


def snapshot() -> Table:
    procs: dict[int, Proc] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return Table({}, {})
    for d in names:
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                st = f.read()
        except OSError:
            continue                       # exited mid-scan
        try:
            # comm may contain spaces / parens: split around the LAST ")".
            comm = st[st.index("(") + 1:st.rindex(")")]
            rest = st[st.rindex(")") + 2:].split()
            procs[int(d)] = Proc(
                pid=int(d), ppid=int(rest[1]), comm=comm,
                start=int(rest[19]) / CLK_TCK,
                cpu=(int(rest[11]) + int(rest[12])) / CLK_TCK,
                reaped=(int(rest[13]) + int(rest[14])) / CLK_TCK,
            )
        except (ValueError, IndexError):
            continue
    kids: dict[int, list[int]] = defaultdict(list)
    for p in procs.values():
        kids[p.ppid].append(p.pid)
    return Table(procs, dict(kids))


def pss_bytes(pid: int) -> int:
    """Proportional set size: the process's private memory plus its share of
    pages shared with others — sums across processes without the double
    counting RSS has (RSS over-reported the sessions by ~60 %)."""
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for line in f:
                if line.startswith("Pss:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


async def run_argv(argv: list[str], timeout: float, *, env: dict[str, str] | None = None,
                   check: bool = False) -> str | None:
    """stdout of a command given as an argv list — no shell, so nothing in
    argv is ever interpreted — or None if it couldn't start, timed out (it
    is killed and reaped) or, with ``check``, exited non-zero. The one
    subprocess helper for ccpipe's short queries (tmux, du)."""
    import asyncio
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env)
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return None
    if check and proc.returncode != 0:
        return None
    return out.decode("utf-8", "replace")
