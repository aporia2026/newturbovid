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
    """429 — token / RPM bucket exhausted."""


class OpenAITimeoutError(OpenAIError):
    """Request exceeded ``timeout``."""


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
        self._client = client or openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
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

        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except openai.AuthenticationError as e:
            raise OpenAIAuthError(str(e)) from e
        except openai.RateLimitError as e:
            raise OpenAIRateLimitError(str(e)) from e
        except openai.APITimeoutError as e:
            raise OpenAITimeoutError(str(e)) from e
        except openai.APIError as e:
            raise OpenAIError(str(e)) from e

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


def build_client_from_settings(settings: Settings | None = None) -> OpenAIClient:
    s = settings or get_settings()
    if not s.OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY is empty; cannot build OpenAIClient")
    return OpenAIClient(api_key=s.OPENAI_API_KEY)
