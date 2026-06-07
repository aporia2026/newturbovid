"""Shared retry helper for paid-provider adapters.

Wraps a single async call (an OpenAI chat, a Gemini TTS synth, a Sheets read)
with bounded exponential-backoff retries plus optional ``Retry-After`` honor.

Why this exists:
  - Plan ``_plans/2026-06-07-overload-handling-and-template-defaults.md`` §A.1.
  - The OpenAI client raised ``OpenAIRateLimitError`` on 429 with no retry,
    failing the row instantly. Now each adapter classifies its own retryable
    vs. terminal errors and passes them in.

Design rules:
  - Attempt count is hard-capped (default 3) — never run unbounded.
  - Per-attempt wait is capped at ``max_seconds`` (default 30) — no multi-minute
    waits even if a 429 ``Retry-After`` asks for one.
  - Full jitter on every wait so concurrent rows don't synchronise into a herd.
  - Terminal errors (auth, bad request) raise immediately without burning attempts.
  - Final failure re-raises the original exception unchanged so callers'
    error-type contracts (``OpenAIRateLimitError`` etc.) are preserved.

Logging shape (plan rule 14):
  - ``[ns op] retry attempt=N of=MAX wait_s=... reason=... retry_after=...``
  - ``[ns op] retry_exhausted attempts=N final_reason=...``
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from bulkvid.logging import get_logger

T = TypeVar("T")

_log = get_logger("retry")


DEFAULT_ATTEMPTS = 3
DEFAULT_BASE_SECONDS = 1.0
DEFAULT_MAX_SECONDS = 30.0


def _compute_wait(
    attempt_index: int,
    *,
    base_seconds: float,
    max_seconds: float,
    retry_after_seconds: float | None,
) -> float:
    """Pick the next wait. Returns seconds in ``[0, max_seconds]``.

    Precedence:
      1. ``Retry-After`` from the provider, if present (capped at ``max_seconds``).
      2. Exponential backoff ``base * 2 ** attempt_index`` with full jitter.

    ``attempt_index`` is 0-based: the wait *after* the first failed attempt
    is ``base`` (or jittered up to ``base``), not ``base*2``.
    """
    if retry_after_seconds is not None and retry_after_seconds > 0:
        return min(float(retry_after_seconds), max_seconds)
    target = base_seconds * (2 ** attempt_index)
    capped = min(target, max_seconds)
    # Full jitter: uniform in [0, capped]. Cheaper than half-jitter to reason
    # about; collision probability under concurrent rows stays low.
    return random.uniform(0.0, capped)


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    op: str,
    retryable: tuple[type[BaseException], ...],
    attempts: int = DEFAULT_ATTEMPTS,
    base_seconds: float = DEFAULT_BASE_SECONDS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    extract_retry_after: Callable[[BaseException], float | None] | None = None,
) -> T:
    """Run ``fn``; retry on ``retryable`` exceptions with exponential backoff.

    Args:
        fn: A zero-arg async callable producing the result. Wrap your actual
            call in a lambda or partial so this stays simple.
        op: Namespaced log tag like ``"openai chat"`` or ``"gemini tts"``.
            Emitted bracketed in every log line.
        retryable: Exception classes that trigger a retry. Anything not in
            this tuple bubbles immediately (terminal).
        attempts: Hard cap on total tries (including the first). Must be >= 1.
        base_seconds: Base for exponential backoff. Wait between attempt N and
            N+1 is jittered up to ``base * 2 ** N`` seconds.
        max_seconds: Per-wait ceiling. ``Retry-After`` is also clamped here.
        extract_retry_after: Optional. Given the caught exception, return
            ``Retry-After`` seconds if the provider supplied one, else ``None``.

    Returns:
        Whatever ``fn`` returns on success.

    Raises:
        The last caught ``retryable`` exception when attempts are exhausted,
        or any non-retryable exception immediately.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    last_exc: BaseException | None = None
    for attempt_index in range(attempts):
        try:
            return await fn()
        except retryable as e:
            last_exc = e
            attempts_left = attempts - attempt_index - 1
            if attempts_left == 0:
                _log.warning(
                    "retry_exhausted",
                    op=op,
                    attempts=attempts,
                    final_reason=type(e).__name__,
                    error=str(e)[:200],
                )
                raise
            retry_after = (
                extract_retry_after(e) if extract_retry_after is not None else None
            )
            wait_s = _compute_wait(
                attempt_index,
                base_seconds=base_seconds,
                max_seconds=max_seconds,
                retry_after_seconds=retry_after,
            )
            _log.info(
                "retry",
                op=op,
                attempt=attempt_index + 1,
                of=attempts,
                wait_s=round(wait_s, 3),
                reason=type(e).__name__,
                retry_after=retry_after,
                error=str(e)[:200],
            )
            await asyncio.sleep(wait_s)
    # Unreachable: loop either returns, re-raises, or sleeps and loops again.
    # mypy/strict-mode safety net.
    assert last_exc is not None
    raise last_exc
