"""motion-ads row processor — a silent motion-ad video + ad copy.

The Motion_Ads tab turns an article into a native motion ad. Per row it produces
ONE 12-second SILENT video (no voiceover, no CTA, no captions, no audio track of
any kind) plus two text outputs written back to the sheet:

  * Headline    (col D, <= 60 chars)
  * Description (col E, <= 80 chars)

Pipeline (deliberately lean — no planner, no TTS, no Rendi):
  1. Article fetch (ScrapingBee -> direct).
  2. language detect -> reconcile with the operator's market -> safety check.
  3. ONE LLM call (motion_ads_copy) -> {headline, description, image_scene}.
  4. Image: a pasted Manual Image (col F) is animated as-is; a blank cell is
     generated as a REALISTIC photograph from the scene. Apple=Yes forces the
     GENERATED image to contain no people; a sensitive-apparel vertical forces
     product-only regardless.
  5. Seedance animates the image into ONE 12s silent clip (gentle push-in).
  6. Persist the clip to our storage. No concat, no audio.

Copy is set on the result on BOTH the success and any post-copy failure path, so
the Headline / Description land in the sheet even when the video step fails; the
Ready Video URL only lands on success.

Plan: ``_plans/2026-07-08-motion-ads-tab.md``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from bulkvid.adapters.kie import (
    nano_banana_2_text_to_image,
    seedance_image_to_video,
)
from bulkvid.adapters.rendi import normalize_aspect_ratio
from bulkvid.http_download import download_image
from bulkvid.logging import get_logger, set_context
from bulkvid.models.row import (
    STATUS_ARTICLE_FETCH_FAILED,
    STATUS_IMAGE_DOWNLOAD_FAILED,
    STATUS_IMAGE_GEN_FAILED,
    STATUS_INTERNAL_ERROR,
    STATUS_SUCCESS,
    STATUS_VIDEO_ASSEMBLY_FAILED,
    MotionAdsRow,
    RowResult,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.runtime_settings import SETTING_SENSITIVE_APPAREL_RULES
from bulkvid.pipeline.cartoon_prompt import NO_BRANDING, REALISTIC_STYLE
from bulkvid.pipeline.language import detect_language, reconcile_language
from bulkvid.pipeline.motion_ads_copy import generate_motion_ads_copy
from bulkvid.pipeline.safety import append_safety_block, resolve_safety

_log = get_logger("row")


# ── Tunables ─────────────────────────────────────────────────────────────────

# Always 12s (Yoav 2026-07-08 "make it always 12"). Seedance accepts only
# 4/8/12; 12 is the closest to the tab's 15s ceiling and loops on Apple/Taboola.
MA_VIDEO_DURATION_SECONDS = 12
MA_VIDEO_RESOLUTION = "720p"       # 16:9 @ 720p = 1280x720 (the Motion Ads spec)
MA_IMAGE_RESOLUTION = "2K"         # crisp source frame for the animation
MA_DEFAULT_ASPECT = "16:9"         # Motion Ads spec default

# Universal, gentle motion for a near-static ad image. The scene is generated
# (or pasted) independently of this, so a scene-specific motion would risk a
# mismatch — a subtle push-in is the safe, on-brief "slightly motion".
MOTION_PROMPT = (
    "Subtle, natural movement with a slow, gentle cinematic camera push-in. "
    "Keep the scene calm and stable — no fast motion, no cuts, no on-screen text."
)

# Hard no-people clause for Apple=Yes. Baked into the prompt the image model
# actually sees (a planner-only rule isn't enough — mirrors NO_BRANDING).
NO_PEOPLE = (
    "Do not include any people, humans, faces, hands, or body parts anywhere in "
    "the image — show only objects, products, environments, or scenery."
)


def _slug(row_num: int, job_id: str | None = None) -> str:
    job_part = (job_id or "job").replace("/", "_")
    return f"{job_part}_r{row_num}_{int(time.time())}"


def _compose_image_prompt(
    scene: str, *, apple: bool, safety: Any, safety_block: str
) -> str:
    """Build the full nano-banana-2 prompt: realistic style + scene + no-brands,
    plus a no-people clause on Apple and the sensitive-apparel block on match."""
    base = f"{REALISTIC_STYLE} {scene.strip()} {NO_BRANDING}"
    if apple:
        base = f"{base} {NO_PEOPLE}"
    return append_safety_block(base, safety, safety_block)


@dataclass
class _Costs:
    article: float = 0.0
    language: float = 0.0
    copy: float = 0.0
    image_gen: float = 0.0
    seedance: float = 0.0
    storage: float = 0.0

    @property
    def total(self) -> float:
        return round(
            self.article + self.language + self.copy
            + self.image_gen + self.seedance + self.storage,
            6,
        )


async def process_motion_ads_row(
    row: MotionAdsRow,
    clients: PipelineClients,
    *,
    job_id: str | None = None,
) -> RowResult:
    """Run the Motion_Ads pipeline for one row. Returns a RowResult. Never raises."""
    set_context(batch_id=job_id, row_num=row.row_num)
    t0 = time.monotonic()
    costs = _Costs()
    slug = _slug(row.row_num, job_id)
    aspect = normalize_aspect_ratio(row.aspect_ratio, default=MA_DEFAULT_ASPECT)
    manual = (row.manual_image_url or "").strip()
    # Copy is generated before the video; keep it in scope so every failure path
    # after copy still writes Headline / Description back to the sheet.
    headline = ""
    description = ""
    metadata: dict[str, Any] = {
        "row_num": row.row_num,
        "country": row.country,
        "vertical": row.vertical,
        "article_url": row.article_url,
        "aspect_ratio": row.aspect_ratio,
        "apple": row.apple,
        "manual_image": bool(manual),
        "tab": "motion_ads",
    }

    _log.info(
        "row_start",
        country=row.country,
        vertical=row.vertical,
        aspect=row.aspect_ratio,
        apple=row.apple,
        manual_image=bool(manual),
        tab="motion_ads",
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
            return _fail(
                row, STATUS_ARTICLE_FETCH_FAILED, str(e), t0, costs, metadata,
                headline, description,
            )

        # ─── Stage 2: language -> safety -> copy (headline / description / scene) ───
        try:
            lang = await detect_language(clients.openai, article_body)
            costs.language += lang.cost_usd
            lang = reconcile_language(
                lang, article_url=row.article_url, country=row.country
            )

            safety = await resolve_safety(
                clients.settings_store, row.vertical, row.row_num
            )
            metadata["safety_matched"] = safety.matched
            metadata["safety_keyword"] = safety.matched_keyword
            safety_block = ""
            if safety.matched and clients.settings_store is not None:
                safety_block = await clients.settings_store.get(
                    SETTING_SENSITIVE_APPAREL_RULES
                )

            copy = await generate_motion_ads_copy(
                clients.openai,
                article_body=article_body,
                language=lang.language,
                country=row.country,
                vertical=row.vertical,
                open_comments=row.open_comments,
                apple=row.apple,
            )
            costs.copy += copy.cost_usd
            headline = copy.headline
            description = copy.description
            metadata["language"] = lang.language
            metadata["headline_chars"] = len(headline)
            metadata["description_chars"] = len(description)
        except Exception as e:
            return _fail(
                row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata,
                headline, description,
            )

        # ─── Stage 3: the source image (manual as-is, or generated realistic) ───
        if manual:
            try:
                raw = await download_image(manual, timeout=60.0)
                up = await clients.storage.upload_bytes(
                    raw,
                    key=f"bulkvid/motion_ads_images/{slug}.png",
                    content_type="image/png",
                )
                costs.storage += up.cost_usd
                image_url = up.url
                metadata["image_source"] = "manual"
            except Exception as e:
                return _fail(
                    row, STATUS_IMAGE_DOWNLOAD_FAILED,
                    f"manual image download failed: {e}",
                    t0, costs, metadata, headline, description,
                )
        else:
            prompt = _compose_image_prompt(
                copy.image_scene, apple=row.apple, safety=safety,
                safety_block=safety_block,
            )
            try:
                image_url, img_cost = await nano_banana_2_text_to_image(
                    clients.kie, prompt, aspect, resolution=MA_IMAGE_RESOLUTION,
                )
                costs.image_gen += img_cost
                metadata["image_source"] = "generated"
            except Exception as e:
                return _fail(
                    row, STATUS_IMAGE_GEN_FAILED,
                    f"image generation failed: {e}",
                    t0, costs, metadata, headline, description,
                )

        # ─── Stage 4: animate into one 12s SILENT clip ───
        try:
            clip_url, clip_cost = await seedance_image_to_video(
                clients.kie, image_url, MOTION_PROMPT, aspect,
                duration=MA_VIDEO_DURATION_SECONDS, resolution=MA_VIDEO_RESOLUTION,
            )
            costs.seedance += clip_cost
        except Exception as e:
            return _fail(
                row, STATUS_VIDEO_ASSEMBLY_FAILED,
                f"animation failed: {e}",
                t0, costs, metadata, headline, description,
            )

        # ─── Stage 5: persist to our storage (no Rendi, no audio) ───
        try:
            data = await download_image(clip_url, timeout=180.0)
            up = await clients.storage.upload_bytes(
                data,
                key=f"bulkvid/videos/{slug}/v1.mp4",
                content_type="video/mp4",
            )
            costs.storage += up.cost_usd
            final_url = up.url
        except Exception as e:
            return _fail(
                row, STATUS_VIDEO_ASSEMBLY_FAILED,
                f"video persist failed: {e}",
                t0, costs, metadata, headline, description,
            )

        metadata["videos_produced"] = 1
        return _ok(row, [final_url], t0, costs, metadata, headline, description)

    except Exception as e:
        _log.exception("row_internal_error", error=str(e))
        return _fail(
            row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata,
            headline, description,
        )


# ── Result builders ──────────────────────────────────────────────────────────


def _ok(
    row: MotionAdsRow,
    video_urls: list[str],
    t0: float,
    costs: _Costs,
    metadata: dict[str, Any],
    headline: str,
    description: str,
) -> RowResult:
    elapsed = round(time.monotonic() - t0, 3)
    metadata["cost_breakdown"] = costs.__dict__.copy()
    _log.info(
        "row_done",
        status=STATUS_SUCCESS,
        cost_usd=costs.total,
        elapsed_seconds=elapsed,
        video_count=len(video_urls),
        headline_chars=len(headline),
        description_chars=len(description),
    )
    return RowResult(
        row_num=row.row_num,
        status=STATUS_SUCCESS,
        video_urls=video_urls,
        cost_usd=costs.total,
        elapsed_seconds=elapsed,
        metadata=metadata,
        headline=headline,
        description=description,
    )


def _fail(
    row: MotionAdsRow,
    status: str,
    error: str,
    t0: float,
    costs: _Costs,
    metadata: dict[str, Any],
    headline: str,
    description: str,
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
        headline=headline,
        description=description,
    )
