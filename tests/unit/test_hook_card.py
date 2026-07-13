"""Unit tests for the Hook_Card tab.

Hook_Card turns an article (or pasted images) into a 9:16 slideshow with a fixed
lower-third hook box + background music. Covers:

  - Payload round-trip for ``HookCardRow`` + runner tab routing.
  - The transparent hook-overlay renderer (valid PNG, empty-text transparency,
    multi-script hook does not crash).
  - The two new Rendi command builders (Ken Burns zoompan; overlay + music).
  - Copy generation: skip-with-no-call when nothing is needed, char cap + scene
    count enforcement, graceful JSON fallback.
  - Processor paths: AI-generate, manual images (no LLM/image gen), no bundled
    track (silent overlay), and fail-soft on a Rendi error.
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

import bulkvid.orchestrator.row_processor_hook_card as rphc
from bulkvid.adapters.rendi import (
    RendiOutput,
    render_ken_burns_command,
    render_overlay_and_music_command,
)
from bulkvid.adapters.storage import UploadResult
from bulkvid.models.row import (
    STATUS_SUCCESS,
    STATUS_VIDEO_ASSEMBLY_FAILED,
    HookCardRow,
    RowResult,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.queue import (
    TAB_HOOK_CARD,
    _row_to_payload,
    payload_to_row,
)
from bulkvid.orchestrator.row_processor_hook_card import (
    HC_TOTAL_SECONDS,
    process_hook_card_row,
)
from bulkvid.orchestrator.runner import _TAB_HOOK_CARD, _tab_for_row
from bulkvid.pipeline.card_renderer import render_hook_overlay_bytes
from bulkvid.pipeline.cartoon_prompt import NO_BRANDING, REALISTIC_STYLE
from bulkvid.pipeline.hook_card_copy import (
    HOOK_MAX_CHARS,
    HookCardCopy,
    generate_hook_card_copy,
)
from bulkvid.pipeline.language import LanguageResult
from bulkvid.pipeline.safety import SAFE, SafetyContext


def _row(**overrides) -> HookCardRow:
    base = dict(
        row_num=2,
        country="FR",
        vertical="Abandoned Houses",
        article_url="https://example.com/a",
        num_images=4,
        text="",
        manual_image_urls=[],
        aspect_ratio="9:16",
        open_comments="",
    )
    base.update(overrides)
    return HookCardRow(**base)


# ── Payload round-trip + routing ────────────────────────────────────────────


def test_payload_round_trip_hook_card() -> None:
    row = _row(text="Buy abandoned houses", manual_image_urls=["https://i/1.png"])
    payload = _row_to_payload(row, TAB_HOOK_CARD)
    assert '"__tab__": "hook_card"' in payload
    # Deserialize exactly as the worker does: JSON payload -> dict -> the LIVE
    # ``payload_to_row`` (not a dead look-alike — see the motion_ads round-trip
    # test for why testing the real function matters).
    restored = payload_to_row(json.loads(payload))
    assert isinstance(restored, HookCardRow)
    assert restored == row


def test_runner_routes_hook_card_row() -> None:
    assert _tab_for_row(_row()) == _TAB_HOOK_CARD
    assert _TAB_HOOK_CARD == "hook_card"


# ── Overlay renderer ────────────────────────────────────────────────────────


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def test_hook_overlay_is_valid_rgba_png_at_frame_size() -> None:
    data = render_hook_overlay_bytes(
        text="Acheter des maisons abandonnées en France en 2026",
        width=1080, height=1920,
    )
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    img = _open(data)
    assert img.size == (1080, 1920)
    assert img.mode == "RGBA"
    # The box is drawn -> at least some fully-opaque pixels exist.
    assert img.getchannel("A").getextrema()[1] == 255


def test_hook_overlay_empty_text_is_fully_transparent() -> None:
    img = _open(render_hook_overlay_bytes(text="", width=1080, height=1920))
    assert img.size == (1080, 1920)
    # No box, no text -> every pixel transparent.
    assert img.getchannel("A").getextrema() == (0, 0)


def test_hook_overlay_multiscript_hook_does_not_crash() -> None:
    # Hebrew routes to a bundled non-Inter font via the shared script router.
    data = render_hook_overlay_bytes(
        text="קנו בתים נטושים בצרפת", width=1080, height=1920
    )
    assert _open(data).getchannel("A").getextrema()[1] == 255


# ── Rendi command builders ──────────────────────────────────────────────────


def test_ken_burns_command_zoom_in() -> None:
    cmd = render_ken_burns_command(1080, 1920, 2.0, zoom_in=True)
    assert "zoompan" in cmd
    assert "scale=3240:5760" in cmd          # 3x supersample of 1080x1920
    assert "s=1080x1920" in cmd
    assert "1.0+0.12*on/" in cmd             # linear push-in
    assert "-frames:v 60" in cmd             # 2.0s * 30fps
    assert "-an" in cmd                       # silent clip


def test_ken_burns_command_zoom_out() -> None:
    cmd = render_ken_burns_command(1080, 1920, 1.6, zoom_in=False)
    assert "1.12-0.12*on/" in cmd            # linear pull-out
    assert "-frames:v 48" in cmd             # 1.6s * 30fps


def test_overlay_and_music_command_maps_music_as_audio() -> None:
    cmd = render_overlay_and_music_command()
    assert "overlay=0:0" in cmd
    assert "-map 1:a" in cmd                  # in_2 (music) is the audio track
    assert "-shortest" in cmd                 # trim music to the slideshow


# ── Copy generation ─────────────────────────────────────────────────────────


class _FakeOpenAI:
    def __init__(self, text: str, cost: float = 0.0012) -> None:
        self._text = text
        self._cost = cost
        self.calls = 0

    async def chat(self, **_kwargs) -> SimpleNamespace:
        self.calls += 1
        return SimpleNamespace(text=self._text, cost_usd=self._cost)


async def test_copy_skips_call_when_nothing_needed() -> None:
    client = _FakeOpenAI("{}")
    copy = await generate_hook_card_copy(
        client, article_body="body", language="fr", country="FR",
        vertical="Homes", open_comments="", want_hook=False, want_scenes=0,
    )
    assert client.calls == 0        # no LLM call at all
    assert copy.hook == ""
    assert copy.scenes == []
    assert copy.cost_usd == 0.0


async def test_copy_enforces_cap_and_scene_count() -> None:
    client = _FakeOpenAI(
        json.dumps({"hook": "word " * 60, "scenes": ["only one scene"]})
    )
    copy = await generate_hook_card_copy(
        client, article_body="body", language="fr", country="FR",
        vertical="Homes", open_comments="", want_hook=True, want_scenes=3,
    )
    assert 0 < len(copy.hook) <= HOOK_MAX_CHARS
    # Model returned 1 scene but 3 were requested -> padded from the fallback.
    assert len(copy.scenes) == 3


async def test_copy_falls_back_on_bad_json() -> None:
    client = _FakeOpenAI("not json at all")
    copy = await generate_hook_card_copy(
        client, article_body="body", language="en", country="US",
        vertical="Solar Panels", open_comments="", want_hook=True, want_scenes=2,
    )
    assert 0 < len(copy.hook) <= HOOK_MAX_CHARS
    assert len(copy.scenes) == 2


# ── Processor paths ─────────────────────────────────────────────────────────


class _FakeArticle:
    async def fetch(self, url: str) -> SimpleNamespace:
        return SimpleNamespace(
            content="A story about abandoned French houses.",
            source="scrapingbee", char_count=40, cost_usd=0.008, url=url,
        )


class _FakeStorage:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def upload_bytes(
        self, data: bytes, key: str, content_type: str = "application/octet-stream"
    ) -> UploadResult:
        self.keys.append(key)
        return UploadResult(
            url=f"https://storage.test/{key}", backend="gcs",
            bytes_written=len(data), cost_usd=0.0001,
        )


class _FakeRendi:
    """Records calls; each helper returns a distinct RendiOutput."""

    def __init__(self, *, ken_burns_raises: bool = False) -> None:
        self.calls: list[tuple] = []
        self._ken_burns_raises = ken_burns_raises
        self._n = 0

    async def ken_burns_clip(
        self, image_url, output_filename="out.mp4", *,
        aspect_ratio="9:16", seconds, zoom_in=True, **_kw,
    ) -> RendiOutput:
        if self._ken_burns_raises:
            raise RuntimeError("rendi boom")
        self._n += 1
        self.calls.append(("ken_burns", image_url, round(seconds, 3), zoom_in))
        return RendiOutput(
            url=f"https://rendi/{output_filename}", cost_usd=0.01,
            command_id=f"kb-{self._n}",
        )

    async def concat_clips_with_audio(
        self, clip_urls, audio_url, per_clip_seconds,
        output_filename="out.mp4", *, aspect_ratio="9:16", **_kw,
    ) -> RendiOutput:
        self.calls.append(("concat", tuple(clip_urls), audio_url))
        return RendiOutput(
            url="https://rendi/slideshow.mp4", cost_usd=0.01, command_id="concat",
        )

    async def overlay_and_add_music(
        self, video_url, overlay_url, music_url, output_filename="out.mp4", **_kw,
    ) -> RendiOutput:
        self.calls.append(("overlay_music", video_url, overlay_url, music_url))
        return RendiOutput(
            url="https://rendi/final.mp4", cost_usd=0.01, command_id="final",
        )

    async def overlay_image_on_video(
        self, video_url, overlay_url, output_filename="out.mp4", **_kw,
    ) -> RendiOutput:
        self.calls.append(("overlay_silent", video_url, overlay_url))
        return RendiOutput(
            url="https://rendi/final_silent.mp4", cost_usd=0.01,
            command_id="final-silent",
        )

    async def cleanup_commands(self, ids) -> None:
        self.calls.append(("cleanup", tuple(ids)))

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


def _clients(storage: _FakeStorage, rendi: _FakeRendi) -> PipelineClients:
    dummy = SimpleNamespace()
    return PipelineClients(
        openai=dummy, kie=dummy, tts=dummy, rendi=rendi,
        storage=storage, article=_FakeArticle(), settings_store=None,
    )


def _patch_ai(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {"image_prompts": []}

    async def _fake_detect(_client, _body, **_kw) -> LanguageResult:
        return LanguageResult(language="fr", confidence=1.0, cost_usd=0.0, cached=False)

    async def _fake_safety(_store, _vertical, _row_num=0) -> SafetyContext:
        return SAFE

    async def _fake_copy(_client, *, want_hook, want_scenes, **kwargs) -> HookCardCopy:
        captured["copy_kwargs"] = {"want_hook": want_hook, "want_scenes": want_scenes}
        return HookCardCopy(
            hook="Acheter des maisons abandonnées" if want_hook else "",
            scenes=[f"scene {i}" for i in range(want_scenes)],
            cost_usd=0.001,
        )

    async def _fake_t2i(_kie, prompt, aspect, resolution="2K"):
        captured["image_prompts"].append(prompt)
        captured["image_aspect"] = aspect
        return f"https://kie/img{len(captured['image_prompts'])}.png", 0.06

    async def _fake_download(_url, timeout=60.0) -> bytes:
        return b"x" * 20_000    # > _MIN_FINAL_BYTES

    monkeypatch.setattr(rphc, "detect_language", _fake_detect)
    monkeypatch.setattr(rphc, "resolve_safety", _fake_safety)
    monkeypatch.setattr(rphc, "generate_hook_card_copy", _fake_copy)
    monkeypatch.setattr(rphc, "nano_banana_2_text_to_image", _fake_t2i)
    monkeypatch.setattr(rphc, "download_image", _fake_download)
    monkeypatch.setattr(rphc, "select_track", lambda _seed: None)
    return captured


async def test_process_ai_path_generates_scenes_and_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_ai(monkeypatch)
    storage, rendi = _FakeStorage(), _FakeRendi()
    result = await process_hook_card_row(_row(num_images=4), _clients(storage, rendi), job_id="j")
    assert isinstance(result, RowResult)
    assert result.status == STATUS_SUCCESS
    assert result.video_urls[0].startswith("https://storage.test/")
    # 4 scenes -> 4 image gens -> 4 Ken Burns clips, then concat + silent overlay.
    assert len(captured["image_prompts"]) == 4
    assert captured["copy_kwargs"] == {"want_hook": True, "want_scenes": 4}
    assert rendi.kinds().count("ken_burns") == 4
    assert "concat" in rendi.kinds()
    assert "overlay_silent" in rendi.kinds()    # no bundled track -> silent
    # Each Ken Burns clip is 8s/4 = 2s and every image prompt is brand-guarded.
    assert all(REALISTIC_STYLE in p and NO_BRANDING in p for p in captured["image_prompts"])
    kb_seconds = {c[2] for c in rendi.calls if c[0] == "ken_burns"}
    assert kb_seconds == {round(HC_TOTAL_SECONDS / 4, 3)}


async def test_process_manual_images_skip_llm_and_image_gen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_ai(monkeypatch)

    async def _boom(*_a, **_k):
        raise AssertionError("must not generate images on the manual path")

    async def _boom_copy(*_a, **_k):
        raise AssertionError("must not call copy when text + images are supplied")

    monkeypatch.setattr(rphc, "nano_banana_2_text_to_image", _boom)
    monkeypatch.setattr(rphc, "generate_hook_card_copy", _boom_copy)

    storage, rendi = _FakeStorage(), _FakeRendi()
    row = _row(
        text="Acheter des maisons abandonnées",
        manual_image_urls=["https://u/1.png", "https://u/2.png", "https://u/3.png"],
    )
    result = await process_hook_card_row(row, _clients(storage, rendi), job_id="j")
    assert result.status == STATUS_SUCCESS
    # 3 pasted images -> 3 Ken Burns clips, each 8s/3.
    assert rendi.kinds().count("ken_burns") == 3
    assert captured["image_prompts"] == []


async def test_process_uses_music_when_track_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    _patch_ai(monkeypatch)
    track = tmp_path / "bed.mp3"
    track.write_bytes(b"ID3fakeaudio")
    monkeypatch.setattr(rphc, "select_track", lambda _seed: track)

    storage, rendi = _FakeStorage(), _FakeRendi()
    result = await process_hook_card_row(_row(num_images=2), _clients(storage, rendi), job_id="j")
    assert result.status == STATUS_SUCCESS
    assert "overlay_music" in rendi.kinds()
    assert "overlay_silent" not in rendi.kinds()
    assert any("hook_card_music" in k for k in storage.keys)


async def test_process_fail_soft_on_rendi_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_ai(monkeypatch)
    storage, rendi = _FakeStorage(), _FakeRendi(ken_burns_raises=True)
    result = await process_hook_card_row(_row(), _clients(storage, rendi), job_id="j")
    assert result.status == STATUS_VIDEO_ASSEMBLY_FAILED
    assert result.video_urls == []
    assert result.error    # never raises; carries the reason
