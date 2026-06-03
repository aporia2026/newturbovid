"""Integration-style tests for the Image-VO row processor.

Mocks every dependency the processor talks to:
  - httpx.AsyncClient.get  (article + image + video downloads) via respx
  - OpenAI HTTP                                                via respx
  - kie.ai HTTP                                                via respx
  - Rendi HTTP                                                 via respx
  - ZapCap HTTP                                                via respx
  - Storage / TTS / ArticleFetcher                             via inject

Covers:
  - Happy path Image-VO + VO=Yes + ZapCap=No -> 4 video URLs, STATUS_SUCCESS
  - Article fetch failure -> STATUS_ARTICLE_FETCH_FAILED
  - Image download failure -> STATUS_IMAGE_DOWNLOAD_FAILED
  - Image gen (kie) failure -> STATUS_IMAGE_GEN_FAILED
  - TTS failure -> STATUS_TTS_FAILED
  - Rendi failure -> STATUS_VIDEO_ASSEMBLY_FAILED
  - ZapCap=Yes happy path -> captioned URLs returned
  - ZapCap failure -> STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS with original videos
  - Cost is summed across stages
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from PIL import Image

from bulkvid.adapters.article_fetch import ArticleResult
from bulkvid.adapters.gemini_tts import TTSResult
from bulkvid.adapters.kie import KieClient, KiePool
from bulkvid.adapters.openai_client import OpenAIClient
from bulkvid.adapters.rendi import RendiClient
from bulkvid.adapters.storage import StorageClient, UploadResult
from bulkvid.adapters.zapcap import ZapCapClient
from bulkvid.models.row import (
    STATUS_ARTICLE_FETCH_FAILED,
    STATUS_IMAGE_DOWNLOAD_FAILED,
    STATUS_IMAGE_GEN_FAILED,
    STATUS_SUCCESS,
    STATUS_TTS_FAILED,
    STATUS_VIDEO_ASSEMBLY_FAILED,
    STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS,
    ImageVORow,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.row_processor_image_vo import process_image_vo_row

OPENAI_BASE = "https://api.openai.com/v1"
KIE_BASE = "https://api.kie.ai"
RENDI_BASE = "https://api.rendi.dev"
ZAPCAP_BASE = "https://api.zapcap.ai"
KEY_A = "kie_test_key_AAAAAAAAAAAA"


# ── Fakes for adapter-level dependencies ────────────────────────────────────


class _FakeArticleFetcher:
    def __init__(self, content: str = "Article body about smartwatches.", fail: bool = False):
        self._content = content
        self._fail = fail

    async def fetch(self, url: str) -> ArticleResult:
        if self._fail:
            from bulkvid.adapters.article_fetch import ArticleFetchError

            raise ArticleFetchError("simulated tavily+scrapingbee fail")
        return ArticleResult(
            url=url, content=self._content, source="tavily",
            char_count=len(self._content), cost_usd=0.008,
        )


class _FakeStorageClient:
    def __init__(self, fail: bool = False):
        self._fail = fail
        self.calls: list[tuple[str, str]] = []
        self._counter = 0

    async def upload_bytes(self, data: bytes, key: str, content_type: str = "application/octet-stream") -> UploadResult:
        if self._fail:
            raise RuntimeError("storage down")
        self.calls.append((key, content_type))
        self._counter += 1
        return UploadResult(
            url=f"https://storage.test/{key}",
            backend="s3",
            bytes_written=len(data),
            cost_usd=0.0001,
        )


class _FakeTTS:
    def __init__(self, fail: bool = False):
        self._fail = fail

    async def synthesize(self, text: str, language: str, voice: str | None = None, style_prompt: str | None = None, country: str = "") -> TTSResult:
        if self._fail:
            from bulkvid.adapters.gemini_tts import GeminiTTSNoAudioError

            raise GeminiTTSNoAudioError("simulated tts failure")
        # 0.5s of 24kHz mono 16-bit PCM, wrapped in WAV via stdlib.
        import wave

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24_000)
            wf.writeframes(b"\x00" * (24_000 * 1 * 2 // 2))
        return TTSResult(
            wav_bytes=buf.getvalue(),
            voice=voice or "Kore",
            language=language,
            duration_seconds=0.5,
            character_count=len(text),
            cost_usd=0.003,
        )


# ── Image fixture: a 4-color 2x2 collage as PNG bytes ───────────────────────


def _make_collage_png(size: int = 200) -> bytes:
    half = size // 2
    img = Image.new("RGB", (size, size), (0, 0, 0))
    img.paste(Image.new("RGB", (half, half), (255, 0, 0)), (0, 0))
    img.paste(Image.new("RGB", (half, half), (0, 255, 0)), (half, 0))
    img.paste(Image.new("RGB", (half, half), (0, 0, 255)), (0, half))
    img.paste(Image.new("RGB", (half, half), (255, 255, 0)), (half, half))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── Test fixture: build a fresh PipelineClients + register respx mocks ──────


def _openai_chat_resp(content: str, model: str = "gpt-5.4-mini") -> dict:
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 1717_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130},
    }


def _register_default_openai_routes() -> None:
    """One catch-all OpenAI route returns valid JSON for any chat call."""
    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # Decide payload by inspecting which prompt we got.
        sys = body.get("messages", [{}])[0].get("content", "")
        user = body.get("messages", [{}])[-1].get("content", "")
        user_str = user if isinstance(user, str) else json.dumps(user)

        if "detect the primary language" in sys:
            text = json.dumps({"language": "en", "confidence": 0.95})
        elif "classify 'Open Comments'" in sys:
            text = json.dumps({
                "mode": "none", "tone_hints": [], "directives": [],
                "override_script": None,
            })
        elif "voiceover scripts for bulk" in sys:
            text = json.dumps({
                "script": "Discover the latest smartwatch features in under ten seconds today.",
                "style_direction": "Confident and warm.",
            })
        elif "advertising creative director" in sys:
            text = "Create a 2x2 grid collage. TOP-LEFT: a serene scene."
        elif "Analyse this advertising image" in user_str or "Analyse this advertising image" in sys:
            text = "SUBJECT: a calm beach. STYLE: photographic. COLORS: blue and gold."
        else:
            # Vision call carries the prompt in user.text part.
            if isinstance(user, list):
                user_text = next((p.get("text", "") for p in user if p.get("type") == "text"), "")
                if "Analyse this advertising image" in user_text:
                    text = "SUBJECT: a calm beach. STYLE: photographic."
                else:
                    text = json.dumps({"language": "en", "confidence": 0.95})
            else:
                text = "ok"
        return httpx.Response(200, json=_openai_chat_resp(text))

    respx.post(f"{OPENAI_BASE}/chat/completions").mock(side_effect=_handler)


def _register_default_kie_routes() -> None:
    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(
        return_value=httpx.Response(
            200, json={"code": 200, "data": {"taskId": "kie-task-1"}}
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


def _register_default_rendi_routes() -> None:
    submit_route = respx.post(f"{RENDI_BASE}/v1/run-ffmpeg-command")
    counter = {"n": 0}

    def _submit(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(200, json={"command_id": f"cmd-{counter['n']}"})

    submit_route.mock(side_effect=_submit)

    def _poll(request: httpx.Request) -> httpx.Response:
        # Extract command_id from URL path.
        cmd_id = str(request.url).rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={
                "status": "SUCCESS",
                "output_files": {
                    "out_1": {"storage_url": f"https://r.dev/{cmd_id}.mp4"}
                },
            },
        )

    respx.get(url__regex=r"https://api\.rendi\.dev/v1/commands/.+").mock(side_effect=_poll)
    # Best-effort storage cleanup fired after videos are persisted.
    respx.delete(url__regex=r"https://api\.rendi\.dev/v1/commands/.+/files").mock(
        return_value=httpx.Response(200, json={})
    )


def _register_seed_image_download(collage_bytes: bytes) -> None:
    """Both source image AND upscaled image downloads return the collage."""
    respx.get("https://example.com/seed.png").mock(
        return_value=httpx.Response(200, content=collage_bytes)
    )
    respx.get("https://cdn.kie/img.png").mock(
        return_value=httpx.Response(200, content=collage_bytes)
    )
    respx.get(url__regex=r"https://r\.dev/.+\.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00fake-mp4")
    )
    respx.get(url__regex=r"https://zc\.test/.+\.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00captioned-mp4")
    )
    # Storage URLs re-downloaded by the ZapCap stage.
    respx.get(url__regex=r"https://storage\.test/.+\.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00persisted-video")
    )


def _build_clients(
    *,
    article_fail: bool = False,
    storage_fail: bool = False,
    tts_fail: bool = False,
    with_zapcap: bool = False,
) -> PipelineClients:
    return PipelineClients(
        openai=OpenAIClient(api_key="sk-test"),
        kie=KieClient(pool=KiePool(keys=[KEY_A]), base_url=KIE_BASE),
        tts=_FakeTTS(fail=tts_fail),                # type: ignore[arg-type]
        rendi=RendiClient(api_key="rendi-test", base_url=RENDI_BASE),
        storage=_FakeStorageClient(fail=storage_fail),    # type: ignore[arg-type]
        article=_FakeArticleFetcher(fail=article_fail),    # type: ignore[arg-type]
        zapcap=ZapCapClient(
            api_key="zc-test", template_id="tpl-x", base_url=ZAPCAP_BASE
        ) if with_zapcap else None,
    )


def _row(*, zapcap: bool = False, vo: bool = True) -> ImageVORow:
    return ImageVORow(
        row_num=2,
        country="US",
        vertical="tech",
        article_url="https://example.com/article",
        manual_image_url="https://example.com/seed.png",
        voice_over=vo,
        zapcap=zapcap,
        aspect_ratio="9:16",
        script_pattern="How To",
        open_comments="",
    )


# ── Tests ────────────────────────────────────────────────────────────────────


@respx.mock
async def test_happy_path_imageVO_returns_four_video_urls() -> None:
    _register_default_openai_routes()
    _register_default_kie_routes()
    _register_default_rendi_routes()
    _register_seed_image_download(_make_collage_png(200))

    clients = _build_clients()
    result = await process_image_vo_row(_row(), clients)

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 4
    assert result.error is None
    # Each video URL should be a storage URL (post-upload).
    for v in result.video_urls:
        assert v.startswith("https://storage.test/bulkvid/videos/")
    # Cost was summed across stages.
    assert result.cost_usd > 0
    # Elapsed is positive.
    assert result.elapsed_seconds >= 0


@respx.mock
async def test_article_fetch_failure_returns_specific_status() -> None:
    _register_default_openai_routes()
    _register_default_kie_routes()
    _register_default_rendi_routes()
    _register_seed_image_download(_make_collage_png(200))

    clients = _build_clients(article_fail=True)
    result = await process_image_vo_row(_row(), clients)

    assert result.status == STATUS_ARTICLE_FETCH_FAILED
    assert result.video_urls == []
    assert "simulated" in (result.error or "")


@respx.mock
async def test_image_download_failure_returns_specific_status() -> None:
    _register_default_openai_routes()
    _register_default_kie_routes()
    _register_default_rendi_routes()
    # No respx mock for the seed image -> respx will pass through and fail.
    respx.get("https://example.com/seed.png").mock(return_value=httpx.Response(404))

    clients = _build_clients()
    result = await process_image_vo_row(_row(), clients)

    assert result.status == STATUS_IMAGE_DOWNLOAD_FAILED
    assert result.video_urls == []


@respx.mock
async def test_kie_failure_returns_image_gen_failed() -> None:
    _register_default_openai_routes()
    _register_default_rendi_routes()
    _register_seed_image_download(_make_collage_png(200))

    # kie.ai returns task-failed
    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(
        return_value=httpx.Response(
            200, json={"code": 200, "data": {"taskId": "kie-task-1"}}
        )
    )
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        return_value=httpx.Response(
            200,
            json={"code": 200, "data": {"state": "fail", "failMsg": "bad prompt"}},
        )
    )

    clients = _build_clients()
    result = await process_image_vo_row(_row(), clients)

    assert result.status == STATUS_IMAGE_GEN_FAILED
    assert result.video_urls == []


@respx.mock
async def test_tts_failure_returns_tts_failed() -> None:
    _register_default_openai_routes()
    _register_default_kie_routes()
    _register_default_rendi_routes()
    _register_seed_image_download(_make_collage_png(200))

    clients = _build_clients(tts_fail=True)
    result = await process_image_vo_row(_row(), clients)

    assert result.status == STATUS_TTS_FAILED
    assert result.video_urls == []


@respx.mock
async def test_rendi_failure_returns_video_assembly_failed() -> None:
    _register_default_openai_routes()
    _register_default_kie_routes()
    _register_seed_image_download(_make_collage_png(200))

    # Rendi submit ok, poll says FAILED with stderr.
    respx.post(f"{RENDI_BASE}/v1/run-ffmpeg-command").mock(
        return_value=httpx.Response(200, json={"command_id": "cmd-fail"})
    )
    respx.get(url__regex=r"https://api\.rendi\.dev/v1/commands/.+").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "FAILED",
                "error": {"message": "ffmpeg broke", "stderr": "Invalid input"},
            },
        )
    )

    clients = _build_clients()
    result = await process_image_vo_row(_row(), clients)

    assert result.status == STATUS_VIDEO_ASSEMBLY_FAILED
    assert result.video_urls == []


@respx.mock
async def test_zapcap_happy_path_returns_captioned_urls() -> None:
    _register_default_openai_routes()
    _register_default_kie_routes()
    _register_default_rendi_routes()
    _register_seed_image_download(_make_collage_png(200))

    # ZapCap upload + task + poll
    respx.post(f"{ZAPCAP_BASE}/videos").mock(
        return_value=httpx.Response(201, json={"id": "v-zc"})
    )
    respx.post(url__regex=r"https://api\.zapcap\.ai/videos/.+/task").mock(
        return_value=httpx.Response(200, json={"taskId": "t-zc"})
    )
    respx.get(url__regex=r"https://api\.zapcap\.ai/videos/.+/task/.+").mock(
        return_value=httpx.Response(
            200,
            json={"status": "completed", "downloadUrl": "https://zc.test/final.mp4"},
        )
    )

    clients = _build_clients(with_zapcap=True)
    result = await process_image_vo_row(_row(zapcap=True), clients)

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 4
    # Final URLs come from our storage AFTER captioning.
    for v in result.video_urls:
        assert "videos_captioned" in v
    assert result.metadata.get("zapcap_applied") is True


@respx.mock
async def test_zapcap_failure_keeps_uncaptioned_videos() -> None:
    _register_default_openai_routes()
    _register_default_kie_routes()
    _register_default_rendi_routes()
    _register_seed_image_download(_make_collage_png(200))

    # ZapCap upload fails with 500.
    respx.post(f"{ZAPCAP_BASE}/videos").mock(
        return_value=httpx.Response(500, text="server down")
    )

    clients = _build_clients(with_zapcap=True)
    result = await process_image_vo_row(_row(zapcap=True), clients)

    assert result.status == STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS
    assert len(result.video_urls) == 4
    for v in result.video_urls:
        # These are the UNcaptioned storage URLs.
        assert "videos_captioned" not in v
        assert "videos/" in v
    assert result.metadata.get("zapcap_applied") is False
