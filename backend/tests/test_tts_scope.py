"""Tests for the TTS scope filter and code-block stripping."""
from ccpipe.tts import (
    _apply_scope,
    _last_paragraph,
    _last_sentence,
    _strip_code_blocks,
    prepare_for_tts,
    split_sentences,
)


# ─── Code-block stripping ─────────────────────────────────────────────────

def test_strip_inline_code_block():
    # The two spaces around ``` collapse adjacent — the fence is gone,
    # whitespace around it isn't collapsed.
    assert _strip_code_blocks("hello ```code``` world") == "hello  world"


def test_strip_multiline_code_block():
    text = "before\n```python\nx = 1\n```\nafter"
    out = _strip_code_blocks(text)
    assert "x = 1" not in out
    assert "before" in out and "after" in out


def test_strip_multiple_code_blocks():
    text = "A ```one``` B ```two``` C"
    out = _strip_code_blocks(text)
    assert "one" not in out and "two" not in out
    assert "A" in out and "B" in out and "C" in out


def test_unclosed_code_block_left_alone():
    # An unterminated fence shouldn't eat everything to EOF.
    text = "before ```code without closing"
    assert _strip_code_blocks(text) == text


def test_keeps_inline_single_backticks():
    text = "use `foo` here"
    assert _strip_code_blocks(text) == text


# ─── Last paragraph ───────────────────────────────────────────────────────

def test_last_paragraph_single_para_returns_self():
    assert _last_paragraph("Only one paragraph.") == "Only one paragraph."


def test_last_paragraph_with_blank_separator():
    text = "First paragraph here.\n\nSecond bit.\n\nFinal paragraph that is the ask."
    assert _last_paragraph(text) == "Final paragraph that is the ask."


def test_last_paragraph_strips_trailing_blanks():
    text = "First.\n\nSecond.\n\n\n"
    assert _last_paragraph(text) == "Second."


# ─── Last sentence ────────────────────────────────────────────────────────

def test_last_sentence_period():
    assert _last_sentence("First. Second. Final sentence.") == "Final sentence."


def test_last_sentence_question():
    assert _last_sentence("I did the thing. Want me to continue?") == "Want me to continue?"


def test_last_sentence_ignores_abbreviations():
    # "e.g." followed by lowercase should not be a split point.
    text = "This is fine e.g. lower-case continuation. The final sentence."
    assert _last_sentence(text) == "The final sentence."


def test_last_sentence_single_sentence_returns_self():
    assert _last_sentence("Just one sentence.") == "Just one sentence."


# ─── Scope dispatch ───────────────────────────────────────────────────────

def test_scope_full():
    text = "Para one.\n\nPara two ending with question?"
    assert _apply_scope(text, "full") == text.strip()


def test_scope_last_paragraph():
    text = "Para one.\n\nPara two ending with question?"
    assert _apply_scope(text, "last_paragraph") == "Para two ending with question?"


def test_scope_last_sentence():
    text = "Long explanation here. The closing question?"
    assert _apply_scope(text, "last_sentence") == "The closing question?"


def test_scope_last_question_returns_question_when_present():
    text = "Made some changes.\n\nWant me to continue?"
    assert _apply_scope(text, "last_question") == "Want me to continue?"


def test_scope_last_question_falls_back_to_paragraph():
    text = "First paragraph.\n\nFinal paragraph without a trailing question."
    assert _apply_scope(text, "last_question") == "Final paragraph without a trailing question."


def test_scope_off_returns_empty():
    assert _apply_scope("anything at all", "off") == ""


def test_scope_unknown_passes_through():
    text = "anything"
    assert _apply_scope(text, "bogus") == "anything"


# ─── Pipeline ─────────────────────────────────────────────────────────────

def test_pipeline_strips_code_then_narrows():
    text = (
        "Let me show you something.\n\n"
        "```python\nx = 1\nprint(x)\n```\n\n"
        "Want me to also run it?"
    )
    out = prepare_for_tts(text, "last_question")
    assert out == "Want me to also run it?"


# ─── Sentence splitting (used by the pipelined TTS path) ─────────────────

def test_split_sentences_basic():
    s = split_sentences("First sentence. Second sentence. Third one.")
    assert s == ["First sentence.", "Second sentence.", "Third one."]


def test_split_sentences_keeps_terminators():
    s = split_sentences("Hello world. Is anyone there? Yes!")
    assert s == ["Hello world.", "Is anyone there?", "Yes!"]


def test_split_sentences_respects_abbreviations():
    # "e.g." with lowercase continuation must not break the sentence apart.
    s = split_sentences("This is fine e.g. lower-case continuation. Next one.")
    assert s == [
        "This is fine e.g. lower-case continuation.",
        "Next one.",
    ]


def test_split_sentences_empty():
    assert split_sentences("") == []
    assert split_sentences("   \n  ") == []


def test_split_sentences_single():
    assert split_sentences("Just one.") == ["Just one."]


def test_pipeline_strips_code_full_mode_preserves_remainder():
    text = "Here is the change ```diff\n-old\n+new\n``` done."
    out = prepare_for_tts(text, "full")
    assert "-old" not in out and "+new" not in out
    assert "Here is the change" in out and "done." in out
