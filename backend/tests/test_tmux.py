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
async def test_apply_server_defaults_anchors_server_before_options(monkeypatch):
    # Regression: on a cold boot apply_server_defaults() runs before any
    # tmux session exists. A session-less server exits immediately, so the
    # -g options must be set on a server kept alive by a real session —
    # otherwise sticky-restore spawns a fresh, unconfigured server and the
    # sessions come up at tmux defaults (alternate-screen ON / history 2000),
    # breaking scrollback. So a persistent anchor `new-session` must be the
    # FIRST command, before any set-option.
    from ccpipe.tmux_control import CONTROL_SESSION_NAME
    calls: list[tuple[str, ...]] = []

    async def fake_run_tmux(*args: str, capture: bool = True) -> tuple[int, str]:
        calls.append(args)
        return 0, ""

    monkeypatch.setattr(tmux_setup, "_run_tmux", fake_run_tmux)
    await tmux_setup.apply_server_defaults()

    assert calls, "expected tmux commands to be issued"
    # First command creates the long-lived anchor session.
    assert calls[0][0] == "new-session", (
        f"a persistent anchor new-session must be first, got {calls[0]}"
    )
    assert CONTROL_SESSION_NAME in calls[0]
    assert "sleep" in calls[0] and "infinity" in calls[0]
    # Every set-option / set-window-option must come AFTER the anchor.
    first_set = next(
        i for i, c in enumerate(calls)
        if c and c[0] in ("set-option", "set-window-option")
    )
    assert first_set > 0
    # The scrollback-critical defaults are actually issued.
    flat = {c[2] for c in calls if c[0] in ("set-option", "set-window-option") and len(c) >= 3}
    assert {"history-limit", "alternate-screen", "window-size"} <= flat
