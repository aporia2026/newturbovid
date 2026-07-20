"""Unit + integration tests for the google-simple-motion tab.

This tab speaks a FIXED, localized two-sentence script (3s gap) and produces up
to FOUR size-variant videos per row — the same creative at different aspects.

Covers:
  - Payload round-trip for ``GoogleSimpleMotionRow`` + tab routing.
  - N videos = Number of Videos; each rendered at its Change Size (aspects).
  - Shared voiceover: the script is generated ONCE and TTS'd TWICE (two
    sentences) regardless of N — the builder does NOT re-synthesize.
  - Slot → image mapping: slots 1/2 use the manual images; slots 3/4 generate an
    AI image (image-to-image) using the manual image as reference.
  - Video length floored at 11s.
  - Voice Over = No → silent videos (no script gen, no TTS).
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
import bulkvid.orchestrator.row_processor_google_simple_motion as rpgsm
from bulkvid.adapters.gemini_tts import TTSResult
from bulkvid.adapters.rendi import RendiOutput
from bulkvid.adapters.storage import UploadResult
from bulkvid.models.row import STATUS_SUCCESS, GoogleSimpleMotionRow
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.queue import (
    TAB_GOOGLE_SIMPLE_MOTION,
    _row_to_payload,
    payload_to_row,
)
from bulkvid.orchestrator.row_processor_google_simple_motion import (
    process_google_simple_motion_row,
)
from bulkvid.orchestrator.runner import _dispatch_to_processor, _tab_for_row
from bulkvid.pipeline.cartoon_prompt import (
    CartoonIdea,
    CartoonPlan,
    CartoonShot,
)
from bulkvid.pipeline.google_simple_motion import LearnMoreScript
from bulkvid.pipeline.language import LanguageResult

# ── Fakes ────────────────────────────────────────────────────────────────────


class _FakeArticleFetcher:
    async def fetch(self, url: str):
        from bulkvid.adapters.article_fetch import ArticleResult

        return ArticleResult(
            url=url, content="Realistic car-deals story.", source="scrapingbee",
            char_count=24, cost_usd=0.008,
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
    def __init__(self, seconds: float = 2.0) -> None:
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

    async def concat_clips_with_audio(
        self, clip_urls, audio_url, per_clip_seconds,
        output_filename="out.mp4", *, aspect_ratio="9:16",
        total_video_seconds=None, atempo=None, **_,
    ) -> RendiOutput:
        self.concat_calls.append({
            "clips": list(clip_urls), "audio": audio_url,
            "aspect": aspect_ratio, "total": total_video_seconds,
        })
        return RendiOutput(
            url=f"https://r.dev/{output_filename}", cost_usd=0.01,
            command_id=f"cmd-{output_filename}",
        )

    async def cleanup_commands(self, command_ids) -> None:
        return None


def _plan() -> CartoonPlan:
    return CartoonPlan(
        ideas=[
            CartoonIdea(
                voiceover="unused on this tab",
                style_direction="Warm.",
                shots=[
                    CartoonShot(scene="Scene one.", motion="planner push"),
                    CartoonShot(scene="Scene two.", motion="planner pan"),
                ],
            )
        ],
        cost_usd=0.001,
    )


def _script() -> LearnMoreScript:
    return LearnMoreScript(
        subject="the new car deals",
        sentence1_variants=[
            "Explore more about the new car deals.",
            "Learn more about the new car deals.",
            "Read more about the new car deals.",
        ],
        sentence2="Discover key details and useful information about the new car deals.",
        cost_usd=0.001,
    )


@pytest.fixture(autouse=True)
def _stub_pipeline(monkeypatch):
    async def _detect(_c, _b):
        return LanguageResult(language="de", confidence=0.99, cost_usd=0.0, cached=False)

    async def _classify(_c, _t):
        return SimpleNamespace(mode=SimpleNamespace(value="none"), cost_usd=0.0)

    async def _safety(_store, _vertical, _row):
        return SimpleNamespace(matched=False, matched_keyword=None)

    async def _gen_plan(_c, **_kw):
        return _plan()

    async def _gen_script(_c, **_kw):
        return _script()

    monkeypatch.setattr(rpgsm, "detect_language", _detect)
    monkeypatch.setattr(rpgsm, "classify_open_comments", _classify)
    monkeypatch.setattr(rpgsm, "resolve_safety", _safety)
    monkeypatch.setattr(rpgsm, "generate_cartoon_plan", _gen_plan)
    monkeypatch.setattr(rpgsm, "generate_learn_more_script", _gen_script)


def _patch_kie(monkeypatch) -> dict:
    """Patch kie in BOTH namespaces: the builder (pc) generates shots; the
    processor (rpgsm) generates the AI-slot base images."""
    cap = {"t2i": 0, "i2i": 0, "seedance": 0, "proc_i2i": 0, "proc_i2i_srcs": []}

    async def _t2i(_kie, _prompt, _aspect, resolution="1K", **_):
        cap["t2i"] += 1
        return f"https://kie.test/t2i-{cap['t2i']}.png", 0.04

    async def _i2i(_kie, _src, _prompt, _aspect, resolution="1K", **_):
        cap["i2i"] += 1
        return f"https://kie.test/i2i-{cap['i2i']}.png", 0.04

    async def _seed(_kie, _img, _motion, _aspect, duration=4, resolution="720p", **_):
        cap["seedance"] += 1
        return f"https://kie.test/clip-{cap['seedance']}.mp4", 0.07

    async def _proc_i2i(_kie, src, _prompt, _aspect, resolution="1K", **_):
        cap["proc_i2i"] += 1
        cap["proc_i2i_srcs"].append(src)
        return f"https://kie.test/ai-base-{cap['proc_i2i']}.png", 0.04

    monkeypatch.setattr(pc, "nano_banana_2_text_to_image", _t2i)
    monkeypatch.setattr(pc, "nano_banana_2_image_to_image", _i2i)
    monkeypatch.setattr(pc, "seedance_image_to_video", _seed)
    monkeypatch.setattr(rpgsm, "nano_banana_2_image_to_image", _proc_i2i)
    return cap


def _clients(tts_seconds: float = 2.0):
    return PipelineClients(
        openai=SimpleNamespace(),                # type: ignore[arg-type]
        kie=SimpleNamespace(),                   # type: ignore[arg-type]
        tts=_FakeTTS(tts_seconds),               # type: ignore[arg-type]
        rendi=_FakeRendi(),                      # type: ignore[arg-type]
        storage=_FakeStorageClient(),            # type: ignore[arg-type]
        article=_FakeArticleFetcher(),           # type: ignore[arg-type]
        zapcap=None,
    )


def _row(*, num_videos=2, aspects=None, m1="https://manual.test/1.png",
         m2="https://manual.test/2.png", vo=True) -> GoogleSimpleMotionRow:
    return GoogleSimpleMotionRow(
        row_num=2, country="DE", vertical="cars",
        article_url="https://example.com/article",
        manual_image_1=m1, manual_image_2=m2,
        num_videos=num_videos, voice_over=vo, zapcap=False,
        aspect_ratios=aspects or ["9:16", "1:1", "16:9", "4:5"],
        script_pattern="", open_comments="",
        cta_enabled=False, cta_text="",
    )


def _register_downloads() -> None:
    respx.get(url__regex=r"https://r\.dev/.+\.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00fake-mp4")
    )
    respx.get(url__regex=r"https://manual\.test/.+").mock(
        return_value=httpx.Response(200, content=b"\x89PNG\x00manual")
    )
    respx.get(url__regex=r"https://kie\.test/.+").mock(
        return_value=httpx.Response(200, content=b"\x89PNG\x00ai")
    )


# ── Round-trip + routing ─────────────────────────────────────────────────────


def test_row_payload_round_trip() -> None:
    row = _row(num_videos=3)
    payload = json.loads(_row_to_payload(row, TAB_GOOGLE_SIMPLE_MOTION))
    assert payload["__tab__"] == TAB_GOOGLE_SIMPLE_MOTION
    restored = payload_to_row(payload)
    assert isinstance(restored, GoogleSimpleMotionRow)
    assert restored == row


def test_runner_routes_row() -> None:
    assert _tab_for_row(_row()) == "google_simple_motion"


async def test_dispatch_routes_to_processor(monkeypatch) -> None:
    called: dict = {}

    async def _fake(row, clients, *, job_id=None):
        called["hit"] = True
        return SimpleNamespace(row_num=row.row_num)

    monkeypatch.setattr(
        "bulkvid.orchestrator.runner.process_google_simple_motion_row", _fake
    )
    await _dispatch_to_processor(_row(), SimpleNamespace(), "job")  # type: ignore[arg-type]
    assert called.get("hit") is True


# ── Processor: multi-video, shared VO, slot mapping ──────────────────────────


@respx.mock
async def test_two_videos_share_one_voiceover(monkeypatch) -> None:
    _register_downloads()
    _patch_kie(monkeypatch)
    clients = _clients()

    result = await process_google_simple_motion_row(
        _row(num_videos=2, aspects=["9:16", "1:1", "", ""]), clients, job_id="j"
    )

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 2 and all(result.video_urls)
    # Shared voiceover: script generated once → exactly TWO TTS calls (the two
    # sentences), NOT re-synthesized per video.
    assert clients.tts.calls == 2
    # Two stitches, one per size, each floored to 11s and at its own aspect.
    assert len(clients.rendi.concat_calls) == 2
    assert {c["aspect"] for c in clients.rendi.concat_calls} == {"9:16", "1:1"}
    assert all(c["total"] == pytest.approx(11.0, abs=0.01)
               for c in clients.rendi.concat_calls)


@respx.mock
async def test_four_videos_ai_slots_use_manual_as_reference(monkeypatch) -> None:
    _register_downloads()
    cap = _patch_kie(monkeypatch)
    clients = _clients()

    result = await process_google_simple_motion_row(
        _row(num_videos=4, m1="https://manual.test/1.png",
             m2="https://manual.test/2.png"),
        clients, job_id="j",
    )

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 4 and all(result.video_urls)
    # Slots 3 & 4 generated an AI base via image-to-image, using each manual
    # image as the reference.
    assert cap["proc_i2i"] == 2
    assert set(cap["proc_i2i_srcs"]) == {
        "https://manual.test/1.png", "https://manual.test/2.png"
    }
    # Still ONE shared voiceover across all four.
    assert clients.tts.calls == 2


@respx.mock
async def test_voice_over_off_makes_silent_videos(monkeypatch) -> None:
    _register_downloads()
    _patch_kie(monkeypatch)
    clients = _clients()

    result = await process_google_simple_motion_row(
        _row(num_videos=2, vo=False), clients, job_id="j"
    )

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 2 and all(result.video_urls)
    assert clients.tts.calls == 0                       # no script, no TTS
    assert all(c["audio"] is None for c in clients.rendi.concat_calls)
    # Silent videos still floored at 11s.
    assert all(c["total"] == pytest.approx(11.0, abs=0.01)
               for c in clients.rendi.concat_calls)


@respx.mock
async def test_short_vo_is_single_shot_no_ai_continuation(monkeypatch) -> None:
    """The common case (~11s) is ONE shot — a pure animation of the source image,
    no AI continuation. One video with manual image 1 → 1 Seedance clip, and the
    builder generates nothing (no chained shot 2)."""
    _register_downloads()
    cap = _patch_kie(monkeypatch)
    clients = _clients(tts_seconds=2.0)   # 2 + 3 gap + 2 = 7s → 11s floor, 1 shot

    result = await process_google_simple_motion_row(
        _row(num_videos=1), clients, job_id="j"
    )
    assert result.status == STATUS_SUCCESS
    assert result.metadata["num_shots"] == 1
    assert cap["seedance"] == 1        # one clip for the one video
    assert cap["i2i"] == 0 and cap["t2i"] == 0   # builder generated no shot 2


@respx.mock
async def test_long_vo_adds_chained_second_shot(monkeypatch) -> None:
    """A long voiceover pushes the video past a single Seedance clip's reach, so a
    SECOND image-to-image continuation shot is added (never truncating the words).
    One video → 2 Seedance clips + one builder image-to-image (the chained shot)."""
    _register_downloads()
    cap = _patch_kie(monkeypatch)
    clients = _clients(tts_seconds=7.0)   # 7 + 3 gap + 7 = 17s → 2 shots

    result = await process_google_simple_motion_row(
        _row(num_videos=1), clients, job_id="j"
    )
    assert result.status == STATUS_SUCCESS
    assert result.metadata["num_shots"] == 2
    assert cap["seedance"] == 2        # two clips for the one video
    assert cap["i2i"] == 1             # builder chained shot 2 on shot 1
    # Length follows the (long) audio, never truncated to a cap.
    assert clients.rendi.concat_calls[0]["total"] == pytest.approx(17.5, abs=0.01)


@respx.mock
async def test_num_videos_clamped_and_blank_aspect_defaults(monkeypatch) -> None:
    _register_downloads()
    _patch_kie(monkeypatch)
    clients = _clients()

    # num_videos=1, and a blank Change Size → default 9:16.
    result = await process_google_simple_motion_row(
        _row(num_videos=1, aspects=["", "", "", ""]), clients, job_id="j"
    )
    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 1
    assert clients.rendi.concat_calls[0]["aspect"] == "9:16"
