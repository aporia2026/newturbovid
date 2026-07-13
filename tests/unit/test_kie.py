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
    COST_NANO_BANANA_2_1K_USD,
    COST_NANO_BANANA_2_USD,
    COST_NANO_BANANA_EDIT_USD,
    COST_RECRAFT_UPSCALE_USD,
    COST_SEEDANCE_PRO_720P_4S_USD,
    COST_SEEDANCE_PRO_720P_8S_USD,
    MODEL_GPT_IMAGE_2,
    MODEL_NANO_BANANA_2,
    MODEL_NANO_BANANA_EDIT,
    MODEL_RECRAFT_UPSCALE,
    MODEL_SEEDANCE_PRO,
    SEEDANCE_DEFAULT_ASPECT_RATIO,
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
    nano_banana_2_image_to_image,
    nano_banana_2_text_to_image,
    nano_banana_edit,
    nearest_seedance_aspect_ratio,
    recraft_crisp_upscale,
    seedance_image_to_video,
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
    # 1-key pool keeps the retry loop from also cooling a second key —
    # this test focuses on the cooldown-on-HTTP-429 invariant. Multi-key
    # retry behavior is covered by the dedicated tests below.
    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(
        return_value=httpx.Response(429, text="rate limited")
    )
    pool = KiePool(keys=[KEY_A], cooldown_seconds=300.0)
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        with pytest.raises(KieRateLimitError):
            await client.create_task(MODEL_NANO_BANANA_EDIT, {"prompt": "x"})

    # KEY_A was the only key in the pool; it must be on cooldown now.
    assert pool._states[0].cooldown_until > 0


@respx.mock
async def test_create_task_body_code_429_marks_cooldown_and_raises() -> None:
    """kie.ai signals per-key rate-limit via HTTP 200 + body code 429.

    Treat it identically to HTTP 429: cooldown the key + raise
    ``KieRateLimitError``. Without this, the same tripped key gets re-acquired
    by the gpt-image-2 fallback (and by every other parallel row) until the
    whole image fallback chain collapses (observed 2026-06-11). Plan:
    ``_plans/2026-06-11-kie-body-code-429.md``.
    """
    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(
        return_value=httpx.Response(
            200,
            json={"code": 429, "msg": "rate limit exceeded"},
        )
    )
    pool = KiePool(keys=[KEY_A], cooldown_seconds=300.0)
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        with pytest.raises(KieRateLimitError) as exc_info:
            await client.create_task(MODEL_NANO_BANANA_EDIT, {"prompt": "x"})

    # The kie ``msg`` field is surfaced for debugging, not swallowed.
    assert "rate limit exceeded" in str(exc_info.value)
    assert pool._states[0].cooldown_until > 0


@respx.mock
async def test_create_task_429_retries_other_keys_then_succeeds() -> None:
    """First key trips 429 → cooldown + retry next key → success.

    Ensures the in-`create_task` retry loop falls forward inside the same call
    instead of bubbling immediately. Result: the row gets its task_id, the
    tripped key is on cooldown, the healthy key stays available.
    """
    call_count = {"n": 0}

    def _submit(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(
                200, json={"code": 429, "msg": "rate limit exceeded"}
            )
        return httpx.Response(200, json={"code": 200, "data": {"taskId": "task-ok"}})

    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(side_effect=_submit)
    pool = KiePool(keys=[KEY_A, KEY_B], cooldown_seconds=300.0)
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        pinned = await client.create_task(MODEL_NANO_BANANA_EDIT, {"prompt": "x"})

    real, suffix = _unpin_task_id(pinned)
    assert real == "task-ok"
    # Second attempt won with KEY_B; KEY_A is on cooldown.
    assert suffix == KEY_B[-12:]
    assert pool._states[0].cooldown_until > 0    # KEY_A
    assert pool._states[1].cooldown_until == 0.0    # KEY_B


@respx.mock
async def test_create_task_429_all_keys_exhausted_raises() -> None:
    """When every key in the pool trips 429, the last error propagates so the
    outer fallback chain (gpt-image-2 → AtlasCloud) can take over."""
    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(
        return_value=httpx.Response(
            200, json={"code": 429, "msg": "rate limit exceeded"}
        )
    )
    pool = KiePool(keys=[KEY_A, KEY_B], cooldown_seconds=300.0)
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        with pytest.raises(KieRateLimitError):
            await client.create_task(MODEL_NANO_BANANA_EDIT, {"prompt": "x"})

    # Both keys cooled — the retry loop tried each before giving up.
    assert pool._states[0].cooldown_until > 0
    assert pool._states[1].cooldown_until > 0


def test_pool_exposes_key_count() -> None:
    """Stable property the retry loop in ``create_task`` reads to cap attempts."""
    assert KiePool(keys=[KEY_A]).key_count == 1
    assert KiePool(keys=[KEY_A, KEY_B]).key_count == 2
    assert KiePool(keys=[KEY_A, KEY_B, KEY_C]).key_count == 3


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


# ── poll_task read-back resilience (cartoon "no Seedance clips" bug) ──────────
# A task that SUCCEEDED on kie must never be reported as a failed clip because
# the read-back flapped. Each transient below re-polls; only exhaustion surfaces
# (as KieTimeoutError, which the seedance wrapper then resubmit-retries).


def _poll_success(url: str = "https://cdn/clip.mp4") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "code": 200,
            "data": {"state": "success", "resultJson": json.dumps({"resultUrls": [url]})},
        },
    )


@respx.mock
async def test_poll_task_retries_through_transport_error_then_succeeds() -> None:
    # A network flap on the GET (ReadTimeout is an httpx.TransportError) must
    # NOT kill a clip that finished on kie's side — the next poll gets it.
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        side_effect=[httpx.ReadTimeout("slow poll"), _poll_success()]
    )
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        urls = await client.poll_task(
            _pin_task_id("t", KEY_A), max_attempts=3, delay_seconds=0.0
        )
    assert urls == ["https://cdn/clip.mp4"]


@respx.mock
async def test_poll_task_retries_through_http_429_then_succeeds() -> None:
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        side_effect=[httpx.Response(429, text="rate limited"), _poll_success()]
    )
    pool = KiePool(keys=[KEY_A], cooldown_seconds=300.0)
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        urls = await client.poll_task(
            _pin_task_id("t", KEY_A), max_attempts=3, delay_seconds=0.0
        )
    assert urls == ["https://cdn/clip.mp4"]
    # The poll cooled the tripped key so other callers back off.
    assert pool._states[0].cooldown_until > 0


@respx.mock
async def test_poll_task_retries_through_body_code_429_then_succeeds() -> None:
    # HTTP 200 + body {"code": 429} is kie's OTHER rate-limit signal. Unhandled,
    # data is empty, state reads None, and the poll silently spins to timeout on
    # a clip that is READY — the exact reported failure. It must re-poll instead.
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        side_effect=[
            httpx.Response(200, json={"code": 429, "msg": "rate limit exceeded"}),
            _poll_success(),
        ]
    )
    pool = KiePool(keys=[KEY_A], cooldown_seconds=300.0)
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        urls = await client.poll_task(
            _pin_task_id("t", KEY_A), max_attempts=3, delay_seconds=0.0
        )
    assert urls == ["https://cdn/clip.mp4"]
    assert pool._states[0].cooldown_until > 0


@respx.mock
async def test_poll_task_transport_error_exhausts_as_timeout() -> None:
    # Sustained transport errors surface as KieTimeoutError (NOT a bare httpx
    # error and NOT a generic KieError), so the seedance wrapper resubmit-retries.
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        side_effect=[httpx.ReadTimeout("a"), httpx.ReadTimeout("b")]
    )
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        with pytest.raises(KieTimeoutError):
            await client.poll_task(
                _pin_task_id("t", KEY_A), max_attempts=2, delay_seconds=0.0
            )


@respx.mock
async def test_poll_task_sustained_body_429_exhausts_as_timeout() -> None:
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        return_value=httpx.Response(200, json={"code": 429, "msg": "rate limit"})
    )
    pool = KiePool(keys=[KEY_A], cooldown_seconds=300.0)
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        with pytest.raises(KieTimeoutError):
            await client.poll_task(
                _pin_task_id("t", KEY_A), max_attempts=2, delay_seconds=0.0
            )


@respx.mock
async def test_poll_task_unparseable_body_retries_then_succeeds() -> None:
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        side_effect=[httpx.Response(200, text="<html>gateway error</html>"), _poll_success()]
    )
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        urls = await client.poll_task(
            _pin_task_id("t", KEY_A), max_attempts=3, delay_seconds=0.0
        )
    assert urls == ["https://cdn/clip.mp4"]


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


def _submit_resp(task_id: str) -> httpx.Response:
    return httpx.Response(200, json={"code": 200, "data": {"taskId": task_id}})


def _fail_resp(msg: str) -> httpx.Response:
    return httpx.Response(
        200, json={"code": 200, "data": {"state": "fail", "failMsg": msg}}
    )


def _success_resp(url: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "code": 200,
            "data": {
                "state": "success",
                "resultJson": json.dumps({"resultUrls": [url]}),
            },
        },
    )


@respx.mock
async def test_recraft_upscale_retries_transient_fail_then_succeeds() -> None:
    # First task fails with a server-side blip ("internal error, please try
    # again later." — the exact reported failure); the resubmit succeeds. On the
    # old single-shot wrapper this raised and killed the row.
    submit = respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(
        side_effect=[_submit_resp("t1"), _submit_resp("t2")]
    )
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        side_effect=[
            _fail_resp("internal error, please try again later."),
            _success_resp("https://cdn/up.png"),
        ]
    )
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        url, cost = await recraft_crisp_upscale(
            client,
            image_url="https://cdn/collage.png",
            max_attempts=1,
            delay_seconds=0.0,
            retries=2,
            retry_backoff_seconds=0.0,
        )
    assert url == "https://cdn/up.png"
    assert cost == COST_RECRAFT_UPSCALE_USD
    assert submit.call_count == 2    # one retry after the transient fail


@respx.mock
async def test_recraft_upscale_does_not_retry_nontransient_fail() -> None:
    # A deterministic rejection (content policy) must NOT be resubmitted — a
    # retry would only repeat it and burn money.
    submit = respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(
        side_effect=[_submit_resp("t1")]
    )
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        side_effect=[_fail_resp("content policy violation: firearms")]
    )
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        with pytest.raises(KieTaskFailedError):
            await recraft_crisp_upscale(
                client,
                image_url="https://cdn/collage.png",
                max_attempts=1,
                delay_seconds=0.0,
                retries=2,
                retry_backoff_seconds=0.0,
            )
    assert submit.call_count == 1    # no retry on a deterministic failure


@respx.mock
async def test_recraft_upscale_raises_after_exhausting_retries() -> None:
    # A sustained transient outage exhausts the retries and surfaces the error,
    # which the row processor catches to fall back to the un-upscaled collage.
    submit = respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(
        side_effect=[_submit_resp("t1"), _submit_resp("t2")]
    )
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        side_effect=[
            _fail_resp("internal error, please try again later."),
            _fail_resp("internal error, please try again later."),
        ]
    )
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        with pytest.raises(KieTaskFailedError):
            await recraft_crisp_upscale(
                client,
                image_url="https://cdn/collage.png",
                max_attempts=1,
                delay_seconds=0.0,
                retries=1,
                retry_backoff_seconds=0.0,
            )
    assert submit.call_count == 2    # initial attempt + 1 retry, both failed


# ── Cartoon-mode wrappers ────────────────────────────────────────────────────


def _capture_submit_then_succeed(result_url: str) -> list[dict]:
    """Mock createTask (capturing the body) + a successful recordInfo. Returns the
    list the request bodies are appended to."""
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
                    "resultJson": json.dumps({"resultUrls": [result_url]}),
                },
            },
        )
    )
    return captured


@respx.mock
async def test_nano_banana_2_text_to_image_has_no_seed() -> None:
    captured = _capture_submit_then_succeed("https://cdn/t2i.png")
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        url, cost = await nano_banana_2_text_to_image(
            client, prompt="A cartoon scene", aspect_ratio="9:16",
            resolution="1K", max_attempts=2, delay_seconds=0.0,
        )
    assert url == "https://cdn/t2i.png"
    assert cost == COST_NANO_BANANA_2_1K_USD
    body = captured[0]
    assert body["model"] == MODEL_NANO_BANANA_2
    assert "image_input" not in body["input"]    # text-to-image: no seed
    assert body["input"]["aspect_ratio"] == "9:16"
    assert body["input"]["resolution"] == "1K"


@respx.mock
async def test_nano_banana_2_image_to_image_chains_on_source() -> None:
    captured = _capture_submit_then_succeed("https://cdn/i2i.png")
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        url, cost = await nano_banana_2_image_to_image(
            client, source_image_url="https://cdn/shot1.png",
            prompt="Same character, new scene", aspect_ratio="9:16",
            resolution="1K", max_attempts=2, delay_seconds=0.0,
        )
    assert url == "https://cdn/i2i.png"
    assert cost == COST_NANO_BANANA_2_1K_USD
    body = captured[0]
    assert body["input"]["image_input"] == ["https://cdn/shot1.png"]


@respx.mock
async def test_seedance_sends_duration_as_string() -> None:
    captured = _capture_submit_then_succeed("https://cdn/clip.mp4")
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        url, cost = await seedance_image_to_video(
            client, image_url="https://cdn/shot1.png", prompt="gentle motion",
            aspect_ratio="9:16", duration=4, resolution="720p",
            max_attempts=2, delay_seconds=0.0,
        )
    assert url == "https://cdn/clip.mp4"
    assert cost == COST_SEEDANCE_PRO_720P_4S_USD
    body = captured[0]
    assert body["model"] == MODEL_SEEDANCE_PRO
    assert body["input"]["input_urls"] == ["https://cdn/shot1.png"]
    # The API rejects an integer duration — it MUST be a string.
    assert body["input"]["duration"] == "4"
    assert isinstance(body["input"]["duration"], str)
    # Seedance 1.5 Pro is a native audio-visual model; audio is OFF unless a
    # caller opts in, so every clip is silent by default (cheaper + on-spec).
    assert body["input"]["generate_audio"] is False


@respx.mock
async def test_seedance_generate_audio_opt_in() -> None:
    captured = _capture_submit_then_succeed("https://cdn/clip.mp4")
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        await seedance_image_to_video(
            client, image_url="https://cdn/shot1.png", prompt="gentle motion",
            aspect_ratio="9:16", duration=4, resolution="720p",
            generate_audio=True, max_attempts=2, delay_seconds=0.0,
        )
    assert captured[0]["input"]["generate_audio"] is True


@respx.mock
async def test_seedance_8s_returns_long_tier_cost() -> None:
    # Cartoon mode's long-VO path requests Seedance 8s for the last shot — the
    # billed cost must scale to the 8s tier so cost reporting stays accurate.
    captured = _capture_submit_then_succeed("https://cdn/clip8.mp4")
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        url, cost = await seedance_image_to_video(
            client, image_url="https://cdn/shot1.png", prompt="gentle motion",
            aspect_ratio="9:16", duration=8, resolution="720p",
            max_attempts=2, delay_seconds=0.0,
        )
    assert url == "https://cdn/clip8.mp4"
    assert cost == COST_SEEDANCE_PRO_720P_8S_USD
    assert captured[0]["input"]["duration"] == "8"


@respx.mock
async def test_seedance_resubmits_on_poll_timeout() -> None:
    # First task never finishes (poll times out -> KieTimeoutError). The wrapper
    # must resubmit a FRESH task and return its clip, not drop the shot. This is
    # the difference between the row's "no Seedance clips produced" failure and a
    # recovered clip.
    submits = {"n": 0}

    def _submit(request: httpx.Request) -> httpx.Response:
        submits["n"] += 1
        task_id = "t1" if submits["n"] == 1 else "t2"
        return httpx.Response(200, json={"code": 200, "data": {"taskId": task_id}})

    def _poll(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("taskId") == "t2":
            return _poll_success("https://cdn/recovered.mp4")
        return httpx.Response(200, json={"code": 200, "data": {"state": "generating"}})

    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(side_effect=_submit)
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(side_effect=_poll)

    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        url, cost = await seedance_image_to_video(
            client, image_url="https://cdn/shot1.png", prompt="gentle motion",
            aspect_ratio="9:16", duration=4, resolution="720p",
            max_attempts=1, delay_seconds=0.0, retries=1,
        )
    assert url == "https://cdn/recovered.mp4"
    assert cost == COST_SEEDANCE_PRO_720P_4S_USD
    assert submits["n"] == 2    # original timed out, resubmit recovered it


@respx.mock
async def test_seedance_resubmits_on_submit_transport_error() -> None:
    # A network flap on the SUBMIT (not the poll) must also resubmit rather than
    # drop the shot.
    submits = {"n": 0}

    def _submit(request: httpx.Request) -> httpx.Response:
        submits["n"] += 1
        if submits["n"] == 1:
            raise httpx.ConnectError("submit flap")
        return httpx.Response(200, json={"code": 200, "data": {"taskId": "t2"}})

    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(side_effect=_submit)
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        return_value=_poll_success("https://cdn/after_flap.mp4")
    )

    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        url, cost = await seedance_image_to_video(
            client, image_url="https://cdn/shot1.png", prompt="gentle motion",
            aspect_ratio="9:16", duration=4, resolution="720p",
            max_attempts=2, delay_seconds=0.0, retries=1,
        )
    assert url == "https://cdn/after_flap.mp4"
    assert cost == COST_SEEDANCE_PRO_720P_4S_USD
    assert submits["n"] == 2


@respx.mock
async def test_seedance_does_not_resubmit_on_task_failure() -> None:
    # A genuine model failure (state=fail) is deterministic — resubmitting would
    # just repeat it and double-bill. It must NOT be retried.
    submits = {"n": 0}

    def _submit(request: httpx.Request) -> httpx.Response:
        submits["n"] += 1
        return httpx.Response(200, json={"code": 200, "data": {"taskId": "t1"}})

    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(side_effect=_submit)
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        return_value=httpx.Response(
            200, json={"code": 200, "data": {"state": "fail", "failMsg": "bad prompt"}}
        )
    )

    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        with pytest.raises(KieTaskFailedError):
            await seedance_image_to_video(
                client, image_url="https://cdn/shot1.png", prompt="gentle motion",
                aspect_ratio="9:16", duration=4, resolution="720p",
                max_attempts=2, delay_seconds=0.0, retries=1,
            )
    assert submits["n"] == 1    # no resubmit on a deterministic failure


# ── Seedance aspect-ratio clamp (the real "no clips" bug: 2:3 rejected) ──────


def test_nearest_seedance_aspect_ratio_snaps_disallowed() -> None:
    # 2:3 (0.667) is closer to 3:4 (0.75) than to 9:16 (0.5625).
    assert nearest_seedance_aspect_ratio("2:3") == "3:4"
    # 3:2 (1.5) is closer to 4:3 (1.333) than to 16:9 (1.778).
    assert nearest_seedance_aspect_ratio("3:2") == "4:3"
    # 4:5 (0.8) is closer to 3:4 (0.75) than to 1:1.
    assert nearest_seedance_aspect_ratio("4:5") == "3:4"


def test_nearest_seedance_aspect_ratio_passes_allowed_through() -> None:
    for allowed in ("9:16", "3:4", "1:1", "4:3", "16:9", "21:9"):
        assert nearest_seedance_aspect_ratio(allowed) == allowed


def test_nearest_seedance_aspect_ratio_handles_pixels_and_garbage() -> None:
    # Native-probed pixels: 1080x1620 = 0.667 -> 3:4.
    assert nearest_seedance_aspect_ratio("1080x1620") == "3:4"
    # 1920x1080 = 1.778 -> 16:9 (exact match numerically).
    assert nearest_seedance_aspect_ratio("1920x1080") == "16:9"
    # Sheets leading-zero cast still parses.
    assert nearest_seedance_aspect_ratio("09:16") == "9:16"
    # Unparseable -> safe default, never a crash.
    assert nearest_seedance_aspect_ratio("") == SEEDANCE_DEFAULT_ASPECT_RATIO
    assert nearest_seedance_aspect_ratio("portrait") == SEEDANCE_DEFAULT_ASPECT_RATIO
    assert nearest_seedance_aspect_ratio("0:0") == SEEDANCE_DEFAULT_ASPECT_RATIO


@respx.mock
async def test_seedance_clamps_disallowed_aspect_before_submit() -> None:
    # The bug: the row's 2:3 flowed straight to Seedance and was rejected at
    # submit ("aspect_ratio is not within the range of allowed options"). The
    # wrapper must send 3:4 instead so the clip is actually produced.
    captured = _capture_submit_then_succeed("https://cdn/clip.mp4")
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        url, _cost = await seedance_image_to_video(
            client, image_url="https://cdn/shot1.png", prompt="gentle motion",
            aspect_ratio="2:3", duration=4, resolution="720p",
            max_attempts=2, delay_seconds=0.0,
        )
    assert url == "https://cdn/clip.mp4"
    assert captured[0]["input"]["aspect_ratio"] == "3:4"    # NOT "2:3"


@respx.mock
async def test_seedance_leaves_allowed_aspect_untouched() -> None:
    captured = _capture_submit_then_succeed("https://cdn/clip.mp4")
    pool = KiePool(keys=[KEY_A])
    async with KieClient(pool=pool, base_url=KIE_BASE) as client:
        await seedance_image_to_video(
            client, image_url="https://cdn/shot1.png", prompt="gentle motion",
            aspect_ratio="9:16", duration=4, resolution="720p",
            max_attempts=2, delay_seconds=0.0,
        )
    assert captured[0]["input"]["aspect_ratio"] == "9:16"


# ── Sanity on the model names + cost constants (catch accidental renames) ────


def test_model_names_pinned() -> None:
    assert MODEL_NANO_BANANA_EDIT == "google/nano-banana-edit"
    assert MODEL_RECRAFT_UPSCALE == "recraft/crisp-upscale"
    assert MODEL_SEEDANCE_PRO == "bytedance/seedance-1.5-pro"


def test_cost_constants_are_positive() -> None:
    assert COST_NANO_BANANA_EDIT_USD > 0
    assert COST_RECRAFT_UPSCALE_USD > 0
    assert COST_NANO_BANANA_2_1K_USD > 0
    assert COST_SEEDANCE_PRO_720P_4S_USD > 0
    # 8s tier should bill more than 4s and not less than 2x in the model we use.
    assert COST_SEEDANCE_PRO_720P_8S_USD > COST_SEEDANCE_PRO_720P_4S_USD
    assert COST_SEEDANCE_PRO_720P_8S_USD >= 2 * COST_SEEDANCE_PRO_720P_4S_USD * 0.95
