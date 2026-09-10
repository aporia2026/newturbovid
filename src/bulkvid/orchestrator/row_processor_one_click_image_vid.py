"""1-click-image-vid row processor — one source image → one captioned story video.

A trimmed sibling of :mod:`row_processor_image_vo`. The image FRONT-HALF is
identical (source image → GPT-4o describe → collage prompt → nano-banana-2 →
recraft upscale → split into 4 quadrants), except the collage prompt runs in
STORY mode so the four cells are a single narrative arc (curiosity → discovery →
trying it → happy result) rather than four unrelated variations.

The video BACK-HALF differs: instead of 4 separate videos, the 4 quadrants are
sequenced into ONE still-image video sized to the voiceover (no dead-air tail),
then optionally overlaid with a CTA pill and captioned by ZapCap. Exactly one
``Ready Video`` URL is returned.

Pipeline:
  1. Parallel: article fetch + source-image pre-upload (+ base64).
  2. Sensitive-apparel safety resolve.
  3. Parallel:
     3a. image side: describe → story collage prompt → nano-banana-2 (+ fallbacks)
         → recraft upscale (soft-fallback to raw) → split 2x2 → optimize 4 quads.
     3b. script side: language detect/reconcile → classify Open Comments → script
         gen → Gemini TTS (if VO).
  4. Optional CTA pill overlay setup (mirrors simple-motion).
  5. Upload 4 quadrants to storage.
  6. Render each quadrant to a silent clip; concat the 4 clips + VO into ONE video
     sized to the VO length.
  7. Optional CTA overlay on the concatenated video.
  8. Optional ZapCap (only when there is a voiceover to transcribe).
  9. Persist the final video to storage; return one Ready Video URL.

Plan: ``_plans/2026-09-10-one-click-image-vid-tab.md``.
"""

from __future__ import annotations

import asyncio
import base64
import io
import math
import time
from dataclasses import dataclass
from typing import Any

from PIL import Image

from bulkvid.adapters.kie import KieError, recraft_crisp_upscale
from bulkvid.adapters.rendi import dimensions_for_ratio, normalize_aspect_ratio
from bulkvid.http_download import download_image
from bulkvid.image_ops import (
    DEFAULT_EDGE_CROP_PIXELS,
    optimize_image_for_size,
    split_collage_2x2,
)
from bulkvid.logging import get_logger, set_context
from bulkvid.models.row import (
    STATUS_ARTICLE_FETCH_FAILED,
    STATUS_IMAGE_DOWNLOAD_FAILED,
    STATUS_IMAGE_GEN_FAILED,
    STATUS_INTERNAL_ERROR,
    STATUS_STORAGE_FAILED,
    STATUS_SUCCESS,
    STATUS_TTS_FAILED,
    STATUS_VIDEO_ASSEMBLY_FAILED,
    STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS,
    OneClickImageVidRow,
    RowResult,
)
from bulkvid.orchestrator.aspect_resolve import resolve_aspect_ratio
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.runtime_settings import SETTING_SIMPLE_X4_SCRIPT_PROMPT
from bulkvid.pipeline.cartoon_cta import render_cartoon_cta_overlay_bytes
from bulkvid.pipeline.cta_defaults import default_cta_for_language
from bulkvid.pipeline.image_gen import edit_with_fallback, generate_with_fallback
from bulkvid.pipeline.image_prompt import build_collage_prompt, describe_source_image
from bulkvid.pipeline.language import detect_language, reconcile_language
from bulkvid.pipeline.open_comments import classify_open_comments
from bulkvid.pipeline.safety import resolve_safety
from bulkvid.pipeline.script_gen import generate_script
from bulkvid.pipeline.yt_cartoon import VO_TAIL_SECONDS

_log = get_logger("row")


# ── Tunables (this tab only) ────────────────────────────────────────────────

OCI_NUM_IMAGES = 4              # the 4-beat story arc = the 2x2 split
OCI_MIN_VIDEO_SECONDS = 8.0     # length floor; the VO otherwise wins (no cap)
OCI_ATEMPO = 1.0                # natural pace — a slideshow, not a rushed read
OCI_SILENT_SHOT_SECONDS = 4.0   # per-image dwell on the VO-off path


# ── Helpers ──────────────────────────────────────────────────────────────────


def _slug(row_num: int, job_id: str | None = None) -> str:
    job_part = (job_id or "job").replace("/", "_")
    return f"{job_part}_r{row_num}_{int(time.time())}"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _context_brief(country: str, vertical: str) -> str:
    """The 'description' the story collage prompt expects when there is NO source
    image — a short context brief from the row's market + topic. The article
    excerpt (passed separately to ``build_collage_prompt``) supplies the concrete
    subject; this line steers the market and framing so the invented scenes fit
    the audience. Used for the from-scratch (text-to-image) path."""
    market = (country or "").strip() or "an unspecified market"
    topic = (vertical or "").strip() or "the article's topic"
    return (
        f"There is NO source photo. Invent a fitting, realistic photographic "
        f"scene for the {topic} vertical, aimed at the {market} market, grounded "
        f"in the article below. Use natural, believable people and settings."
    )


def _optimize_pil_bytes(quadrant_bytes: bytes) -> bytes:
    """Apply the 2MB cap optimizer to one quadrant's bytes."""
    with Image.open(io.BytesIO(quadrant_bytes)) as img:
        img.load()
        # ``optimize_image_for_size`` consumes (and may close) its argument; pass a copy.
        copy = img.copy()
    buf, _fmt, _ct = optimize_image_for_size(copy)
    return buf.getvalue()


def _even_clips(total: float, num_shots: int) -> list[float]:
    """Split ``total`` seconds evenly across ``num_shots``, drift on the last.

    Mirrors ``orchestrator.pinned_cartoon._even_clips`` — kept local rather than
    importing a private symbol across modules.
    """
    per = round(total / num_shots, 3)
    clips = [per] * num_shots
    drift = round(total - sum(clips), 3)
    clips[-1] = round(clips[-1] + drift, 3)
    return clips


@dataclass
class _Costs:
    article: float = 0.0
    vision: float = 0.0
    collage_prompt: float = 0.0
    image_gen: float = 0.0
    upscale: float = 0.0
    storage: float = 0.0
    language: float = 0.0
    classify: float = 0.0
    script: float = 0.0
    tts: float = 0.0
    rendi: float = 0.0
    zapcap: float = 0.0

    @property
    def total(self) -> float:
        return round(
            self.article + self.vision + self.collage_prompt + self.image_gen
            + self.upscale + self.storage + self.language + self.classify
            + self.script + self.tts + self.rendi + self.zapcap,
            6,
        )


class _StageError(Exception):
    """Carries the RowResult status to report for a failed pipeline stage."""

    def __init__(self, status: str, message: str) -> None:
        self.status = status
        super().__init__(message)


@dataclass
class _ScriptSide:
    language: str
    vo_url: str | None
    vo_duration_seconds: float


# ── Public entrypoint ────────────────────────────────────────────────────────


async def process_one_click_image_vid_row(
    row: OneClickImageVidRow,
    clients: PipelineClients,
    *,
    job_id: str | None = None,
    edge_crop_pixels: int = DEFAULT_EDGE_CROP_PIXELS,
) -> RowResult:
    """Run the 1-click-image-vid pipeline for a single row.

    Returns a RowResult with status, ONE video URL, total cost, elapsed time,
    and metadata. Never raises — internal errors map to ``STATUS_INTERNAL_ERROR``.
    """
    set_context(batch_id=job_id, row_num=row.row_num)
    t0 = time.monotonic()
    costs = _Costs()
    slug = _slug(row.row_num, job_id)
    # Blank Change Size → use the source image's native pixel dimensions.
    row.aspect_ratio = await resolve_aspect_ratio(
        row.aspect_ratio,
        manual_image_url=row.manual_image_url,
        row_num=row.row_num,
    )
    metadata: dict[str, Any] = {
        "row_num": row.row_num,
        "country": row.country,
        "vertical": row.vertical,
        "article_url": row.article_url,
        "aspect_ratio": row.aspect_ratio,
        "voice_over": row.voice_over,
        "zapcap": row.zapcap,
        "cta_enabled": row.cta_enabled,
    }

    _log.info(
        "row_start",
        tab="one_click_image_vid",
        country=row.country,
        vertical=row.vertical,
        aspect=row.aspect_ratio,
        zapcap=row.zapcap,
        vo=row.voice_over,
        cta=row.cta_enabled,
    )

    try:
        # ─── Stage 1 (parallel): article fetch + source-image pre-upload ───

        async def _fetch_article() -> str | Exception:
            try:
                art = await clients.article.fetch(row.article_url)
                costs.article += art.cost_usd
                metadata["article_chars"] = art.char_count
                metadata["article_source"] = art.source
                return art.content
            except Exception as e:
                return e

        async def _prep_source_image() -> tuple[str | None, str | None] | Exception:
            """Download the source image (if any), upload it, return (url, b64).

            Blank Manual Image → ``(None, None)``: the 4 story frames are then
            generated from scratch (text-to-image) grounded in the article,
            vertical, and country instead of derived from a seed photo.
            """
            if not (row.manual_image_url or "").strip():
                return None, None
            try:
                raw = await download_image(row.manual_image_url, timeout=60.0)
            except Exception as e:
                return e
            try:
                upload = await clients.storage.upload_bytes(
                    raw, key=f"bulkvid/sources/{slug}.png", content_type="image/png"
                )
                costs.storage += upload.cost_usd
                return upload.url, _b64(raw)
            except Exception as e:
                return e

        article_task = asyncio.create_task(_fetch_article())
        source_task = asyncio.create_task(_prep_source_image())

        article_result = await article_task
        if isinstance(article_result, Exception):
            return _fail(
                row, STATUS_ARTICLE_FETCH_FAILED, str(article_result), t0, costs, metadata
            )
        article_body: str = article_result

        source_result = await source_task
        if isinstance(source_result, Exception):
            return _fail(
                row, STATUS_IMAGE_DOWNLOAD_FAILED, str(source_result), t0, costs, metadata
            )
        source_url, source_b64 = source_result
        metadata["from_scratch"] = source_url is None

        # ─── Sensitive-apparel safeguard (per row) ───

        safety = await resolve_safety(
            clients.settings_store, row.vertical, row.row_num
        )
        metadata["safety_matched"] = safety.matched
        metadata["safety_keyword"] = safety.matched_keyword

        # ─── Stage 3 (parallel): image (story collage) + script/TTS ───

        async def _image_side() -> list[bytes] | Exception:
            try:
                # With a seed image → describe it and generate FROM it (image-to-
                # image). Blank Manual Image → build a context brief from the
                # market + topic and generate the collage from scratch (text-to-
                # image); the article excerpt grounds the concrete subject.
                if source_url is not None and source_b64 is not None:
                    description, c1 = await describe_source_image(
                        clients.openai, source_b64
                    )
                    costs.vision += c1
                else:
                    description = _context_brief(row.country, row.vertical)

                # STORY mode: 4 sequential narrative beats, one recurring subject,
                # NO baked text (captions are added by ZapCap afterwards).
                collage_prompt, c2 = await build_collage_prompt(
                    clients.openai,
                    description,
                    article_excerpt=article_body[:1500],
                    settings_store=clients.settings_store,
                    safety=safety,
                    story=True,
                )
                costs.collage_prompt += c2

                if source_url is not None:
                    collage_url, c3 = await edit_with_fallback(
                        kie=clients.kie,
                        atlas=clients.atlas,
                        source_image_url=source_url,
                        prompt=collage_prompt,
                        aspect_ratio=normalize_aspect_ratio(row.aspect_ratio),
                    )
                else:
                    collage_url, c3 = await generate_with_fallback(
                        kie=clients.kie,
                        atlas=clients.atlas,
                        prompt=collage_prompt,
                        aspect_ratio=normalize_aspect_ratio(row.aspect_ratio),
                    )
                costs.image_gen += c3

                # Upscale is a quality boost, not a hard requirement — a transient
                # recraft outage falls back to the raw collage so the row ships.
                try:
                    split_source_url, c4 = await recraft_crisp_upscale(
                        clients.kie, collage_url
                    )
                    costs.upscale += c4
                except KieError as e:
                    _log.warning("upscale_failed_kept_raw", error=str(e)[:200])
                    metadata["upscale_fallback_raw"] = True
                    split_source_url = collage_url

                upscaled_bytes = await download_image(split_source_url, timeout=120.0)
                quadrants = split_collage_2x2(upscaled_bytes, edge_crop_pixels=edge_crop_pixels)
                if len(quadrants) != OCI_NUM_IMAGES:
                    raise RuntimeError(
                        f"split_collage_2x2 returned {len(quadrants)} quadrants"
                    )

                optimized = await asyncio.gather(
                    *[asyncio.to_thread(_optimize_pil_bytes, q) for q in quadrants]
                )
                return list(optimized)
            except Exception as e:
                return e

        async def _script_side() -> _ScriptSide | _StageError:
            """Script + TTS, concurrent with image generation."""
            try:
                lang = await detect_language(clients.openai, article_body)
                costs.language += lang.cost_usd
                lang = reconcile_language(
                    lang, article_url=row.article_url, country=row.country
                )

                analysis = await classify_open_comments(clients.openai, row.open_comments)
                costs.classify += analysis.cost_usd

                script = await generate_script(
                    clients.openai,
                    article_body=article_body,
                    country=row.country,
                    vertical=row.vertical,
                    language=lang.language,
                    script_pattern=row.script_pattern,
                    open_comments=analysis,
                    settings_store=clients.settings_store,
                    prompt_setting_key=SETTING_SIMPLE_X4_SCRIPT_PROMPT,
                    safety=safety,
                )
                costs.script += script.cost_usd
                metadata["language"] = lang.language
                metadata["open_comments_mode"] = analysis.mode.value
                metadata["script_word_count"] = script.word_count
                metadata["script_used_override"] = script.used_override
                metadata["script_override_oversize"] = script.override_oversize
                if script.chosen_template_id:
                    metadata["chosen_template_id"] = script.chosen_template_id
            except Exception as e:
                return _StageError(STATUS_INTERNAL_ERROR, str(e))

            if not row.voice_over:
                return _ScriptSide(language=lang.language, vo_url=None, vo_duration_seconds=0.0)

            try:
                tts_result = await clients.tts.synthesize(
                    text=script.script, language=lang.language,
                    voice=script.voice,
                    style_prompt=script.style_direction, country=row.country,
                )
                costs.tts += tts_result.cost_usd
                vo_upload = await clients.storage.upload_bytes(
                    tts_result.wav_bytes,
                    key=f"bulkvid/vo/{slug}/vo.wav",
                    content_type="audio/wav",
                )
                costs.storage += vo_upload.cost_usd
                metadata["vo_voice"] = tts_result.voice
                metadata["vo_duration_seconds"] = round(tts_result.duration_seconds, 2)
                return _ScriptSide(
                    language=lang.language,
                    vo_url=vo_upload.url,
                    vo_duration_seconds=float(tts_result.duration_seconds),
                )
            except Exception as e:
                return _StageError(STATUS_TTS_FAILED, str(e))

        image_task = asyncio.create_task(_image_side())
        script_task = asyncio.create_task(_script_side())

        image_result = await image_task
        if isinstance(image_result, Exception):
            return _fail(row, STATUS_IMAGE_GEN_FAILED, str(image_result), t0, costs, metadata)
        quadrants: list[bytes] = image_result

        script_result = await script_task
        if isinstance(script_result, _StageError):
            return _fail(row, script_result.status, str(script_result), t0, costs, metadata)
        language = script_result.language
        vo_url = script_result.vo_url

        # ─── Stage 4: CTA pill overlay setup (mirrors simple-motion) ───

        cta_overlay_url: str | None = None
        if row.cta_enabled:
            cta_text_used = (row.cta_text.strip() or default_cta_for_language(language))
            try:
                overlay_w, overlay_h = dimensions_for_ratio(row.aspect_ratio)
                # Off the event loop — the Pillow/raqm render is CPU-bound and would
                # otherwise block every other row's async I/O under concurrency.
                overlay_bytes = await asyncio.to_thread(
                    render_cartoon_cta_overlay_bytes,
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
                metadata["cta_text_used"] = cta_text_used[:80]
            except Exception as e:
                _log.error(
                    "oci_cta_overlay_failed_skipped", error=str(e)[:200],
                    cta_text=cta_text_used[:80],
                )
                metadata["cta_enabled"] = False
                metadata["cta_overlay_error"] = str(e)[:200]

        # ─── Stage 5 (parallel): upload 4 quadrants ───

        async def _upload_quadrant(idx: int, data: bytes) -> str:
            up = await clients.storage.upload_bytes(
                data,
                key=f"bulkvid/images/{slug}/q{idx + 1}.jpg",
                content_type="image/jpeg",
            )
            costs.storage += up.cost_usd
            return up.url

        try:
            quadrant_urls = await asyncio.gather(
                *[_upload_quadrant(i, q) for i, q in enumerate(quadrants)]
            )
        except Exception as e:
            return _fail(row, STATUS_STORAGE_FAILED, str(e), t0, costs, metadata)

        # ─── Stage 6: 4 stills → ONE video sized to the VO ───

        aspect = normalize_aspect_ratio(row.aspect_ratio)
        if vo_url is not None:
            raw = script_result.vo_duration_seconds
            # atempo 1.0 → played length == raw; grow the video to the voice, no
            # trailing silence (VO_TAIL is a small dwell after the last word).
            total = max(OCI_MIN_VIDEO_SECONDS, round(raw / OCI_ATEMPO + VO_TAIL_SECONDS, 3))
        else:
            raw = 0.0
            total = round(OCI_NUM_IMAGES * OCI_SILENT_SHOT_SECONDS, 3)
        per_clip = _even_clips(total, OCI_NUM_IMAGES)
        clip_seconds = math.ceil(max(per_clip)) + 1   # each still ≥ its trim
        metadata["oci_total_seconds"] = total
        _log.info(
            "oci_timing",
            vo_raw_seconds=round(raw, 3),
            total_seconds=total,
            per_clip=[round(p, 3) for p in per_clip],
            atempo=OCI_ATEMPO,
            has_vo=vo_url is not None,
        )

        cleanup_command_ids: list[str] = []

        async def _make_clip(idx: int, image_url: str) -> tuple[str, str]:
            out = await clients.rendi.image_to_silent_video(
                image_url=image_url,
                output_filename=f"clip{idx + 1}.mp4",
                aspect_ratio=aspect,
                seconds=clip_seconds,
            )
            costs.rendi += out.cost_usd
            return out.url, out.command_id

        try:
            clip_results = await asyncio.gather(
                *[_make_clip(i, u) for i, u in enumerate(quadrant_urls)]
            )
            clip_urls = [u for u, _ in clip_results]
            cleanup_command_ids.extend(cid for _, cid in clip_results)

            stitched = await clients.rendi.concat_clips_with_audio(
                clip_urls,
                vo_url,
                per_clip_seconds=per_clip,
                output_filename="story.mp4",
                aspect_ratio=aspect,
                total_video_seconds=total,
                atempo=OCI_ATEMPO,
            )
            costs.rendi += stitched.cost_usd
            cleanup_command_ids.append(stitched.command_id)
            _log.info("oci_concat_ok", clips=len(clip_urls))
            video_url = stitched.url

            # ─── Stage 7: optional CTA pill overlay (non-fatal) ───
            if cta_overlay_url:
                try:
                    overlaid = await clients.rendi.overlay_image_on_video(
                        video_url=video_url,
                        overlay_url=cta_overlay_url,
                        output_filename="story_cta.mp4",
                    )
                    costs.rendi += overlaid.cost_usd
                    cleanup_command_ids.append(overlaid.command_id)
                    video_url = overlaid.url
                except Exception as cta_err:
                    _log.error(
                        "oci_cta_overlay_failed_kept_original", error=str(cta_err)[:300]
                    )
                    metadata["cta_overlay_apply_error"] = str(cta_err)[:200]
        except Exception as e:
            await clients.rendi.cleanup_commands(cleanup_command_ids)
            return _fail(row, STATUS_VIDEO_ASSEMBLY_FAILED, str(e), t0, costs, metadata)

        # ─── Stage 8: fetch the assembled video, optionally caption it ───

        zapcap_status = STATUS_SUCCESS
        try:
            video_bytes = await download_image(video_url, timeout=180.0)
        except Exception as e:
            await clients.rendi.cleanup_commands(cleanup_command_ids)
            return _fail(row, STATUS_VIDEO_ASSEMBLY_FAILED, str(e), t0, costs, metadata)

        if row.zapcap and vo_url is not None and clients.zapcap is not None:
            try:
                cap_url, cap_cost = await clients.zapcap.caption_video(
                    video_bytes=video_bytes,
                    language=language,
                    filename="story.mp4",
                    video_duration_seconds=total,
                )
                costs.zapcap += cap_cost
                video_bytes = await download_image(cap_url, timeout=180.0)
                metadata["zapcap_applied"] = True
            except Exception as e:
                _log.error("zapcap_failed_kept_originals", error=str(e)[:200])
                metadata["zapcap_applied"] = False
                metadata["zapcap_error"] = str(e)[:200]
                zapcap_status = STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS
        elif row.zapcap and vo_url is None:
            _log.info("oci_zapcap_skipped_no_vo")
            metadata["zapcap_applied"] = False
            metadata["zapcap_skipped_no_vo"] = True

        # ─── Stage 9: persist the final video to OUR storage ───

        try:
            up = await clients.storage.upload_bytes(
                video_bytes,
                key=f"bulkvid/videos/{slug}/story.mp4",
                content_type="video/mp4",
            )
            costs.storage += up.cost_usd
            final_url = up.url
        except Exception as e:
            await clients.rendi.cleanup_commands(cleanup_command_ids)
            return _fail(row, STATUS_STORAGE_FAILED, str(e), t0, costs, metadata)

        # Free the Rendi copies now that the finished video lives in our storage.
        await clients.rendi.cleanup_commands(cleanup_command_ids)

        return _ok(row, [final_url], t0, costs, metadata, status=zapcap_status)

    except Exception as e:    # belt-and-braces — never let an exception escape
        _log.exception("row_internal_error", error=str(e))
        return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)


# ── Result builders ──────────────────────────────────────────────────────────


def _ok(
    row: OneClickImageVidRow,
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
    row: OneClickImageVidRow,
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
