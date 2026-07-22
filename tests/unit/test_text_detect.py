"""Tests for the "does this creative already have a headline?" vision check.

Mocks OpenAI's HTTP layer with respx — the detector exercises the real
``OpenAIClient`` adapter but hits no network.

Every case here is really the same assertion from a different angle: only an
unambiguous, well-formed, confident YES may return ``has_overlay_text=True``.
That is the only answer that makes a row skip its overlay, and a wrong skip
ships an ad with no headline at all.

Covers:
  - Confident yes / plain no round-trip, with cost propagation
  - Low, missing, or unrecognised confidence downgrades a yes to compose
  - Confidence is irrelevant to a "no" (already the safe answer)
  - Non-bool ``has_overlay_text`` (yes / 1 / "true") is rejected
  - Malformed JSON, a JSON non-object, and a missing key all fail open
  - HTTP 500 and auth failure fail open with zero cost
  - The request actually carries the image and asks for strict JSON
"""

from __future__ import annotations

import json

import httpx
import respx

from bulkvid.adapters.openai_client import MODEL_TEXT_DETECT, OpenAIClient
from bulkvid.pipeline.text_detect import detect_overlay_text

API_KEY = "sk-test"
BASE = "https://api.openai.com/v1"
B64 = "aGVsbG8="    # stand-in payload; the transport is mocked


def _chat_response(content: str, ptokens: int = 1200, ctokens: int = 30) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1717_000_000,
        "model": MODEL_TEXT_DETECT,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": ptokens,
            "completion_tokens": ctokens,
            "total_tokens": ptokens + ctokens,
        },
    }


def _mock(content: str) -> respx.Route:
    return respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_response(content)),
    )


async def _detect(content: str):
    async with OpenAIClient(api_key=API_KEY) as client:
        return await detect_overlay_text(client, B64)


# ── Happy paths ─────────────────────────────────────────────────────────────


@respx.mock
async def test_confident_yes_is_the_only_thing_that_skips() -> None:
    """A well-formed, high-confidence yes is the one answer that makes the
    caller pass the image through untouched."""
    _mock(json.dumps({
        "has_overlay_text": True,
        "confidence": "high",
        "reason": "Large white headline with a black stroke laid over the photo.",
    }))

    verdict = await _detect("")

    assert verdict.has_overlay_text is True
    assert verdict.confidence == "high"
    assert "headline" in verdict.reason
    assert verdict.cost_usd > 0    # token cost propagates for batch accounting


@respx.mock
async def test_clean_photo_returns_no() -> None:
    _mock(json.dumps({
        "has_overlay_text": False,
        "confidence": "high",
        "reason": "Plain photograph, no added text layer.",
    }))

    verdict = await _detect("")

    assert verdict.has_overlay_text is False
    assert verdict.cost_usd > 0


@respx.mock
async def test_scene_text_answered_no_stays_no() -> None:
    """The shop-sign / licence-plate case. The prompt teaches the model to
    answer no; this locks in that a no is carried through verbatim rather
    than being second-guessed."""
    _mock(json.dumps({
        "has_overlay_text": False,
        "confidence": "high",
        "reason": "Only a shop sign inside the photographed scene.",
    }))

    assert (await _detect("")).has_overlay_text is False


# ── Confidence gate ─────────────────────────────────────────────────────────


@respx.mock
async def test_low_confidence_yes_downgrades_to_compose() -> None:
    """"Maybe there's text" must never skip the overlay — an ad with no
    headline is silent, doubled text is visible."""
    _mock(json.dumps({
        "has_overlay_text": True,
        "confidence": "low",
        "reason": "Possibly a caption, hard to tell.",
    }))

    verdict = await _detect("")

    assert verdict.has_overlay_text is False
    assert verdict.confidence == "low"       # preserved for the row metadata
    assert verdict.cost_usd > 0


@respx.mock
async def test_missing_confidence_downgrades_to_compose() -> None:
    _mock(json.dumps({"has_overlay_text": True, "reason": "headline"}))

    verdict = await _detect("")

    assert verdict.has_overlay_text is False
    assert verdict.confidence == ""


@respx.mock
async def test_unrecognised_confidence_downgrades_to_compose() -> None:
    """Anything that isn't exactly "high" fails the gate — no fuzzy matching
    on model output that decides whether an ad gets a headline."""
    _mock(json.dumps({
        "has_overlay_text": True, "confidence": "medium", "reason": "banner",
    }))

    assert (await _detect("")).has_overlay_text is False


@respx.mock
async def test_confidence_is_case_insensitive() -> None:
    """"HIGH" is the same answer as "high"; casing is a formatting quirk, not
    a hedge, so it must not cost a legitimate skip."""
    _mock(json.dumps({
        "has_overlay_text": True, "confidence": "HIGH", "reason": "headline",
    }))

    verdict = await _detect("")

    assert verdict.has_overlay_text is True
    assert verdict.confidence == "high"


@respx.mock
async def test_low_confidence_no_is_still_no() -> None:
    """The gate only guards the yes direction — a hedged no is already the
    safe answer and needs no downgrade."""
    _mock(json.dumps({
        "has_overlay_text": False, "confidence": "low", "reason": "unclear",
    }))

    assert (await _detect("")).has_overlay_text is False


# ── Malformed output fails open ─────────────────────────────────────────────


@respx.mock
async def test_non_bool_flag_is_rejected() -> None:
    """A model that answers "yes" instead of ``true`` is off-script enough
    that we compose rather than trust it into a silent skip."""
    for bad in ("yes", "true", 1, None, ["true"]):
        _mock(json.dumps({
            "has_overlay_text": bad, "confidence": "high", "reason": "r",
        }))
        verdict = await _detect("")
        assert verdict.has_overlay_text is False, bad
        assert verdict.cost_usd > 0, bad    # we were billed; report it


@respx.mock
async def test_missing_flag_key_fails_open() -> None:
    _mock(json.dumps({"confidence": "high", "reason": "no flag key at all"}))

    assert (await _detect("")).has_overlay_text is False


@respx.mock
async def test_malformed_json_fails_open() -> None:
    _mock("this is not JSON at all")

    verdict = await _detect("")

    assert verdict.has_overlay_text is False
    assert verdict.cost_usd > 0


@respx.mock
async def test_json_non_object_fails_open() -> None:
    """Valid JSON, wrong shape. ``json.loads("[1,2]")`` succeeds, so the
    dict check has to be explicit or ``.get`` would raise."""
    _mock("[1, 2, 3]")

    assert (await _detect("")).has_overlay_text is False


@respx.mock
async def test_reason_is_truncated() -> None:
    """``reason`` is free model text that lands in row metadata and logs.
    Cap it so one chatty response can't bloat a 500-row job record."""
    _mock(json.dumps({
        "has_overlay_text": True, "confidence": "high", "reason": "x" * 900,
    }))

    assert len((await _detect("")).reason) == 200


# ── Transport failures fail open ────────────────────────────────────────────


@respx.mock
async def test_server_error_fails_open_with_zero_cost() -> None:
    """A 5xx is retried inside the adapter and then surfaces as an exception.
    The detector swallows it: the row still ships, with the overlay."""
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(500, json={"error": {"message": "boom"}}),
    )

    verdict = await _detect("")

    assert verdict.has_overlay_text is False
    assert verdict.cost_usd == 0.0
    assert verdict.reason == ""


@respx.mock
async def test_auth_error_fails_open() -> None:
    """A bad key must degrade the tab to its old behaviour, not break it."""
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": {"message": "bad key"}}),
    )

    verdict = await _detect("")

    assert verdict.has_overlay_text is False
    assert verdict.cost_usd == 0.0


# ── Request shape ───────────────────────────────────────────────────────────


@respx.mock
async def test_request_carries_image_and_asks_for_strict_json() -> None:
    """Guards the adapter passthrough: without ``response_format`` the model
    is free to wrap its answer in prose and every parse would fail open,
    silently reducing the feature to a no-op that still costs money."""
    route = _mock(json.dumps({
        "has_overlay_text": False, "confidence": "high", "reason": "clean",
    }))

    await _detect("")

    body = json.loads(route.calls[0].request.content)
    assert body["model"] == MODEL_TEXT_DETECT
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 0.0

    parts = body["messages"][0]["content"]
    image_parts = [p for p in parts if p["type"] == "image_url"]
    assert len(image_parts) == 1
    # Inline base64, never a URL — OpenAI's fetcher is blocked by Facebook's
    # ad endpoint, which is where most Manual Images come from.
    assert image_parts[0]["image_url"]["url"] == f"data:image/png;base64,{B64}"
    assert image_parts[0]["image_url"]["detail"] == "high"
