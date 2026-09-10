"""Unit + integration tests for the 1-click-image-vid tab.

One source image in → a 4-panel STORY collage (nano-banana-2, story mode) → the
4 quadrants sequenced into ONE still-image video sized to the voiceover →
optional CTA pill → optional ZapCap. Exactly one Ready Video per row.

Covers (mirrors the image_vo harness — real adapters + respx — plus a fake Rendi
for the assembly-shape / VO-sizing assertions the real client can't expose):
  - Happy path (VO=Yes, ZapCap=No) → ONE video URL, STATUS_SUCCESS, cost summed.
  - The collage prompt is requested in STORY mode.
  - Assembly shape: 4 silent stills + ONE concat sized to the VO (no dead air).
  - VO off → silent stitch, no TTS, ZapCap skipped.
  - CTA enabled → pill overlay applied on the single video.
  - ZapCap happy path + ZapCap failure keeps the uncaptioned video.
  - Article / image-download / image-gen / TTS / Rendi failures → precise status.
  - Payload round-trip + runner routing; settings registered.
  - The story message builder + the ``story`` flag routing in build_collage_prompt.
"""

from __future__ import annotations

import io
import json
import wave
from types import SimpleNamespace

import httpx
import pytest
import respx
from PIL import Image

from bulkvid.adapters.article_fetch import ArticleResult
from bulkvid.adapters.gemini_tts import TTSResult
from bulkvid.adapters.kie import KieClient, KiePool
from bulkvid.adapters.openai_client import OpenAIClient
from bulkvid.adapters.rendi import RendiClient, RendiOutput
from bulkvid.adapters.storage import UploadResult
from bulkvid.adapters.zapcap import ZapCapClient
from bulkvid.models.row import (
    STATUS_ARTICLE_FETCH_FAILED,
    STATUS_IMAGE_DOWNLOAD_FAILED,
    STATUS_IMAGE_GEN_FAILED,
    STATUS_SUCCESS,
    STATUS_TTS_FAILED,
    STATUS_VIDEO_ASSEMBLY_FAILED,
    STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS,
    OneClickImageVidRow,
)
from bulkvid.orchestrator import row_processor_one_click_image_vid as mod
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.queue import (
    TAB_ONE_CLICK_IMAGE_VID,
    _row_to_payload,
    payload_to_row,
)
from bulkvid.orchestrator.row_processor_one_click_image_vid import (
    OCI_MIN_VIDEO_SECONDS,
    OCI_NUM_IMAGES,
    _even_clips,
    process_one_click_image_vid_row,
)
from bulkvid.orchestrator.runner import _dispatch_to_processor, _tab_for_row
from bulkvid.orchestrator.runtime_settings import (
    SETTING_ROW_TIMEOUT_ONE_CLICK_IMAGE_VID,
    lookup,
)
from bulkvid.pipeline.image_prompt import _collage_user_message_story

OPENAI_BASE = "https://api.openai.com/v1"
KIE_BASE = "https://api.kie.ai"
RENDI_BASE = "https://api.rendi.dev"
ZAPCAP_BASE = "https://api.zapcap.ai"
KEY_A = "kie_test_key_AAAAAAAAAAAA"


# ── Adapter fakes ────────────────────────────────────────────────────────────


class _FakeArticleFetcher:
    def __init__(self, fail: bool = False):
        self._fail = fail

    async def fetch(self, url: str) -> ArticleResult:
        if self._fail:
            from bulkvid.adapters.article_fetch import ArticleFetchError

            raise ArticleFetchError("simulated scrapingbee+direct fail")
        body = "Article body about seized cars in Mexico."
        return ArticleResult(
            url=url, content=body, source="scrapingbee",
            char_count=len(body), cost_usd=0.003,
        )


class _FakeStorageClient:
    def __init__(self, fail: bool = False):
        self._fail = fail
        self.calls: list[tuple[str, str]] = []

    async def upload_bytes(
        self, data: bytes, key: str, content_type: str = "application/octet-stream"
    ) -> UploadResult:
        if self._fail:
            raise RuntimeError("storage down")
        self.calls.append((key, content_type))
        return UploadResult(
            url=f"https://storage.test/{key}", backend="s3",
            bytes_written=len(data), cost_usd=0.0001,
        )


class _FakeTTS:
    def __init__(self, fail: bool = False, duration: float = 12.0):
        self._fail = fail
        self._duration = duration
        self.calls = 0

    async def synthesize(
        self, text: str, language: str, voice: str | None = None,
        style_prompt: str | None = None, country: str = "",
    ) -> TTSResult:
        if self._fail:
            from bulkvid.adapters.gemini_tts import GeminiTTSNoAudioError

            raise GeminiTTSNoAudioError("simulated tts failure")
        self.calls += 1
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24_000)
            wf.writeframes(b"\x00" * 24_000)
        return TTSResult(
            wav_bytes=buf.getvalue(), voice=voice or "Kore", language=language,
            duration_seconds=self._duration, character_count=len(text), cost_usd=0.003,
        )


class _FakeRendi:
    """Records assembly calls so the tests can assert the 4-stills → ONE video
    shape and the VO-sizing (the real client only exposes an opaque URL)."""

    def __init__(self) -> None:
        self.silent_calls: list[dict] = []
        self.concat_calls: list[dict] = []
        self.overlay_calls: list[dict] = []
        self.cleaned: list[str] = []

    async def image_to_silent_video(
        self, image_url, output_filename="out.mp4", *, aspect_ratio="9:16",
        seconds=10, **_,
    ) -> RendiOutput:
        self.silent_calls.append({"image_url": image_url, "seconds": seconds})
        return RendiOutput(
            url=f"https://r.dev/{output_filename}", cost_usd=0.01,
            command_id=f"cmd-{output_filename}",
        )

    async def concat_clips_with_audio(
        self, clip_urls, audio_url, per_clip_seconds, output_filename="out.mp4",
        *, aspect_ratio="9:16", total_video_seconds=None, atempo=None, **_,
    ) -> RendiOutput:
        self.concat_calls.append({
            "clips": list(clip_urls), "audio_url": audio_url,
            "per_clip": list(per_clip_seconds), "total": total_video_seconds,
            "atempo": atempo,
        })
        return RendiOutput(
            url=f"https://r.dev/{output_filename}", cost_usd=0.01,
            command_id=f"cmd-{output_filename}",
        )

    async def overlay_image_on_video(
        self, video_url, overlay_url, output_filename="out.mp4", **_,
    ) -> RendiOutput:
        self.overlay_calls.append({"video_url": video_url, "overlay_url": overlay_url})
        return RendiOutput(
            url=f"https://r.dev/{output_filename}", cost_usd=0.01,
            command_id=f"cmd-{output_filename}",
        )

    async def cleanup_commands(self, command_ids) -> None:
        self.cleaned.extend(command_ids)


# ── OpenAI / kie / download mocks (identical shape to the image_vo harness) ──


def _openai_chat_resp(content: str, model: str = "gpt-5.4-mini") -> dict:
    return {
        "id": "x", "object": "chat.completion", "created": 1717_000_000, "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content},
             "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130},
    }


def _register_default_openai_routes() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sys = body.get("messages", [{}])[0].get("content", "")
        user = body.get("messages", [{}])[-1].get("content", "")
        user_str = user if isinstance(user, str) else json.dumps(user)

        if "detect the primary language" in sys:
            text = json.dumps({"language": "en", "confidence": 0.95})
        elif "classify 'Open Comments'" in sys:
            text = json.dumps(
                {"mode": "none", "tone_hints": [], "directives": [], "override_script": None}
            )
        elif "voiceover scripts for bulk" in sys:
            text = json.dumps({
                "script": "See how these seized cars go from unknown to a happy drive today.",
                "style_direction": "Warm and curious.",
            })
        elif "advertising creative director" in sys:
            text = "Create a single image that is a STRICT 2x2 grid telling one story."
        elif "Analyse this advertising image" in user_str:
            text = "SUBJECT: a person by a car. STYLE: photographic."
        else:
            if isinstance(user, list):
                text = "SUBJECT: a person by a car. STYLE: photographic."
            else:
                text = "ok"
        return httpx.Response(200, json=_openai_chat_resp(text))

    respx.post(f"{OPENAI_BASE}/chat/completions").mock(side_effect=_handler)


def _register_default_kie_routes() -> None:
    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(
        return_value=httpx.Response(200, json={"code": 200, "data": {"taskId": "kie-1"}})
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
    return counter


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


def _register_downloads(collage_bytes: bytes) -> None:
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
    respx.get(url__regex=r"https://storage\.test/.+\.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00persisted-video")
    )


def _build_clients(
    *,
    article_fail: bool = False,
    storage_fail: bool = False,
    tts_fail: bool = False,
    tts_duration: float = 12.0,
    with_zapcap: bool = False,
    fake_rendi: _FakeRendi | None = None,
) -> PipelineClients:
    return PipelineClients(
        openai=OpenAIClient(api_key="sk-test"),
        kie=KieClient(pool=KiePool(keys=[KEY_A]), base_url=KIE_BASE),
        tts=_FakeTTS(fail=tts_fail, duration=tts_duration),   # type: ignore[arg-type]
        rendi=fake_rendi if fake_rendi is not None
        else RendiClient(api_key="rendi-test", base_url=RENDI_BASE),
        storage=_FakeStorageClient(fail=storage_fail),        # type: ignore[arg-type]
        article=_FakeArticleFetcher(fail=article_fail),       # type: ignore[arg-type]
        zapcap=ZapCapClient(
            api_key="zc-test", template_id="tpl-x", base_url=ZAPCAP_BASE
        ) if with_zapcap else None,
    )


def _row(*, zapcap: bool = False, vo: bool = True, cta: bool = False,
         cta_text: str = "",
         manual_image_url: str = "https://example.com/seed.png") -> OneClickImageVidRow:
    return OneClickImageVidRow(
        row_num=2, country="MX", vertical="cars",
        article_url="https://example.com/article",
        manual_image_url=manual_image_url,
        voice_over=vo, zapcap=zapcap, aspect_ratio="9:16",
        script_pattern="How To", open_comments="",
        cta_enabled=cta, cta_text=cta_text,
    )


# ── Golden path + story mode (real adapters) ─────────────────────────────────


@respx.mock
async def test_happy_path_returns_one_video_url() -> None:
    _register_default_openai_routes()
    _register_default_kie_routes()
    _register_default_rendi_routes()
    _register_downloads(_make_collage_png(200))

    clients = _build_clients()
    result = await process_one_click_image_vid_row(_row(), clients, job_id="j")

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 1                    # ONE video per row
    assert result.video_urls[0].startswith("https://storage.test/bulkvid/videos/")
    assert result.error is None
    assert result.cost_usd > 0
    # VO 12s → sized to the voice (floor is 8s), no dead-air tail.
    assert result.metadata["oci_total_seconds"] == 12.5
    # Seed image present → image-to-image, not from-scratch.
    assert result.metadata["from_scratch"] is False


@respx.mock
async def test_collage_prompt_requested_in_story_mode(monkeypatch) -> None:
    _register_default_openai_routes()
    _register_default_kie_routes()
    _register_default_rendi_routes()
    _register_downloads(_make_collage_png(200))
    captured: dict = {}

    real = mod.build_collage_prompt

    async def _spy(client, description, **kw):
        captured.update(kw)
        return await real(client, description, **kw)

    monkeypatch.setattr(mod, "build_collage_prompt", _spy)

    clients = _build_clients()
    result = await process_one_click_image_vid_row(_row(), clients, job_id="j")

    assert result.status == STATUS_SUCCESS
    assert captured.get("story") is True


# ── From-scratch: no manual image → text-to-image ────────────────────────────


@respx.mock
async def test_from_scratch_when_no_manual_image() -> None:
    _register_default_openai_routes()
    _register_default_kie_routes()
    _register_default_rendi_routes()
    _register_downloads(_make_collage_png(200))

    clients = _build_clients()
    result = await process_one_click_image_vid_row(
        _row(manual_image_url=""), clients, job_id="j"
    )

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 1
    assert result.metadata["from_scratch"] is True


@respx.mock
async def test_from_scratch_does_not_describe_a_source_image(monkeypatch) -> None:
    # No seed → describe_source_image must NOT be called (nothing to describe);
    # the collage is generated from the context brief + article instead.
    _register_default_openai_routes()
    _register_default_kie_routes()
    _register_default_rendi_routes()
    _register_downloads(_make_collage_png(200))

    async def _boom(*a, **k):
        raise AssertionError("describe_source_image called on the from-scratch path")

    monkeypatch.setattr(mod, "describe_source_image", _boom)

    clients = _build_clients()
    result = await process_one_click_image_vid_row(
        _row(manual_image_url=""), clients, job_id="j"
    )
    assert result.status == STATUS_SUCCESS
    assert result.metadata["from_scratch"] is True


def test_context_brief_mentions_market_and_topic() -> None:
    brief = mod._context_brief("MX", "cars")
    assert "MX" in brief and "cars" in brief
    assert "no source photo" in brief.lower()


async def test_generate_with_fallback_uses_atlas_when_kie_fails(monkeypatch) -> None:
    from bulkvid.adapters.kie import KieError
    from bulkvid.pipeline import image_gen

    async def _fail_t2i(kie, prompt, aspect_ratio, resolution="2K", **_):
        raise KieError("kie t2i down")

    monkeypatch.setattr(image_gen, "nano_banana_2_text_to_image", _fail_t2i)

    class _Atlas:
        async def text_to_image(self, prompt, aspect_ratio, **_):
            return "https://atlas.test/img.png", 0.05

    url, cost = await image_gen.generate_with_fallback(
        kie=SimpleNamespace(), atlas=_Atlas(), prompt="p", aspect_ratio="9:16"
    )
    assert url == "https://atlas.test/img.png"
    assert cost == 0.05


async def test_generate_with_fallback_raises_without_atlas(monkeypatch) -> None:
    from bulkvid.adapters.kie import KieError
    from bulkvid.pipeline import image_gen

    async def _fail_t2i(kie, prompt, aspect_ratio, resolution="2K", **_):
        raise KieError("kie t2i down")

    monkeypatch.setattr(image_gen, "nano_banana_2_text_to_image", _fail_t2i)
    with pytest.raises(KieError):
        await image_gen.generate_with_fallback(
            kie=SimpleNamespace(), atlas=None, prompt="p", aspect_ratio="9:16"
        )


# ── Assembly shape + VO sizing (fake Rendi) ──────────────────────────────────


@respx.mock
async def test_assembles_four_stills_into_one_video_sized_to_vo() -> None:
    _register_default_openai_routes()
    _register_default_kie_routes()
    _register_downloads(_make_collage_png(200))
    rendi = _FakeRendi()

    clients = _build_clients(tts_duration=12.0, fake_rendi=rendi)
    result = await process_one_click_image_vid_row(_row(), clients, job_id="j")

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 1
    # 4 stills → ONE concat carrying the VO, sized to the voice length.
    assert len(rendi.silent_calls) == OCI_NUM_IMAGES
    assert len(rendi.concat_calls) == 1
    call = rendi.concat_calls[0]
    assert len(call["clips"]) == OCI_NUM_IMAGES
    assert call["audio_url"] is not None
    assert call["total"] == 12.5
    assert round(sum(call["per_clip"]), 3) == 12.5
    assert call["atempo"] == 1.0
    # Each still is rendered at least as long as its concat trim.
    assert all(s["seconds"] >= max(call["per_clip"]) for s in rendi.silent_calls)


@respx.mock
async def test_short_vo_is_floored_to_min_video_seconds() -> None:
    _register_default_openai_routes()
    _register_default_kie_routes()
    _register_downloads(_make_collage_png(200))
    rendi = _FakeRendi()

    clients = _build_clients(tts_duration=0.5, fake_rendi=rendi)
    result = await process_one_click_image_vid_row(_row(), clients, job_id="j")

    assert result.status == STATUS_SUCCESS
    assert rendi.concat_calls[0]["total"] == OCI_MIN_VIDEO_SECONDS


@respx.mock
async def test_voice_over_off_silent_stitch_no_tts_no_zapcap() -> None:
    _register_default_openai_routes()
    _register_default_kie_routes()
    _register_downloads(_make_collage_png(200))
    rendi = _FakeRendi()

    clients = _build_clients(fake_rendi=rendi)
    # ZapCap requested but VO off → nothing to transcribe, skipped.
    result = await process_one_click_image_vid_row(
        _row(vo=False, zapcap=True), clients, job_id="j"
    )

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 1
    assert clients.tts.calls == 0
    assert rendi.concat_calls[0]["audio_url"] is None
    assert result.metadata.get("zapcap_skipped_no_vo") is True


@respx.mock
async def test_cta_enabled_overlays_pill_on_single_video() -> None:
    _register_default_openai_routes()
    _register_default_kie_routes()
    _register_downloads(_make_collage_png(200))
    rendi = _FakeRendi()

    clients = _build_clients(fake_rendi=rendi)
    result = await process_one_click_image_vid_row(
        _row(cta=True, cta_text="See Prices"), clients, job_id="j"
    )

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 1
    assert len(rendi.overlay_calls) == 1
    assert result.metadata.get("cta_text_used") == "See Prices"


# ── ZapCap (real adapters) ───────────────────────────────────────────────────


@respx.mock
async def test_zapcap_happy_path_applies_captions() -> None:
    _register_default_openai_routes()
    _register_default_kie_routes()
    _register_default_rendi_routes()
    _register_downloads(_make_collage_png(200))
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
    result = await process_one_click_image_vid_row(_row(zapcap=True), clients, job_id="j")

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 1
    assert result.metadata.get("zapcap_applied") is True


@respx.mock
async def test_zapcap_failure_keeps_uncaptioned_video() -> None:
    _register_default_openai_routes()
    _register_default_kie_routes()
    _register_default_rendi_routes()
    _register_downloads(_make_collage_png(200))
    respx.post(f"{ZAPCAP_BASE}/videos").mock(
        return_value=httpx.Response(500, text="server down")
    )

    clients = _build_clients(with_zapcap=True)
    result = await process_one_click_image_vid_row(_row(zapcap=True), clients, job_id="j")

    assert result.status == STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS
    assert len(result.video_urls) == 1
    assert result.metadata.get("zapcap_applied") is False


# ── Failure statuses ─────────────────────────────────────────────────────────


@respx.mock
async def test_article_fetch_failure() -> None:
    _register_default_openai_routes()
    _register_default_kie_routes()
    _register_default_rendi_routes()
    _register_downloads(_make_collage_png(200))

    clients = _build_clients(article_fail=True)
    result = await process_one_click_image_vid_row(_row(), clients, job_id="j")

    assert result.status == STATUS_ARTICLE_FETCH_FAILED
    assert result.video_urls == []


@respx.mock
async def test_missing_source_image_download_failure() -> None:
    _register_default_openai_routes()
    _register_default_kie_routes()
    _register_default_rendi_routes()
    respx.get("https://example.com/seed.png").mock(return_value=httpx.Response(404))

    clients = _build_clients()
    result = await process_one_click_image_vid_row(_row(), clients, job_id="j")

    assert result.status == STATUS_IMAGE_DOWNLOAD_FAILED
    assert result.video_urls == []


@respx.mock
async def test_image_gen_failure() -> None:
    _register_default_openai_routes()
    _register_default_rendi_routes()
    _register_downloads(_make_collage_png(200))
    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(
        return_value=httpx.Response(200, json={"code": 200, "data": {"taskId": "kie-1"}})
    )
    respx.get(f"{KIE_BASE}/api/v1/jobs/recordInfo").mock(
        return_value=httpx.Response(
            200, json={"code": 200, "data": {"state": "fail", "failMsg": "bad prompt"}}
        )
    )

    clients = _build_clients()
    result = await process_one_click_image_vid_row(_row(), clients, job_id="j")

    assert result.status == STATUS_IMAGE_GEN_FAILED
    assert result.video_urls == []


@respx.mock
async def test_tts_failure() -> None:
    _register_default_openai_routes()
    _register_default_kie_routes()
    _register_default_rendi_routes()
    _register_downloads(_make_collage_png(200))

    clients = _build_clients(tts_fail=True)
    result = await process_one_click_image_vid_row(_row(), clients, job_id="j")

    assert result.status == STATUS_TTS_FAILED
    assert result.video_urls == []


@respx.mock
async def test_rendi_failure_returns_video_assembly_failed() -> None:
    _register_default_openai_routes()
    _register_default_kie_routes()
    _register_downloads(_make_collage_png(200))
    respx.post(f"{RENDI_BASE}/v1/run-ffmpeg-command").mock(
        return_value=httpx.Response(200, json={"command_id": "cmd-fail"})
    )
    respx.get(url__regex=r"https://api\.rendi\.dev/v1/commands/.+").mock(
        return_value=httpx.Response(
            200, json={"status": "FAILED",
                       "error": {"message": "ffmpeg broke", "stderr": "Invalid input"}}
        )
    )

    clients = _build_clients()
    result = await process_one_click_image_vid_row(_row(), clients, job_id="j")

    assert result.status == STATUS_VIDEO_ASSEMBLY_FAILED
    assert result.video_urls == []


# ── Payload round-trip + routing + settings ──────────────────────────────────


def test_payload_round_trip() -> None:
    row = OneClickImageVidRow(
        row_num=4, country="BR", vertical="cars",
        article_url="https://example.com/a",
        manual_image_url="https://m.test/1.png",
        voice_over=True, zapcap=True, aspect_ratio="9:16",
        script_pattern="How To", open_comments="keep it real",
        cta_enabled=True, cta_text="Read More",
    )
    payload = json.loads(_row_to_payload(row, TAB_ONE_CLICK_IMAGE_VID))
    assert payload["__tab__"] == TAB_ONE_CLICK_IMAGE_VID
    restored = payload_to_row(payload)
    assert isinstance(restored, OneClickImageVidRow)
    assert restored == row


def test_runner_routes_one_click_image_vid_row() -> None:
    assert _tab_for_row(_row()) == "one_click_image_vid"


async def test_dispatch_routes_to_processor(monkeypatch) -> None:
    called: dict = {}

    async def _fake(row, clients, *, job_id=None):
        called["hit"] = True
        return SimpleNamespace(row_num=row.row_num)

    monkeypatch.setattr(
        "bulkvid.orchestrator.runner.process_one_click_image_vid_row", _fake
    )
    await _dispatch_to_processor(_row(), SimpleNamespace(), "job")  # type: ignore[arg-type]
    assert called.get("hit") is True


def test_timeout_setting_registered() -> None:
    assert lookup(SETTING_ROW_TIMEOUT_ONE_CLICK_IMAGE_VID) is not None


# ── Story prompt builder ─────────────────────────────────────────────────────


def test_even_clips_sum_to_total() -> None:
    clips = _even_clips(12.5, 4)
    assert len(clips) == 4
    assert round(sum(clips), 3) == 12.5


def test_story_message_is_text_free_four_beat_narrative() -> None:
    msg = _collage_user_message_story("SUBJECT: a person by a car.", "article about cars")
    assert "2x2" in msg.lower() or "2x2 grid" in msg.lower()
    # Four narrative beats, one recurring subject, zero text.
    for beat in ("curiosity", "discovery", "trying it", "happy result"):
        assert beat.lower() in msg.lower()
    assert "same recurring subject" in msg.lower() or "same subject" in msg.lower()
    assert "no text" in msg.lower()
