"""Pages on an older build than the one served get no speech: on the iPad
such pages lived on hidden after their reload and fought the live page for
the one audio output (2026-11-03)."""
import json

from ccpipe import ws


def _conn(build: str):
    c = ws._TerminalConnection.__new__(ws._TerminalConnection)
    c.build = build
    return c


def test_outdated_pages(tmp_path, monkeypatch):
    (tmp_path / "version.json").write_text(json.dumps({"build": "5478df8 · 2026-11-03 21:20Z"}))
    monkeypatch.setenv("CCPIPE_FRONTEND_DIST", str(tmp_path))
    monkeypatch.setattr(ws, "_build_cache", (-1.0, None))
    assert ws._current_build() == "5478df8"
    assert not _conn("5478df8").outdated()
    assert _conn("b36d90e").outdated()
    assert not _conn("").outdated()          # no build given: not judged


def test_no_version_file_judges_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("CCPIPE_FRONTEND_DIST", str(tmp_path))
    monkeypatch.setattr(ws, "_build_cache", (-1.0, None))
    assert ws._current_build() is None
    assert not _conn("b36d90e").outdated()
