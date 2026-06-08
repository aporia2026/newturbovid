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
from bulkvid.pipeline.cartoon_cta import render_cartoon_cta_overlay_bytes
from bulkvid.pipeline.cartoon_prompt import (
    CARTOON_TARGET_WORDS,
    CartoonIdea,
    generate_cartoon_plan,
    image_prompt_for_shot,
    shorten_voiceover,
)
from bulkvid.pipeline.cta_defaults import default_cta_for_language
from bulkvid.pipeline.language import detect_language
from bulkvid.pipeline.open_comments import classify_open_comments
from bulkvid.pipeline.safety import resolve_safety

_log = get_logger("row")


# ── Tunables (admin-surfaced in Phase 5) ─────────────────────────────────────

CARTOON_NUM_IDEAS = 2          # videos per row (Ready Video 1 + 2)
CARTOON_NUM_SHOTS = 2          # shots stitched per video
SEEDANCE_DURATION_SHORT = 4    # seconds per Seedance clip (4/8/12 only)
SEEDANCE_RESOLUTION = "720p"
IMAGE_RESOLUTION = "1K"        # nano-banana-2 resolution (animated -> 720p)

# Cartoon videos are a **flat 8.0s every time**. Two 4s Seedance clips concat
# to 8s of footage; the VO overlays the first ~6-7s of that with VO_TAIL
# silence dwell to the end. If the synthesized VO measures effectively longer
# than MAX_EFFECTIVE_VO_SECONDS, the row processor calls shorten_voiceover()
# and re-TTSes ONCE; if it still doesn't fit, the idea is dropped and the
# OTHER idea ships. The audio is never truncated mid-word.
# See _plans/2026-06-04-cartoon-8s-hard-cap.md.
TARGET_VIDEO_SECONDS = 8.0
VO_TAIL_SECONDS = 0.5                                  # silence dwell after VO
MAX_EFFECTIVE_VO_SECONDS = TARGET_VIDEO_SECONDS - VO_TAIL_SECONDS    # 7.5s

# When shortening a too-long VO, target this many fewer words than the
# planner's normal target — and never go below VO_SHORTEN_MIN_WORDS (a 6-word
# line is roughly 4s at slow delivery, leaving healthy margin under the cap).
VO_SHORTEN_MIN_WORDS = 6
VO_SHORTEN_STEP = 3

# Per-row playback speed for the VO. The default ``SPEECH_ATEMPO`` (1.3) was
# tuned for "Gemini TTS reads too slowly" — but fast TTS deliveries (short
# lines, energetic style) come out 3–5s raw and don't need to be sped up.
# ``compute_atempo`` picks the smallest atempo (>=1.0) that still lets the
# raw audio fit inside ``MAX_EFFECTIVE_VO_SECONDS``, capped at ``SPEECH_ATEMPO``
# to protect voice quality. Net: short VOs play at natural speed and fill more
# of the 8s window; long VOs still get sped up just enough to fit.
SPEECH_ATEMPO_MIN = 1.0

# Retry-only ceiling. The first attempt protects voice quality with the
# 1.3x default, but if the shortener trimmed the line and the re-TTS still
# overshoots by a small margin (Spanish/German compounds blow up character
# counts) it's far better to nudge the speed to 1.5x than to drop the idea
# entirely. 1.5x stays inside Gemini TTS's "natural-sounding" range; past
# 1.6x the voice starts to feel rushed. See job-1780933855-3c614650 where
# three of four idea-1s dropped on margins of 78ms / 1.25s / 2.3s.
SPEECH_ATEMPO_RETRY_MAX = 1.5


def compute_atempo(
    raw_seconds: float, *, max_atempo: float = SPEECH_ATEMPO,
) -> tuple[float, float]:
    """Pick a per-row atempo for the VO.

    Returns ``(atempo, effective_seconds)`` where ``effective_seconds`` is the
    actual played length of the VO after ffmpeg applies the ``atempo`` filter.

    ``max_atempo`` (default ``SPEECH_ATEMPO`` = 1.3) is the ceiling on speedup.
    Pass ``SPEECH_ATEMPO_RETRY_MAX`` on the shorten-and-retry path to rescue
    borderline overshoots instead of dropping the idea.

    Logic:
      - Raw audio ≤ MAX_EFFECTIVE_VO_SECONDS (7.5s): no speedup needed, atempo=1.0.
        The VO plays at natural pace, which sounds better and fills more of the
        8s video — directly addresses the "v2-style" 3-second trailing-silence
        bug.
      - Raw audio > 7.5s but ≤ 7.5 × max_atempo: speed up just enough to fit.
        ``atempo = raw / 7.5``; effective lands at exactly 7.5s.
      - Raw audio > 7.5 × max_atempo: even max speedup doesn't fit. Return
        ``(max_atempo, raw/max_atempo)`` — the caller will see
        ``effective > MAX_EFFECTIVE_VO_SECONDS`` and trigger the
        shorten-and-retry path (or drop, if this already WAS the retry).

    Floating-point note: in the "speed up just enough to fit" branch we
    return the cap value as a literal, NOT ``raw_seconds / atempo``. By
    construction the math says effective == cap, but IEEE 754 division can
    drift a few ULPs and the caller's ``effective > cap`` check then false-
    positives on FP noise (see job ``local-desktop-l6i1bf7-20260604T103814Z``
    for the regression that prompted this comment — raw 8.17s came back as
    7.5000000001 and dropped a clean idea).
    """
    if raw_seconds <= 0:
        return max_atempo, 0.0
    if raw_seconds <= MAX_EFFECTIVE_VO_SECONDS:
        return SPEECH_ATEMPO_MIN, raw_seconds
    needed = raw_seconds / MAX_EFFECTIVE_VO_SECONDS
    if needed <= max_atempo:
        # Audio fits with some speedup; effective lands exactly at the cap.
        # Return the cap as a literal to avoid FP drift past it.
        return needed, MAX_EFFECTIVE_VO_SECONDS
    # Audio doesn't fit even at max speedup; caller will see effective > cap
    # and trigger shorten-and-retry. We DO return the math here (not the cap)
    # because the value carries meaningful "how badly does this overshoot"
    # information for the log line.
    return max_atempo, raw_seconds / max_atempo


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

            safety = await resolve_safety(
                clients.settings_store, row.vertical, row.row_num
            )
            metadata["safety_matched"] = safety.matched
            metadata["safety_keyword"] = safety.matched_keyword

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
                settings_store=clients.settings_store,
                safety=safety,
            )
            costs.plan += plan.cost_usd
            metadata["language"] = lang.language
            metadata["open_comments_mode"] = analysis.mode.value
            if plan.chosen_template_id:
                metadata["chosen_template_id"] = plan.chosen_template_id
        except Exception as e:
            return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)

        # ─── CTA overlay setup (Yoav 2026-06-08) ─────────────────────────
        # When the row's CTA column is set to Yes, every generated cartoon
        # video gets a yellow CTA pill overlaid at the bottom. CTA text:
        #   * operator's CTA Text column if non-empty
        #   * else per-language "Read More" fallback (matches simple_x4 default)
        # The overlay PNG is rendered ONCE per row (same text on all ideas),
        # uploaded to storage, then composited onto each stitched cartoon
        # video via Rendi. When ``CTA = No``, no overlay step runs and the
        # row behaves exactly like the legacy cartoon path.
        cta_overlay_url: str | None = None
        cta_text_used: str = ""
        cta_setup_error: str | None = None        # surfaced into row.error
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
                # Overlay render/upload failure: ship videos WITHOUT the CTA
                # rather than killing the whole row. Loud log AND surface to
                # row.error so the operator sees it in the sidebar without
                # digging through HF Space logs.
                cta_setup_error = str(e)[:200]
                _log.error(
                    "cartoon_cta_overlay_failed_skipped",
                    error=cta_setup_error, cta_text=cta_text_used[:80],
                )
                metadata["cta_enabled"] = False
                metadata["cta_overlay_error"] = cta_setup_error
        else:
            metadata["cta_enabled"] = False

        # ─── Stage 3+4: build each idea into a finished video (concurrently) ───

        # Per-idea exception messages — collected so a row that fails
        # entirely surfaces the ACTUAL ffmpeg/kie/seedance error in its
        # error field (operators can see it in the sidebar) instead of
        # the unhelpful generic "no usable videos produced for any idea".
        # Also captures the CTA-overlay failures (non-fatal, video ships
        # without the pill) so we can diagnose those without HF Logs.
        idea_failure_messages: list[str] = []
        cta_overlay_errors: list[str] = []

        async def _build_idea(idx: int, idea: CartoonIdea) -> str | None:
            """Build one stitched, voiced, optionally-captioned video. None on failure."""
            nonlocal zapcap_failed
            try:
                # 4a. Voiceover (optional). Hard-cap design: video is ALWAYS
                # TARGET_VIDEO_SECONDS (8.0s), two equal 4s Seedance clips. If
                # the synthesized VO measures effectively longer than
                # MAX_EFFECTIVE_VO_SECONDS, shorten the line and re-TTS ONCE;
                # if that still doesn't fit, drop the idea so the OTHER idea
                # can still ship. The audio is never truncated mid-word.
                vo_url: str | None = None
                seedance_durations: list[int] = [SEEDANCE_DURATION_SHORT] * CARTOON_NUM_SHOTS
                per_clip_seconds: list[float] = [float(SEEDANCE_DURATION_SHORT)] * CARTOON_NUM_SHOTS
                target_video_seconds = TARGET_VIDEO_SECONDS
                # Per-row playback speed for the VO. SPEECH_ATEMPO is the
                # default for the silent path (no audio); when there's a VO
                # we replace it with compute_atempo's per-row pick.
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
                    # Pick the lowest atempo (>=1.0) that still lets the raw
                    # audio fit in the 7.5s effective window. Short VOs play
                    # at natural speed and fill more of the 8s video; long
                    # VOs get just enough speedup to fit.
                    vo_atempo, effective = compute_atempo(tts.duration_seconds)
                    original_effective = effective
                    shortened = False

                    if effective > MAX_EFFECTIVE_VO_SECONDS:
                        shorten_target = max(
                            VO_SHORTEN_MIN_WORDS,
                            CARTOON_TARGET_WORDS - VO_SHORTEN_STEP,
                        )
                        _log.warning(
                            "cartoon_vo_too_long_shortening",
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

                        # The shortener's defensive fallbacks (bad JSON, empty
                        # response, not-actually-shorter) return the original
                        # text. There's no point re-TTSing the same string —
                        # drop the idea now.
                        if shorten_result.voiceover == final_text:
                            _log.error(
                                "cartoon_vo_shortener_no_change_dropped",
                                idea=idx + 1,
                                original_effective=round(effective, 3),
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
                        # Allow up to 1.5x on the retry to rescue borderline
                        # overshoots that the default 1.3x cap would drop.
                        # See SPEECH_ATEMPO_RETRY_MAX docstring for context.
                        vo_atempo, effective = compute_atempo(
                            tts.duration_seconds,
                            max_atempo=SPEECH_ATEMPO_RETRY_MAX,
                        )
                        shortened = True

                        if effective > MAX_EFFECTIVE_VO_SECONDS:
                            # Shortened TTS still overshoots. Don't truncate
                            # the audio; drop this idea so the OTHER idea
                            # ships clean. Row only fails if BOTH ideas drop.
                            _log.error(
                                "cartoon_vo_too_long_after_retry_dropped",
                                idea=idx + 1,
                                original_effective=round(original_effective, 3),
                                retry_effective=round(effective, 3),
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
                        "cartoon_vo_sized",
                        idea=idx + 1,
                        vo_words=len(final_text.split()),
                        vo_raw_seconds=round(tts.duration_seconds, 3),
                        vo_effective_seconds=round(effective, 3),
                        vo_atempo=round(vo_atempo, 3),
                        vo_dwell_seconds=round(
                            target_video_seconds - effective, 3
                        ),
                        target_video_seconds=target_video_seconds,
                        per_clip_seconds=[round(p, 3) for p in per_clip_seconds],
                        seedance_durations=list(seedance_durations),
                        shortened=shortened,
                        fits=True,
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
                # ``seedance_durations[s]`` is 4s for every shot except the last
                # when long_audio is True — that last shot gets the 8s tier so
                # the concat has room to fit the full VO + dwell.
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

                # 4d. Stitch + overlay VO. ``per_clip_seconds`` is uniform
                # [4.0, 4.0] and ``total_video_seconds`` is the flat 8.0s
                # ceiling — see the constants. By construction the VO that
                # reaches this step is <= MAX_EFFECTIVE_VO_SECONDS, so the
                # concat never truncates audio.
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

                # 4d.5. Optional CTA overlay (Yoav 2026-06-08). When the
                # row's CTA column is Yes, composite the pre-rendered yellow
                # pill PNG onto the stitched video via Rendi ffmpeg overlay.
                # The PNG was rendered + uploaded once per row at the top of
                # process_cartoon_row; reused across both ideas.
                #
                # NON-FATAL: an overlay failure falls back to the stitched
                # video WITHOUT the CTA pill. Loud log so the operator can
                # spot it. Matches the ZapCap-keep-original pattern below —
                # one new step never tanks an otherwise-good row.
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
                            "cartoon_cta_overlay_failed_kept_original",
                            idea=idx + 1, error=err_msg,
                        )

                # 4e. Persist to our storage, then free the Rendi copies.
                data = await _download(video_url_for_persist, timeout=180.0)
                up = await clients.storage.upload_bytes(
                    data,
                    key=f"bulkvid/videos/{slug}/v{idx + 1}.mp4",
                    content_type="video/mp4",
                )
                costs.storage += up.cost_usd
                final_url = up.url
                await clients.rendi.cleanup_commands(cleanup_command_ids)

                # 4f. Optional ZapCap. When the CTA pill is on, push the
                # caption position higher (top=30) so it doesn't cover the
                # pill at the bottom; matches the simple_x4 templated-cell
                # behavior. On failure keep the uncaptioned video.
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
                err_msg = str(e)[:300]
                idea_failure_messages.append(f"idea {idx + 1}: {err_msg}")
                _log.error("cartoon_idea_failed", idea=idx + 1, error=err_msg)
                return None

        results = await asyncio.gather(
            *[_build_idea(i, idea) for i, idea in enumerate(plan.ideas)]
        )
        video_urls = [u for u in results if u]

        # Surface CTA-overlay failures even when the row itself succeeded —
        # without this, a row that shipped videos but lost the CTA pill is
        # indistinguishable from a row that intentionally ran with CTA off.
        if cta_overlay_errors:
            metadata["cta_overlay_errors"] = cta_overlay_errors
            metadata["cta_overlay_applied"] = False

        if not video_urls:
            # Detailed failure message: surfaces the ACTUAL per-idea errors
            # so the operator can debug from the sidebar (no need to dig
            # through HF Space logs for the ffmpeg/kie/seedance stderr).
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

        # Build a sidebar-visible CTA warning when the row otherwise
        # succeeded but the operator's CTA pill didn't actually land on
        # one or more videos. Without this, a row that intentionally ran
        # with CTA off and a row that ran with CTA on but quietly lost
        # the pill look identical from the sidebar. Truncated at 1000
        # chars to match the RowResult.error budget (see _fail()).
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
    row: CartoonRow,
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
