"""Tests for the kie.ai adapter.

All network calls are mocked via respx — no real kie.ai requests.

Covers:
  - KiePool round-robin
  - KiePool cooldown / skip / find-by-suffix
  - Task ID pinning + unpinning
  - KieClient.create_task: success, 401, 429 (with cooldown), non-200
  - KieClient.poll_task: success, fail, timeout, key pinning
  - High-level wrappers: nano_banana_edit, recraft_crisp_upscale
  - Cost values
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from bulkvid.adapters.kie import (
    COST_GPT_IMAGE_2_USD,
    COST_NANO_BANANA_2_USD,
    COST_NANO_BANANA_EDIT_USD,
    COST_RECRAFT_UPSCALE_USD,
    MODEL_GPT_IMAGE_2,
    MODEL_NANO_BANANA_2,
    MODEL_NANO_BANANA_EDIT,
    MODEL_RECRAFT_UPSCALE,
    KieAuthError,
    KieClient,
    KiePool,
    KieRateLimitError,
    KieTaskFailedError,
    KieTimeoutError,
    _pin_task_id,
    _unpin_task_id,
    gpt_image_2,
    nano_banana_2,
    nano_banana_edit,
    recraft_crisp_upscale,
)

# 24-char test keys → last-12 suffixes are deterministic and distinct.
KEY_A = "kie_test_key_AAAAAAAAAAAA"
KEY_B = "kie_test_key_BBBBBBBBBBBB"
KEY_C = "kie_test_key_CCCCCCCCCCCC"

KIE_BASE = "https://api.kie.ai"


# ── KiePool ──────────────────────────────────────────────────────────────────


async def test_pool_round_robins_keys() -> None:
    pool = KiePool(keys=[KEY_A, KEY_B, KEY_C])
    keys = [await pool.acquire() for _ in range(7)]
    assert keys == [KEY_A, KEY_B, KEY_C, KEY_A, KEY_B, KEY_C, KEY_A]


async def test_pool_skips_cooldown_key() -> None:
    pool = KiePool(keys=[KEY_A, KEY_B], cooldown_seconds=300.0)
    await pool.mark_rate_limited(KEY_A)
    # Both acquires should return KEY_B since KEY_A is in cooldown.
    assert await pool.acquire() == KEY_B
    assert await pool.acquire() == KEY_B


def test_pool_rejects_empty_keys() -> None:
    with pytest.raises(ValueError):
        KiePool(keys=[])


def test_pool_find_by_suffix() -> None:
    pool = KiePool(keys=[KEY_A, KEY_B])
    assert pool.find_by_suffix(KEY_A[-12:]) == KEY_A
    assert pool.find_by_suffix(KEY_B[-12:]) == KEY_B
    assert pool.find_by_suffix("notpresent12") is None


# ── Task-ID pinning ──────────────────────────────────────────────────────────


def test_pin_and_unpin_task_id() -> None:
    pinned = _pin_task_id("task-xyz", KEY_A)
    real, suffix = _unpin_task_id(pinned)
    assert real == "task-xyz"
    assert suffix == KEY_A[-12:]


def test_unpin_handles_unpinned_id() -> None:
    real, suffix = _unpin_task_id("plain-task-id")
    assert real == "plain-task-id"
    assert suffix is None


# ── KieClient.create_task ────────────────────────────────────────────────────


@respx.mock
async def test_create_task_success_returns_pinned_id() -> None:
    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(
        return_value=httpx.Response(
            200,
            json={"code": 200, "data": {"taskId": "task-abc"}},
        )
    )
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        pinned = await client.create_task(MODEL_NANO_BANANA_EDIT, {"prompt": "x"})

    real, suffix = _unpin_task_id(pinned)
    assert real == "task-abc"
    assert suffix == KEY_A[-12:]


@respx.mock
async def test_create_task_401_raises_auth_error() -> None:
    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(
        return_value=httpx.Response(401, text="unauthorized")
    )
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        with pytest.raises(KieAuthError):
            await client.create_task(MODEL_NANO_BANANA_EDIT, {"prompt": "x"})


@respx.mock
async def test_create_task_429_marks_cooldown_and_raises() -> None:
    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(
        return_value=httpx.Response(429, text="rate limited")
    )
    pool = KiePool(keys=[KEY_A, KEY_B], cooldown_seconds=300.0)
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        with pytest.raises(KieRateLimitError):
            await client.create_task(MODEL_NANO_BANANA_EDIT, {"prompt": "x"})

    # KEY_A was acquired first (round-robin head); it should now be in cooldown.
    # Next acquire returns KEY_B (KEY_A skipped).
    assert await pool.acquire() == KEY_B


@respx.mock
async def test_create_task_missing_task_id_raises() -> None:
    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(
        return_value=httpx.Response(200, json={"code": 200, "data": {}})
    )
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        with pytest.raises(Exception):
            await client.create_task(MODEL_NANO_BANANA_EDIT, {"prompt": "x"})


# ── KieClient.poll_task ──────────────────────────────────────────────────────


@respx.mock
async def test_poll_task_success_returns_urls() -> None:
    result_json = json.dumps({"resultUrls": ["https://cdn.kie/img.png"]})
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 200,
                "data": {"state": "success", "resultJson": result_json},
            },
        )
    )
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        urls = await client.poll_task(
            _pin_task_id("task-1", KEY_A),
            max_attempts=2,
            delay_seconds=0.0,
        )
    assert urls == ["https://cdn.kie/img.png"]


@respx.mock
async def test_poll_task_fail_raises() -> None:
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 200,
                "data": {"state": "fail", "failMsg": "bad prompt"},
            },
        )
    )
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        with pytest.raises(KieTaskFailedError):
            await client.poll_task(
                _pin_task_id("task-1", KEY_A),
                max_attempts=2,
                delay_seconds=0.0,
            )


@respx.mock
async def test_poll_task_timeout_raises() -> None:
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        return_value=httpx.Response(
            200,
            json={"code": 200, "data": {"state": "generating"}},
        )
    )
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        with pytest.raises(KieTimeoutError):
            await client.poll_task(
                _pin_task_id("task-1", KEY_A),
                max_attempts=3,
                delay_seconds=0.0,
            )


@respx.mock
async def test_poll_task_routes_to_pinned_key() -> None:
    # Two keys in the pool. We pin to KEY_B. The Authorization header on the
    # poll MUST be KEY_B's bearer, NOT KEY_A's (which would be next in round-robin).
    captured_auth: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured_auth.append(request.headers.get("authorization", ""))
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "state": "success",
                    "resultJson": json.dumps({"resultUrls": ["u"]}),
                },
            },
        )

    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(side_effect=_handler)

    pool = KiePool(keys=[KEY_A, KEY_B])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        await client.poll_task(
            _pin_task_id("task-1", KEY_B),
            max_attempts=2,
            delay_seconds=0.0,
        )

    assert captured_auth == [f"Bearer {KEY_B}"]


# ── High-level wrappers ──────────────────────────────────────────────────────


@respx.mock
async def test_nano_banana_edit_returns_url_and_cost() -> None:
    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(
        return_value=httpx.Response(
            200, json={"code": 200, "data": {"taskId": "t1"}}
        )
    )
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "state": "success",
                    "resultJson": json.dumps({"resultUrls": ["https://cdn/x.png"]}),
                },
            },
        )
    )
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        url, cost = await nano_banana_edit(
            client,
            source_image_url="https://src/seed.png",
            prompt="2x2 collage",
            aspect_ratio="9:16",
            max_attempts=2,
            delay_seconds=0.0,
        )
    assert url == "https://cdn/x.png"
    assert cost == COST_NANO_BANANA_EDIT_USD


@respx.mock
async def test_nano_banana_2_sends_correct_model_and_fields() -> None:
    captured: list[dict] = []

    def _submit(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 200, "data": {"taskId": "t1"}})

    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(side_effect=_submit)
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "state": "success",
                    "resultJson": json.dumps({"resultUrls": ["https://cdn/nb2.png"]}),
                },
            },
        )
    )
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        url, cost = await nano_banana_2(
            client,
            source_image_url="https://src/seed.png",
            prompt="2x2 ad collage with CTA",
            aspect_ratio="9:16",
            resolution="2K",
            max_attempts=2,
            delay_seconds=0.0,
        )
    assert url == "https://cdn/nb2.png"
    assert cost == COST_NANO_BANANA_2_USD
    body = captured[0]
    assert body["model"] == MODEL_NANO_BANANA_2
    # Nano Banana 2 uses image_input (array) + aspect_ratio + resolution.
    assert body["input"]["image_input"] == ["https://src/seed.png"]
    assert body["input"]["aspect_ratio"] == "9:16"
    assert body["input"]["resolution"] == "2K"


@respx.mock
async def test_gpt_image_2_sends_correct_model_and_input_urls() -> None:
    captured: list[dict] = []

    def _submit(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 200, "data": {"taskId": "t1"}})

    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(side_effect=_submit)
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "state": "success",
                    "resultJson": json.dumps({"resultUrls": ["https://cdn/gpt.png"]}),
                },
            },
        )
    )
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        url, cost = await gpt_image_2(
            client,
            source_image_url="https://src/seed.png",
            prompt="2x2 ad collage with CTA",
            aspect_ratio="9:16",
            max_attempts=2,
            delay_seconds=0.0,
        )
    assert url == "https://cdn/gpt.png"
    assert cost == COST_GPT_IMAGE_2_USD
    body = captured[0]
    assert body["model"] == MODEL_GPT_IMAGE_2
    # GPT Image 2 image-to-image uses input_urls (NOT image_input).
    assert body["input"]["input_urls"] == ["https://src/seed.png"]
    assert body["input"]["aspect_ratio"] == "9:16"


@respx.mock
async def test_recraft_crisp_upscale_returns_url_and_cost() -> None:
    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(
        return_value=httpx.Response(
            200, json={"code": 200, "data": {"taskId": "t2"}}
        )
    )
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "state": "success",
                    "resultJson": json.dumps({"resultUrls": ["https://cdn/up.png"]}),
                },
            },
        )
    )
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        url, cost = await recraft_crisp_upscale(
            client,
            image_url="https://cdn/collage.png",
            max_attempts=2,
            delay_seconds=0.0,
        )
    assert url == "https://cdn/up.png"
    assert cost == COST_RECRAFT_UPSCALE_USD


# ── Sanity on the model names + cost constants (catch accidental renames) ────


def test_model_names_pinned() -> None:
    assert MODEL_NANO_BANANA_EDIT == "google/nano-banana-edit"
    assert MODEL_RECRAFT_UPSCALE == "recraft/crisp-upscale"


def test_cost_constants_are_positive() -> None:
    assert COST_NANO_BANANA_EDIT_USD > 0
    assert COST_RECRAFT_UPSCALE_USD > 0
