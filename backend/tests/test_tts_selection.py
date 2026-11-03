"""What gets read aloud: which transcript records count as Claude's reply,
and how its markdown is turned into something a voice can say. The record
shapes are copied from real Claude Code transcripts (2.1.3xx)."""
import json

import pytest

from ccpipe import config as app_config
from ccpipe.tts import TtsService, _extract_assistant_text, prepare_for_tts, speakable

SID = "cac3f913-1bad-4ad4-9675-a6f97ef070d0"


def _reply(text: str, stop_reason: str | None = "end_turn", **extra) -> dict:
    return {
        "type": "assistant", "isSidechain": False, "sessionId": SID,
        "timestamp": "2026-11-03T10:00:00Z", "cwd": "/proj",
        "message": {"id": "msg_01", "role": "assistant", "model": "claude-opus-5-5",
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": stop_reason},
        **extra,
    }


# ─── Which records are Claude's reply ─────────────────────────────────────

def test_final_reply_is_spoken():
    assert _extract_assistant_text(_reply("All done.")) == "All done."


@pytest.mark.parametrize("operation", ["enqueue", "remove", "popAll"])
def test_prompt_typed_while_busy_is_not_spoken(operation):
    # The "it reads what I send to Claude" bug: a queued prompt.
    rec = {"type": "queue-operation", "operation": operation, "sessionId": SID,
           "timestamp": "2026-11-03T10:00:00Z",
           "content": "I can now see that work mode is off. Why isn't it on?"}
    assert _extract_assistant_text(rec, progress=True) is None


def test_request_dump_is_not_spoken():
    rec = {"type": "api-request-blob", "sessionId": SID, "hash": "4a76",
           "message": {"role": "assistant", "content": "An old turn, sent again."}}
    assert _extract_assistant_text(rec, progress=True) is None


def test_subagent_is_not_spoken():
    rec = _reply("Now step 1 and the check/test.", isSidechain=True)
    assert _extract_assistant_text(rec, progress=True) is None


def test_made_up_reply_is_not_spoken_but_errors_are():
    synth = _reply("No response requested.", isApiErrorMessage=False)
    synth["message"]["model"] = "<synthetic>"
    assert _extract_assistant_text(synth) is None
    err = _reply("API Error: 529 Overloaded.", isApiErrorMessage=True)
    err["message"]["model"] = "<synthetic>"
    assert _extract_assistant_text(err) == "API Error: 529 Overloaded."


def test_progress_notes_only_when_asked():
    note = _reply("I'll start by loading the analysis skill.", stop_reason="tool_use")
    assert _extract_assistant_text(note) is None
    assert _extract_assistant_text(note, progress=True) == "I'll start by loading the analysis skill."


def test_unknown_stop_reason_without_a_tool_call_is_spoken():
    # Some models (seen with GLM) leave stop_reason null.
    assert _extract_assistant_text(_reply("Finished.", stop_reason=None)) == "Finished."


def test_user_prompt_is_not_spoken():
    rec = {"type": "user", "sessionId": SID,
           "message": {"role": "user", "content": "now add it to the healthcheck"}}
    assert _extract_assistant_text(rec, progress=True) is None


# ─── Markdown → speech ────────────────────────────────────────────────────

@pytest.mark.parametrize("text, spoken", [
    ("This is **important** now.", "This is important now."),
    ("Use __init__ and ~~old~~ new.", "Use init and old new."),
    ("An *emphasised* word.", "An emphasised word."),
    ("Edit `tts.py` today.", "Edit tts.py today."),
    ("See backend/ccpipe/tts.py:156 for it.", "See tts.py for it."),
    ("Logs in ~/.local/state/ccpipe and /run/user/1000.", "Logs in ccpipe and 1000."),
    ("Read [the docs](https://example.com/a/b) first.", "Read the docs first."),
    ("It's at https://www.github.com/x/y?q=1 now.", "It's at github.com now."),
    ("Load → render ⇒ show -> done.", "Load to render to show to done."),
    ("It's ~40% faster.", "It's about 40% faster."),
    # Left alone: not markup, not a path.
    ("Read and/or write 3/4 of TCP/IP.", "Read and/or write 3/4 of TCP/IP."),
    ("snake_case_name stays, 2 * 3 stays, *.py stays.", "snake_case_name stays, 2 * 3 stays, *.py stays."),
])
def test_speakable_inline(text, spoken):
    assert speakable(text) == spoken


def test_speakable_lines():
    text = ("## Summary\n\n"
            "- first item\n"
            "* second item.\n"
            "1. Run the tests\n\n"
            "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
            "Want me to keep it?")
    assert speakable(text) == ("Summary.\n\n"
                               "first item.\n"
                               "second item.\n"
                               "1. Run the tests.\n\n"
                               "\n\n\n\n"
                               "Want me to keep it?")


def test_trailing_table_does_not_become_the_last_paragraph():
    text = "Here are the numbers.\n\n| run | ms |\n|---|---|\n| 1 | 37 |"
    assert prepare_for_tts(text, "last_paragraph") == "Here are the numbers."


def test_last_paragraph_of_a_markdown_reply():
    text = ("I changed **two** files.\n\n"
            "```python\nx = 1\n```\n\n"
            "The fix is in `backend/ccpipe/tts.py` → **deployed**.")
    assert prepare_for_tts(text, "last_paragraph") == "The fix is in tts.py to deployed."


# ─── End to end through the transcript tail ───────────────────────────────

@pytest.fixture
def tts_config(tmp_path, monkeypatch):
    """A private config file, so these never read the live one."""
    path = tmp_path / "config.json"
    monkeypatch.setenv(app_config.CONFIG_FILE_ENV, str(path))
    app_config.reset_cache()

    def write(**tts):
        path.write_text(json.dumps({"tts": {"scope": "full", **tts}}))
        app_config.reset_cache()
    write()
    yield write
    app_config.reset_cache()


async def _tail(tmp_path, records: list[dict]) -> list[str]:
    f = tmp_path / "proj" / "session.jsonl"
    f.parent.mkdir(exist_ok=True)
    f.write_text("")
    svc = TtsService(kokoro_url="http://nonexistent", projects_dir=tmp_path)
    spoken: list[str] = []
    svc._launch_speak = lambda text, rec: spoken.append(text)  # type: ignore[method-assign]
    await svc._process_file(f)
    with f.open("a") as fp:
        for r in records:
            fp.write(json.dumps(r) + "\n")
    await svc._process_file(f)
    return spoken


_TURN = [
    {"type": "user", "sessionId": SID, "message": {"role": "user", "content": "fix the test"}},
    _reply("I'll look at the test first.", stop_reason="tool_use"),
    {"type": "queue-operation", "operation": "enqueue", "sessionId": SID,
     "timestamp": "2026-11-03T10:00:01Z", "content": "and check the docs"},
    _reply("Now the docs.", stop_reason="tool_use"),
    {"type": "queue-operation", "operation": "remove", "sessionId": SID,
     "timestamp": "2026-11-03T10:00:02Z", "content": "and check the docs"},
    _reply("Fixed: the **test** passes."),
    {"type": "system", "subtype": "turn_duration", "sessionId": SID},
]


async def test_a_turn_speaks_only_its_reply(tmp_path, tts_config):
    assert await _tail(tmp_path, _TURN) == ["Fixed: the test passes."]


async def test_a_turn_with_progress_notes(tmp_path, tts_config):
    tts_config(progress=True)
    assert await _tail(tmp_path, _TURN) == [
        "I'll look at the test first.", "Now the docs.", "Fixed: the test passes."]


def test_progress_setting_round_trips():
    cfg = app_config.AppConfig.from_dict({"tts": {"progress": True}})
    assert cfg.tts.progress is True
    assert app_config.AppConfig.from_dict(cfg.to_dict()).tts.progress is True
    assert app_config.AppConfig.from_dict({"tts": {"progress": "yes"}}).tts.progress is False
    assert app_config.AppConfig().tts.progress is False


# ─── Each sentence knows the reply it belongs to ──────────────────────────

async def test_sentences_carry_the_whole_reply_and_its_id():
    svc = TtsService(kokoro_url="http://nonexistent", projects_dir=__import__("pathlib").Path("/tmp"))
    svc._http = object()  # type: ignore[assignment]  # only checked for None

    async def fake_stream(text):
        yield ("start", text)
        yield ("chunk", b"mp3")
        yield ("end", None)
    svc._kokoro_stream = fake_stream  # type: ignore[method-assign]
    starts: list[tuple[str, str, int]] = []

    async def on_start(sentence, utterance, uid):
        starts.append((sentence, utterance, uid))

    async def nothing(*_a):
        return None
    svc.subscribe(on_start=on_start, on_chunk=nothing, on_end=nothing)
    await svc._speak("First one here. Second one here.", {})
    await svc._speak("Only sentence.", {})
    assert starts == [
        ("First one here.", "First one here. Second one here.", 1),
        ("Second one here.", "First one here. Second one here.", 1),
        ("Only sentence.", "Only sentence.", 2),
    ]
