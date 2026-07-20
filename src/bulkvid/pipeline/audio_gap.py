"""Audio-gap util — splice TTS segments with a fixed silent gap into one WAV.

The ``google-simple-motion`` tab speaks a two-sentence fixed script with a
deliberate 3-second silence between the sentences. Gemini 2.5 TTS VOCALIZES any
markup we prepend (2026-07-06 incident), so an SSML ``<break>`` or a ``[pause]``
token is out — the model would read it aloud. Instead we synthesize each sentence
separately and splice a run of zero-PCM silence between them here, producing ONE
WAV whose duration is exact and whose silence is real (so ZapCap, which captions
off the transcribed audio, gets a clean split between the two caption blocks).

Two separate Gemini synth calls can differ in loudness; ``join_with_silence``
RMS-matches the later segments to the first (clamped; near-silent segments left
alone) so the seam across the gap isn't an audible volume jump.

Pure + stdlib-only (``wave`` / ``array``) — no numpy, no ``audioop`` (removed in
Python 3.13; we target 3.12+). Endianness is handled explicitly so 16-bit PCM
round-trips correctly on any host. Plan
``_plans/2026-07-20-google-simple-motion-tab.md``.
"""

from __future__ import annotations

import io
import sys
import wave
from array import array
from dataclasses import dataclass

from bulkvid.logging import get_logger

_log = get_logger("audiogap")

# Gain clamp: a large factor on a near-silent or clipped segment would amplify
# noise / hard-clip, so the loudness match is bounded to a sane band.
_MIN_GAIN = 0.25
_MAX_GAIN = 4.0
# Segments quieter than this RMS are treated as effectively silent and left
# untouched (scaling silence just amplifies hiss).
_SILENCE_RMS_FLOOR = 1.0

_INT16_MAX = 32767
_INT16_MIN = -32768
_NEEDS_SWAP = sys.byteorder == "big"


@dataclass(frozen=True)
class _WavPcm:
    pcm: bytes
    sample_rate: int
    channels: int
    sample_width: int          # bytes per sample


def _read_wav(wav_bytes: bytes) -> _WavPcm:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        return _WavPcm(
            pcm=wf.readframes(wf.getnframes()),
            sample_rate=wf.getframerate(),
            channels=wf.getnchannels(),
            sample_width=wf.getsampwidth(),
        )


def _wrap_pcm_to_wav(pcm: bytes, fmt: _WavPcm) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(fmt.channels)
        wf.setsampwidth(fmt.sample_width)
        wf.setframerate(fmt.sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _pcm_duration_seconds(pcm: bytes, fmt: _WavPcm) -> float:
    bytes_per_second = fmt.sample_rate * fmt.channels * fmt.sample_width
    if bytes_per_second == 0:
        return 0.0
    return len(pcm) / bytes_per_second


def _pcm_to_samples(pcm: bytes) -> array:
    a = array("h")
    a.frombytes(pcm)
    if _NEEDS_SWAP:                 # WAV PCM is little-endian; array is native
        a.byteswap()
    return a


def _samples_to_pcm(samples: array) -> bytes:
    if _NEEDS_SWAP:
        swapped = array("h", samples)
        swapped.byteswap()
        return swapped.tobytes()
    return samples.tobytes()


def _rms(samples: array) -> float:
    n = len(samples)
    if n == 0:
        return 0.0
    total = 0.0
    for s in samples:
        total += float(s) * float(s)
    return (total / n) ** 0.5


def _scale(samples: array, gain: float) -> array:
    out = array("h")
    for s in samples:
        v = int(s * gain)
        if v > _INT16_MAX:
            v = _INT16_MAX
        elif v < _INT16_MIN:
            v = _INT16_MIN
        out.append(v)
    return out


def _silence_bytes(gap_seconds: float, fmt: _WavPcm) -> bytes:
    frames = round(max(0.0, gap_seconds) * fmt.sample_rate)
    return b"\x00" * (frames * fmt.channels * fmt.sample_width)


def join_with_silence(
    wav_segments: list[bytes],
    gap_seconds: float,
    *,
    match_loudness: bool = True,
) -> tuple[bytes, float]:
    """Concatenate WAV segments with ``gap_seconds`` of silence between each.

    Returns ``(wav_bytes, duration_seconds)``. The output format matches the first
    segment (all segments must share format — our TTS always emits 24kHz mono
    16-bit). With ``match_loudness`` (default), segments after the first are scaled
    so their RMS matches the first segment's, within a clamped gain band, so two
    separate Gemini synth calls don't jump in volume across the gap.
    """
    if not wav_segments:
        raise ValueError("join_with_silence requires at least one WAV segment")

    segs = [_read_wav(w) for w in wav_segments]
    base = segs[0]
    if base.sample_width != 2:
        raise ValueError("join_with_silence supports 16-bit PCM only")
    for s in segs[1:]:
        if (s.sample_rate, s.channels, s.sample_width) != (
            base.sample_rate, base.channels, base.sample_width
        ):
            raise ValueError(
                "join_with_silence: segments have mismatched audio formats "
                f"({s.sample_rate}/{s.channels}/{s.sample_width} vs "
                f"{base.sample_rate}/{base.channels}/{base.sample_width})"
            )

    ref_rms = _rms(_pcm_to_samples(base.pcm)) if match_loudness else 0.0
    gap = _silence_bytes(gap_seconds, base)

    pieces: list[bytes] = []
    for i, seg in enumerate(segs):
        pcm = seg.pcm
        if match_loudness and i > 0 and ref_rms >= _SILENCE_RMS_FLOOR:
            samples = _pcm_to_samples(pcm)
            seg_rms = _rms(samples)
            if seg_rms >= _SILENCE_RMS_FLOOR:
                gain = max(_MIN_GAIN, min(_MAX_GAIN, ref_rms / seg_rms))
                if abs(gain - 1.0) > 0.01:
                    pcm = _samples_to_pcm(_scale(samples, gain))
                    _log.info(
                        "audio_gap_loudness_matched",
                        segment=i + 1,
                        gain=round(gain, 3),
                    )
        pieces.append(pcm)
        if i < len(segs) - 1:
            pieces.append(gap)

    combined = b"".join(pieces)
    wav = _wrap_pcm_to_wav(combined, base)
    duration = _pcm_duration_seconds(combined, base)
    _log.info(
        "audio_gap_joined",
        segments=len(segs),
        gap_seconds=round(gap_seconds, 3),
        duration_seconds=round(duration, 3),
    )
    return wav, duration
