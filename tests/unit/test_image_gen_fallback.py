"""Tests for the kie.ai, AtlasCloud, and MuAPI image-gen fallbacks."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from bulkvid.adapters.atlascloud import AtlasCloudClient, AtlasTaskFailedError
from bulkvid.adapters.kie import KieClient, KieError, KiePool
from bulkvid.pipeline.image_gen import edit_with_fallback, generate_with_fallback

KIE_BASE = "https://api.kie.ai"
ATLAS_BASE = "https://api.atlascloud.ai"
KEY = "kie_test_key_AAAAAAAAAAAA"


def _kie_client() -> KieClient:
    return KieClient(pool=KiePool(keys=[KEY]), base_url=KIE_BASE)


def _atlas_client() -> AtlasCloudClient:
    return AtlasCloudClient(api_key="apikey-x", base_url=ATLAS_BASE)


# ── Kie success → no fallback ──────────────────────────────────────────────


@respx.mock
async def test_kie_success_skips_atlas() -> None:
    # Kie submit + poll succeed.
    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(
        return_value=httpx.Response(
            200, json={"code": 200, "data": {"taskId": "task-1"}}
        )
    )
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "state": "success",
                    "resultJson": json.dumps({"resultUrls": ["https://cdn.kie/img.png"]}),
                },
            },
        )
    )
    # No atlas mock — if it were called the test would fail with a respx error.

    async with _kie_client() as kie, _atlas_client() as atlas:
        url, cost = await edit_with_fallback(
            kie=kie, atlas=atlas,
            source_image_url="https://src/seed.png",
            prompt="x",
            aspect_ratio="9:16",
        )
    assert url == "https://cdn.kie/img.png"
    # Cost is the kie cost.
    assert cost > 0


# ── Kie fail → atlas fallback ──────────────────────────────────────────────


@respx.mock
async def test_kie_failure_falls_back_to_atlas() -> None:
    # Kie task FAILS.
    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(
        return_value=httpx.Response(
            200, json={"code": 200, "data": {"taskId": "task-1"}}
        )
    )
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 200,
                "data": {"state": "fail", "failMsg": "rate limited"},
            },
        )
    )
    # Atlas succeeds.
    respx.post(f"{ATLAS_BASE}/api/v1/model/generateImage").mock(
        return_value=httpx.Response(200, json={"prediction_id": "p-1"})
    )
    respx.get(f"{ATLAS_BASE}/api/v1/model/prediction/p-1").mock(
        return_value=httpx.Response(
            200,
            json={"status": "completed", "outputs": ["https://cdn.atlas/img.png"]},
        )
    )

    async with _kie_client() as kie, _atlas_client() as atlas:
        url, cost = await edit_with_fallback(
            kie=kie, atlas=atlas,
            source_image_url="https://src/seed.png",
            prompt="x",
            aspect_ratio="9:16",
        )
    assert url == "https://cdn.atlas/img.png"
    assert cost > 0


# ── Kie fail + no atlas configured → propagate kie error ───────────────────


@respx.mock
async def test_no_atlas_propagates_kie_error() -> None:
    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(
        return_value=httpx.Response(
            200, json={"code": 200, "data": {"taskId": "task-1"}}
        )
    )
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        return_value=httpx.Response(
            200, json={"code": 200, "data": {"state": "fail", "failMsg": "oops"}}
        )
    )

    async with _kie_client() as kie:
        with pytest.raises(KieError):
            await edit_with_fallback(
                kie=kie, atlas=None,
                source_image_url="https://src/seed.png",
                prompt="x",
                aspect_ratio="9:16",
            )


# ── Both fail → KieError with combined message ─────────────────────────────


@respx.mock
async def test_both_fail_raises_with_combined_message() -> None:
    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(
        return_value=httpx.Response(
            200, json={"code": 200, "data": {"taskId": "task-1"}}
        )
    )
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        return_value=httpx.Response(
            200, json={"code": 200, "data": {"state": "fail", "failMsg": "kie down"}}
        )
    )
    respx.post(f"{ATLAS_BASE}/api/v1/model/generateImage").mock(
        return_value=httpx.Response(200, json={"prediction_id": "p-1"})
    )
    respx.get(f"{ATLAS_BASE}/api/v1/model/prediction/p-1").mock(
        return_value=httpx.Response(
            200, json={"status": "failed", "error": "atlas dead too"}
        )
    )

    async with _kie_client() as kie, _atlas_client() as atlas:
        with pytest.raises(KieError) as exc:
            await edit_with_fallback(
                kie=kie, atlas=atlas,
                source_image_url="https://src/seed.png",
                prompt="x",
                aspect_ratio="9:16",
            )

    # Both errors mentioned so operator can debug.
    assert "kie" in str(exc.value).lower()
    assert "atlas" in str(exc.value).lower()


async def test_text_generation_uses_muapi_after_kie_and_atlas_fail(monkeypatch) -> None:
    from bulkvid.pipeline import image_gen

    async def _fail_t2i(kie, prompt, aspect_ratio, resolution="2K", **_):
        raise KieError("kie t2i down")

    monkeypatch.setattr(image_gen, "nano_banana_2_text_to_image", _fail_t2i)

    class _Atlas:
        async def text_to_image(self, prompt, aspect_ratio, **_):
            raise AtlasTaskFailedError("atlas t2i down")

    class _MuAPI:
        async def text_to_image(self, prompt, aspect_ratio, **_):
            return "https://cdn.muapi.test/image.png", 0.003

    url, cost = await generate_with_fallback(
        kie=SimpleNamespace(),
        atlas=_Atlas(),
        muapi=_MuAPI(),
        prompt="A valid prompt",
        aspect_ratio="9:16",
    )
    assert url == "https://cdn.muapi.test/image.png"
    assert cost == 0.003


async def test_text_generation_uses_muapi_without_atlas(monkeypatch) -> None:
    from bulkvid.pipeline import image_gen

    async def _fail_t2i(kie, prompt, aspect_ratio, resolution="2K", **_):
        raise KieError("kie t2i down")

    monkeypatch.setattr(image_gen, "nano_banana_2_text_to_image", _fail_t2i)

    class _MuAPI:
        async def text_to_image(self, prompt, aspect_ratio, **_):
            return "https://cdn.muapi.test/no-atlas.png", 0.003

    url, cost = await generate_with_fallback(
        kie=SimpleNamespace(),
        atlas=None,
        muapi=_MuAPI(),
        prompt="A valid prompt",
        aspect_ratio="9:16",
    )
    assert url == "https://cdn.muapi.test/no-atlas.png"
    assert cost == 0.003
