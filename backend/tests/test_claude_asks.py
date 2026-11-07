"""Claude stopping to ask: reading its dialog off the screen. The screens
are real captures (Claude Code 2.1.317, 2026-11-03), with the working
directory replaced. Noticing the wait is test_attention.py."""
from ccpipe.claude_asks import Ask, describe

RULE = "─" * 80
DASH = "╌" * 80

QUESTION_SCREEN = f"""\
 ▐▛███▛█   Claude Code v2.1.317
▝▜██████▀  Haiku 5.5 · Claude Ultra
 ▝▝   ▝▝   /home/dev/proj


❯ Use the AskUserQuestion tool to ask me which colour I prefer, red or blue. Do nothing else.
{RULE}
 ☐ Colour

Which colour do you prefer, red or blue?

❯ 1. Red
     Choose red
  2. Blue
     Choose blue
  3. Type something.
{RULE}
  4. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
""".split("\n")

PERMISSION_SCREEN = f"""\
❯ Run this exact Bash command: rm hello.txt

● Deleting hello.txt from the working directory
  ⎿  $ rm hello.txt

{RULE}
 Bash command
 Tip: auto mode handles these prompts for you — choose "switch to auto mode" below
 Delete hello.txt from the working directory
{DASH}
 rm hello.txt
{DASH}
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and always allow access to /home/dev/proj from
      this project
   3. Yes, and switch to auto mode · auto mode handles these prompts for you
   4. No

 Esc to cancel · Tab to amend
""".split("\n")


def test_question():
    ask = describe(QUESTION_SCREEN, "input needed")
    assert ask == Ask("question", "Claude has a question",
                      "Which colour do you prefer, red or blue? Red, or Blue.")
    assert ask.spoken == "Claude asks: Which colour do you prefer, red or blue? Red, or Blue."


def test_permission_prompt_says_what_for():
    ask = describe(PERMISSION_SCREEN, "permission prompt")
    assert ask == Ask("permission", "Claude needs your permission",
                      "Bash command: Delete hello.txt from the working directory.")
    assert ask.spoken.startswith("Claude needs your permission. Bash command: Delete")


def test_permission_prompt_with_its_own_question():
    screen = [RULE, " Edit file", " backend/ccpipe/tts.py", DASH, "  1 -old", "  1 +new", DASH,
              " Do you want to make this edit to tts.py?", " ❯ 1. Yes", "   2. No"]
    assert describe(screen, "permission prompt").body == "Do you want to make this edit to tts.py?"


def test_several_questions_and_a_preview_column():
    screen = [RULE, " ←  ☐ Approach  ☐ Model  ✔ Submit  →", "",
              "How should I pick the new", "announcement model?", "",
              "❯ 1. Bake-off first (Recommended)      │ Runs 3 candidates side by side",
              "     I create the agent…                │",
              "  2. Just use Sonnet                    │",
              "  3. Type something.", RULE, "  4. Chat about this"]
    ask = describe(screen, "input needed")
    assert ask.title == "Claude has 2 questions"
    assert ask.body == ("How should I pick the new announcement model? "
                        "Bake-off first (Recommended), or Just use Sonnet.")
    assert ask.spoken.startswith("Claude has 2 questions. The first: How should")


def test_a_numbered_list_above_the_dialog_is_not_the_question():
    screen = ["● Two ways:", "  1. Rebuild", "  2. Patch", ""] + QUESTION_SCREEN
    assert describe(screen, "input needed").body.startswith("Which colour")


def test_question_in_a_frame():
    # As drawn in this repo's own session (2.1.316): the question has a "│" bar.
    screen = [RULE, "←  ☐ Since reopen  ☐ Ever worked  ✔ Submit  →", "",
              "│ Since you reopened ccpipe on the iPad, have you heard anything at all, including this question?",
              "", "❯ 1. Heard this one", "     This question was read out.", "  2. Heard it twice",
              "  3. Nothing at all", "  4. Type something.", RULE, "  5. Chat about this"]
    assert describe(screen, "input needed").spoken == (
        "Claude has 2 questions. The first: Since you reopened ccpipe on the iPad, have you heard "
        "anything at all, including this question? Heard this one, Heard it twice, or Nothing at all.")


MULTI_CHOICE = [RULE, " ☐ Repeat test", "",
                "│ Tick some. Did you hear this more than once?", "",
                "❯ 1. [ ] Heard it once", "         Read out a single time.",
                "  2. [✔] Apple", "  3. [ ] Type something", "     Submit", RULE, "  4. Chat about this"]
REVIEW = [RULE, "←  ☒ Repeat test  ✔ Submit  →", "", "Review your answers", "",
          " │ ● Tick some. Did you hear this more than once?", "   → Apple", "",
          "Ready to submit your answers?", "", "❯ 1. Submit answers", "  2. Cancel"]


def test_multiple_choice_labels_lose_their_tick_boxes():
    assert describe(MULTI_CHOICE, "input needed").body == (
        "Tick some. Did you hear this more than once? Heard it once, or Apple.")


def test_the_review_screen_is_the_same_dialog():
    # Seen live: ticking options moved the dialog to its review, which was
    # announced as a second question.
    assert describe(REVIEW, "input needed").key == describe(MULTI_CHOICE, "input needed").key


def test_no_dialog_found():
    assert describe(["just text"], "input needed") == Ask("question", "Claude has a question", "")
    ask = describe([], "permission prompt")
    assert ask.kind == "permission" and ask.spoken == "Claude needs your permission."
