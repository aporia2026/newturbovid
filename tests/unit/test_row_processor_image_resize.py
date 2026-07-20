"""Tests for the image_resize row processor.

The image_resize tab reframes the operator's Manual Image to a new aspect ratio
with Nano Banana 2 (image-to-image) and writes back a single image URL. Covers:
  - Happy path (ratio target)  -> STATUS_SUCCESS + exactly 1 image URL
  - Pixel target (WxH)         -> the uploaded image is exactly that size
  - Blank / unparseable size   -> soft error, model never called, no spend
  - Bad manual_image_url       -> STATUS_IMAGE_DOWNLOAD_FAILED, model not called
  - Reframe failure            -> STATUS_IMAGE_GEN_FAILED
  - Reframed-URL download 5xx  -> STATUS_IMAGE_DOWNLOAD_FAILED
  - Cost breakdown             -> image_gen + storage only
  - The article / TTS / Rendi / ZapCap clients are NEVER called
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timezone
from typing import Any, cast

import httpx
import respx
from PIL import Image

from bulkvid.adapters.article_fetch import ArticleFetcher
from bulkvid.adapters.kie import KieClient, KiePool
from bulkvid.adapters.openai_client import OpenAIClient
from bulkvid.adapters.rendi import RendiClient
from bulkvid.adapters.storage import UploadResult
from bulkvid.adapters.zapcap import ZapCapClient
from bulkvid.models.row import (
    STATUS_IMAGE_DOWNLOAD_FAILED,
    STATUS_IMAGE_GEN_FAILED,
    STATUS_INTERNAL_ERROR,
    STATUS_SUCCESS,
    ImageResizeRow,
)
from bulkvid.orchestrator import row_processor_image_resize as mod
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.row_processor_image_resize import (
    _image_object_key,
    process_image_resize_row,
)

OPENAI_BASE = "https://api.openai.com/v1"
RENDI_BASE = "https://api.rendi.dev"
ZAPCAP_BASE = "https://api.zapcap.ai"
TTS_BASE = "https://generativelanguage.googleapis.com"
SCRAPINGBEE_BASE = "https://app.scrapingbee.com"

REFRAMED_URL = "https://kie.test/reframed.png"


class _FakeStorageClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.last_bytes: bytes = b""

    async def upload_bytes(
        self, data: bytes, key: str, content_type: str = "application/octet-stream"
    ) -> UploadResult:
        self.calls.append((key, content_type, len(data)))
        self.last_bytes = data
        return UploadResult(
            url=f"https://storage.test/{key}", backend="gcs",
            bytes_written=len(data), cost_usd=0.0001,
        )


def _src_png(width: int = 1024, height: int = 1024, color=(120, 180, 220)) -> bytes:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    img.close()
    return buf.getvalue()


def _build_clients(storage: _FakeStorageClient | None = None) -> PipelineClients:
    # Real client instances with dummy creds; the image-only pipeline never
    # touches OpenAI / TTS / Rendi / ZapCap / article-fetch, and the network
    # mocks below (left unregistered on purpose) would reject any stray call.
    return PipelineClients(
        openai=OpenAIClient(api_key="sk-test"),
        kie=KieClient(pool=KiePool(keys=["k_unused_AAAAAAAAAAAA"]), base_url="https://api.kie.ai"),
        tts=cast(Any, object()),
        rendi=RendiClient(api_key="rendi-test", base_url=RENDI_BASE),
        storage=storage or _FakeStorageClient(),    # type: ignore[arg-type]
        article=ArticleFetcher(scrapingbee_api_key="sb-test"),
        zapcap=ZapCapClient(api_key="zc-test", template_id="tpl", base_url=ZAPCAP_BASE),
    )


def _row(
    *,
    image_url: str = "https://example.com/ad.png",
    aspect_ratio: str = "9:16",
) -> ImageResizeRow:
    return ImageResizeRow(
        row_num=2,
        country="DE",
        vertical="sleepwear-pr",
        article_url="https://example.com/article",    # ignored by processor
        manual_image_url=image_url,
        text="baked-into-image, ignored",             # ignored
        voice_over=True,                               # ignored
        zapcap=False,                                  # ignored
        aspect_ratio=aspect_ratio,
        script_pattern="",                             # ignored
        open_comments="",                              # ignored
    )


def _install_fake_reframe(monkeypatch, *, cost: float = 0.06, raises: bool = False):
    """Swap edit_with_fallback for a fake that records its calls. Returns the
    list of captured kwargs so a test can assert whether/how it was invoked."""
    calls: list[dict[str, Any]] = []

    async def _fake(**kwargs: Any) -> tuple[str, float]:
        calls.append(kwargs)
        if raises:
            raise RuntimeError("all image backends failed")
        return REFRAMED_URL, cost

    monkeypatch.setattr(mod, "edit_with_fallback", _fake)
    return calls


# ── Tests ────────────────────────────────────────────────────────────────────


@respx.mock
async def test_happy_path_ratio_returns_one_image_url(monkeypatch) -> None:
    """Ratio target: reframe, download the result, upload one image, SUCCESS.
    The storage key carries country / vertical / date / size / row / hex."""
    calls = _install_fake_reframe(monkeypatch)
    respx.get(REFRAMED_URL).mock(return_value=httpx.Response(200, content=_src_png()))

    storage = _FakeStorageClient()
    result = await process_image_resize_row(_row(), _build_clients(storage), job_id="jobX")

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 1
    assert result.video_urls[0].startswith("https://storage.test/bulkvid/image_resize/")
    assert result.metadata["tab"] == "image_resize"
    assert result.metadata["target_type"] == "ratio"
    assert result.metadata["final_image_bytes"] > 1024
    # The model was asked to reframe the operator's image at the target ratio.
    assert len(calls) == 1
    assert calls[0]["source_image_url"] == "https://example.com/ad.png"
    assert calls[0]["aspect_ratio"] == "9:16"
    assert calls[0]["resolution"] == "2K"
    # Exactly ONE storage upload — the reframed image.
    assert len(storage.calls) == 1
    key = storage.calls[0][0]
    filename = key.rsplit("/", 1)[-1]
    assert filename.startswith("DE_sleepwear-pr_resize_")
    assert "_9x16_" in filename
    assert "_r2_" in filename


@respx.mock
async def test_pixel_target_produces_exact_dimensions(monkeypatch) -> None:
    """A WxH target crops+resizes to exactly that pixel size, regardless of the
    model output's aspect. The uploaded bytes decode to 1080x1350."""
    _install_fake_reframe(monkeypatch)
    respx.get(REFRAMED_URL).mock(
        return_value=httpx.Response(200, content=_src_png(1600, 900)),
    )

    storage = _FakeStorageClient()
    result = await process_image_resize_row(
        _row(aspect_ratio="1080x1350"), _build_clients(storage), job_id="j"
    )

    assert result.status == STATUS_SUCCESS
    assert result.metadata["target_type"] == "pixels"
    with Image.open(io.BytesIO(storage.last_bytes)) as out:
        assert out.size == (1080, 1350)


@respx.mock
async def test_blank_size_is_soft_error_no_spend(monkeypatch) -> None:
    """Blank Change Size -> soft error before any model call. The reframe is
    never invoked (no $ spent) and nothing is uploaded."""
    calls = _install_fake_reframe(monkeypatch)

    storage = _FakeStorageClient()
    result = await process_image_resize_row(
        _row(aspect_ratio=""), _build_clients(storage), job_id="j"
    )

    assert result.status == STATUS_INTERNAL_ERROR
    assert result.video_urls == []
    assert result.error is not None and "Change Size" in result.error
    assert calls == []                 # model never called
    assert storage.calls == []         # nothing uploaded
    assert result.cost_usd == 0.0


@respx.mock
async def test_unparseable_size_is_soft_error(monkeypatch) -> None:
    """A size we cannot parse (no ratio, no pixels) is treated like blank."""
    calls = _install_fake_reframe(monkeypatch)

    result = await process_image_resize_row(
        _row(aspect_ratio="not-a-size"), _build_clients(), job_id="j"
    )

    assert result.status == STATUS_INTERNAL_ERROR
    assert result.error is not None and "Change Size" in result.error
    assert calls == []


async def test_bad_image_url_fails_fast(monkeypatch) -> None:
    """Missing http(s) prefix -> STATUS_IMAGE_DOWNLOAD_FAILED before any model
    call. No respx mock registered — a stray fetch would error the test."""
    calls = _install_fake_reframe(monkeypatch)

    result = await process_image_resize_row(
        _row(image_url="not-a-url"), _build_clients(), job_id="j"
    )

    assert result.status == STATUS_IMAGE_DOWNLOAD_FAILED
    assert result.video_urls == []
    assert calls == []                 # model never called


@respx.mock
async def test_reframe_failure_maps_to_image_gen_failed(monkeypatch) -> None:
    """Every image backend failing surfaces as STATUS_IMAGE_GEN_FAILED."""
    _install_fake_reframe(monkeypatch, raises=True)

    result = await process_image_resize_row(_row(), _build_clients(), job_id="j")

    assert result.status == STATUS_IMAGE_GEN_FAILED
    assert result.video_urls == []
    assert result.error is not None and "reframe failed" in result.error


@respx.mock
async def test_reframed_download_failure(monkeypatch) -> None:
    """A 5xx fetching the reframed image surfaces as IMAGE_DOWNLOAD_FAILED with
    a useful error (exception class + URL host)."""
    _install_fake_reframe(monkeypatch)
    respx.get(REFRAMED_URL).mock(return_value=httpx.Response(500, content=b"boom"))

    result = await process_image_resize_row(_row(), _build_clients(), job_id="j")

    assert result.status == STATUS_IMAGE_DOWNLOAD_FAILED
    assert result.video_urls == []
    assert result.error is not None and "kie.test" in result.error


@respx.mock
async def test_cost_breakdown_image_gen_and_storage(monkeypatch) -> None:
    """The lean pipeline has exactly two cost sources: image_gen + storage."""
    _install_fake_reframe(monkeypatch, cost=0.06)
    respx.get(REFRAMED_URL).mock(return_value=httpx.Response(200, content=_src_png()))

    result = await process_image_resize_row(_row(), _build_clients(), job_id="j")

    assert result.status == STATUS_SUCCESS
    breakdown = result.metadata["cost_breakdown"]
    assert set(breakdown.keys()) == {"image_gen", "storage"}
    assert breakdown["image_gen"] == 0.06
    assert breakdown["storage"] > 0


@respx.mock
async def test_no_external_pipeline_clients_called(monkeypatch) -> None:
    """The image-only pipeline must NOT hit OpenAI / Rendi / TTS / ZapCap /
    article-fetch. Register mocks at those bases and assert none fired."""
    _install_fake_reframe(monkeypatch)
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
    respx.get(REFRAMED_URL).mock(return_value=httpx.Response(200, content=_src_png()))

    result = await process_image_resize_row(_row(), _build_clients(), job_id="j")

    assert result.status == STATUS_SUCCESS
    assert not openai_route.called, "OpenAI must not be called"
    assert not rendi_route.called, "Rendi must not be called"
    assert not zapcap_route.called, "ZapCap must not be called"
    assert not tts_route.called, "Gemini TTS must not be called"
    assert not scrapingbee_route.called, "Article fetch must not be called"


# ── Object-key naming ───────────────────────────────────────────────────────


def test_image_object_key_full_shape() -> None:
    """Shape: bulkvid/image_resize/COUNTRY_vertical_resize_YYYY-MM-DD_WxH_rN_HEX.png."""
    row = _row(aspect_ratio="09:16")   # leading zero normalizes -> 9x16
    row.country = "de"
    row.vertical = "Sleepwear PR"
    row.row_num = 7
    key = _image_object_key(row, now=datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc))

    assert re.fullmatch(
        r"bulkvid/image_resize/DE_sleepwear-pr_resize_2026-07-20_9x16_r7_[0-9a-f]{6}\.png",
        key,
    ), key


def test_image_object_key_jpg_extension_follows_content_type() -> None:
    """When the optimizer downgrades to JPEG, the key ends .jpg, not .png."""
    key = _image_object_key(_row(), ext="jpg", now=datetime(2026, 7, 20, tzinfo=timezone.utc))
    assert key.endswith(".jpg")


def test_image_object_key_empty_fields_fall_back_gracefully() -> None:
    """Blank country / vertical must not produce ``__`` runs — the slug helpers
    substitute readable fallbacks (NA / general)."""
    row = _row(aspect_ratio="1:1")
    row.country = ""
    row.vertical = ""
    row.row_num = 3
    key = _image_object_key(row, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    fname = key.rsplit("/", 1)[-1]

    assert "__" not in fname
    assert fname.startswith("NA_general_resize_2026-01-01_1x1_r3_")


def test_image_object_key_collisions_unlikely_across_runs() -> None:
    """Back-to-back keys for the same row differ (random hex tail)."""
    keys = {_image_object_key(_row()) for _ in range(50)}
    assert len(keys) == 50
