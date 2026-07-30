"""fast-and-furious row processor — N TikTok-style realistic videos per row.

A sibling of ``row_processor_simple_motion`` (realistic photographic scenes, two
4s shots, operator-pasted images in cols D/E). The differences:

  * N DIFFERENT creative variations per row (``Number of Videos``, 1-4) — each its
    own Gen-Z script + its own visuals, like the cartoon tab's multi-idea output.
  * PER-VIDEO aspect ratio — video ``i`` uses ``Change Size i`` (blank → 9:16).
  * TikTok/Gen-Z narration sized to FILL each video (the video length is driven by
    the voiceover, so there is no ~3s silent tail).

Manual Image 1/2 (cols D/E) are SHARED by all N videos as shot 1 / shot 2; a blank
cell is auto-generated per video. The manual images are downloaded + re-uploaded
ONCE and reused across the N videos (the source is aspect-independent; only the
Seedance animation is per-aspect). Outputs land in Ready Video 1..N.

This module does NOT touch ``process_simple_motion_row`` / ``process_cartoon_row``.
It REUSES the shared pure helpers — the planner (``generate_cartoon_plan`` with the
fast-furious prompt), the atempo sizer (``compute_atempo``), the shortener, the CTA
renderer, ZapCap, the Rendi concat, and the pinned builder — and only adds the
multi-variation, per-aspect, VO-driven-length orchestration.

Pipeline:
  1. Article fetch (ScrapingBee -> direct)
  2. language detect -> classify Open Comments -> safety
  3. generate_cartoon_plan (fast-furious prompt) -> N ideas, each a VO + 2 shots
  4. Pre-resolve the shared manual images ONCE (download + re-upload)
  5. Build N videos concurrently, each at its own aspect:
     a. TTS the idea's voiceover -> size the VIDEO to the VO (no silent tail)
     b. shot images: manual (shared) as-is; blank -> nano-banana-2 realistic
     c. Seedance: animate each shot image (4s clips)
     d. Rendi: concat + overlay VO, optional per-video CTA pill
     e. persist, free Rendi storage, optional ZapCap
  6. Write back N Ready Video URLs (Ready Video 1..N).

Graceful degradation mirrors simple-motion: a failed later shot holds the previous
shot's image/clip; a failed variation is dropped but the others still ship; only a
row with ZERO usable videos fails. On a pinned OVERRIDE ("use this script:") each
of the N videos speaks the operator's exact script (audio-driven length) over its
own visuals — the verbatim path is identical to simple-motion's.

Plan: ``_plans/2026-07-30-fast-and-furious-tab.md``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from bulkvid.adapters.kie import (
    nano_banana_2_image_to_image,
    nano_banana_2_text_to_image,
    seedance_image_to_video,
)
from bulkvid.adapters.rendi import (
    SPEECH_ATEMPO,
    dimensions_for_ratio,
    normalize_aspect_ratio,
)
from bulkvid.adapters.zapcap import (
    ZapCapRenderOptions,
    ZapCapStyleOptions,
    ZapCapSubsOptions,
)
from bulkvid.http_download import download_image
from bulkvid.logging import get_logger, set_context
from bulkvid.models.row import (
    STATUS_ARTICLE_FETCH_FAILED,
    STATUS_INTERNAL_ERROR,
    STATUS_SUCCESS,
    STATUS_VIDEO_ASSEMBLY_FAILED,
    STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS,
    FastFuriousRow,
    RowResult,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.pinned_cartoon import (
    PinnedShotSpec,
    build_pinned_cartoon_video,
    fold_pinned_costs,
)
from bulkvid.orchestrator.row_processor_cartoon import (
    IMAGE_RESOLUTION,
    SEEDANCE_DURATION_SHORT,
    SEEDANCE_RESOLUTION,
    SPEECH_ATEMPO_RETRY_MAX,
    TARGET_VIDEO_SECONDS,
    VO_SHORTEN_MIN_WORDS,
    VO_SHORTEN_STEP,
    compute_atempo,
)
from bulkvid.orchestrator.runtime_settings import (
    FAST_FURIOUS_PLANNER_PROMPT_DEFAULT,
    SETTING_FAST_FURIOUS_PLANNER_PROMPT,
)
from bulkvid.pipeline.cartoon_cta import render_cartoon_cta_overlay_bytes
from bulkvid.pipeline.cartoon_prompt import (
    REALISTIC_STYLE,
    CartoonIdea,
    generate_cartoon_plan,
    image_prompt_for_shot,
    shorten_voiceover,
)
from bulkvid.pipeline.cta_defaults import default_cta_for_language
from bulkvid.pipeline.language import detect_language, reconcile_language
from bulkvid.pipeline.open_comments import OpenCommentsMode, classify_open_comments
from bulkvid.pipeline.safety import resolve_safety

_log = get_logger("row")


# ── Tunables ─────────────────────────────────────────────────────────────────

FF_NUM_SHOTS = 2          # two 4s shots stitched into each video
FF_MAX_VIDEOS = 4         # "Number of Videos" ceiling (Ready Video 1..4)

# A longer, energetic line than simple-motion's (10), so the Gen-Z narration fills
# the video. The video length is driven BY the voiceover (see FF_MAX_EFFECTIVE +
# FF_VO_TAIL), so there is no ~3s silent tail. Plan
# ``_plans/2026-07-30-fast-and-furious-tab.md``.
FF_TARGET_WORDS = 17
FF_MIN_WORDS = 14
FF_MAX_WORDS = 22
# VO fit ceiling: nudged up from simple-motion's 7.5 so the longer line fits
# inside the 8s of footage without triggering shorten-and-retry, while staying
# < footage so the final frame never freezes.
FF_MAX_EFFECTIVE_VO_SECONDS = 7.8
FF_VO_TAIL_SECONDS = 0.3     # small breath after the last spoken word

# Motion prompt for a PASTED (manual) image. The planner can't see the operator's
# photo, so a scene-specific motion would mismatch it — a universal, gentle
# cinematic push-in is the safe default. Generated shots use the planner's own
# scene-matched motion (mirrors simple-motion).
MANUAL_IMAGE_MOTION = (
    "Subtle, natural movement with a slow, gentle cinematic camera push-in."
)


def _slug(row_num: int, job_id: str | None = None) -> str:
    job_part = (job_id or "job").replace("/", "_")
    return f"{job_part}_r{row_num}_{int(time.time())}"


def _clamp_num_videos(n: int) -> int:
    """Clamp the operator's ``Number of Videos`` into [1, FF_MAX_VIDEOS]."""
    try:
        v = int(n)
    except (TypeError, ValueError):
        return 1
    return max(1, min(FF_MAX_VIDEOS, v))


def _aspect_for_idea(aspect_ratios: list[str], idx: int) -> str:
    """Aspect for video ``idx``: ``Change Size {idx+1}`` if present, else 9:16."""
    if idx < len(aspect_ratios):
        raw = (aspect_ratios[idx] or "").strip()
        if raw:
            return normalize_aspect_ratio(raw)
    return normalize_aspect_ratio("9:16")


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


async def process_fast_furious_row(
    row: FastFuriousRow,
    clients: PipelineClients,
    *,
    job_id: str | None = None,
) -> RowResult:
    """Run the fast-and-furious pipeline for one row. Returns a RowResult. Never raises."""
    set_context(batch_id=job_id, row_num=row.row_num)
    t0 = time.monotonic()
    costs = _Costs()
    slug = _slug(row.row_num, job_id)
    num_videos = _clamp_num_videos(row.num_videos)
    aspects = [_aspect_for_idea(row.aspect_ratios, i) for i in range(num_videos)]
    manual_for_shot = [
        (row.manual_image_1 or "").strip(),
        (row.manual_image_2 or "").strip(),
    ]
    metadata: dict[str, Any] = {
        "row_num": row.row_num,
        "country": row.country,
        "vertical": row.vertical,
        "article_url": row.article_url,
        "num_videos": num_videos,
        "aspect_ratios": aspects,
        "voice_over": row.voice_over,
        "zapcap": row.zapcap,
        "tab": "fast_furious",
        "num_shots": FF_NUM_SHOTS,
        "manual_image_1": bool(manual_for_shot[0]),
        "manual_image_2": bool(manual_for_shot[1]),
    }
    zapcap_failed = False

    _log.info(
        "row_start",
        country=row.country,
        vertical=row.vertical,
        num_videos=num_videos,
        aspects=aspects,
        zapcap=row.zapcap,
        vo=row.voice_over,
        tab="fast_furious",
        manual_image_1=bool(manual_for_shot[0]),
        manual_image_2=bool(manual_for_shot[1]),
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
            lang = reconcile_language(
                lang, article_url=row.article_url, country=row.country
            )

            analysis = await classify_open_comments(clients.openai, row.open_comments)
            costs.classify += analysis.cost_usd

            safety = await resolve_safety(
                clients.settings_store, row.vertical, row.row_num
            )
            metadata["safety_matched"] = safety.matched
            metadata["safety_keyword"] = safety.matched_keyword

            # One planner call → N INDEPENDENT ideas (variations), each with a
            # Gen-Z VO line + 2 realistic scene/motion shots. The fast-furious
            # prompt + larger word budget fill each video's window.
            plan = await generate_cartoon_plan(
                clients.openai,
                article_body=article_body,
                country=row.country,
                vertical=row.vertical,
                language=lang.language,
                script_pattern=row.script_pattern,
                open_comments=analysis,
                num_ideas=num_videos,
                num_shots=FF_NUM_SHOTS,
                settings_store=clients.settings_store,
                safety=safety,
                planner_prompt_key=SETTING_FAST_FURIOUS_PLANNER_PROMPT,
                planner_prompt_default=FAST_FURIOUS_PLANNER_PROMPT_DEFAULT,
                target_words=FF_TARGET_WORDS,
                min_words=FF_MIN_WORDS,
                max_words=FF_MAX_WORDS,
            )
            costs.plan += plan.cost_usd
            metadata["language"] = lang.language
            metadata["open_comments_mode"] = analysis.mode.value
            is_pinned = (
                analysis.mode is OpenCommentsMode.OVERRIDE
                and bool(analysis.override_script)
            )
            metadata["script_used_override"] = is_pinned
            if is_pinned:
                metadata["script_override_oversize"] = analysis.override_oversize
            if plan.chosen_template_id:
                metadata["chosen_template_id"] = plan.chosen_template_id
        except Exception as e:
            return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)

        # ─── Stage 3: pre-resolve the SHARED manual images (once) ───
        # A manual image is aspect-independent (Seedance handles the aspect), so
        # download + re-upload ONCE and reuse the stable URL for every video's
        # matching shot. A failed download degrades that shot to "generate" rather
        # than failing all N videos.
        shared_manual_urls: list[str | None] = [None, None]
        for s, murl in enumerate(manual_for_shot):
            if not murl:
                continue
            try:
                raw = await download_image(murl, timeout=60.0)
                up = await clients.storage.upload_bytes(
                    raw,
                    key=f"bulkvid/fast_furious_images/{slug}/manual_shot{s + 1}.png",
                    content_type="image/png",
                )
                costs.storage += up.cost_usd
                shared_manual_urls[s] = up.url
            except Exception as e:
                _log.warning(
                    "fast_furious_manual_image_failed_will_generate",
                    shot=s + 1, error=str(e)[:200],
                )
                shared_manual_urls[s] = None
        metadata["manual_image_1_resolved"] = shared_manual_urls[0] is not None
        metadata["manual_image_2_resolved"] = shared_manual_urls[1] is not None

        # ─── Stage 4: build N videos (one per variation), each at its aspect ───
        idea_failure_messages: list[str] = []

        def _zapcap_opts(cta_on: bool) -> ZapCapRenderOptions | None:
            # When the CTA pill is on, push the caption higher so it doesn't cover
            # the pill (mirrors simple-motion / cartoon).
            if not cta_on:
                return None
            return ZapCapRenderOptions(
                subs=ZapCapSubsOptions(),
                style=ZapCapStyleOptions(top=30, font_size=36),
            )

        async def _make_cta_overlay(idx: int, aspect: str) -> tuple[str | None, str | None]:
            """Render + upload the CTA pill overlay for one video's aspect.

            Returns ``(overlay_url, error)``. Non-fatal: on failure the video ships
            without the pill. Rendered per-video because the aspect differs.
            """
            if not row.cta_enabled:
                return None, None
            cta_text_used = (row.cta_text.strip()
                             or default_cta_for_language(lang.language))
            try:
                overlay_w, overlay_h = dimensions_for_ratio(aspect)
                overlay_bytes = await asyncio.to_thread(
                    render_cartoon_cta_overlay_bytes,
                    cta_text_used,
                    canvas_width=overlay_w,
                    canvas_height=overlay_h,
                )
                overlay_upload = await clients.storage.upload_bytes(
                    overlay_bytes,
                    key=f"bulkvid/cta_overlays/{slug}/v{idx + 1}.png",
                    content_type="image/png",
                )
                costs.storage += overlay_upload.cost_usd
                return overlay_upload.url, None
            except Exception as e:
                err = str(e)[:200]
                _log.error(
                    "fast_furious_cta_overlay_failed_skipped",
                    idea=idx + 1, error=err, cta_text=cta_text_used[:80],
                )
                return None, err

        async def _build_idea(idx: int, idea: CartoonIdea, aspect: str) -> str | None:
            """Build one variation's stitched, voiced, optionally-captioned video."""
            nonlocal zapcap_failed
            try:
                cta_overlay_url, _cta_err = await _make_cta_overlay(idx, aspect)

                # 4a. Voiceover (optional). Two 4s clips = 8s of footage; the VIDEO
                # length is driven by the VO (no silent tail). If the synthesized VO
                # measures longer than the fit ceiling, shorten + re-TTS once; if it
                # still overshoots, drop this variation.
                vo_url: str | None = None
                per_clip_seconds: list[float] = [float(SEEDANCE_DURATION_SHORT)] * FF_NUM_SHOTS
                seedance_durations: list[int] = [SEEDANCE_DURATION_SHORT] * FF_NUM_SHOTS
                target_video_seconds = TARGET_VIDEO_SECONDS
                vo_atempo = SPEECH_ATEMPO

                if row.voice_over:
                    final_text = idea.voiceover
                    tts = await clients.tts.synthesize(
                        text=final_text,
                        language=lang.language,
                        style_prompt=idea.style_direction,
                        country=row.country,
                    )
                    costs.tts += tts.cost_usd
                    vo_atempo, effective = compute_atempo(
                        tts.duration_seconds,
                        max_effective=FF_MAX_EFFECTIVE_VO_SECONDS,
                    )
                    original_effective = effective
                    shortened = False

                    if effective > FF_MAX_EFFECTIVE_VO_SECONDS:
                        shorten_target = max(
                            VO_SHORTEN_MIN_WORDS, FF_TARGET_WORDS - VO_SHORTEN_STEP
                        )
                        _log.warning(
                            "fast_furious_vo_too_long_shortening",
                            idea=idx + 1,
                            original_words=len(final_text.split()),
                            original_effective=round(effective, 3),
                            shorten_target_words=shorten_target,
                        )
                        shorten_result = await shorten_voiceover(
                            clients.openai,
                            text=final_text,
                            language=lang.language,
                            target_words=shorten_target,
                        )
                        costs.plan += shorten_result.cost_usd

                        if shorten_result.voiceover == final_text:
                            _log.error(
                                "fast_furious_vo_shortener_no_change_dropped",
                                idea=idx + 1, original_effective=round(effective, 3),
                            )
                            idea_failure_messages.append(
                                f"video {idx + 1}: VO shortener returned the "
                                f"original text — couldn't trim {round(effective, 1)}s "
                                f"VO under {FF_MAX_EFFECTIVE_VO_SECONDS}s cap"
                            )
                            return None

                        final_text = shorten_result.voiceover
                        tts = await clients.tts.synthesize(
                            text=final_text,
                            language=lang.language,
                            style_prompt=idea.style_direction,
                            country=row.country,
                        )
                        costs.tts += tts.cost_usd
                        vo_atempo, effective = compute_atempo(
                            tts.duration_seconds,
                            max_atempo=SPEECH_ATEMPO_RETRY_MAX,
                            max_effective=FF_MAX_EFFECTIVE_VO_SECONDS,
                        )
                        shortened = True

                        if effective > FF_MAX_EFFECTIVE_VO_SECONDS:
                            _log.error(
                                "fast_furious_vo_too_long_after_retry_dropped",
                                idea=idx + 1,
                                original_effective=round(original_effective, 3),
                                retry_effective=round(effective, 3),
                            )
                            idea_failure_messages.append(
                                f"video {idx + 1}: VO too long even after "
                                f"shorten+retry ({round(effective, 2)}s vs "
                                f"{FF_MAX_EFFECTIVE_VO_SECONDS}s cap @ "
                                f"{SPEECH_ATEMPO_RETRY_MAX}x max speedup)"
                            )
                            return None

                    # End the video WITH the voiceover: length = played VO + a small
                    # breath, capped at the 8s of footage. No silent tail.
                    target_video_seconds = min(
                        effective + FF_VO_TAIL_SECONDS, TARGET_VIDEO_SECONDS
                    )

                    vo_up = await clients.storage.upload_bytes(
                        tts.wav_bytes,
                        key=f"bulkvid/vo/{slug}/v{idx + 1}.wav",
                        content_type="audio/wav",
                    )
                    costs.storage += vo_up.cost_usd
                    vo_url = vo_up.url

                    _log.info(
                        "fast_furious_vo_sized",
                        idea=idx + 1,
                        aspect=aspect,
                        vo_words=len(final_text.split()),
                        vo_raw_seconds=round(tts.duration_seconds, 3),
                        vo_effective_seconds=round(effective, 3),
                        vo_atempo=round(vo_atempo, 3),
                        video_seconds=round(target_video_seconds, 3),
                        vo_dwell_seconds=round(target_video_seconds - effective, 3),
                        shortened=shortened,
                    )

                # 4b. Shot images. Each shot is EITHER the SHARED manual image
                # (used as-is) OR generated in REALISTIC_STYLE at this video's
                # aspect. A generated shot 2 chains on shot 1 so it matches.
                image_urls: list[str] = []
                shot_motions: list[str] = []
                for s, shot in enumerate(idea.shots[:FF_NUM_SHOTS]):
                    manual_url = (
                        shared_manual_urls[s] if s < len(shared_manual_urls) else None
                    )
                    if manual_url:
                        image_urls.append(manual_url)
                        shot_motions.append(MANUAL_IMAGE_MOTION)
                        continue

                    is_chained = s > 0 and bool(image_urls)
                    prompt = image_prompt_for_shot(
                        shot.scene, is_chained=is_chained, style=REALISTIC_STYLE
                    )
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
                        shot_motions.append(shot.motion)
                    except Exception as e:
                        if not image_urls:
                            raise    # first shot must succeed
                        _log.warning(
                            "fast_furious_shot_image_failed_held",
                            idea=idx + 1, shot=s + 1, error=str(e)[:200],
                        )
                        image_urls.append(image_urls[-1])    # hold previous frame
                        shot_motions.append(shot.motion)

                # 4c. Animate each image (concurrently). A failed clip holds a
                # neighbour so the concat still has FF_NUM_SHOTS clips in order.
                async def _animate(s: int, image_url: str) -> tuple[int, str | None]:
                    try:
                        clip_url, cost = await seedance_image_to_video(
                            clients.kie, image_url, shot_motions[s], aspect,
                            duration=seedance_durations[s], resolution=SEEDANCE_RESOLUTION,
                        )
                        costs.seedance += cost
                        return s, clip_url
                    except Exception as e:
                        _log.warning(
                            "fast_furious_shot_animate_failed",
                            idea=idx + 1, shot=s + 1, error=str(e)[:200],
                        )
                        return s, None

                animated = await asyncio.gather(
                    *[_animate(s, u) for s, u in enumerate(image_urls)]
                )
                clip_by_shot = {s: url for s, url in animated}
                ordered: list[str | None] = [
                    clip_by_shot.get(s) for s in range(len(image_urls))
                ]
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
                    _log.error("fast_furious_idea_no_clips", idea=idx + 1)
                    idea_failure_messages.append(
                        f"video {idx + 1}: no Seedance clips produced "
                        f"for any of {len(image_urls)} shots"
                    )
                    return None

                # 4d. Stitch + overlay VO. The output is trimmed to the VO-driven
                # length (no silent tail); the VO fits inside it by construction.
                stitched = await clients.rendi.concat_clips_with_audio(
                    clip_urls,
                    vo_url,
                    per_clip_seconds=per_clip_seconds,
                    output_filename=f"v{idx + 1}.mp4",
                    aspect_ratio=aspect,
                    total_video_seconds=target_video_seconds,
                    atempo=vo_atempo,
                )
                costs.rendi += stitched.cost_usd
                cleanup_command_ids: list[str] = [stitched.command_id]

                # 4d.5. Optional CTA overlay (NON-FATAL).
                video_url_for_persist = stitched.url
                if cta_overlay_url:
                    try:
                        overlaid = await clients.rendi.overlay_image_on_video(
                            video_url=stitched.url,
                            overlay_url=cta_overlay_url,
                            output_filename=f"v{idx + 1}_cta.mp4",
                        )
                        costs.rendi += overlaid.cost_usd
                        cleanup_command_ids.append(overlaid.command_id)
                        video_url_for_persist = overlaid.url
                    except Exception as cta_err:
                        _log.error(
                            "fast_furious_cta_overlay_failed_kept_original",
                            idea=idx + 1, error=str(cta_err)[:300],
                        )

                # 4e. Persist to our storage, then free the Rendi copies.
                data = await download_image(video_url_for_persist, timeout=180.0)
                up = await clients.storage.upload_bytes(
                    data,
                    key=f"bulkvid/videos/{slug}/v{idx + 1}.mp4",
                    content_type="video/mp4",
                )
                costs.storage += up.cost_usd
                final_url = up.url
                await clients.rendi.cleanup_commands(cleanup_command_ids)

                # 4f. Optional ZapCap. On failure keep the uncaptioned video.
                if row.zapcap and clients.zapcap is not None:
                    try:
                        cap_url, cost = await clients.zapcap.caption_video(
                            video_bytes=data,
                            language=lang.language,
                            filename=f"v{idx + 1}.mp4",
                            render_options=_zapcap_opts(bool(cta_overlay_url)),
                            video_duration_seconds=target_video_seconds,
                        )
                        costs.zapcap += cost
                        cap_bytes = await download_image(cap_url, timeout=180.0)
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
                            "fast_furious_zapcap_failed_kept_original",
                            idea=idx + 1, error=str(e)[:200],
                        )

                return final_url
            except Exception as e:
                err_msg = str(e)[:300]
                idea_failure_messages.append(f"video {idx + 1}: {err_msg}")
                _log.error("fast_furious_idea_failed", idea=idx + 1, error=err_msg)
                return None

        async def _build_pinned_idea(
            idx: int, idea: CartoonIdea, aspect: str
        ) -> str | None:
            """Build one variation speaking the operator's EXACT pinned script over
            this idea's visuals (shared manual images or generated), audio-driven
            length. Reuses the shared pinned builder."""
            nonlocal zapcap_failed
            try:
                cta_overlay_url, _cta_err = await _make_cta_overlay(idx, aspect)
                shots: list[PinnedShotSpec] = []
                for s in range(FF_NUM_SHOTS):
                    scene = idea.shots[s].scene if s < len(idea.shots) else ""
                    manual_url = (
                        shared_manual_urls[s] if s < len(shared_manual_urls) else None
                    )
                    motion = (
                        MANUAL_IMAGE_MOTION if manual_url
                        else (idea.shots[s].motion if s < len(idea.shots)
                              else MANUAL_IMAGE_MOTION)
                    )
                    shots.append(
                        PinnedShotSpec(
                            scene=scene, motion=motion,
                            manual_image_url=(manual_url or ""),
                        )
                    )
                pinned_res = await build_pinned_cartoon_video(
                    clients=clients,
                    slug=f"{slug}_v{idx + 1}",
                    pinned_script=analysis.override_script or "",
                    style_direction=idea.style_direction,
                    shots=shots,
                    language=lang.language,
                    country=row.country,
                    aspect=aspect,
                    voice_over=row.voice_over,
                    fixed_shots=True,
                    image_style=REALISTIC_STYLE,
                    cta_overlay_url=cta_overlay_url,
                    zapcap_enabled=bool(row.zapcap and clients.zapcap is not None),
                    zapcap_render_options=_zapcap_opts(bool(cta_overlay_url)),
                )
                fold_pinned_costs(costs, pinned_res)
                if pinned_res.zapcap_failed:
                    zapcap_failed = True
                if pinned_res.final_url:
                    return pinned_res.final_url
                idea_failure_messages.append(
                    f"video {idx + 1}: {pinned_res.error or 'pinned build returned no video'}"
                )
                return None
            except Exception as e:
                err_msg = str(e)[:300]
                idea_failure_messages.append(f"video {idx + 1}: {err_msg}")
                _log.error("fast_furious_pinned_idea_failed", idea=idx + 1, error=err_msg)
                return None

        builder = _build_pinned_idea if is_pinned else _build_idea
        results = await asyncio.gather(
            *[
                builder(i, plan.ideas[i], aspects[i])
                for i in range(min(num_videos, len(plan.ideas)))
            ]
        )
        video_urls = [u for u in results if u]

        if not video_urls:
            detail = (
                " | ".join(idea_failure_messages)
                if idea_failure_messages
                else "variations returned None without raising"
            )
            return _fail(
                row, STATUS_VIDEO_ASSEMBLY_FAILED,
                f"no usable videos produced — {detail}",
                t0, costs, metadata,
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
    row: FastFuriousRow,
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
    row: FastFuriousRow,
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
