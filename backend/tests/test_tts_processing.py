"""Tests for TtsService._process_file edge cases: truncation, missing files,
symlink rejection, partial trailing lines."""
import asyncio
import json
import os
from pathlib import Path

import pytest

from ccpipe.tts import TtsService


def _record(cwd: str, ts: str, text: str) -> str:
    return json.dumps({
        "type": "assistant",
        "cwd": cwd,
        "timestamp": ts,
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": text}]},
    }) + "\n"


async def _make_service(projects_dir: Path) -> TtsService:
    svc = TtsService(kokoro_url="http://nonexistent", projects_dir=projects_dir)
    # We skip the watchdog/observer setup; we just want _process_file.
    return svc


async def test_skips_missing_file(tmp_path):
    svc = await _make_service(tmp_path)
    svc._enabled = True
    await svc._process_file(tmp_path / "does-not-exist.jsonl")
    # No crash, no positions added.
    assert svc._positions == {}


async def test_first_visit_snapshots_eof_without_replay(tmp_path):
    f = tmp_path / "proj" / "session.jsonl"
    f.parent.mkdir()
    f.write_text(_record("/proj", "2026-01-01T00:00:00Z", "old turn"))
    svc = await _make_service(tmp_path)
    svc._enabled = True
    spoken = []
    svc._launch_speak = lambda text, rec: spoken.append((text, rec))  # type: ignore
    await svc._process_file(f)
    assert spoken == []  # historical content not replayed
    assert svc._positions[f.resolve()] == f.stat().st_size


async def test_speaks_appended_lines(tmp_path):
    f = tmp_path / "proj" / "session.jsonl"
    f.parent.mkdir()
    f.write_text("")
    svc = await _make_service(tmp_path)
    svc._enabled = True
    spoken = []
    svc._launch_speak = lambda text, rec: spoken.append((text, rec))  # type: ignore
    # First visit snapshots EOF=0.
    await svc._process_file(f)
    # Append a new turn and re-process.
    with f.open("a") as fp:
        fp.write(_record("/proj", "2026-01-02T00:00:00Z", "new turn"))
    await svc._process_file(f)
    assert len(spoken) == 1
    assert spoken[0][0] == "new turn"


async def test_partial_trailing_line_held_back(tmp_path):
    f = tmp_path / "proj" / "session.jsonl"
    f.parent.mkdir()
    f.write_text("")
    svc = await _make_service(tmp_path)
    svc._enabled = True
    spoken = []
    svc._launch_speak = lambda text, rec: spoken.append((text, rec))  # type: ignore
    await svc._process_file(f)
    # Write one complete line and a half-line (no trailing newline).
    full_line = _record("/proj", "2026-01-02T00:00:00Z", "hello")
    f.write_text(full_line + '{"partial":')
    await svc._process_file(f)
    assert [t for t, _ in spoken] == ["hello"]
    # Complete the partial line.
    with f.open("a") as fp:
        fp.write(' "yes"}\n')
    # That partial is not assistant-text so still nothing more spoken.
    await svc._process_file(f)
    assert [t for t, _ in spoken] == ["hello"]


async def test_truncated_file_resets_position(tmp_path):
    f = tmp_path / "proj" / "session.jsonl"
    f.parent.mkdir()
    # Make the initial content long so the post-truncate file is strictly
    # smaller than the snapshotted EOF; otherwise it's an "append" not a
    # "truncate" from our perspective.
    f.write_text(_record("/p", "2026-01-01T00:00:00Z", "x" * 200))
    svc = await _make_service(tmp_path)
    svc._enabled = True
    spoken = []
    svc._launch_speak = lambda text, rec: spoken.append((text, rec))  # type: ignore
    await svc._process_file(f)
    # Truncate + write a short new record (smaller than the snapshot).
    f.write_text(_record("/p", "2026-01-02T00:00:00Z", "short"))
    assert f.stat().st_size < 220  # strictly less than the snapshot we took
    await svc._process_file(f)
    assert [t for t, _ in spoken] == ["short"]


async def test_symlinks_refused(tmp_path):
    real = tmp_path / "real" / "session.jsonl"
    real.parent.mkdir()
    real.write_text(_record("/p", "2026-01-01T00:00:00Z", "should not speak"))
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    svc = await _make_service(tmp_path)
    svc._enabled = True
    spoken = []
    svc._launch_speak = lambda text, rec: spoken.append((text, rec))  # type: ignore
    await svc._process_file(link)
    assert spoken == []


async def test_concurrent_process_file_does_not_double_dispatch(tmp_path):
    """Regression for the sentence-mode duplication bug.

    Two watchdog events for the same file firing in quick succession used
    to both observe the stale position (because it was updated AFTER the
    awaited file read), so the same JSONL line was dispatched twice. The
    per-path lock serializes _process_file; only one task reads the new
    bytes, the other arrives with last == EOF and returns clean.
    """
    f = tmp_path / "proj" / "session.jsonl"
    f.parent.mkdir()
    f.write_text("")
    svc = await _make_service(tmp_path)
    svc._enabled = True
    spoken = []
    svc._launch_speak = lambda text, rec: spoken.append((text, rec))  # type: ignore
    await svc._process_file(f)  # snapshot EOF=0
    with f.open("a") as fp:
        fp.write(_record("/proj", "2026-01-02T00:00:00Z", "only once"))
    # Fire two concurrent calls for the same path — what the worker's
    # debounce window can produce when two TimerHandles overlap an
    # awaiting _read_range.
    await asyncio.gather(svc._process_file(f), svc._process_file(f))
    assert [t for t, _ in spoken] == ["only once"]


async def test_speak_task_tracked_for_cancellation(tmp_path, monkeypatch):
    """stop() must cancel outstanding speak tasks so a stalled Kokoro
    read doesn't leak past shutdown."""
    f = tmp_path / "proj" / "session.jsonl"
    f.parent.mkdir()
    f.write_text("")
    svc = await _make_service(tmp_path)
    svc._enabled = True

    started = asyncio.Event()
    blocked = asyncio.Event()

    async def fake_speak(text, record):
        started.set()
        # Park until cancelled — simulates a Kokoro request that never returns.
        try:
            await blocked.wait()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(svc, "_speak", fake_speak)

    await svc._process_file(f)  # snapshot EOF=0
    with f.open("a") as fp:
        fp.write(_record("/proj", "2026-01-02T00:00:00Z", "trigger"))
    await svc._process_file(f)

    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert len(svc._speak_tasks) == 1
    # stop() should cancel the in-flight task.
    await asyncio.wait_for(svc.stop(), timeout=2.0)
    assert svc._speak_tasks == set()


async def test_kokoro_stream_defers_start_until_first_chunk(tmp_path, monkeypatch):
    """_kokoro_stream should not emit 'start' or 'end' for a 200 response
    that produces zero audio bytes — otherwise the frontend visualiser
    flickers on for an utterance the user never hears."""
    import httpx
    svc = await _make_service(tmp_path)

    class _Resp:
        status_code = 200
        async def aread(self): return b""
        async def aiter_bytes(self):
            return
            yield  # pragma: no cover — make this a generator

    class _Stream:
        async def __aenter__(self): return _Resp()
        async def __aexit__(self, *a): return False

    class _FakeHttp:
        def stream(self, *a, **kw): return _Stream()

    svc._http = _FakeHttp()
    events = [evt async for evt in svc._kokoro_stream("hello")]
    assert events == []


async def test_kokoro_stream_emits_start_then_chunks_then_end(tmp_path):
    """Sanity check: when chunks DO arrive, 'start' precedes them and
    'end' follows. Empty chunks are filtered."""
    svc = await _make_service(tmp_path)

    class _Resp:
        status_code = 200
        async def aread(self): return b""
        async def aiter_bytes(self):
            for c in (b"", b"\x01\x02", b"", b"\x03"):
                yield c

    class _Stream:
        async def __aenter__(self): return _Resp()
        async def __aexit__(self, *a): return False

    class _FakeHttp:
        def stream(self, *a, **kw): return _Stream()

    svc._http = _FakeHttp()
    events = [evt async for evt in svc._kokoro_stream("hi")]
    assert events[0] == ("start", "hi")
    assert events[-1] == ("end", None)
    chunks = [p for k, p in events if k == "chunk"]
    assert chunks == [b"\x01\x02", b"\x03"]


async def test_per_path_lock_dropped_on_forget(tmp_path):
    """Forgetting a path (file deleted) drops both the position and the
    lock so we don't slowly leak Lock objects as users churn sessions."""
    f = tmp_path / "proj" / "session.jsonl"
    f.parent.mkdir()
    f.write_text("")
    svc = await _make_service(tmp_path)
    svc._enabled = True
    svc._loop = asyncio.get_running_loop()
    await svc._process_file(f)
    resolved = f.resolve()
    assert resolved in svc._positions
    # Touch a lock entry by going through process_file once with content.
    with f.open("a") as fp:
        fp.write(_record("/proj", "2026-01-02T00:00:00Z", "x"))
    svc._launch_speak = lambda text, rec: None  # type: ignore
    await svc._process_file(f)
    assert resolved in svc._locks
    svc._forget_threadsafe(resolved)
    # call_soon_threadsafe runs at next loop tick.
    await asyncio.sleep(0)
    assert resolved not in svc._positions
    assert resolved not in svc._locks
