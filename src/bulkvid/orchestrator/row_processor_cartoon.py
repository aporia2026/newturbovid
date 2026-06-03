"""Cartoon row processor — animated, multi-shot videos generated from text.

The "cartoon" tab does NO seed image. Per row it produces TWO independent
~6-7s videos, each a stitched sequence of short Seedance image-to-video clips
over a short voiceover.

Pipeline (plan _plans/2026-06-03-cartoon-mode.md):
  1. Article fetch (Tavily -> ScrapingBee)
  2. language detect -> classify Open Comments
  3. generate_cartoon_plan -> 2 ideas, each with a VO line + N scene/motion shots
  4. For EACH idea (concurrently), build one video:
     a. TTS the voiceover -> measure duration -> upload                (if VO=Yes)
     b. nano-banana-2: shot 1 text-to-image, shots 2+ image-to-image
        chained on shot 1 (carries the character/style across the cut)
     c. Seedance: animate each shot image (4s clips)                   (concurrent)
     d. Rendi: trim each clip to VO/num_shots and concat + overlay VO
     e. persist to storage, free Rendi storage, optional ZapCap
  5. Write back two Ready Video URLs.

Graceful degradation: a failed later shot reuses the previous shot's image/clip
(a hold) rather than failing the whole video; a failed idea is dropped but the
other idea still ships. Only a row with ZERO usable videos fails.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from bulkvid.adapters.kie import (
    nano_banana_2_image_to_image,
    nano_banana_2_text_to_image,
    seedance_image_to_video,
)
from bulkvid.adapters.rendi import SPEECH_ATEMPO, normalize_aspect_ratio
from bulkvid.logging import get_logger, set_context
from bulkvid.models.row import (
    STATUS_ARTICLE_FETCH_FAILED,
    STATUS_INTERNAL_ERROR,
    STATUS_SUCCESS,
    STATUS_VIDEO_ASSEMBLY_FAILED,
    STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS,
    CartoonRow,
    RowResult,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.pipeline.cartoon_prompt import (
    CartoonIdea,
    generate_cartoon_plan,
    image_prompt_for_shot,
)
from bulkvid.pipeline.language import detect_language
from bulkvid.pipeline.open_comments import classify_open_comments

_log = get_logger("row")


# ── Tunables (admin-surfaced in Phase 5) ─────────────────────────────────────

CARTOON_NUM_IDEAS = 2          # videos per row (Ready Video 1 + 2)
CARTOON_NUM_SHOTS = 2          # shots stitched per video
SEEDANCE_DURATION = 4          # seconds per Seedance clip (4/8/12 only)
SEEDANCE_RESOLUTION = "720p"
IMAGE_RESOLUTION = "1K"        # nano-banana-2 resolution (animated -> 720p)
NO_VO_PER_SHOT_SECONDS = 3.5   # per-shot length when Voice Over = No (gives 7s, in band)
MIN_PER_SHOT_SECONDS = 1.5     # floor so a short VO never yields micro-cuts

# Every cartoon video lands in this band regardless of the VO's natural length:
# short VOs extend with held video + a brief trailing silence (= VO_TAIL),
# long VOs are cut with a 0.3s audio fade. Set in Rendi via -t + afade (see
# render_cartoon_concat_command). The floor is 4s — anything tighter feels
# rushed — and the soft tail (rather than a 6s hard floor) keeps short VOs
# from sitting in 2-3 seconds of dead air at the end.
TARGET_VIDEO_MIN_SECONDS = 4.0
TARGET_VIDEO_MAX_SECONDS = 8.0
VO_TAIL_SECONDS = 0.8          # silence dwell after VO ends, before the cap


async def _download(url: str, *, timeout: float = 60.0) -> bytes:
    async with httpx.AsyncClient(timeout=timeout) as c:
        resp = await c.get(url, follow_redirects=True)
        resp.raise_for_status()
        return resp.content


def _slug(row_num: int, job_id: str | None = None) -> str:
    job_part = (job_id or "job").replace("/", "_")
    return f"{job_part}_r{row_num}_{int(time.time())}"


@dataclass
class _Costs:
    article: float = 0.0
    language: float = 0.0
    classify: float = 0.0
    plan: float = 0.0
    image_gen: float = 0.0
    tts: float = 0.0
    seedance: float = 0.0
    rendi: float = 0.0
    zapcap: float = 0.0
    storage: float = 0.0

    @property
    def total(self) -> float:
        return round(
            self.article + self.language + self.classify + self.plan
            + self.image_gen + self.tts + self.seedance + self.rendi
            + self.zapcap + self.storage,
            6,
        )


async def process_cartoon_row(
    row: CartoonRow,
    clients: PipelineClients,
    *,
    job_id: str | None = None,
) -> RowResult:
    """Run the cartoon pipeline for one row. Returns a RowResult. Never raises."""
    set_context(batch_id=job_id, row_num=row.row_num)
    t0 = time.monotonic()
    costs = _Costs()
    slug = _slug(row.row_num, job_id)
    aspect = normalize_aspect_ratio(row.aspect_ratio)
    metadata: dict[str, Any] = {
        "row_num": row.row_num,
        "country": row.country,
        "vertical": row.vertical,
        "article_url": row.article_url,
        "aspect_ratio": row.aspect_ratio,
        "voice_over": row.voice_over,
        "zapcap": row.zapcap,
        "tab": "cartoon",
        "num_ideas": CARTOON_NUM_IDEAS,
        "num_shots": CARTOON_NUM_SHOTS,
    }
    zapcap_failed = False

    _log.info(
        "row_start",
        country=row.country,
        vertical=row.vertical,
        aspect=row.aspect_ratio,
        zapcap=row.zapcap,
        vo=row.voice_over,
        tab="cartoon",
    )

    try:
        # ─── Stage 1: article fetch ───
        try:
            art = await clients.article.fetch(row.article_url)
            costs.article += art.cost_usd
            metadata["article_chars"] = art.char_count
            metadata["article_source"] = art.source
            article_body: str = art.content
        except Exception as e:
            return _fail(row, STATUS_ARTICLE_FETCH_FAILED, str(e), t0, costs, metadata)

        # ─── Stage 2: language detect -> classify -> plan ───
        try:
            lang = await detect_language(clients.openai, article_body)
            costs.language += lang.cost_usd

            analysis = await classify_open_comments(clients.openai, row.open_comments)
            costs.classify += analysis.cost_usd

            plan = await generate_cartoon_plan(
                clients.openai,
                article_body=article_body,
                country=row.country,
                vertical=row.vertical,
                language=lang.language,
                script_pattern=row.script_pattern,
                open_comments=analysis,
                num_ideas=CARTOON_NUM_IDEAS,
                num_shots=CARTOON_NUM_SHOTS,
            )
            costs.plan += plan.cost_usd
            metadata["language"] = lang.language
            metadata["open_comments_mode"] = analysis.mode.value
        except Exception as e:
            return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)

        # ─── Stage 3+4: build each idea into a finished video (concurrently) ───

        async def _build_idea(idx: int, idea: CartoonIdea) -> str | None:
            """Build one stitched, voiced, optionally-captioned video. None on failure."""
            nonlocal zapcap_failed
            try:
                # 4a. Voiceover (optional). The video's *output* length is clamped
                # to [TARGET_VIDEO_MIN, TARGET_VIDEO_MAX] regardless of VO length:
                # short VOs leave trailing silence, long VOs are cut with a fade
                # in the Rendi concat (-t + afade). per_shot is then derived from
                # the clamped target so the two video clips fill it exactly.
                vo_url: str | None = None
                target_video_seconds = NO_VO_PER_SHOT_SECONDS * CARTOON_NUM_SHOTS
                per_shot = NO_VO_PER_SHOT_SECONDS
                if row.voice_over:
                    tts = await clients.tts.synthesize(
                        text=idea.voiceover,
                        language=lang.language,
                        style_prompt=idea.style_direction,
                        country=row.country,
                    )
                    costs.tts += tts.cost_usd
                    vo_up = await clients.storage.upload_bytes(
                        tts.wav_bytes,
                        key=f"bulkvid/vo/{slug}/idea{idx + 1}.wav",
                        content_type="audio/wav",
                    )
                    costs.storage += vo_up.cost_usd
                    vo_url = vo_up.url
                    # The concat command speeds the VO up by SPEECH_ATEMPO, so the
                    # effective played length is shorter than the raw WAV. Target
                    # = effective VO + a small dwell, clamped into the band.
                    effective = tts.duration_seconds / SPEECH_ATEMPO
                    natural_target = effective + VO_TAIL_SECONDS
                    target_video_seconds = min(
                        max(natural_target, TARGET_VIDEO_MIN_SECONDS),
                        TARGET_VIDEO_MAX_SECONDS,
                    )
                    per_shot = min(
                        max(target_video_seconds / CARTOON_NUM_SHOTS, MIN_PER_SHOT_SECONDS),
                        float(SEEDANCE_DURATION),
                    )
                    # The flag reflects what actually happened to the natural
                    # (effective + tail) target — "floor" means the dwell got
                    # padded up, "ceiling" means the audio fade engaged.
                    if natural_target < TARGET_VIDEO_MIN_SECONDS:
                        clamp_state = "floor"
                    elif natural_target > TARGET_VIDEO_MAX_SECONDS:
                        clamp_state = "ceiling"
                    else:
                        clamp_state = "none"
                    _log.info(
                        "cartoon_vo_sized",
                        idea=idx + 1,
                        vo_raw_seconds=round(tts.duration_seconds, 3),
                        vo_effective_seconds=round(effective, 3),
                        natural_target_seconds=round(natural_target, 3),
                        target_video_seconds=round(target_video_seconds, 3),
                        per_shot_seconds=round(per_shot, 3),
                        clamp=clamp_state,
                    )

                # 4b. Scene images — shot 1 from text, shots 2+ chained on shot 1.
                image_urls: list[str] = []
                for s, shot in enumerate(idea.shots):
                    is_chained = s > 0
                    prompt = image_prompt_for_shot(shot.scene, is_chained=is_chained)
                    try:
                        if is_chained:
                            url, cost = await nano_banana_2_image_to_image(
                                clients.kie, image_urls[0], prompt, aspect,
                                resolution=IMAGE_RESOLUTION,
                            )
                        else:
                            url, cost = await nano_banana_2_text_to_image(
                                clients.kie, prompt, aspect, resolution=IMAGE_RESOLUTION,
                            )
                        costs.image_gen += cost
                        image_urls.append(url)
                    except Exception as e:
                        if not image_urls:
                            raise    # first shot must succeed
                        _log.warning(
                            "cartoon_shot_image_failed_held",
                            idea=idx + 1, shot=s + 1, error=str(e)[:200],
                        )
                        image_urls.append(image_urls[-1])    # hold previous frame

                # 4c. Animate each image (concurrently). A failed clip holds a
                # neighbour so the concat still has NUM_SHOTS clips in order.
                async def _animate(s: int, image_url: str) -> tuple[int, str | None]:
                    try:
                        clip_url, cost = await seedance_image_to_video(
                            clients.kie, image_url, idea.shots[s].motion, aspect,
                            duration=SEEDANCE_DURATION, resolution=SEEDANCE_RESOLUTION,
                        )
                        costs.seedance += cost
                        return s, clip_url
                    except Exception as e:
                        _log.warning(
                            "cartoon_shot_animate_failed",
                            idea=idx + 1, shot=s + 1, error=str(e)[:200],
                        )
                        return s, None

                animated = await asyncio.gather(
                    *[_animate(s, u) for s, u in enumerate(image_urls)]
                )
                clip_by_shot = {s: url for s, url in animated}
                # Fill gaps with the nearest successful clip: forward pass holds
                # the previous good clip, then a backward pass covers leading gaps
                # (a failed FIRST shot). If any clip succeeded, all slots fill.
                ordered: list[str | None] = [clip_by_shot.get(s) for s in range(len(image_urls))]
                last_good: str | None = None
                for s in range(len(ordered)):
                    if ordered[s]:
                        last_good = ordered[s]
                    elif last_good:
                        ordered[s] = last_good
                next_good: str | None = None
                for s in range(len(ordered) - 1, -1, -1):
                    if ordered[s]:
                        next_good = ordered[s]
                    elif next_good:
                        ordered[s] = next_good
                clip_urls = [c for c in ordered if c]
                if not clip_urls:
                    _log.error("cartoon_idea_no_clips", idea=idx + 1)
                    return None

                # 4d. Stitch + overlay VO. ``total_video_seconds`` clamps the
                # output to the [6, 8]s band — the row processor sized per_shot
                # to fill that exactly.
                stitched = await clients.rendi.concat_clips_with_audio(
                    clip_urls,
                    vo_url,
                    per_clip_seconds=per_shot,
                    output_filename=f"v{idx + 1}.mp4",
                    aspect_ratio=aspect,
                    total_video_seconds=target_video_seconds,
                )
                costs.rendi += stitched.cost_usd

                # 4e. Persist to our storage, then free the Rendi copy.
                data = await _download(stitched.url, timeout=180.0)
                up = await clients.storage.upload_bytes(
                    data,
                    key=f"bulkvid/videos/{slug}/v{idx + 1}.mp4",
                    content_type="video/mp4",
                )
                costs.storage += up.cost_usd
                final_url = up.url
                await clients.rendi.cleanup_commands([stitched.command_id])

                # 4f. Optional ZapCap. On failure keep the uncaptioned video.
                if row.zapcap and clients.zapcap is not None:
                    try:
                        cap_url, cost = await clients.zapcap.caption_video(
                            video_bytes=data,
                            language=lang.language,
                            filename=f"v{idx + 1}.mp4",
                        )
                        costs.zapcap += cost
                        cap_bytes = await _download(cap_url, timeout=180.0)
                        cap_up = await clients.storage.upload_bytes(
                            cap_bytes,
                            key=f"bulkvid/videos_captioned/{slug}/v{idx + 1}.mp4",
                            content_type="video/mp4",
                        )
                        costs.storage += cap_up.cost_usd
                        final_url = cap_up.url
                    except Exception as e:
                        zapcap_failed = True
                        _log.error(
                            "cartoon_zapcap_failed_kept_original",
                            idea=idx + 1, error=str(e)[:200],
                        )

                return final_url
            except Exception as e:
                _log.error("cartoon_idea_failed", idea=idx + 1, error=str(e)[:300])
                return None

        results = await asyncio.gather(
            *[_build_idea(i, idea) for i, idea in enumerate(plan.ideas)]
        )
        video_urls = [u for u in results if u]

        if not video_urls:
            return _fail(
                row, STATUS_VIDEO_ASSEMBLY_FAILED,
                "no usable videos produced for any idea", t0, costs, metadata,
            )

        metadata["videos_produced"] = len(video_urls)
        if zapcap_failed:
            metadata["zapcap_applied"] = False
            return _ok(
                row, video_urls, t0, costs, metadata,
                status=STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS,
            )
        if row.zapcap and clients.zapcap is not None:
            metadata["zapcap_applied"] = True
        return _ok(row, video_urls, t0, costs, metadata)

    except Exception as e:
        _log.exception("row_internal_error", error=str(e))
        return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)


# ── Result builders ──────────────────────────────────────────────────────────


def _ok(
    row: CartoonRow,
    video_urls: list[str],
    t0: float,
    costs: _Costs,
    metadata: dict[str, Any],
    *,
    status: str = STATUS_SUCCESS,
) -> RowResult:
    elapsed = round(time.monotonic() - t0, 3)
    metadata["cost_breakdown"] = costs.__dict__.copy()
    _log.info(
        "row_done",
        status=status,
        cost_usd=costs.total,
        elapsed_seconds=elapsed,
        video_count=len(video_urls),
    )
    return RowResult(
        row_num=row.row_num,
        status=status,
        video_urls=video_urls,
        cost_usd=costs.total,
        elapsed_seconds=elapsed,
        metadata=metadata,
    )


def _fail(
    row: CartoonRow,
    status: str,
    error: str,
    t0: float,
    costs: _Costs,
    metadata: dict[str, Any],
) -> RowResult:
    elapsed = round(time.monotonic() - t0, 3)
    metadata["cost_breakdown"] = costs.__dict__.copy()
    _log.error(
        "row_failed",
        status=status,
        error=error[:300],
        cost_usd=costs.total,
        elapsed_seconds=elapsed,
    )
    return RowResult(
        row_num=row.row_num,
        status=status,
        video_urls=[],
        cost_usd=costs.total,
        elapsed_seconds=elapsed,
        error=error[:1000],
        metadata=metadata,
    )
