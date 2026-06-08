"""Tests for the card-headline generator.

A single small GPT call extracts a punchy ≤8-word headline for the simple
x4 card overlay. The module is designed to soft-fail (return ``("", 0.0)``)
so a transient OpenAI hiccup never kills a row.

Covers:
  - happy path returns trimmed headline + reports cost
  - surrounding quotes stripped
  - trailing period stripped (single only — not "..." or "?.")
  - over-budget output truncated to ``max_words``
  - any exception from the chat client yields ``("", 0.0)``
  - blank article body still produces a valid call (no crash)

Plan: ``_plans/2026-06-08-simple-x4-template-cards.md`` §D.4.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from bulkvid.pipeline.headline_gen import generate_card_headline


@dataclass
class _StubChatResult:
    text: str
    prompt_tokens: int = 30
    completion_tokens: int = 8
    cost_usd: float = 0.000123
    model: str = "gpt-5.4-mini"


class _StubOpenAIClient:
    """Minimal async stub that returns whatever text we hand it."""

    def __init__(self, text: str = "", *, raise_exc: Exception | None = None) -> None:
        self._text = text
        self._raise = raise_exc
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise is not None:
            raise self._raise
        return _StubChatResult(text=self._text)


# ── Happy path ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_returns_text_and_cost() -> None:
    client = _StubOpenAIClient(text="Cheap Repossessed Cars 2026")
    text, cost = await generate_card_headline(
        client,
        article_excerpt="Body about cheap repossessed cars in Germany.",
        language="en",
        vertical="Car Deals PR",
    )
    assert text == "Cheap Repossessed Cars 2026"
    assert cost == pytest.approx(0.000123)


@pytest.mark.asyncio
async def test_surrounding_quotes_stripped() -> None:
    """GPT loves to wrap output in quotes; cards shouldn't show them."""
    client = _StubOpenAIClient(text='"Buy Smart, Drive Cheap"')
    text, _ = await generate_card_headline(
        client, article_excerpt="x", language="en", vertical=""
    )
    assert text == "Buy Smart, Drive Cheap"


@pytest.mark.asyncio
async def test_trailing_period_stripped() -> None:
    client = _StubOpenAIClient(text="Smart Shoppers Save Big.")
    text, _ = await generate_card_headline(
        client, article_excerpt="x", language="en", vertical=""
    )
    assert text == "Smart Shoppers Save Big"


@pytest.mark.asyncio
async def test_long_output_truncated_to_max_words() -> None:
    """Defense in depth: prompt asks for ≤8 words but the renderer can't
    wrap an essay. The truncation guarantee belongs to the caller."""
    long = "one two three four five six seven eight nine ten eleven twelve thirteen"
    client = _StubOpenAIClient(text=long)
    text, _ = await generate_card_headline(
        client,
        article_excerpt="x",
        language="en",
        vertical="",
        max_words=8,
    )
    assert text == "one two three four five six seven eight"


# ── Soft-fail behavior ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_exception_returns_empty_no_cost() -> None:
    """Any exception => ('', 0.0). The renderer just draws no title."""
    client = _StubOpenAIClient(raise_exc=RuntimeError("openai 500"))
    text, cost = await generate_card_headline(
        client, article_excerpt="x", language="en", vertical=""
    )
    assert text == ""
    assert cost == 0.0


@pytest.mark.asyncio
async def test_blank_article_does_not_crash() -> None:
    """Empty article body still sends a sane prompt and returns whatever
    the model produces (might be empty)."""
    client = _StubOpenAIClient(text="Smart Shopping Today")
    text, _ = await generate_card_headline(
        client, article_excerpt="", language="en", vertical="Car Deals"
    )
    assert text == "Smart Shopping Today"
    # The user message should NOT be empty — it always has the framing
    # block. Otherwise we'd be paying for a no-op call.
    user_msg = client.calls[0]["messages"][-1]["content"]
    assert "Language:" in user_msg
    assert "Vertical:" in user_msg
