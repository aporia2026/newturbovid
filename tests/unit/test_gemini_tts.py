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

import asyncio
import io
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import structlog.testing

from bulkvid.adapters import _retry
from bulkvid.adapters import gemini_tts as _gemini_tts_mod

# Capture the real ``asyncio.sleep`` BEFORE any fixture replaces it. The
# autouse ``_no_sleep_between_retries`` fixture below monkeypatches
# ``_retry.asyncio.sleep`` — which IS ``asyncio.sleep`` globally — to a
# no-op so retry backoffs don't slow the suite. Tests that need to yield
# the event loop (semaphore-cap probes, log-wait probes) call ``_real_sleep``
# instead so the scheduler actually advances other tasks.
_real_sleep = asyncio.sleep
from bulkvid.adapters.gemini_tts import (
    COST_GEMINI_TTS_PER_CHAR_USD,
    DEFAULT_VOICE,
    GEMINI_TTS_CHANNELS,
    GEMINI_TTS_DEFAULT_MAX_CONCURRENT,
    GEMINI_TTS_DEFAULT_MAX_PER_MINUTE,
    GEMINI_TTS_RETRY_MAX_SECONDS,
    GEMINI_TTS_RPM_WAIT_LOG_THRESHOLD_SECONDS,
    GEMINI_TTS_SAMPLE_RATE_HZ,
    GEMINI_TTS_SAMPLE_WIDTH_BYTES,
    GEMINI_TTS_SEMAPHORE_WAIT_LOG_THRESHOLD_SECONDS,
    VOICE_BY_LANGUAGE,
    GeminiTTSClient,
    GeminiTTSNoAudioError,
    GeminiTTSRateLimitError,
    GeminiTTSServerError,
    GeminiTTSTimeoutError,
    _RpmLimiter,
    accent_directive,
    pcm_duration_seconds,
    pick_voice,
    wrap_pcm_to_wav,
)


@pytest.fixture(autouse=True)
def _no_sleep_between_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every retry sleep instant so the suite stays fast."""

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_retry.asyncio, "sleep", _instant)


# Stand-in exception names that match what the google-api-core SDK uses.
# Defining them locally keeps the test suite from importing google-api-core
# (the classifier identifies by type name + message, not by isinstance).


class ResourceExhausted(Exception):
    pass


class DeadlineExceeded(Exception):
    pass


class ServiceUnavailable(Exception):
    pass


def _make_failing_client(exc: Exception) -> SimpleNamespace:
    """Build a fake client whose ``generate_content`` always raises ``exc``."""
    generate = AsyncMock(side_effect=exc)
    return SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    )


def _make_eventually_succeeding_client(
    exc: Exception, response: SimpleNamespace
) -> SimpleNamespace:
    """Fake client: raises ``exc`` once, then returns ``response`` forever."""
    generate = AsyncMock(side_effect=[exc, response])
    return SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate))
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


# ── Retry behavior ──────────────────────────────────────────────────────────


async def test_synthesize_retries_on_resource_exhausted_then_succeeds() -> None:
    pcm = b"\x00" * 4000
    response = _make_fake_response(pcm)
    fake_client = _make_eventually_succeeding_client(
        ResourceExhausted("quota exceeded"), response
    )

    tts = GeminiTTSClient(project="amit-tts", client=fake_client)
    result = await tts.synthesize(text="hello world", language="en")

    assert result.wav_bytes[:4] == b"RIFF"
    assert fake_client.aio.models.generate_content.await_count == 2


async def test_synthesize_exhausts_then_raises_rate_limit() -> None:
    fake_client = _make_failing_client(ResourceExhausted("quota exceeded"))
    tts = GeminiTTSClient(project="amit-tts", client=fake_client)

    with pytest.raises(GeminiTTSRateLimitError):
        await tts.synthesize(text="hello", language="en")

    assert fake_client.aio.models.generate_content.await_count == 3


async def test_synthesize_retries_on_deadline_exceeded() -> None:
    fake_client = _make_failing_client(DeadlineExceeded("deadline exceeded"))
    tts = GeminiTTSClient(project="amit-tts", client=fake_client)

    with pytest.raises(GeminiTTSTimeoutError):
        await tts.synthesize(text="hello", language="en")

    assert fake_client.aio.models.generate_content.await_count == 3


async def test_synthesize_retries_on_service_unavailable() -> None:
    fake_client = _make_failing_client(ServiceUnavailable("503 service unavailable"))
    tts = GeminiTTSClient(project="amit-tts", client=fake_client)

    with pytest.raises(GeminiTTSServerError):
        await tts.synthesize(text="hello", language="en")

    assert fake_client.aio.models.generate_content.await_count == 3


async def test_synthesize_does_not_retry_unknown_error() -> None:
    # A garden-variety ValueError isn't recognised as retryable; the classifier
    # leaves it alone and it bubbles immediately.
    fake_client = _make_failing_client(ValueError("you handed in nonsense"))
    tts = GeminiTTSClient(project="amit-tts", client=fake_client)

    with pytest.raises(ValueError):
        await tts.synthesize(text="hello", language="en")

    assert fake_client.aio.models.generate_content.await_count == 1


# ── Per-provider semaphore + lifted retry ceiling ───────────────────────────
#
# Coverage for ``_plans/2026-06-08-200-row-batch-failures.md`` §Phase 1 Part 4.
# Added 2026-06-08 after the 277-row simple-tab batch hit 54 TTS_FAILED rows
# from per-minute Gemini quota bursts.


def test_constructor_rejects_max_concurrent_zero() -> None:
    with pytest.raises(ValueError):
        GeminiTTSClient(project="amit-tts", max_concurrent=0)


async def test_semaphore_default_matches_module_constant() -> None:
    # Sanity: factory default == module constant. If one moves the other moves.
    client = GeminiTTSClient(project="amit-tts")
    sem = client._get_sem()
    assert sem._value == GEMINI_TTS_DEFAULT_MAX_CONCURRENT


async def test_semaphore_caps_concurrent_synth() -> None:
    # With max_concurrent=2, only 2 ``synthesize`` calls should be mid-flight
    # at any instant. Probe the peak via a shared counter inside the fake
    # generate_content (which runs after the slot is acquired).
    #
    # NB: we bind the async function DIRECTLY to the SimpleNamespace rather
    # than wrapping it in AsyncMock. ``AsyncMock(side_effect=async_fn)``
    # calls the async function but does NOT auto-await the returned
    # coroutine — the synthesize await receives a coroutine, not a real
    # response, and our in-flight probe never bumps.
    in_flight = {"now": 0, "peak": 0}
    release = asyncio.Event()
    pcm = b"\x00" * (24_000 * 1 * 2 // 2)    # half a second of audio

    async def _generate(**_: Any) -> SimpleNamespace:
        in_flight["now"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        try:
            await release.wait()    # hold the slot
            return _make_fake_response(pcm)
        finally:
            in_flight["now"] -= 1

    fake_client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=_generate))
    )

    tts = GeminiTTSClient(project="amit-tts", max_concurrent=2, client=fake_client)

    async def _one() -> None:
        await tts.synthesize(text="hello", language="en")

    tasks = [asyncio.create_task(_one()) for _ in range(5)]
    # Let the scheduler grant up to max_concurrent slots before we release.
    # Use ``_real_sleep`` because the autouse retry-no-sleep fixture has
    # patched ``asyncio.sleep`` itself to a no-op.
    await _real_sleep(0.05)
    assert in_flight["peak"] == 2, (
        f"expected peak concurrency 2, saw {in_flight['peak']} "
        "(semaphore is NOT capping cross-row Gemini TTS calls)"
    )
    release.set()
    await asyncio.gather(*tasks)


async def test_semaphore_wait_logged_when_threshold_exceeded() -> None:
    # When the cap bites for longer than the log threshold, we want a
    # gemini_tts_semaphore_wait event so we can see the cap is biting.
    release = asyncio.Event()
    pcm = b"\x00" * (24_000 * 1 * 2 // 2)

    async def _generate(**_: Any) -> SimpleNamespace:
        await release.wait()
        return _make_fake_response(pcm)

    fake_client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=_generate))
    )
    tts = GeminiTTSClient(project="amit-tts", max_concurrent=1, client=fake_client)

    with structlog.testing.capture_logs() as logs:
        t1 = asyncio.create_task(tts.synthesize(text="a", language="en"))
        t2 = asyncio.create_task(tts.synthesize(text="b", language="en"))
        await _real_sleep(GEMINI_TTS_SEMAPHORE_WAIT_LOG_THRESHOLD_SECONDS + 0.2)
        release.set()
        await asyncio.gather(t1, t2)

    waits = [e for e in logs if e.get("event") == "gemini_tts_semaphore_wait"]
    assert waits, (
        "expected at least one gemini_tts_semaphore_wait event when the cap is biting"
    )
    assert waits[0]["queued_for_s"] >= GEMINI_TTS_SEMAPHORE_WAIT_LOG_THRESHOLD_SECONDS


async def test_synthesize_passes_lifted_max_seconds_to_with_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bug-fix-as-test (rule 18): Gemini's per-minute quota window needs a
    # Retry-After honor ceiling well above 30s. The shared with_retry default
    # is 30s, which would clamp a 60s Retry-After to half its window — the
    # second attempt then hits the SAME quota window and bounces. This test
    # locks in max_seconds=GEMINI_TTS_RETRY_MAX_SECONDS (65s) for the
    # Gemini-TTS call site specifically.
    pcm = b"\x00" * (24_000 * 1 * 2 // 2)
    fake_client = _make_fake_client(_make_fake_response(pcm))

    captured: dict[str, Any] = {}

    async def _spy_with_retry(fn: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return await fn()

    monkeypatch.setattr(_gemini_tts_mod, "with_retry", _spy_with_retry)

    tts = GeminiTTSClient(project="amit-tts", client=fake_client)
    await tts.synthesize(text="hello", language="en")

    assert captured.get("max_seconds") == GEMINI_TTS_RETRY_MAX_SECONDS, (
        f"Gemini synthesize must pass max_seconds={GEMINI_TTS_RETRY_MAX_SECONDS} "
        f"to with_retry (got {captured.get('max_seconds')!r}). The default 30s "
        "clamps Retry-After below the per-minute quota window — second attempt "
        "hits the same window and bounces."
    )


# ── Per-minute RPM limiter ──────────────────────────────────────────────────
#
# Vertex enforces ``generate_content_requests_per_minute`` at the project
# level; on the amit-tts project that ceiling is 15 RPM and exceeding it
# returns 429 RESOURCE_EXHAUSTED (chat 2026-06-14). The concurrency
# semaphore alone can't prevent it — 4 in-flight × ~4s each = ~60 RPM
# peak. These tests lock in:
#   1. ``_RpmLimiter`` blocks the (N+1)th acquire until the oldest entry
#      ages out of the window.
#   2. ``GeminiTTSClient`` plumbs ``max_per_minute`` through to a real
#      ``_RpmLimiter`` and acquires on every call.
#   3. The constructor rejects nonsense values.
#   4. RPM gate sits INSIDE the retry boundary so each retry attempt
#      consumes a slot (a 15-call burst × 3 retries each is otherwise
#      ~45 hits inside one window — defeats the whole cap).


def test_rpm_limiter_rejects_zero() -> None:
    with pytest.raises(ValueError):
        _RpmLimiter(max_per_minute=0)


def test_rpm_limiter_rejects_nonpositive_window() -> None:
    with pytest.raises(ValueError):
        _RpmLimiter(max_per_minute=15, window_seconds=0)


async def test_rpm_limiter_lets_first_n_through_immediately() -> None:
    # With cap=3, the first three acquires should each report zero wait.
    lim = _RpmLimiter(max_per_minute=3, window_seconds=60.0)
    for _ in range(3):
        waited = await lim.acquire()
        assert waited == 0.0


async def test_rpm_limiter_blocks_overflow_until_window_rolls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The 4th acquire under cap=3 must NOT return until the oldest entry
    # ages out of the (tiny) window. Restore the real ``asyncio.sleep``
    # so the limiter's wait advances real time instead of spinning under
    # the autouse no-op-sleep fixture.
    monkeypatch.setattr(_retry.asyncio, "sleep", _real_sleep)

    window = 0.3
    lim = _RpmLimiter(max_per_minute=3, window_seconds=window)
    for _ in range(3):
        await lim.acquire()

    t0 = asyncio.get_event_loop().time()
    waited = await lim.acquire()
    elapsed = asyncio.get_event_loop().time() - t0

    # We slept until the oldest entry aged out — must be at least ~window.
    # The limiter pads with +0.01 to avoid edge-of-window races; allow a
    # generous lower bound and a sane upper bound.
    assert elapsed >= window * 0.9, (
        f"4th acquire returned after {elapsed:.3f}s, expected >= {window:.3f}s — "
        "limiter is NOT blocking when the window is full"
    )
    assert waited > 0.0


# ── Constructor / wiring ────────────────────────────────────────────────────


def test_constructor_rejects_max_per_minute_zero() -> None:
    with pytest.raises(ValueError):
        GeminiTTSClient(project="amit-tts", max_per_minute=0)


async def test_rpm_default_matches_module_constant() -> None:
    # Sanity: factory default == module constant. If one moves, the other moves.
    client = GeminiTTSClient(project="amit-tts")
    rpm = client._get_rpm()
    assert rpm._max == GEMINI_TTS_DEFAULT_MAX_PER_MINUTE


async def test_synthesize_acquires_rpm_before_generate_content() -> None:
    # Order matters: rpm.acquire MUST be awaited before generate_content so
    # the per-minute quota is paced. Spy on both and verify the timeline.
    pcm = b"\x00" * (24_000 * 1 * 2 // 2)
    fake_client = _make_fake_client(_make_fake_response(pcm))
    tts = GeminiTTSClient(project="amit-tts", client=fake_client)

    timeline: list[str] = []

    rpm = tts._get_rpm()
    real_acquire = rpm.acquire

    async def _spy_acquire() -> float:
        timeline.append("rpm.acquire")
        return await real_acquire()

    rpm.acquire = _spy_acquire    # type: ignore[method-assign]

    real_gen = fake_client.aio.models.generate_content

    async def _spy_generate(**kwargs: Any) -> SimpleNamespace:
        timeline.append("generate_content")
        return await real_gen(**kwargs)

    fake_client.aio.models.generate_content = _spy_generate

    await tts.synthesize(text="hello", language="en")

    assert timeline == ["rpm.acquire", "generate_content"], (
        f"expected rpm.acquire before generate_content, got {timeline!r}"
    )


async def test_rpm_acquired_per_retry_attempt() -> None:
    # The cap is per generate_content CALL, not per synthesize. A retried
    # synthesize must consume an RPM slot on EACH attempt — otherwise a
    # 15-call burst × 3 retries each gets ~45 calls into one window.
    pcm = b"\x00" * (24_000 * 1 * 2 // 2)
    fake_client = _make_eventually_succeeding_client(
        ResourceExhausted("quota exceeded"), _make_fake_response(pcm)
    )
    tts = GeminiTTSClient(project="amit-tts", client=fake_client)

    rpm = tts._get_rpm()
    acquire_count = {"n": 0}
    real_acquire = rpm.acquire

    async def _counting_acquire() -> float:
        acquire_count["n"] += 1
        return await real_acquire()

    # Replace the bound method on this instance so synthesize hits the spy.
    rpm.acquire = _counting_acquire  # type: ignore[method-assign]

    await tts.synthesize(text="hello", language="en")

    # 1st attempt raises ResourceExhausted (1 rpm), 2nd attempt succeeds (1 rpm).
    assert acquire_count["n"] == 2, (
        f"expected 2 rpm acquires (one per attempt), got {acquire_count['n']}"
    )


async def test_rpm_wait_logged_when_threshold_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When the rate gate makes a caller wait longer than the log threshold,
    # we want a ``gemini_tts_rpm_wait`` event so production can see the cap
    # biting. Use a tiny window so the wait actually exceeds the threshold
    # without blowing up the test runtime.
    monkeypatch.setattr(_retry.asyncio, "sleep", _real_sleep)

    pcm = b"\x00" * (24_000 * 1 * 2 // 2)
    fake_client = _make_fake_client(_make_fake_response(pcm))

    # Pick window = threshold + 0.5s so the second acquire WILL wait > threshold.
    window = GEMINI_TTS_RPM_WAIT_LOG_THRESHOLD_SECONDS + 0.5
    tts = GeminiTTSClient(
        project="amit-tts",
        client=fake_client,
        max_per_minute=1,
    )
    # Shrink the window on the lazy-built limiter.
    tts._rpm = _RpmLimiter(max_per_minute=1, window_seconds=window)

    with structlog.testing.capture_logs() as logs:
        await tts.synthesize(text="a", language="en")    # fills the 1-slot bucket
        await tts.synthesize(text="b", language="en")    # waits for it to age out

    waits = [e for e in logs if e.get("event") == "gemini_tts_rpm_wait"]
    assert waits, (
        "expected at least one gemini_tts_rpm_wait event when the rate gate is biting"
    )
    assert waits[0]["queued_for_s"] >= GEMINI_TTS_RPM_WAIT_LOG_THRESHOLD_SECONDS
