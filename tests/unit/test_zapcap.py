"""Tests for the ZapCap adapter.

All network calls mocked via respx. Covers:
  - upload_video: 201 success, 401, non-201, missing id
  - create_task: 200/201 success, missing taskId
  - poll_task: completed (with downloadUrl), failed, timeout, pending->retry
  - caption_video end-to-end
  - x-api-key header is used (NOT Authorization: Bearer)
  - render options serialize to the camelCase shape ZapCap expects
  - constructor rejects empty api_key / template_id
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from bulkvid.adapters.zapcap import (
    COST_ZAPCAP_PER_VIDEO_USD,
    ZapCapAuthError,
    ZapCapClient,
    ZapCapError,
    ZapCapRenderOptions,
    ZapCapStyleOptions,
    ZapCapSubsOptions,
    ZapCapTaskFailedError,
    ZapCapTimeoutError,
    _render_options_to_api,
    default_style_options,
    default_subs_options,
)

API_KEY = "zapcap_test_key"
TEMPLATE_ID = "test-template-id"
BASE = "https://api.zapcap.ai"


# ── Render-options serialization ────────────────────────────────────────────


def test_default_options_match_existing_production() -> None:
    # The defaults match what stage_6_zapcap_processing.py uses today (verified
    # against refs/stage_6_zapcap_processing.py L900-915).
    s = default_subs_options()
    assert s.emoji is True
    assert s.emoji_animation is True
    assert s.emphasize_keywords is True

    t = default_style_options()
    assert t.top == 70
    assert t.font_size == 42
    assert t.font_weight == 700
    assert t.font_color == "#FFFFFF"
    assert t.stroke_color == "#000000"


def test_render_options_serialize_camelcase() -> None:
    opts = ZapCapRenderOptions(
        subs=ZapCapSubsOptions(emoji=False, emoji_animation=True, emphasize_keywords=True),
        style=ZapCapStyleOptions(top=55, font_size=36, font_weight=600),
    )
    api = _render_options_to_api(opts)
    # snake_case in Python; camelCase in the API payload.
    assert api["subsOptions"]["emoji"] is False
    assert api["subsOptions"]["emojiAnimation"] is True
    assert api["subsOptions"]["emphasizeKeywords"] is True
    assert api["styleOptions"]["top"] == 55
    assert api["styleOptions"]["fontSize"] == 36
    assert api["styleOptions"]["fontWeight"] == 600
    # And no snake_case keys leak through.
    assert "emoji_animation" not in api["subsOptions"]
    assert "font_size" not in api["styleOptions"]


# ── Constructor validation ──────────────────────────────────────────────────


def test_constructor_rejects_empty_api_key() -> None:
    with pytest.raises(ValueError):
        ZapCapClient(api_key="", template_id=TEMPLATE_ID)


def test_constructor_rejects_empty_template_id() -> None:
    with pytest.raises(ValueError):
        ZapCapClient(api_key=API_KEY, template_id="")


# ── upload_video ────────────────────────────────────────────────────────────


@respx.mock
async def test_upload_video_success_returns_id() -> None:
    respx.post(f"{BASE}/videos").mock(
        return_value=httpx.Response(201, json={"id": "video-abc"})
    )
    async with ZapCapClient(api_key=API_KEY, template_id=TEMPLATE_ID, base_url=BASE) as c:
        video_id = await c.upload_video(b"\x00\x01\x02fake mp4 bytes")
    assert video_id == "video-abc"


@respx.mock
async def test_upload_video_sends_x_api_key_header() -> None:
    captured: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("x-api-key", ""))
        # Sanity: NOT Authorization: Bearer.
        assert not request.headers.get("authorization", "").startswith("Bearer")
        return httpx.Response(201, json={"id": "v1"})

    respx.post(f"{BASE}/videos").mock(side_effect=_handler)
    async with ZapCapClient(api_key=API_KEY, template_id=TEMPLATE_ID, base_url=BASE) as c:
        await c.upload_video(b"data")
    assert captured == [API_KEY]


@respx.mock
async def test_upload_video_401_raises_auth_error() -> None:
    respx.post(f"{BASE}/videos").mock(
        return_value=httpx.Response(401, text="unauthorized")
    )
    async with ZapCapClient(api_key=API_KEY, template_id=TEMPLATE_ID, base_url=BASE) as c:
        with pytest.raises(ZapCapAuthError):
            await c.upload_video(b"x")


@respx.mock
async def test_upload_video_non_201_raises() -> None:
    respx.post(f"{BASE}/videos").mock(
        return_value=httpx.Response(500, text="server error")
    )
    async with ZapCapClient(api_key=API_KEY, template_id=TEMPLATE_ID, base_url=BASE) as c:
        with pytest.raises(ZapCapError):
            await c.upload_video(b"x")


@respx.mock
async def test_upload_video_missing_id_raises() -> None:
    respx.post(f"{BASE}/videos").mock(return_value=httpx.Response(201, json={}))
    async with ZapCapClient(api_key=API_KEY, template_id=TEMPLATE_ID, base_url=BASE) as c:
        with pytest.raises(ZapCapError):
            await c.upload_video(b"x")


# ── create_task ─────────────────────────────────────────────────────────────


@respx.mock
async def test_create_task_success_with_taskId() -> None:
    respx.post(f"{BASE}/videos/v1/task").mock(
        return_value=httpx.Response(200, json={"taskId": "task-1"})
    )
    async with ZapCapClient(api_key=API_KEY, template_id=TEMPLATE_ID, base_url=BASE) as c:
        task_id = await c.create_task("v1", language="he")
    assert task_id == "task-1"


@respx.mock
async def test_create_task_success_with_id_field() -> None:
    # Some endpoints return {"id": ...} instead of {"taskId": ...} — both accepted.
    respx.post(f"{BASE}/videos/v1/task").mock(
        return_value=httpx.Response(201, json={"id": "task-alt"})
    )
    async with ZapCapClient(api_key=API_KEY, template_id=TEMPLATE_ID, base_url=BASE) as c:
        task_id = await c.create_task("v1", language="en")
    assert task_id == "task-alt"


@respx.mock
async def test_create_task_sends_expected_body() -> None:
    captured_body: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured_body.append(json.loads(request.content))
        return httpx.Response(200, json={"taskId": "t1"})

    respx.post(f"{BASE}/videos/v1/task").mock(side_effect=_handler)
    async with ZapCapClient(api_key=API_KEY, template_id=TEMPLATE_ID, base_url=BASE) as c:
        await c.create_task(
            "v1",
            language="HE",   # uppercase input
            render_options=ZapCapRenderOptions(
                subs=ZapCapSubsOptions(emoji=False),
                style=ZapCapStyleOptions(font_size=50),
            ),
        )
    body = captured_body[0]
    assert body["templateId"] == TEMPLATE_ID
    assert body["language"] == "he"          # normalised to lowercase
    assert body["autoApprove"] is True
    assert body["renderOptions"]["subsOptions"]["emoji"] is False
    assert body["renderOptions"]["styleOptions"]["fontSize"] == 50


@respx.mock
async def test_create_task_missing_id_raises() -> None:
    respx.post(f"{BASE}/videos/v1/task").mock(
        return_value=httpx.Response(200, json={"foo": "bar"})
    )
    async with ZapCapClient(api_key=API_KEY, template_id=TEMPLATE_ID, base_url=BASE) as c:
        with pytest.raises(ZapCapError):
            await c.create_task("v1")


# ── poll_task ───────────────────────────────────────────────────────────────


@respx.mock
async def test_poll_task_completed_returns_download_url() -> None:
    respx.get(f"{BASE}/videos/v1/task/t1").mock(
        return_value=httpx.Response(
            200,
            json={"status": "completed", "downloadUrl": "https://zc/out.mp4"},
        )
    )
    async with ZapCapClient(api_key=API_KEY, template_id=TEMPLATE_ID, base_url=BASE) as c:
        url = await c.poll_task("v1", "t1", max_attempts=2, delay_seconds=0.0)
    assert url == "https://zc/out.mp4"


@respx.mock
async def test_poll_task_failed_raises() -> None:
    respx.get(f"{BASE}/videos/v1/task/t1").mock(
        return_value=httpx.Response(
            200,
            json={"status": "failed", "error": "bad audio"},
        )
    )
    async with ZapCapClient(api_key=API_KEY, template_id=TEMPLATE_ID, base_url=BASE) as c:
        with pytest.raises(ZapCapTaskFailedError) as exc:
            await c.poll_task("v1", "t1", max_attempts=2, delay_seconds=0.0)
    assert "bad audio" in str(exc.value)


@respx.mock
async def test_poll_task_timeout_raises() -> None:
    respx.get(f"{BASE}/videos/v1/task/t1").mock(
        return_value=httpx.Response(200, json={"status": "transcribing"})
    )
    async with ZapCapClient(api_key=API_KEY, template_id=TEMPLATE_ID, base_url=BASE) as c:
        with pytest.raises(ZapCapTimeoutError):
            await c.poll_task("v1", "t1", max_attempts=3, delay_seconds=0.0)


@respx.mock
async def test_poll_task_completed_without_download_url_raises() -> None:
    respx.get(f"{BASE}/videos/v1/task/t1").mock(
        return_value=httpx.Response(200, json={"status": "completed"})
    )
    async with ZapCapClient(api_key=API_KEY, template_id=TEMPLATE_ID, base_url=BASE) as c:
        with pytest.raises(ZapCapError):
            await c.poll_task("v1", "t1", max_attempts=2, delay_seconds=0.0)


# ── caption_video end-to-end ────────────────────────────────────────────────


@respx.mock
async def test_caption_video_end_to_end_returns_url_and_cost() -> None:
    respx.post(f"{BASE}/videos").mock(
        return_value=httpx.Response(201, json={"id": "v-end"})
    )
    respx.post(f"{BASE}/videos/v-end/task").mock(
        return_value=httpx.Response(200, json={"taskId": "t-end"})
    )
    respx.get(f"{BASE}/videos/v-end/task/t-end").mock(
        return_value=httpx.Response(
            200,
            json={"status": "completed", "downloadUrl": "https://zc/final.mp4"},
        )
    )

    async with ZapCapClient(api_key=API_KEY, template_id=TEMPLATE_ID, base_url=BASE) as c:
        url, cost = await c.caption_video(
            video_bytes=b"fake-mp4-data",
            language="he",
            max_attempts=2,
            delay_seconds=0.0,
        )

    assert url == "https://zc/final.mp4"
    assert cost == COST_ZAPCAP_PER_VIDEO_USD


# ── Cost constant sanity ────────────────────────────────────────────────────


def test_cost_constant_positive() -> None:
    assert COST_ZAPCAP_PER_VIDEO_USD > 0
