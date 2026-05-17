import pytest

from ccpipe.tmux import safe_name


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
