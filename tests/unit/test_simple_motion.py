"""Unit + integration tests for the simple-motion tab.

The simple-motion tab is a sibling of cartoon: ONE 8s video per row (two 4s
shots), SUPER-REALISTIC images, and operator-pasted images in columns D/E.

Covers:
  - The manual-image resolution matrix (the core new behavior): blank → generate
    realistic; filled → animate as-is; shot 2 chains on shot 1.
  - REALISTIC_STYLE reaches the image model; manual shots get the universal
    push-in motion while generated shots use the planner's motion.
  - Payload round-trip for ``SimpleMotionRow`` + tab routing through the runner.
  - REGRESSION GUARD: the cartoon ``image_prompt_for_shot`` default is byte-
    identical (the realistic style is opt-in via the new ``style`` arg).
  - The realistic planner prompt + timeout setting are registered.
"""

from __future__ import annotations

import io
import json
import wave
from types import SimpleNamespace

import httpx
import pytest
import respx

import bulkvid.orchestrator.row_processor_simple_motion as rpsm
from bulkvid.adapters.gemini_tts import TTSResult
from bulkvid.adapters.rendi import RendiOutput
from bulkvid.adapters.storage import UploadResult
from bulkvid.models.row import STATUS_SUCCESS, SimpleMotionRow
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.queue import (
    TAB_SIMPLE_MOTION,
    _row_to_payload,
    payload_to_row,
)
from bulkvid.orchestrator.row_processor_simple_motion import (
    MANUAL_IMAGE_MOTION,
    process_simple_motion_row,
)
from bulkvid.orchestrator.runner import _dispatch_to_processor, _tab_for_row
from bulkvid.orchestrator.runtime_settings import (
    SETTING_ROW_TIMEOUT_SIMPLE_MOTION,
    SETTING_SIMPLE_MOTION_PLANNER_PROMPT,
    SIMPLE_MOTION_PLANNER_PROMPT_DEFAULT,
    lookup,
)
from bulkvid.pipeline.cartoon_prompt import (
    CARTOON_STYLE,
    CONSISTENCY_CLAUSE,
    NO_BRANDING,
    REALISTIC_STYLE,
    CartoonIdea,
    CartoonPlan,
    CartoonShot,
    image_prompt_for_shot,
)
from bulkvid.pipeline.language import LanguageResult

PLANNER_MOTION = "planner push-in"


# ── Fakes (mirror test_row_processor_cartoon) ────────────────────────────────


class _FakeArticleFetcher:
    async def fetch(self, url: str):
        from bulkvid.adapters.article_fetch import ArticleResult

        return ArticleResult(
            url=url, content="Realistic product story.", source="scrapingbee",
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

    def manual_reuploads(self) -> int:
        return sum(1 for k, _ in self.calls if "simple_motion_images" in k)


class _FakeTTS:
    def __init__(self, duration: float = 6.0) -> None:
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
            {"clips": list(clip_urls), "per_clip": per_clip_seconds,
             "total_video_seconds": total_video_seconds}
        )
        return RendiOutput(
            url=f"https://r.dev/{output_filename}", cost_usd=0.01,
            command_id=f"cmd-{output_filename}",
        )

    async def cleanup_commands(self, command_ids) -> None:
        return None


# ── Stub collaborators ───────────────────────────────────────────────────────


def _plan(num_ideas: int = 1, num_shots: int = 2) -> CartoonPlan:
    ideas = [
        CartoonIdea(
            voiceover="A realistic line about the product.",
            style_direction="Warm.",
            shots=[
                CartoonShot(scene=f"Scene {i+1}.{s+1}", motion=PLANNER_MOTION)
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
        return _plan(kw.get("num_ideas", 1), kw.get("num_shots", 2))

    monkeypatch.setattr(rpsm, "detect_language", _detect)
    monkeypatch.setattr(rpsm, "classify_open_comments", _classify)
    monkeypatch.setattr(rpsm, "generate_cartoon_plan", _gen_plan)
    return captured


def _patch_kie(monkeypatch) -> dict:
    """Patch the kie wrappers with counters + prompt/motion capture."""
    cap: dict = {
        "t2i": 0, "i2i": 0, "seedance": 0,
        "t2i_prompts": [], "i2i_prompts": [], "i2i_srcs": [],
        "seedance_motions": [], "seedance_imgs": [],
    }

    async def _t2i(_kie, prompt, _aspect, resolution="1K", **_):
        cap["t2i"] += 1
        cap["t2i_prompts"].append(prompt)
        return f"https://kie.test/img-t2i-{cap['t2i']}.png", 0.04

    async def _i2i(_kie, src, prompt, _aspect, resolution="1K", **_):
        cap["i2i"] += 1
        cap["i2i_prompts"].append(prompt)
        cap["i2i_srcs"].append(src)
        return f"https://kie.test/img-i2i-{cap['i2i']}.png", 0.04

    async def _seedance(_kie, img, motion, _aspect, duration=4, resolution="720p", **_):
        cap["seedance"] += 1
        cap["seedance_motions"].append(motion)
        cap["seedance_imgs"].append(img)
        return f"https://kie.test/clip-{cap['seedance']}.mp4", 0.07

    monkeypatch.setattr(rpsm, "nano_banana_2_text_to_image", _t2i)
    monkeypatch.setattr(rpsm, "nano_banana_2_image_to_image", _i2i)
    monkeypatch.setattr(rpsm, "seedance_image_to_video", _seedance)
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


def _row(*, manual1: str = "", manual2: str = "", vo: bool = True) -> SimpleMotionRow:
    return SimpleMotionRow(
        row_num=2, country="MX", vertical="appliances",
        article_url="https://example.com/article",
        manual_image_1=manual1, manual_image_2=manual2,
        voice_over=vo, zapcap=False, aspect_ratio="9:16",
        script_pattern="", open_comments="",
    )


def _register_downloads() -> None:
    # Final stitched video + any pasted manual image fetch.
    respx.get(url__regex=r"https://r\.dev/.+\.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00fake-mp4")
    )
    respx.get(url__regex=r"https://manual\.test/.+").mock(
        return_value=httpx.Response(200, content=b"\x89PNG\x00manual")
    )


# ── Manual-image resolution matrix (the core new behavior) ───────────────────


@respx.mock
async def test_matrix_both_blank(monkeypatch) -> None:
    _register_downloads()
    cap = _patch_kie(monkeypatch)
    clients = _build_clients()

    result = await process_simple_motion_row(_row(), clients, job_id="j")

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 1                 # ONE video per row
    assert cap["t2i"] == 1 and cap["i2i"] == 1         # shot1 t2i, shot2 chained
    assert clients.storage.manual_reuploads() == 0     # nothing pasted
    assert cap["seedance"] == 2
    # Realistic style reaches the image model; brand-safety preserved.
    assert cap["t2i_prompts"][0].startswith(REALISTIC_STYLE)
    assert NO_BRANDING in cap["t2i_prompts"][0]
    assert CARTOON_STYLE not in cap["t2i_prompts"][0]
    # shot 2 chains on shot 1's generated image + carries the consistency clause.
    assert cap["i2i_srcs"][0] == "https://kie.test/img-t2i-1.png"
    assert CONSISTENCY_CLAUSE in cap["i2i_prompts"][0]
    # both shots animated with the planner's scene-matched motion.
    assert cap["seedance_motions"] == [PLANNER_MOTION, PLANNER_MOTION]


@respx.mock
async def test_matrix_d_set_e_blank(monkeypatch) -> None:
    _register_downloads()
    cap = _patch_kie(monkeypatch)
    clients = _build_clients()

    result = await process_simple_motion_row(
        _row(manual1="https://manual.test/d.png"), clients, job_id="j"
    )

    assert result.status == STATUS_SUCCESS
    assert cap["t2i"] == 0                              # shot1 is the pasted image
    assert cap["i2i"] == 1                              # shot2 generated, chained
    assert clients.storage.manual_reuploads() == 1     # D downloaded + re-uploaded
    # shot 2 chains on the RE-UPLOADED manual image (stable URL), not the raw paste.
    assert cap["i2i_srcs"][0].startswith("https://storage.test/")
    assert "simple_motion_images" in cap["i2i_srcs"][0]
    # motion: shot1 manual → universal push-in; shot2 generated → planner motion.
    assert cap["seedance_motions"] == [MANUAL_IMAGE_MOTION, PLANNER_MOTION]


@respx.mock
async def test_matrix_d_blank_e_set(monkeypatch) -> None:
    _register_downloads()
    cap = _patch_kie(monkeypatch)
    clients = _build_clients()

    result = await process_simple_motion_row(
        _row(manual2="https://manual.test/e.png"), clients, job_id="j"
    )

    assert result.status == STATUS_SUCCESS
    assert cap["t2i"] == 1                              # shot1 generated realistic
    assert cap["i2i"] == 0                              # shot2 is the pasted image
    assert clients.storage.manual_reuploads() == 1     # E downloaded + re-uploaded
    assert cap["seedance_motions"] == [PLANNER_MOTION, MANUAL_IMAGE_MOTION]


@respx.mock
async def test_matrix_both_set_no_image_gen(monkeypatch) -> None:
    _register_downloads()
    cap = _patch_kie(monkeypatch)
    clients = _build_clients()

    result = await process_simple_motion_row(
        _row(manual1="https://manual.test/d.png", manual2="https://manual.test/e.png"),
        clients, job_id="j",
    )

    assert result.status == STATUS_SUCCESS
    assert cap["t2i"] == 0 and cap["i2i"] == 0          # zero image-gen cost
    assert clients.storage.manual_reuploads() == 2      # both pasted, both re-uploaded
    assert cap["seedance"] == 2
    assert cap["seedance_motions"] == [MANUAL_IMAGE_MOTION, MANUAL_IMAGE_MOTION]


@respx.mock
async def test_planner_called_with_one_idea_and_realistic_prompt(
    monkeypatch, _stub_pipeline
) -> None:
    _register_downloads()
    _patch_kie(monkeypatch)
    clients = _build_clients()

    await process_simple_motion_row(_row(), clients, job_id="j")

    assert _stub_pipeline["num_ideas"] == 1
    assert _stub_pipeline["num_shots"] == 2
    assert _stub_pipeline["planner_prompt_key"] == SETTING_SIMPLE_MOTION_PLANNER_PROMPT


@respx.mock
async def test_voice_over_off_skips_tts_still_one_video(monkeypatch) -> None:
    _register_downloads()
    _patch_kie(monkeypatch)
    clients = _build_clients()

    result = await process_simple_motion_row(_row(vo=False), clients, job_id="j")

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 1
    assert clients.tts.calls == 0


# ── image_prompt_for_shot: realistic style + cartoon regression guard ────────


def test_image_prompt_realistic_style_opt_in() -> None:
    p = image_prompt_for_shot("a kitchen", is_chained=False, style=REALISTIC_STYLE)
    assert p.startswith(REALISTIC_STYLE)
    assert "a kitchen" in p
    assert NO_BRANDING in p
    assert CONSISTENCY_CLAUSE not in p          # not chained


def test_image_prompt_realistic_chained_has_consistency() -> None:
    p = image_prompt_for_shot("a kitchen", is_chained=True, style=REALISTIC_STYLE)
    assert p.startswith(REALISTIC_STYLE)
    assert CONSISTENCY_CLAUSE in p


def test_image_prompt_cartoon_default_unchanged() -> None:
    # REGRESSION GUARD: the cartoon/yt-cartoon callers pass no style, so the
    # default MUST stay byte-identical to the pre-simple-motion behavior.
    assert image_prompt_for_shot("scn", is_chained=False) == (
        f"{CARTOON_STYLE} scn {NO_BRANDING}"
    )
    assert image_prompt_for_shot("scn", is_chained=True) == (
        f"{CARTOON_STYLE} scn {NO_BRANDING} {CONSISTENCY_CLAUSE}"
    )


# ── Payload round-trip + routing ─────────────────────────────────────────────


def test_simple_motion_row_payload_round_trip() -> None:
    row = SimpleMotionRow(
        row_num=4, country="DE", vertical="cars",
        article_url="https://example.com/a",
        manual_image_1="https://m.test/1.png", manual_image_2="",
        voice_over=True, zapcap=True, aspect_ratio="9:16",
        script_pattern="How To", open_comments="keep it real",
        cta_enabled=True, cta_text="Read More",
    )
    payload = json.loads(_row_to_payload(row, TAB_SIMPLE_MOTION))
    assert payload["__tab__"] == TAB_SIMPLE_MOTION
    restored = payload_to_row(payload)
    assert isinstance(restored, SimpleMotionRow)
    assert restored == row


def test_runner_routes_simple_motion_row() -> None:
    row = _row()
    assert _tab_for_row(row) == "simple_motion"


async def test_dispatch_routes_to_simple_motion_processor(monkeypatch) -> None:
    called: dict = {}

    async def _fake(row, clients, *, job_id=None):
        called["hit"] = True
        return SimpleNamespace(row_num=row.row_num)

    monkeypatch.setattr(
        "bulkvid.orchestrator.runner.process_simple_motion_row", _fake
    )
    await _dispatch_to_processor(_row(), SimpleNamespace(), "job")  # type: ignore[arg-type]
    assert called.get("hit") is True


# ── Settings registry ────────────────────────────────────────────────────────


def _patch_pinned_kie(monkeypatch) -> dict:
    """Patch kie in the pinned BUILDER's namespace (separate module)."""
    import bulkvid.orchestrator.pinned_cartoon as pc

    counters = {"t2i": 0, "i2i": 0, "seedance": 0}

    async def _t2i(_kie, _p, _a, resolution="1K", **_):
        counters["t2i"] += 1
        return f"https://kie.test/t2i-{counters['t2i']}.png", 0.04

    async def _i2i(_kie, _s, _p, _a, resolution="1K", **_):
        counters["i2i"] += 1
        return f"https://kie.test/i2i-{counters['i2i']}.png", 0.04

    async def _seed(_kie, _img, _m, _a, duration=4, resolution="720p", **_):
        counters["seedance"] += 1
        return f"https://kie.test/clip-{counters['seedance']}.mp4", 0.07

    monkeypatch.setattr(pc, "nano_banana_2_text_to_image", _t2i)
    monkeypatch.setattr(pc, "nano_banana_2_image_to_image", _i2i)
    monkeypatch.setattr(pc, "seedance_image_to_video", _seed)
    return counters


@respx.mock
async def test_simple_motion_pinned_uses_manual_images_one_video(monkeypatch) -> None:
    """A pinned OVERRIDE on simple-motion speaks the exact script over the
    operator's OWN two images (fixed shots, no scene generation), producing one
    video flagged as an override."""
    from bulkvid.pipeline.open_comments import OpenCommentsAnalysis, OpenCommentsMode

    pinned = "Speak these exact words over my two photos, please."

    async def _classify_override(_client, _text):
        return OpenCommentsAnalysis(
            mode=OpenCommentsMode.OVERRIDE, raw_text=_text, override_script=pinned
        )

    monkeypatch.setattr(rpsm, "classify_open_comments", _classify_override)
    counters = _patch_pinned_kie(monkeypatch)
    respx.get(url__regex=r"https://r\.dev/.+\.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00mp4")
    )
    respx.get(url__regex=r"https://img\.test/.+").mock(
        return_value=httpx.Response(200, content=b"\x00png")
    )
    clients = _build_clients()

    result = await process_simple_motion_row(
        _row(manual1="https://img.test/a.png", manual2="https://img.test/b.png"),
        clients, job_id="jp",
    )

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 1
    assert result.metadata["script_used_override"] is True
    assert result.metadata["pinned_num_shots"] == 2          # the operator's two shots
    # Operator images used as-is — no scene generation at all.
    assert counters["t2i"] == 0 and counters["i2i"] == 0
    # The exact script was spoken once (no shorten path on the pinned builder).
    assert clients.tts.calls == 1
    # Manual images re-uploaded under the pinned key.
    assert any("pinned_images" in k for k, _ in clients.storage.calls)


def test_simple_motion_settings_registered() -> None:
    prompt = lookup(SETTING_SIMPLE_MOTION_PLANNER_PROMPT)
    timeout = lookup(SETTING_ROW_TIMEOUT_SIMPLE_MOTION)
    assert prompt is not None and prompt.multiline is True
    assert timeout is not None
    # The realistic prompt drives photographic scenes (so it doesn't fight
    # REALISTIC_STYLE) — it must speak of realism, not cartoons-as-the-goal.
    low = SIMPLE_MOTION_PLANNER_PROMPT_DEFAULT.lower()
    assert "realistic" in low
    assert "photographic" in low
