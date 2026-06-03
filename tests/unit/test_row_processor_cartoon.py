"""Integration-style tests for the cartoon row processor.

Collaborators (kie wrappers, the planner, language/open-comments classifiers)
are stubbed in the processor's module namespace so the test is deterministic and
focuses on the processor's own orchestration + graceful-degradation logic. The
kie/planner internals have their own unit tests.

Covers:
  - Happy path -> 2 videos, STATUS_SUCCESS, tab metadata
  - Voice Over = No -> 2 videos, no TTS
  - Article fetch failure -> STATUS_ARTICLE_FETCH_FAILED
  - A failed LATER shot image is held (still 2 videos)
  - A failed shot animation is gap-filled (still 2 videos)
  - All animations fail -> STATUS_VIDEO_ASSEMBLY_FAILED
  - One idea fully fails -> the other still ships (1 video, SUCCESS)
  - ZapCap=Yes -> 2 captioned videos
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import httpx
import pytest
import respx

import bulkvid.orchestrator.row_processor_cartoon as rpc
from bulkvid.adapters.gemini_tts import TTSResult
from bulkvid.adapters.rendi import RendiOutput
from bulkvid.adapters.storage import UploadResult
from bulkvid.models.row import (
    STATUS_ARTICLE_FETCH_FAILED,
    STATUS_SUCCESS,
    STATUS_VIDEO_ASSEMBLY_FAILED,
    CartoonRow,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.row_processor_cartoon import process_cartoon_row
from bulkvid.pipeline.cartoon_prompt import CartoonIdea, CartoonPlan, CartoonShot

# ── Fakes ─────────────────────────────────────────────────────────────────────


class _FakeArticleFetcher:
    def __init__(self, fail: bool = False) -> None:
        self._fail = fail

    async def fetch(self, url: str):
        if self._fail:
            from bulkvid.adapters.article_fetch import ArticleFetchError

            raise ArticleFetchError("simulated fetch failure")
        from bulkvid.adapters.article_fetch import ArticleResult

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
    def __init__(self, duration: float = 6.0) -> None:
        self.calls = 0
        self._duration = duration

    async def synthesize(
        self, text: str, language: str, voice: str | None = None,
        style_prompt: str | None = None, country: str = "",
    ) -> TTSResult:
        self.calls += 1
        import wave

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
    def __init__(self) -> None:
        self.concat_calls: list[dict] = []

    async def concat_clips_with_audio(
        self, clip_urls, audio_url, per_clip_seconds,
        output_filename="out.mp4", *, aspect_ratio="9:16",
        total_video_seconds=None, **_,
    ) -> RendiOutput:
        self.concat_calls.append(
            {
                "clips": list(clip_urls),
                "audio": audio_url,
                "per_clip": per_clip_seconds,
                "out": output_filename,
                "total_video_seconds": total_video_seconds,
            }
        )
        return RendiOutput(
            url=f"https://r.dev/{output_filename}", cost_usd=0.01,
            command_id=f"cmd-{output_filename}",
        )

    async def cleanup_commands(self, command_ids) -> None:
        return None


class _FakeZapCap:
    async def caption_video(self, video_bytes, language, filename):
        return f"https://zc.test/{filename}", 0.1


# ── Stub collaborators (patched into the processor namespace) ─────────────────


def _plan(num_ideas: int = 2, num_shots: int = 2) -> CartoonPlan:
    ideas = [
        CartoonIdea(
            voiceover=f"Voiceover idea {i + 1} about cheaper cars.",
            style_direction="Upbeat.",
            shots=[
                CartoonShot(scene=f"Scene {i+1}.{s+1}", motion="gentle push-in")
                for s in range(num_shots)
            ],
        )
        for i in range(num_ideas)
    ]
    return CartoonPlan(ideas=ideas, cost_usd=0.001)


@pytest.fixture(autouse=True)
def _stub_pipeline(monkeypatch):
    async def _detect(_client, _body):
        return SimpleNamespace(language="en", cost_usd=0.0)

    async def _classify(_client, _text):
        return SimpleNamespace(mode=SimpleNamespace(value="none"), cost_usd=0.0)

    async def _gen_plan(_client, **_kw):
        return _plan(_kw.get("num_ideas", 2), _kw.get("num_shots", 2))

    monkeypatch.setattr(rpc, "detect_language", _detect)
    monkeypatch.setattr(rpc, "classify_open_comments", _classify)
    monkeypatch.setattr(rpc, "generate_cartoon_plan", _gen_plan)


def _patch_kie(
    monkeypatch,
    *,
    t2i_fail_on: int | None = None,
    i2i_fail_on: int | None = None,
    seedance_fail_on: int | None = None,
    seedance_fail_all: bool = False,
):
    """Patch the three kie wrappers with counters + optional failure injection."""
    counters = {"t2i": 0, "i2i": 0, "seedance": 0}

    async def _t2i(_kie, _prompt, _aspect, resolution="1K", **_):
        counters["t2i"] += 1
        if t2i_fail_on is not None and counters["t2i"] == t2i_fail_on:
            raise RuntimeError("t2i boom")
        return f"https://kie.test/img-t2i-{counters['t2i']}.png", 0.04

    async def _i2i(_kie, _src, _prompt, _aspect, resolution="1K", **_):
        counters["i2i"] += 1
        if i2i_fail_on is not None and counters["i2i"] == i2i_fail_on:
            raise RuntimeError("i2i boom")
        return f"https://kie.test/img-i2i-{counters['i2i']}.png", 0.04

    async def _seedance(_kie, _img, _motion, _aspect, duration=4, resolution="720p", **_):
        counters["seedance"] += 1
        if seedance_fail_all or (
            seedance_fail_on is not None and counters["seedance"] == seedance_fail_on
        ):
            raise RuntimeError("seedance boom")
        return f"https://kie.test/clip-{counters['seedance']}.mp4", 0.07

    monkeypatch.setattr(rpc, "nano_banana_2_text_to_image", _t2i)
    monkeypatch.setattr(rpc, "nano_banana_2_image_to_image", _i2i)
    monkeypatch.setattr(rpc, "seedance_image_to_video", _seedance)
    return counters


def _build_clients(*, article_fail: bool = False, with_zapcap: bool = False):
    return PipelineClients(
        openai=SimpleNamespace(),                         # type: ignore[arg-type]
        kie=SimpleNamespace(),                            # type: ignore[arg-type]
        tts=_FakeTTS(),                                   # type: ignore[arg-type]
        rendi=_FakeRendi(),                               # type: ignore[arg-type]
        storage=_FakeStorageClient(),                     # type: ignore[arg-type]
        article=_FakeArticleFetcher(fail=article_fail),   # type: ignore[arg-type]
        zapcap=_FakeZapCap() if with_zapcap else None,    # type: ignore[arg-type]
    )


def _row(*, vo: bool = True, zapcap: bool = False) -> CartoonRow:
    return CartoonRow(
        row_num=2, country="MX", vertical="automotive",
        article_url="https://example.com/article",
        voice_over=vo, zapcap=zapcap, aspect_ratio="09:16",
        script_pattern="How To", open_comments="",
    )


def _register_downloads() -> None:
    respx.get(url__regex=r"https://r\.dev/.+\.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00fake-mp4")
    )
    respx.get(url__regex=r"https://zc\.test/.+\.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00captioned")
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


@respx.mock
async def test_cartoon_happy_path_two_videos(monkeypatch) -> None:
    _patch_kie(monkeypatch)
    _register_downloads()
    clients = _build_clients()

    result = await process_cartoon_row(_row(), clients, job_id="jobX")

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 2
    assert result.metadata["tab"] == "cartoon"
    assert result.metadata["videos_produced"] == 2
    # Each idea was voiced (2 TTS calls) and stitched (2 concat calls).
    assert clients.tts.calls == 2
    assert len(clients.rendi.concat_calls) == 2
    # The voiceover was wired into the stitch.
    assert all(c["audio"] is not None for c in clients.rendi.concat_calls)


@respx.mock
async def test_cartoon_vo_short_clamped_to_floor(monkeypatch) -> None:
    # Raw 3.5s VO -> effective 2.69s. Soft tail (+0.8s) gives 3.49s, BELOW the
    # 4s floor → clamps UP to 4.0s. Tail silence ≈ 1.3s, not the prior 3s.
    _patch_kie(monkeypatch)
    _register_downloads()
    clients = _build_clients()
    clients.tts = _FakeTTS(duration=3.5)    # type: ignore[assignment]

    result = await process_cartoon_row(_row(), clients, job_id="j")
    assert result.status == STATUS_SUCCESS
    call = clients.rendi.concat_calls[0]
    assert call["total_video_seconds"] == pytest.approx(4.0, abs=0.01)
    assert call["per_clip"] == pytest.approx(2.0, abs=0.01)


@respx.mock
async def test_cartoon_vo_long_clamped_to_ceiling(monkeypatch) -> None:
    # Raw 13s -> effective 10s + 0.8s tail = 10.8s, above the 8s ceiling. Clamps
    # DOWN to 8.0 (regression: pre-fix live runs overshot here).
    _patch_kie(monkeypatch)
    _register_downloads()
    clients = _build_clients()
    clients.tts = _FakeTTS(duration=13.0)    # type: ignore[assignment]

    result = await process_cartoon_row(_row(), clients, job_id="j")
    assert result.status == STATUS_SUCCESS
    call = clients.rendi.concat_calls[0]
    assert call["total_video_seconds"] == pytest.approx(8.0, abs=0.01)
    assert call["per_clip"] == pytest.approx(4.0, abs=0.01)


@respx.mock
async def test_cartoon_vo_in_band_keeps_tail(monkeypatch) -> None:
    # Raw 7s -> effective ~5.38s + 0.8s tail = 6.18s, in the [4, 8]s band.
    # Target follows the VO with the dwell, no clamping either side.
    _patch_kie(monkeypatch)
    _register_downloads()
    clients = _build_clients()
    clients.tts = _FakeTTS(duration=7.0)    # type: ignore[assignment]

    result = await process_cartoon_row(_row(), clients, job_id="j")
    assert result.status == STATUS_SUCCESS
    call = clients.rendi.concat_calls[0]
    expected = 7.0 / 1.3 + 0.8
    assert call["total_video_seconds"] == pytest.approx(expected, abs=0.01)
    assert 4.0 <= call["total_video_seconds"] <= 8.0


@respx.mock
async def test_cartoon_no_vo_uses_default_target(monkeypatch) -> None:
    # No-VO rows keep the existing 3.5s per shot * 2 = 7s default target (in band).
    _patch_kie(monkeypatch)
    _register_downloads()
    clients = _build_clients()

    result = await process_cartoon_row(_row(vo=False), clients, job_id="j")
    assert result.status == STATUS_SUCCESS
    call = clients.rendi.concat_calls[0]
    assert call["audio"] is None
    assert call["total_video_seconds"] == pytest.approx(7.0, abs=0.01)
    assert call["per_clip"] == pytest.approx(3.5, abs=0.01)


@respx.mock
async def test_cartoon_voice_over_no_skips_tts(monkeypatch) -> None:
    _patch_kie(monkeypatch)
    _register_downloads()
    clients = _build_clients()

    result = await process_cartoon_row(_row(vo=False), clients, job_id="j")
    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 2
    assert clients.tts.calls == 0
    assert all(c["audio"] is None for c in clients.rendi.concat_calls)


async def test_cartoon_article_failure(monkeypatch) -> None:
    _patch_kie(monkeypatch)
    clients = _build_clients(article_fail=True)
    result = await process_cartoon_row(_row(), clients, job_id="j")
    assert result.status == STATUS_ARTICLE_FETCH_FAILED
    assert result.video_urls == []


@respx.mock
async def test_cartoon_failed_later_shot_image_is_held(monkeypatch) -> None:
    # Fail one image-to-image call: the shot holds the previous frame, video still ships.
    _patch_kie(monkeypatch, i2i_fail_on=1)
    _register_downloads()
    clients = _build_clients()

    result = await process_cartoon_row(_row(), clients, job_id="j")
    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 2
    # Both stitches still received 2 clips.
    assert all(len(c["clips"]) == 2 for c in clients.rendi.concat_calls)


@respx.mock
async def test_cartoon_failed_animation_is_gap_filled(monkeypatch) -> None:
    # Fail one animation: the gap is filled by a neighbour clip, video still ships.
    _patch_kie(monkeypatch, seedance_fail_on=1)
    _register_downloads()
    clients = _build_clients()

    result = await process_cartoon_row(_row(), clients, job_id="j")
    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 2
    assert all(len(c["clips"]) == 2 for c in clients.rendi.concat_calls)


@respx.mock
async def test_cartoon_all_animations_fail(monkeypatch) -> None:
    _patch_kie(monkeypatch, seedance_fail_all=True)
    _register_downloads()
    clients = _build_clients()

    result = await process_cartoon_row(_row(), clients, job_id="j")
    assert result.status == STATUS_VIDEO_ASSEMBLY_FAILED
    assert result.video_urls == []


@respx.mock
async def test_cartoon_one_idea_fails_other_ships(monkeypatch) -> None:
    # Fail the 2nd first-shot text-to-image -> exactly one idea loses shot 1 and is
    # dropped; the other idea still produces a video.
    _patch_kie(monkeypatch, t2i_fail_on=2)
    _register_downloads()
    clients = _build_clients()

    result = await process_cartoon_row(_row(), clients, job_id="j")
    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 1


@respx.mock
async def test_cartoon_zapcap_applied(monkeypatch) -> None:
    _patch_kie(monkeypatch)
    _register_downloads()
    clients = _build_clients(with_zapcap=True)

    result = await process_cartoon_row(_row(zapcap=True), clients, job_id="j")
    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 2
    assert result.metadata.get("zapcap_applied") is True
    # Captioned outputs were persisted.
    assert any("videos_captioned" in key for key, _ in clients.storage.calls)
