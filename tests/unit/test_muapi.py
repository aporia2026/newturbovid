"""Tests for the MuAPI OpenAI-compatible image adapter."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from bulkvid.adapters.muapi import (
    COST_MUAPI_FLUX_SCHNELL_USD,
    MuAPIAuthError,
    MuAPIClient,
    MuAPIError,
    build_client_from_settings,
    size_for_ratio,
)
from bulkvid.config import Settings

BASE = "https://api.muapi.ai/v1"
API_KEY = "muapi-test-key"


@pytest.mark.parametrize(
    ("aspect_ratio", "expected"),
    [
        ("9:16", "1024x1792"),
        ("09:16", "1024x1792"),
        ("16:9", "1792x1024"),
        ("1280x720", "1792x1024"),
        ("1:1", "1024x1024"),
        ("4:5", "1024x1792"),
        ("auto", "1024x1792"),
        ("invalid", "1024x1792"),
    ],
)
def test_size_for_ratio_uses_documented_compatibility_sizes(
    aspect_ratio: str, expected: str
) -> None:
    assert size_for_ratio(aspect_ratio) == expected


def test_constructor_rejects_empty_key() -> None:
    with pytest.raises(ValueError):
        MuAPIClient(api_key="")


@respx.mock
async def test_text_to_image_sends_openai_compatible_request() -> None:
    captured: list[tuple[str, dict]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            (
                request.headers.get("authorization", ""),
                json.loads(request.content),
            )
        )
        return httpx.Response(
            200,
            json={"created": 1, "data": [{"url": "https://cdn.muapi.test/image.png"}]},
        )

    respx.post(f"{BASE}/images/generations").mock(side_effect=_handler)
    async with MuAPIClient(api_key=API_KEY, base_url=BASE) as client:
        url, cost = await client.text_to_image("A red paper boat", "09:16")

    auth, body = captured[0]
    assert auth == f"Bearer {API_KEY}"
    assert body == {
        "model": "flux-schnell",
        "prompt": "A red paper boat",
        "n": 1,
        "size": "1024x1792",
    }
    assert url == "https://cdn.muapi.test/image.png"
    assert cost == COST_MUAPI_FLUX_SCHNELL_USD


@respx.mock
async def test_auth_failure_is_distinct() -> None:
    respx.post(f"{BASE}/images/generations").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"})
    )
    async with MuAPIClient(api_key=API_KEY, base_url=BASE) as client:
        with pytest.raises(MuAPIAuthError):
            await client.text_to_image("A valid prompt", "1:1")


@respx.mock
async def test_invalid_success_payload_is_rejected() -> None:
    respx.post(f"{BASE}/images/generations").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    async with MuAPIClient(api_key=API_KEY, base_url=BASE) as client:
        with pytest.raises(MuAPIError, match="did not contain an image"):
            await client.text_to_image("A valid prompt", "1:1")


def test_builder_is_optional_and_uses_settings() -> None:
    assert build_client_from_settings(Settings(_env_file=None)) is None  # type: ignore[call-arg]
    client = build_client_from_settings(
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            MUAPI_API_KEY=API_KEY,
            MUAPI_DEFAULT_MODEL="flux-dev",
            MUAPI_COST_PER_IMAGE_USD=0.02,
        )
    )
    assert client is not None
    assert client._default_model == "flux-dev"
    assert client._cost_per_image_usd == 0.02
