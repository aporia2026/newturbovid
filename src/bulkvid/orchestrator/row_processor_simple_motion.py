"""simple-motion row processor — animate super-realistic images.

A sibling of ``row_processor_cartoon``. The cartoon tab generates cartoon scenes
from scratch and produces TWO videos per row. The simple-motion tab produces ONE
8-second video per row (two 4s shots stitched) from SUPER-REALISTIC photographs,
and lets the operator paste their OWN images:

  * Manual Image 1 (sheet col D) is shot 1; Manual Image 2 (col E) is shot 2.
  * A blank cell is auto-generated (realistic style); a filled cell is animated
    as-is (downloaded + re-uploaded to our storage, no AI rewrite).

To keep the existing cartoon / yt-cartoon paths byte-identical, this module does
NOT touch ``process_cartoon_row``. It REUSES the shared pure helpers — the planner
(``generate_cartoon_plan`` with the realistic prompt + ``num_ideas=1``), the
atempo sizer (``compute_atempo``), the shortener, the CTA renderer, ZapCap, and
the Rendi concat — and only swaps the image step (manual-or-generate) and the
image style (``REALISTIC_STYLE``).

Pipeline:
  1. Article fetch (Tavily -> ScrapingBee)
  2. language detect -> classify Open Comments -> safety
  3. generate_cartoon_plan (realistic prompt) -> 1 idea: a VO line + 2 scene/motion shots
  4. Build ONE video:
     a. TTS the voiceover -> size to the flat 8.0s window                (if VO=Yes)
     b. shot images: manual -> as-is; blank -> nano-banana-2 realistic
        (shot 2 chains on shot 1 so a generated frame matches its neighbour)
     c. Seedance: animate each shot image (4s clips)                     (concurrent)
     d. Rendi: concat + overlay VO, optional CTA pill
     e. persist to storage, free Rendi storage, optional ZapCap
  5. Write back ONE Ready Video URL (Ready Video 1).

Graceful degradation mirrors cartoon: a failed later shot reuses the previous
shot's image/clip (a hold); only a row with ZERO usable clips fails. The story is
driven entirely by the article (Yoav 2026-06-22); the motion for a pasted image
is a universal gentle push-in (the planner can't see the photo), while a
generated shot uses the planner's scene-matched motion.

Plan: ``_plans/2026-06-22-simple-motion-tab.md``.
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
    RowResult,
    SimpleMotionRow,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.row_processor_cartoon import (
    IMAGE_RESOLUTION,
    MAX_EFFECTIVE_VO_SECONDS,
    SEEDANCE_DURATION_SHORT,
    SEEDANCE_RESOLUTION,
    SPEECH_ATEMPO_RETRY_MAX,
    TARGET_VIDEO_SECONDS,
    VO_SHORTEN_MIN_WORDS,
    VO_SHORTEN_STEP,
    compute_atempo,
)
from bulkvid.orchestrator.runtime_settings import (
    SETTING_SIMPLE_MOTION_PLANNER_PROMPT,
    SIMPLE_MOTION_PLANNER_PROMPT_DEFAULT,
)
from bulkvid.pipeline.cartoon_cta import render_cartoon_cta_overlay_bytes
from bulkvid.pipeline.cartoon_prompt import (
    CARTOON_TARGET_WORDS,
    REALISTIC_STYLE,
    CartoonIdea,
    generate_cartoon_plan,
    image_prompt_for_shot,
    shorten_voiceover,
)
from bulkvid.pipeline.cta_defaults import default_cta_for_language
from bulkvid.pipeline.language import detect_language, reconcile_language
from bulkvid.pipeline.open_comments import classify_open_comments
from bulkvid.pipeline.safety import resolve_safety

_log = get_logger("row")


# ── Tunables ─────────────────────────────────────────────────────────────────

SM_NUM_IDEAS = 1          # ONE video per row (Ready Video 1)
SM_NUM_SHOTS = 2          # two 4s shots stitched into the 8s video

# Motion prompt for a PASTED (manual) image. The planner can't see the operator's
# photo, so a scene-specific motion would mismatch it — a universal, gentle
# cinematic push-in is the safe default. Generated shots use the planner's own
# scene-matched motion. Plan ``_plans/2026-06-22-simple-motion-tab.md``.
MANUAL_IMAGE_MOTION = (
    "Subtle, natural movement with a slow, gentle cinematic camera push-in."
)


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


async def process_simple_motion_row(
    row: SimpleMotionRow,
    clients: PipelineClients,
    *,
    job_id: str | None = None,
) -> RowResult:
    """Run the simple-motion pipeline for one row. Returns a RowResult. Never raises."""
    set_context(batch_id=job_id, row_num=row.row_num)
    t0 = time.monotonic()
    costs = _Costs()
    slug = _slug(row.row_num, job_id)
    aspect = normalize_aspect_ratio(row.aspect_ratio)
    manual_for_shot = [
        (row.manual_image_1 or "").strip(),
        (row.manual_image_2 or "").strip(),
    ]
    metadata: dict[str, Any] = {
        "row_num": row.row_num,
        "country": row.country,
        "vertical": row.vertical,
        "article_url": row.article_url,
        "aspect_ratio": row.aspect_ratio,
        "voice_over": row.voice_over,
        "zapcap": row.zapcap,
        "tab": "simple_motion",
        "num_ideas": SM_NUM_IDEAS,
        "num_shots": SM_NUM_SHOTS,
        "manual_image_1": bool(manual_for_shot[0]),
        "manual_image_2": bool(manual_for_shot[1]),
    }
    zapcap_failed = False

    _log.info(
        "row_start",
        country=row.country,
        vertical=row.vertical,
        aspect=row.aspect_ratio,
        zapcap=row.zapcap,
        vo=row.voice_over,
        tab="simple_motion",
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
            # Safety net: a wrong/transient scrape can return wrong-language
            # content; prefer the operator's explicit market on conflict.
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

            # Same planner as cartoon, but with the realistic prompt (photographic
            # scenes, not cartoon scenes) and ONE idea. The article tells the
            # story (voiceover); blank shots reuse the scene descriptions.
            plan = await generate_cartoon_plan(
                clients.openai,
                article_body=article_body,
                country=row.country,
                vertical=row.vertical,
                language=lang.language,
                script_pattern=row.script_pattern,
                open_comments=analysis,
                num_ideas=SM_NUM_IDEAS,
                num_shots=SM_NUM_SHOTS,
                settings_store=clients.settings_store,
                safety=safety,
                planner_prompt_key=SETTING_SIMPLE_MOTION_PLANNER_PROMPT,
                planner_prompt_default=SIMPLE_MOTION_PLANNER_PROMPT_DEFAULT,
            )
            costs.plan += plan.cost_usd
            metadata["language"] = lang.language
            metadata["open_comments_mode"] = analysis.mode.value
            if plan.chosen_template_id:
                metadata["chosen_template_id"] = plan.chosen_template_id
        except Exception as e:
            return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)

        # ─── CTA overlay setup (mirrors cartoon) ─────────────────────────
        cta_overlay_url: str | None = None
        cta_text_used: str = ""
        cta_setup_error: str | None = None
        if row.cta_enabled:
            cta_text_used = (row.cta_text.strip() or
                             default_cta_for_language(lang.language))
            try:
                overlay_w, overlay_h = dimensions_for_ratio(row.aspect_ratio)
                overlay_bytes = render_cartoon_cta_overlay_bytes(
                    cta_text_used,
                    canvas_width=overlay_w,
                    canvas_height=overlay_h,
                )
                overlay_upload = await clients.storage.upload_bytes(
                    overlay_bytes,
                    key=f"bulkvid/cta_overlays/{slug}.png",
                    content_type="image/png",
                )
                costs.storage += overlay_upload.cost_usd
                cta_overlay_url = overlay_upload.url
                metadata["cta_enabled"] = True
                metadata["cta_text_used"] = cta_text_used[:80]
            except Exception as e:
                cta_setup_error = str(e)[:200]
                _log.error(
                    "simple_motion_cta_overlay_failed_skipped",
                    error=cta_setup_error, cta_text=cta_text_used[:80],
                )
                metadata["cta_enabled"] = False
                metadata["cta_overlay_error"] = cta_setup_error
        else:
            metadata["cta_enabled"] = False

        # ─── Stage 3+4: build the single video ───
        idea_failure_messages: list[str] = []
        cta_overlay_errors: list[str] = []

        async def _build_idea(idx: int, idea: CartoonIdea) -> str | None:
            """Build one stitched, voiced, optionally-captioned video. None on failure."""
            nonlocal zapcap_failed
            try:
                # 4a. Voiceover (optional). Flat 8.0s video, two 4s clips. If the
                # synthesized VO measures longer than MAX_EFFECTIVE_VO_SECONDS,
                # shorten + re-TTS once; if it still overshoots, drop the idea.
                vo_url: str | None = None
                seedance_durations: list[int] = [SEEDANCE_DURATION_SHORT] * SM_NUM_SHOTS
                per_clip_seconds: list[float] = [float(SEEDANCE_DURATION_SHORT)] * SM_NUM_SHOTS
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
                    vo_atempo, effective = compute_atempo(tts.duration_seconds)
                    original_effective = effective
                    shortened = False

                    if effective > MAX_EFFECTIVE_VO_SECONDS:
                        shorten_target = max(
                            VO_SHORTEN_MIN_WORDS,
                            CARTOON_TARGET_WORDS - VO_SHORTEN_STEP,
                        )
                        _log.warning(
                            "simple_motion_vo_too_long_shortening",
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
                                "simple_motion_vo_shortener_no_change_dropped",
                                idea=idx + 1,
                                original_effective=round(effective, 3),
                            )
                            idea_failure_messages.append(
                                f"idea {idx + 1}: VO shortener returned the "
                                f"original text — couldn't trim "
                                f"{round(effective, 1)}s VO under {MAX_EFFECTIVE_VO_SECONDS}s cap"
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
                        )
                        shortened = True

                        if effective > MAX_EFFECTIVE_VO_SECONDS:
                            _log.error(
                                "simple_motion_vo_too_long_after_retry_dropped",
                                idea=idx + 1,
                                original_effective=round(original_effective, 3),
                                retry_effective=round(effective, 3),
                            )
                            idea_failure_messages.append(
                                f"idea {idx + 1}: VO too long even after "
                                f"shorten+retry ({round(effective, 2)}s vs "
                                f"{MAX_EFFECTIVE_VO_SECONDS}s cap @ "
                                f"{SPEECH_ATEMPO_RETRY_MAX}x max speedup)"
                            )
                            return None

                    vo_up = await clients.storage.upload_bytes(
                        tts.wav_bytes,
                        key=f"bulkvid/vo/{slug}/idea{idx + 1}.wav",
                        content_type="audio/wav",
                    )
                    costs.storage += vo_up.cost_usd
                    vo_url = vo_up.url

                    _log.info(
                        "simple_motion_vo_sized",
                        idea=idx + 1,
                        vo_words=len(final_text.split()),
                        vo_raw_seconds=round(tts.duration_seconds, 3),
                        vo_effective_seconds=round(effective, 3),
                        vo_atempo=round(vo_atempo, 3),
                        vo_dwell_seconds=round(
                            target_video_seconds - effective, 3
                        ),
                        target_video_seconds=target_video_seconds,
                        shortened=shortened,
                        fits=True,
                    )

                # 4b. Shot images. Each shot is EITHER a pasted manual image
                # (used as-is — downloaded + re-uploaded so the URL is stable +
                # Rendi-reachable) OR generated in REALISTIC_STYLE. A generated
                # shot 2 chains on shot 1 (image-to-image) so it matches its
                # neighbour, even when shot 1 is the operator's own photo.
                image_urls: list[str] = []
                image_sources: list[str] = []
                shot_motions: list[str] = []
                for s, shot in enumerate(idea.shots):
                    manual = manual_for_shot[s] if s < len(manual_for_shot) else ""
                    if manual:
                        try:
                            raw = await download_image(manual, timeout=60.0)
                            up = await clients.storage.upload_bytes(
                                raw,
                                key=f"bulkvid/simple_motion_images/{slug}/idea{idx + 1}_shot{s + 1}.png",
                                content_type="image/png",
                            )
                            costs.storage += up.cost_usd
                            image_urls.append(up.url)
                            image_sources.append("manual")
                            shot_motions.append(MANUAL_IMAGE_MOTION)
                            continue
                        except Exception as e:
                            if not image_urls:
                                raise    # first shot must succeed
                            _log.warning(
                                "simple_motion_manual_image_failed_held",
                                idea=idx + 1, shot=s + 1, error=str(e)[:200],
                            )
                            image_urls.append(image_urls[-1])    # hold previous
                            image_sources.append("held")
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
                        image_sources.append("generated")
                        shot_motions.append(shot.motion)
                    except Exception as e:
                        if not image_urls:
                            raise    # first shot must succeed
                        _log.warning(
                            "simple_motion_shot_image_failed_held",
                            idea=idx + 1, shot=s + 1, error=str(e)[:200],
                        )
                        image_urls.append(image_urls[-1])    # hold previous frame
                        image_sources.append("held")
                        shot_motions.append(shot.motion)

                metadata["image_sources"] = image_sources

                # 4c. Animate each image (concurrently). A failed clip holds a
                # neighbour so the concat still has SM_NUM_SHOTS clips in order.
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
                            "simple_motion_shot_animate_failed",
                            idea=idx + 1, shot=s + 1, error=str(e)[:200],
                        )
                        return s, None

                animated = await asyncio.gather(
                    *[_animate(s, u) for s, u in enumerate(image_urls)]
                )
                clip_by_shot = {s: url for s, url in animated}
                # Fill gaps with the nearest successful clip: forward pass holds
                # the previous good clip, then a backward pass covers leading gaps.
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
                    _log.error("simple_motion_idea_no_clips", idea=idx + 1)
                    idea_failure_messages.append(
                        f"idea {idx + 1}: no Seedance clips produced "
                        f"for any of {len(image_urls)} shots"
                    )
                    return None

                # 4d. Stitch + overlay VO. Uniform [4.0, 4.0] clips trimmed into
                # the flat 8.0s ceiling; the VO that reaches here is
                # <= MAX_EFFECTIVE_VO_SECONDS, so the concat never truncates audio.
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

                # 4d.5. Optional CTA overlay (mirrors cartoon). NON-FATAL — an
                # overlay failure ships the video WITHOUT the pill.
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
                        err_msg = str(cta_err)[:300]
                        cta_overlay_errors.append(f"idea {idx + 1}: {err_msg}")
                        _log.error(
                            "simple_motion_cta_overlay_failed_kept_original",
                            idea=idx + 1, error=err_msg,
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

                # 4f. Optional ZapCap. When the CTA pill is on, push the caption
                # higher (top=30) so it doesn't cover the pill. On failure keep
                # the uncaptioned video.
                if row.zapcap and clients.zapcap is not None:
                    try:
                        zapcap_opts: ZapCapRenderOptions | None = None
                        if cta_overlay_url:
                            zapcap_opts = ZapCapRenderOptions(
                                subs=ZapCapSubsOptions(),
                                style=ZapCapStyleOptions(top=30, font_size=36),
                            )
                        cap_url, cost = await clients.zapcap.caption_video(
                            video_bytes=data,
                            language=lang.language,
                            filename=f"v{idx + 1}.mp4",
                            render_options=zapcap_opts,
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
                            "simple_motion_zapcap_failed_kept_original",
                            idea=idx + 1, error=str(e)[:200],
                        )

                return final_url
            except Exception as e:
                err_msg = str(e)[:300]
                idea_failure_messages.append(f"idea {idx + 1}: {err_msg}")
                _log.error("simple_motion_idea_failed", idea=idx + 1, error=err_msg)
                return None

        results = await asyncio.gather(
            *[_build_idea(i, idea) for i, idea in enumerate(plan.ideas)]
        )
        video_urls = [u for u in results if u]

        if cta_overlay_errors:
            metadata["cta_overlay_errors"] = cta_overlay_errors
            metadata["cta_overlay_applied"] = False

        if not video_urls:
            detail = (
                " | ".join(idea_failure_messages)
                if idea_failure_messages
                else "ideas returned None without raising"
            )
            return _fail(
                row, STATUS_VIDEO_ASSEMBLY_FAILED,
                f"no usable videos produced — {detail}",
                t0, costs, metadata,
            )

        metadata["videos_produced"] = len(video_urls)

        cta_warning_parts: list[str] = []
        if cta_setup_error:
            cta_warning_parts.append(
                f"CTA overlay skipped for all videos — setup failed: {cta_setup_error}"
            )
        if cta_overlay_errors:
            cta_warning_parts.append(
                "CTA overlay failed on " + "; ".join(cta_overlay_errors)
            )
        cta_warning = " | ".join(cta_warning_parts)[:1000] or None

        if zapcap_failed:
            metadata["zapcap_applied"] = False
            return _ok(
                row, video_urls, t0, costs, metadata,
                status=STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS,
                warning=cta_warning,
            )
        if row.zapcap and clients.zapcap is not None:
            metadata["zapcap_applied"] = True
        return _ok(row, video_urls, t0, costs, metadata, warning=cta_warning)

    except Exception as e:
        _log.exception("row_internal_error", error=str(e))
        return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)


# ── Result builders ──────────────────────────────────────────────────────────


def _ok(
    row: SimpleMotionRow,
    video_urls: list[str],
    t0: float,
    costs: _Costs,
    metadata: dict[str, Any],
    *,
    status: str = STATUS_SUCCESS,
    warning: str | None = None,
) -> RowResult:
    elapsed = round(time.monotonic() - t0, 3)
    metadata["cost_breakdown"] = costs.__dict__.copy()
    _log.info(
        "row_done",
        status=status,
        cost_usd=costs.total,
        elapsed_seconds=elapsed,
        video_count=len(video_urls),
        warning=warning,
    )
    return RowResult(
        row_num=row.row_num,
        status=status,
        video_urls=video_urls,
        cost_usd=costs.total,
        elapsed_seconds=elapsed,
        error=warning,
        metadata=metadata,
    )


def _fail(
    row: SimpleMotionRow,
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
