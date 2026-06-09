"""Avatar row processor — 8 s video with two AI scenes + TikTok avatar narration.

Pipeline (mirrors cartoon, with the TikTok avatar step replacing Gemini TTS):

  1. Article fetch (Tavily → ScrapingBee)
  2. language detect → classify Open Comments
  3. generate_cartoon_plan with 1 idea × 2 shots — reuses the cartoon
     planner (it already enforces brand-safety + character consistency).
  4. In parallel:
     a. Shot 1 image — image-to-image if ``manual_image_url`` set, else
        text-to-image from the plan.
     b. Shot 2 image — image-to-image chained on Shot 1 (consistent
        character look) regardless of whether Shot 1 used a manual seed.
     c. TikTok Symphony avatar — POST script + avatar_id, poll until SUCCESS.
  5. Seedance animates each image to a 4 s clip (parallel).
  6. Rendi concat the 2 clips with NO audio → 8 s background.
  7. Rendi composite the avatar video at bottom-left (~30 % width), using
     the avatar's audio (overlay drives output).
  8. Persist video; free Rendi storage; optional ZapCap.

Graceful degradation:
  * Avatar API failure → row fails (no narration = no avatar tab output).
  * One shot image fails → hold the previous frame, video still ships.
  * CTA pill render/upload failure → ship without CTA (non-fatal).

Plan: ``_plans/2026-06-09-video-with-avatar-tab.md``.
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
    dimensions_for_ratio,
    normalize_aspect_ratio,
)
from bulkvid.adapters.tiktok_avatar import (
    TikTokAvatarClient,
    TikTokAvatarError,
)
from bulkvid.adapters.zapcap import (
    ZapCapRenderOptions,
    ZapCapStyleOptions,
    ZapCapSubsOptions,
)
from bulkvid.logging import get_logger, set_context
from bulkvid.models.row import (
    STATUS_ARTICLE_FETCH_FAILED,
    STATUS_IMAGE_GEN_FAILED,
    STATUS_INTERNAL_ERROR,
    STATUS_SUCCESS,
    STATUS_TTS_FAILED,
    STATUS_VIDEO_ASSEMBLY_FAILED,
    STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS,
    AvatarRow,
    RowResult,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.pipeline.cartoon_cta import render_cartoon_cta_overlay_bytes
from bulkvid.pipeline.cartoon_prompt import (
    generate_cartoon_plan,
    image_prompt_for_shot,
)
from bulkvid.pipeline.cta_defaults import default_cta_for_language
from bulkvid.pipeline.language import detect_language
from bulkvid.pipeline.open_comments import classify_open_comments
from bulkvid.pipeline.safety import resolve_safety

_log = get_logger("row")


# ── Tunables ────────────────────────────────────────────────────────────────

AVATAR_NUM_SHOTS = 2                # 2 background scenes
SEEDANCE_DURATION_SHORT = 4         # 4 s per Seedance clip → 8 s total
SEEDANCE_RESOLUTION = "720p"
IMAGE_RESOLUTION = "1K"

# Avatar overlay geometry — bottom-left, ~30 % canvas width.
AVATAR_OVERLAY_WIDTH_FRAC = 0.30
AVATAR_OVERLAY_MARGIN_PX = 40


def _is_valid_http_url(url: str) -> bool:
    return isinstance(url, str) and url.strip().startswith(("http://", "https://"))


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
    seedance: float = 0.0
    tiktok: float = 0.0     # operator pays TikTok directly; tracked as 0 here
    rendi: float = 0.0
    zapcap: float = 0.0
    storage: float = 0.0

    @property
    def total(self) -> float:
        return round(
            self.article + self.language + self.classify + self.plan
            + self.image_gen + self.seedance + self.tiktok + self.rendi
            + self.zapcap + self.storage,
            6,
        )


async def process_avatar_row(
    row: AvatarRow,
    clients: PipelineClients,
    *,
    job_id: str | None = None,
) -> RowResult:
    """Run the avatar pipeline for one row. Returns a RowResult. Never raises."""
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
        "tab": "avatar",
        "avatar_id": row.avatar_id,
        "manual_image_provided": bool(row.manual_image_url),
        "num_shots": AVATAR_NUM_SHOTS,
    }
    zapcap_failed = False

    _log.info(
        "row_start",
        country=row.country,
        vertical=row.vertical,
        aspect=row.aspect_ratio,
        zapcap=row.zapcap,
        avatar_id=row.avatar_id,
        manual_image=bool(row.manual_image_url),
        tab="avatar",
    )

    if not row.avatar_id.strip():
        return _fail(
            row, STATUS_INTERNAL_ERROR,
            "avatar_id missing — pick one from the /admin/avatars page",
            t0, costs, metadata,
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

        # ─── Stage 2: language detect → classify → plan ───
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

            # Reuse the cartoon planner — one idea, 2 shots. The planner
            # already enforces brand-safety + character consistency. We use
            # the voiceover field as the avatar's narration script.
            plan = await generate_cartoon_plan(
                clients.openai,
                article_body=article_body,
                country=row.country,
                vertical=row.vertical,
                language=lang.language,
                script_pattern=row.script_pattern,
                open_comments=analysis,
                num_ideas=1,
                num_shots=AVATAR_NUM_SHOTS,
                settings_store=clients.settings_store,
                safety=safety,
            )
            costs.plan += plan.cost_usd
            if not plan.ideas:
                return _fail(
                    row, STATUS_INTERNAL_ERROR,
                    "planner returned 0 ideas — cannot generate scenes or narration",
                    t0, costs, metadata,
                )
            idea = plan.ideas[0]
            metadata["language"] = lang.language
            metadata["open_comments_mode"] = analysis.mode.value
            metadata["script_chars"] = len(idea.voiceover)
        except Exception as e:
            return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)

        # ─── Stage 3: CTA overlay setup (Yes/No) ───
        cta_overlay_url: str | None = None
        cta_setup_error: str | None = None
        if row.cta_enabled:
            cta_text = (
                row.cta_text.strip()
                or default_cta_for_language(lang.language)
            )
            try:
                ow, oh = dimensions_for_ratio(row.aspect_ratio)
                overlay_bytes = render_cartoon_cta_overlay_bytes(
                    cta_text,
                    canvas_width=ow,
                    canvas_height=oh,
                )
                up = await clients.storage.upload_bytes(
                    overlay_bytes,
                    key=f"bulkvid/cta_overlays/{slug}.png",
                    content_type="image/png",
                )
                costs.storage += up.cost_usd
                cta_overlay_url = up.url
                metadata["cta_enabled"] = True
                metadata["cta_text_used"] = cta_text[:80]
            except Exception as e:
                cta_setup_error = str(e)[:200]
                _log.error(
                    "avatar_cta_overlay_failed_skipped",
                    error=cta_setup_error, cta_text=cta_text[:80],
                )
                metadata["cta_enabled"] = False
                metadata["cta_overlay_error"] = cta_setup_error
        else:
            metadata["cta_enabled"] = False

        # ─── Stage 4: in parallel — image generation + avatar generation ───
        async def _generate_images() -> list[str]:
            urls: list[str] = []
            for s, shot in enumerate(idea.shots):
                is_first = s == 0
                prompt = image_prompt_for_shot(shot.scene, is_chained=not is_first)
                try:
                    if is_first and row.manual_image_url:
                        # Smart: use the operator's Manual Image as seed.
                        url, cost = await nano_banana_2_image_to_image(
                            clients.kie, row.manual_image_url, prompt, aspect,
                            resolution=IMAGE_RESOLUTION,
                        )
                    elif is_first:
                        url, cost = await nano_banana_2_text_to_image(
                            clients.kie, prompt, aspect,
                            resolution=IMAGE_RESOLUTION,
                        )
                    else:
                        # Shot 2+: image-to-image chained on shot 1 so the
                        # character / look stays consistent across the cut.
                        url, cost = await nano_banana_2_image_to_image(
                            clients.kie, urls[0], prompt, aspect,
                            resolution=IMAGE_RESOLUTION,
                        )
                    costs.image_gen += cost
                    urls.append(url)
                except Exception as e:
                    if not urls:
                        raise
                    _log.warning(
                        "avatar_shot_image_failed_held",
                        shot=s + 1, error=str(e)[:200],
                    )
                    urls.append(urls[-1])    # hold previous frame
            return urls

        async def _generate_avatar() -> tuple[str, float | None]:
            tiktok = TikTokAvatarClient()
            result = await tiktok.create_and_wait(
                avatar_id=row.avatar_id,
                script=idea.voiceover,
                video_name=f"bulkvid-{slug}",
            )
            return result.preview_url, result.duration_seconds

        try:
            images_task = asyncio.create_task(_generate_images())
            avatar_task = asyncio.create_task(_generate_avatar())
            image_urls = await images_task
        except Exception as e:
            avatar_task.cancel()
            return _fail(row, STATUS_IMAGE_GEN_FAILED, str(e), t0, costs, metadata)

        try:
            avatar_preview_url, avatar_duration = await avatar_task
        except TikTokAvatarError as e:
            return _fail(
                row, STATUS_TTS_FAILED,
                f"TikTok avatar failed: {e!s}", t0, costs, metadata,
            )
        except Exception as e:
            return _fail(
                row, STATUS_TTS_FAILED,
                f"avatar generation crashed: {e!s}", t0, costs, metadata,
            )

        metadata["avatar_duration_seconds"] = avatar_duration

        # ─── Stage 5: animate each image to a 4s clip (parallel) ───
        async def _animate(idx: int, image_url: str) -> tuple[int, str | None]:
            try:
                clip_url, cost = await seedance_image_to_video(
                    clients.kie, image_url, idea.shots[idx].motion, aspect,
                    duration=SEEDANCE_DURATION_SHORT,
                    resolution=SEEDANCE_RESOLUTION,
                )
                costs.seedance += cost
                return idx, clip_url
            except Exception as e:
                _log.warning(
                    "avatar_shot_animate_failed",
                    shot=idx + 1, error=str(e)[:200],
                )
                return idx, None

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
            return _fail(
                row, STATUS_VIDEO_ASSEMBLY_FAILED,
                "no Seedance clips produced for any shot",
                t0, costs, metadata,
            )

        # ─── Stage 6: stitch background (silent), then overlay avatar ───
        cleanup_command_ids: list[str] = []
        try:
            stitched = await clients.rendi.concat_clips_with_audio(
                clip_urls,
                None,    # silent — avatar drives audio
                per_clip_seconds=[float(SEEDANCE_DURATION_SHORT)] * AVATAR_NUM_SHOTS,
                output_filename="bg.mp4",
                aspect_ratio=aspect,
                total_video_seconds=float(SEEDANCE_DURATION_SHORT * AVATAR_NUM_SHOTS),
            )
            costs.rendi += stitched.cost_usd
            cleanup_command_ids.append(stitched.command_id)
        except Exception as e:
            return _fail(row, STATUS_VIDEO_ASSEMBLY_FAILED, str(e), t0, costs, metadata)

        try:
            canvas_w, _ = dimensions_for_ratio(row.aspect_ratio)
            overlay_px = max(120, int(canvas_w * AVATAR_OVERLAY_WIDTH_FRAC))
            composited = await clients.rendi.overlay_video_bottom_left(
                background_video_url=stitched.url,
                overlay_video_url=avatar_preview_url,
                overlay_width_px=overlay_px,
                margin_x=AVATAR_OVERLAY_MARGIN_PX,
                margin_y=AVATAR_OVERLAY_MARGIN_PX,
                output_filename="v1.mp4",
            )
            costs.rendi += composited.cost_usd
            cleanup_command_ids.append(composited.command_id)
            video_url_for_persist = composited.url
            metadata["avatar_overlay_width_px"] = overlay_px
        except Exception as e:
            return _fail(
                row, STATUS_VIDEO_ASSEMBLY_FAILED,
                f"avatar overlay failed: {e!s}",
                t0, costs, metadata,
            )

        # ─── Stage 6b: optional CTA pill overlay (non-fatal) ───
        if cta_overlay_url:
            try:
                overlaid = await clients.rendi.overlay_image_on_video(
                    video_url=video_url_for_persist,
                    overlay_url=cta_overlay_url,
                    output_filename="v1_cta.mp4",
                )
                costs.rendi += overlaid.cost_usd
                cleanup_command_ids.append(overlaid.command_id)
                video_url_for_persist = overlaid.url
            except Exception as cta_err:
                _log.error(
                    "avatar_cta_overlay_failed_kept_original",
                    error=str(cta_err)[:200],
                )
                metadata["cta_overlay_apply_error"] = str(cta_err)[:200]

        # ─── Stage 7: persist video to storage ───
        try:
            data = await _download(video_url_for_persist, timeout=180.0)
            up = await clients.storage.upload_bytes(
                data,
                key=f"bulkvid/videos/{slug}/v1.mp4",
                content_type="video/mp4",
            )
            costs.storage += up.cost_usd
            final_url = up.url
        except Exception as e:
            return _fail(row, STATUS_VIDEO_ASSEMBLY_FAILED, str(e), t0, costs, metadata)

        await clients.rendi.cleanup_commands(cleanup_command_ids)

        # ─── Stage 8: optional ZapCap ───
        if row.zapcap and clients.zapcap is not None:
            try:
                zapcap_opts: ZapCapRenderOptions | None = None
                if cta_overlay_url:
                    # Push captions higher so they don't overlap the CTA pill.
                    zapcap_opts = ZapCapRenderOptions(
                        subs=ZapCapSubsOptions(),
                        style=ZapCapStyleOptions(top=30, font_size=36),
                    )
                cap_url, cost = await clients.zapcap.caption_video(
                    video_bytes=data,
                    language=lang.language,
                    filename="v1.mp4",
                    render_options=zapcap_opts,
                    video_duration_seconds=(
                        avatar_duration
                        or float(SEEDANCE_DURATION_SHORT * AVATAR_NUM_SHOTS)
                    ),
                )
                costs.zapcap += cost
                cap_bytes = await _download(cap_url, timeout=180.0)
                cap_up = await clients.storage.upload_bytes(
                    cap_bytes,
                    key=f"bulkvid/videos_captioned/{slug}/v1.mp4",
                    content_type="video/mp4",
                )
                costs.storage += cap_up.cost_usd
                final_url = cap_up.url
                metadata["zapcap_applied"] = True
            except Exception as e:
                zapcap_failed = True
                _log.error(
                    "avatar_zapcap_failed_kept_original", error=str(e)[:200],
                )

        warning_parts: list[str] = []
        if cta_setup_error:
            warning_parts.append(
                f"CTA overlay skipped — setup failed: {cta_setup_error}"
            )
        warning = " | ".join(warning_parts)[:1000] or None

        if zapcap_failed:
            metadata["zapcap_applied"] = False
            return _ok(
                row, [final_url], t0, costs, metadata,
                status=STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS,
                warning=warning,
            )
        return _ok(row, [final_url], t0, costs, metadata, warning=warning)

    except Exception as e:
        _log.exception("row_internal_error", error=str(e))
        return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)


# ── Result builders ──────────────────────────────────────────────────────────


def _ok(
    row: AvatarRow,
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
    row: AvatarRow,
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
