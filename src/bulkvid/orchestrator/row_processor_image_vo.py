"""Image-VO row processor — the full per-row state machine.

Implements the pipeline from plan §5 ("Per-row pipeline (Image-VO tab)"):

  1. Parallel kickoff:
     1a. Article fetch (Tavily -> ScrapingBee fallback)
     1b. Pre-upload source image to storage + capture base64
  2. After 1b: GPT-4o visual description
  3. After 2:  gpt-5.4-mini collage prompt
  4. After 3:  kie.ai nano-banana-edit collage
  5. After 4:  kie.ai recraft/crisp-upscale
  6. After 5:  PIL split into 4 quadrants
  7. After 6:  optimize + upload each quadrant to storage  (parallel)
  8. After 1a: language detect -> classify Open Comments -> script gen
  9. After 8:  Gemini TTS
  10. After 7 AND 9: Rendi.dev stills_to_video x 4         (parallel)
  11. After 10: upload videos to storage                   (parallel)
  12. If ZapCap=Yes: upload to ZapCap, poll, store result  (parallel)
  13. Compile result with cost + metadata

Plan: ``_plans/2026-06-02-aporia-bulk-video-tool.md`` §5, §7, §8, §11.
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
from bulkvid.adapters.rendi import normalize_aspect_ratio
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
    ImageVORow,
    RowResult,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.runtime_settings import SETTING_SIMPLE_X4_SCRIPT_PROMPT
from bulkvid.pipeline.image_gen import edit_with_fallback
from bulkvid.pipeline.image_prompt import build_collage_prompt, describe_source_image
from bulkvid.pipeline.language import detect_language
from bulkvid.pipeline.open_comments import classify_open_comments
from bulkvid.pipeline.safety import resolve_safety
from bulkvid.pipeline.script_gen import generate_script

_log = get_logger("row")


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _download(url: str, *, timeout: float = 60.0) -> bytes:
    """Generic async HTTP GET -> bytes. Raises on non-2xx."""
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
    """Apply the 2MB cap optimizer to one quadrant bytes."""
    with Image.open(io.BytesIO(quadrant_bytes)) as img:
        img.load()
        # ``optimize_image_for_size`` consumes (and may close) its argument; pass a copy.
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
    """Carries the RowResult status to report for a failed pipeline stage, so a
    coroutine can fail with the right status (e.g. TTS vs script) and the caller
    just reads ``.status``."""

    def __init__(self, status: str, message: str) -> None:
        self.status = status
        super().__init__(message)


# ── Public entrypoint ────────────────────────────────────────────────────────


async def process_image_vo_row(
    row: ImageVORow,
    clients: PipelineClients,
    *,
    job_id: str | None = None,
    edge_crop_pixels: int = DEFAULT_EDGE_CROP_PIXELS,
) -> RowResult:
    """Run the Image-VO pipeline for a single row.

    Returns a RowResult with status, video URLs, total cost, elapsed time,
    and metadata for the SYMPHONY_DB log. Never raises — internal errors
    are caught and mapped to ``STATUS_INTERNAL_ERROR``.
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
    }

    _log.info(
        "row_start",
        country=row.country,
        vertical=row.vertical,
        aspect=row.aspect_ratio,
        zapcap=row.zapcap,
        vo=row.voice_over,
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
            except Exception as e:    # bubble up via tuple so we can report cleanly
                return e

        async def _prep_source_image() -> tuple[str, str] | Exception:
            """Download manual image, upload to storage, return (url, b64)."""
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

        # ─── Sensitive-apparel safeguard (per row, before the parallel work) ───

        safety = await resolve_safety(
            clients.settings_store, row.vertical, row.row_num
        )
        metadata["safety_matched"] = safety.matched
        metadata["safety_keyword"] = safety.matched_keyword

        # ─── Stage 2 + 8 (parallel): image-side prompt build + script-side run ───

        async def _image_side() -> list[bytes] | Exception:
            try:
                description, c1 = await describe_source_image(clients.openai, source_b64)
                costs.vision += c1
                # Keep the ad's text/CTA, change only the photo — grounded in the
                # article topic, not whatever the inspiration photo showed.
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
                quadrants = split_collage_2x2(upscaled_bytes, edge_crop_pixels=edge_crop_pixels)
                if len(quadrants) != 4:
                    raise RuntimeError(f"split_collage_2x2 returned {len(quadrants)} quadrants")

                # Optimize each quadrant (CPU-bound) in a thread pool.
                optimized = await asyncio.gather(
                    *[asyncio.to_thread(_optimize_pil_bytes, q) for q in quadrants]
                )
                return list(optimized)
            except Exception as e:
                return e

        async def _script_side() -> tuple[str, str, str, str | None] | _StageError:
            """Script + TTS, run concurrently with image generation. Returns
            (script_text, style_direction, language, vo_url). TTS overlaps the
            (slower) image side instead of waiting for it to finish."""
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

            if not row.voice_over:
                return script.script, script.style_direction, lang.language, None

            try:
                tts_result = await clients.tts.synthesize(
                    text=script.script, language=lang.language,
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
                return script.script, script.style_direction, lang.language, vo_upload.url
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
        # script_text/style_direction were consumed by TTS inside _script_side;
        # only language (for ZapCap) and vo_url are needed downstream.
        _script_text, _style_direction, language, vo_url = script_result

        # ─── Stage 7 (parallel): upload 4 quadrants ───

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

        # ─── Stage 10 (parallel): Rendi stills_to_video x 4 ───
        # (script + TTS already ran concurrently with image generation above)

        async def _make_video(idx: int, image_url: str) -> tuple[str, str]:
            aspect = normalize_aspect_ratio(row.aspect_ratio)
            if vo_url is None:
                # Voice Over = No -> silent video (image only), no voiceover.
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

        # ─── Stage 11 (parallel): persist videos to OUR storage ───

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

        # ─── Stage 11b: free Rendi storage (best-effort) ───
        # The finished videos now live in our own storage, so the Rendi copies
        # are dead weight against the account quota. Drop them. Never fails the row.
        await clients.rendi.cleanup_commands(rendi_command_ids)

        # ─── Stage 12 (optional): ZapCap ───

        if row.zapcap and clients.zapcap is not None:
            # ZapCap bills per second of rendered output. The VO drives the
            # video length; ``vo_duration_seconds`` was stamped onto
            # ``metadata`` inside ``_script_side()`` above. Fall back to the
            # silent-video default when VO is off.
            vo_duration = float(metadata.get("vo_duration_seconds") or 10.0)

            async def _caption(idx: int, video_url: str) -> str:
                video_bytes = await _download(video_url, timeout=180.0)
                cap_url, cost = await clients.zapcap.caption_video(
                    video_bytes=video_bytes,
                    language=language,
                    filename=f"v{idx + 1}.mp4",
                    video_duration_seconds=vo_duration,
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
                # Continue with the uncaptioned videos rather than failing the row.
                return _ok(
                    row,
                    final_video_urls,
                    t0,
                    costs,
                    metadata,
                    status=STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS,
                )

        return _ok(row, final_video_urls, t0, costs, metadata)

    except Exception as e:    # belt-and-braces — never let an exception escape
        _log.exception("row_internal_error", error=str(e))
        return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)


# ── Result builders ──────────────────────────────────────────────────────────


def _ok(
    row: ImageVORow,
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
    row: ImageVORow,
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
