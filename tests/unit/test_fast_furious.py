"""Unit + integration tests for the fast-and-furious tab.

fast-and-furious produces N DIFFERENT Gen-Z variations per row (one per "Number
of Videos"), each its own punchy script, each at its own "Change Size" aspect,
each routed through the shared ``build_pinned_cartoon_video`` (which sizes the
video TO the voiceover — no dead-air tail — and speaks the override verbatim).

These exercise the REAL builder (kie patched at the ``pinned_cartoon`` layer),
mirroring the google-simple-motion test. Covers:
  - Payload round-trip + runner routing/dispatch.
  - N videos out for N = Number of Videos, each at its Change Size.
  - Each variation speaks its OWN generated Gen-Z script (distinct per video).
  - ``use this script:`` → EVERY variation speaks the operator text verbatim.
  - The video length is driven BY the voiceover (no fixed padding).
  - The fast-and-furious planner prompt + larger word budget are used.
  - Voice Over = No → silent videos (no TTS). Settings registered.
"""

from __future__ import annotations

import io
import json
import wave
from types import SimpleNamespace

import httpx
import pytest
import respx

import bulkvid.orchestrator.pinned_cartoon as pc
import bulkvid.orchestrator.row_processor_fast_furious as rpff
from bulkvid.adapters.gemini_tts import TTSResult
from bulkvid.adapters.rendi import RendiOutput
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
    FF_VO_ATEMPO,
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
from bulkvid.pipeline.yt_cartoon import VO_TAIL_SECONDS

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
    def __init__(self, seconds: float = 8.0) -> None:
        self.calls = 0
        self.texts: list[str] = []
        self._seconds = seconds

    async def synthesize(
        self, text: str, language: str, voice: str | None = None,
        style_prompt: str | None = None, country: str = "",
    ) -> TTSResult:
        self.calls += 1
        self.texts.append(text)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24_000)
            wf.writeframes(b"\x00" * int(24_000 * 2 * self._seconds))
        return TTSResult(
            wav_bytes=buf.getvalue(), voice=voice or "Kore", language=language,
            duration_seconds=self._seconds, character_count=len(text), cost_usd=0.003,
        )


class _FakeRendi:
    def __init__(self) -> None:
        self.concat_calls: list[dict] = []
        self.music_mixes: list[dict] = []

    async def concat_clips_with_audio(
        self, clip_urls, audio_url, per_clip_seconds,
        output_filename="out.mp4", *, aspect_ratio="9:16",
        total_video_seconds=None, atempo=None, **_,
    ) -> RendiOutput:
        self.concat_calls.append({
            "clips": list(clip_urls), "audio": audio_url,
            "aspect": aspect_ratio, "total": total_video_seconds, "atempo": atempo,
        })
        return RendiOutput(
            url=f"https://r.dev/{output_filename}", cost_usd=0.01,
            command_id=f"cmd-{output_filename}",
        )

    async def mix_music(self, video_url, music_url, output_filename="out.mp4", **_) -> RendiOutput:
        self.music_mixes.append({"video": video_url, "music": music_url})
        return RendiOutput(
            url=f"https://r.dev/{output_filename}", cost_usd=0.01,
            command_id=f"cmd-{output_filename}",
        )

    async def cleanup_commands(self, command_ids) -> None:
        return None


def _plan(num_ideas: int) -> CartoonPlan:
    return CartoonPlan(
        ideas=[
            CartoonIdea(
                voiceover=f"Variation {i + 1}: a fast, lively Gen-Z line here.",
                style_direction="Fast, upbeat, Gen-Z.",
                shots=[
                    CartoonShot(scene=f"Scene {i + 1}.1", motion="planner push"),
                    CartoonShot(scene=f"Scene {i + 1}.2", motion="planner pan"),
                ],
            )
            for i in range(num_ideas)
        ],
        cost_usd=0.001,
    )


@pytest.fixture(autouse=True)
def _stub_pipeline(monkeypatch):
    captured: dict = {}

    async def _detect(_c, _b):
        return LanguageResult(language="en", confidence=0.99, cost_usd=0.0, cached=False)

    async def _classify(_c, _t):
        return SimpleNamespace(mode=SimpleNamespace(value="none"), cost_usd=0.0)

    async def _safety(_store, _vertical, _row):
        return SimpleNamespace(matched=False, matched_keyword=None)

    async def _gen_plan(_c, **kw):
        captured["planner_prompt_key"] = kw.get("planner_prompt_key")
        captured["target_words"] = kw.get("target_words")
        captured["max_words"] = kw.get("max_words")
        captured["num_ideas"] = kw.get("num_ideas")
        return _plan(kw.get("num_ideas", 1))

    monkeypatch.setattr(rpff, "detect_language", _detect)
    monkeypatch.setattr(rpff, "classify_open_comments", _classify)
    monkeypatch.setattr(rpff, "resolve_safety", _safety)
    monkeypatch.setattr(rpff, "generate_cartoon_plan", _gen_plan)
    return captured


def _patch_kie(monkeypatch) -> dict:
    """Patch kie in the builder (pc) namespace — the shots are generated there."""
    cap = {"t2i": 0, "i2i": 0, "seedance": 0}

    async def _t2i(_kie, _prompt, _aspect, resolution="1K", **_):
        cap["t2i"] += 1
        return f"https://kie.test/t2i-{cap['t2i']}.png", 0.04

    async def _i2i(_kie, _src, _prompt, _aspect, resolution="1K", **_):
        cap["i2i"] += 1
        return f"https://kie.test/i2i-{cap['i2i']}.png", 0.04

    async def _seed(_kie, _img, _motion, _aspect, duration=4, resolution="720p", **_):
        cap["seedance"] += 1
        return f"https://kie.test/clip-{cap['seedance']}.mp4", 0.07

    monkeypatch.setattr(pc, "nano_banana_2_text_to_image", _t2i)
    monkeypatch.setattr(pc, "nano_banana_2_image_to_image", _i2i)
    monkeypatch.setattr(pc, "seedance_image_to_video", _seed)
    return cap


def _clients(tts_seconds: float = 8.0):
    return PipelineClients(
        openai=SimpleNamespace(),                # type: ignore[arg-type]
        kie=SimpleNamespace(),                   # type: ignore[arg-type]
        tts=_FakeTTS(tts_seconds),               # type: ignore[arg-type]
        rendi=_FakeRendi(),                      # type: ignore[arg-type]
        storage=_FakeStorageClient(),            # type: ignore[arg-type]
        article=_FakeArticleFetcher(),           # type: ignore[arg-type]
        zapcap=None,
    )


def _row(*, num_videos=2, aspects=None, m1="", m2="", vo=True,
         open_comments="") -> FastFuriousRow:
    return FastFuriousRow(
        row_num=2, country="GB", vertical="Weight Loss Injections PR",
        article_url="https://example.com/article",
        manual_image_1=m1, manual_image_2=m2,
        num_videos=num_videos, voice_over=vo, zapcap=False,
        aspect_ratios=aspects or ["", "", "", ""],
        script_pattern="", open_comments=open_comments,
        cta_enabled=False, cta_text="",
    )


def _register_downloads() -> None:
    respx.get(url__regex=r"https://r\.dev/.+\.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00fake-mp4")
    )
    respx.get(url__regex=r"https://manual\.test/.+").mock(
        return_value=httpx.Response(200, content=b"\x89PNG\x00manual")
    )
    respx.get(url__regex=r"https://storage\.test/.+").mock(
        return_value=httpx.Response(200, content=b"\x89PNG\x00stored")
    )
    respx.get(url__regex=r"https://kie\.test/.+").mock(
        return_value=httpx.Response(200, content=b"\x89PNG\x00ai")
    )


# ── Round-trip + routing ─────────────────────────────────────────────────────


def test_row_payload_round_trip() -> None:
    row = _row(num_videos=3, aspects=["9:16", "16:9", "1:1", ""])
    payload = json.loads(_row_to_payload(row, TAB_FAST_FURIOUS))
    assert payload["__tab__"] == TAB_FAST_FURIOUS
    restored = payload_to_row(payload)
    assert isinstance(restored, FastFuriousRow)
    assert restored == row


def test_runner_routes_row() -> None:
    assert _tab_for_row(_row()) == "fast_furious"


async def test_dispatch_routes_to_processor(monkeypatch) -> None:
    called: dict = {}

    async def _fake(row, clients, *, job_id=None):
        called["hit"] = True
        return SimpleNamespace(row_num=row.row_num)

    monkeypatch.setattr(
        "bulkvid.orchestrator.runner.process_fast_furious_row", _fake
    )
    await _dispatch_to_processor(_row(), SimpleNamespace(), "job")  # type: ignore[arg-type]
    assert called.get("hit") is True


# ── N videos, per-variation aspect, distinct Gen-Z scripts ───────────────────


@respx.mock
async def test_n_videos_each_own_script_and_aspect(monkeypatch) -> None:
    _register_downloads()
    _patch_kie(monkeypatch)
    clients = _clients()

    result = await process_fast_furious_row(
        _row(num_videos=3, aspects=["16:9", "1:1", "9:16", ""]), clients, job_id="j"
    )

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 3 and all(result.video_urls)
    # Each variation speaks its OWN generated Gen-Z line (distinct scripts) — one
    # TTS per variation.
    assert clients.tts.calls == 3
    assert set(clients.tts.texts) == {
        "Variation 1: a fast, lively Gen-Z line here.",
        "Variation 2: a fast, lively Gen-Z line here.",
        "Variation 3: a fast, lively Gen-Z line here.",
    }
    # Each variation rendered at its own Change Size.
    from bulkvid.adapters.rendi import normalize_aspect_ratio
    aspects_used = [c["aspect"] for c in clients.rendi.concat_calls]
    assert aspects_used == [
        normalize_aspect_ratio("16:9"),
        normalize_aspect_ratio("1:1"),
        normalize_aspect_ratio("9:16"),
    ]


@respx.mock
async def test_num_videos_clamped(monkeypatch) -> None:
    _register_downloads()
    _patch_kie(monkeypatch)
    clients = _clients()

    result = await process_fast_furious_row(_row(num_videos=9), clients, job_id="j")

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 4     # clamped to FF_MAX_VIDEOS


@respx.mock
async def test_uses_gen_z_prompt_and_bigger_budget(monkeypatch, _stub_pipeline) -> None:
    _register_downloads()
    _patch_kie(monkeypatch)
    clients = _clients()

    await process_fast_furious_row(_row(num_videos=2), clients, job_id="j")

    assert _stub_pipeline["planner_prompt_key"] == SETTING_FAST_FURIOUS_PLANNER_PROMPT
    assert _stub_pipeline["num_ideas"] == 2
    assert _stub_pipeline["target_words"] == FF_TARGET_WORDS
    assert _stub_pipeline["max_words"] == FF_MAX_WORDS
    assert FF_TARGET_WORDS > 10


# ── No dead-air: video length driven by the voiceover ────────────────────────


@respx.mock
async def test_video_length_follows_sped_voiceover(monkeypatch) -> None:
    _register_downloads()
    _patch_kie(monkeypatch)
    clients = _clients(tts_seconds=12.0)   # sped length > FF_MIN_VIDEO_SECONDS floor

    await process_fast_furious_row(_row(num_videos=1), clients, job_id="j")

    # Livelier: the VO plays at FF_VO_ATEMPO, and the video is sized to the SPED
    # length (+ tail) — no fixed window, no trailing silence.
    assert clients.rendi.concat_calls[0]["atempo"] == pytest.approx(FF_VO_ATEMPO)
    total = clients.rendi.concat_calls[0]["total"]
    assert total == pytest.approx(12.0 / FF_VO_ATEMPO + VO_TAIL_SECONDS, abs=0.05)


@respx.mock
async def test_voice_over_off_is_silent(monkeypatch) -> None:
    _register_downloads()
    _patch_kie(monkeypatch)
    clients = _clients()

    result = await process_fast_furious_row(_row(num_videos=2, vo=False), clients, job_id="j")

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 2 and all(result.video_urls)
    assert clients.tts.calls == 0
    assert all(c["audio"] is None for c in clients.rendi.concat_calls)
    # No VO → no music to duck under (mix_music needs existing audio).
    assert clients.rendi.music_mixes == []


# ── Energetic background music ───────────────────────────────────────────────


@respx.mock
async def test_energetic_music_ducked_once_per_variation(monkeypatch) -> None:
    _register_downloads()
    _patch_kie(monkeypatch)
    clients = _clients()

    result = await process_fast_furious_row(_row(num_videos=3), clients, job_id="j")

    assert result.status == STATUS_SUCCESS
    # One energetic track picked + uploaded once, ducked under EACH variation.
    assert result.metadata.get("music_track")
    assert len(clients.rendi.music_mixes) == 3
    music_urls = {m["music"] for m in clients.rendi.music_mixes}
    assert len(music_urls) == 1                       # shared across variations
    assert "fast_furious_music" in next(iter(music_urls))
    # The final videos are the music-mixed outputs.
    assert all("videos_music" in u for u in result.video_urls)


# ── TikTok-low CTA + caption positions ───────────────────────────────────────


@respx.mock
async def test_tiktok_low_cta_and_caption_positions(monkeypatch) -> None:
    from bulkvid.orchestrator.row_processor_fast_furious import (
        FF_CAPTION_TOP_WITH_CTA,
        FF_CTA_BOTTOM_MARGIN_FRAC,
    )

    _register_downloads()
    _patch_kie(monkeypatch)
    clients = _clients()

    cta_margins: list[float] = []

    def _fake_cta(cta_text, *, canvas_width, canvas_height, bottom_margin_frac=None, **_):
        cta_margins.append(bottom_margin_frac)
        return b"\x89PNG\x00cta"

    captured_opts: list = []

    async def _fake_build(**kw):
        captured_opts.append(kw.get("zapcap_render_options"))
        return SimpleNamespace(
            final_url="https://storage.test/v.mp4", zapcap_failed=False, error=None
        )

    monkeypatch.setattr(rpff, "render_cartoon_cta_overlay_bytes", _fake_cta)
    monkeypatch.setattr(rpff, "build_pinned_cartoon_video", _fake_build)
    monkeypatch.setattr(rpff, "fold_pinned_costs", lambda costs, res: None)

    row = _row(num_videos=1)
    row.cta_enabled = True
    result = await process_fast_furious_row(row, clients, job_id="j")

    assert result.status == STATUS_SUCCESS
    # CTA pill rendered LOW (TikTok margin), not the cartoon default 0.19.
    assert cta_margins == [FF_CTA_BOTTOM_MARGIN_FRAC]
    assert FF_CTA_BOTTOM_MARGIN_FRAC < 0.19
    # Captions positioned low, just above the pill (higher `top` = lower on screen).
    assert captured_opts[0] is not None
    assert captured_opts[0].style.top == FF_CAPTION_TOP_WITH_CTA
    assert FF_CAPTION_TOP_WITH_CTA > 30      # lower than the old fixed top=30


# ── Shared manual images + verbatim override ─────────────────────────────────


@respx.mock
async def test_manual_images_shared_resolved_once(monkeypatch) -> None:
    _register_downloads()
    _patch_kie(monkeypatch)
    clients = _clients()

    # manual1 set, 3 videos → the manual image is downloaded + re-uploaded ONCE
    # (shared), not once per variation.
    result = await process_fast_furious_row(
        _row(num_videos=3, m1="https://manual.test/a.png"), clients, job_id="j"
    )

    assert result.status == STATUS_SUCCESS
    assert clients.storage.manual_reuploads() == 1
    assert result.metadata["manual_image_1_resolved"] is True
    assert result.metadata["manual_image_2_resolved"] is False


@respx.mock
async def test_use_this_script_override_every_variation(monkeypatch) -> None:
    from bulkvid.pipeline.open_comments import OpenCommentsAnalysis, OpenCommentsMode

    pinned = "Speak these exact words over every variation, please."

    async def _classify_override(_c, _t):
        return OpenCommentsAnalysis(
            mode=OpenCommentsMode.OVERRIDE, raw_text=_t, override_script=pinned
        )

    monkeypatch.setattr(rpff, "classify_open_comments", _classify_override)
    _register_downloads()
    _patch_kie(monkeypatch)
    clients = _clients()

    result = await process_fast_furious_row(
        _row(num_videos=2, open_comments=f"use this script: {pinned}"),
        clients, job_id="jp",
    )

    assert result.status == STATUS_SUCCESS
    assert result.metadata["script_used_override"] is True
    assert len(result.video_urls) == 2 and all(result.video_urls)
    # Every variation spoke the EXACT operator text (verbatim), not a generated one.
    assert clients.tts.calls == 2
    assert set(clients.tts.texts) == {pinned}


# ── Settings registry ────────────────────────────────────────────────────────


def test_settings_registered() -> None:
    prompt = lookup(SETTING_FAST_FURIOUS_PLANNER_PROMPT)
    timeout = lookup(SETTING_ROW_TIMEOUT_FAST_FURIOUS)
    assert prompt is not None and prompt.multiline is True
    assert timeout is not None
    low = FAST_FURIOUS_PLANNER_PROMPT_DEFAULT.lower()
    assert "tiktok" in low
    assert "photographic" in low or "realistic" in low
    assert "compliance" in low
