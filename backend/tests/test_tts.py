import json
from pathlib import Path

from ccpipe.tts import TtsService, _extract_assistant_text


def test_extracts_text_from_message_content_list():
    line = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello world"}],
        },
    }
    assert _extract_assistant_text(line) == "Hello world"


def test_concatenates_multiple_text_blocks():
    line = {
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Part one. "},
                {"type": "tool_use", "name": "Read"},
                {"type": "text", "text": "Part two."},
            ],
        },
    }
    assert _extract_assistant_text(line) == "Part one. Part two."


def test_returns_none_for_user_role():
    line = {"type": "user", "message": {"role": "user", "content": "hi"}}
    assert _extract_assistant_text(line) is None


def test_returns_none_for_tool_only_turn():
    line = {
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Read", "input": {}}],
        },
    }
    assert _extract_assistant_text(line) is None


def test_handles_string_content():
    line = {"role": "assistant", "content": "Just a string"}
    assert _extract_assistant_text(line) == "Just a string"


def test_returns_none_for_empty_content():
    assert _extract_assistant_text({"role": "assistant", "content": ""}) is None
    assert _extract_assistant_text({"role": "assistant", "content": []}) is None


def test_skips_summary_records():
    assert _extract_assistant_text({"type": "summary", "summary": "x"}) is None


def _noop_subscribe_kwargs():
    return {
        "on_start": lambda t: None,   # type: ignore[arg-type, return-value]
        "on_chunk": lambda c: None,   # type: ignore[arg-type, return-value]
        "on_end": lambda: None,       # type: ignore[arg-type, return-value]
    }


def test_subscription_content_filter_accepts_matching_cwd():
    svc = TtsService(kokoro_url="x", projects_dir=Path("/tmp"))
    sub = svc.subscribe(
        **_noop_subscribe_kwargs(),
        content_filter=lambda r: r.get("cwd") == "/home/u/foo",
    )
    try:
        assert sub.accepts({"cwd": "/home/u/foo"}) is True
        assert sub.accepts({"cwd": "/home/u/bar"}) is False
        assert sub.accepts({}) is False
    finally:
        sub.cancel()


def test_subscription_content_filter_default_accepts_all():
    svc = TtsService(kokoro_url="x", projects_dir=Path("/tmp"))
    sub = svc.subscribe(**_noop_subscribe_kwargs())
    try:
        assert sub.accepts({"anything": True}) is True
        assert sub.accepts({}) is True
    finally:
        sub.cancel()


def test_subscription_content_filter_can_gate_on_timestamp():
    svc = TtsService(kokoro_url="x", projects_dir=Path("/tmp"))
    cutoff = "2026-05-16T12:00:00Z"
    sub = svc.subscribe(
        **_noop_subscribe_kwargs(),
        content_filter=lambda r: r.get("timestamp", "") >= cutoff,
    )
    try:
        assert sub.accepts({"timestamp": "2026-05-16T11:59:59Z"}) is False
        assert sub.accepts({"timestamp": "2026-05-16T12:00:00Z"}) is True
        assert sub.accepts({"timestamp": "2026-05-16T12:00:01Z"}) is True
    finally:
        sub.cancel()


def test_real_world_jsonl_line():
    """A representative full JSONL line as Claude Code writes it."""
    raw = json.dumps({
        "parentUuid": "abc",
        "isSidechain": False,
        "userType": "external",
        "cwd": "/home/x",
        "sessionId": "session-123",
        "version": "2.1.69",
        "type": "assistant",
        "message": {
            "id": "msg_01",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-4-7",
            "content": [{"type": "text", "text": "Sure, I'll do that."}],
            "stop_reason": "end_turn",
        },
        "uuid": "u1",
        "timestamp": "2026-05-16T12:00:00Z",
    })
    obj = json.loads(raw)
    assert _extract_assistant_text(obj) == "Sure, I'll do that."
