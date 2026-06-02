"""4Images-VO2 row processor — user-supplied images variant.

Implements the simpler pipeline from plan §5 ("Per-row pipeline (4Images-VO2)"):

  1. Parallel kickoff:
     1a. Article fetch
     1b. URL validation for the first ``how_many`` supplied images
  2. After 1b: Rendi resize each image to the target aspect ratio          (parallel)
  3. After 1a: language detect -> classify Open Comments -> script gen
  4. After 3:  Gemini TTS -> upload VO
  5. After 2 AND 4: Rendi stills_to_video for each image                   (parallel)
  6. After 5: upload videos to storage                                     (parallel)
  7. If ZapCap=Yes: caption each video                                     (parallel)
  8. Compile result with cost + metadata

No kie.ai, no GPT-4o image description, no collage method — the user already
chose what they want to see in each frame.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

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
    FourImagesVO2Row,
    RowResult,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.pipeline.language import detect_language
from bulkvid.pipeline.open_comments import classify_open_comments
from bulkvid.pipeline.script_gen import generate_script

_log = get_logger("row")


# ── Helpers ──────────────────────────────────────────────────────────────────


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


# ── Public entrypoint ────────────────────────────────────────────────────────


async def process_4images_vo2_row(
    row: FourImagesVO2Row,
    clients: PipelineClients,
    *,
    job_id: str | None = None,
) -> RowResult:
    """Run the 4Images-VO2 pipeline for a single row.

    The user supplies up to 4 image URLs and a ``how_many`` count. We use
    exactly the first ``how_many`` images, resize each one to the target
    aspect ratio, then build N videos.

    Returns a RowResult with status, video URLs, cost, elapsed, and metadata.
    Never raises.
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
        "how_many": row.how_many,
        "tab": "4Images-VO2",
    }

    _log.info(
        "row_start",
        country=row.country,
        vertical=row.vertical,
        aspect=row.aspect_ratio,
        zapcap=row.zapcap,
        vo=row.voice_over,
        how_many=row.how_many,
    )

    # ── Pre-flight: validate how_many + URLs ───────────────────────────────
    if row.how_many < 1 or row.how_many > 4:
        return _fail(
            row, STATUS_INTERNAL_ERROR,
            f"how_many must be 1..4 (got {row.how_many})",
            t0, costs, metadata,
        )

    selected_urls = row.image_urls[: row.how_many]
    invalid = [u for u in selected_urls if not _is_valid_http_url(u)]
    if invalid or len(selected_urls) != row.how_many:
        return _fail(
            row, STATUS_IMAGE_DOWNLOAD_FAILED,
            f"Need {row.how_many} valid HTTP image URLs; got "
            f"{len(selected_urls) - len(invalid)} valid",
            t0, costs, metadata,
        )

    try:
        # ─── Stage 1+2 (parallel): article fetch + resize images ───

        async def _fetch_article() -> str | Exception:
            try:
                art = await clients.article.fetch(row.article_url)
                costs.article += art.cost_usd
                metadata["article_chars"] = art.char_count
                metadata["article_source"] = art.source
                return art.content
            except Exception as e:
                return e

        async def _resize_one(idx: int, src_url: str) -> str | Exception:
            try:
                url, cost = await clients.rendi.resize_image(
                    source_url=src_url,
                    aspect_ratio=row.aspect_ratio,
                    output_filename=f"resized_{idx + 1}.png",
                )
                costs.resize += cost
                return url
            except Exception as e:
                return e

        article_task = asyncio.create_task(_fetch_article())
        resize_tasks = [
            asyncio.create_task(_resize_one(i, u)) for i, u in enumerate(selected_urls)
        ]

        article_result = await article_task
        if isinstance(article_result, Exception):
            # Cancel remaining resize work — its output won't be used.
            for t in resize_tasks:
                t.cancel()
            return _fail(
                row, STATUS_ARTICLE_FETCH_FAILED, str(article_result), t0, costs, metadata
            )
        article_body: str = article_result

        resize_results = await asyncio.gather(*resize_tasks, return_exceptions=False)
        for r in resize_results:
            if isinstance(r, Exception):
                return _fail(row, STATUS_IMAGE_GEN_FAILED, str(r), t0, costs, metadata)
        resized_urls: list[str] = list(resize_results)

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

        if row.voice_over:
            try:
                tts = await clients.tts.synthesize(
                    text=script.script,
                    language=lang.language,
                    style_prompt=script.style_direction,
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
        else:
            # VO=No is rare but valid on this tab. Skip TTS for Phase 3.
            return _fail(
                row, STATUS_INTERNAL_ERROR,
                "VO=No is not supported yet (Phase 3 limitation)",
                t0, costs, metadata,
            )

        # ─── Stage 5 (parallel): Rendi stills_to_video × N ───

        async def _make_video(idx: int, image_url: str) -> str:
            url, cost = await clients.rendi.stills_to_video(
                image_url=image_url,
                audio_url=vo_url,
                output_filename=f"v{idx + 1}.mp4",
            )
            costs.rendi += cost
            return url

        try:
            rendi_urls = await asyncio.gather(
                *[_make_video(i, u) for i, u in enumerate(resized_urls)]
            )
        except Exception as e:
            return _fail(row, STATUS_VIDEO_ASSEMBLY_FAILED, str(e), t0, costs, metadata)

        # ─── Stage 6 (parallel): persist videos to storage ───

        async def _persist(idx: int, rendi_url: str) -> str:
            data = await _download(rendi_url, timeout=180.0)
            up = await clients.storage.upload_bytes(
                data,
                key=f"bulkvid/videos/{slug}/v{idx + 1}.mp4",
                content_type="video/mp4",
            )
            costs.storage += up.cost_usd
            return up.url

        try:
            final_urls = await asyncio.gather(
                *[_persist(i, u) for i, u in enumerate(rendi_urls)]
            )
        except Exception as e:
            return _fail(row, STATUS_STORAGE_FAILED, str(e), t0, costs, metadata)

        # ─── Stage 7 (optional): ZapCap ───

        if row.zapcap and clients.zapcap is not None:
            async def _caption(idx: int, video_url: str) -> str:
                video_bytes = await _download(video_url, timeout=180.0)
                cap_url, cost = await clients.zapcap.caption_video(
                    video_bytes=video_bytes,
                    language=lang.language,
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
                    *[_caption(i, v) for i, v in enumerate(final_urls)]
                )
                final_urls = list(captioned)
                metadata["zapcap_applied"] = True
            except Exception as e:
                _log.error("zapcap_failed_kept_originals", error=str(e)[:200])
                metadata["zapcap_applied"] = False
                metadata["zapcap_error"] = str(e)[:200]
                return _ok(
                    row, final_urls, t0, costs, metadata,
                    status=STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS,
                )

        return _ok(row, final_urls, t0, costs, metadata)

    except Exception as e:
        _log.exception("row_internal_error", error=str(e))
        return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)


# ── Result builders ──────────────────────────────────────────────────────────


def _ok(
    row: FourImagesVO2Row,
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
    row: FourImagesVO2Row,
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
