"""google-simple-motion row processor — N size-variant fixed-script motion ads.

A sibling of ``row_processor_simple_motion``. Where simple-motion writes an
article-driven voiceover and produces ONE 8s video, this tab speaks a FIXED,
localized two-sentence script and produces up to FOUR videos per row — the SAME
creative rendered at different sizes:

  * ``Number of Videos`` (col F, 1-4) → how many videos.
  * ``Change Size i`` (cols I-L) → the aspect of Ready Video ``i`` (cols Q-T).
  * Slot source image: 1 = Manual Image 1 as-is, 2 = Manual Image 2 as-is,
    3 = AI image (image-to-image using Image 1 as reference), 4 = AI image (ref
    Image 2). A blank manual cell falls back to a generated realistic scene.

The subject, opening (random of Explore/Learn/Read more), localized script, and
voiceover audio are generated ONCE and shared across all N videos — only the
source image + aspect differ per slot.

Each video is 2 shots from its source image (shot 1 = the source, shot 2 = an
image-to-image continuation chained on it), stretched to the shared voiceover.
This reuses ``build_pinned_cartoon_video(fixed_shots=True)`` — the same shared
verbatim builder simple-motion uses — via its ``prebuilt_vo`` (the two-sentence
WAV with a 3s silence spliced in) and ``min_video_seconds=11`` extensions.

Pipeline:
  1. Article fetch → language detect → classify Open Comments (scene context
     only; the script is fixed) → safety.
  2. Realistic scene plan (``generate_cartoon_plan``, 1 idea / 2 shots) for
     blank-cell shot-1 bases + the chained shot-2 + AI-ref prompting.
  3. Fixed script (``generate_learn_more_script``) → random opening → TTS each
     sentence → splice 3.0s silence (``pipeline.audio_gap``) → shared prebuilt VO.
  4. Per slot, concurrently: resolve the shot-1 base at the slot's aspect, render
     the CTA overlay, then ``build_pinned_cartoon_video`` (2 shots, prebuilt VO,
     11s floor, CTA, ZapCap).
  5. Write back Ready Video 1..N (slot-aligned; "" leaves a cell empty).

Plan: ``_plans/2026-07-20-google-simple-motion-tab.md``.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any

from bulkvid.adapters.kie import nano_banana_2_image_to_image
from bulkvid.adapters.rendi import dimensions_for_ratio, normalize_aspect_ratio
from bulkvid.adapters.zapcap import (
    ZapCapRenderOptions,
    ZapCapStyleOptions,
    ZapCapSubsOptions,
)
from bulkvid.logging import get_logger, set_context
from bulkvid.models.row import (
    STATUS_ARTICLE_FETCH_FAILED,
    STATUS_INTERNAL_ERROR,
    STATUS_SUCCESS,
    STATUS_TTS_FAILED,
    STATUS_VIDEO_ASSEMBLY_FAILED,
    STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS,
    GoogleSimpleMotionRow,
    RowResult,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.pinned_cartoon import (
    IMAGE_RESOLUTION,
    PinnedShotSpec,
    PrebuiltVoiceover,
    build_pinned_cartoon_video,
    fold_pinned_costs,
)
from bulkvid.orchestrator.runtime_settings import (
    SETTING_SIMPLE_MOTION_PLANNER_PROMPT,
    SIMPLE_MOTION_PLANNER_PROMPT_DEFAULT,
)
from bulkvid.pipeline.audio_gap import join_with_silence
from bulkvid.pipeline.cartoon_cta import render_cartoon_cta_overlay_bytes
from bulkvid.pipeline.cartoon_prompt import (
    REALISTIC_STYLE,
    generate_cartoon_plan,
    image_prompt_for_shot,
)
from bulkvid.pipeline.cta_defaults import default_cta_for_language
from bulkvid.pipeline.google_simple_motion import (
    OPENINGS,
    generate_learn_more_script,
)
from bulkvid.pipeline.language import detect_language, reconcile_language
from bulkvid.pipeline.open_comments import classify_open_comments
from bulkvid.pipeline.safety import resolve_safety
from bulkvid.pipeline.yt_cartoon import VO_TAIL_SECONDS

_log = get_logger("row")


# ── Tunables ─────────────────────────────────────────────────────────────────

GSM_MAX_VIDEOS = 4        # Number of Videos is clamped to 1..4
GSM_PLANNER_SHOTS = 2     # planner emits 2 scenes (shot-1 fallback + shot-2)
GSM_MIN_VIDEO_SECONDS = 11.0     # length floor (Yoav 2026-07-20)
GSM_INTER_SENTENCE_GAP_SECONDS = 3.0   # silence between the two sentences
GSM_PLANNER_IDEAS = 1
DEFAULT_ASPECT = "9:16"   # blank Change Size within 1..N (Yoav 2026-07-20)

# Each video is normally ONE shot: the source image animated for the whole video.
# But a single Seedance clip caps at 12s and Rendi won't hold a frame past a
# clip's end, so when a long voiceover pushes the video past this many seconds we
# add a SECOND shot — an image-to-image continuation chained on the source — so
# the motion fills the length without a frozen tail (never truncating the words).
GSM_SINGLE_SHOT_MAX_SECONDS = 11.9

# Motion for shot 1 (the source image as-is): a universal gentle push-in — the
# planner can't see a pasted photo, so a scene-specific motion could mismatch it.
SHOT1_MOTION = (
    "Subtle, natural movement with a slow, gentle cinematic camera push-in."
)


def _slug(row_num: int, job_id: str | None = None) -> str:
    job_part = (job_id or "job").replace("/", "_")
    return f"{job_part}_r{row_num}_{int(time.time())}"


@dataclass
class _Costs:
    article: float = 0.0
    language: float = 0.0
    classify: float = 0.0
    plan: float = 0.0
    script: float = 0.0
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
            + self.script + self.image_gen + self.tts + self.seedance
            + self.rendi + self.zapcap + self.storage,
            6,
        )


def _resolve_num_videos(raw: object) -> int:
    """Clamp Number of Videos to 1..4; garbage/blank → 1 (cheapest sane)."""
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return 1
    return max(1, min(GSM_MAX_VIDEOS, n))


def _resolve_aspects(aspect_ratios: list[str] | None, n: int) -> list[str]:
    """Aspect for each of the N slots. Blank within range → 9:16."""
    raw = list(aspect_ratios or [])
    out: list[str] = []
    for i in range(n):
        cell = raw[i].strip() if i < len(raw) and raw[i] else ""
        out.append(normalize_aspect_ratio(cell) if cell else DEFAULT_ASPECT)
    return out


async def process_google_simple_motion_row(
    row: GoogleSimpleMotionRow,
    clients: PipelineClients,
    *,
    job_id: str | None = None,
) -> RowResult:
    """Run the google-simple-motion pipeline for one row. Never raises."""
    set_context(batch_id=job_id, row_num=row.row_num)
    t0 = time.monotonic()
    costs = _Costs()
    slug = _slug(row.row_num, job_id)
    num_videos = _resolve_num_videos(row.num_videos)
    aspects = _resolve_aspects(row.aspect_ratios, num_videos)
    manual_for_slot = [
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
        "tab": "google_simple_motion",
        "num_videos": num_videos,
        "aspects": aspects,
        "manual_image_1": bool(manual_for_slot[0]),
        "manual_image_2": bool(manual_for_slot[1]),
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
        tab="google_simple_motion",
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

        # ─── Stage 2: language → classify → scene plan ───
        try:
            lang = await detect_language(clients.openai, article_body)
            costs.language += lang.cost_usd
            lang = reconcile_language(
                lang, article_url=row.article_url, country=row.country
            )

            # Open Comments feeds the scene planner for context; it does NOT
            # override the fixed script on this tab.
            analysis = await classify_open_comments(clients.openai, row.open_comments)
            costs.classify += analysis.cost_usd

            safety = await resolve_safety(
                clients.settings_store, row.vertical, row.row_num
            )
            metadata["safety_matched"] = safety.matched
            metadata["safety_keyword"] = safety.matched_keyword

            plan = await generate_cartoon_plan(
                clients.openai,
                article_body=article_body,
                country=row.country,
                vertical=row.vertical,
                language=lang.language,
                script_pattern=row.script_pattern,
                open_comments=analysis,
                num_ideas=GSM_PLANNER_IDEAS,
                num_shots=GSM_PLANNER_SHOTS,
                settings_store=clients.settings_store,
                safety=safety,
                planner_prompt_key=SETTING_SIMPLE_MOTION_PLANNER_PROMPT,
                planner_prompt_default=SIMPLE_MOTION_PLANNER_PROMPT_DEFAULT,
            )
            costs.plan += plan.cost_usd
            metadata["language"] = lang.language
            metadata["open_comments_mode"] = analysis.mode.value
            idea0 = plan.ideas[0]
            scene1 = idea0.shots[0].scene if idea0.shots else ""
            scene2 = (
                idea0.shots[1].scene if len(idea0.shots) > 1
                else scene1
            )
            shot2_motion = (
                idea0.shots[1].motion if len(idea0.shots) > 1
                else idea0.shots[0].motion if idea0.shots
                else SHOT1_MOTION
            )
            gen_shot1_motion = (
                idea0.shots[0].motion if idea0.shots else SHOT1_MOTION
            )
            style_direction = idea0.style_direction
        except Exception as e:
            return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)

        # ─── Stage 3: fixed script + shared voiceover (once for all videos) ───
        prebuilt_vo: PrebuiltVoiceover | None = None
        full_script_text = ""
        if row.voice_over:
            try:
                script = await generate_learn_more_script(
                    clients.openai,
                    article_body=article_body,
                    language=lang.language,
                )
                costs.script += script.cost_usd
                opening_idx = random.randrange(len(script.sentence1_variants))
                sentence1 = script.sentence1_variants[opening_idx]
                sentence2 = script.sentence2
                full_script_text = f"{sentence1} {sentence2}"
                metadata["subject"] = script.subject[:120]
                metadata["opening"] = OPENINGS[opening_idx] if opening_idx < len(OPENINGS) else ""

                tts1 = await clients.tts.synthesize(
                    text=sentence1, language=lang.language,
                    style_prompt=style_direction, country=row.country,
                )
                costs.tts += tts1.cost_usd
                tts2 = await clients.tts.synthesize(
                    text=sentence2, language=lang.language,
                    style_prompt=style_direction, country=row.country,
                )
                costs.tts += tts2.cost_usd
                combined_wav, vo_seconds = join_with_silence(
                    [tts1.wav_bytes, tts2.wav_bytes],
                    GSM_INTER_SENTENCE_GAP_SECONDS,
                )
                prebuilt_vo = PrebuiltVoiceover(
                    wav_bytes=combined_wav, duration_seconds=vo_seconds
                )
                metadata["vo_seconds"] = round(vo_seconds, 3)
                _log.info(
                    "gsm_vo_built",
                    opening=metadata["opening"],
                    vo_seconds=round(vo_seconds, 3),
                    gap_seconds=GSM_INTER_SENTENCE_GAP_SECONDS,
                )
            except Exception as e:
                return _fail(row, STATUS_TTS_FAILED, str(e), t0, costs, metadata)

        # Shot count: ONE clip fills the common ~11s video (a pure animation of
        # the source image); a long voiceover that pushes the video past a single
        # Seedance clip's reach gets a SECOND, chained continuation shot.
        if prebuilt_vo is not None:
            predicted_total = max(
                GSM_MIN_VIDEO_SECONDS,
                round(prebuilt_vo.duration_seconds + VO_TAIL_SECONDS, 3),
            )
        else:
            predicted_total = GSM_MIN_VIDEO_SECONDS
        num_shots = 2 if predicted_total > GSM_SINGLE_SHOT_MAX_SECONDS else 1
        metadata["num_shots"] = num_shots
        metadata["video_seconds_est"] = round(predicted_total, 3)

        # ─── Stage 4: render each size variant concurrently ───
        slot_errors: list[str] = []

        async def _build_slot(idx: int) -> str | None:
            """Build the video for slot ``idx`` at ``aspects[idx]``. None on failure."""
            nonlocal zapcap_failed
            aspect = aspects[idx]
            slot_slug = f"{slug}_v{idx + 1}"
            try:
                shot1 = await _resolve_shot1(
                    idx, aspect, manual_for_slot, scene1, gen_shot1_motion,
                    clients, costs,
                )
                shots = [shot1]
                if num_shots == 2:
                    # A long VO needs a second beat: an image-to-image continuation
                    # chained on the source (the builder generates + chains it).
                    shots.append(PinnedShotSpec(scene=scene2, motion=shot2_motion))

                # CTA overlay at THIS slot's dimensions (per-aspect render).
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
                            "gsm_cta_overlay_failed_skipped",
                            slot=idx + 1, error=str(e)[:200],
                        )

                zapcap_opts: ZapCapRenderOptions | None = None
                if cta_overlay_url:
                    zapcap_opts = ZapCapRenderOptions(
                        subs=ZapCapSubsOptions(),
                        style=ZapCapStyleOptions(top=30, font_size=36),
                    )

                res = await build_pinned_cartoon_video(
                    clients=clients,
                    slug=slot_slug,
                    pinned_script=full_script_text,
                    style_direction=style_direction,
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
                    prebuilt_vo=prebuilt_vo,
                    min_video_seconds=GSM_MIN_VIDEO_SECONDS,
                )
                fold_pinned_costs(costs, res)
                if res.zapcap_failed:
                    zapcap_failed = True
                if res.final_url:
                    return res.final_url
                slot_errors.append(f"slot {idx + 1}: {res.error or 'no video'}")
                return None
            except Exception as e:
                slot_errors.append(f"slot {idx + 1}: {str(e)[:200]}")
                _log.error("gsm_slot_failed", slot=idx + 1, error=str(e)[:200])
                return None

        results = await asyncio.gather(*[_build_slot(i) for i in range(num_videos)])
        # Slot-aligned: "" leaves Ready Video i empty, others keep their slot.
        video_urls = [url or "" for url in results]
        produced = sum(1 for u in video_urls if u)
        metadata["videos_produced"] = produced
        if slot_errors:
            metadata["slot_errors"] = slot_errors

        if produced == 0:
            detail = " | ".join(slot_errors) or "all slots returned no video"
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


async def _resolve_shot1(
    idx: int,
    aspect: str,
    manual_for_slot: list[str],
    scene1: str,
    gen_shot1_motion: str,
    clients: PipelineClients,
    costs: _Costs,
) -> PinnedShotSpec:
    """Resolve the shot-1 (source image) spec for slot ``idx``.

    Slots 1/2 use their Manual Image as-is; slots 3/4 generate an AI variation
    (image-to-image) using that Manual Image as reference. A blank Manual Image
    falls back to a generated realistic scene (the builder text-to-images it).
    """
    manual = manual_for_slot[idx % 2]        # slot 3 refs image 1, slot 4 refs image 2
    is_ai_slot = idx >= 2

    if is_ai_slot and manual:
        # AI reinterpretation of the operator's image, at this slot's aspect.
        prompt = image_prompt_for_shot(scene1, is_chained=True, style=REALISTIC_STYLE)
        url, cost = await nano_banana_2_image_to_image(
            clients.kie, manual, prompt, aspect, resolution=IMAGE_RESOLUTION,
        )
        costs.image_gen += cost
        return PinnedShotSpec(scene=scene1, motion=SHOT1_MOTION, manual_image_url=url)

    if manual:
        # Operator's image used as-is (builder downloads + re-uploads).
        return PinnedShotSpec(scene=scene1, motion=SHOT1_MOTION, manual_image_url=manual)

    # Blank cell (or AI slot with no reference): builder generates the scene.
    return PinnedShotSpec(scene=scene1, motion=gen_shot1_motion)


# ── Result builders ──────────────────────────────────────────────────────────


def _ok(
    row: GoogleSimpleMotionRow,
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
    row: GoogleSimpleMotionRow,
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
