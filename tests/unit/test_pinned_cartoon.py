"""Tests for the verbatim (pinned) cartoon-family builder.

``build_pinned_cartoon_video`` is the single shared surgery point for the three
animated tabs when an operator pins an exact script (``use this script:``). The
kie wrappers are stubbed in the BUILDER's module namespace (``pinned_cartoon``)
— not a processor's — because that's where the builder resolves them. The
adapters (tts/storage/rendi/zapcap) are fakes on the clients object.

Covers:
  - variable mode (cartoon / yt-cartoon): ONE video, length driven by the audio,
    natural atempo, shot count from the measured duration, verbatim TTS
  - fixed mode (simple-motion): exactly the operator's manual images, stretched
  - no-VO degenerate case → silent video
  - ZapCap path
  - all animations fail → no video + surfaced error
  - empty shots guard
"""

from __future__ import annotations

import io
import wave
from types import SimpleNamespace

import httpx
import pytest
import respx

import bulkvid.orchestrator.pinned_cartoon as pc
from bulkvid.adapters.gemini_tts import TTSResult
from bulkvid.adapters.rendi import RendiOutput
from bulkvid.adapters.storage import UploadResult
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.pinned_cartoon import (
    PinnedShotSpec,
    build_pinned_cartoon_video,
)

# ── Fakes ────────────────────────────────────────────────────────────────────


def _wav_bytes() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24_000)
        wf.writeframes(b"\x00" * 24_000)
    return buf.getvalue()


class _FakeTTS:
    def __init__(self, duration: float = 12.0) -> None:
        self.calls = 0
        self._d = duration
        self.last_texts: list[str] = []

    async def synthesize(
        self, text: str, language: str, voice: str | None = None,
        style_prompt: str | None = None, country: str = "",
    ) -> TTSResult:
        self.calls += 1
        self.last_texts.append(text)
        return TTSResult(
            wav_bytes=_wav_bytes(), voice=voice or "Kore", language=language,
            duration_seconds=self._d, character_count=len(text), cost_usd=0.003,
        )


class _FakeStorage:
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


class _FakeRendi:
    def __init__(self) -> None:
        self.concat_calls: list[dict] = []
        self.overlay_calls: list[str] = []

    async def concat_clips_with_audio(
        self, clip_urls, audio_url, per_clip_seconds,
        output_filename="out.mp4", *, aspect_ratio="9:16",
        total_video_seconds=None, atempo=None, **_,
    ) -> RendiOutput:
        self.concat_calls.append({
            "clips": list(clip_urls), "audio": audio_url,
            "per_clip": list(per_clip_seconds), "total": total_video_seconds,
            "atempo": atempo,
        })
        return RendiOutput(
            url=f"https://r.dev/{output_filename}", cost_usd=0.01,
            command_id=f"cmd-{output_filename}",
        )

    async def overlay_image_on_video(self, *, video_url, overlay_url, output_filename):
        self.overlay_calls.append(output_filename)
        return RendiOutput(
            url=f"https://r.dev/{output_filename}", cost_usd=0.005,
            command_id=f"ov-{output_filename}",
        )

    async def cleanup_commands(self, command_ids) -> None:
        return None


class _FakeZapCap:
    async def caption_video(
        self, video_bytes, language, filename, render_options=None,
        *, video_duration_seconds,
    ):
        return f"https://zc.test/{filename}", 0.1


def _clients(*, tts_duration: float = 12.0, with_zapcap: bool = False) -> PipelineClients:
    return PipelineClients(
        openai=SimpleNamespace(),                              # type: ignore[arg-type]
        kie=SimpleNamespace(),                                 # type: ignore[arg-type]
        tts=_FakeTTS(tts_duration),                            # type: ignore[arg-type]
        rendi=_FakeRendi(),                                    # type: ignore[arg-type]
        storage=_FakeStorage(),                                # type: ignore[arg-type]
        article=SimpleNamespace(),                             # type: ignore[arg-type]
        zapcap=_FakeZapCap() if with_zapcap else None,         # type: ignore[arg-type]
    )


def _patch_kie(monkeypatch, *, seedance_fail_all: bool = False) -> dict:
    counters = {"t2i": 0, "i2i": 0, "seedance": 0, "durations": []}

    async def _t2i(_kie, _prompt, _aspect, resolution="1K", **_):
        counters["t2i"] += 1
        return f"https://kie.test/t2i-{counters['t2i']}.png", 0.04

    async def _i2i(_kie, _src, _prompt, _aspect, resolution="1K", **_):
        counters["i2i"] += 1
        return f"https://kie.test/i2i-{counters['i2i']}.png", 0.04

    async def _seed(_kie, _img, _motion, _aspect, duration=4, resolution="720p", **_):
        counters["seedance"] += 1
        counters["durations"].append(int(duration))
        if seedance_fail_all:
            raise RuntimeError("seedance boom")
        return f"https://kie.test/clip-{counters['seedance']}.mp4", 0.07

    monkeypatch.setattr(pc, "nano_banana_2_text_to_image", _t2i)
    monkeypatch.setattr(pc, "nano_banana_2_image_to_image", _i2i)
    monkeypatch.setattr(pc, "seedance_image_to_video", _seed)
    return counters


def _register_downloads() -> None:
    respx.get(url__regex=r"https://r\.dev/.+\.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00mp4")
    )
    respx.get(url__regex=r"https://zc\.test/.+\.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00cap")
    )
    respx.get(url__regex=r"https://img\.test/.+").mock(
        return_value=httpx.Response(200, content=b"\x00png")
    )


def _shots(n: int = 8) -> list[PinnedShotSpec]:
    return [PinnedShotSpec(scene=f"Scene {i + 1}", motion="gentle push-in") for i in range(n)]


# ── Variable mode (cartoon / yt-cartoon) ─────────────────────────────────────


@respx.mock
async def test_pinned_variable_one_video_audio_driven(monkeypatch) -> None:
    _patch_kie(monkeypatch)
    _register_downloads()
    clients = _clients(tts_duration=12.0)

    res = await build_pinned_cartoon_video(
        clients=clients, slug="s1",
        pinned_script="Beslagautos in Nederland worden online geveild en getoond.",
        style_direction="Upbeat.", shots=_shots(8),
        language="nl", country="NL", aspect="9:16",
        voice_over=True, fixed_shots=False,
    )

    assert res.final_url is not None
    assert res.error is None
    # Exactly ONE video (one stitch) — never the cartoon tab's two.
    assert len(clients.rendi.concat_calls) == 1
    call = clients.rendi.concat_calls[0]
    # Length follows the audio (12s + 0.5 dwell), at natural pace.
    assert call["total"] == pytest.approx(12.5, abs=0.01)
    assert call["atempo"] == pytest.approx(1.0)
    assert call["audio"] is not None
    # 12s → 2 shots from plan_pinned_shots; the 8 planner scenes are sliced to 2.
    assert res.num_shots == 2
    assert len(call["clips"]) == 2
    # The script was spoken VERBATIM — one TTS call, unchanged text, no shorten.
    assert clients.tts.calls == 1
    assert clients.tts.last_texts[0].startswith("Beslagautos")


@respx.mock
async def test_pinned_long_script_scales_shots(monkeypatch) -> None:
    _patch_kie(monkeypatch)
    _register_downloads()
    clients = _clients(tts_duration=30.0)

    res = await build_pinned_cartoon_video(
        clients=clients, slug="s1b", pinned_script="A long pinned ad read.",
        style_direction="x", shots=_shots(8),
        language="en", country="US", aspect="9:16",
        voice_over=True, fixed_shots=False,
    )
    # 30s audio → more shots for variety, length still follows the audio.
    assert res.num_shots == 6
    assert clients.rendi.concat_calls[0]["total"] == pytest.approx(30.5, abs=0.01)


# ── Fixed mode (simple-motion) ───────────────────────────────────────────────


@respx.mock
async def test_pinned_fixed_uses_manual_images(monkeypatch) -> None:
    counters = _patch_kie(monkeypatch)
    _register_downloads()
    clients = _clients(tts_duration=10.0)

    shots = [
        PinnedShotSpec(scene="ignored", motion="pan", manual_image_url="https://img.test/a.png"),
        PinnedShotSpec(scene="ignored", motion="pan", manual_image_url="https://img.test/b.png"),
    ]
    res = await build_pinned_cartoon_video(
        clients=clients, slug="s2", pinned_script="Exact words here please.",
        style_direction="Calm.", shots=shots, language="en", country="US",
        aspect="9:16", voice_over=True, fixed_shots=True,
    )

    assert res.final_url is not None
    assert res.num_shots == 2                       # kept the operator's two shots
    # Operator images used as-is — NO scene generation at all.
    assert counters["t2i"] == 0 and counters["i2i"] == 0
    call = clients.rendi.concat_calls[0]
    assert call["total"] == pytest.approx(10.5, abs=0.01)
    assert len(call["clips"]) == 2


# ── Edge / robustness ────────────────────────────────────────────────────────


@respx.mock
async def test_pinned_no_vo_makes_silent_video(monkeypatch) -> None:
    _patch_kie(monkeypatch)
    _register_downloads()
    clients = _clients()

    res = await build_pinned_cartoon_video(
        clients=clients, slug="s3", pinned_script="Will not be spoken.",
        style_direction="x", shots=_shots(4), language="en", country="US",
        aspect="9:16", voice_over=False, fixed_shots=False,
    )
    assert res.final_url is not None
    assert clients.tts.calls == 0                   # nothing synthesised
    assert clients.rendi.concat_calls[0]["audio"] is None


@respx.mock
async def test_pinned_zapcap_captions(monkeypatch) -> None:
    _patch_kie(monkeypatch)
    _register_downloads()
    clients = _clients(with_zapcap=True)

    res = await build_pinned_cartoon_video(
        clients=clients, slug="s4", pinned_script="Caption me exactly as written.",
        style_direction="x", shots=_shots(8), language="en", country="US",
        aspect="9:16", voice_over=True, fixed_shots=False, zapcap_enabled=True,
    )
    assert res.final_url is not None
    assert any("videos_captioned" in k for k, _ in clients.storage.calls)
    assert res.cost_zapcap > 0


@respx.mock
async def test_pinned_all_animations_fail_returns_error(monkeypatch) -> None:
    _patch_kie(monkeypatch, seedance_fail_all=True)
    _register_downloads()
    clients = _clients()

    res = await build_pinned_cartoon_video(
        clients=clients, slug="s5", pinned_script="x y z and here we go now.",
        style_direction="x", shots=_shots(8), language="en", country="US",
        aspect="9:16", voice_over=True, fixed_shots=False,
    )
    assert res.final_url is None
    assert res.error is not None
    assert "no Seedance clips" in res.error


async def test_pinned_empty_shots_errors() -> None:
    clients = _clients()
    res = await build_pinned_cartoon_video(
        clients=clients, slug="s6", pinned_script="anything",
        style_direction="x", shots=[], language="en", country="US",
        aspect="9:16", voice_over=True, fixed_shots=False,
    )
    assert res.final_url is None
    assert "no shots" in (res.error or "")
