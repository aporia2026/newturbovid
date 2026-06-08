"""Text-on-image row processor — one video from the user's manual image
with the operator-typed text overlaid in the center.

Pipeline (mirrors the ``simple`` tab, with the text overlay step inserted
between the manual image and Rendi):

  1. Validate the manual image URL
  2. Article fetch
  3. language detect -> classify Open Comments -> script gen
  4. Gemini TTS -> upload VO
  5. Download manual image -> cover-crop to target aspect -> overlay text
  6. Upload the composed image
  7. Rendi image_to_video_fit on the composed image + VO -> 1 video
  8. Upload video to storage
  9. Free Rendi storage (best-effort)
 10. If ZapCap=Yes: caption the video

No kie.ai, no GPT-4o description, no collage. The text overlay is pure
Pillow CPU; runs in an asyncio thread pool via ``asyncio.to_thread`` so
the event loop stays free for other rows.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from bulkvid.adapters.rendi import normalize_aspect_ratio
from bulkvid.logging import get_logger, set_context
from bulkvid.models.row import (
    STATUS_ARTICLE_FETCH_FAILED,
    STATUS_IMAGE_DOWNLOAD_FAILED,
    STATUS_INTERNAL_ERROR,
    STATUS_STORAGE_FAILED,
    STATUS_SUCCESS,
    STATUS_TTS_FAILED,
    STATUS_VIDEO_ASSEMBLY_FAILED,
    STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS,
    RowResult,
    TextOnImgRow,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.runtime_settings import SETTING_SIMPLE_SCRIPT_PROMPT
from bulkvid.pipeline.language import detect_language
from bulkvid.pipeline.open_comments import classify_open_comments
from bulkvid.pipeline.safety import resolve_safety
from bulkvid.pipeline.script_gen import generate_script
from bulkvid.pipeline.text_overlay import overlay_text_on_image_bytes

_log = get_logger("row")


async def _download(url: str, *, timeout: float = 60.0) -> bytes:
    async with httpx.AsyncClient(timeout=timeout) as c:
        resp = await c.get(url, follow_redirects=True)
        resp.raise_for_status()
        return resp.content


def _slug(row_num: int, job_id: str | None = None) -> str:
    job_part = (job_id or "job").replace("/", "_")
    return f"{job_part}_r{row_num}_{int(time.time())}"


def _is_valid_http_url(url: str) -> bool:
    return isinstance(url, str) and url.strip().startswith(("http://", "https://"))


@dataclass
class _Costs:
    article: float = 0.0
    language: float = 0.0
    classify: float = 0.0
    script: float = 0.0
    tts: float = 0.0
    rendi: float = 0.0
    zapcap: float = 0.0
    storage: float = 0.0

    @property
    def total(self) -> float:
        return round(
            self.article + self.language + self.classify + self.script
            + self.tts + self.rendi + self.zapcap + self.storage,
            6,
        )


async def process_text_on_img_row(
    row: TextOnImgRow,
    clients: PipelineClients,
    *,
    job_id: str | None = None,
) -> RowResult:
    """Run the text-on-img pipeline for one row. Returns a RowResult. Never raises."""
    set_context(batch_id=job_id, row_num=row.row_num)
    t0 = time.monotonic()
    costs = _Costs()
    slug = _slug(row.row_num, job_id)
    metadata: dict[str, Any] = {
        "row_num": row.row_num,
        "country": row.country,
        "vertical": row.vertical,
        "article_url": row.article_url,
        "aspect_ratio": row.aspect_ratio,
        "voice_over": row.voice_over,
        "zapcap": row.zapcap,
        "tab": "text_on_img",
        "overlay_chars": len(row.text or ""),
    }

    _log.info(
        "row_start",
        country=row.country,
        vertical=row.vertical,
        aspect=row.aspect_ratio,
        zapcap=row.zapcap,
        vo=row.voice_over,
        tab="text_on_img",
        overlay_chars=len(row.text or ""),
    )

    if not _is_valid_http_url(row.manual_image_url):
        return _fail(
            row, STATUS_IMAGE_DOWNLOAD_FAILED,
            "manual_image_url is missing or not an HTTP(S) URL",
            t0, costs, metadata,
        )

    try:
        # ─── Stage 2: article fetch ───
        try:
            art = await clients.article.fetch(row.article_url)
            costs.article += art.cost_usd
            metadata["article_chars"] = art.char_count
            metadata["article_source"] = art.source
            article_body: str = art.content
        except Exception as e:
            return _fail(row, STATUS_ARTICLE_FETCH_FAILED, str(e), t0, costs, metadata)

        # ─── Stage 3: language detect -> classify -> script ───
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

            script = await generate_script(
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
            costs.script += script.cost_usd
            metadata["language"] = lang.language
            metadata["open_comments_mode"] = analysis.mode.value
            metadata["script_word_count"] = script.word_count
            metadata["script_used_override"] = script.used_override
            if script.chosen_template_id:
                metadata["chosen_template_id"] = script.chosen_template_id
        except Exception as e:
            return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)

        # ─── Stage 4: TTS + VO upload ───
        vo_url: str | None = None
        tts_duration = 10.0    # fallback for the no-VO ZapCap branch
        if row.voice_over:
            try:
                tts = await clients.tts.synthesize(
                    text=script.script,
                    language=lang.language,
                    style_prompt=script.style_direction,
                    country=row.country,
                )
                costs.tts += tts.cost_usd
                vo_upload = await clients.storage.upload_bytes(
                    tts.wav_bytes,
                    key=f"bulkvid/vo/{slug}/vo.wav",
                    content_type="audio/wav",
                )
                costs.storage += vo_upload.cost_usd
                vo_url = vo_upload.url
                tts_duration = tts.duration_seconds
                metadata["vo_voice"] = tts.voice
                metadata["vo_duration_seconds"] = round(tts.duration_seconds, 2)
            except Exception as e:
                return _fail(row, STATUS_TTS_FAILED, str(e), t0, costs, metadata)

        # ─── Stage 5: download manual image -> overlay text -> upload ───
        try:
            source_image_bytes = await _download(row.manual_image_url, timeout=60.0)
        except Exception as e:
            return _fail(
                row, STATUS_IMAGE_DOWNLOAD_FAILED,
                f"manual image download failed: {e!s}",
                t0, costs, metadata,
            )

        try:
            # Pillow rendering is CPU-bound — offload so the event loop stays
            # free to advance other rows during the overlay paint.
            composed_bytes = await asyncio.to_thread(
                overlay_text_on_image_bytes,
                source_image_bytes,
                row.text,
                aspect_ratio=row.aspect_ratio,
            )
            composed_upload = await clients.storage.upload_bytes(
                composed_bytes,
                key=f"bulkvid/text_on_img/{slug}/composed.png",
                content_type="image/png",
            )
            costs.storage += composed_upload.cost_usd
            composed_url = composed_upload.url
            metadata["composed_image_bytes"] = len(composed_bytes)
        except Exception as e:
            _log.exception("text_overlay_failed", error=str(e)[:200])
            return _fail(
                row, STATUS_VIDEO_ASSEMBLY_FAILED,
                f"text overlay failed: {e!s}",
                t0, costs, metadata,
            )

        # ─── Stage 6: composed image -> video (Rendi, with VO if any) ───
        try:
            video = await clients.rendi.image_to_video_fit(
                image_url=composed_url,
                audio_url=vo_url,    # None -> silent clip
                output_filename="v1.mp4",
                aspect_ratio=normalize_aspect_ratio(row.aspect_ratio),
            )
            costs.rendi += video.cost_usd
        except Exception as e:
            return _fail(row, STATUS_VIDEO_ASSEMBLY_FAILED, str(e), t0, costs, metadata)

        # ─── Stage 7: persist video to storage ───
        try:
            data = await _download(video.url, timeout=180.0)
            up = await clients.storage.upload_bytes(
                data,
                key=f"bulkvid/videos/{slug}/v1.mp4",
                content_type="video/mp4",
            )
            costs.storage += up.cost_usd
            final_url = up.url
        except Exception as e:
            return _fail(row, STATUS_STORAGE_FAILED, str(e), t0, costs, metadata)

        # ─── Stage 7b: free Rendi storage (best-effort) ───
        await clients.rendi.cleanup_commands([video.command_id])

        # ─── Stage 8 (optional): ZapCap ───
        if row.zapcap and clients.zapcap is not None:
            try:
                video_bytes = await _download(final_url, timeout=180.0)
                cap_url, cost = await clients.zapcap.caption_video(
                    video_bytes=video_bytes,
                    language=lang.language,
                    filename="v1.mp4",
                    video_duration_seconds=tts_duration,
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
                _log.error("zapcap_failed_kept_original", error=str(e)[:200])
                metadata["zapcap_applied"] = False
                metadata["zapcap_error"] = str(e)[:200]
                return _ok(
                    row, [final_url], t0, costs, metadata,
                    status=STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS,
                )

        return _ok(row, [final_url], t0, costs, metadata)

    except Exception as e:
        _log.exception("row_internal_error", error=str(e))
        return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)


# ── Result builders ──────────────────────────────────────────────────────────


def _ok(
    row: TextOnImgRow,
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
    row: TextOnImgRow,
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
