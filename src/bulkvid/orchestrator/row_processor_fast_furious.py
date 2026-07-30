"""fast-and-furious row processor — N TikTok/Gen-Z variations per row.

A sibling of ``row_processor_google_simple_motion`` (same column layout, same
N-video / per-slot-aspect structure, same realistic images and manual-image
support). The differences:

  * Each of the N videos is its OWN Gen-Z/TikTok creative variation — its own
    lively, fast, modern spoken script — not one shared creative rendered at N
    sizes. (google-simple-motion shares one fixed "learn more" script; this tab
    generates ``num_videos`` independent punchy scripts.)
  * ``Number of Videos`` (col F, 1-4) → how many variations. Video ``i`` uses
    ``Change Size i`` (blank → 9:16) and lands in Ready Video ``i`` (cols Q-T).
  * Manual Image 1/2 (cols D/E) are SHARED by all variations as shot 1 / shot 2;
    a blank cell generates a realistic scene per variation. They are downloaded +
    re-uploaded ONCE and reused across the variations.
  * ``use this script: <text>`` in Open Comments → EVERY variation speaks that
    exact operator text (verbatim, any language) instead of the generated one.

The narration is spoken at natural pace and each video's length is driven BY its
voiceover (no ~3s silent tail) — both come free from the shared
``build_pinned_cartoon_video`` (``fixed_shots=True``), which every variation is
routed through. That is the same proven builder simple-motion / google-simple-
motion use, so this processor stays thin: plan N Gen-Z ideas, resolve the shared
images once, then build each variation.

Pipeline:
  1. Article fetch → language detect → classify Open Comments (detects the
     ``use this script:`` override + feeds scene context) → safety.
  2. ``generate_cartoon_plan`` with the fast-and-furious prompt → N ideas, each a
     Gen-Z voiceover + 2 realistic scenes.
  3. Resolve the shared Manual Image 1/2 ONCE (download + re-upload).
  4. Per variation, concurrently: render the CTA overlay at the slot's aspect,
     then ``build_pinned_cartoon_video`` (script = the idea's Gen-Z line, or the
     operator override; shots = shared manual images or generated scenes).
  5. Write back Ready Video 1..N (slot-aligned; "" leaves a cell empty).

Plan: ``_plans/2026-07-30-fast-and-furious-tab.md``.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any

from bulkvid.adapters.rendi import dimensions_for_ratio, normalize_aspect_ratio
from bulkvid.adapters.zapcap import (
    ZapCapRenderOptions,
    ZapCapStyleOptions,
    ZapCapSubsOptions,
)
from bulkvid.http_download import download_image
from bulkvid.logging import get_logger, set_context
from bulkvid.models.row import (
    STATUS_ARTICLE_FETCH_FAILED,
    STATUS_INTERNAL_ERROR,
    STATUS_SUCCESS,
    STATUS_VIDEO_ASSEMBLY_FAILED,
    STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS,
    FastFuriousRow,
    RowResult,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.pinned_cartoon import (
    PinnedShotSpec,
    build_pinned_cartoon_video,
    fold_pinned_costs,
)
from bulkvid.orchestrator.runtime_settings import (
    FAST_FURIOUS_PLANNER_PROMPT_DEFAULT,
    SETTING_FAST_FURIOUS_PLANNER_PROMPT,
)
from bulkvid.pipeline.cartoon_cta import render_cartoon_cta_overlay_bytes
from bulkvid.pipeline.cartoon_prompt import (
    REALISTIC_STYLE,
    CartoonIdea,
    generate_cartoon_plan,
)
from bulkvid.pipeline.cta_defaults import default_cta_for_language
from bulkvid.pipeline.hook_card_music import content_type_for, select_track
from bulkvid.pipeline.language import detect_language, reconcile_language
from bulkvid.pipeline.open_comments import OpenCommentsMode, classify_open_comments
from bulkvid.pipeline.safety import resolve_safety

_log = get_logger("row")


# ── Tunables ─────────────────────────────────────────────────────────────────

FF_MAX_VIDEOS = 4         # Number of Videos is clamped to 1..4
FF_NUM_SHOTS = 2          # each variation is a 2-shot video (shot1 + shot2)
DEFAULT_ASPECT = "9:16"   # blank Change Size within 1..N

# A longer, punchy line than simple-motion's (10) so the Gen-Z narration fills
# the ~8s video. The video length is driven BY the voiceover in the shared
# builder, so there is no silent tail; this budget just shapes typical length.
FF_TARGET_WORDS = 17
FF_MIN_WORDS = 14
FF_MAX_WORDS = 22

# Length floor for the shared builder. Gen-Z variations are short/punchy, so keep
# a low floor — the video otherwise follows the voiceover exactly.
FF_MIN_VIDEO_SECONDS = 6.0

# Play the voiceover faster than natural pace for a lively, TikTok energy (the
# shared pinned builder defaults to 1.0 = calm; that read too flat). The builder
# sizes the video to the SPED length, so there's still no trailing silence.
FF_VO_ATEMPO = 1.25

# Energetic background music, ducked under the voiceover (mix_music = music at
# 30% under the full VO). Picked once per row from this upbeat subset of the
# bundled library and shared across the row's variations. Only added when the row
# has a voiceover (mix_music ducks UNDER existing audio). Reuses the hook_card
# music pool (``pipeline.hook_card_music``).
FF_MUSIC_STYLES = ("energetic", "uplifting", "electronic")

# Motion for a shot backed by a pasted (manual) image: a universal gentle push-in
# — the planner can't see the operator's photo, so a scene-specific motion could
# mismatch it. Generated shots use the planner's own scene-matched motion.
MANUAL_IMAGE_MOTION = (
    "Subtle, natural movement with a slow, gentle cinematic camera push-in."
)


def _slug(row_num: int, job_id: str | None = None) -> str:
    job_part = (job_id or "job").replace("/", "_")
    return f"{job_part}_r{row_num}_{int(time.time())}"


def _resolve_num_videos(raw: object) -> int:
    """Clamp Number of Videos to 1..4; garbage/blank → 1."""
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return 1
    return max(1, min(FF_MAX_VIDEOS, n))


def _resolve_aspects(aspect_ratios: list[str] | None, n: int) -> list[str]:
    """Aspect for each of the N variations. Blank within range → 9:16."""
    raw = list(aspect_ratios or [])
    out: list[str] = []
    for i in range(n):
        cell = raw[i].strip() if i < len(raw) and raw[i] else ""
        out.append(normalize_aspect_ratio(cell) if cell else DEFAULT_ASPECT)
    return out


@dataclass
class _Costs:
    article: float = 0.0
    language: float = 0.0
    classify: float = 0.0
    plan: float = 0.0
    image_gen: float = 0.0
    tts: float = 0.0
    seedance: float = 0.0
    rendi: float = 0.0
    zapcap: float = 0.0
    storage: float = 0.0

    @property
    def total(self) -> float:
        return round(
            self.article + self.language + self.classify + self.plan
            + self.image_gen + self.tts + self.seedance + self.rendi
            + self.zapcap + self.storage,
            6,
        )


async def process_fast_furious_row(
    row: FastFuriousRow,
    clients: PipelineClients,
    *,
    job_id: str | None = None,
) -> RowResult:
    """Run the fast-and-furious pipeline for one row. Never raises."""
    set_context(batch_id=job_id, row_num=row.row_num)
    t0 = time.monotonic()
    costs = _Costs()
    slug = _slug(row.row_num, job_id)
    num_videos = _resolve_num_videos(row.num_videos)
    aspects = _resolve_aspects(row.aspect_ratios, num_videos)
    manual_for_shot = [
        (row.manual_image_1 or "").strip(),
        (row.manual_image_2 or "").strip(),
    ]
    metadata: dict[str, Any] = {
        "row_num": row.row_num,
        "country": row.country,
        "vertical": row.vertical,
        "article_url": row.article_url,
        "voice_over": row.voice_over,
        "zapcap": row.zapcap,
        "tab": "fast_furious",
        "num_videos": num_videos,
        "aspects": aspects,
        "num_shots": FF_NUM_SHOTS,
        "manual_image_1": bool(manual_for_shot[0]),
        "manual_image_2": bool(manual_for_shot[1]),
    }
    zapcap_failed = False

    _log.info(
        "row_start",
        country=row.country,
        vertical=row.vertical,
        num_videos=num_videos,
        aspects=aspects,
        zapcap=row.zapcap,
        vo=row.voice_over,
        tab="fast_furious",
        manual_image_1=bool(manual_for_shot[0]),
        manual_image_2=bool(manual_for_shot[1]),
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

        # ─── Stage 2: language → classify → N Gen-Z ideas ───
        try:
            lang = await detect_language(clients.openai, article_body)
            costs.language += lang.cost_usd
            lang = reconcile_language(
                lang, article_url=row.article_url, country=row.country
            )

            analysis = await classify_open_comments(clients.openai, row.open_comments)
            costs.classify += analysis.cost_usd

            safety = await resolve_safety(
                clients.settings_store, row.vertical, row.row_num
            )
            metadata["safety_matched"] = safety.matched
            metadata["safety_keyword"] = safety.matched_keyword

            # One planner call → N INDEPENDENT Gen-Z variations, each a punchy
            # voiceover + 2 realistic scene/motion shots.
            plan = await generate_cartoon_plan(
                clients.openai,
                article_body=article_body,
                country=row.country,
                vertical=row.vertical,
                language=lang.language,
                script_pattern=row.script_pattern,
                open_comments=analysis,
                num_ideas=num_videos,
                num_shots=FF_NUM_SHOTS,
                settings_store=clients.settings_store,
                safety=safety,
                planner_prompt_key=SETTING_FAST_FURIOUS_PLANNER_PROMPT,
                planner_prompt_default=FAST_FURIOUS_PLANNER_PROMPT_DEFAULT,
                target_words=FF_TARGET_WORDS,
                min_words=FF_MIN_WORDS,
                max_words=FF_MAX_WORDS,
            )
            costs.plan += plan.cost_usd
            metadata["language"] = lang.language
            metadata["open_comments_mode"] = analysis.mode.value
            # ``use this script: …`` → every variation speaks the operator's exact
            # text (verbatim, any language) instead of its generated line.
            is_pinned = (
                analysis.mode is OpenCommentsMode.OVERRIDE
                and bool(analysis.override_script)
            )
            metadata["script_used_override"] = is_pinned
            if is_pinned:
                metadata["script_override_oversize"] = analysis.override_oversize
            if plan.chosen_template_id:
                metadata["chosen_template_id"] = plan.chosen_template_id
        except Exception as e:
            return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)

        # ─── Stage 3: resolve the SHARED manual images ONCE ───
        # A manual image is aspect-independent (Seedance handles the aspect), so
        # download + re-upload ONCE and reuse the stable URL for every variation.
        # A blank/failed image degrades that shot to a generated scene.
        shared_manual_urls: list[str | None] = [None, None]
        for s, murl in enumerate(manual_for_shot):
            if not murl:
                continue
            try:
                raw_img = await download_image(murl, timeout=60.0)
                up = await clients.storage.upload_bytes(
                    raw_img,
                    key=f"bulkvid/fast_furious_images/{slug}/manual_shot{s + 1}.png",
                    content_type="image/png",
                )
                costs.storage += up.cost_usd
                shared_manual_urls[s] = up.url
            except Exception as e:
                _log.warning(
                    "fast_furious_manual_image_failed_will_generate",
                    shot=s + 1, error=str(e)[:200],
                )
                shared_manual_urls[s] = None
        metadata["manual_image_1_resolved"] = shared_manual_urls[0] is not None
        metadata["manual_image_2_resolved"] = shared_manual_urls[1] is not None

        # ─── Stage 3b: pick + upload the energetic background music ONCE ───
        # One upbeat track for the whole row, ducked under each variation's VO
        # (mix_music = music @30% under the full VO). Only when there's a VO to
        # duck under; on failure the row still ships (VO only). Shared upload.
        music_url: str | None = None
        if row.voice_over:
            track = select_track(random.choice(FF_MUSIC_STYLES))
            if track is not None:
                try:
                    mu = await clients.storage.upload_bytes(
                        track.read_bytes(),
                        key=f"bulkvid/fast_furious_music/{slug}/{track.name}",
                        content_type=content_type_for(track),
                    )
                    costs.storage += mu.cost_usd
                    music_url = mu.url
                    metadata["music_track"] = track.name
                except Exception as e:
                    _log.warning(
                        "fast_furious_music_upload_failed_no_music",
                        error=str(e)[:200],
                    )
            else:
                _log.warning("fast_furious_no_music_track_available")

        # ─── Stage 4: build each variation concurrently ───
        slot_errors: list[str] = []

        async def _build_variation(idx: int) -> str | None:
            """Build variation ``idx`` at ``aspects[idx]``. None on failure."""
            nonlocal zapcap_failed
            aspect = aspects[idx]
            slot_slug = f"{slug}_v{idx + 1}"
            idea: CartoonIdea = plan.ideas[idx]
            script = (analysis.override_script or "") if is_pinned else idea.voiceover
            try:
                # Shot 1 / shot 2: the shared manual image as-is, else a generated
                # scene from this variation's plan (a blank manual_image_url makes
                # the builder text-to-image the scene; shot 2 chains on shot 1).
                shots: list[PinnedShotSpec] = []
                for s in range(FF_NUM_SHOTS):
                    manual_url = shared_manual_urls[s] if s < len(shared_manual_urls) else None
                    scene = idea.shots[s].scene if s < len(idea.shots) else ""
                    motion = (
                        MANUAL_IMAGE_MOTION if manual_url
                        else (idea.shots[s].motion if s < len(idea.shots)
                              else MANUAL_IMAGE_MOTION)
                    )
                    shots.append(
                        PinnedShotSpec(
                            scene=scene, motion=motion,
                            manual_image_url=(manual_url or ""),
                        )
                    )

                # CTA overlay at THIS variation's dimensions (per-aspect render).
                cta_overlay_url: str | None = None
                if row.cta_enabled:
                    cta_text = (row.cta_text.strip()
                                or default_cta_for_language(lang.language))
                    try:
                        w, h = dimensions_for_ratio(aspect)
                        overlay_bytes = await asyncio.to_thread(
                            render_cartoon_cta_overlay_bytes,
                            cta_text, canvas_width=w, canvas_height=h,
                        )
                        up = await clients.storage.upload_bytes(
                            overlay_bytes,
                            key=f"bulkvid/cta_overlays/{slot_slug}.png",
                            content_type="image/png",
                        )
                        costs.storage += up.cost_usd
                        cta_overlay_url = up.url
                    except Exception as e:
                        _log.error(
                            "fast_furious_cta_overlay_failed_skipped",
                            slot=idx + 1, error=str(e)[:200],
                        )

                zapcap_opts: ZapCapRenderOptions | None = None
                if cta_overlay_url:
                    zapcap_opts = ZapCapRenderOptions(
                        subs=ZapCapSubsOptions(),
                        style=ZapCapStyleOptions(top=30, font_size=36),
                    )

                # The shared builder speaks ``script`` verbatim, sped up by
                # ``vo_atempo`` for a lively read, and sizes the video TO the
                # (sped) voiceover — no silent tail. Same path the override + the
                # generated lines use.
                res = await build_pinned_cartoon_video(
                    clients=clients,
                    slug=slot_slug,
                    pinned_script=script,
                    style_direction=idea.style_direction,
                    shots=shots,
                    language=lang.language,
                    country=row.country,
                    aspect=aspect,
                    voice_over=row.voice_over,
                    fixed_shots=True,
                    image_style=REALISTIC_STYLE,
                    cta_overlay_url=cta_overlay_url,
                    zapcap_enabled=bool(row.zapcap and clients.zapcap is not None),
                    zapcap_render_options=zapcap_opts,
                    min_video_seconds=FF_MIN_VIDEO_SECONDS,
                    vo_atempo=FF_VO_ATEMPO,
                )
                fold_pinned_costs(costs, res)
                if res.zapcap_failed:
                    zapcap_failed = True
                if not res.final_url:
                    slot_errors.append(f"video {idx + 1}: {res.error or 'no video'}")
                    return None

                # Duck energetic music under the finished (VO'd, captioned) video.
                # Non-fatal — on failure ship the video without music. Done AFTER
                # ZapCap so captions transcribe the clean VO, not the music mix.
                final_url = res.final_url
                if music_url:
                    try:
                        mixed = await clients.rendi.mix_music(
                            final_url, music_url,
                            output_filename=f"{slot_slug}_music.mp4",
                        )
                        costs.rendi += mixed.cost_usd
                        data = await download_image(mixed.url, timeout=180.0)
                        up = await clients.storage.upload_bytes(
                            data,
                            key=f"bulkvid/videos_music/{slot_slug}.mp4",
                            content_type="video/mp4",
                        )
                        costs.storage += up.cost_usd
                        await clients.rendi.cleanup_commands([mixed.command_id])
                        final_url = up.url
                    except Exception as e:
                        _log.error(
                            "fast_furious_music_mix_failed_kept_original",
                            slot=idx + 1, error=str(e)[:200],
                        )
                return final_url
            except Exception as e:
                slot_errors.append(f"video {idx + 1}: {str(e)[:200]}")
                _log.error("fast_furious_slot_failed", slot=idx + 1, error=str(e)[:200])
                return None

        results = await asyncio.gather(
            *[_build_variation(i) for i in range(min(num_videos, len(plan.ideas)))]
        )
        # Slot-aligned: "" leaves Ready Video i empty, others keep their slot.
        video_urls = [url or "" for url in results]
        produced = sum(1 for u in video_urls if u)
        metadata["videos_produced"] = produced
        if slot_errors:
            metadata["slot_errors"] = slot_errors

        if produced == 0:
            detail = " | ".join(slot_errors) or "all variations returned no video"
            return _fail(
                row, STATUS_VIDEO_ASSEMBLY_FAILED,
                f"no usable videos produced — {detail}", t0, costs, metadata,
            )

        warning = (" | ".join(slot_errors))[:1000] or None
        if zapcap_failed:
            metadata["zapcap_applied"] = False
            return _ok(
                row, video_urls, t0, costs, metadata,
                status=STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS, warning=warning,
            )
        if row.zapcap and clients.zapcap is not None:
            metadata["zapcap_applied"] = True
        return _ok(row, video_urls, t0, costs, metadata, warning=warning)

    except Exception as e:
        _log.exception("row_internal_error", error=str(e))
        return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)


# ── Result builders ──────────────────────────────────────────────────────────


def _ok(
    row: FastFuriousRow,
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
        video_count=sum(1 for u in video_urls if u),
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
    row: FastFuriousRow,
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
