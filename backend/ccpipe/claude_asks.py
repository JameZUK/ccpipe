"""Notice when Claude stops to ask you something, and say what it asks.

Claude Code marks it in its session file, ``~/.claude/sessions/<pid>.json``:
``"status"`` goes to ``"waiting"`` while a question (its AskUserQuestion
dialog) or a permission prompt is on screen, and ``"waitingFor"`` says which
(``"input needed"``, ``"permission prompt"``). Measured on 2.1.317,
2026-11-03. The question itself is only on the screen: Claude writes the
tool call to the transcript after you answer, so the TTS transcript tail
can't see it in time. So this watches the status, and reads the dialog off
Claude's pane when it appears.

One watcher runs per attached terminal (not tiles). It reports each new
question once; one already on screen when you attach is not reported, since
you're looking at it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import tmux

log = logging.getLogger(__name__)

POLL_S = 1.0          # how often the session file is looked at (a stat)
PID_EVERY_S = 10.0    # how often Claude's pid is re-resolved (it can restart)
# The status flips as the dialog is drawn: give the screen a moment, and look
# again if the dialog isn't there yet.
_SCREEN_TRIES_S = (0.3, 0.6, 1.0)
_MAX_PART = 300


@dataclass(frozen=True)
class Ask:
    kind: str     # "question" | "permission"
    title: str    # for the notification: "Claude has a question"
    body: str     # the question and its options, or what needs permission

    @property
    def spoken(self) -> str:
        lead = {"question": "Claude asks:",
                "permission": "Claude needs your permission."}[self.kind]
        if self.title.startswith("Claude has ") and self.title.endswith(" questions"):
            lead = f"{self.title}. The first:"
        return f"{lead} {self.body}".strip()


# ─── Reading the dialog off the screen ────────────────────────────────────
# A question:                          A permission prompt:
#   ──────────────────────               ──────────────────────
#    ☐ Colour                             Bash command
#                                         Tip: auto mode handles these …
#   Which colour do you prefer?           Delete hello.txt from the …
#                                         ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
#   ❯ 1. Red                              rm hello.txt
#        Choose red                       ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
#     2. Blue                             Do you want to proceed?
#     3. Type something.                  ❯ 1. Yes
#   ──────────────────────                  2. Yes, and always allow …
#     4. Chat about this                    4. No

_OPTION_RE = re.compile(r"^\s*(?:❯\s*)?(\d{1,2})\.\s+(\S.*)$")
_RULE_RE = re.compile(r"^\s*[─━╌┄┈-]{8,}\s*$")
_SOLID_RULE_RE = re.compile(r"^\s*[─━]{8,}\s*$")
_TAB_RE = re.compile(r"[☐☒]")
# Claude's own extra choices on every question, not the question's options.
_BUILTIN = ("Type something", "Chat about this", "Other")
_GENERIC_Q_RE = re.compile(r"(?:Do you want|Would you like) to proceed\??", re.I)
# A preview pane beside the options, or wide padding: cut the label there.
_COLUMN_RE = re.compile(r"\s{3,}|│")


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_PART else text[: _MAX_PART - 1].rstrip() + "…"


def _or_list(labels: list[str]) -> str:
    if len(labels) == 1:
        return labels[0] + "."
    return ", ".join(labels[:-1]) + ", or " + labels[-1] + "."


def describe(lines: list[str], waiting_for: str | None) -> Ask:
    """What the dialog at the bottom of the screen asks. Public for tests."""
    permission = "permission" in (waiting_for or "").lower()
    first = next((i for i in range(len(lines) - 1, -1, -1)
                  if (m := _OPTION_RE.match(lines[i])) and m.group(1) == "1"), None)
    if first is None:
        return (Ask("permission", "Claude needs your permission", "") if permission
                else Ask("question", "Claude has a question", ""))

    # The question: the lines just above the first option.
    j = first - 1
    while j >= 0 and not lines[j].strip():
        j -= 1
    q_lines: list[str] = []
    while j >= 0 and lines[j].strip() and not _RULE_RE.match(lines[j]) \
            and not _TAB_RE.search(lines[j]):
        q_lines.insert(0, lines[j].strip())
        j -= 1
    question = _clip(" ".join(q_lines))

    # The dialog: from the last full-width rule above the options.
    top = max((k for k in range(first) if _SOLID_RULE_RE.match(lines[k])), default=-1)
    block = [ln.strip() for ln in lines[top + 1: first]
             if ln.strip() and not _RULE_RE.match(ln)]

    if permission:
        if question and not _GENERIC_Q_RE.fullmatch(question):
            body = question
        else:
            title = block[0] if block else ""
            detail = next((ln for ln in block[1:]
                           if not ln.startswith("Tip:") and ln != question), "")
            body = _clip(f"{title}: {detail}." if title and detail else title)
        return Ask("permission", "Claude needs your permission", body)

    labels: list[str] = []
    for ln in lines[first:]:
        m = _OPTION_RE.match(ln)
        if not m:
            continue
        label = _COLUMN_RE.split(m.group(2).strip())[0].strip()
        if label and not label.startswith(_BUILTIN):
            labels.append(_clip(label))
    tabs = next((len(_TAB_RE.findall(ln)) for ln in block if _TAB_RE.search(ln)), 0)
    title = f"Claude has {tabs} questions" if tabs > 1 else "Claude has a question"
    body = question
    if labels:
        body = f"{body} {_or_list(labels[:6])}".strip()
    return Ask("question", title, body)


# ─── Watching Claude's status ─────────────────────────────────────────────

def _read_state(pid: int) -> dict[str, Any] | None:
    """Claude's session file, if it is this live process's."""
    path = Path.home() / ".claude" / "sessions" / f"{pid}.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("pid") != pid:
        return None
    start = tmux._proc_start_time(pid)
    if start is None or str(data.get("procStart", start)) != start:
        return None   # a stale file for a reused pid
    return data


async def _pane_of(session: str, state: dict[str, Any]) -> str:
    """Claude's own pane (it may not be the active one), if it really is in
    this session; else the session's active pane."""
    where = state.get("tmux")
    pane = where.rpartition(".")[2] if isinstance(where, str) else ""
    if re.fullmatch(r"%\d+", pane):
        out = await tmux._tmux_out("list-panes", "-s", "-t", tmux.pane_target(session),
                                   "-F", "#{pane_id}")
        if out and pane in out.split():
            return pane
    return tmux.pane_target(session)


async def _read_dialog(session: str, pid: int, state: dict[str, Any]) -> Ask | None:
    """The dialog now on screen, or None if Claude stopped waiting meanwhile
    (you answered it already)."""
    target = await _pane_of(session, state)
    ask: Ask | None = None
    for delay in _SCREEN_TRIES_S:
        await asyncio.sleep(delay)
        now = await asyncio.to_thread(_read_state, pid)
        if not now or now.get("status") != "waiting":
            return None
        out = await tmux._tmux_out("capture-pane", "-p", "-t", target)
        lines = [ln.rstrip() for ln in (out or "").split("\n")]
        ask = describe(lines, now.get("waitingFor"))
        if ask.body:
            return ask
    return ask


async def watch(session: str, on_ask: Callable[[Ask], Awaitable[None]]) -> None:
    """Call ``on_ask`` each time Claude in ``session`` starts waiting on you.
    Runs until cancelled."""
    pid: int | None = None
    pid_at = float("-inf")
    was_waiting: bool | None = None      # None: not read yet
    last_since: Any = None
    last_said = ""
    while True:
        try:
            now = time.monotonic()
            if pid is None or now - pid_at >= PID_EVERY_S:
                pid = await tmux.cached_claude_pid(session)
                pid_at = now
            state = await asyncio.to_thread(_read_state, pid) if pid else None
            waiting = bool(state) and state.get("status") == "waiting"
            since = state.get("statusUpdatedAt") if state else None
            # A new wait: from not waiting, or a fresh status stamp (a second
            # prompt right after the first, between two looks).
            fresh = waiting and (was_waiting is False
                                 or (was_waiting and since != last_since))
            was_waiting, last_since = waiting, since
            if not waiting:
                last_said = ""
            if fresh and pid and state:
                ask = await _read_dialog(session, pid, state)
                if ask is not None and ask.spoken != last_said:
                    last_said = ask.spoken
                    log.info("claude asks in %s: %s", session, ask.title)
                    await on_ask(ask)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("claude_asks watch of %s failed", session, exc_info=True)
        await asyncio.sleep(POLL_S)
