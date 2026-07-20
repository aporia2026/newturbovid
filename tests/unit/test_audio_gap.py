"""Tests for the audio-gap util (``pipeline.audio_gap``).

Covers the load-bearing behaviour the google-simple-motion 3s pause relies on:
  - the spliced WAV's duration = sum(segments) + gap * (n-1), exactly
  - the middle really is silence (zero PCM), so ZapCap gets a clean split
  - RMS loudness-match scales a quieter later segment UP toward the first
  - a single segment produces no gap
  - mismatched formats / empty input raise
  - the output is a valid RIFF/WAVE readable by stdlib ``wave``
"""

from __future__ import annotations

import io
import math
import wave
from array import array

import pytest

from bulkvid.pipeline.audio_gap import join_with_silence

_RATE = 24_000
_CHANNELS = 1
_WIDTH = 2


def _wav(samples: list[int], *, rate: int = _RATE, channels: int = _CHANNELS) -> bytes:
    """Build a 16-bit PCM WAV from int16 samples."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(_WIDTH)
        wf.setframerate(rate)
        wf.writeframes(array("h", samples).tobytes())
    return buf.getvalue()


def _tone(n_frames: int, amplitude: int) -> list[int]:
    """A simple sine-ish tone at a given peak amplitude."""
    return [
        int(amplitude * math.sin(2 * math.pi * 220 * i / _RATE))
        for i in range(n_frames)
    ]


def _read_samples(wav_bytes: bytes) -> array:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        a = array("h")
        a.frombytes(wf.readframes(wf.getnframes()))
        return a


def test_duration_is_segments_plus_gap() -> None:
    # 1.0s + 3.0s gap + 0.5s = 4.5s exactly.
    seg1 = _wav(_tone(_RATE, 10_000))            # 1.0s
    seg2 = _wav(_tone(_RATE // 2, 10_000))       # 0.5s
    wav, duration = join_with_silence([seg1, seg2], 3.0)
    assert duration == pytest.approx(4.5, abs=1e-6)
    # And the produced WAV agrees with the returned duration.
    with wave.open(io.BytesIO(wav), "rb") as wf:
        assert wf.getnframes() / wf.getframerate() == pytest.approx(4.5, abs=1e-6)


def test_middle_is_real_silence() -> None:
    seg = _wav(_tone(_RATE, 12_000))             # 1.0s of tone
    wav, _ = join_with_silence([seg, seg], 3.0, match_loudness=False)
    samples = _read_samples(wav)
    # frames: [0, 24000) tone | [24000, 96000) silence | [96000, 120000) tone
    gap = samples[_RATE : _RATE + 3 * _RATE]
    assert len(gap) == 3 * _RATE
    assert set(gap) == {0}, "the 3s gap must be pure silence"
    # The tone regions are not silent.
    assert any(s != 0 for s in samples[:_RATE])
    assert any(s != 0 for s in samples[-_RATE:])


def test_loudness_match_scales_quiet_segment_up() -> None:
    loud = _wav(_tone(_RATE, 20_000))
    quiet = _wav(_tone(_RATE, 4_000))            # ~5x quieter
    wav, _ = join_with_silence([loud, quiet], 0.0, match_loudness=True)
    samples = _read_samples(wav)
    first = samples[:_RATE]
    second = samples[_RATE:]
    peak_first = max(abs(s) for s in first)
    peak_second = max(abs(s) for s in second)
    # The quiet segment should have been scaled up toward the loud one (not exactly
    # equal — clamped/rounded — but clearly louder than its original 4000 peak).
    assert peak_second > 3 * 4_000
    assert peak_second <= peak_first + 1


def test_loudness_match_off_leaves_segment_untouched() -> None:
    loud = _wav(_tone(_RATE, 20_000))
    quiet_samples = _tone(_RATE, 4_000)
    quiet = _wav(quiet_samples)
    wav, _ = join_with_silence([loud, quiet], 0.0, match_loudness=False)
    samples = _read_samples(wav)
    second = samples[_RATE:]
    assert max(abs(s) for s in second) == pytest.approx(
        max(abs(s) for s in quiet_samples), abs=1
    )


def test_single_segment_has_no_gap() -> None:
    seg = _wav(_tone(_RATE, 10_000))
    _wav_out, duration = join_with_silence([seg], 3.0)
    assert duration == pytest.approx(1.0, abs=1e-6)


def test_mismatched_sample_rate_raises() -> None:
    a = _wav(_tone(_RATE, 10_000))
    b = _wav(_tone(16_000, 10_000), rate=16_000)
    with pytest.raises(ValueError, match="mismatched audio formats"):
        join_with_silence([a, b], 3.0)


def test_empty_input_raises() -> None:
    with pytest.raises(ValueError, match="at least one"):
        join_with_silence([], 3.0)


def test_output_is_valid_wav() -> None:
    seg = _wav(_tone(_RATE, 10_000))
    wav, _ = join_with_silence([seg, seg], 3.0)
    with wave.open(io.BytesIO(wav), "rb") as wf:
        assert wf.getframerate() == _RATE
        assert wf.getnchannels() == _CHANNELS
        assert wf.getsampwidth() == _WIDTH
