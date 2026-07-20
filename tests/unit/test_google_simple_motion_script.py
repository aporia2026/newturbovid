"""Tests for the google-simple-motion localized script generator.

The OpenAI client is a hermetic fake returning canned JSON. Covers:
  - happy path: subject + 3 localized opening variants + sentence 2, cost carried
  - variants align to OPENINGS order (explore/learn/read)
  - malformed JSON → English fallback (row still ships)
  - short/incomplete JSON (missing variants) → fallback keeps a usable subject
  - LLM raising → English fallback, no exception escapes
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from bulkvid.pipeline.google_simple_motion import (
    OPENINGS,
    generate_learn_more_script,
)


@dataclass
class _FakeResult:
    text: str
    cost_usd: float = 0.001


class _FakeClient:
    """Minimal OpenAIClient stand-in: returns a canned response or raises."""

    def __init__(self, *, text: str | None = None, raise_exc: Exception | None = None):
        self._text = text
        self._raise = raise_exc
        self.calls = 0

    async def chat(self, *args: Any, **kwargs: Any) -> _FakeResult:
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        return _FakeResult(text=self._text or "")


async def test_happy_path_returns_localized_script() -> None:
    payload = json.dumps(
        {
            "subject": "die neuen Autoangebote",
            "sentence1_variants": [
                "Entdecken Sie mehr über die neuen Autoangebote.",
                "Erfahren Sie mehr über die neuen Autoangebote.",
                "Lesen Sie mehr über die neuen Autoangebote.",
            ],
            "sentence2": "Entdecken Sie wichtige Details und nützliche Informationen über die neuen Autoangebote.",
        }
    )
    client = _FakeClient(text=payload)
    script = await generate_learn_more_script(client, article_body="Auto news...", language="de")
    assert script.subject == "die neuen Autoangebote"
    assert len(script.sentence1_variants) == 3
    assert script.sentence1_variants[0].startswith("Entdecken")   # explore-more slot
    assert script.sentence1_variants[1].startswith("Erfahren")    # learn-more slot
    assert "nützliche Informationen" in script.sentence2
    assert script.cost_usd == pytest.approx(0.001)
    assert len(OPENINGS) == 3


async def test_malformed_json_falls_back_to_english() -> None:
    client = _FakeClient(text="not json at all {")
    script = await generate_learn_more_script(client, article_body="x", language="de")
    # English fallback: 3 variants aligned to OPENINGS, a usable sentence 2.
    assert len(script.sentence1_variants) == 3
    assert script.sentence1_variants[0].startswith("Explore more")
    assert script.sentence1_variants[1].startswith("Learn more")
    assert script.sentence1_variants[2].startswith("Read more")
    assert "Discover key details" in script.sentence2


async def test_incomplete_json_falls_back_but_keeps_subject() -> None:
    payload = json.dumps({"subject": "car deals", "sentence1_variants": ["only one"]})
    client = _FakeClient(text=payload)
    script = await generate_learn_more_script(client, article_body="x", language="en")
    assert script.subject == "car deals"
    assert len(script.sentence1_variants) == 3
    assert "car deals" in script.sentence1_variants[0]
    assert "car deals" in script.sentence2


async def test_llm_exception_falls_back() -> None:
    client = _FakeClient(raise_exc=RuntimeError("boom"))
    script = await generate_learn_more_script(client, article_body="x", language="en")
    assert len(script.sentence1_variants) == 3
    assert script.subject  # a usable default subject
