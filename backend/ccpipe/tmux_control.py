"""Tmux control mode event listener.

Spawns a long-lived `tmux -C` subprocess (no attach) and surfaces server-wide
notifications (sessions-changed, window-add/close, exit, etc.) to async
subscribers. Auto-respawns the subprocess if the tmux server dies.

This is purely an event channel — we don't send commands through it. One-shot
operations go through libtmux (see tmux.py). The per-client attached terminal
is a separate `tmux attach-session` PTY (see pty_relay.py / ws.py).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from . import tmux as _tmux  # for TMUX_BIN

log = logging.getLogger(__name__)

# Hidden tmux session ccpipe keeps alive for its control-mode client.
# Filtered from /api/sessions so users don't see it in the picker.
CONTROL_SESSION_NAME = "__ccpipe_ctrl"

EventCallback = Callable[["TmuxEvent"], Awaitable[None]]


@dataclass(frozen=True)
class TmuxEvent:
    """A notification line from `tmux -C`."""
    name: str           # e.g. 'sessions-changed', 'window-add', 'exit'
    args: list[str]     # whitespace-split args after the name
    raw: str            # full line as received, sans trailing newline


# Notifications we forward to subscribers.  %begin/%end/%error/%output are
# part of command-response framing or attached-pane output and are not useful
# to us here (we never send commands and we never attach).
_FORWARDED_PREFIXES = frozenset({
    "sessions-changed",
    "session-changed",
    "session-renamed",
    "session-window-changed",
    "window-add",
    "window-close",
    "window-renamed",
    "unlinked-window-add",
    "unlinked-window-close",
    "unlinked-window-renamed",
    "layout-change",
    "client-attached",
    "client-detached",
    "client-session-changed",
    "exit",
    "pane-mode-changed",
    "subscription-changed",
})


@dataclass
class _Subscription:
    callback: EventCallback
    client: "TmuxControlClient"

    def cancel(self) -> None:
        try:
            self.client._subscribers.remove(self)
        except ValueError:
            pass


@dataclass
class TmuxControlClient:
    """Long-lived control-mode connection. Use as a singleton per process."""
    _subscribers: list[_Subscription] = field(default_factory=list)
    _task: asyncio.Task[None] | None = None
    _proc: asyncio.subprocess.Process | None = None
    _stopped: bool = False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopped = False
        self._task = asyncio.create_task(self._supervise(), name="tmux-control")

    async def stop(self) -> None:
        self._stopped = True
        if self._proc is not None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def subscribe(self, callback: EventCallback) -> _Subscription:
        sub = _Subscription(callback=callback, client=self)
        self._subscribers.append(sub)
        return sub

    async def _supervise(self) -> None:
        """Run tmux -C; restart on exit with exponential backoff."""
        delay = 0.5
        while not self._stopped:
            try:
                await self._run_once()
                delay = 0.5  # reset on clean exit
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tmux control loop crashed")
            if self._stopped:
                return
            log.info("tmux -C exited; restarting in %.1fs", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _run_once(self) -> None:
        # `tmux -C` (or any tmux client) always attaches to a session. If no
        # sessions exist it creates a default "0". To avoid littering the
        # session picker with a spurious "0" we maintain our own dedicated
        # hidden session named CONTROL_SESSION_NAME and attach to that in
        # control mode. The session picker filters it out of /api/sessions.
        # Only create the control session if it doesn't already exist.
        # `new-session -A` (attach-if-exists) was previously used to make
        # the call idempotent, but combined with `-d` tmux tries to
        # attach-detached and fails with "open terminal failed: not a
        # terminal" on every ccpipe restart. has-session is a clean
        # idempotency check.
        try:
            checker = await asyncio.create_subprocess_exec(
                _tmux.TMUX_BIN, "has-session", "-t", CONTROL_SESSION_NAME,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await checker.wait()
        except FileNotFoundError:
            log.error("tmux binary not found on PATH")
            raise
        if checker.returncode != 0:
            try:
                creator = await asyncio.create_subprocess_exec(
                    _tmux.TMUX_BIN, "new-session", "-d",
                    "-s", CONTROL_SESSION_NAME,
                    "sleep", "infinity",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await creator.communicate()
            except FileNotFoundError:
                log.error("tmux binary not found on PATH")
                raise
            if creator.returncode != 0:
                log.warning("tmux new-session for control client failed "
                            "(rc=%s): %s",
                            creator.returncode,
                            stderr.decode(errors="replace").strip()[:300])

        proc = await asyncio.create_subprocess_exec(
            _tmux.TMUX_BIN, "-C", "attach-session", "-t", CONTROL_SESSION_NAME,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._proc = proc
        assert proc.stdout is not None
        try:
            while True:
                line_bytes = await proc.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
                await self._handle_line(line)
        finally:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            await proc.wait()
            self._proc = None

    async def _handle_line(self, line: str) -> None:
        if not line.startswith("%"):
            # Command-response data lines; we don't send commands so ignore.
            return
        # Strip the leading '%' then split off the name token.
        body = line[1:]
        if not body:
            return
        head, _, rest = body.partition(" ")
        if head not in _FORWARDED_PREFIXES:
            return
        args = rest.split(" ") if rest else []
        event = TmuxEvent(name=head, args=args, raw=line)
        await self._dispatch(event)

    async def _dispatch(self, event: TmuxEvent) -> None:
        # Snapshot then fan out concurrently so one slow subscriber doesn't
        # stall events for the others (mobile-WS reconnect can easily wedge
        # a send for hundreds of ms).
        subs = list(self._subscribers)
        if not subs:
            return
        results = await asyncio.gather(
            *(sub.callback(event) for sub in subs),
            return_exceptions=True,
        )
        for sub, result in zip(subs, results):
            if isinstance(result, BaseException):
                # log.exception only works inside an except block — outside
                # one it prints "NoneType: None" as the traceback, hiding the
                # real cause. Pass exc_info explicitly so the traceback comes
                # from the gathered exception object instead.
                log.error("subscriber raised on %s: %r",
                          event.name, result,
                          exc_info=(type(result), result, result.__traceback__))


# Module-level singleton, populated by main.py's lifespan handler.
control_client: TmuxControlClient = TmuxControlClient()
