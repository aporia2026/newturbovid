"""Unit tests for the fast-and-furious tab.

fast-and-furious produces N DIFFERENT Gen-Z creative variations per row (one per
"Number of Videos"), each at its own "Change Size" aspect, each ending WITH its
voiceover (no silent tail). Manual Image 1/2 are shared by all N videos.

Covers:
  - Payload round-trip for ``FastFuriousRow`` (num_videos + aspect_ratios list).
  - Runner routing + dispatch to ``process_fast_furious_row``.
  - N videos out for N = Number of Videos (clamped 1-4).
  - PER-VIDEO aspect ratio (video i uses Change Size i; blank → 9:16).
  - The video ENDS WITH THE VOICEOVER (VO-driven length, no ~3s dead-air tail).
  - Manual images are resolved (downloaded + re-uploaded) ONCE and shared.
  - The Gen-Z planner prompt + larger word budget are used.
  - The verbatim "use this script:" override produces N pinned videos.
  - The prompt + timeout setting are registered.

Plan: ``_plans/2026-07-30-fast-and-furious-tab.md``.
"""

from __future__ import annotations

import io
import json
import wave
from types import SimpleNamespace

import httpx
import pytest
import respx

import bulkvid.orchestrator.row_processor_fast_furious as rpff
from bulkvid.adapters.gemini_tts import TTSResult
from bulkvid.adapters.rendi import RendiOutput, normalize_aspect_ratio
from bulkvid.adapters.storage import UploadResult
from bulkvid.models.row import STATUS_SUCCESS, FastFuriousRow
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.queue import (
    TAB_FAST_FURIOUS,
    _row_to_payload,
    payload_to_row,
)
from bulkvid.orchestrator.row_processor_fast_furious import (
    FF_MAX_WORDS,
    FF_TARGET_WORDS,
    FF_VO_TAIL_SECONDS,
    process_fast_furious_row,
)
from bulkvid.orchestrator.runner import _dispatch_to_processor, _tab_for_row
from bulkvid.orchestrator.runtime_settings import (
    FAST_FURIOUS_PLANNER_PROMPT_DEFAULT,
    SETTING_FAST_FURIOUS_PLANNER_PROMPT,
    SETTING_ROW_TIMEOUT_FAST_FURIOUS,
    lookup,
)
from bulkvid.pipeline.cartoon_prompt import CartoonIdea, CartoonPlan, CartoonShot
from bulkvid.pipeline.language import LanguageResult

PLANNER_MOTION = "planner push-in"
TTS_SECONDS = 6.0


# ── Fakes ────────────────────────────────────────────────────────────────────


class _FakeArticleFetcher:
    async def fetch(self, url: str):
        from bulkvid.adapters.article_fetch import ArticleResult

        return ArticleResult(
            url=url, content="Fast, punchy product story.", source="scrapingbee",
            char_count=26, cost_usd=0.008,
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

    def manual_reuploads(self) -> int:
        return sum(1 for k, _ in self.calls if "fast_furious_images" in k)


class _FakeTTS:
    def __init__(self, duration: float = TTS_SECONDS) -> None:
        self.calls = 0
        self._duration = duration

    async def synthesize(
        self, text: str, language: str, voice: str | None = None,
        style_prompt: str | None = None, country: str = "",
    ) -> TTSResult:
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
    def __init__(self) -> None:
        self.concat_calls: list[dict] = []

    async def concat_clips_with_audio(
        self, clip_urls, audio_url, per_clip_seconds,
        output_filename="out.mp4", *, aspect_ratio="9:16",
        total_video_seconds=None, atempo=None, **_,
    ) -> RendiOutput:
        self.concat_calls.append(
            {"clips": list(clip_urls), "aspect_ratio": aspect_ratio,
             "total_video_seconds": total_video_seconds}
        )
        return RendiOutput(
            url=f"https://r.dev/{output_filename}", cost_usd=0.01,
            command_id=f"cmd-{output_filename}",
        )

    async def cleanup_commands(self, command_ids) -> None:
        return None


def _plan(num_ideas: int, num_shots: int = 2) -> CartoonPlan:
    ideas = [
        CartoonIdea(
            voiceover=f"Variation {i + 1}: a fast, lively Gen-Z line.",
            style_direction="Fast, upbeat, Gen-Z.",
            shots=[
                CartoonShot(scene=f"Scene {i + 1}.{s + 1}", motion=PLANNER_MOTION)
                for s in range(num_shots)
            ],
        )
        for i in range(num_ideas)
    ]
    return CartoonPlan(ideas=ideas, cost_usd=0.001)


@pytest.fixture(autouse=True)
def _stub_pipeline(monkeypatch):
    async def _detect(_client, _body):
        return LanguageResult(language="es", confidence=0.99, cost_usd=0.0, cached=False)

    async def _classify(_client, _text):
        return SimpleNamespace(mode=SimpleNamespace(value="none"), cost_usd=0.0)

    captured: dict = {}

    async def _gen_plan(_client, **kw):
        captured["num_ideas"] = kw.get("num_ideas")
        captured["num_shots"] = kw.get("num_shots")
        captured["planner_prompt_key"] = kw.get("planner_prompt_key")
        captured["target_words"] = kw.get("target_words")
        captured["max_words"] = kw.get("max_words")
        return _plan(kw.get("num_ideas", 1), kw.get("num_shots", 2))

    monkeypatch.setattr(rpff, "detect_language", _detect)
    monkeypatch.setattr(rpff, "classify_open_comments", _classify)
    monkeypatch.setattr(rpff, "generate_cartoon_plan", _gen_plan)
    return captured


def _patch_kie(monkeypatch) -> dict:
    cap: dict = {"t2i": 0, "i2i": 0, "seedance": 0}

    async def _t2i(_kie, prompt, _aspect, resolution="1K", **_):
        cap["t2i"] += 1
        return f"https://kie.test/img-t2i-{cap['t2i']}.png", 0.04

    async def _i2i(_kie, src, prompt, _aspect, resolution="1K", **_):
        cap["i2i"] += 1
        return f"https://kie.test/img-i2i-{cap['i2i']}.png", 0.04

    async def _seedance(_kie, img, motion, _aspect, duration=4, resolution="720p", **_):
        cap["seedance"] += 1
        return f"https://kie.test/clip-{cap['seedance']}.mp4", 0.07

    monkeypatch.setattr(rpff, "nano_banana_2_text_to_image", _t2i)
    monkeypatch.setattr(rpff, "nano_banana_2_image_to_image", _i2i)
    monkeypatch.setattr(rpff, "seedance_image_to_video", _seedance)
    return cap


def _build_clients():
    return PipelineClients(
        openai=SimpleNamespace(),                # type: ignore[arg-type]
        kie=SimpleNamespace(),                   # type: ignore[arg-type]
        tts=_FakeTTS(),                          # type: ignore[arg-type]
        rendi=_FakeRendi(),                      # type: ignore[arg-type]
        storage=_FakeStorageClient(),            # type: ignore[arg-type]
        article=_FakeArticleFetcher(),           # type: ignore[arg-type]
        zapcap=None,
    )


def _row(
    *, num_videos: int = 1, aspect_ratios: list[str] | None = None,
    manual1: str = "", manual2: str = "", vo: bool = True, open_comments: str = "",
) -> FastFuriousRow:
    return FastFuriousRow(
        row_num=2, country="GB", vertical="Weight Loss Injections PR",
        article_url="https://example.com/article",
        manual_image_1=manual1, manual_image_2=manual2,
        num_videos=num_videos, voice_over=vo, zapcap=False,
        aspect_ratios=aspect_ratios if aspect_ratios is not None else [],
        script_pattern="", open_comments=open_comments,
    )


def _register_downloads() -> None:
    respx.get(url__regex=r"https://r\.dev/.+\.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00fake-mp4")
    )
    respx.get(url__regex=r"https://img\.test/.+").mock(
        return_value=httpx.Response(200, content=b"\x89PNG\x00manual")
    )


# ── Payload round-trip + routing ─────────────────────────────────────────────


def test_fast_furious_row_payload_round_trip() -> None:
    row = FastFuriousRow(
        row_num=4, country="DE", vertical="Home Loans PR",
        article_url="https://example.com/a",
        manual_image_1="https://m.test/1.png", manual_image_2="",
        num_videos=3, voice_over=True, zapcap=True,
        aspect_ratios=["9:16", "16:9", "1:1"],
        script_pattern="How To", open_comments="use this script: hello world",
        cta_enabled=True, cta_text="Read More",
    )
    payload = json.loads(_row_to_payload(row, TAB_FAST_FURIOUS))
    assert payload["__tab__"] == TAB_FAST_FURIOUS
    restored = payload_to_row(payload)
    assert isinstance(restored, FastFuriousRow)
    assert restored == row


def test_runner_routes_fast_furious_row() -> None:
    assert _tab_for_row(_row()) == "fast_furious"


async def test_dispatch_routes_to_fast_furious_processor(monkeypatch) -> None:
    called: dict = {}

    async def _fake(row, clients, *, job_id=None):
        called["hit"] = True
        return SimpleNamespace(row_num=row.row_num)

    monkeypatch.setattr(
        "bulkvid.orchestrator.runner.process_fast_furious_row", _fake
    )
    await _dispatch_to_processor(_row(), SimpleNamespace(), "job")  # type: ignore[arg-type]
    assert called.get("hit") is True


# ── N videos + per-video aspect ──────────────────────────────────────────────


@respx.mock
async def test_fast_furious_makes_n_videos(monkeypatch) -> None:
    _register_downloads()
    _patch_kie(monkeypatch)
    clients = _build_clients()

    result = await process_fast_furious_row(_row(num_videos=3), clients, job_id="j")

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 3
    assert len(clients.rendi.concat_calls) == 3
    assert clients.tts.calls == 3          # a distinct VO per variation
    assert result.metadata["num_videos"] == 3


@respx.mock
async def test_fast_furious_num_videos_clamped(monkeypatch) -> None:
    _register_downloads()
    _patch_kie(monkeypatch)
    clients = _build_clients()

    result = await process_fast_furious_row(_row(num_videos=9), clients, job_id="j")

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 4     # clamped to FF_MAX_VIDEOS


@respx.mock
async def test_fast_furious_per_video_aspect(monkeypatch, _stub_pipeline) -> None:
    _register_downloads()
    _patch_kie(monkeypatch)
    clients = _build_clients()

    await process_fast_furious_row(
        _row(num_videos=2, aspect_ratios=["16:9", "1:1"]), clients, job_id="j"
    )

    used = [c["aspect_ratio"] for c in clients.rendi.concat_calls]
    assert used == [normalize_aspect_ratio("16:9"), normalize_aspect_ratio("1:1")]


@respx.mock
async def test_fast_furious_blank_change_size_defaults_9_16(monkeypatch) -> None:
    _register_downloads()
    _patch_kie(monkeypatch)
    clients = _build_clients()

    # Two videos, only the first size supplied → video 2 defaults to 9:16.
    await process_fast_furious_row(
        _row(num_videos=2, aspect_ratios=["16:9"]), clients, job_id="j"
    )

    used = [c["aspect_ratio"] for c in clients.rendi.concat_calls]
    assert used == [normalize_aspect_ratio("16:9"), normalize_aspect_ratio("9:16")]


# ── No dead-air tail ─────────────────────────────────────────────────────────


@respx.mock
async def test_fast_furious_video_ends_with_voiceover(monkeypatch) -> None:
    _register_downloads()
    _patch_kie(monkeypatch)
    clients = _build_clients()

    result = await process_fast_furious_row(_row(num_videos=2), clients, job_id="j")

    assert result.status == STATUS_SUCCESS
    # TTS is 6.0s at atempo 1.0 → video length is VO-driven (effective + tail),
    # never padded to 8s → no ~3s silent tail.
    for call in clients.rendi.concat_calls:
        assert call["total_video_seconds"] == pytest.approx(
            TTS_SECONDS + FF_VO_TAIL_SECONDS, abs=0.05
        )
        assert call["total_video_seconds"] < 8.0


@respx.mock
async def test_fast_furious_voice_over_off_keeps_flat_window(monkeypatch) -> None:
    _register_downloads()
    _patch_kie(monkeypatch)
    clients = _build_clients()

    result = await process_fast_furious_row(_row(num_videos=1, vo=False), clients, job_id="j")

    assert result.status == STATUS_SUCCESS
    assert clients.tts.calls == 0
    assert clients.rendi.concat_calls[0]["total_video_seconds"] == pytest.approx(8.0)


# ── Shared manual images (resolved once) ─────────────────────────────────────


@respx.mock
async def test_fast_furious_manual_images_shared_resolved_once(monkeypatch) -> None:
    _register_downloads()
    cap = _patch_kie(monkeypatch)
    clients = _build_clients()

    # manual1 set, 3 videos → the manual image is downloaded + re-uploaded ONCE
    # (shared), not once per video. Shot 1 uses it; shot 2 is generated per video.
    result = await process_fast_furious_row(
        _row(num_videos=3, manual1="https://img.test/a.png"), clients, job_id="j"
    )

    assert result.status == STATUS_SUCCESS
    assert clients.storage.manual_reuploads() == 1     # shared, once
    assert cap["t2i"] == 0                              # shot1 is the pasted image
    assert cap["i2i"] == 3                              # shot2 generated per video
    assert result.metadata["manual_image_1_resolved"] is True
    assert result.metadata["manual_image_2_resolved"] is False


# ── Gen-Z prompt + larger word budget ────────────────────────────────────────


@respx.mock
async def test_fast_furious_uses_gen_z_prompt_and_bigger_budget(
    monkeypatch, _stub_pipeline
) -> None:
    _register_downloads()
    _patch_kie(monkeypatch)
    clients = _build_clients()

    await process_fast_furious_row(_row(num_videos=2), clients, job_id="j")

    assert _stub_pipeline["num_ideas"] == 2
    assert _stub_pipeline["planner_prompt_key"] == SETTING_FAST_FURIOUS_PLANNER_PROMPT
    assert _stub_pipeline["target_words"] == FF_TARGET_WORDS
    assert _stub_pipeline["max_words"] == FF_MAX_WORDS
    assert FF_TARGET_WORDS > 10


# ── Verbatim "use this script:" override → N pinned videos ───────────────────


@respx.mock
async def test_fast_furious_pinned_override_makes_n_videos(monkeypatch) -> None:
    from bulkvid.pipeline.open_comments import OpenCommentsAnalysis, OpenCommentsMode

    pinned = "Speak these exact words over every variation, please."

    async def _classify_override(_client, _text):
        return OpenCommentsAnalysis(
            mode=OpenCommentsMode.OVERRIDE, raw_text=_text, override_script=pinned
        )

    captured: list[dict] = []

    async def _fake_build_pinned(**kw):
        captured.append(kw)
        return SimpleNamespace(final_url="https://storage.test/pinned.mp4",
                               zapcap_failed=False, error=None)

    monkeypatch.setattr(rpff, "classify_open_comments", _classify_override)
    monkeypatch.setattr(rpff, "build_pinned_cartoon_video", _fake_build_pinned)
    monkeypatch.setattr(rpff, "fold_pinned_costs", lambda costs, res: None)
    _register_downloads()
    clients = _build_clients()

    result = await process_fast_furious_row(
        _row(num_videos=2, manual1="https://img.test/a.png",
             manual2="https://img.test/b.png",
             open_comments=f"use this script: {pinned}"),
        clients, job_id="jp",
    )

    assert result.status == STATUS_SUCCESS
    assert result.metadata["script_used_override"] is True
    assert len(result.video_urls) == 2                 # one pinned video per variation
    assert len(captured) == 2
    assert all(kw["pinned_script"] == pinned for kw in captured)


# ── Settings registry ────────────────────────────────────────────────────────


def test_fast_furious_settings_registered() -> None:
    prompt = lookup(SETTING_FAST_FURIOUS_PLANNER_PROMPT)
    timeout = lookup(SETTING_ROW_TIMEOUT_FAST_FURIOUS)
    assert prompt is not None and prompt.multiline is True
    assert timeout is not None
    low = FAST_FURIOUS_PLANNER_PROMPT_DEFAULT.lower()
    assert "tiktok" in low
    assert "photographic" in low or "realistic" in low
    assert "compliance" in low
