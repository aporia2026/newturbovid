"""OpenAI adapter — chat completions + vision, with token-based cost tracking.

Used for:
  - gpt-5.4-mini  (Yoav directive)  -> script gen, prompt build, classifier
  - gpt-4o                          -> image description (vision input)

Pricing table is module-level and admin-overridable in Phase 5. Cost is
computed from the API's ``usage`` field (prompt_tokens + completion_tokens)
and returned alongside each result.

Errors are normalised to a small local hierarchy (``OpenAIAuthError``,
``OpenAIRateLimitError``, ``OpenAITimeoutError``) so callers don't have to
import openai's classes.

Plan: ``_plans/2026-06-02-aporia-bulk-video-tool.md`` §5 (models),
§8 (observability), §11 (cost model — refresh before each release).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import openai

from bulkvid.adapters._retry import with_retry
from bulkvid.config import Settings, get_settings
from bulkvid.logging import get_logger

_log = get_logger("openai")


# ── Pricing (USD per 1M tokens) ──────────────────────────────────────────────
# Source: OpenAI public pricing, verified 2026-06-02. Per CLAUDE.md rule 8,
# refresh before each release. Admin panel (Phase 5) overrides these without
# a redeploy.

PRICING_PER_1M_TOKENS: dict[str, dict[str, float]] = {
    "gpt-5.4-mini": {"input": 0.75, "output": 4.50},
    "gpt-5-mini":   {"input": 0.75, "output": 4.50},   # alias
    "gpt-4o":       {"input": 2.50, "output": 10.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},   # legacy fallback only
}


# Model identifiers locked at the adapter boundary.
MODEL_SCRIPT_GEN = "gpt-5.4-mini"
MODEL_VISION = "gpt-4o"
MODEL_COLLAGE_PROMPT = "gpt-5.4-mini"
MODEL_CLASSIFIER = "gpt-5.4-mini"


# ── Errors ───────────────────────────────────────────────────────────────────


class OpenAIError(RuntimeError):
    """Base class for adapter-level OpenAI errors."""


class OpenAIAuthError(OpenAIError):
    """401 — invalid API key."""


class OpenAIRateLimitError(OpenAIError):
    """429 — token / RPM bucket exhausted. Retryable via the ``_retry`` helper."""


class OpenAITimeoutError(OpenAIError):
    """Request exceeded ``timeout``. Retryable."""


class OpenAIServerError(OpenAIError):
    """5xx from OpenAI. Retryable — server-side hiccup, not our request."""


class OpenAIConnectionError(OpenAIError):
    """Network blew up before the request reached OpenAI. Retryable."""


# ── Result type ──────────────────────────────────────────────────────────────


@dataclass
class ChatResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    model: str


# ── Cost helper ──────────────────────────────────────────────────────────────


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Return token-based cost in USD. Unknown models return 0 (logged, not raised)."""
    pricing = PRICING_PER_1M_TOKENS.get(model)
    if pricing is None:
        _log.warning("openai_unknown_model_for_pricing", model=model)
        return 0.0
    input_cost = (prompt_tokens / 1_000_000) * pricing["input"]
    output_cost = (completion_tokens / 1_000_000) * pricing["output"]
    return round(input_cost + output_cost, 6)


# ── Client ───────────────────────────────────────────────────────────────────


class OpenAIClient:
    """Thin async wrapper around ``openai.AsyncOpenAI`` with cost tracking."""

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        timeout: float = 60.0,
        client: openai.AsyncOpenAI | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAIClient requires an api_key")
        self._owned = client is None
        # ``max_retries=0`` disables the SDK's built-in retry (default 2 with
        # internal exponential backoff). We own retries via ``_retry.with_retry``
        # so the policy is consistent across adapters and the log shape is
        # uniform. Without this, a single failing call could trigger 2 SDK
        # retries × 3 wrapper retries = 6 round-trips with hidden latency.
        self._client = client or openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
        )

    async def aclose(self) -> None:
        if self._owned:
            await self._client.close()

    async def __aenter__(self) -> OpenAIClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    # ── Plain chat ────────────────────────────────────────────────────────

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> ChatResult:
        """Submit a chat completion. Returns text + token counts + cost."""
        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if max_tokens is not None:
            # OpenAI deprecated ``max_tokens`` in favour of
            # ``max_completion_tokens``. Newer models (gpt-5.x, o-series)
            # reject ``max_tokens`` outright; the new spelling is accepted
            # everywhere. We keep ``max_tokens`` as the Python parameter
            # name so callers don't have to change.
            kwargs["max_completion_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature
        if response_format is not None:
            kwargs["response_format"] = response_format

        _log.info(
            "openai_chat",
            model=model,
            message_count=len(messages),
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=bool(response_format),
        )

        async def _call() -> Any:
            try:
                return await self._client.chat.completions.create(**kwargs)
            except openai.AuthenticationError as e:
                # Terminal — a wrong key won't fix itself on retry.
                raise OpenAIAuthError(str(e)) from e
            except openai.BadRequestError as e:
                # Terminal — 400/422; the prompt itself is wrong.
                raise OpenAIError(str(e)) from e
            except openai.PermissionDeniedError as e:
                # Terminal — 403.
                raise OpenAIError(str(e)) from e
            except openai.RateLimitError as e:
                # Retryable.
                raise OpenAIRateLimitError(str(e)) from e
            except openai.APITimeoutError as e:
                # Retryable — the request didn't get a response in time.
                raise OpenAITimeoutError(str(e)) from e
            except openai.InternalServerError as e:
                # Retryable — 5xx, server-side hiccup.
                raise OpenAIServerError(str(e)) from e
            except openai.APIConnectionError as e:
                # Retryable — the network died before OpenAI answered.
                raise OpenAIConnectionError(str(e)) from e
            except openai.APIError as e:
                # Unknown — fail closed as terminal so we don't burn budget
                # retrying something the helper has no opinion on.
                raise OpenAIError(str(e)) from e

        resp = await with_retry(
            _call,
            op="openai chat",
            retryable=(
                OpenAIRateLimitError,
                OpenAITimeoutError,
                OpenAIServerError,
                OpenAIConnectionError,
            ),
            extract_retry_after=_extract_openai_retry_after,
        )

        text = (resp.choices[0].message.content or "").strip()
        usage = resp.usage
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        cost = estimate_cost_usd(model, prompt_tokens, completion_tokens)

        _log.info(
            "openai_chat_ok",
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            text_chars=len(text),
        )

        return ChatResult(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            model=model,
        )

    # ── Vision (gpt-4o, single image) ─────────────────────────────────────

    async def vision_describe(
        self,
        prompt: str,
        image_url: str | None = None,
        image_b64: str | None = None,
        *,
        model: str = MODEL_VISION,
        detail: str = "high",
        max_tokens: int = 500,
    ) -> ChatResult:
        """Single-image vision call. Provide ``image_url`` OR ``image_b64``."""
        if not image_url and not image_b64:
            raise ValueError("vision_describe requires image_url or image_b64")

        if image_b64:
            url_value = f"data:image/png;base64,{image_b64}"
        else:
            url_value = image_url  # type: ignore[assignment]

        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": url_value, "detail": detail},
                    },
                ],
            }
        ]

        return await self.chat(model=model, messages=messages, max_tokens=max_tokens)


def _extract_openai_retry_after(exc: BaseException) -> float | None:
    """Pull ``Retry-After`` (seconds) from the underlying OpenAI exception.

    The SDK's ``RateLimitError`` (parent ``APIStatusError``) carries the raw
    httpx response on ``.response``. The wrapper exception chains the SDK one
    via ``__cause__``, so walk back to the original and read the header.

    Returns ``None`` when no usable value is present so the retry helper falls
    back to its exponential backoff.
    """
    orig = exc.__cause__
    response = getattr(orig, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        # The spec also allows an HTTP-date here; treat it as "use backoff".
        return None


def build_client_from_settings(settings: Settings | None = None) -> OpenAIClient:
    s = settings or get_settings()
    if not s.OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY is empty; cannot build OpenAIClient")
    return OpenAIClient(api_key=s.OPENAI_API_KEY)
