"""hook_card row processor — a 9:16 hook-box slideshow with background music.

The Hook_Card tab turns an article (or pasted images) into the competitor's
faceless short-video format: 1-5 background scenes, each Ken Burns zoomed, cut
in sequence under a FIXED lower-third black semi-transparent rounded box holding
one bold white hook line, with a bundled royalty-free track. No voiceover.

Pipeline (lean — reuses the shared kie image + Rendi helpers):
  1. Decide scenes: pasted Manual Images (cols F-J) win; else AI-generate
     ``Num of Images`` (col D) realistic photos from the article.
  2. Decide the hook: the Text cell (col E) verbatim; else AI-generate one hook
     in the market language. The copy call is SKIPPED entirely when both the
     Text cell and manual images are supplied.
  3. Ken Burns each scene into a silent clip (parallel).
  4. Concat the clips into one silent slideshow (clips are force-normalized by
     the concat command, so mixed manual/AI dimensions are safe).
  5. Render the transparent hook-overlay PNG; overlay it AND set a rotated music
     track in one final call (silent overlay when no track is bundled).
  6. Persist to our storage; free the Rendi copies.

Fail-soft: never raises; returns a RowResult with a status on every path.

Plan: ``_plans/2026-07-13-hook-card-tab.md``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from bulkvid.adapters.kie import nano_banana_2_text_to_image
from bulkvid.adapters.rendi import dimensions_for_ratio, normalize_aspect_ratio
from bulkvid.http_download import download_image
from bulkvid.logging import get_logger, set_context
from bulkvid.models.row import (
    STATUS_ARTICLE_FETCH_FAILED,
    STATUS_IMAGE_DOWNLOAD_FAILED,
    STATUS_IMAGE_GEN_FAILED,
    STATUS_INTERNAL_ERROR,
    STATUS_STORAGE_FAILED,
    STATUS_SUCCESS,
    STATUS_VIDEO_ASSEMBLY_FAILED,
    HookCardRow,
    RowResult,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.runtime_settings import SETTING_SENSITIVE_APPAREL_RULES
from bulkvid.pipeline.card_renderer import render_hook_overlay_bytes
from bulkvid.pipeline.cartoon_prompt import NO_BRANDING, REALISTIC_STYLE
from bulkvid.pipeline.hook_card_copy import generate_hook_card_copy
from bulkvid.pipeline.hook_card_music import content_type_for, select_track
from bulkvid.pipeline.language import detect_language, reconcile_language
from bulkvid.pipeline.safety import append_safety_block, resolve_safety

_log = get_logger("row")


# ── Tunables ─────────────────────────────────────────────────────────────────

HC_TOTAL_SECONDS = 8.0             # matches the reference creatives
HC_MAX_SCENES = 5                  # cols F-J / Num of Images cap
HC_IMAGE_RESOLUTION = "2K"         # crisp source frame for the Ken Burns push
HC_DEFAULT_ASPECT = "9:16"         # Hook_Card spec default
_MIN_FINAL_BYTES = 10_000          # a valid mp4 is never this small


def _slug(row_num: int, job_id: str | None = None) -> str:
    job_part = (job_id or "job").replace("/", "_")
    return f"{job_part}_r{row_num}_{int(time.time())}"


def _is_valid_http_url(url: str) -> bool:
    return isinstance(url, str) and url.strip().startswith(("http://", "https://"))


def _compose_image_prompt(scene: str, *, safety: Any, safety_block: str) -> str:
    """Realistic style + scene + no-brands, plus the sensitive-apparel block on
    a safety match (mirrors motion_ads)."""
    base = f"{REALISTIC_STYLE} {scene.strip()} {NO_BRANDING}"
    return append_safety_block(base, safety, safety_block)


@dataclass
class _Costs:
    article: float = 0.0
    language: float = 0.0
    copy: float = 0.0
    image_gen: float = 0.0
    rendi: float = 0.0
    storage: float = 0.0

    @property
    def total(self) -> float:
        return round(
            self.article + self.language + self.copy
            + self.image_gen + self.rendi + self.storage,
            6,
        )


async def process_hook_card_row(
    row: HookCardRow,
    clients: PipelineClients,
    *,
    job_id: str | None = None,
) -> RowResult:
    """Run the Hook_Card pipeline for one row. Returns a RowResult. Never raises."""
    set_context(batch_id=job_id, row_num=row.row_num)
    t0 = time.monotonic()
    costs = _Costs()
    slug = _slug(row.row_num, job_id)
    aspect = normalize_aspect_ratio(row.aspect_ratio, default=HC_DEFAULT_ASPECT)
    width, height = dimensions_for_ratio(aspect)

    # Manual images: any filled cell wins (all-or-nothing vs AI). A cell that is
    # filled but NOT a valid http URL is an operator error, not a silent AI
    # fallback — fail loudly like the 4images tab.
    raw_manual = [u.strip() for u in row.manual_image_urls if isinstance(u, str) and u.strip()]
    manual = [u for u in raw_manual if _is_valid_http_url(u)]

    hook_text = (row.text or "").strip()
    num_scenes = (
        min(len(manual), HC_MAX_SCENES) if manual
        else max(1, min(int(row.num_images or 1), HC_MAX_SCENES))
    )

    metadata: dict[str, Any] = {
        "row_num": row.row_num,
        "country": row.country,
        "vertical": row.vertical,
        "article_url": row.article_url,
        "aspect_ratio": aspect,
        "num_images": row.num_images,
        "manual_images": len(manual),
        "text_provided": bool(hook_text),
        "music_requested": row.music or None,
        "tab": "hook_card",
    }

    _log.info(
        "row_start",
        country=row.country,
        vertical=row.vertical,
        aspect=aspect,
        num_scenes=num_scenes,
        manual_images=len(manual),
        text_provided=bool(hook_text),
        tab="hook_card",
    )

    if raw_manual and len(manual) != len(raw_manual):
        return _fail(
            row, STATUS_IMAGE_DOWNLOAD_FAILED,
            "one or more Manual Image cells are not valid http(s) URLs",
            t0, costs, metadata,
        )

    try:
        want_hook = not hook_text
        want_scenes = 0 if manual else num_scenes
        copy = None
        safety: Any = None
        safety_block = ""

        # ─── Stage 1: article -> language -> copy (only when we must generate) ───
        if want_hook or want_scenes:
            try:
                art = await clients.article.fetch(row.article_url)
                costs.article += art.cost_usd
                metadata["article_chars"] = art.char_count
                metadata["article_source"] = art.source
                article_body: str = art.content
            except Exception as e:
                return _fail(
                    row, STATUS_ARTICLE_FETCH_FAILED, str(e), t0, costs, metadata
                )

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
                if safety.matched and clients.settings_store is not None:
                    safety_block = await clients.settings_store.get(
                        SETTING_SENSITIVE_APPAREL_RULES
                    )

                copy = await generate_hook_card_copy(
                    clients.openai,
                    article_body=article_body,
                    language=lang.language,
                    country=row.country,
                    vertical=row.vertical,
                    open_comments=row.open_comments,
                    want_hook=want_hook,
                    want_scenes=want_scenes,
                )
                costs.copy += copy.cost_usd
                metadata["language"] = lang.language
            except Exception as e:
                return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)

            if want_hook:
                hook_text = copy.hook
        metadata["hook_chars"] = len(hook_text)

        # ─── Stage 2: resolve the scene images (manual as-is, or AI-generate) ───
        if manual:
            scene_image_urls = manual[:num_scenes]
            metadata["image_source"] = "manual"
        else:
            assert copy is not None    # want_scenes>0 -> Stage 1 ran

            async def _gen(idx: int, scene: str) -> str:
                prompt = _compose_image_prompt(
                    scene, safety=safety, safety_block=safety_block
                )
                image_url, img_cost = await nano_banana_2_text_to_image(
                    clients.kie, prompt, aspect, resolution=HC_IMAGE_RESOLUTION,
                )
                costs.image_gen += img_cost
                return image_url

            try:
                scene_image_urls = list(
                    await asyncio.gather(
                        *[_gen(i, s) for i, s in enumerate(copy.scenes)]
                    )
                )
                metadata["image_source"] = "generated"
            except Exception as e:
                return _fail(
                    row, STATUS_IMAGE_GEN_FAILED,
                    f"image generation failed: {e}", t0, costs, metadata,
                )

        if not scene_image_urls:
            return _fail(
                row, STATUS_VIDEO_ASSEMBLY_FAILED,
                "no scene images resolved", t0, costs, metadata,
            )

        per = HC_TOTAL_SECONDS / len(scene_image_urls)
        rendi_command_ids: list[str] = []

        # ─── Stage 3: Ken Burns each scene into a silent clip (parallel) ───
        async def _ken_burns(idx: int, image_url: str) -> tuple[str, str]:
            out = await clients.rendi.ken_burns_clip(
                image_url,
                output_filename=f"kb{idx + 1}.mp4",
                aspect_ratio=aspect,
                seconds=per,
                zoom_in=(idx % 2 == 0),    # alternate push-in / pull-out
            )
            costs.rendi += out.cost_usd
            return out.url, out.command_id

        try:
            kb_results = await asyncio.gather(
                *[_ken_burns(i, u) for i, u in enumerate(scene_image_urls)]
            )
        except Exception as e:
            return _fail(
                row, STATUS_VIDEO_ASSEMBLY_FAILED,
                f"ken burns failed: {e}", t0, costs, metadata,
            )
        clip_urls = [url for url, _ in kb_results]
        rendi_command_ids.extend(cid for _, cid in kb_results)

        # ─── Stage 4: concat clips -> one silent slideshow ───
        try:
            concat_out = await clients.rendi.concat_clips_with_audio(
                clip_urls,
                None,    # silent stitch
                per_clip_seconds=per,
                output_filename="slideshow.mp4",
                aspect_ratio=aspect,
            )
            costs.rendi += concat_out.cost_usd
            rendi_command_ids.append(concat_out.command_id)
            slideshow_url = concat_out.url
        except Exception as e:
            return _fail(
                row, STATUS_VIDEO_ASSEMBLY_FAILED,
                f"concat failed: {e}", t0, costs, metadata,
            )

        # ─── Stage 5: render the hook overlay PNG -> upload ───
        try:
            overlay_bytes = render_hook_overlay_bytes(
                text=hook_text, width=width, height=height
            )
            ov = await clients.storage.upload_bytes(
                overlay_bytes,
                key=f"bulkvid/hook_card_overlays/{slug}.png",
                content_type="image/png",
            )
            costs.storage += ov.cost_usd
            overlay_url = ov.url
        except Exception as e:
            return _fail(
                row, STATUS_VIDEO_ASSEMBLY_FAILED,
                f"overlay render failed: {e}", t0, costs, metadata,
            )

        # ─── Stage 6: overlay hook + set music (silent overlay if no track) ───
        try:
            track = select_track(row.music)    # named track, or random when blank
            if track is not None:
                mu = await clients.storage.upload_bytes(
                    track.read_bytes(),
                    key=f"bulkvid/hook_card_music/{slug}/{track.name}",
                    content_type=content_type_for(track),
                )
                costs.storage += mu.cost_usd
                final_out = await clients.rendi.overlay_and_add_music(
                    slideshow_url, overlay_url, mu.url,
                    output_filename="final.mp4",
                )
                metadata["music_track"] = track.name
            else:
                final_out = await clients.rendi.overlay_image_on_video(
                    slideshow_url, overlay_url, output_filename="final.mp4",
                )
                metadata["music_track"] = None
            costs.rendi += final_out.cost_usd
            rendi_command_ids.append(final_out.command_id)
            rendi_final_url = final_out.url
        except Exception as e:
            return _fail(
                row, STATUS_VIDEO_ASSEMBLY_FAILED,
                f"final assembly failed: {e}", t0, costs, metadata,
            )

        # ─── Stage 7: persist final to our storage ───
        try:
            data = await download_image(rendi_final_url, timeout=180.0)
            if len(data) < _MIN_FINAL_BYTES:
                raise ValueError(
                    f"final video suspiciously small ({len(data)} bytes)"
                )
            up = await clients.storage.upload_bytes(
                data,
                key=f"bulkvid/videos/{slug}/v1.mp4",
                content_type="video/mp4",
            )
            costs.storage += up.cost_usd
            final_url = up.url
        except Exception as e:
            return _fail(
                row, STATUS_STORAGE_FAILED,
                f"video persist failed: {e}", t0, costs, metadata,
            )

        # ─── Stage 8: free the Rendi copies (best-effort) ───
        await clients.rendi.cleanup_commands(rendi_command_ids)

        metadata["videos_produced"] = 1
        metadata["scene_count"] = len(scene_image_urls)
        return _ok(row, [final_url], t0, costs, metadata)

    except Exception as e:
        _log.exception("row_internal_error", error=str(e))
        return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)


# ── Result builders ──────────────────────────────────────────────────────────


def _ok(
    row: HookCardRow,
    video_urls: list[str],
    t0: float,
    costs: _Costs,
    metadata: dict[str, Any],
) -> RowResult:
    elapsed = round(time.monotonic() - t0, 3)
    metadata["cost_breakdown"] = costs.__dict__.copy()
    _log.info(
        "row_done",
        status=STATUS_SUCCESS,
        cost_usd=costs.total,
        elapsed_seconds=elapsed,
        video_count=len(video_urls),
    )
    return RowResult(
        row_num=row.row_num,
        status=STATUS_SUCCESS,
        video_urls=video_urls,
        cost_usd=costs.total,
        elapsed_seconds=elapsed,
        metadata=metadata,
    )


def _fail(
    row: HookCardRow,
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
