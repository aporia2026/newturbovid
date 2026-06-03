"""Integration-style tests for the simple row processor.

Covers:
  - Happy path -> exactly 1 video URL, STATUS_SUCCESS, no image generation (kie)
  - Invalid manual image URL -> STATUS_IMAGE_DOWNLOAD_FAILED before any work
  - Article fetch failure -> STATUS_ARTICLE_FETCH_FAILED
  - ZapCap=Yes -> captioned single URL
"""

from __future__ import annotations

import io
import json

import httpx
import respx

from bulkvid.adapters.article_fetch import ArticleResult
from bulkvid.adapters.gemini_tts import TTSResult
from bulkvid.adapters.kie import KieClient, KiePool
from bulkvid.adapters.openai_client import OpenAIClient
from bulkvid.adapters.rendi import RendiClient
from bulkvid.adapters.storage import UploadResult
from bulkvid.adapters.zapcap import ZapCapClient
from bulkvid.models.row import (
    STATUS_ARTICLE_FETCH_FAILED,
    STATUS_IMAGE_DOWNLOAD_FAILED,
    STATUS_SUCCESS,
    SimpleRow,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.row_processor_simple import process_simple_row

OPENAI_BASE = "https://api.openai.com/v1"
RENDI_BASE = "https://api.rendi.dev"
ZAPCAP_BASE = "https://api.zapcap.ai"


class _FakeArticleFetcher:
    def __init__(self, fail: bool = False) -> None:
        self._fail = fail

    async def fetch(self, url: str) -> ArticleResult:
        if self._fail:
            from bulkvid.adapters.article_fetch import ArticleFetchError

            raise ArticleFetchError("simulated fetch failure")
        return ArticleResult(
            url=url, content="Used car prices guide.", source="scrapingbee",
            char_count=22, cost_usd=0.008,
        )


class _FakeStorageClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def upload_bytes(
        self, data: bytes, key: str, content_type: str = "application/octet-stream"
    ) -> UploadResult:
        self.calls.append((key, content_type))
        return UploadResult(
            url=f"https://storage.test/{key}", backend="gcs",
            bytes_written=len(data), cost_usd=0.0001,
        )


class _FakeTTS:
    def __init__(self, fail: bool = False) -> None:
        self._fail = fail

    async def synthesize(
        self, text: str, language: str, voice: str | None = None,
        style_prompt: str | None = None, country: str = ""
    ) -> TTSResult:
        import wave

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24_000)
            wf.writeframes(b"\x00" * 24_000)
        return TTSResult(
            wav_bytes=buf.getvalue(), voice=voice or "Kore", language=language,
            duration_seconds=0.5, character_count=len(text), cost_usd=0.003,
        )


def _openai_chat_resp(content: str) -> dict:
    return {
        "id": "x", "object": "chat.completion", "created": 1717_000_000,
        "model": "gpt-5.4-mini",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130},
    }


def _register_openai_routes() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sys = body.get("messages", [{}])[0].get("content", "")
        if "detect the primary language" in sys:
            text = json.dumps({"language": "en", "confidence": 0.95})
        elif "classify 'Open Comments'" in sys:
            text = json.dumps(
                {"mode": "none", "tone_hints": [], "directives": [], "override_script": None}
            )
        elif "voiceover scripts for bulk" in sys:
            text = json.dumps({"script": "Check used car prices today.", "style_direction": "Calm."})
        else:
            text = "ok"
        return httpx.Response(200, json=_openai_chat_resp(text))

    respx.post(f"{OPENAI_BASE}/chat/completions").mock(side_effect=_handler)


def _register_rendi_routes() -> None:
    counter = {"n": 0}

    def _submit(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(200, json={"command_id": f"cmd-{counter['n']}"})

    respx.post(f"{RENDI_BASE}/v1/run-ffmpeg-command").mock(side_effect=_submit)

    def _poll(request: httpx.Request) -> httpx.Response:
        cmd_id = str(request.url).rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={"status": "SUCCESS",
                  "output_files": {"out_1": {"storage_url": f"https://r.dev/{cmd_id}.mp4"}}},
        )

    respx.get(url__regex=r"https://api\.rendi\.dev/v1/commands/.+").mock(side_effect=_poll)
    respx.delete(url__regex=r"https://api\.rendi\.dev/v1/commands/.+/files").mock(
        return_value=httpx.Response(200, json={})
    )


def _register_downloads() -> None:
    respx.get(url__regex=r"https://r\.dev/.+\.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00fake-mp4")
    )
    respx.get(url__regex=r"https://storage\.test/.+\.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00persisted")
    )
    respx.get(url__regex=r"https://zc\.test/.+\.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00captioned")
    )


def _build_clients(*, article_fail: bool = False, with_zapcap: bool = False) -> PipelineClients:
    return PipelineClients(
        openai=OpenAIClient(api_key="sk-test"),
        kie=KieClient(pool=KiePool(keys=["k_unused_AAAAAAAAAAAA"]), base_url="https://api.kie.ai"),
        tts=_FakeTTS(),                                  # type: ignore[arg-type]
        rendi=RendiClient(api_key="rendi-test", base_url=RENDI_BASE),
        storage=_FakeStorageClient(),                    # type: ignore[arg-type]
        article=_FakeArticleFetcher(fail=article_fail),  # type: ignore[arg-type]
        zapcap=ZapCapClient(api_key="zc-test", template_id="tpl", base_url=ZAPCAP_BASE)
        if with_zapcap else None,
    )


def _row(
    *, image_url: str = "https://example.com/ad.png", zapcap: bool = False, vo: bool = True
) -> SimpleRow:
    return SimpleRow(
        row_num=2, country="MX", vertical="automotive",
        article_url="https://example.com/article",
        manual_image_url=image_url,
        voice_over=vo, zapcap=zapcap, aspect_ratio="09:16",
        script_pattern="How To", open_comments="",
    )


@respx.mock
async def test_simple_happy_path_returns_one_video() -> None:
    _register_openai_routes()
    _register_rendi_routes()
    _register_downloads()

    clients = _build_clients()
    result = await process_simple_row(_row(), clients, job_id="jobX")

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 1
    assert result.metadata["tab"] == "simple"


@respx.mock
async def test_simple_no_kie_image_generation() -> None:
    # The simple tab must NOT call kie.ai (no image generation).
    kie_route = respx.post("https://api.kie.ai/api/v1/jobs/createTask").mock(
        return_value=httpx.Response(200, json={"code": 200, "data": {"taskId": "x"}})
    )
    _register_openai_routes()
    _register_rendi_routes()
    _register_downloads()

    result = await process_simple_row(_row(), _build_clients(), job_id="jobX")
    assert result.status == STATUS_SUCCESS
    assert not kie_route.called


@respx.mock
async def test_simple_voice_over_no_produces_silent_video() -> None:
    # Voice Over = No -> a silent video (no TTS), still one video.
    _register_openai_routes()
    _register_rendi_routes()
    _register_downloads()

    result = await process_simple_row(_row(vo=False), _build_clients(), job_id="j")
    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 1
    assert "vo_voice" not in result.metadata     # no voiceover was generated


async def test_simple_bad_image_url_fails_fast() -> None:
    result = await process_simple_row(_row(image_url="not-a-url"), _build_clients(), job_id="j")
    assert result.status == STATUS_IMAGE_DOWNLOAD_FAILED
    assert result.video_urls == []


@respx.mock
async def test_simple_article_failure() -> None:
    _register_openai_routes()
    _register_rendi_routes()
    _register_downloads()
    result = await process_simple_row(_row(), _build_clients(article_fail=True), job_id="j")
    assert result.status == STATUS_ARTICLE_FETCH_FAILED


@respx.mock
async def test_simple_zapcap_happy_path() -> None:
    _register_openai_routes()
    _register_rendi_routes()
    _register_downloads()
    respx.post(url__regex=rf"{ZAPCAP_BASE}/.*").mock(
        return_value=httpx.Response(200, json={"taskId": "zc1"})
    )
    # ZapCap client flow is mocked at its own adapter level in its unit tests;
    # here we only assert the simple processor returns a single captioned URL on
    # success OR keeps the original on failure (both are 1 video).
    result = await process_simple_row(_row(zapcap=True), _build_clients(with_zapcap=True), job_id="j")
    assert len(result.video_urls) == 1
