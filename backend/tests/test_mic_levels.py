"""The mic_stop journal line's level summary (mic._Levels)."""
import math
from array import array

from ccpipe.mic import _Levels


def pcm(amplitude: int, n: int) -> bytes:
    return array("h", [int(amplitude * math.sin(i / 3)) for i in range(n)]).tobytes()


def test_speech_floor_and_peak():
    lv = _Levels()
    lv.add(pcm(100, 16000))         # 1 s of near-silence
    lv.add(pcm(8000, 16000))        # 1 s of speech-level tone
    speech, floor, peak = (int(p.split("=")[1]) for p in lv.summary().split()[1:4])
    assert -16 <= speech <= -10     # 8000 ≈ -12 dBFS peak, ~-15 RMS
    assert floor < -45
    assert -13 <= peak <= -11


def test_odd_byte_chunks_and_partial_blocks():
    lv = _Levels()
    data = pcm(4000, 4000)
    for i in range(0, len(data), 333):      # odd-sized frames
        lv.add(data[i:i + 333])
    assert len(lv.blocks) == 2              # 4000 samples = 2 full 100 ms blocks
    assert lv.summary().startswith("level speech=")


def test_nothing_recorded():
    assert _Levels().summary() == "level n/a"
