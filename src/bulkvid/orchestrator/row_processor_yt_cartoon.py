"""yt-cartoon row processor — engaging, variable-length cartoon videos.

A variable-geometry sibling of ``row_processor_cartoon``. The cartoon tab is a
fixed-geometry machine (flat 8.0s, 2 shots, calm narration, hardcoded). The
yt-cartoon tab makes length, tone, caption height, and CTA height per-row knobs.

To keep the existing cartoon path byte-identical and regression-proof, this
module does NOT touch ``process_cartoon_row``. Instead it REUSES the shared,
already-pure helpers — the planner (``generate_cartoon_plan``, now parameterised
by prompt + word budget), the atempo sizer (``compute_atempo`` with a per-row
``max_effective``), the shortener, the CTA renderer, ZapCap, and the Rendi
concat — and drives them from a single ``ShotPlan`` derived from the row's
``Vid Length`` cell.

Per row:
  * resolve ``ShotPlan`` from Vid Length (10s -> 2 videos x 3 shots; 15s -> 1 x 4;
    20s -> 1 x 5; all clips generated at Seedance's cheap 4s tier and trimmed +
    concatenated to the exact target).
  * resolve Tone → engaging (default) or calm planner prompt.
  * resolve Cap/CTA Position nudges → ZapCap ``top`` offset + CTA bottom-margin.
  * build each video concurrently (TTS → nano-banana shots → Seedance → Rendi
    concat → optional CTA overlay → persist → optional ZapCap), reusing the
    cartoon graceful-degradation patterns (hold a frame on a failed shot;
    shorten-and-retry a too-long VO; keep the uncaptioned video on ZapCap fail).

Plan: ``_plans/2026-06-17-yt-cartoon-tab.md``.
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
    YtCartoonRow,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.row_processor_cartoon import (
    IMAGE_RESOLUTION,
    SEEDANCE_RESOLUTION,
    SPEECH_ATEMPO_RETRY_MAX,
    VO_SHORTEN_MIN_WORDS,
    VO_SHORTEN_STEP,
    compute_atempo,
)
from bulkvid.orchestrator.runtime_settings import (
    SETTING_YT_CARTOON_ENGAGING_PROMPT,
    YT_CARTOON_ENGAGING_PROMPT_DEFAULT,
)
from bulkvid.pipeline.cartoon_cta import (
    PILL_BOTTOM_MARGIN_FRAC,
    render_cartoon_cta_overlay_bytes,
)
from bulkvid.pipeline.cartoon_prompt import (
    CartoonIdea,
    generate_cartoon_plan,
    image_prompt_for_shot,
    shorten_voiceover,
)
from bulkvid.pipeline.cta_defaults import default_cta_for_language
from bulkvid.pipeline.language import detect_language, reconcile_language
from bulkvid.pipeline.open_comments import classify_open_comments
from bulkvid.pipeline.safety import resolve_safety
from bulkvid.pipeline.yt_cartoon import (
    TONE_CALM,
    ShotPlan,
    fit_video_to_vo,
    normalize_tone,
    plan_shots_for_length,
    resolve_cap_top,
    resolve_cta_margin,
)

_log = get_logger("row")


# ── ZapCap base positions (mirrors cartoon) ──────────────────────────────────
# Default caption band; the "Cap Position" nudge offsets these. When a CTA pill
# is present we start higher (top=30) so captions don't cover the pill, exactly
# like the cartoon tab.
ZAPCAP_BASE_TOP_DEFAULT = 70
ZAPCAP_BASE_TOP_WITH_CTA = 30
ZAPCAP_FONT_SIZE_WITH_CTA = 36


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


async def process_yt_cartoon_row(
    row: YtCartoonRow,
    clients: PipelineClients,
    *,
    job_id: str | None = None,
) -> RowResult:
    """Run the yt-cartoon pipeline for one row. Returns a RowResult. Never raises."""
    set_context(batch_id=job_id, row_num=row.row_num)
    t0 = time.monotonic()
    costs = _Costs()
    slug = _slug(row.row_num, job_id)
    aspect = normalize_aspect_ratio(row.aspect_ratio)

    # Resolve the three new knobs up front (all pure, all defensive).
    shot_plan: ShotPlan = plan_shots_for_length(row.vid_length)
    tone = normalize_tone(row.tone)
    cta_margin_frac = resolve_cta_margin(PILL_BOTTOM_MARGIN_FRAC, row.cta_position)

    metadata: dict[str, Any] = {
        "row_num": row.row_num,
        "country": row.country,
        "vertical": row.vertical,
        "article_url": row.article_url,
        "aspect_ratio": row.aspect_ratio,
        "voice_over": row.voice_over,
        "zapcap": row.zapcap,
        "tab": "yt_cartoon",
        "tone": tone,
        "vid_length_bucket": shot_plan.bucket_seconds,
        "num_videos": shot_plan.num_videos,
        "num_shots": shot_plan.num_shots,
        "cap_position": row.cap_position,
        "cta_position": row.cta_position,
    }
    zapcap_failed = False

    _log.info(
        "row_start",
        country=row.country,
        vertical=row.vertical,
        aspect=row.aspect_ratio,
        zapcap=row.zapcap,
        vo=row.voice_over,
        tab="yt_cartoon",
        tone=tone,
        vid_length=shot_plan.bucket_seconds,
        num_videos=shot_plan.num_videos,
        num_shots=shot_plan.num_shots,
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

            # Tone selects the planner prompt; the calm tone falls through to the
            # cartoon defaults (generate_cartoon_plan's own defaults). The word
            # budget always scales with Vid Length, regardless of tone.
            plan_kwargs: dict[str, Any] = {
                "target_words": shot_plan.target_words,
                "min_words": shot_plan.min_words,
                "max_words": shot_plan.max_words,
            }
            if tone != TONE_CALM:
                plan_kwargs["planner_prompt_key"] = SETTING_YT_CARTOON_ENGAGING_PROMPT
                plan_kwargs["planner_prompt_default"] = (
                    YT_CARTOON_ENGAGING_PROMPT_DEFAULT
                )

            plan = await generate_cartoon_plan(
                clients.openai,
                article_body=article_body,
                country=row.country,
                vertical=row.vertical,
                language=lang.language,
                script_pattern=row.script_pattern,
                open_comments=analysis,
                num_ideas=shot_plan.num_videos,
                num_shots=shot_plan.num_shots,
                settings_store=clients.settings_store,
                safety=safety,
                **plan_kwargs,
            )
            costs.plan += plan.cost_usd
            metadata["language"] = lang.language
            metadata["open_comments_mode"] = analysis.mode.value
            if plan.chosen_template_id:
                metadata["chosen_template_id"] = plan.chosen_template_id
        except Exception as e:
            return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)

        # ─── CTA overlay setup (mirrors cartoon, + CTA Position nudge) ───
        cta_overlay_url: str | None = None
        cta_text_used: str = ""
        cta_setup_error: str | None = None
        if row.cta_enabled:
            cta_text_used = (row.cta_text.strip() or
                             default_cta_for_language(lang.language))
            try:
                overlay_w, overlay_h = dimensions_for_ratio(aspect)
                overlay_bytes = render_cartoon_cta_overlay_bytes(
                    cta_text_used,
                    canvas_width=overlay_w,
                    canvas_height=overlay_h,
                    bottom_margin_frac=cta_margin_frac,
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
                metadata["cta_margin_frac"] = round(cta_margin_frac, 3)
            except Exception as e:
                cta_setup_error = str(e)[:200]
                _log.error(
                    "yt_cartoon_cta_overlay_failed_skipped",
                    error=cta_setup_error, cta_text=cta_text_used[:80],
                )
                metadata["cta_enabled"] = False
                metadata["cta_overlay_error"] = cta_setup_error
        else:
            metadata["cta_enabled"] = False

        # ZapCap caption band: base higher when a CTA pill is present, then apply
        # the operator's Cap Position nudge (clamped). Built once per row.
        base_top = ZAPCAP_BASE_TOP_WITH_CTA if cta_overlay_url else ZAPCAP_BASE_TOP_DEFAULT
        zapcap_top = resolve_cap_top(base_top, row.cap_position)
        metadata["zapcap_top"] = zapcap_top

        # ─── Build each video (concurrently) ───
        idea_failure_messages: list[str] = []
        cta_overlay_errors: list[str] = []

        async def _build_idea(idx: int, idea: CartoonIdea) -> str | None:
            """Build one stitched, voiced, optionally-captioned video. None on failure."""
            nonlocal zapcap_failed
            try:
                num_shots = shot_plan.num_shots
                seedance_durations: list[int] = list(shot_plan.seedance_durations)
                per_clip_seconds: list[float] = list(shot_plan.per_clip_seconds)
                target_video_seconds = shot_plan.target_seconds
                max_effective = shot_plan.max_effective_vo
                vo_url: str | None = None
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
                        tts.duration_seconds, max_effective=max_effective
                    )
                    original_effective = effective
                    shortened = False

                    if effective > max_effective:
                        shorten_target = max(
                            VO_SHORTEN_MIN_WORDS,
                            shot_plan.target_words - VO_SHORTEN_STEP,
                        )
                        _log.warning(
                            "yt_cartoon_vo_too_long_shortening",
                            idea=idx + 1,
                            original_words=len(final_text.split()),
                            original_effective=round(effective, 3),
                            shorten_target_words=shorten_target,
                            max_effective=max_effective,
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
                                "yt_cartoon_vo_shortener_no_change_dropped",
                                idea=idx + 1,
                                original_effective=round(effective, 3),
                            )
                            idea_failure_messages.append(
                                f"idea {idx + 1}: VO shortener returned the "
                                f"original text — couldn't trim "
                                f"{round(effective, 1)}s VO under {max_effective}s cap"
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
                            max_effective=max_effective,
                        )
                        shortened = True

                        if effective > max_effective:
                            _log.error(
                                "yt_cartoon_vo_too_long_after_retry_dropped",
                                idea=idx + 1,
                                original_effective=round(original_effective, 3),
                                retry_effective=round(effective, 3),
                            )
                            idea_failure_messages.append(
                                f"idea {idx + 1}: VO too long even after "
                                f"shorten+retry ({round(effective, 2)}s vs "
                                f"{max_effective}s cap @ "
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

                    # Shrink the video to the narration so a fast/short VO
                    # doesn't leave dead air (Yoav 2026-06-17). Capped at the
                    # bucket, floored so shots can breathe; per-clip trims
                    # redistributed so every shot still appears.
                    target_video_seconds, per_clip_seconds = fit_video_to_vo(
                        effective, shot_plan, has_vo=True
                    )

                    _log.info(
                        "yt_cartoon_vo_sized",
                        idea=idx + 1,
                        vo_words=len(final_text.split()),
                        vo_raw_seconds=round(tts.duration_seconds, 3),
                        vo_effective_seconds=round(effective, 3),
                        vo_atempo=round(vo_atempo, 3),
                        target_video_seconds=round(target_video_seconds, 3),
                        vo_dwell_seconds=round(target_video_seconds - effective, 3),
                        bucket_seconds=shot_plan.target_seconds,
                        max_effective=max_effective,
                        per_clip_seconds=[round(p, 3) for p in per_clip_seconds],
                        seedance_durations=list(seedance_durations),
                        shortened=shortened,
                        fits=True,
                    )

                # Scene images — shot 1 from text, shots 2+ chained on shot 1.
                image_urls: list[str] = []
                for s in range(num_shots):
                    shot = idea.shots[s]
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
                            "yt_cartoon_shot_image_failed_held",
                            idea=idx + 1, shot=s + 1, error=str(e)[:200],
                        )
                        image_urls.append(image_urls[-1])    # hold previous frame

                # Animate each image (concurrently). A failed clip holds a
                # neighbour so the concat still has num_shots clips in order.
                async def _animate(s: int, image_url: str) -> tuple[int, str | None]:
                    try:
                        clip_url, cost = await seedance_image_to_video(
                            clients.kie, image_url, idea.shots[s].motion, aspect,
                            duration=seedance_durations[s], resolution=SEEDANCE_RESOLUTION,
                        )
                        costs.seedance += cost
                        return s, clip_url
                    except Exception as e:
                        _log.warning(
                            "yt_cartoon_shot_animate_failed",
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
                    _log.error("yt_cartoon_idea_no_clips", idea=idx + 1)
                    idea_failure_messages.append(
                        f"idea {idx + 1}: no Seedance clips produced "
                        f"for any of {len(image_urls)} shots"
                    )
                    return None

                # Stitch + overlay VO. per_clip_seconds sums to the bucket target;
                # total_video_seconds forces the exact length. By construction the
                # VO that reaches here is <= max_effective, so no audio truncation.
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

                # Optional CTA overlay (non-fatal — keep the original on failure).
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
                            "yt_cartoon_cta_overlay_failed_kept_original",
                            idea=idx + 1, error=err_msg,
                        )

                # Persist to our storage, then free the Rendi copies.
                data = await download_image(video_url_for_persist, timeout=180.0)
                up = await clients.storage.upload_bytes(
                    data,
                    key=f"bulkvid/videos/{slug}/v{idx + 1}.mp4",
                    content_type="video/mp4",
                )
                costs.storage += up.cost_usd
                final_url = up.url
                await clients.rendi.cleanup_commands(cleanup_command_ids)

                # Optional ZapCap. Caption band already nudged by Cap Position.
                # On failure keep the uncaptioned video.
                if row.zapcap and clients.zapcap is not None:
                    try:
                        style = ZapCapStyleOptions(top=zapcap_top)
                        if cta_overlay_url:
                            style = ZapCapStyleOptions(
                                top=zapcap_top, font_size=ZAPCAP_FONT_SIZE_WITH_CTA
                            )
                        zapcap_opts = ZapCapRenderOptions(
                            subs=ZapCapSubsOptions(), style=style
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
                            "yt_cartoon_zapcap_failed_kept_original",
                            idea=idx + 1, error=str(e)[:200],
                        )

                return final_url
            except Exception as e:
                err_msg = str(e)[:300]
                idea_failure_messages.append(f"idea {idx + 1}: {err_msg}")
                _log.error("yt_cartoon_idea_failed", idea=idx + 1, error=err_msg)
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
                f"no usable videos produced for any idea — {detail}",
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
    row: YtCartoonRow,
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
    row: YtCartoonRow,
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
