"""Text-to-speech service.

Tails Claude Code's per-session JSONL transcripts at
``~/.claude/projects/<hash>/<session>.jsonl``, extracts new assistant text
as it's appended, and streams audio for each utterance from a Kokoro-FastAPI
endpoint to subscribers.

Each subscription supplies a ``content_filter`` that receives the parsed
JSONL record. ccpipe uses this to scope TTS to the cwd of the claude process
running in the browser's attached tmux session, and to suppress records
from before the WS subscription started. See ``ws.py::_build_tts_filter``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import config as app_config

# Bound the in-memory bookkeeping for files-seen so a host with thousands
# of historical claude sessions doesn't leak. JSONL transcripts are
# append-only and watchdog rarely fires "deleted" events for them, so
# eviction-by-delete alone (in _forget_threadsafe) isn't enough.
_LRU_CAP = 5000

# Bound the watchdog → worker queue so a stalled Kokoro + fast writes
# can't grow it unbounded.
_QUEUE_MAX = 1000

log = logging.getLogger(__name__)

ChunkCallback = Callable[[bytes], Awaitable[None]]
TurnCallback = Callable[[str], Awaitable[None]]   # text of the utterance just started
EndCallback = Callable[[], Awaitable[None]]
ContentFilter = Callable[[dict[str, Any]], bool]


@dataclass
class _Subscription:
    on_start: TurnCallback
    on_chunk: ChunkCallback
    on_end: EndCallback
    service: "TtsService"
    content_filter: ContentFilter | None = None
    # Per-WS mute state, mirrored from the browser via the `tts_mute`
    # text frame. When every subscriber that *accepts* an utterance is
    # muted, _speak() short-circuits — saving the Kokoro round-trip and
    # the audio bytes we'd otherwise stream straight into a client that
    # was just going to drop them.
    muted: bool = False

    def accepts(self, record: dict[str, Any]) -> bool:
        return self.content_filter is None or self.content_filter(record)

    def cancel(self) -> None:
        try:
            self.service._subscribers.remove(self)
        except ValueError:
            pass


# ─── Speech preparation: strip code, narrow scope ─────────────────────────


_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
# Sentence boundary: punctuation preceded by at least 3 word characters,
# followed by whitespace. Requiring 3+ chars before the punctuation rules
# out abbreviations like "e.g." (1 char), "Mr." (2 chars), and decimals
# like "3.14" — without requiring the next sentence to start with an
# uppercase letter, which was previously preventing lowercase prose
# ("then we …", "i was thinking …") from splitting and starving the
# depth-2 Kokoro pipeline of pre-fetchable sentences.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=\w{3}[.!?])\s+")
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n+")


def _strip_code_blocks(text: str) -> str:
    """Remove fenced ```...``` blocks. Spoken code is unintelligible."""
    return _CODE_BLOCK_RE.sub("", text)


def _last_paragraph(text: str) -> str:
    paras = [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(text) if p.strip()]
    return paras[-1] if paras else ""


def split_sentences(text: str) -> list[str]:
    """Split prepared TTS text into sentences. Public so the chunked
    pipelining in _speak can use the same split as the scope filter."""
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]


def _last_sentence(text: str) -> str:
    sentences = split_sentences(text)
    return sentences[-1] if sentences else ""


def _apply_scope(text: str, scope: str) -> str:
    """Narrow the assistant turn to just the portion the user wants spoken.

    scope values (see ccpipe.config.VALID_SCOPES):
      - full           : whole response
      - last_paragraph : final block of non-empty lines
      - last_sentence  : final sentence
      - last_question  : final sentence if it ends in '?', else last paragraph
      - off            : speak nothing
    """
    text = text.strip()
    if not text or scope == "off":
        return ""
    if scope == "full":
        return text
    if scope == "last_paragraph":
        return _last_paragraph(text) or text
    if scope == "last_sentence":
        return _last_sentence(text) or text
    if scope == "last_question":
        last = _last_sentence(text)
        if last and last.rstrip().endswith("?"):
            return last
        return _last_paragraph(text) or text
    return text  # unknown scope → speak everything


def prepare_for_tts(text: str, scope: str) -> str:
    """Public for tests. Strips code blocks then applies scope filtering."""
    return _apply_scope(_strip_code_blocks(text), scope).strip()


def _extract_assistant_text(line_obj: dict[str, Any]) -> str | None:
    """Pull human-speakable text out of one Claude Code transcript line.

    The transcript schema has shifted a few times; we handle the common
    shapes: top-level ``role``, nested ``message.role``, ``type``-tagged
    records. Returns None for non-assistant lines or messages with no
    speakable text (e.g. pure tool-use turns).
    """
    if line_obj.get("type") not in (None, "assistant") and line_obj.get("role") != "assistant":
        if line_obj.get("type") in ("user", "system", "tool_result", "tool_use", "summary"):
            return None
    msg = line_obj.get("message") if isinstance(line_obj.get("message"), dict) else line_obj
    if msg.get("role") and msg.get("role") != "assistant":
        return None
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        joined = "".join(parts).strip()
        return joined or None
    return None


class _Handler(FileSystemEventHandler):
    """Watchdog handler that bridges fs events into the asyncio loop."""

    def __init__(self, on_change: Callable[[Path], None],
                 on_delete: Callable[[Path], None]) -> None:
        super().__init__()
        self._on_change = on_change
        self._on_delete = on_delete

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._on_change(Path(event.src_path))

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._on_change(Path(event.src_path))

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._on_delete(Path(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        # Source is gone (rotated away); drop its tracked position.
        if not event.is_directory:
            self._on_delete(Path(event.src_path))


@dataclass
class TtsService:
    kokoro_url: str
    projects_dir: Path
    voice: str = "af_bella"
    model: str = "kokoro"
    response_format: str = "mp3"

    # OrderedDict-backed LRU so a long-running operator doesn't leak one
    # entry per claude session ever started. Cap at LRU_CAP entries;
    # least-recently-touched evicted on insert. JSONL transcripts are
    # append-only and rarely deleted, so we can't rely on _forget alone.
    _positions: "OrderedDict[Path, int]" = field(default_factory=OrderedDict)
    # Per-path locks serialize _process_file so two concurrent watchdog
    # events for the same file can't read the same byte range twice.
    _locks: "OrderedDict[Path, asyncio.Lock]" = field(default_factory=OrderedDict)
    _subscribers: list[_Subscription] = field(default_factory=list)
    # Cap so a stalled Kokoro + a fast-writing claude session can't grow
    # the queue unbounded. 1000 events is comfortably more than any
    # legitimate burst, and overflow drops the new event (next watchdog
    # tick re-enqueues anyway).
    _queue: asyncio.Queue[Path] | None = None
    _observer: Observer | None = None
    _worker_task: asyncio.Task[None] | None = None
    # Set when start() fires before ~/.claude/projects exists; polls
    # for the dir appearing and finishes setup. Cancelled by stop().
    _setup_retry_task: asyncio.Task[None] | None = None
    # Debounce timers per path — pulled out of _worker's local so stop()
    # can cancel them before the worker task is cancelled. Without this,
    # a call_later scheduled before stop() can fire after shutdown and
    # call create_task on a torn-down loop.
    _debounce_handles: dict[Path, asyncio.TimerHandle] = field(default_factory=dict)
    # Outstanding per-utterance speak tasks. Tracked so stop() can cancel
    # them rather than leaving Kokoro reads dangling on shutdown.
    _speak_tasks: set[asyncio.Task] = field(default_factory=set)
    _loop: asyncio.AbstractEventLoop | None = None
    _http: httpx.AsyncClient | None = None
    _enabled: bool = True
    # Set in stop() so any in-flight watchdog event that fires AFTER we
    # tear down the loop becomes a no-op rather than calling
    # call_soon_threadsafe on a closed loop (which raises RuntimeError).
    # observer.join(timeout=…) is best-effort and can return with the
    # thread still alive on a slow filesystem, so we need this guard.
    _stopped: bool = False

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        # Short connect timeout means Kokoro outages bounce fast; the read
        # window stays generous for long utterances. Per-utterance speaks
        # run as detached tasks so a stalled one doesn't block the worker.
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2.0, read=60.0, write=10.0, pool=2.0),
        )

        # If the projects dir doesn't exist yet (first-run user who
        # installs claude later), defer setting up the watcher. The
        # retry task polls the parent dir every 30s and finishes
        # setup as soon as ~/.claude/projects appears. Previously
        # `service idle` was permanent until next ccpipe restart.
        if not self.projects_dir.exists():
            log.warning("TTS projects dir %s missing; will retry every 30s",
                        self.projects_dir)
            self._setup_retry_task = asyncio.create_task(
                self._wait_for_projects_dir(), name="tts-projects-retry")
            return

        self._finish_start()

    async def _wait_for_projects_dir(self) -> None:
        """Poll for the projects dir appearing and finish initialising
        the watcher when it does. Quietly bails on stop(). The 30 s
        cadence is fine — TTS is operator-facing and the user can
        always /reload the service if they're impatient."""
        while not self._stopped:
            try:
                await asyncio.sleep(30.0)
            except asyncio.CancelledError:
                return
            if self._stopped:
                return
            if self.projects_dir.exists():
                log.info("TTS projects dir %s now present; resuming setup",
                         self.projects_dir)
                self._finish_start()
                return

    def _finish_start(self) -> None:
        """Watcher setup that depends on projects_dir existing. Split
        from start() so the retry path can call it later."""
        # Snapshot current EOF for existing files so we don't replay history.
        for p in self.projects_dir.rglob("*.jsonl"):
            try:
                self._positions[p] = p.stat().st_size
            except OSError:
                continue

        self._observer = Observer()
        self._observer.schedule(
            _Handler(self._enqueue_threadsafe, self._forget_threadsafe),
            str(self.projects_dir), recursive=True)
        self._observer.start()
        self._worker_task = asyncio.create_task(self._worker(), name="tts-worker")
        log.info("TTS watching %s; %d existing files snapshotted at EOF",
                 self.projects_dir, len(self._positions))

    async def stop(self) -> None:
        # Flip the flag FIRST so any watchdog thread still alive after our
        # join (timeout below is best-effort) drops its events instead of
        # poking call_soon_threadsafe on a torn-down loop.
        self._stopped = True
        # Cancel the projects-dir retry poller if start() armed it.
        if self._setup_retry_task is not None:
            self._setup_retry_task.cancel()
            try:
                await self._setup_retry_task
            except (asyncio.CancelledError, Exception):
                pass
            self._setup_retry_task = None
        # Cancel debounce timers BEFORE the worker — if any timer's
        # deadline has not yet fired its create_task lambda would
        # otherwise run AFTER stop() completes, against an already-
        # closed HTTP client.
        for handle in list(self._debounce_handles.values()):
            handle.cancel()
        self._debounce_handles.clear()
        if self._observer is not None:
            self._observer.stop()
            # join() is a blocking thread join; run it in a worker so
            # it doesn't stall uvicorn's graceful shutdown timer.
            await asyncio.to_thread(self._observer.join, 2.0)
            self._observer = None
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
            self._worker_task = None
        # Cancel in-flight per-utterance speak tasks so their Kokoro reads
        # don't keep the HTTP client alive past aclose().
        speak_tasks = list(self._speak_tasks)
        for t in speak_tasks:
            t.cancel()
        for t in speak_tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._speak_tasks.clear()
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def subscribe(self, *, on_start: TurnCallback, on_chunk: ChunkCallback,
                  on_end: EndCallback,
                  content_filter: ContentFilter | None = None) -> _Subscription:
        sub = _Subscription(
            on_start=on_start, on_chunk=on_chunk, on_end=on_end,
            service=self, content_filter=content_filter,
        )
        self._subscribers.append(sub)
        return sub

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _enqueue_threadsafe(self, path: Path) -> None:
        if self._stopped or self._loop is None or self._queue is None:
            return
        if path.suffix != ".jsonl":
            return
        try:
            self._loop.call_soon_threadsafe(self._put_path, path)
        except RuntimeError:
            # Loop closed between our flag check and the scheduling call;
            # harmless during shutdown.
            pass

    def _put_path(self, path: Path) -> None:
        """call_soon_threadsafe target that drops events on overflow
        rather than raising. The worker debounces by 100ms anyway, so a
        burst of writes coalesces; if the queue is genuinely full the
        next watchdog tick will re-enqueue the latest position."""
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(path)
        except asyncio.QueueFull:
            log.warning("tts queue full (%d); dropping event for %s",
                         self._queue.qsize(), path)

    def _forget_threadsafe(self, path: Path) -> None:
        """Drop the tracked position for a vanished file so _positions
        doesn't grow unbounded as users come and go from claude sessions."""
        if self._stopped or self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._positions.pop, path, None)
            # Drop the lock too — keeping it around would slowly leak memory
            # as users churn through sessions.
            self._loop.call_soon_threadsafe(self._locks.pop, path, None)
        except RuntimeError:
            pass

    async def _worker(self) -> None:
        assert self._queue is not None

        while True:
            path = await self._queue.get()
            if self._stopped:
                return
            prev = self._debounce_handles.pop(path, None)
            if prev is not None:
                prev.cancel()
            handle = asyncio.get_running_loop().call_later(
                0.1, lambda p=path: self._fire_debounced(p))
            self._debounce_handles[path] = handle

    def _fire_debounced(self, path: Path) -> None:
        """call_later target. Skips the work if stop() already ran —
        avoids creating tasks against a torn-down HTTP client when the
        deadline lands just past our shutdown."""
        self._debounce_handles.pop(path, None)
        if self._stopped:
            return
        asyncio.create_task(self._process_path(path))

    async def _process_path(self, path: Path) -> None:
        try:
            await self._process_file(path)
        except Exception:
            log.exception("tts process failed for %s", path)

    async def _process_file(self, path: Path) -> None:
        if not self._enabled:
            return
        # Defense in depth: refuse symlinks anywhere along the path
        # (not just the leaf) and check that the resolved path is still
        # inside the projects dir. A symlink in an ancestor that points
        # outside the projects tree would slip past the original leaf-
        # only check: e.g. /home/x/.claude/projects/proj-x is a symlink
        # to /tmp/loot and /tmp/loot/session.jsonl is an attacker-
        # writable file — resolved would land inside the canonical
        # projects dir via relative_to() only if the resolve happens
        # to come back inside it (it doesn't here, but the leaf-only
        # check would have allowed the path if it did).
        try:
            for ancestor in (path, *path.parents):
                if ancestor == self.projects_dir or ancestor == self.projects_dir.parent:
                    break
                if ancestor.is_symlink():
                    log.warning("tts: refusing path with symlinked ancestor %s", ancestor)
                    return
            resolved = path.resolve()
            resolved.relative_to(self.projects_dir.resolve())
        except (OSError, ValueError):
            return
        path = resolved

        # Serialize per-path. Two watchdog events firing close together
        # would otherwise both read the position, both kick off an async
        # _read_range, and both dispatch the same lines — audible as TTS
        # repeating sentences. The lock keeps reads-and-position-updates
        # atomic for a given file.
        lock = self._locks.get(path)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[path] = lock
            self._evict_lru()
        else:
            self._locks.move_to_end(path)
        async with lock:
            await self._process_file_locked(path)

    def _evict_lru(self) -> None:
        """Trim _positions and _locks to LRU_CAP. Called whenever we
        insert a new path so the dicts can't grow without bound."""
        while len(self._positions) > _LRU_CAP:
            self._positions.popitem(last=False)
        while len(self._locks) > _LRU_CAP:
            self._locks.popitem(last=False)

    async def _process_file_locked(self, path: Path) -> None:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return

        # Newly-discovered file: snapshot to EOF rather than reading from 0.
        # That avoids replaying historical lines from a JSONL that was created
        # while the service was already running (e.g. a fresh claude session
        # that flushes its system prompt + first turn before we attach).
        if path not in self._positions:
            self._positions[path] = stat.st_size
            self._evict_lru()
            return

        # Move-to-end on each access so the LRU eviction naturally
        # preserves the actively-tailed files and drops the stale ones.
        self._positions.move_to_end(path)
        last = self._positions[path]
        if stat.st_size < last:
            last = 0  # file truncated/rotated
        if stat.st_size == last:
            return

        new_bytes = await asyncio.to_thread(_read_range, path, last, stat.st_size)
        self._positions[path] = stat.st_size

        end = new_bytes.rfind(b"\n")
        if end == -1:
            # Partial trailing line; rewind position and wait.
            self._positions[path] = last
            return
        complete = new_bytes[: end + 1]
        partial_len = len(new_bytes) - (end + 1)
        if partial_len > 0:
            self._positions[path] = stat.st_size - partial_len

        # Scope is constant for a batch of lines — load it once.
        scope = app_config.load().tts.scope

        for raw_line in complete.splitlines():
            if not raw_line.strip():
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            text = _extract_assistant_text(obj)
            if text:
                spoken = prepare_for_tts(text, scope)
                log.info("tts: assistant turn detected "
                         "(%d raw → %d spoken chars, scope=%s, %d subs, cwd=%s)",
                         len(text), len(spoken), scope,
                         len(self._subscribers), obj.get("cwd"))
                if spoken:
                    self._launch_speak(spoken, obj)

    def _launch_speak(self, text: str, record: dict[str, Any]) -> None:
        """Spawn _speak as a detached task so the file-watcher worker
        keeps draining its queue even if Kokoro stalls. The handle is
        tracked so stop() can cancel outstanding utterances.

        Capped at 50 outstanding tasks — past that, Kokoro is clearly
        not draining and queuing more would just consume memory until
        eventual OOM. Dropping is the only sane response; the user
        will hear the next utterance after Kokoro recovers.
        """
        if len(self._speak_tasks) >= 50:
            log.warning("tts: %d speak tasks pending, dropping new utterance",
                         len(self._speak_tasks))
            return
        task = asyncio.create_task(self._speak(text, record),
                                    name=f"tts-speak-{record.get('uuid', '?')[:8]}")
        self._speak_tasks.add(task)
        task.add_done_callback(self._speak_tasks.discard)

    async def _speak(self, text: str, record: dict[str, Any]) -> None:
        if not self._enabled or not self._subscribers or self._http is None:
            log.info("tts: _speak skipped (enabled=%s subs=%d http=%s)",
                     self._enabled, len(self._subscribers), self._http is not None)
            return
        # Snapshot once so a subscriber cancelling mid-utterance (e.g.
        # WS drops, on_chunk's send fails and triggers tts_sub.cancel)
        # can't mutate the list we're iterating below.
        subs = list(self._subscribers)
        targets = [s for s in subs if s.accepts(record)]
        if not targets:
            log.info("tts: filter rejected utterance (cwd=%s subs=%d)",
                     record.get("cwd"), len(self._subscribers))
            return
        # If every accepting subscriber is muted, skip the Kokoro call
        # entirely — synthesising MP3 just to stream it into a player
        # that drops every chunk is pure waste. If even one subscriber
        # is listening we still synthesise once (Kokoro work is shared
        # across subscribers anyway).
        if all(s.muted for s in targets):
            log.info("tts: all %d subscribers muted; skipping kokoro for cwd=%s",
                     len(targets), record.get("cwd"))
            return
        # Also drop the muted subscribers from the dispatch set so we
        # don't burn WS frames on them even when at least one peer is
        # active. The browser would silently discard the audio either
        # way; this just saves bandwidth on mobile uplinks.
        targets = [s for s in targets if not s.muted]

        sentences = split_sentences(text)
        if not sentences:
            return

        if len(sentences) == 1:
            log.info("tts: speaking utterance (single sentence, %d targets) → kokoro",
                     len(targets))
            await self._speak_one(sentences[0], targets)
            return

        log.info("tts: speaking utterance (%d sentences, %d targets, pipelined) → kokoro",
                 len(sentences), len(targets))
        await self._speak_pipelined(sentences, targets)

    async def _speak_one(self, text: str, targets: list[_Subscription]) -> None:
        """Single Kokoro round-trip — used when there's only one sentence."""
        async for evt in self._kokoro_stream(text):
            kind, payload = evt
            if kind == "start":
                for sub in targets:
                    try: await sub.on_start(text)
                    except Exception: log.exception("on_start failed")
            elif kind == "chunk":
                for sub in targets:
                    try: await sub.on_chunk(payload)
                    except Exception: log.exception("on_chunk failed")
            elif kind == "end":
                for sub in targets:
                    try: await sub.on_end()
                    except Exception: log.exception("on_end failed")

    async def _speak_pipelined(self, sentences: list[str],
                                targets: list[_Subscription]) -> None:
        """Sentence-chunked TTS with depth-2 pipelining.

        Each sentence becomes its own Kokoro request and its own
        start/chunk/end cycle on the frontend (the TtsPlayer queues them
        and plays in order). While sentence N is being streamed and
        played, sentence N+1's Kokoro request is already in flight,
        which reduces the time-to-first-audio to roughly one sentence's
        generation cost rather than the whole response's.

        Strict in-order delivery: we don't forward chunks from sentence
        N+1 until sentence N's stream has fully drained.
        """
        DEPTH = 2
        EOS: object = object()

        async def producer(sentence: str, q: asyncio.Queue):
            """Drain Kokoro's stream for one sentence into the queue.
            Always finishes with an EOS sentinel so the consumer knows
            when to move on."""
            try:
                async for evt in self._kokoro_stream(sentence):
                    await q.put(evt)
            except Exception:
                log.exception("tts: pipeline producer failed")
            finally:
                await q.put(EOS)

        # Prime the pipeline with the first DEPTH sentences' requests.
        in_flight: list[tuple[asyncio.Queue, asyncio.Task, str]] = []
        for s in sentences[:DEPTH]:
            q: asyncio.Queue = asyncio.Queue(maxsize=16)
            t = asyncio.create_task(producer(s, q))
            in_flight.append((q, t, s))

        next_idx = DEPTH

        while in_flight:
            q, task, sentence = in_flight.pop(0)
            sent_start = False
            try:
                while True:
                    evt = await q.get()
                    if evt is EOS:
                        break
                    kind, payload = evt
                    if kind == "start":
                        # We mediate start/end at the pipeline level so each
                        # sentence appears as its own utterance to the client.
                        # Now deferred: _kokoro_stream only emits 'start' once
                        # it has at least one chunk, so sent_start tracking
                        # here is purely defensive against a duplicate event.
                        if not sent_start:
                            for sub in targets:
                                try: await sub.on_start(sentence)
                                except Exception: log.exception("on_start failed")
                            sent_start = True
                    elif kind == "chunk":
                        for sub in targets:
                            try: await sub.on_chunk(payload)
                            except Exception: log.exception("on_chunk failed")
                    elif kind == "end":
                        # We'll fire on_end below after EOS, so we know we've
                        # actually exhausted the queue.
                        pass
            finally:
                # Only emit on_end if we actually emitted on_start — otherwise
                # the client sees an end event for a sentence it never knew
                # had started, which corrupts its visualiser state.
                if sent_start:
                    for sub in targets:
                        try: await sub.on_end()
                        except Exception: log.exception("on_end failed")
                # Reap the producer task cleanly. Cancel-then-gather so if
                # we ourselves are being cancelled (outer task teardown),
                # the producer is told to stop AND its result is reaped —
                # without the cancel, the producer would keep streaming
                # from Kokoro into a queue nobody will ever read.
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

            # Slot in the next sentence to keep depth-2 pipelining alive.
            if next_idx < len(sentences):
                s = sentences[next_idx]
                q2: asyncio.Queue = asyncio.Queue(maxsize=16)
                t2 = asyncio.create_task(producer(s, q2))
                in_flight.append((q2, t2, s))
                next_idx += 1

    async def _kokoro_stream(self, text: str):
        """Open one Kokoro request, yield events as a single sentence's
        text is converted. Events: ('start', text), ('chunk', bytes),
        ('end', None).

        'start' is deferred until the FIRST non-empty chunk arrives. That
        way a Kokoro request that 200's but produces no audio (rare but
        possible: server-side timeout, bad voice id, immediate stream
        truncation) doesn't trigger a phantom start/end on the client —
        the visualiser would briefly flicker on with no sound to show.
        On total failure (transport error, non-200, or zero chunks) no
        events are yielded.
        """
        if self._http is None:
            return
        cfg = app_config.load().tts
        url = self.kokoro_url.rstrip("/") + "/v1/audio/speech"
        body = {
            "model": self.model,
            "input": text,
            "voice": cfg.voice,
            "speed": cfg.speech_rate,
            "response_format": self.response_format,
        }
        try:
            async with self._http.stream("POST", url, json=body) as resp:
                if resp.status_code != 200:
                    err_body = await resp.aread()
                    log.warning("kokoro returned %d: %s",
                                resp.status_code, err_body[:200])
                    return
                started = False
                # Defense: cap how much audio we'll relay per utterance.
                # A misbehaving Kokoro endpoint (or one pointed at an
                # attacker during a misconfig) could otherwise stream
                # unbounded bytes through to every TTS subscriber, OOM-
                # ing the backend and clients. 32 MiB is ~5-10 minutes
                # of MP3 at typical bitrates — well above any reasonable
                # single-utterance bound.
                bytes_yielded = 0
                _MAX_UTTERANCE_BYTES = 32 * 1024 * 1024
                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue
                    bytes_yielded += len(chunk)
                    if bytes_yielded > _MAX_UTTERANCE_BYTES:
                        log.warning("kokoro stream exceeded %d B for one "
                                    "utterance; truncating",
                                    _MAX_UTTERANCE_BYTES)
                        break
                    if not started:
                        yield ("start", text)
                        started = True
                    yield ("chunk", chunk)
                if started:
                    yield ("end", None)
        except (httpx.HTTPError, OSError) as exc:
            log.warning("kokoro request failed: %s", exc)


def _read_range(path: Path, start: int, end: int) -> bytes:
    with path.open("rb") as f:
        f.seek(start)
        return f.read(end - start)


# Module-level singleton, configured by main.py's lifespan.
tts_service: TtsService = TtsService(
    kokoro_url=os.environ.get("CCPIPE_KOKORO_URL", "http://localhost:8880"),
    projects_dir=Path(os.environ.get("CCPIPE_CLAUDE_PROJECTS",
                                      str(Path.home() / ".claude" / "projects"))),
    voice=os.environ.get("CCPIPE_TTS_VOICE", "af_bella"),
)
