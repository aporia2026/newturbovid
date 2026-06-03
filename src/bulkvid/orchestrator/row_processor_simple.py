"""Simple row processor — one video from the user's existing image + VO.

The "simple" tab does NO image generation. It takes the user's supplied
``manual_image_url``, resizes it to the target aspect ratio ("mind the size"),
generates a voiceover, and assembles a single video. Exactly one ``Ready Video``
is written back.

Pipeline:
  1. Validate the manual image URL
  2. Parallel: article fetch + resize the manual image to the aspect ratio
  3. language detect -> classify Open Comments -> script gen
  4. Gemini TTS -> upload VO
  5. Rendi/ffmpeg stills_to_video (resized image + VO) -> 1 video
  6. Upload video to storage
  7. Free Rendi storage (best-effort)
  8. If ZapCap=Yes: caption the video

No kie.ai, no GPT-4o description, no collage method.
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
    STATUS_IMAGE_GEN_FAILED,
    STATUS_INTERNAL_ERROR,
    STATUS_STORAGE_FAILED,
    STATUS_SUCCESS,
    STATUS_TTS_FAILED,
    STATUS_VIDEO_ASSEMBLY_FAILED,
    STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS,
    RowResult,
    SimpleRow,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.pipeline.language import detect_language
from bulkvid.pipeline.open_comments import classify_open_comments
from bulkvid.pipeline.script_gen import generate_script

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
    resize: float = 0.0
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
            self.article + self.resize + self.language + self.classify
            + self.script + self.tts + self.rendi + self.zapcap + self.storage,
            6,
        )


async def process_simple_row(
    row: SimpleRow,
    clients: PipelineClients,
    *,
    job_id: str | None = None,
) -> RowResult:
    """Run the simple pipeline for one row. Returns a RowResult. Never raises."""
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
        "tab": "simple",
    }

    _log.info(
        "row_start",
        country=row.country,
        vertical=row.vertical,
        aspect=row.aspect_ratio,
        zapcap=row.zapcap,
        vo=row.voice_over,
        tab="simple",
    )

    if not _is_valid_http_url(row.manual_image_url):
        return _fail(
            row, STATUS_IMAGE_DOWNLOAD_FAILED,
            "manual_image_url is missing or not an HTTP(S) URL",
            t0, costs, metadata,
        )

    try:
        # ─── Stage 1+2 (parallel): article fetch + resize the manual image ───

        async def _fetch_article() -> str | Exception:
            try:
                art = await clients.article.fetch(row.article_url)
                costs.article += art.cost_usd
                metadata["article_chars"] = art.char_count
                metadata["article_source"] = art.source
                return art.content
            except Exception as e:
                return e

        async def _resize() -> tuple[str, str] | Exception:
            try:
                out = await clients.rendi.resize_image(
                    source_url=row.manual_image_url,
                    aspect_ratio=normalize_aspect_ratio(row.aspect_ratio),
                    output_filename="resized.png",
                )
                costs.resize += out.cost_usd
                return out.url, out.command_id
            except Exception as e:
                return e

        article_task = asyncio.create_task(_fetch_article())
        resize_task = asyncio.create_task(_resize())

        article_result = await article_task
        if isinstance(article_result, Exception):
            resize_task.cancel()
            return _fail(
                row, STATUS_ARTICLE_FETCH_FAILED, str(article_result), t0, costs, metadata
            )
        article_body: str = article_result

        resize_result = await resize_task
        if isinstance(resize_result, Exception):
            return _fail(row, STATUS_IMAGE_GEN_FAILED, str(resize_result), t0, costs, metadata)
        resized_url, resize_command_id = resize_result

        # ─── Stage 3: language detect -> classify -> script ───

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
            )
            costs.script += script.cost_usd
            metadata["language"] = lang.language
            metadata["open_comments_mode"] = analysis.mode.value
            metadata["script_word_count"] = script.word_count
            metadata["script_used_override"] = script.used_override
        except Exception as e:
            return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)

        # ─── Stage 4: TTS + VO upload ───

        vo_url: str | None = None
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
                metadata["vo_voice"] = tts.voice
                metadata["vo_duration_seconds"] = round(tts.duration_seconds, 2)
            except Exception as e:
                return _fail(row, STATUS_TTS_FAILED, str(e), t0, costs, metadata)

        # ─── Stage 5: stills_to_video (resized image + VO) ───

        try:
            aspect = normalize_aspect_ratio(row.aspect_ratio)
            if vo_url is None:
                video = await clients.rendi.image_to_silent_video(
                    image_url=resized_url, output_filename="v1.mp4", aspect_ratio=aspect,
                )
            else:
                video = await clients.rendi.stills_to_video(
                    image_url=resized_url, audio_url=vo_url,
                    output_filename="v1.mp4", aspect_ratio=aspect,
                )
            costs.rendi += video.cost_usd
        except Exception as e:
            return _fail(row, STATUS_VIDEO_ASSEMBLY_FAILED, str(e), t0, costs, metadata)

        # ─── Stage 6: persist video to storage ───

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

        # ─── Stage 6b: free Rendi storage (best-effort) ───
        await clients.rendi.cleanup_commands([resize_command_id, video.command_id])

        # ─── Stage 7 (optional): ZapCap ───

        if row.zapcap and clients.zapcap is not None:
            try:
                video_bytes = await _download(final_url, timeout=180.0)
                cap_url, cost = await clients.zapcap.caption_video(
                    video_bytes=video_bytes,
                    language=lang.language,
                    filename="v1.mp4",
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
    row: SimpleRow,
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
    row: SimpleRow,
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
