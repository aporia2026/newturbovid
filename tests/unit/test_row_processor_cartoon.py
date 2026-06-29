"""Integration-style tests for the cartoon row processor.

Collaborators (kie wrappers, the planner, language/open-comments classifiers)
are stubbed in the processor's module namespace so the test is deterministic and
focuses on the processor's own orchestration + graceful-degradation logic. The
kie/planner internals have their own unit tests.

Covers:
  - Happy path -> CARTOON_NUM_IDEAS videos, STATUS_SUCCESS, tab metadata
  - Voice Over = No -> CARTOON_NUM_IDEAS videos, no TTS
  - Article fetch failure -> STATUS_ARTICLE_FETCH_FAILED
  - A failed LATER shot image is held (still all videos)
  - A failed shot animation is gap-filled (still all videos)
  - All animations fail -> STATUS_VIDEO_ASSEMBLY_FAILED
  - One idea fully fails -> the others still ship
  - ZapCap=Yes -> all captioned videos
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
from bulkvid.orchestrator.row_processor_cartoon import (
    MAX_EFFECTIVE_VO_SECONDS,
    SPEECH_ATEMPO_MIN,
    compute_atempo,
    process_cartoon_row,
)
from bulkvid.pipeline.cartoon_prompt import CartoonIdea, CartoonPlan, CartoonShot
from bulkvid.pipeline.language import LanguageResult

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
    def __init__(
        self,
        duration: float = 6.0,
        durations: list[float] | None = None,
    ) -> None:
        """``durations`` (if given) lets a test return a different VO length on
        each call — useful for the shorten-then-retry flow where the first
        synthesize overshoots and the retry must fit."""
        self.calls = 0
        self._duration = duration
        self._durations = list(durations) if durations else []
        self.last_texts: list[str] = []     # capture what was synthesised, in order

    async def synthesize(
        self, text: str, language: str, voice: str | None = None,
        style_prompt: str | None = None, country: str = "",
    ) -> TTSResult:
        self.calls += 1
        self.last_texts.append(text)
        if self._durations:
            duration = self._durations[(self.calls - 1) % len(self._durations)]
        else:
            duration = self._duration
        import wave

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24_000)
            wf.writeframes(b"\x00" * 24_000)
        return TTSResult(
            wav_bytes=buf.getvalue(), voice=voice or "Kore", language=language,
            duration_seconds=duration, character_count=len(text), cost_usd=0.003,
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
            {
                "clips": list(clip_urls),
                "audio": audio_url,
                "per_clip": per_clip_seconds,
                "out": output_filename,
                "total_video_seconds": total_video_seconds,
                "atempo": atempo,
            }
        )
        return RendiOutput(
            url=f"https://r.dev/{output_filename}", cost_usd=0.01,
            command_id=f"cmd-{output_filename}",
        )

    async def cleanup_commands(self, command_ids) -> None:
        return None


class _FakeZapCap:
    async def caption_video(
        self,
        video_bytes,
        language,
        filename,
        render_options=None,
        *,
        video_duration_seconds,
    ):
        # The real adapter charges ``video_duration_seconds * $0.10/60`` —
        # return a deterministic constant here so cost assertions stay stable
        # across cartoon-flow changes, but accept the new kwargs so the row
        # processor's call passes through (render_options added 2026-06-08
        # for the cartoon CTA path; ignored here).
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
        # Real LanguageResult (not SimpleNamespace) so reconcile_language can
        # read .confidence / replace(); "es" matches the MX test row so the
        # safety net passes it through unchanged (reconcile has its own cover
        # in test_language_reconcile.py).
        return LanguageResult(language="es", confidence=0.99, cost_usd=0.0, cached=False)

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

    durations: list[int] = []

    async def _seedance(_kie, _img, _motion, _aspect, duration=4, resolution="720p", **_):
        counters["seedance"] += 1
        durations.append(int(duration))
        if seedance_fail_all or (
            seedance_fail_on is not None and counters["seedance"] == seedance_fail_on
        ):
            raise RuntimeError("seedance boom")
        cost = 0.14 if duration == 8 else 0.07
        return f"https://kie.test/clip-{counters['seedance']}.mp4", cost

    monkeypatch.setattr(rpc, "nano_banana_2_text_to_image", _t2i)
    monkeypatch.setattr(rpc, "nano_banana_2_image_to_image", _i2i)
    monkeypatch.setattr(rpc, "seedance_image_to_video", _seedance)
    counters["durations"] = durations    # type: ignore[assignment]
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


# ── compute_atempo ──────────────────────────────────────────────────────────


def test_compute_atempo_short_vo_plays_at_natural_speed() -> None:
    # Raw 4s VO comfortably fits in 7.5s window -> no speedup needed.
    # Previously this case played at 1.3x (3.08s effective, 4.9s of dwell);
    # now it plays at 1.0x (4.0s effective, 4.0s of dwell). The remaining
    # dwell is the structural floor and is unavoidable without variable
    # video length.
    atempo, effective = compute_atempo(4.0)
    assert atempo == pytest.approx(1.0)
    assert effective == pytest.approx(4.0)


def test_compute_atempo_medium_vo_plays_at_natural_speed() -> None:
    # 6.27s raw was the v2 case from the field report — old behavior gave
    # 4.82s effective (3.18s dwell). New behavior: 6.27s effective (1.73s
    # dwell). That's the win.
    atempo, effective = compute_atempo(6.27)
    assert atempo == pytest.approx(1.0)
    assert effective == pytest.approx(6.27)


def test_compute_atempo_at_threshold_uses_natural_speed() -> None:
    # Exactly 7.5s raw — at the boundary, no speedup.
    atempo, effective = compute_atempo(MAX_EFFECTIVE_VO_SECONDS)
    assert atempo == pytest.approx(SPEECH_ATEMPO_MIN)
    assert effective == pytest.approx(MAX_EFFECTIVE_VO_SECONDS)


def test_compute_atempo_long_vo_speeds_up_just_enough() -> None:
    # 9.0s raw > 7.5s cap. atempo = 9.0 / 7.5 = 1.2 (less than 1.3 max),
    # effective lands at exactly 7.5s.
    atempo, effective = compute_atempo(9.0)
    assert atempo == pytest.approx(1.2)
    assert effective == pytest.approx(MAX_EFFECTIVE_VO_SECONDS)


def test_compute_atempo_very_long_vo_caps_at_1_3() -> None:
    # 12s raw can't fit at 1.3x either (12 / 1.3 = 9.23 > 7.5). atempo
    # caps at 1.3 and effective stays above the cap — caller sees
    # `effective > MAX_EFFECTIVE_VO_SECONDS` and triggers the shorten path.
    atempo, effective = compute_atempo(12.0)
    assert atempo == pytest.approx(1.3)
    assert effective > MAX_EFFECTIVE_VO_SECONDS
    assert effective == pytest.approx(12.0 / 1.3)


def test_compute_atempo_boundary_returns_cap_exactly_no_fp_drift() -> None:
    # Regression for job local-desktop-l6i1bf7-20260604T103814Z: raw=8.17s
    # made the previous implementation return effective=7.5000000001 (FP
    # drift), which tripped the row processor's `effective > cap` check
    # and dropped an otherwise-clean idea. compute_atempo now returns the
    # cap as a literal in this branch — exhaustive sweep across the band
    # confirms effective is never strictly greater than the cap.
    band_raws = [7.51, 7.6, 7.99, 8.0, 8.17, 8.5, 9.0, 9.5, 9.74, 9.749]
    for raw in band_raws:
        atempo, effective = compute_atempo(raw)
        assert effective == MAX_EFFECTIVE_VO_SECONDS, (
            f"raw={raw}: expected effective=={MAX_EFFECTIVE_VO_SECONDS}, "
            f"got {effective!r}"
        )
        # And the row processor's strict gate must NOT fire on the boundary.
        assert not (effective > MAX_EFFECTIVE_VO_SECONDS), (
            f"raw={raw}: FP drift would re-introduce the bug"
        )
        assert SPEECH_ATEMPO_MIN < atempo <= 1.3


def test_compute_atempo_retry_cap_rescues_borderline_drops() -> None:
    """Regression for job-1780933855-3c614650: three idea-1s dropped on
    retry-effective values of 7.578s / 8.747s / 9.793s — all rescuable
    with a slightly higher atempo. The default 1.3x cap drops them; the
    1.5x retry cap ships them at a still-natural speed."""
    # r3: 7.578s raw — needs barely any speedup.
    atempo, effective = compute_atempo(7.578, max_atempo=rpc.SPEECH_ATEMPO_RETRY_MAX)
    assert atempo == pytest.approx(7.578 / MAX_EFFECTIVE_VO_SECONDS)
    assert effective == MAX_EFFECTIVE_VO_SECONDS

    # r2: 8.747s raw — comfortably under the 1.5x ceiling.
    atempo, effective = compute_atempo(8.747, max_atempo=rpc.SPEECH_ATEMPO_RETRY_MAX)
    assert atempo == pytest.approx(8.747 / MAX_EFFECTIVE_VO_SECONDS)
    assert effective == MAX_EFFECTIVE_VO_SECONDS

    # r4: 9.793s raw — would have been dropped at 1.3x (1.306 > 1.3) but
    # fits cleanly at 1.31x under the 1.5x ceiling.
    atempo, effective = compute_atempo(9.793, max_atempo=rpc.SPEECH_ATEMPO_RETRY_MAX)
    assert atempo == pytest.approx(9.793 / MAX_EFFECTIVE_VO_SECONDS)
    assert effective == MAX_EFFECTIVE_VO_SECONDS

    # Sanity: WITHOUT the retry cap (default 1.3x), all three still get
    # dropped — proves the test would have failed pre-fix.
    for raw in (7.578, 8.747, 9.793):
        _, effective_default = compute_atempo(raw)
        if raw / MAX_EFFECTIVE_VO_SECONDS > 1.3:
            assert effective_default > MAX_EFFECTIVE_VO_SECONDS


def test_compute_atempo_retry_cap_still_drops_truly_huge_vos() -> None:
    """The retry cap rescues borderline overshoots, NOT genuinely
    oversized VOs. A 16s VO at 1.55x is still 10.32s — the caller's
    `effective > MAX_EFFECTIVE_VO_SECONDS` gate has to still fire so the
    idea is dropped instead of shipping with rushed audio."""
    atempo, effective = compute_atempo(16.0, max_atempo=rpc.SPEECH_ATEMPO_RETRY_MAX)
    assert atempo == pytest.approx(rpc.SPEECH_ATEMPO_RETRY_MAX)
    assert effective > MAX_EFFECTIVE_VO_SECONDS


def test_compute_atempo_retry_cap_rescues_long_german_vo() -> None:
    """Regression for job-1780936528-524e40fb row 5 idea 1: an 11.61s
    German VO was dropped at the previous 1.5x ceiling (effective
    7.741s > 7.5s cap) by a margin of 241ms. The 1.55x ceiling
    rescues it — effective lands at exactly the 7.5s cap, no drop."""
    atempo, effective = compute_atempo(11.61, max_atempo=rpc.SPEECH_ATEMPO_RETRY_MAX)
    # 11.61 / 7.5 = 1.548 — fits under the 1.55x ceiling.
    assert atempo == pytest.approx(11.61 / MAX_EFFECTIVE_VO_SECONDS)
    assert effective == MAX_EFFECTIVE_VO_SECONDS

    # Sanity: WITHOUT the 1.55x cap (i.e. the old 1.5x), this would
    # drop — proves the test would have failed pre-fix.
    _, effective_at_1_5 = compute_atempo(11.61, max_atempo=1.5)
    assert effective_at_1_5 > MAX_EFFECTIVE_VO_SECONDS


# ── Tests ─────────────────────────────────────────────────────────────────────


@respx.mock
async def test_cartoon_happy_path_all_videos(monkeypatch) -> None:
    _patch_kie(monkeypatch)
    _register_downloads()
    clients = _build_clients()

    result = await process_cartoon_row(_row(), clients, job_id="jobX")

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == rpc.CARTOON_NUM_IDEAS
    assert result.metadata["tab"] == "cartoon"
    assert result.metadata["videos_produced"] == rpc.CARTOON_NUM_IDEAS
    # Each idea was voiced and stitched once.
    assert clients.tts.calls == rpc.CARTOON_NUM_IDEAS
    assert len(clients.rendi.concat_calls) == rpc.CARTOON_NUM_IDEAS
    # The voiceover was wired into every stitch.
    assert all(c["audio"] is not None for c in clients.rendi.concat_calls)


@respx.mock
async def test_cartoon_video_always_8s_when_short_vo(monkeypatch) -> None:
    # Raw 3.5s VO -> effective 2.69s, well under MAX_EFFECTIVE_VO_SECONDS.
    # Hard cap: video is still exactly 8.0s, two flat 4s clips, VO ends ~5s
    # into the video with the rest as trailing silence. No clamp logic.
    counters = _patch_kie(monkeypatch)
    _register_downloads()
    clients = _build_clients()
    clients.tts = _FakeTTS(duration=3.5)    # type: ignore[assignment]

    result = await process_cartoon_row(_row(), clients, job_id="j")
    assert result.status == STATUS_SUCCESS
    call = clients.rendi.concat_calls[0]
    assert call["total_video_seconds"] == pytest.approx(8.0, abs=0.01)
    assert call["per_clip"] == [pytest.approx(4.0, abs=0.01)] * 2
    # Short VO -> atempo stays at 1.0 (natural pace). No artificial speedup.
    assert call["atempo"] == pytest.approx(1.0)
    # All Seedance calls used the 4s tier — no 8s last-shot mode anywhere.
    assert all(d == 4 for d in counters["durations"])


@respx.mock
async def test_cartoon_video_always_8s_when_normal_vo(monkeypatch) -> None:
    # Raw 7s VO -> fits at natural speed (atempo=1.0, effective 7s, dwell 1s).
    # Video still exactly 8.0s, two 4s clips. No shortening needed.
    counters = _patch_kie(monkeypatch)
    _register_downloads()
    clients = _build_clients()
    clients.tts = _FakeTTS(duration=7.0)    # type: ignore[assignment]

    result = await process_cartoon_row(_row(), clients, job_id="j")
    assert result.status == STATUS_SUCCESS
    call = clients.rendi.concat_calls[0]
    assert call["total_video_seconds"] == pytest.approx(8.0, abs=0.01)
    assert call["per_clip"] == [pytest.approx(4.0, abs=0.01)] * 2
    # 7s raw is still under the 7.5s cap -> natural speed, no speedup.
    assert call["atempo"] == pytest.approx(1.0)


@respx.mock
async def test_cartoon_long_vo_speeds_up_to_fit(monkeypatch) -> None:
    # Raw 9s VO -> can't fit at 1.0x. atempo bumps to 9/7.5 = 1.2 so the
    # effective played length lands at exactly 7.5s. Shortener NOT called.
    counters = _patch_kie(monkeypatch)
    _register_downloads()
    clients = _build_clients()
    clients.tts = _FakeTTS(duration=9.0)    # type: ignore[assignment]

    result = await process_cartoon_row(_row(), clients, job_id="j")
    assert result.status == STATUS_SUCCESS
    call = clients.rendi.concat_calls[0]
    assert call["total_video_seconds"] == pytest.approx(8.0, abs=0.01)
    # 9 / 7.5 = 1.2, capped at 1.3.
    assert call["atempo"] == pytest.approx(1.2, abs=0.01)
    # Only one TTS call — the new range covers this without invoking the
    # shortener.
    assert clients.tts.calls == 2    # both ideas, no retries
    assert all(d == 4 for d in counters["durations"])
    # Only one TTS call per idea — no retry was needed.
    assert clients.tts.calls == rpc.CARTOON_NUM_IDEAS
    assert all(d == 4 for d in counters["durations"])


@respx.mock
async def test_cartoon_vo_too_long_triggers_shorten_then_fits(monkeypatch) -> None:
    """The regression scenario that produced refs/v1.mp4 (11s, VO cut mid-word):
    first TTS overshoots, shorten_voiceover is called, second TTS fits inside
    the 8s ceiling, video ships normally at exactly 8.0s with no truncation."""
    from bulkvid.pipeline.cartoon_prompt import ShortenResult

    shorten_calls: list[tuple[str, int]] = []

    async def _shorten(_client, *, text: str, language: str, target_words: int, **_):
        shorten_calls.append((text, target_words))
        return ShortenResult(voiceover="A shorter line.", cost_usd=0.0008)

    monkeypatch.setattr(rpc, "shorten_voiceover", _shorten)

    counters = _patch_kie(monkeypatch)
    _register_downloads()
    clients = _build_clients()
    # First TTS per idea = 13s raw -> effective 10s (over cap). Retry = 5s ->
    # 3.85s effective (under cap). Every idea follows the same pattern.
    pattern = [13.0, 5.0] * rpc.CARTOON_NUM_IDEAS
    clients.tts = _FakeTTS(durations=pattern)    # type: ignore[assignment]

    result = await process_cartoon_row(_row(), clients, job_id="j")
    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == rpc.CARTOON_NUM_IDEAS
    # N ideas * (1 initial + 1 retry) TTS calls.
    assert clients.tts.calls == rpc.CARTOON_NUM_IDEAS * 2
    # Shortener was invoked once per idea.
    assert len(shorten_calls) == rpc.CARTOON_NUM_IDEAS
    # All stitched videos are still 8.0s with uniform 4s clips.
    for call in clients.rendi.concat_calls:
        assert call["total_video_seconds"] == pytest.approx(8.0, abs=0.01)
        assert call["per_clip"] == [pytest.approx(4.0, abs=0.01)] * 2
    # The TTS retry was called with the SHORTENED text.
    assert "A shorter line." in clients.tts.last_texts
    assert all(d == 4 for d in counters["durations"])


@respx.mock
async def test_cartoon_vo_too_long_after_retry_drops_idea(monkeypatch) -> None:
    """If the shortened TTS *still* overshoots the 8s ceiling, the idea is
    dropped (returns None from _build_idea). The remaining ideas ship normally
    so the row succeeds. Row only fails when ALL ideas drop."""
    from bulkvid.pipeline.cartoon_prompt import ShortenResult

    async def _shorten(_client, *, text: str, language: str, target_words: int, **_):
        return ShortenResult(voiceover="Still too verbose for this format.", cost_usd=0.0008)

    monkeypatch.setattr(rpc, "shorten_voiceover", _shorten)

    counters = _patch_kie(monkeypatch)
    _register_downloads()
    clients = _build_clients()

    # Custom TTS sequence: every idea overshoots on its first call. On retry,
    # all-but-one still overshoot → dropped; the last retry fits → ships.
    # Result: exactly 1 video, regardless of asyncio scheduling order between
    # the concurrent ideas (the sequence is consumed call-by-call).
    overshoot_initial = [13.0] * rpc.CARTOON_NUM_IDEAS
    overshoot_retries = [13.0] * (rpc.CARTOON_NUM_IDEAS - 1) + [5.0]
    overshoot_sequence = iter(overshoot_initial + overshoot_retries)
    fake_tts = _FakeTTS()

    async def _synth(text, language, voice=None, style_prompt=None, country=""):
        fake_tts.calls += 1
        fake_tts.last_texts.append(text)
        try:
            duration = next(overshoot_sequence)
        except StopIteration:
            duration = 5.0
        import wave
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(24_000)
            wf.writeframes(b"\x00" * 24_000)
        return TTSResult(
            wav_bytes=buf.getvalue(), voice=voice or "Kore", language=language,
            duration_seconds=duration, character_count=len(text), cost_usd=0.003,
        )

    fake_tts.synthesize = _synth    # type: ignore[method-assign]
    clients.tts = fake_tts          # type: ignore[assignment]

    result = await process_cartoon_row(_row(), clients, job_id="j")
    # N-1 ideas dropped (over-cap after retry), one shipped — row still succeeds.
    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 1
    assert result.metadata["videos_produced"] == 1
    assert all(d == 4 for d in counters["durations"])


@respx.mock
async def test_cartoon_vo_shortener_no_change_drops_idea(monkeypatch) -> None:
    """If shorten_voiceover returns the SAME text (its defensive fallback for
    bad JSON / empty / not-actually-shorter), there's no point re-TTSing — the
    idea is dropped immediately to spare the cost."""
    from bulkvid.pipeline.cartoon_prompt import ShortenResult

    original = "Voiceover idea 1 about cheaper cars."   # matches the test plan

    async def _shorten_returns_original(_client, *, text: str, **_):
        return ShortenResult(voiceover=text, cost_usd=0.0008)

    monkeypatch.setattr(rpc, "shorten_voiceover", _shorten_returns_original)

    _patch_kie(monkeypatch)
    _register_downloads()
    clients = _build_clients()
    # Both ideas overshoot, shortener gives up on both → both dropped.
    clients.tts = _FakeTTS(duration=13.0)    # type: ignore[assignment]

    result = await process_cartoon_row(_row(), clients, job_id="j")
    # All ideas dropped → row fails with VIDEO_ASSEMBLY_FAILED (existing path).
    assert result.status == STATUS_VIDEO_ASSEMBLY_FAILED
    assert result.video_urls == []
    # Each idea: one initial TTS, then shortener returned the same text → no retry.
    assert clients.tts.calls == rpc.CARTOON_NUM_IDEAS
    # Sidebar-visible error must name the actual drop reason, not the
    # old generic "ideas returned None without raising" line.
    assert result.error is not None
    assert "VO shortener returned the original text" in result.error
    _ = original    # noqa: F841


@respx.mock
async def test_cartoon_no_vo_video_is_8s(monkeypatch) -> None:
    """No-VO rows still get the flat 8.0s video — two 4s clips, no audio
    overlay. Previously this was 7s (3.5s per shot); the hard cap is universal."""
    counters = _patch_kie(monkeypatch)
    _register_downloads()
    clients = _build_clients()

    result = await process_cartoon_row(_row(vo=False), clients, job_id="j")
    assert result.status == STATUS_SUCCESS
    call = clients.rendi.concat_calls[0]
    assert call["audio"] is None
    assert call["total_video_seconds"] == pytest.approx(8.0, abs=0.01)
    assert call["per_clip"] == [pytest.approx(4.0, abs=0.01)] * 2
    assert all(d == 4 for d in counters["durations"])


@respx.mock
async def test_cartoon_voice_over_no_skips_tts(monkeypatch) -> None:
    _patch_kie(monkeypatch)
    _register_downloads()
    clients = _build_clients()

    result = await process_cartoon_row(_row(vo=False), clients, job_id="j")
    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == rpc.CARTOON_NUM_IDEAS
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
    assert len(result.video_urls) == rpc.CARTOON_NUM_IDEAS
    # All stitches still received the right number of clips.
    assert all(len(c["clips"]) == rpc.CARTOON_NUM_SHOTS for c in clients.rendi.concat_calls)


@respx.mock
async def test_cartoon_failed_animation_is_gap_filled(monkeypatch) -> None:
    # Fail one animation: the gap is filled by a neighbour clip, video still ships.
    _patch_kie(monkeypatch, seedance_fail_on=1)
    _register_downloads()
    clients = _build_clients()

    result = await process_cartoon_row(_row(), clients, job_id="j")
    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == rpc.CARTOON_NUM_IDEAS
    assert all(len(c["clips"]) == rpc.CARTOON_NUM_SHOTS for c in clients.rendi.concat_calls)


@respx.mock
async def test_cartoon_all_animations_fail(monkeypatch) -> None:
    _patch_kie(monkeypatch, seedance_fail_all=True)
    _register_downloads()
    clients = _build_clients()

    result = await process_cartoon_row(_row(), clients, job_id="j")
    assert result.status == STATUS_VIDEO_ASSEMBLY_FAILED
    assert result.video_urls == []
    # Sidebar-visible error must surface the no-clips drop reason rather
    # than the old generic "ideas returned None without raising" line.
    assert result.error is not None
    assert "no Seedance clips produced" in result.error


@respx.mock
async def test_cartoon_one_idea_fails_others_ship(monkeypatch) -> None:
    # Fail the 2nd first-shot text-to-image -> exactly one idea loses shot 1 and is
    # dropped; the remaining N-1 ideas still produce videos.
    _patch_kie(monkeypatch, t2i_fail_on=2)
    _register_downloads()
    clients = _build_clients()

    result = await process_cartoon_row(_row(), clients, job_id="j")
    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == rpc.CARTOON_NUM_IDEAS - 1


@respx.mock
async def test_cartoon_zapcap_applied(monkeypatch) -> None:
    _patch_kie(monkeypatch)
    _register_downloads()
    clients = _build_clients(with_zapcap=True)

    result = await process_cartoon_row(_row(zapcap=True), clients, job_id="j")
    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == rpc.CARTOON_NUM_IDEAS
    assert result.metadata.get("zapcap_applied") is True
    # Captioned outputs were persisted.
    assert any("videos_captioned" in key for key, _ in clients.storage.calls)


@respx.mock
async def test_cartoon_cta_overlay_failure_surfaces_to_row_error(monkeypatch) -> None:
    """When CTA is enabled and the per-idea overlay step fails, the row
    still ships its videos (non-fatal) but the failure has to land in
    ``result.error`` so the operator sees it in the sidebar — without
    this, a row that quietly lost its CTA pill is indistinguishable
    from a row that ran with CTA off."""
    _patch_kie(monkeypatch)
    _register_downloads()
    clients = _build_clients()

    # Force every overlay attempt to fail; concat still succeeds, so
    # the videos ship without the CTA pill.
    async def _overlay_boom(*, video_url, overlay_url, output_filename):
        raise RuntimeError("rendi overlay boom")

    clients.rendi.overlay_image_on_video = _overlay_boom    # type: ignore[attr-defined]

    row = CartoonRow(
        row_num=2, country="MX", vertical="automotive",
        article_url="https://example.com/article",
        voice_over=True, zapcap=False, aspect_ratio="09:16",
        script_pattern="How To", open_comments="",
        cta_enabled=True, cta_text="Get Your Quote",
    )

    result = await process_cartoon_row(row, clients, job_id="j")

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == rpc.CARTOON_NUM_IDEAS
    assert result.error is not None
    assert "CTA overlay failed" in result.error
    assert "rendi overlay boom" in result.error
    assert result.metadata.get("cta_overlay_errors")
    assert result.metadata.get("cta_overlay_applied") is False


# ── Pinned (verbatim) script routing ─────────────────────────────────────────


def _patch_pinned_kie(monkeypatch) -> dict:
    """Patch the kie wrappers in the BUILDER's namespace (separate module from
    the processor — patching ``rpc.*`` would miss the pinned path entirely)."""
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
async def test_cartoon_pinned_script_makes_one_verbatim_video(monkeypatch) -> None:
    """A pinned OVERRIDE makes ONE video (not CARTOON_NUM_IDEAS), speaks the
    exact script (no shorten), at natural pace, and flags the override in
    metadata."""
    from bulkvid.pipeline.open_comments import OpenCommentsAnalysis, OpenCommentsMode

    pinned = "Beslagautos in Nederland worden online geveild en getoond vandaag."

    async def _classify_override(_client, _text):
        return OpenCommentsAnalysis(
            mode=OpenCommentsMode.OVERRIDE, raw_text=_text, override_script=pinned
        )

    monkeypatch.setattr(rpc, "classify_open_comments", _classify_override)
    _patch_pinned_kie(monkeypatch)
    _register_downloads()
    clients = _build_clients()

    result = await process_cartoon_row(_row(), clients, job_id="jp")

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 1                       # ONE, not two
    assert result.metadata["script_used_override"] is True
    assert result.metadata["pinned_num_shots"] >= 2
    # Exactly one stitch, at natural (un-sped) pace.
    assert len(clients.rendi.concat_calls) == 1
    assert clients.rendi.concat_calls[0]["atempo"] == pytest.approx(1.0)
    # The pinned script was the TTS input, verbatim and un-shortened.
    assert clients.tts.last_texts == [pinned]
