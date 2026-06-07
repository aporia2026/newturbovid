"""Tests for the shared retry helper.

Covers:
  - Success on first attempt: no sleeping, no logging
  - Retries on the configured exception type
  - Does NOT retry on a non-retryable exception
  - Exhausts attempts on persistent failure and re-raises the original
  - Honors ``Retry-After`` when ``extract_retry_after`` returns a value
  - Caps the wait at ``max_seconds`` even when ``Retry-After`` exceeds it
  - Rejects ``attempts < 1``
  - Wait is jittered (not deterministic) — assert it's bounded, not equal
"""

from __future__ import annotations

import pytest

from bulkvid.adapters import _retry
from bulkvid.adapters._retry import with_retry


class TransientError(Exception):
    """Stand-in for an OpenAI 429 / Gemini ResourceExhausted."""


class TerminalError(Exception):
    """Stand-in for an OpenAI 401 / Gemini InvalidArgument."""


# ── Success paths ───────────────────────────────────────────────────────────


async def test_returns_on_first_success() -> None:
    calls = {"n": 0}

    async def fn() -> str:
        calls["n"] += 1
        return "ok"

    result = await with_retry(fn, op="test op", retryable=(TransientError,))
    assert result == "ok"
    assert calls["n"] == 1


async def test_returns_after_retry_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin sleep to a no-op so the test runs instantly.
    monkeypatch.setattr(_retry.asyncio, "sleep", _no_sleep)

    calls = {"n": 0}

    async def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientError("flaky")
        return "ok"

    result = await with_retry(
        fn, op="test op", retryable=(TransientError,), attempts=3
    )
    assert result == "ok"
    assert calls["n"] == 3


# ── Failure paths ───────────────────────────────────────────────────────────


async def test_does_not_retry_terminal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_retry.asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    async def fn() -> str:
        calls["n"] += 1
        raise TerminalError("auth")

    with pytest.raises(TerminalError):
        await with_retry(
            fn, op="test op", retryable=(TransientError,), attempts=5
        )
    # Terminal errors bypass the retry loop entirely.
    assert calls["n"] == 1


async def test_exhausts_attempts_and_reraises_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_retry.asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    async def fn() -> str:
        calls["n"] += 1
        raise TransientError(f"attempt {calls['n']}")

    with pytest.raises(TransientError) as excinfo:
        await with_retry(
            fn, op="test op", retryable=(TransientError,), attempts=3
        )
    # Original exception bubbles unchanged (preserves caller error contracts).
    assert "attempt 3" in str(excinfo.value)
    assert calls["n"] == 3


async def test_attempts_must_be_positive() -> None:
    async def fn() -> str:
        return "ok"

    with pytest.raises(ValueError):
        await with_retry(
            fn, op="test op", retryable=(TransientError,), attempts=0
        )


# ── Retry-After honor ───────────────────────────────────────────────────────


async def test_honors_retry_after_when_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []

    async def capture_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(_retry.asyncio, "sleep", capture_sleep)

    calls = {"n": 0}

    async def fn() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TransientError("rate limited")
        return "ok"

    def extract_retry_after(e: BaseException) -> float | None:
        # Pretend the provider's exception carried Retry-After: 7s.
        return 7.0

    result = await with_retry(
        fn,
        op="test op",
        retryable=(TransientError,),
        attempts=2,
        extract_retry_after=extract_retry_after,
        max_seconds=30.0,
    )
    assert result == "ok"
    assert len(waits) == 1
    # Retry-After takes precedence over exponential backoff.
    assert waits[0] == 7.0


async def test_retry_after_is_capped_at_max_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []

    async def capture_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(_retry.asyncio, "sleep", capture_sleep)

    calls = {"n": 0}

    async def fn() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TransientError("rate limited")
        return "ok"

    def extract_retry_after(_e: BaseException) -> float | None:
        # Hostile provider asks us to wait 5 minutes; we cap at max_seconds.
        return 300.0

    await with_retry(
        fn,
        op="test op",
        retryable=(TransientError,),
        attempts=2,
        extract_retry_after=extract_retry_after,
        max_seconds=15.0,
    )
    assert waits == [15.0]


# ── Backoff bounds ──────────────────────────────────────────────────────────


async def test_backoff_waits_stay_within_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []

    async def capture_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(_retry.asyncio, "sleep", capture_sleep)
    calls = {"n": 0}

    async def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 4:
            raise TransientError("transient")
        return "ok"

    await with_retry(
        fn,
        op="test op",
        retryable=(TransientError,),
        attempts=4,
        base_seconds=1.0,
        max_seconds=5.0,
    )
    # 3 retries → 3 waits. Full-jitter waits are non-deterministic but each
    # must land in [0, capped] where capped = min(base * 2**i, max_seconds).
    assert len(waits) == 3
    for i, w in enumerate(waits):
        upper = min(1.0 * (2 ** i), 5.0)
        assert 0.0 <= w <= upper + 1e-9, (i, w, upper)


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _no_sleep(_seconds: float) -> None:
    """Replacement for ``asyncio.sleep`` that doesn't actually sleep."""
    return None
