import asyncio

import pytest

from ccpipe.tmux_control import TmuxControlClient, TmuxEvent


async def _collect_events(client: TmuxControlClient, lines: list[str]) -> list[TmuxEvent]:
    received: list[TmuxEvent] = []

    async def cb(event: TmuxEvent) -> None:
        received.append(event)

    sub = client.subscribe(cb)
    try:
        for line in lines:
            await client._handle_line(line)
    finally:
        sub.cancel()
    return received


async def test_forwards_sessions_changed():
    client = TmuxControlClient()
    events = await _collect_events(client, ["%sessions-changed"])
    assert len(events) == 1
    assert events[0].name == "sessions-changed"
    assert events[0].args == []


async def test_forwards_window_renamed_with_args():
    client = TmuxControlClient()
    events = await _collect_events(client, ["%window-renamed @5 my-window"])
    assert len(events) == 1
    assert events[0].name == "window-renamed"
    assert events[0].args == ["@5", "my-window"]


async def test_ignores_command_response_lines():
    client = TmuxControlClient()
    lines = [
        "%begin 0 1 0",
        "session-data",
        "more-data",
        "%end 0 1 0",
    ]
    events = await _collect_events(client, lines)
    assert events == []


async def test_ignores_non_percent_lines():
    client = TmuxControlClient()
    events = await _collect_events(client, ["foo bar", ""])
    assert events == []


async def test_subscribers_isolated_on_exception():
    client = TmuxControlClient()
    called: list[str] = []

    async def good(e: TmuxEvent) -> None:
        called.append(e.name)

    async def bad(e: TmuxEvent) -> None:
        raise RuntimeError("boom")

    sub1 = client.subscribe(bad)
    sub2 = client.subscribe(good)
    try:
        await client._handle_line("%sessions-changed")
    finally:
        sub1.cancel()
        sub2.cancel()
    assert called == ["sessions-changed"]


async def test_subscription_cancel():
    client = TmuxControlClient()
    received: list[TmuxEvent] = []

    async def cb(e: TmuxEvent) -> None:
        received.append(e)

    sub = client.subscribe(cb)
    await client._handle_line("%sessions-changed")
    sub.cancel()
    await client._handle_line("%sessions-changed")
    assert len(received) == 1


async def test_double_cancel_is_safe():
    client = TmuxControlClient()
    sub = client.subscribe(lambda e: asyncio.sleep(0))  # type: ignore[arg-type]
    sub.cancel()
    sub.cancel()  # should not raise
