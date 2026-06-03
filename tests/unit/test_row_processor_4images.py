"""Integration-style tests for the 4Images-VO2 row processor.

Covers:
  - Happy path with how_many=2 -> 2 video URLs, STATUS_SUCCESS
  - Happy path with how_many=4 -> 4 video URLs
  - Invalid how_many (0 or 5) -> STATUS_INTERNAL_ERROR
  - Invalid image URL -> STATUS_IMAGE_DOWNLOAD_FAILED before any work
  - Mismatch (how_many=3 but only 2 URLs) -> STATUS_IMAGE_DOWNLOAD_FAILED
  - Article fetch failure -> STATUS_ARTICLE_FETCH_FAILED
  - Rendi resize failure -> STATUS_IMAGE_GEN_FAILED
  - TTS failure -> STATUS_TTS_FAILED
  - Rendi assembly failure -> STATUS_VIDEO_ASSEMBLY_FAILED
  - ZapCap=Yes happy path -> captioned URLs
  - ZapCap failure -> STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS
"""

from __future__ import annotations

import io
import json

import httpx
import pytest
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
    STATUS_IMAGE_GEN_FAILED,
    STATUS_INTERNAL_ERROR,
    STATUS_SUCCESS,
    STATUS_TTS_FAILED,
    STATUS_VIDEO_ASSEMBLY_FAILED,
    STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS,
    FourImagesVO2Row,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.row_processor_4images import process_4images_vo2_row

OPENAI_BASE = "https://api.openai.com/v1"
RENDI_BASE = "https://api.rendi.dev"
ZAPCAP_BASE = "https://api.zapcap.ai"


# ── Fakes (lifted from the Image-VO test patterns) ──────────────────────────


class _FakeArticleFetcher:
    def __init__(self, fail: bool = False) -> None:
        self._fail = fail

    async def fetch(self, url: str) -> ArticleResult:
        if self._fail:
            from bulkvid.adapters.article_fetch import ArticleFetchError

            raise ArticleFetchError("simulated fetch failure")
        return ArticleResult(
            url=url,
            content="Article body about smartwatches and fitness tracking.",
            source="tavily",
            char_count=53,
            cost_usd=0.008,
        )


class _FakeStorageClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def upload_bytes(
        self, data: bytes, key: str, content_type: str = "application/octet-stream"
    ) -> UploadResult:
        self.calls.append((key, content_type))
        return UploadResult(
            url=f"https://storage.test/{key}",
            backend="s3",
            bytes_written=len(data),
            cost_usd=0.0001,
        )


class _FakeTTS:
    def __init__(self, fail: bool = False) -> None:
        self._fail = fail

    async def synthesize(
        self, text: str, language: str, voice: str | None = None,
        style_prompt: str | None = None, country: str = ""
    ) -> TTSResult:
        if self._fail:
            from bulkvid.adapters.gemini_tts import GeminiTTSNoAudioError

            raise GeminiTTSNoAudioError("simulated tts failure")
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
            text = json.dumps(
                {
                    "script": "Discover the latest smartwatch in ten seconds.",
                    "style_direction": "Confident.",
                }
            )
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
            json={
                "status": "SUCCESS",
                "output_files": {"out_1": {"storage_url": f"https://r.dev/{cmd_id}.mp4"}},
            },
        )

    respx.get(url__regex=r"https://api\.rendi\.dev/v1/commands/.+").mock(side_effect=_poll)
    # Best-effort storage cleanup fired after videos are persisted.
    respx.delete(url__regex=r"https://api\.rendi\.dev/v1/commands/.+/files").mock(
        return_value=httpx.Response(200, json={})
    )


def _register_video_downloads() -> None:
    respx.get(url__regex=r"https://r\.dev/.+\.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00fake-mp4")
    )
    respx.get(url__regex=r"https://storage\.test/.+\.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00persisted")
    )
    respx.get(url__regex=r"https://zc\.test/.+\.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00captioned")
    )


def _build_clients(
    *,
    article_fail: bool = False,
    tts_fail: bool = False,
    with_zapcap: bool = False,
) -> PipelineClients:
    return PipelineClients(
        openai=OpenAIClient(api_key="sk-test"),
        kie=KieClient(pool=KiePool(keys=["k_unused_AAAAAAAAAAAA"]), base_url="https://api.kie.ai"),
        tts=_FakeTTS(fail=tts_fail),                # type: ignore[arg-type]
        rendi=RendiClient(api_key="rendi-test", base_url=RENDI_BASE),
        storage=_FakeStorageClient(),                # type: ignore[arg-type]
        article=_FakeArticleFetcher(fail=article_fail),    # type: ignore[arg-type]
        zapcap=ZapCapClient(
            api_key="zc-test", template_id="tpl", base_url=ZAPCAP_BASE
        ) if with_zapcap else None,
    )


def _row(
    *,
    how_many: int = 2,
    image_urls: list[str] | None = None,
    zapcap: bool = False,
    vo: bool = True,
) -> FourImagesVO2Row:
    return FourImagesVO2Row(
        row_num=5,
        country="US",
        vertical="tech",
        article_url="https://example.com/article",
        how_many=how_many,
        voice_over=vo,
        image_urls=image_urls
        or [
            "https://example.com/img1.jpg",
            "https://example.com/img2.jpg",
            "https://example.com/img3.jpg",
            "https://example.com/img4.jpg",
        ],
        zapcap=zapcap,
        aspect_ratio="9:16",
        script_pattern="How To",
        open_comments="",
    )


# ── Tests ────────────────────────────────────────────────────────────────────


@respx.mock
async def test_happy_path_two_images() -> None:
    _register_openai_routes()
    _register_rendi_routes()
    _register_video_downloads()

    clients = _build_clients()
    result = await process_4images_vo2_row(_row(how_many=2), clients)

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 2
    for v in result.video_urls:
        assert v.startswith("https://storage.test/bulkvid/videos/")
    assert result.cost_usd > 0
    assert result.metadata["how_many"] == 2
    assert result.metadata["tab"] == "4Images-VO2"


@respx.mock
async def test_happy_path_four_images() -> None:
    _register_openai_routes()
    _register_rendi_routes()
    _register_video_downloads()

    clients = _build_clients()
    result = await process_4images_vo2_row(_row(how_many=4), clients)

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 4


@pytest.mark.parametrize("bad_how_many", [0, -1, 5, 10])
@respx.mock
async def test_invalid_how_many_rejected(bad_how_many: int) -> None:
    clients = _build_clients()
    result = await process_4images_vo2_row(_row(how_many=bad_how_many), clients)
    assert result.status == STATUS_INTERNAL_ERROR
    assert result.video_urls == []


@respx.mock
async def test_invalid_image_url_rejected() -> None:
    clients = _build_clients()
    result = await process_4images_vo2_row(
        _row(how_many=2, image_urls=["not-a-url", "https://example.com/img2.jpg"]),
        clients,
    )
    assert result.status == STATUS_IMAGE_DOWNLOAD_FAILED


@respx.mock
async def test_too_few_urls_rejected() -> None:
    clients = _build_clients()
    result = await process_4images_vo2_row(
        _row(how_many=3, image_urls=["https://example.com/a.jpg", "https://example.com/b.jpg"]),
        clients,
    )
    assert result.status == STATUS_IMAGE_DOWNLOAD_FAILED


@respx.mock
async def test_article_fetch_failure() -> None:
    _register_openai_routes()
    _register_rendi_routes()
    _register_video_downloads()

    clients = _build_clients(article_fail=True)
    result = await process_4images_vo2_row(_row(how_many=2), clients)

    assert result.status == STATUS_ARTICLE_FETCH_FAILED


@respx.mock
async def test_rendi_resize_failure_returns_image_gen_failed() -> None:
    _register_openai_routes()
    _register_video_downloads()

    # Submit succeeds but the poll says FAILED.
    respx.post(f"{RENDI_BASE}/v1/run-ffmpeg-command").mock(
        return_value=httpx.Response(200, json={"command_id": "cmd-1"})
    )
    respx.get(url__regex=r"https://api\.rendi\.dev/v1/commands/.+").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "FAILED",
                "error": {"message": "scale filter rejected input", "stderr": "no input"},
            },
        )
    )

    clients = _build_clients()
    result = await process_4images_vo2_row(_row(how_many=2), clients)

    assert result.status == STATUS_IMAGE_GEN_FAILED


@respx.mock
async def test_tts_failure() -> None:
    _register_openai_routes()
    _register_rendi_routes()
    _register_video_downloads()

    clients = _build_clients(tts_fail=True)
    result = await process_4images_vo2_row(_row(how_many=2), clients)

    assert result.status == STATUS_TTS_FAILED


@respx.mock
async def test_rendi_assembly_failure_after_first_succeeds() -> None:
    """Resize succeeds for both images, but stills_to_video FAILED on poll."""
    _register_openai_routes()
    _register_video_downloads()

    submit_count = {"n": 0}

    def _submit(request: httpx.Request) -> httpx.Response:
        submit_count["n"] += 1
        return httpx.Response(200, json={"command_id": f"cmd-{submit_count['n']}"})

    respx.post(f"{RENDI_BASE}/v1/run-ffmpeg-command").mock(side_effect=_submit)

    def _poll(request: httpx.Request) -> httpx.Response:
        cmd_id = str(request.url).rsplit("/", 1)[-1]
        # cmd-1 and cmd-2 are the resize calls; cmd-3+ are the stills_to_video calls.
        num = int(cmd_id.split("-")[1])
        if num <= 2:
            return httpx.Response(
                200,
                json={
                    "status": "SUCCESS",
                    "output_files": {"out_1": {"storage_url": f"https://r.dev/{cmd_id}.png"}},
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "FAILED",
                "error": {"message": "assembly broke", "stderr": "x"},
            },
        )

    respx.get(url__regex=r"https://api\.rendi\.dev/v1/commands/.+").mock(side_effect=_poll)
    # Resize outputs (.png) — not downloaded, only their URL is passed to the next stage.
    respx.get(url__regex=r"https://r\.dev/.+\.png").mock(
        return_value=httpx.Response(200, content=b"resized")
    )

    clients = _build_clients()
    result = await process_4images_vo2_row(_row(how_many=2), clients)

    assert result.status == STATUS_VIDEO_ASSEMBLY_FAILED


@respx.mock
async def test_zapcap_happy_path() -> None:
    _register_openai_routes()
    _register_rendi_routes()
    _register_video_downloads()

    respx.post(f"{ZAPCAP_BASE}/videos").mock(
        return_value=httpx.Response(201, json={"id": "v-zc"})
    )
    respx.post(url__regex=r"https://api\.zapcap\.ai/videos/.+/task").mock(
        return_value=httpx.Response(200, json={"taskId": "t-zc"})
    )
    respx.get(url__regex=r"https://api\.zapcap\.ai/videos/.+/task/.+").mock(
        return_value=httpx.Response(
            200, json={"status": "completed", "downloadUrl": "https://zc.test/final.mp4"}
        )
    )

    clients = _build_clients(with_zapcap=True)
    result = await process_4images_vo2_row(_row(how_many=2, zapcap=True), clients)

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 2
    for v in result.video_urls:
        assert "videos_captioned" in v
    assert result.metadata["zapcap_applied"] is True


@respx.mock
async def test_zapcap_failure_keeps_uncaptioned() -> None:
    _register_openai_routes()
    _register_rendi_routes()
    _register_video_downloads()

    respx.post(f"{ZAPCAP_BASE}/videos").mock(
        return_value=httpx.Response(500, text="server down")
    )

    clients = _build_clients(with_zapcap=True)
    result = await process_4images_vo2_row(_row(how_many=2, zapcap=True), clients)

    assert result.status == STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS
    assert len(result.video_urls) == 2
    for v in result.video_urls:
        assert "videos_captioned" not in v
    assert result.metadata["zapcap_applied"] is False
