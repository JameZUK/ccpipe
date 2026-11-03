"""How much of the Claude subscription's usage limits is used, for the
session bar, its pop-up and the landing page (``plan`` in /api/system).

Both sources are the official ``claude`` program reporting on itself;
ccpipe never reads or sends the login token.

1. **The status line.** Sessions ccpipe starts run ``claude --settings``
   with statusline_record.py as the status-line command. After each reply
   Claude hands it ``rate_limits`` (``five_hour`` / ``seven_day``: used %
   and reset time) and the context window's fill; the recorder saves that
   to ``<state>/statusline/<tmux server pid>_<pane>.json`` and runs your
   own status-line command, so Claude's footer is unchanged. Live and free,
   but only the all-models weekly meter (and the 5-hour one on plans that
   have it), and only from ccpipe's sessions.
2. **``/usage``, headless.** ``claude -p /usage --output-format
   stream-json`` answers with the ``/usage`` report, which carries
   ``usage_report.rate_limits``: every meter the server reports (weekly all
   models, weekly per model, the 5-hour session one where it applies) and
   the usage credits. No model call, no tokens; about 13 s (measured
   2026-11-03 with MCP servers skipped). It sees use from anywhere: claude.ai,
   the apps, other machines. The endpoint behind it is itself rate limited
   (``/usage`` in your sessions then says "rate limited until …"), so it
   runs at most every ``poll_minutes`` while someone is looking at ccpipe,
   when the pop-up opens on a reading older than FRESH_S, and never twice
   within MIN_GAP_S.

The readout is the latest ``/usage`` report, with the status line's numbers
laid over the meters they cover when they are newer.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, sysstats, tmux
from .paths import state_dir
from .safe_write import atomic_write_text
from .tmux_control import control_client

log = logging.getLogger(__name__)

FRESH_S = 5 * 60            # the pop-up asks again when the reading is older
MIN_GAP_S = 2 * 60          # never two /usage runs closer than this
BACKOFF_MAX_S = 60 * 60
RUN_TIMEOUT_S = 90
TICK_S = 60
RECORD_MAX_AGE_S = 8 * 24 * 3600
_RECORDER = Path(__file__).with_name("statusline_record.py")
_LIMITED_RE = re.compile(r"rate limited until|last-known usage", re.I)
_spawn = asyncio.create_subprocess_exec

WINDOW_S = {"session": 5 * 3600, "weekly": 7 * 24 * 3600}


def statusline_dir() -> Path:
    return state_dir() / "statusline"


def _work_dir() -> Path:
    return state_dir() / "plan-usage"


def _state_path() -> Path:
    return _work_dir() / "usage.json"


# ─── launching Claude ─────────────────────────────────────────────────────

def _own_status_line() -> dict[str, Any]:
    conf = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    try:
        data = json.loads((Path(conf) / "settings.json").read_text())
    except (OSError, ValueError):
        return {}
    sl = data.get("statusLine") if isinstance(data, dict) else None
    return sl if isinstance(sl, dict) else {}


def launch_settings() -> str | None:
    """The ``--settings`` JSON that makes Claude run the recorder as its
    status line, or None when that's turned off. Your own status line's
    ``padding`` and ``refreshInterval`` carry over (read at launch)."""
    cfg = config.load().plan_usage
    if not (cfg.enabled and cfg.statusline):
        return None
    cmd = " ".join(shlex.quote(p) for p in (sys.executable, "-I", str(_RECORDER), str(statusline_dir())))
    sl: dict[str, Any] = {"type": "command", "command": cmd}
    own = _own_status_line()
    for key in ("padding", "refreshInterval"):
        if isinstance(own.get(key), int) and not isinstance(own.get(key), bool):
            sl[key] = own[key]
    return json.dumps({"statusLine": sl}, separators=(",", ":"))


def claude_command(args: str = "") -> str:
    """The shell command that starts Claude in a session: ``claude``, the
    status-line recorder's ``--settings``, then *args* (already quoted)."""
    parts = ["claude"]
    flag = launch_settings()
    if flag:
        parts += ["--settings", shlex.quote(flag)]
    if args:
        parts.append(args)
    return " ".join(parts)


# ─── the /usage report ────────────────────────────────────────────────────

def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _epoch(v: Any) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _scope_name(scope: Any) -> str | None:
    if not isinstance(scope, dict):
        return None
    for key in ("model", "surface"):
        part = scope.get(key)
        if isinstance(part, dict) and isinstance(part.get("display_name"), str):
            return part["display_name"]
    return None


def label(kind: str, scope: Any = None) -> str:
    if kind == "session":
        return "5-hour"
    if kind == "weekly_all":
        return "week · all models"
    if kind == "weekly_scoped":
        return f"week · {_scope_name(scope) or 'scoped'}"
    return kind.replace("_", " ")


def parse_report(stdout: str) -> tuple[dict[str, Any] | None, str]:
    """(usage_report, the report's text) from ``claude -p /usage
    --output-format stream-json`` output."""
    for line in stdout.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict) or msg.get("type") != "assistant":
            continue
        message = msg.get("message") if isinstance(msg.get("message"), dict) else {}
        content = message.get("content")
        text = ""
        if isinstance(content, list):
            text = "\n".join(c["text"] for c in content
                             if isinstance(c, dict) and isinstance(c.get("text"), str))
        report = msg.get("usage_report")
        return (report if isinstance(report, dict) else None), text
    return None, ""


def meters_from_report(report: dict[str, Any], at: float
                       ) -> tuple[list[dict[str, Any]], dict[str, Any] | None] | None:
    """The report's meters and usage credits, or None when it carries no
    plan limits (an API-key login, or a token without the profile scope)."""
    rl = report.get("rate_limits")
    if not isinstance(rl, dict):
        return None
    meters: list[dict[str, Any]] = []
    for row in rl.get("limits") or []:
        if not isinstance(row, dict):
            continue
        kind = row.get("kind")
        pct = _num(row.get("percent"))
        if not isinstance(kind, str) or not kind or pct is None:
            continue
        group = row.get("group") if isinstance(row.get("group"), str) else ""
        meters.append({
            "kind": kind, "group": group, "label": label(kind, row.get("scope")),
            "percent": round(max(0.0, pct), 1), "resets_at": _epoch(row.get("resets_at")),
            "window": WINDOW_S.get(group) or WINDOW_S.get(kind),
            "severity": row.get("severity") if isinstance(row.get("severity"), str) else "",
            "active": row.get("is_active") is True, "source": "usage", "at": at,
        })
    credits = None
    eu = rl.get("extra_usage")
    if isinstance(eu, dict):
        credits = {
            "enabled": eu.get("is_enabled") is True,
            "limit": _num(eu.get("monthly_limit")), "used": _num(eu.get("used_credits")),
            "pct": _num(eu.get("utilization")),
            "currency": eu.get("currency") if isinstance(eu.get("currency"), str) else None,
        }
    return meters, credits


# ─── status-line readings ─────────────────────────────────────────────────

_records: dict[str, tuple[int, float, dict[str, Any]]] = {}    # name → (mtime_ns, mtime, data)


def _read_record(path: Path) -> tuple[float, dict[str, Any]] | None:
    try:
        st = path.stat()
    except OSError:
        _records.pop(path.name, None)
        return None
    hit = _records.get(path.name)
    if hit and hit[0] == st.st_mtime_ns:
        return hit[1], hit[2]
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    _records[path.name] = (st.st_mtime_ns, st.st_mtime, data)
    return st.st_mtime, data


def latest_statusline() -> tuple[float, dict[str, Any]] | None:
    """(when, rate_limits) from the newest status-line reading that has
    them. They are the account's, whichever session reported them."""
    try:
        paths = [p for p in statusline_dir().iterdir() if p.suffix == ".json"]
    except OSError:
        return None
    best: tuple[float, dict[str, Any]] | None = None
    for p in paths:
        rec = _read_record(p)
        if not rec:
            continue
        at, data = rec
        rl = data.get("rate_limits")
        if isinstance(rl, dict) and rl and (best is None or at > best[0]):
            best = (at, rl)
    return best


def _prune_records(now: float) -> None:
    try:
        paths = list(statusline_dir().iterdir())
    except OSError:
        return
    for p in paths:
        with contextlib.suppress(OSError):
            if now - p.stat().st_mtime > RECORD_MAX_AGE_S:
                p.unlink()
                _records.pop(p.name, None)


_SL_KINDS = (("five_hour", "session", "session"), ("seven_day", "weekly_all", "weekly"))


def merge(usage: dict[str, Any] | None, statusline: tuple[float, dict[str, Any]] | None,
          now: float) -> list[dict[str, Any]]:
    """The /usage meters with the status line's figures laid over the ones
    it covers, when they are newer. A window whose reset time has passed
    reads 0 % (marked ``reset``) until the next reading."""
    meters = [dict(m) for m in (usage or {}).get("meters") or []]
    usage_at = (usage or {}).get("at") or 0
    if statusline and statusline[0] > usage_at:
        at, rl = statusline
        for key, kind, group in _SL_KINDS:
            w = rl.get(key)
            pct = _num(w.get("used_percentage")) if isinstance(w, dict) else None
            if pct is None:
                continue
            row = next((m for m in meters if m["kind"] == kind), None)
            if row is None:
                row = {"kind": kind, "group": group, "label": label(kind), "window": WINDOW_S[group],
                       "severity": "", "active": False}
                meters.append(row)
            row.update(percent=round(max(0.0, pct), 1), resets_at=_epoch(w.get("resets_at")),
                       source="statusline", at=at)
    if meters and not any(m.get("active") for m in meters):
        top = next((m for m in meters if m["kind"] == "weekly_all"), None) \
            or max(meters, key=lambda m: m["percent"])
        top["active"] = True
    for m in meters:
        if m.get("resets_at") and m["resets_at"] <= now:
            m.update(percent=0.0, reset=True)
    return meters


# ─── the poller ───────────────────────────────────────────────────────────

_usage: dict[str, Any] | None = None    # {"at", "meters", "credits"}
_attempt_at = 0.0
_next_at = 0.0                          # backoff: no run before this
_failures = 0
_error: str | None = None
_running = False
_forced = False
_wake: asyncio.Event | None = None
_loaded = False


def _load_state() -> None:
    global _usage, _attempt_at, _next_at, _failures, _error, _loaded
    _loaded = True
    try:
        data = json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    if isinstance(data.get("usage"), dict):
        _usage = data["usage"]
    _attempt_at = _num(data.get("attempt_at")) or 0.0
    _next_at = _num(data.get("next_at")) or 0.0
    _failures = int(_num(data.get("failures")) or 0)
    _error = data.get("error") if isinstance(data.get("error"), str) else None


def _save_state() -> None:
    try:
        _work_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_text(_state_path(), json.dumps({
            "usage": _usage, "attempt_at": _attempt_at, "next_at": _next_at,
            "failures": _failures, "error": _error,
        }))
    except OSError:
        log.warning("plan usage: couldn't save %s", _state_path(), exc_info=True)


def _poll_s() -> float:
    return config.load().plan_usage.poll_minutes * 60.0


def _due(now: float) -> bool:
    if _running or now < _next_at or now - _attempt_at < MIN_GAP_S:
        return False
    if _forced:
        return True
    return sysstats.demanded() and now - _attempt_at >= _poll_s()


def request_refresh() -> bool:
    """The pop-up opened: check again if the reading is getting old. True
    when a check is (or already was) on its way."""
    global _forced
    if _running:
        return True
    if not config.load().plan_usage.enabled:
        return False
    now = time.time()
    if _usage and now - (_usage.get("at") or 0) < FRESH_S:
        return False
    _forced = True
    if _wake is not None:
        _wake.set()
    return _due(now)


def _claude_argv() -> list[str] | None:
    exe = shutil.which("claude")
    if not exe:
        return None
    argv = [exe, "-p", "/usage", "--output-format", "stream-json", "--verbose",
            "--no-session-persistence", "--strict-mcp-config",
            # No hooks: a plugin's SessionStart hook has no business running
            # every quarter of an hour.
            "--settings", json.dumps({"disableAllHooks": True})]
    if shutil.which("nice"):
        argv = ["nice", "-n", "10", *argv]
    return argv


def _kill(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


async def _run_claude(argv: list[str]) -> str | None:
    """stdout of the /usage run, or None if it failed or timed out (it and
    anything it started are killed)."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "TMUX", "TMUX_PANE")}
    work = _work_dir()
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        proc = await _spawn(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL, cwd=str(work), env=env,
                            start_new_session=True)
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=RUN_TIMEOUT_S)
    except asyncio.TimeoutError:
        _kill(proc)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5)
        return None
    except asyncio.CancelledError:
        _kill(proc)
        raise
    return out.decode("utf-8", "replace") if proc.returncode == 0 else None


def _fail(now: float, why: str) -> None:
    global _failures, _next_at, _error
    _failures += 1
    _error = why
    _next_at = now + min(BACKOFF_MAX_S, _poll_s() * 2 ** (_failures - 1))
    log.info("plan usage: %s (next try in %d min)", why, (_next_at - now) // 60)


async def poll_once() -> None:
    global _usage, _attempt_at, _next_at, _failures, _error, _running, _forced
    _running = True
    _forced = False
    now = time.time()
    _attempt_at = now
    try:
        argv = _claude_argv()
        if argv is None:
            _fail(now, "claude isn't on PATH")
            return
        t0 = time.monotonic()
        out = await _run_claude(argv)
        took = time.monotonic() - t0
        if out is None:
            _fail(now, "claude -p /usage failed or timed out")
            return
        report, text = parse_report(out)
        if _LIMITED_RE.search(text):
            # Claude couldn't fetch fresh figures (it showed what it had);
            # keep ours and leave the endpoint alone for a while.
            _fail(now, "the usage endpoint is rate limited")
            return
        parsed = meters_from_report(report, now) if report else None
        if parsed is None:
            _fail(now, "no plan limits in /usage (not signed in with a claude.ai plan?)")
            return
        meters, credits = parsed
        _usage = {"at": now, "meters": meters, "credits": credits}
        _failures, _error, _next_at = 0, None, 0.0
        log.info("plan usage: %s (%.1f s)",
                 ", ".join(f"{m['label']} {m['percent']:g}%" for m in meters) or "no meters", took)
    finally:
        _running = False
        _save_state()


async def run() -> None:
    """Background loop (started by the app lifespan)."""
    global _wake
    _wake = asyncio.Event()
    if not _loaded:
        _load_state()
    pruned_at = 0.0
    while True:
        try:
            now = time.time()
            if now - pruned_at > 3600:
                pruned_at = now
                await asyncio.to_thread(_prune_records, now)
            if config.load().plan_usage.enabled and _due(now):
                await poll_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("plan usage poll failed")
        _wake.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_wake.wait(), timeout=TICK_S)


# ─── snapshot ─────────────────────────────────────────────────────────────

def snapshot() -> dict[str, Any] | None:
    """``plan`` in /api/system: the merged meters, usage credits, and how
    fresh they are. None when turned off."""
    if not config.load().plan_usage.enabled:
        return None
    if not _loaded:
        _load_state()
    now = time.time()
    if _wake is not None and _due(now):
        _wake.set()                  # someone's looking and a check is due: now, not next tick
    sl = latest_statusline()
    usage_at = (_usage or {}).get("at")
    sl_at = sl[0] if sl else None
    stamps = [t for t in (usage_at, sl_at) if t]
    return {
        "meters": merge(_usage, sl, now),
        "credits": (_usage or {}).get("credits"),
        "at": max(stamps) if stamps else None,
        "usage_at": usage_at,
        "statusline_at": sl_at,
        "checking": _running,
        "error": _error,
        "now": now,
    }


_pane_keys: dict[str, tuple[float, str | None]] = {}   # session → (looked up at, record name)
_PANE_KEY_S = 15.0
_KEY_FORMAT = "#{pid} #{pane_id}"


def _key_from(line: str | None) -> str | None:
    pid, _, pane = (line or "").strip().partition(" ")
    if pid.isdigit() and re.fullmatch(r"%\d+", pane):
        return f"{pid}_{pane[1:]}.json"
    return None


async def _record_key(session: str) -> str | None:
    """The status-line record of the session's active pane."""
    hit = _pane_keys.get(session)
    if hit and time.monotonic() - hit[0] < _PANE_KEY_S:
        return hit[1]
    try:
        tmux.safe_name(session)      # it goes into a tmux command line
    except ValueError:
        return None
    target = tmux.pane_target(session)
    lines = await control_client.query(f"display-message -p -t '{target}' '{_KEY_FORMAT}'")
    key = _key_from(lines[0] if lines else None)
    if key is None:
        key = _key_from(await tmux._tmux_out("display-message", "-p", "-t", target, _KEY_FORMAT))
    if len(_pane_keys) > 256:
        _pane_keys.clear()
    _pane_keys[session] = (time.monotonic(), key)
    return key


async def session_context(session: str) -> dict[str, Any] | None:
    """How full the session's context window is, from its last status-line
    reading: {percent, window, model, at}. None before Claude's first
    reply, or in a session ccpipe didn't start Claude in."""
    if not config.load().plan_usage.enabled:
        return None
    key = await _record_key(session)
    if not key:
        return None
    rec = await asyncio.to_thread(_read_record, statusline_dir() / key)
    if not rec:
        return None
    at, data = rec
    cw = data.get("context_window") if isinstance(data.get("context_window"), dict) else {}
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    pct = _num(cw.get("used_percentage"))
    if pct is None:
        return None
    name = model.get("display_name")
    return {"percent": pct, "window": _num(cw.get("context_window_size")),
            "model": name if isinstance(name, str) else None, "at": at}
