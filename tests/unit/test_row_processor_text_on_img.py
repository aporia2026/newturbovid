"""Tests for the text-on-img row processor.

The 2026-06-09 rewrite stripped the video pipeline — this tab now produces
a still PNG (manual image + center-overlay text). Covers:
  - Happy path -> STATUS_SUCCESS + exactly 1 image URL written back
  - Blank text -> still SUCCESS, image ships without overlay
  - Bad manual_image_url -> STATUS_IMAGE_DOWNLOAD_FAILED, fast fail
  - Article / TTS / Rendi / ZapCap clients are NEVER called
"""

from __future__ import annotations

import io

import httpx
import respx
from PIL import Image

from typing import Any, cast

from bulkvid.adapters.article_fetch import ArticleFetcher
from bulkvid.adapters.kie import KieClient, KiePool
from bulkvid.adapters.openai_client import OpenAIClient
from bulkvid.adapters.rendi import RendiClient
from bulkvid.adapters.storage import UploadResult
from bulkvid.adapters.zapcap import ZapCapClient
from bulkvid.models.row import (
    STATUS_IMAGE_DOWNLOAD_FAILED,
    STATUS_SUCCESS,
    TextOnImgRow,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.row_processor_text_on_img import process_text_on_img_row

OPENAI_BASE = "https://api.openai.com/v1"
RENDI_BASE = "https://api.rendi.dev"
ZAPCAP_BASE = "https://api.zapcap.ai"
TTS_BASE = "https://generativelanguage.googleapis.com"
SCRAPINGBEE_BASE = "https://app.scrapingbee.com"


class _FakeStorageClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def upload_bytes(
        self, data: bytes, key: str, content_type: str = "application/octet-stream"
    ) -> UploadResult:
        self.calls.append((key, content_type, len(data)))
        return UploadResult(
            url=f"https://storage.test/{key}", backend="gcs",
            bytes_written=len(data), cost_usd=0.0001,
        )


def _src_png(width: int = 640, height: int = 360, color=(120, 180, 220)) -> bytes:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    img.close()
    return buf.getvalue()


def _build_clients(storage: _FakeStorageClient | None = None) -> PipelineClients:
    # Real client instances with dummy creds where the constructor allows it,
    # plus an opaque sentinel for ``tts`` (GeminiTTSClient needs a Vertex AI
    # project to instantiate, but the image-only processor never touches it).
    # If any of these get called by accident, the network mocks below (left
    # unregistered on purpose) will reject the call.
    return PipelineClients(
        openai=OpenAIClient(api_key="sk-test"),
        kie=KieClient(pool=KiePool(keys=["k_unused_AAAAAAAAAAAA"]), base_url="https://api.kie.ai"),
        tts=cast(Any, object()),    # never invoked by the image-only pipeline
        rendi=RendiClient(api_key="rendi-test", base_url=RENDI_BASE),
        storage=storage or _FakeStorageClient(),    # type: ignore[arg-type]
        article=ArticleFetcher(tavily_api_key="tv-test", scrapingbee_api_key="sb-test"),
        zapcap=ZapCapClient(api_key="zc-test", template_id="tpl", base_url=ZAPCAP_BASE),
    )


def _row(
    *,
    text: str = "Casas embargadas: precios y oportunidades",
    image_url: str = "https://example.com/ad.png",
    aspect_ratio: str = "9:16",
) -> TextOnImgRow:
    return TextOnImgRow(
        row_num=2,
        country="ES",
        vertical="real-estate",
        article_url="https://example.com/article",    # ignored by processor
        manual_image_url=image_url,
        text=text,
        voice_over=True,                              # ignored
        zapcap=False,                                 # ignored
        aspect_ratio=aspect_ratio,
        script_pattern="",                            # ignored
        open_comments="",                             # ignored
    )


# ── Tests ────────────────────────────────────────────────────────────────────


@respx.mock
async def test_happy_path_returns_one_image_url() -> None:
    """Happy path: manual image downloaded, composed PNG uploaded, one URL
    returned, status SUCCESS."""
    respx.get("https://example.com/ad.png").mock(
        return_value=httpx.Response(200, content=_src_png()),
    )

    storage = _FakeStorageClient()
    result = await process_text_on_img_row(_row(), _build_clients(storage), job_id="jobX")

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 1
    assert result.video_urls[0].startswith("https://storage.test/bulkvid/text_on_img/")
    assert result.video_urls[0].endswith("/composed.png")
    assert result.metadata["tab"] == "text_on_img"
    assert result.metadata["overlay_chars"] > 0
    assert result.metadata["composed_image_bytes"] > 1024
    # Exactly ONE storage upload — the composed image. No VO upload, no
    # captioned-video upload, no source-image upload.
    assert len(storage.calls) == 1
    key, content_type, _ = storage.calls[0]
    assert content_type == "image/png"
    assert key.endswith("/composed.png")


@respx.mock
async def test_blank_text_ships_image_without_overlay() -> None:
    """Empty text is allowed — the overlay function returns the
    blurred-bg-fit image without drawing text. Status still SUCCESS."""
    respx.get("https://example.com/ad.png").mock(
        return_value=httpx.Response(200, content=_src_png()),
    )

    result = await process_text_on_img_row(_row(text=""), _build_clients(), job_id="j")

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 1
    assert result.metadata["overlay_chars"] == 0


async def test_bad_image_url_fails_fast() -> None:
    """Missing http(s) prefix -> STATUS_IMAGE_DOWNLOAD_FAILED before any
    network call. No respx mock registered — if the processor tried to
    fetch, the test would error on an unmocked request."""
    result = await process_text_on_img_row(
        _row(image_url="not-a-url"), _build_clients(), job_id="j"
    )
    assert result.status == STATUS_IMAGE_DOWNLOAD_FAILED
    assert result.video_urls == []


@respx.mock
async def test_no_external_pipeline_clients_called() -> None:
    """The image-only pipeline must NOT hit OpenAI, Rendi, Gemini TTS,
    ZapCap, or any article-fetch backend. Register mocks at those bases
    and assert none were called."""
    openai_route = respx.post(f"{OPENAI_BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json={}),
    )
    rendi_route = respx.post(f"{RENDI_BASE}/v1/run-ffmpeg-command").mock(
        return_value=httpx.Response(200, json={"command_id": "x"}),
    )
    zapcap_route = respx.post(url__regex=rf"{ZAPCAP_BASE}/.*").mock(
        return_value=httpx.Response(200, json={"taskId": "x"}),
    )
    tts_route = respx.post(url__regex=rf"{TTS_BASE}/.*").mock(
        return_value=httpx.Response(200, json={}),
    )
    scrapingbee_route = respx.get(url__regex=rf"{SCRAPINGBEE_BASE}/.*").mock(
        return_value=httpx.Response(200, text="article body"),
    )
    respx.get("https://example.com/ad.png").mock(
        return_value=httpx.Response(200, content=_src_png()),
    )

    result = await process_text_on_img_row(_row(), _build_clients(), job_id="j")

    assert result.status == STATUS_SUCCESS
    assert not openai_route.called, "OpenAI must not be called"
    assert not rendi_route.called, "Rendi must not be called"
    assert not zapcap_route.called, "ZapCap must not be called"
    assert not tts_route.called, "Gemini TTS must not be called"
    assert not scrapingbee_route.called, "Article fetch must not be called"


@respx.mock
async def test_image_download_network_failure() -> None:
    """A 5xx on the manual image fetch surfaces as STATUS_IMAGE_DOWNLOAD_FAILED
    with a useful error string (includes exception class + URL host)."""
    respx.get("https://example.com/ad.png").mock(
        return_value=httpx.Response(500, content=b"server boom"),
    )

    result = await process_text_on_img_row(_row(), _build_clients(), job_id="j")

    assert result.status == STATUS_IMAGE_DOWNLOAD_FAILED
    assert result.video_urls == []
    assert result.error is not None
    assert "example.com" in result.error


@respx.mock
async def test_cost_breakdown_only_storage() -> None:
    """The stripped pipeline has exactly one cost source: storage. No
    article / language / classify / script / tts / rendi / zapcap entries."""
    respx.get("https://example.com/ad.png").mock(
        return_value=httpx.Response(200, content=_src_png()),
    )

    result = await process_text_on_img_row(_row(), _build_clients(), job_id="j")

    assert result.status == STATUS_SUCCESS
    breakdown = result.metadata["cost_breakdown"]
    assert set(breakdown.keys()) == {"storage"}
    assert breakdown["storage"] > 0
