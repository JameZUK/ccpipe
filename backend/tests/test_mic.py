import os
from pathlib import Path

from ccpipe.mic import MicWriter


def test_write_returns_false_when_pipe_missing(tmp_path: Path):
    w = MicWriter(str(tmp_path / "nonexistent.pipe"))
    assert w.write(b"data") is False
    assert "not found" in w.diagnostic()


def test_write_succeeds_when_reader_present(tmp_path: Path):
    pipe_path = tmp_path / "mic.pipe"
    os.mkfifo(pipe_path)

    # Open the reader first so the writer's O_WRONLY|O_NONBLOCK doesn't ENXIO.
    reader_fd = os.open(pipe_path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        w = MicWriter(str(pipe_path))
        # Allow first open attempt regardless of debounce timer.
        w._last_open_attempt = 0
        assert w.write(b"hello") is True
        data = os.read(reader_fd, 1024)
        assert data == b"hello"
        w.close()
    finally:
        os.close(reader_fd)


def test_write_returns_false_when_no_reader(tmp_path: Path):
    pipe_path = tmp_path / "mic.pipe"
    os.mkfifo(pipe_path)
    w = MicWriter(str(pipe_path))
    w._last_open_attempt = 0
    # ENXIO on open with no reader → write fails.
    assert w.write(b"data") is False
    assert "no reader" in w.diagnostic().lower() or "pipe" in w.diagnostic().lower()
