"""Tests for the AtlasCloud adapter."""

from __future__ import annotations

import httpx
import pytest
import respx

from bulkvid.adapters.atlascloud import (
    COST_ATLAS_EDIT_USD,
    COST_ATLAS_GENERATE_USD,
    AtlasAuthError,
    AtlasCloudClient,
    AtlasError,
    AtlasTaskFailedError,
    AtlasTimeoutError,
    size_for_ratio,
)

API_KEY = "apikey-test"
BASE = "https://api.atlascloud.ai"


# ── size_for_ratio ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("input_str", "expected"),
    [
        ("9:16", "1024x1792"),
        ("09:16", "1024x1792"),
        ("1:1", "1024x1024"),
        ("16:9", "1792x1024"),
        ("4:5", "1024x1280"),
        ("auto", "1024x1792"),
        ("", "1024x1792"),
        ("garbage", "1024x1792"),
        ("1280x720", "1280x720"),
    ],
)
def test_size_for_ratio(input_str: str, expected: str) -> None:
    assert size_for_ratio(input_str) == expected


# ── Constructor ─────────────────────────────────────────────────────────────


def test_constructor_rejects_empty_api_key() -> None:
    with pytest.raises(ValueError):
        AtlasCloudClient(api_key="")


# ── submit ──────────────────────────────────────────────────────────────────


@respx.mock
async def test_submit_returns_prediction_id() -> None:
    respx.post(f"{BASE}/api/v1/model/generateImage").mock(
        return_value=httpx.Response(200, json={"prediction_id": "pred-abc"})
    )
    async with AtlasCloudClient(api_key=API_KEY, base_url=BASE) as c:
        pid = await c.submit("test prompt", size="1024x1024")
    assert pid == "pred-abc"


@respx.mock
async def test_submit_sends_bearer_auth() -> None:
    captured: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={"prediction_id": "p1"})

    respx.post(f"{BASE}/api/v1/model/generateImage").mock(side_effect=_handler)
    async with AtlasCloudClient(api_key=API_KEY, base_url=BASE) as c:
        await c.submit("x")
    assert captured == [f"Bearer {API_KEY}"]


@respx.mock
async def test_submit_includes_image_urls_when_provided() -> None:
    captured: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"prediction_id": "p1"})

    respx.post(f"{BASE}/api/v1/model/generateImage").mock(side_effect=_handler)
    async with AtlasCloudClient(api_key=API_KEY, base_url=BASE) as c:
        await c.submit(
            "test prompt",
            image_urls=["https://example.com/seed.png"],
            size="1024x1024",
        )

    body = captured[0]
    assert body["image_urls"] == ["https://example.com/seed.png"]
    assert body["size"] == "1024x1024"
    assert body["prompt"] == "test prompt"


@respx.mock
async def test_submit_401_raises_auth_error() -> None:
    respx.post(f"{BASE}/api/v1/model/generateImage").mock(
        return_value=httpx.Response(401, text="unauthorized")
    )
    async with AtlasCloudClient(api_key=API_KEY, base_url=BASE) as c:
        with pytest.raises(AtlasAuthError):
            await c.submit("x")


@respx.mock
async def test_submit_missing_prediction_id_raises() -> None:
    respx.post(f"{BASE}/api/v1/model/generateImage").mock(
        return_value=httpx.Response(200, json={})
    )
    async with AtlasCloudClient(api_key=API_KEY, base_url=BASE) as c:
        with pytest.raises(AtlasError):
            await c.submit("x")


# ── poll ────────────────────────────────────────────────────────────────────


@respx.mock
async def test_poll_completed_returns_first_output_url() -> None:
    respx.get(f"{BASE}/api/v1/model/prediction/pred-1").mock(
        return_value=httpx.Response(
            200,
            json={"status": "completed", "outputs": ["https://cdn.atlas/img.png"]},
        )
    )
    async with AtlasCloudClient(api_key=API_KEY, base_url=BASE) as c:
        url = await c.poll("pred-1", max_attempts=2, delay_seconds=0.0)
    assert url == "https://cdn.atlas/img.png"


@respx.mock
async def test_poll_completed_handles_dict_outputs() -> None:
    respx.get(f"{BASE}/api/v1/model/prediction/pred-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "completed",
                "outputs": [{"url": "https://cdn.atlas/x.png"}],
            },
        )
    )
    async with AtlasCloudClient(api_key=API_KEY, base_url=BASE) as c:
        url = await c.poll("pred-1", max_attempts=2, delay_seconds=0.0)
    assert url == "https://cdn.atlas/x.png"


@respx.mock
async def test_poll_failed_raises() -> None:
    respx.get(f"{BASE}/api/v1/model/prediction/pred-1").mock(
        return_value=httpx.Response(
            200, json={"status": "failed", "error": "bad prompt"}
        )
    )
    async with AtlasCloudClient(api_key=API_KEY, base_url=BASE) as c:
        with pytest.raises(AtlasTaskFailedError):
            await c.poll("pred-1", max_attempts=2, delay_seconds=0.0)


@respx.mock
async def test_poll_timeout_raises() -> None:
    respx.get(f"{BASE}/api/v1/model/prediction/pred-1").mock(
        return_value=httpx.Response(200, json={"status": "processing"})
    )
    async with AtlasCloudClient(api_key=API_KEY, base_url=BASE) as c:
        with pytest.raises(AtlasTimeoutError):
            await c.poll("pred-1", max_attempts=3, delay_seconds=0.0)


# ── High-level wrappers ────────────────────────────────────────────────────


@respx.mock
async def test_edit_image_end_to_end() -> None:
    respx.post(f"{BASE}/api/v1/model/generateImage").mock(
        return_value=httpx.Response(200, json={"prediction_id": "p-edit"})
    )
    respx.get(f"{BASE}/api/v1/model/prediction/p-edit").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "completed",
                "outputs": ["https://cdn.atlas/edited.png"],
            },
        )
    )
    async with AtlasCloudClient(api_key=API_KEY, base_url=BASE) as c:
        url, cost = await c.edit_image(
            source_image_url="https://src/seed.png",
            prompt="2x2 collage",
            aspect_ratio="9:16",
            max_attempts=2,
            delay_seconds=0.0,
        )
    assert url == "https://cdn.atlas/edited.png"
    assert cost == COST_ATLAS_EDIT_USD


@respx.mock
async def test_text_to_image_end_to_end() -> None:
    respx.post(f"{BASE}/api/v1/model/generateImage").mock(
        return_value=httpx.Response(200, json={"prediction_id": "p-txt"})
    )
    respx.get(f"{BASE}/api/v1/model/prediction/p-txt").mock(
        return_value=httpx.Response(
            200, json={"status": "completed", "outputs": ["https://cdn.atlas/t.png"]}
        )
    )
    async with AtlasCloudClient(api_key=API_KEY, base_url=BASE) as c:
        url, cost = await c.text_to_image(
            prompt="A sunset", aspect_ratio="1:1",
            max_attempts=2, delay_seconds=0.0,
        )
    assert url == "https://cdn.atlas/t.png"
    assert cost == COST_ATLAS_GENERATE_USD


# ── Cost constants ─────────────────────────────────────────────────────────


def test_cost_constants_positive() -> None:
    assert COST_ATLAS_GENERATE_USD > 0
    assert COST_ATLAS_EDIT_USD > 0
