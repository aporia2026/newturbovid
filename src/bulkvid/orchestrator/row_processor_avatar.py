"""Avatar row processor — static-image background + TikTok avatar overlay.

Pipeline (simplified 2026-06-09 per
``_plans/2026-06-09-avatar-static-image-pipeline.md`` — was previously
a 2-shot Seedance-animated cartoon with the avatar composited on top):

  1. Article fetch (Tavily → ScrapingBee)
  2. language detect → classify Open Comments → safety resolve
  3. ``generate_script`` — same article→script flow the simple /
     simple-x4 tabs use; produces the ~10 s narration the avatar will
     speak.
  4. CTA overlay setup (Yes/No) — render the per-language pill PNG
     when ``cta_enabled``.
  5. In parallel:
     a. Background image — Manual Image used **as-is** when set
        (download + re-upload, NO kie call). Otherwise
        ``nano_banana_2_text_to_image`` with an article-derived prompt
        (one image only, no scene plan).
     b. TikTok Symphony avatar — POST script + avatar_id, poll until
        SUCCESS.
  6. Rendi ``still_image_with_avatar_overlay`` — single ffmpeg call:
     still image looped for the avatar duration, avatar composited
     bottom-left (~30 % canvas width), avatar's audio is the only
     audio track. Output length = avatar duration.
  7. Optional CTA pill overlay on the composed video (non-fatal).
  8. Persist video; free Rendi storage; optional ZapCap captions.

Graceful degradation:
  * Avatar API failure → row fails (no narration = no avatar tab output).
  * Background image failure → row fails (no background = no video).
  * CTA pill render/upload failure → ship without CTA (non-fatal).
  * ZapCap failure → ship without captions
    (``STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS``).

Cost impact vs the original 2-shot Seedance pipeline:
  * kie: ~$0.06 (2 shots) → $0 (manual image) or ~$0.03 (1 kie shot)
  * Seedance: ~$0.30 → $0 (dropped)
  * Total: ~$0.39 → ~$0.05 per row (~87 % reduction).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from bulkvid.adapters.kie import nano_banana_2_text_to_image
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
from bulkvid.http_download import download_image
from bulkvid.logging import get_logger, set_context
from bulkvid.models.row import (
    STATUS_ARTICLE_FETCH_FAILED,
    STATUS_IMAGE_GEN_FAILED,
    STATUS_INTERNAL_ERROR,
    STATUS_STORAGE_FAILED,
    STATUS_SUCCESS,
    STATUS_TTS_FAILED,
    STATUS_VIDEO_ASSEMBLY_FAILED,
    STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS,
    AvatarRow,
    RowResult,
)
from bulkvid.orchestrator.aspect_resolve import resolve_aspect_ratio
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.runtime_settings import SETTING_SIMPLE_SCRIPT_PROMPT
from bulkvid.pipeline.cartoon_cta import (
    PILL_BOTTOM_MARGIN_FRAC,
    PILL_HEIGHT_FRAC,
    render_cartoon_cta_overlay_bytes,
)
from bulkvid.pipeline.cta_defaults import default_cta_for_language
from bulkvid.pipeline.language import detect_language, reconcile_language
from bulkvid.pipeline.open_comments import classify_open_comments
from bulkvid.pipeline.safety import resolve_safety
from bulkvid.pipeline.script_gen import generate_script

_log = get_logger("row")


# ── Tunables ────────────────────────────────────────────────────────────────

IMAGE_RESOLUTION = "1K"

# Avatar overlay geometry — bottom-left.
# Per-row size (operator-facing dropdown) maps to a canvas-width
# fraction; the empty / unknown case falls back to ``Medium`` so
# existing sheets without the column keep rendering at today's 30 %.
# Plan: ``_plans/2026-06-09-avatar-overlay-size-shape.md``.
AVATAR_OVERLAY_MARGIN_PX = 40

# Extra vertical gap between the avatar's bottom edge and the CTA pill's
# top edge when BOTH are present. Without this, the avatar's bottom band
# overlaps the pill (chat 2026-06-09 screenshots). Small enough that the
# avatar still feels grounded near the bottom; large enough that the
# anti-aliased pill border doesn't kiss the avatar's bounding box.
AVATAR_OVER_CTA_GAP_PX = 16


def _avatar_margin_y_for_canvas(
    canvas_height: int, *, cta_enabled: bool,
) -> int:
    """Bottom-margin for the avatar overlay in canvas pixels.

    With no CTA, returns the static ``AVATAR_OVERLAY_MARGIN_PX``
    (today's behaviour). With CTA enabled, computes the pill's top
    edge from the same fractional constants ``cartoon_cta.py`` uses
    (``PILL_HEIGHT_FRAC`` + ``PILL_BOTTOM_MARGIN_FRAC``) and pushes
    the avatar's bottom to sit above that edge plus a small gap so
    the two overlays never visually collide.

    Centralising the formula here means a future bump to the pill's
    height or bottom-margin fraction is reflected automatically — no
    manual sync between the CTA renderer and the avatar composite.
    """
    if not cta_enabled:
        return AVATAR_OVERLAY_MARGIN_PX
    # Mirror cartoon_cta.render_cartoon_cta_overlay_bytes: pill height
    # is max(40, round(canvas_h × frac)); same floor here.
    pill_h = max(40, int(round(canvas_height * PILL_HEIGHT_FRAC)))
    pill_bottom_margin = int(round(canvas_height * PILL_BOTTOM_MARGIN_FRAC))
    return pill_bottom_margin + pill_h + AVATAR_OVER_CTA_GAP_PX
_AVATAR_SIZE_TO_FRAC: dict[str, float] = {
    "small": 0.20,
    "medium": 0.30,
    "large": 0.40,
}
_AVATAR_DEFAULT_SIZE = "medium"
# Per-row shape: ``rectangle`` (today's behaviour, the native avatar
# video aspect) or ``circle`` (centre-crop to a square + alpha mask).
_AVATAR_ALLOWED_SHAPES: frozenset[str] = frozenset({"rectangle", "circle"})
_AVATAR_DEFAULT_SHAPE = "rectangle"


def _resolve_avatar_size(raw: str) -> tuple[str, float]:
    """Resolve the operator's Avatar Size cell to (resolved_name, width_fraction).

    Empty / typo / unknown all collapse to the default. The resolved
    name is what we log + stash in metadata so the audit trail shows
    what actually rendered, not what the operator typed.
    """
    name = (raw or "").strip().lower()
    if name not in _AVATAR_SIZE_TO_FRAC:
        name = _AVATAR_DEFAULT_SIZE
    return name, _AVATAR_SIZE_TO_FRAC[name]


def _resolve_avatar_shape(raw: str) -> str:
    """Resolve the operator's Avatar Shape cell to a known enum value."""
    name = (raw or "").strip().lower()
    return name if name in _AVATAR_ALLOWED_SHAPES else _AVATAR_DEFAULT_SHAPE

# Article excerpt size for the background-image prompt. Same budget the
# cartoon planner uses (3 000 chars); long enough to ground the kie
# prompt in concrete article content, short enough to stay cheap.
_BACKGROUND_PROMPT_ARTICLE_CHARS = 3000


def _is_valid_http_url(url: str) -> bool:
    return isinstance(url, str) and url.strip().startswith(("http://", "https://"))


def _slug(row_num: int, job_id: str | None = None) -> str:
    job_part = (job_id or "job").replace("/", "_")
    return f"{job_part}_r{row_num}_{int(time.time())}"


# Avatar IDs from TikTok are alphanumerics + dashes/underscores; anything
# outside that range gets stripped so the filename stays portable across
# storage backends (GCS, S3, HF resolver) that all dislike spaces /
# punctuation / non-ASCII. Length capped because some TikTok IDs are
# long opaque blobs and the storage key has a max.
_AVATAR_ID_SANITIZE_RE = __import__("re").compile(r"[^A-Za-z0-9_\-]")


def _avatar_filename_slug(avatar_id: str) -> str:
    """Filesystem-safe avatar_id for use inside storage keys."""
    cleaned = _AVATAR_ID_SANITIZE_RE.sub("", avatar_id or "")[:32]
    return cleaned or "unknown"


def _background_image_prompt(
    *,
    article_excerpt: str,
    vertical: str,
    country: str,
    language: str,
) -> str:
    """Prompt for the kie text-to-image background when no Manual Image.

    Steers the model toward a clean magazine-style photo where the
    BOTTOM-LEFT third of the frame stays visually quiet — the talking-
    head avatar is composited there. Hard rules forbid text, logos,
    and faces in the avatar zone so the overlay always reads cleanly.
    """
    excerpt = (article_excerpt or "").strip()[:_BACKGROUND_PROMPT_ARTICLE_CHARS]
    return (
        f"High-quality marketing photograph for a {vertical} video ad "
        f"targeting {country} (audience language: {language}).\n\n"
        f"Article context (for subject matter inspiration):\n{excerpt}\n\n"
        "Style: clean, professional, magazine-quality real photography "
        "(no illustration, no 3D-render). Vibrant but restrained colour "
        "palette. Single clear focal subject.\n\n"
        "Composition rules (LOAD-BEARING — a talking-head presenter will "
        "be composited in the bottom-left ~30 % of the frame):\n"
        "  - The BOTTOM-LEFT third of the frame must be visually quiet: "
        "soft, low-contrast, no faces, no text, no important detail.\n"
        "  - Place the main subject in the top-right two-thirds.\n"
        "  - No text, logos, watermarks, captions, or signage in the image.\n"
        "  - No people in the bottom-left third.\n"
    )


@dataclass
class _Costs:
    article: float = 0.0
    language: float = 0.0
    classify: float = 0.0
    script: float = 0.0
    image_gen: float = 0.0
    tiktok: float = 0.0     # operator pays TikTok directly; tracked as 0 here
    rendi: float = 0.0
    zapcap: float = 0.0
    storage: float = 0.0

    @property
    def total(self) -> float:
        return round(
            self.article + self.language + self.classify + self.script
            + self.image_gen + self.tiktok + self.rendi
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
    # Blank Change Size → use the manual image's native pixel dimensions
    # (avatar tab allows manual_image_url to be blank; in that case the
    # resolver falls back to 9:16 since there's nothing to probe). Must
    # run BEFORE ``normalize_aspect_ratio`` so the kie scene generation
    # below sees the snapped ratio derived from the probed pixels.
    row.aspect_ratio = await resolve_aspect_ratio(
        row.aspect_ratio,
        manual_image_url=row.manual_image_url or None,
        row_num=row.row_num,
    )
    aspect = normalize_aspect_ratio(row.aspect_ratio)
    # Resolve operator-facing overlay knobs up front so the row_start log
    # records what we'll ACTUALLY render (not what was raw in the cell).
    resolved_size_name, size_frac = _resolve_avatar_size(row.avatar_size)
    resolved_shape = _resolve_avatar_shape(row.avatar_shape)
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
        "avatar_size": resolved_size_name,
        "avatar_size_raw": row.avatar_size,
        "avatar_shape": resolved_shape,
        "avatar_shape_raw": row.avatar_shape,
        # Pipeline version stamp — bump if the row processor's shape changes
        # so a future "wait, when did this start working/breaking?" question
        # is one grep away.
        "pipeline_version": "static_image_v2",
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
        avatar_size=resolved_size_name,
        avatar_shape=resolved_shape,
        tab="avatar",
        pipeline_version="static_image_v2",
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

        # ─── Stage 2: language detect → classify → safety ───
        try:
            lang = await detect_language(clients.openai, article_body)
            costs.language += lang.cost_usd
            # Safety net: a wrong/transient scrape can return wrong-language
            # content; prefer the operator's explicit market (Country / URL
            # locale) when it conflicts with detection (chat 2026-06-17).
            lang = reconcile_language(
                lang, article_url=row.article_url, country=row.country
            )

            analysis = await classify_open_comments(clients.openai, row.open_comments)
            costs.classify += analysis.cost_usd

            safety = await resolve_safety(
                clients.settings_store, row.vertical, row.row_num
            )
            metadata["language"] = lang.language
            metadata["open_comments_mode"] = analysis.mode.value
            metadata["safety_matched"] = safety.matched
            metadata["safety_keyword"] = safety.matched_keyword
        except Exception as e:
            return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)

        # ─── Stage 3: script generation ───
        # Reuses the simple-tab article→script flow. The resulting text
        # is what the TikTok avatar will speak; length is the simple-tab
        # default (~10–12 s of VO, matching the user's "like simple x4"
        # call). OVERRIDE mode short-circuits with the operator's
        # verbatim script — zero cost when the row is fully pre-written.
        try:
            script_result = await generate_script(
                clients.openai,
                article_body=article_body,
                country=row.country,
                vertical=row.vertical,
                language=lang.language,
                script_pattern=row.script_pattern,
                open_comments=analysis,
                settings_store=clients.settings_store,
                prompt_setting_key=SETTING_SIMPLE_SCRIPT_PROMPT,
                safety=safety,
            )
            costs.script += script_result.cost_usd
            metadata["script_chars"] = len(script_result.script)
            metadata["script_word_count"] = script_result.word_count
            metadata["script_used_override"] = script_result.used_override
        except Exception as e:
            return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)

        # ─── Stage 4: CTA overlay setup (Yes/No) ───
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

        # ─── Stage 5: in parallel — background image + avatar generation ───
        async def _resolve_background_image() -> str:
            """Return the URL of the static background image.

            Manual Image: used **as-is** — downloaded and re-uploaded
            to our own storage so the URL is stable + Rendi-reachable
            (operator pastes can be Drive / signed / expiring links).
            No kie call.

            No Manual Image: one ``nano_banana_2_text_to_image`` call
            with an article-derived prompt; the prompt enforces a
            visually quiet bottom-left zone for the avatar overlay.
            """
            if row.manual_image_url:
                # As-is: download + re-upload. No AI rewrite, no scene
                # description, no aspect-ratio coercion — Rendi's
                # cover-crop in the overlay command handles framing.
                raw = await download_image(row.manual_image_url, timeout=60.0)
                up = await clients.storage.upload_bytes(
                    raw,
                    key=f"bulkvid/avatar_backgrounds/{slug}_manual.png",
                    content_type="image/png",
                )
                costs.storage += up.cost_usd
                metadata["background_source"] = "manual"
                return up.url

            prompt = _background_image_prompt(
                article_excerpt=article_body,
                vertical=row.vertical,
                country=row.country,
                language=lang.language,
            )
            url, cost = await nano_banana_2_text_to_image(
                clients.kie, prompt, aspect,
                resolution=IMAGE_RESOLUTION,
            )
            costs.image_gen += cost
            metadata["background_source"] = "kie"
            metadata["background_prompt_chars"] = len(prompt)
            return url

        async def _generate_avatar() -> tuple[str, float | None]:
            tiktok = TikTokAvatarClient()
            result = await tiktok.create_and_wait(
                avatar_id=row.avatar_id,
                script=script_result.script,
                video_name=f"bulkvid-{slug}",
            )
            return result.preview_url, result.duration_seconds

        background_task = asyncio.create_task(_resolve_background_image())
        avatar_task = asyncio.create_task(_generate_avatar())

        try:
            background_image_url = await background_task
        except Exception as e:
            avatar_task.cancel()
            # Manual-image branch raises through _download (network /
            # storage); kie branch raises through nano_banana_2_*. Both
            # map to image gen failure for the operator.
            status = (
                STATUS_STORAGE_FAILED
                if row.manual_image_url and "upload" in str(e).lower()
                else STATUS_IMAGE_GEN_FAILED
            )
            return _fail(row, status, str(e), t0, costs, metadata)

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

        # ─── Stage 6: Rendi — still image bg + avatar overlay ───
        cleanup_command_ids: list[str] = []
        try:
            canvas_w, canvas_h = dimensions_for_ratio(row.aspect_ratio)
            # 120 px floor so a Small selection on a 1:1 1080-canvas still
            # renders a legible avatar; below that the head reads as a blob.
            overlay_px = max(120, int(canvas_w * size_frac))
            # When CTA is on, raise the avatar's bottom margin so it sits
            # above the pill instead of overlapping it (chat 2026-06-09).
            # The CTA pill is composited AFTER the avatar in Stage 6b, but
            # because the pill spans almost the full canvas width near the
            # bottom, the only reliable fix is to keep the avatar's bounding
            # box clear of the pill's zone. ``cta_overlay_url`` is non-None
            # only when ``cta_enabled`` was True AND the pill PNG render +
            # upload both succeeded — so a CTA setup failure correctly
            # falls back to today's tight bottom margin.
            avatar_margin_y = _avatar_margin_y_for_canvas(
                canvas_h, cta_enabled=cta_overlay_url is not None,
            )
            composited = await clients.rendi.still_image_with_avatar_overlay(
                background_image_url=background_image_url,
                overlay_video_url=avatar_preview_url,
                output_filename="v1.mp4",
                aspect_ratio=aspect,
                overlay_width_px=overlay_px,
                margin_x=AVATAR_OVERLAY_MARGIN_PX,
                margin_y=avatar_margin_y,
                shape=resolved_shape,
            )
            costs.rendi += composited.cost_usd
            cleanup_command_ids.append(composited.command_id)
            video_url_for_persist = composited.url
            metadata["avatar_overlay_width_px"] = overlay_px
            metadata["avatar_overlay_margin_y"] = avatar_margin_y
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
            data = await download_image(video_url_for_persist, timeout=180.0)
            up = await clients.storage.upload_bytes(
                data,
                key=f"bulkvid/videos/{slug}/avatar_{_avatar_filename_slug(row.avatar_id)}.mp4",
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
                    # Fall back to a sane default if TikTok didn't return a
                    # duration — ZapCap will trim to the actual video length
                    # anyway, this just sizes the caption track planner.
                    video_duration_seconds=avatar_duration or 15.0,
                )
                costs.zapcap += cost
                cap_bytes = await download_image(cap_url, timeout=180.0)
                cap_up = await clients.storage.upload_bytes(
                    cap_bytes,
                    key=f"bulkvid/videos_captioned/{slug}/avatar_{_avatar_filename_slug(row.avatar_id)}.mp4",
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
