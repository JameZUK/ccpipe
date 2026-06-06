import pytest

from ccpipe.tmux import safe_name
from ccpipe import tmux_setup


def test_safe_name_accepts_simple():
    assert safe_name("work") == "work"
    assert safe_name("project-1") == "project-1"
    assert safe_name("dev_env") == "dev_env"


def test_safe_name_rejects_shell_metachars():
    for bad in [
        "", "with space", "tab\there", "new\nline",
        "has.dot", "colon:thing", "quote'name", 'double"name',
        "back\\slash", "dollar$", "back`tick",
        "semi;colon", "pipe|x", "amp&", "redir<", "redir>",
        "paren(x)", "brace{x}", "bracket[x]",
        "star*", "qmark?", "hash#",
    ]:
        with pytest.raises(ValueError):
            safe_name(bad)


def test_safe_name_rejects_leading_dash():
    # tmux would otherwise interpret these as flags (-V, -L, etc.)
    for bad in ["-V", "-Lfoo", "-t", "--help", "-"]:
        with pytest.raises(ValueError):
            safe_name(bad)


@pytest.mark.asyncio
async def test_apply_server_defaults_starts_server_before_options(monkeypatch):
    # Regression: on a cold boot apply_server_defaults() runs before any
    # tmux server exists, so it must `start-server` FIRST or every
    # set-option is lost against a missing socket (silently reverting
    # alternate-screen->on / history-limit->2000 and breaking scrollback).
    calls: list[tuple[str, ...]] = []

    async def fake_run_tmux(*args: str) -> tuple[int, str]:
        calls.append(args)
        return 0, ""

    monkeypatch.setattr(tmux_setup, "_run_tmux", fake_run_tmux)
    await tmux_setup.apply_server_defaults()

    assert calls, "expected tmux commands to be issued"
    assert calls[0] == ("start-server",), (
        f"start-server must be the first command, got {calls[0]}"
    )
    # Every set-option / set-window-option must come AFTER start-server.
    first_set = next(
        i for i, c in enumerate(calls)
        if c and c[0] in ("set-option", "set-window-option")
    )
    assert first_set > 0
    # The scrollback-critical defaults are actually issued.
    flat = {c[2] for c in calls if c[0] in ("set-option", "set-window-option") and len(c) >= 3}
    assert {"history-limit", "alternate-screen", "window-size"} <= flat
