"""Simple x4 row processor.

Pipeline = the image_vo pipeline plus two new steps when at least one
``Template*`` cell on the row is non-blank AND the
``card_templates_enabled`` settings switch is on:

  1. **Headline extraction** — one extra gpt-5.4-mini call inside the
     script-side coroutine. Returns a punchy ≤8-word headline drawn on the
     card. Runs concurrently with the rest of the script/TTS work, so it
     doesn't add latency in the common case.

  2. **Per-quadrant card overlay** — between the optimize and upload steps,
     each quadrant whose corresponding ``cards[i].template_id`` is "1" or
     "2" gets a Pillow overlay applied via ``render_card_bytes``. Quadrants
     with an empty template_id pass through unchanged.

When the master switch is OFF, the row behaves byte-identically to the
image_vo path (no headline call, no overlay) — flip the switch to recover
without a redeploy.

Plan: ``_plans/2026-06-08-simple-x4-template-cards.md`` §D.2, §D.3, §D.4.
"""

from __future__ import annotations

import asyncio
import base64
import io
import time
from dataclasses import dataclass

import httpx
from PIL import Image

from bulkvid.adapters.kie import recraft_crisp_upscale
from bulkvid.adapters.rendi import dimensions_for_ratio, normalize_aspect_ratio
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
    CardChoice,
    RowResult,
    SimpleX4Row,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.runtime_settings import (
    SETTING_CARD_TEMPLATE_1_DEFAULT_CTA,
    SETTING_CARD_TEMPLATE_2_DEFAULT_CTA,
    SETTING_CARD_TEMPLATES_ENABLED,
    SETTING_SIMPLE_X4_SCRIPT_PROMPT,
)
from bulkvid.pipeline.card_renderer import (
    SUPPORTED_TEMPLATES,
    TEMPLATE_1,
    TEMPLATE_2,
    render_card_bytes,
)
from bulkvid.pipeline.headline_gen import generate_card_headline
from bulkvid.pipeline.image_gen import edit_with_fallback
from bulkvid.pipeline.image_prompt import build_collage_prompt, describe_source_image
from bulkvid.pipeline.language import detect_language
from bulkvid.pipeline.open_comments import classify_open_comments
from bulkvid.pipeline.safety import resolve_safety
from bulkvid.pipeline.script_gen import generate_script

_log = get_logger("row")


# ── Helpers (mirror image_vo's helpers) ──────────────────────────────────────


async def _download(url: str, *, timeout: float = 60.0) -> bytes:
    async with httpx.AsyncClient(timeout=timeout) as c:
        resp = await c.get(url, follow_redirects=True)
        resp.raise_for_status()
        return resp.content


def _slug(row_num: int, job_id: str | None = None) -> str:
    job_part = (job_id or "job").replace("/", "_")
    return f"{job_part}_r{row_num}_{int(time.time())}"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _optimize_pil_bytes(quadrant_bytes: bytes) -> bytes:
    """2 MB cap optimizer for one quadrant — identical to image_vo's helper."""
    with Image.open(io.BytesIO(quadrant_bytes)) as img:
        img.load()
        copy = img.copy()
    buf, _fmt, _ct = optimize_image_for_size(copy)
    return buf.getvalue()


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
    headline: float = 0.0    # NEW vs image_vo
    tts: float = 0.0
    rendi: float = 0.0
    zapcap: float = 0.0

    @property
    def total(self) -> float:
        return round(
            self.article + self.vision + self.collage_prompt + self.image_gen
            + self.upscale + self.storage + self.language + self.classify
            + self.script + self.headline + self.tts + self.rendi + self.zapcap,
            6,
        )


class _StageError(Exception):
    """Carries the RowResult status to report for a failed pipeline stage."""

    def __init__(self, status: str, message: str) -> None:
        self.status = status
        super().__init__(message)


# ── Card overlay helpers ─────────────────────────────────────────────────────


def _truthy_setting(raw: str | None, *, default: bool) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "y", "on")


async def _resolve_card_runtime(
    clients: PipelineClients,
    cards: list[CardChoice],
) -> tuple[bool, dict[str, str]]:
    """Return ``(enabled, default_cta_by_template)``.

    ``enabled`` is the master switch from settings. When False or when no
    card has a non-blank template_id, the caller skips the overlay step
    entirely.

    ``default_cta_by_template`` maps "1" / "2" to the per-template default
    CTA, used when a card's CTA cell is blank.
    """
    if not any(c.template_id for c in cards):
        return (False, {})
    enabled_raw = await clients.settings_store.get(SETTING_CARD_TEMPLATES_ENABLED)
    enabled = _truthy_setting(enabled_raw, default=True)
    if not enabled:
        return (False, {})

    default_1 = await clients.settings_store.get(SETTING_CARD_TEMPLATE_1_DEFAULT_CTA) or ""
    default_2 = await clients.settings_store.get(SETTING_CARD_TEMPLATE_2_DEFAULT_CTA) or ""
    return (True, {TEMPLATE_1: default_1, TEMPLATE_2: default_2})


def _apply_card_overlay(
    quadrant_bytes: bytes,
    card: CardChoice,
    *,
    headline: str,
    default_cta_by_template: dict[str, str],
    aspect_ratio: str,
) -> bytes:
    """Apply the overlay for one quadrant. Pure CPU — runs in a thread pool."""
    if not card.template_id or card.template_id not in SUPPORTED_TEMPLATES:
        return quadrant_bytes
    cta = card.cta or default_cta_by_template.get(card.template_id, "")
    width, height = dimensions_for_ratio(aspect_ratio)
    return render_card_bytes(
        template_id=card.template_id,
        background_image_bytes=quadrant_bytes,
        headline=headline,
        cta=cta,
        width=width,
        height=height,
    )


# ── Public entrypoint ────────────────────────────────────────────────────────


async def process_simple_x4_row(
    row: SimpleX4Row,
    clients: PipelineClients,
    *,
    job_id: str | None = None,
    edge_crop_pixels: int = DEFAULT_EDGE_CROP_PIXELS,
) -> RowResult:
    """Run the simple_x4 pipeline for one row. Returns a RowResult. Never raises.

    Behaves identically to ``process_image_vo_row`` when:
      - the master switch ``card_templates_enabled`` is off, OR
      - every ``cards[i].template_id`` is blank.
    """
    set_context(batch_id=job_id, row_num=row.row_num)
    t0 = time.monotonic()
    costs = _Costs()
    slug = _slug(row.row_num, job_id)
    metadata: dict = {
        "row_num": row.row_num,
        "country": row.country,
        "vertical": row.vertical,
        "article_url": row.article_url,
        "aspect_ratio": row.aspect_ratio,
        "voice_over": row.voice_over,
        "zapcap": row.zapcap,
        "tab": "simple_x4",
        # Operator picks recorded in metadata so the SYMPHONY log carries them
        # — easy to grep "which template+CTA produced this video?" later.
        "card_picks": [
            {"template_id": c.template_id, "cta_chars": len(c.cta or "")}
            for c in row.cards
        ],
    }

    _log.info(
        "row_start",
        country=row.country,
        vertical=row.vertical,
        aspect=row.aspect_ratio,
        zapcap=row.zapcap,
        vo=row.voice_over,
        tab="simple_x4",
        any_card_template=any(c.template_id for c in row.cards),
    )

    # Resolve master switch + default CTAs once per row.
    cards_enabled, default_cta_by_template = await _resolve_card_runtime(
        clients, row.cards
    )
    metadata["card_overlay_enabled"] = cards_enabled

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

        async def _prep_source_image() -> tuple[str, str] | Exception:
            try:
                raw = await _download(row.manual_image_url, timeout=60.0)
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

        # ─── Sensitive-apparel safeguard ───────────────────────────────────

        safety = await resolve_safety(
            clients.settings_store, row.vertical, row.row_num
        )
        metadata["safety_matched"] = safety.matched
        metadata["safety_keyword"] = safety.matched_keyword

        # ─── Stages 2 + 8 (parallel): image side + script side ────────────

        async def _image_side() -> list[bytes] | Exception:
            try:
                description, c1 = await describe_source_image(clients.openai, source_b64)
                costs.vision += c1

                collage_prompt, c2 = await build_collage_prompt(
                    clients.openai,
                    description,
                    article_excerpt=article_body[:1500],
                    settings_store=clients.settings_store,
                    safety=safety,
                )
                costs.collage_prompt += c2

                collage_url, c3 = await edit_with_fallback(
                    kie=clients.kie,
                    atlas=clients.atlas,
                    source_image_url=source_url,
                    prompt=collage_prompt,
                    aspect_ratio=normalize_aspect_ratio(row.aspect_ratio),
                )
                costs.image_gen += c3

                upscaled_url, c4 = await recraft_crisp_upscale(clients.kie, collage_url)
                costs.upscale += c4

                upscaled_bytes = await _download(upscaled_url, timeout=120.0)
                quadrants = split_collage_2x2(
                    upscaled_bytes, edge_crop_pixels=edge_crop_pixels
                )
                if len(quadrants) != 4:
                    raise RuntimeError(
                        f"split_collage_2x2 returned {len(quadrants)} quadrants"
                    )

                optimized = await asyncio.gather(
                    *[asyncio.to_thread(_optimize_pil_bytes, q) for q in quadrants]
                )
                return list(optimized)
            except Exception as e:
                return e

        async def _script_side() -> (
            tuple[str, str, str, str | None, str] | _StageError
        ):
            """Same as image_vo's _script_side BUT also extracts a headline
            (when any card has a template chosen). Returns
            (script_text, style_direction, language, vo_url, headline)."""
            try:
                lang = await detect_language(clients.openai, article_body)
                costs.language += lang.cost_usd

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
                if script.chosen_template_id:
                    metadata["chosen_template_id"] = script.chosen_template_id
            except Exception as e:
                return _StageError(STATUS_INTERNAL_ERROR, str(e))

            # Headline (NEW) — only run when overlay will actually be applied.
            headline = ""
            if cards_enabled:
                headline, head_cost = await generate_card_headline(
                    clients.openai,
                    article_excerpt=article_body,
                    language=lang.language,
                    vertical=row.vertical,
                )
                costs.headline += head_cost
                metadata["card_headline_chars"] = len(headline)

            if not row.voice_over:
                return script.script, script.style_direction, lang.language, None, headline

            try:
                tts_result = await clients.tts.synthesize(
                    text=script.script,
                    language=lang.language,
                    style_prompt=script.style_direction,
                    country=row.country,
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
                return (
                    script.script,
                    script.style_direction,
                    lang.language,
                    vo_upload.url,
                    headline,
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
        _script_text, _style_direction, language, vo_url, headline = script_result

        # ─── Stage 6.5 (NEW): per-quadrant card overlay ───────────────────
        # Pure CPU. Run the 4 overlays in a thread pool so we don't block
        # the event loop on the largest aspect ratio.

        if cards_enabled:
            try:
                overlaid = await asyncio.gather(
                    *[
                        asyncio.to_thread(
                            _apply_card_overlay,
                            quadrants[i],
                            row.cards[i],
                            headline=headline,
                            default_cta_by_template=default_cta_by_template,
                            aspect_ratio=row.aspect_ratio,
                        )
                        for i in range(4)
                    ]
                )
                # Track per-video overlay outcome — useful when debugging "why
                # does video 3 look different from video 1".
                metadata["card_overlay_applied"] = [
                    bool(c.template_id) for c in row.cards
                ]
                quadrants = list(overlaid)
            except Exception as e:
                # Overlay failure must not kill the row — fall back to the
                # raw kie output, log loud, and continue. The user gets a
                # working video without the card; far better than a dead row.
                _log.error(
                    "card_overlay_failed_kept_raw",
                    error=str(e)[:200],
                )
                metadata["card_overlay_error"] = str(e)[:200]
                metadata["card_overlay_applied"] = [False, False, False, False]

        # ─── Stage 7 (parallel): upload 4 quadrants ───────────────────────

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

        # ─── Stage 10 (parallel): Rendi stills_to_video x 4 ───────────────

        async def _make_video(idx: int, image_url: str) -> tuple[str, str]:
            aspect = normalize_aspect_ratio(row.aspect_ratio)
            if vo_url is None:
                out = await clients.rendi.image_to_silent_video(
                    image_url=image_url,
                    output_filename=f"v{idx + 1}.mp4",
                    aspect_ratio=aspect,
                )
            else:
                out = await clients.rendi.stills_to_video(
                    image_url=image_url,
                    audio_url=vo_url,
                    output_filename=f"v{idx + 1}.mp4",
                    aspect_ratio=aspect,
                )
            costs.rendi += out.cost_usd
            return out.url, out.command_id

        try:
            rendi_results = await asyncio.gather(
                *[_make_video(i, u) for i, u in enumerate(quadrant_urls)]
            )
        except Exception as e:
            return _fail(row, STATUS_VIDEO_ASSEMBLY_FAILED, str(e), t0, costs, metadata)
        rendi_video_urls = [url for url, _ in rendi_results]
        rendi_command_ids = [cid for _, cid in rendi_results]

        # ─── Stage 11 (parallel): persist videos to OUR storage ───────────

        async def _persist_video(idx: int, rendi_url: str) -> str:
            data = await _download(rendi_url, timeout=180.0)
            up = await clients.storage.upload_bytes(
                data,
                key=f"bulkvid/videos/{slug}/v{idx + 1}.mp4",
                content_type="video/mp4",
            )
            costs.storage += up.cost_usd
            return up.url

        try:
            final_video_urls = await asyncio.gather(
                *[_persist_video(i, u) for i, u in enumerate(rendi_video_urls)]
            )
        except Exception as e:
            return _fail(row, STATUS_STORAGE_FAILED, str(e), t0, costs, metadata)

        await clients.rendi.cleanup_commands(rendi_command_ids)

        # ─── Stage 12 (optional): ZapCap ──────────────────────────────────

        if row.zapcap and clients.zapcap is not None:
            async def _caption(idx: int, video_url: str) -> str:
                video_bytes = await _download(video_url, timeout=180.0)
                cap_url, cost = await clients.zapcap.caption_video(
                    video_bytes=video_bytes,
                    language=language,
                    filename=f"v{idx + 1}.mp4",
                )
                costs.zapcap += cost
                cap_bytes = await _download(cap_url, timeout=180.0)
                up = await clients.storage.upload_bytes(
                    cap_bytes,
                    key=f"bulkvid/videos_captioned/{slug}/v{idx + 1}.mp4",
                    content_type="video/mp4",
                )
                costs.storage += up.cost_usd
                return up.url

            try:
                captioned = await asyncio.gather(
                    *[_caption(i, v) for i, v in enumerate(final_video_urls)]
                )
                final_video_urls = list(captioned)
                metadata["zapcap_applied"] = True
            except Exception as e:
                _log.error("zapcap_failed_kept_originals", error=str(e)[:200])
                metadata["zapcap_applied"] = False
                metadata["zapcap_error"] = str(e)[:200]
                return _ok(
                    row, final_video_urls, t0, costs, metadata,
                    status=STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS,
                )

        return _ok(row, final_video_urls, t0, costs, metadata)

    except Exception as e:
        _log.exception("row_internal_error", error=str(e))
        return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)


# ── Result builders ──────────────────────────────────────────────────────────


def _ok(
    row: SimpleX4Row,
    video_urls: list[str],
    t0: float,
    costs: _Costs,
    metadata: dict,
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
        tab="simple_x4",
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
    row: SimpleX4Row,
    status: str,
    error: str,
    t0: float,
    costs: _Costs,
    metadata: dict,
) -> RowResult:
    elapsed = round(time.monotonic() - t0, 3)
    metadata["cost_breakdown"] = costs.__dict__.copy()
    _log.error(
        "row_failed",
        status=status,
        error=error[:300],
        cost_usd=costs.total,
        elapsed_seconds=elapsed,
        tab="simple_x4",
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
