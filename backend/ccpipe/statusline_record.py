"""Claude Code's status-line command in the sessions ccpipe starts
(plan_usage.py passes it with ``claude --settings``).

Claude runs this after each reply, and on a few other events, with the
session's status as JSON on stdin: the model, how full the context window
is, and, for claude.ai subscribers, ``rate_limits`` (how much of the plan's
5-hour and weekly limits is used, and when each resets). This:

1. saves that JSON as ``<dir>/<tmux server pid>_<pane number>.json``, where
   ccpipe reads it for the plan-usage readout and each session's context
   fill; and
2. runs the status-line command you set up yourself (the first
   ``statusLine`` in the project's ``.claude/settings.local.json``, its
   ``.claude/settings.json``, then ``~/.claude/settings.json``, the order
   Claude reads them in), with the same input, and passes its output
   through. What Claude shows doesn't change; with no command of your own
   it prints nothing, and Claude draws no status row.

Run as ``python -I statusline_record.py <dir>``: standard library only, no
ccpipe imports, and nothing from the session's working directory (where
Claude starts it) on ``sys.path``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

MAX_INPUT = 1 << 20
USER_TIMEOUT_S = 10
# Set while your own command runs, so a settings file that names this
# recorder can't make it call itself.
_DEPTH_ENV = "CCPIPE_STATUSLINE_RECORDING"
_PANE_RE = re.compile(r"%(\d+)")


def record_name(env: dict[str, str]) -> str | None:
    """``<server pid>_<pane>.json`` from tmux's $TMUX ("socket,pid,session")
    and $TMUX_PANE ("%12"). The server pid tells a pane of this tmux server
    from a reused pane number after tmux restarts. None outside tmux."""
    parts = env.get("TMUX", "").split(",")
    m = _PANE_RE.fullmatch(env.get("TMUX_PANE", ""))
    if len(parts) < 2 or not parts[1].isdigit() or not m:
        return None
    return f"{parts[1]}_{m.group(1)}.json"


def save(out_dir: str, name: str, data: bytes) -> None:
    try:
        os.makedirs(out_dir, mode=0o700, exist_ok=True)
        tmp = os.path.join(out_dir, f".{name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
        os.replace(tmp, os.path.join(out_dir, name))
    except OSError:
        pass


def _settings_files(info: dict, env: dict[str, str]) -> list[str]:
    ws = info.get("workspace") if isinstance(info.get("workspace"), dict) else {}
    project = ws.get("project_dir") or ws.get("current_dir") or info.get("cwd")
    conf = env.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    files = []
    if isinstance(project, str) and project:
        files += [os.path.join(project, ".claude", "settings.local.json"),
                  os.path.join(project, ".claude", "settings.json")]
    files.append(os.path.join(conf, "settings.json"))
    return files


def own_command(info: dict, env: dict[str, str]) -> str | None:
    """The status-line command you configured, or None."""
    for path in _settings_files(info, env):
        try:
            with open(path, encoding="utf-8") as f:
                settings = json.load(f)
        except (OSError, ValueError):
            continue
        sl = settings.get("statusLine") if isinstance(settings, dict) else None
        if not isinstance(sl, dict):
            continue
        cmd = sl.get("command")
        if sl.get("type", "command") == "command" and isinstance(cmd, str) and cmd.strip():
            return None if os.path.basename(__file__) in cmd else cmd
    return None


def main(argv: list[str]) -> int:
    data = sys.stdin.buffer.read(MAX_INPUT + 1)
    if len(argv) > 1 and len(data) <= MAX_INPUT:
        name = record_name(dict(os.environ))
        if name:
            save(argv[1], name, data)
    if os.environ.get(_DEPTH_ENV):
        return 0
    try:
        info = json.loads(data)
    except ValueError:
        info = {}
    cmd = own_command(info if isinstance(info, dict) else {}, dict(os.environ))
    if not cmd:
        return 0
    sys.stdout.flush()
    try:
        done = subprocess.run(["/bin/sh", "-c", cmd], input=data, timeout=USER_TIMEOUT_S,
                              env={**os.environ, _DEPTH_ENV: "1"})
    except (OSError, subprocess.TimeoutExpired):
        return 1
    return done.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv))
