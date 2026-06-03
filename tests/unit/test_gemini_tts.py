"""Tests for the Gemini TTS adapter.

The underlying google-genai client is replaced with an ``AsyncMock``-driven
fake so the tests stay hermetic — no Vertex AI auth, no network.

Covers:
  - pick_voice: known language → mapped voice; unknown → default; override path
  - wrap_pcm_to_wav: produces a valid RIFF/WAVE file readable by stdlib `wave`
  - pcm_duration_seconds: math is correct for the expected 24kHz / mono / 16-bit format
  - synthesize: success returns TTSResult with cost, voice, duration
  - synthesize: empty text raises ValueError
  - synthesize: missing audio in response raises GeminiTTSNoAudioError
  - synthesize: style_prompt is prepended to the input
  - Constructor rejects empty project
"""

from __future__ import annotations

import io
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bulkvid.adapters.gemini_tts import (
    COST_GEMINI_TTS_PER_CHAR_USD,
    DEFAULT_VOICE,
    GEMINI_TTS_CHANNELS,
    GEMINI_TTS_SAMPLE_RATE_HZ,
    GEMINI_TTS_SAMPLE_WIDTH_BYTES,
    VOICE_BY_LANGUAGE,
    GeminiTTSClient,
    GeminiTTSNoAudioError,
    accent_directive,
    pcm_duration_seconds,
    pick_voice,
    wrap_pcm_to_wav,
)


def _make_fake_response(pcm: bytes) -> SimpleNamespace:
    """Build the minimum shape of a google-genai response carrying audio."""
    part = SimpleNamespace(inline_data=SimpleNamespace(data=pcm))
    content = SimpleNamespace(parts=[part])
    candidate = SimpleNamespace(content=content)
    return SimpleNamespace(candidates=[candidate])


def _make_fake_client(response: SimpleNamespace) -> SimpleNamespace:
    """Build a fake genai client. Mirrors client.aio.models.generate_content shape."""
    generate = AsyncMock(return_value=response)
    return SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    )


# ── pick_voice ───────────────────────────────────────────────────────────────


def test_pick_voice_maps_known_languages() -> None:
    for lang, expected_voice in VOICE_BY_LANGUAGE.items():
        assert pick_voice(lang) == expected_voice


def test_pick_voice_falls_back_to_default() -> None:
    assert pick_voice("xx") == DEFAULT_VOICE
    assert pick_voice("") == DEFAULT_VOICE


def test_pick_voice_honors_valid_override() -> None:
    assert pick_voice("he", override="Charon") == "Charon"


def test_pick_voice_ignores_invalid_override() -> None:
    # An override that isn't in the pool falls back to the language mapping.
    assert pick_voice("he", override="NotARealVoice") == VOICE_BY_LANGUAGE["he"]


# ── wrap_pcm_to_wav / pcm_duration_seconds ───────────────────────────────────


def test_wrap_pcm_to_wav_produces_valid_wave() -> None:
    pcm = b"\x00\x00" * 1000      # 1000 mono 16-bit samples
    wav = wrap_pcm_to_wav(pcm)
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"

    with wave.open(io.BytesIO(wav), "rb") as wf:
        assert wf.getnchannels() == GEMINI_TTS_CHANNELS
        assert wf.getsampwidth() == GEMINI_TTS_SAMPLE_WIDTH_BYTES
        assert wf.getframerate() == GEMINI_TTS_SAMPLE_RATE_HZ
        assert wf.getnframes() == 1000


def test_pcm_duration_seconds_math() -> None:
    # 1 second of 24kHz mono 16-bit = 24000 samples × 2 bytes = 48000 bytes
    one_second_pcm = b"\x00" * (24_000 * 1 * 2)
    assert pcm_duration_seconds(one_second_pcm) == pytest.approx(1.0)

    # Half a second
    half_second_pcm = b"\x00" * (24_000 * 1 * 2 // 2)
    assert pcm_duration_seconds(half_second_pcm) == pytest.approx(0.5)


def test_pcm_duration_seconds_empty() -> None:
    assert pcm_duration_seconds(b"") == 0.0


# ── Constructor ──────────────────────────────────────────────────────────────


def test_constructor_rejects_empty_project() -> None:
    with pytest.raises(ValueError):
        GeminiTTSClient(project="")


# ── synthesize ───────────────────────────────────────────────────────────────


async def test_synthesize_success_returns_full_result() -> None:
    # 0.5 seconds of audio
    pcm = b"\x00" * (24_000 * 1 * 2 // 2)
    fake_client = _make_fake_client(_make_fake_response(pcm))

    tts = GeminiTTSClient(project="amit-tts", client=fake_client)
    result = await tts.synthesize(
        text="Hello world this is a test",
        language="en",
    )

    assert result.voice == VOICE_BY_LANGUAGE["en"]
    assert result.language == "en"
    assert result.character_count == len("Hello world this is a test")
    assert result.duration_seconds == pytest.approx(0.5)
    assert result.wav_bytes[:4] == b"RIFF"
    # Cost = chars × per-char rate
    expected_cost = round(len("Hello world this is a test") * COST_GEMINI_TTS_PER_CHAR_USD, 6)
    assert result.cost_usd == expected_cost


async def test_synthesize_rejects_empty_text() -> None:
    tts = GeminiTTSClient(project="amit-tts", client=_make_fake_client(_make_fake_response(b"")))
    with pytest.raises(ValueError):
        await tts.synthesize(text="   ", language="en")


async def test_synthesize_raises_when_no_audio_in_response() -> None:
    fake_client = _make_fake_client(_make_fake_response(b""))   # empty pcm
    tts = GeminiTTSClient(project="amit-tts", client=fake_client)
    with pytest.raises(GeminiTTSNoAudioError):
        await tts.synthesize(text="hello", language="en")


async def test_synthesize_prepends_style_prompt() -> None:
    pcm = b"\x00" * 2000
    fake_client = _make_fake_client(_make_fake_response(pcm))
    tts = GeminiTTSClient(project="amit-tts", client=fake_client)

    await tts.synthesize(
        text="The actual script body.",
        language="en",
        style_prompt="Say warmly, like a podcast intro.",
    )

    # The fake client recorded the call; check that the prompt carries both pieces.
    call = fake_client.aio.models.generate_content.await_args
    contents_arg = call.kwargs["contents"]
    assert "Say warmly" in contents_arg
    assert "The actual script body." in contents_arg
    # Style must appear before the script body.
    assert contents_arg.index("Say warmly") < contents_arg.index("The actual script body.")


def test_accent_directive_english_by_country() -> None:
    assert accent_directive("en", "UK") == "Speak in a natural British English accent."
    assert accent_directive("en", "United Kingdom") == "Speak in a natural British English accent."
    assert accent_directive("en", "US") == "Speak in a natural American English accent."
    assert accent_directive("en", "australia") == "Speak in a natural Australian English accent."


def test_accent_directive_empty_or_unknown() -> None:
    assert accent_directive("en", "") == ""
    assert accent_directive("en", "Atlantis") == ""    # unknown -> no forced accent


def test_accent_directive_non_english_uses_country_dialect() -> None:
    d = accent_directive("es", "Mexico")
    assert "Mexico" in d
    assert "accent" in d.lower()


def test_accent_directive_expands_country_code() -> None:
    # A bare country code is expanded to a readable name in the directive.
    assert "Poland" in accent_directive("pl", "PL")
    assert "PL" not in accent_directive("pl", "PL")


async def test_synthesize_prepends_accent_for_country() -> None:
    pcm = b"\x00" * 2000
    fake_client = _make_fake_client(_make_fake_response(pcm))
    tts = GeminiTTSClient(project="amit-tts", client=fake_client)

    await tts.synthesize(text="The script body.", language="en", country="UK")

    contents = fake_client.aio.models.generate_content.await_args.kwargs["contents"]
    assert "British English accent" in contents
    assert contents.index("British") < contents.index("The script body.")


async def test_synthesize_honors_voice_override() -> None:
    pcm = b"\x00" * 2000
    fake_client = _make_fake_client(_make_fake_response(pcm))
    tts = GeminiTTSClient(project="amit-tts", client=fake_client)

    result = await tts.synthesize(text="x", language="he", voice="Zephyr")
    assert result.voice == "Zephyr"


# ── Cost constant sanity ─────────────────────────────────────────────────────


def test_cost_constant_positive() -> None:
    assert COST_GEMINI_TTS_PER_CHAR_USD > 0
