"""Verbatim (pinned) script builder for the animated tabs.

When an operator pins an exact voiceover via Open Comments (``use this script:``
— see ``pipeline/open_comments.detect_pinned_script``), the cartoon-family tabs
must speak THOSE words, not a planner-generated narration. The flat-8s cartoon
machine and the bucketed yt-cartoon machine both assume the system writes the
voiceover and then sizes the video around it; a pinned script inverts that — the
AUDIO is fixed, so the video must grow to fit it.

This module is the single, shared place that surgery lives, so the three
processors keep their existing (heavily-tuned) generate-narration paths
byte-identical and only branch at the build step. It supports both geometries:

  * **variable** (cartoon, yt-cartoon): no operator images. The shot count is
    derived from the measured TTS duration via ``plan_pinned_shots`` (2→8 shots,
    one scene per ~5s) and the planner's scenes are sliced to fit.
  * **fixed** (simple-motion): exactly the operator's shots (their own photos or
    generated), stretched to the pinned audio length — never adding scenes we
    have no image for.

Key invariants (council-reviewed, plan
``_plans/2026-06-29-pinned-script-open-comments-all-tabs.md``):

  * The script is spoken VERBATIM — never shortened, never truncated. The video
    length follows the audio (``+`` a short dwell), with NO upper cap, because
    capping would cut the operator's words. ``override_oversize`` upstream is
    what warns about a long paste.
  * Natural delivery pace (atempo 1.0) — we fit the picture to the voice, not the
    voice to a fixed window.
  * Exactly ONE video per pinned row.
  * Same graceful degradation as the cartoon path: a failed later shot holds a
    neighbour's frame; ZapCap failure keeps the uncaptioned video.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from bulkvid.adapters.kie import (
    nano_banana_2_image_to_image,
    nano_banana_2_text_to_image,
    seedance_image_to_video,
)
from bulkvid.adapters.zapcap import ZapCapRenderOptions
from bulkvid.http_download import download_image
from bulkvid.logging import get_logger
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.pipeline.cartoon_prompt import CARTOON_STYLE, image_prompt_for_shot
from bulkvid.pipeline.yt_cartoon import (
    MIN_VIDEO_SECONDS,
    PINNED_MIN_SHOTS,
    VO_TAIL_SECONDS,
    _smallest_legal_duration,
    plan_pinned_shots,
)

_log = get_logger("pinned")

# Mirror the cartoon processors' render tiers so callers don't have to thread
# them in. Kept here (not imported from a processor) to avoid an import cycle.
IMAGE_RESOLUTION = "1K"
SEEDANCE_RESOLUTION = "720p"
# Pinned audio plays at natural pace — the video grows to the voice, so there is
# no speed-up. (Contrast the generate-narration path, which speeds a too-long VO
# to fit a fixed window.)
PINNED_ATEMPO = 1.0
# Clip length for the degenerate "pinned a script but turned VO off" case, which
# has no audio to size against.
SILENT_SHOT_SECONDS = 4


@dataclass
class PinnedShotSpec:
    """One shot for the pinned builder.

    ``manual_image_url`` empty → generate the scene (``scene`` + ``motion``);
    non-empty → use the operator's image as-is and animate it with ``motion``
    (simple-motion's pasted photos). ``scene`` is ignored for manual shots.
    """

    scene: str
    motion: str
    manual_image_url: str = ""


@dataclass
class PinnedVideoResult:
    """Outcome of one pinned build. Cost fields fold into the caller's ``_Costs``."""

    final_url: str | None
    error: str | None = None
    zapcap_failed: bool = False
    video_seconds: float = 0.0
    num_shots: int = 0
    cost_image_gen: float = 0.0
    cost_tts: float = 0.0
    cost_seedance: float = 0.0
    cost_rendi: float = 0.0
    cost_zapcap: float = 0.0
    cost_storage: float = 0.0


def fold_pinned_costs(costs: object, res: PinnedVideoResult) -> None:
    """Add a pinned build's cost components into a processor's ``_Costs``.

    Each animated processor has its own (structurally identical) ``_Costs``
    dataclass; this duck-types across all of them so the builder doesn't depend
    on any one processor's type.
    """
    costs.image_gen += res.cost_image_gen      # type: ignore[attr-defined]
    costs.tts += res.cost_tts                  # type: ignore[attr-defined]
    costs.seedance += res.cost_seedance        # type: ignore[attr-defined]
    costs.rendi += res.cost_rendi              # type: ignore[attr-defined]
    costs.zapcap += res.cost_zapcap            # type: ignore[attr-defined]
    costs.storage += res.cost_storage          # type: ignore[attr-defined]


def _even_clips(total: float, num_shots: int) -> list[float]:
    """Split ``total`` seconds evenly across ``num_shots``, drift on the last."""
    per = round(total / num_shots, 3)
    clips = [per] * num_shots
    drift = round(total - sum(clips), 3)
    clips[-1] = round(clips[-1] + drift, 3)
    return clips


async def build_pinned_cartoon_video(
    *,
    clients: PipelineClients,
    slug: str,
    pinned_script: str,
    style_direction: str,
    shots: list[PinnedShotSpec],
    language: str,
    country: str,
    aspect: str,
    voice_over: bool,
    fixed_shots: bool,
    image_style: str = CARTOON_STYLE,
    cta_overlay_url: str | None = None,
    zapcap_enabled: bool = False,
    zapcap_render_options: ZapCapRenderOptions | None = None,
) -> PinnedVideoResult:
    """Build ONE video whose voiceover is the operator's pinned script, verbatim.

    ``fixed_shots`` True keeps exactly ``shots`` (simple-motion's operator
    images); False derives the shot count from the audio via
    ``plan_pinned_shots`` and slices ``shots`` to it (cartoon / yt-cartoon).
    Never raises — returns a result with ``final_url=None`` and a populated
    ``error`` on failure so the caller can surface it like a dropped idea.
    """
    res = PinnedVideoResult(final_url=None)
    if not shots:
        res.error = "pinned build called with no shots"
        _log.error("pinned_no_shots", slug=slug)
        return res
    try:
        # ── 1. Voiceover (verbatim — no shorten, no cap) + render geometry ──
        vo_url: str | None = None
        if voice_over:
            tts = await clients.tts.synthesize(
                text=pinned_script,
                language=language,
                style_prompt=style_direction,
                country=country,
            )
            res.cost_tts += tts.cost_usd
            raw = float(tts.duration_seconds)
            vo_up = await clients.storage.upload_bytes(
                tts.wav_bytes,
                key=f"bulkvid/vo/{slug}/pinned.wav",
                content_type="audio/wav",
            )
            res.cost_storage += vo_up.cost_usd
            vo_url = vo_up.url

            if fixed_shots:
                # Keep the operator's exact shots; stretch them to the audio.
                # Same MIN floor as the variable path; no upper cap (audio wins).
                num_shots = len(shots)
                total = max(MIN_VIDEO_SECONDS, round(raw + VO_TAIL_SECONDS, 3))
                per_clip = _even_clips(total, num_shots)
                seedance_durations = [_smallest_legal_duration(p) for p in per_clip]
            else:
                plan = plan_pinned_shots(raw)
                num_shots = plan.num_shots
                total = plan.target_seconds
                per_clip = list(plan.per_clip_seconds)
                seedance_durations = list(plan.seedance_durations)
        else:
            # Pinned a script but VO is off — nothing to speak. Ship a silent
            # clip at a sane default length so the row still produces a video.
            _log.warning("pinned_script_but_vo_off", slug=slug)
            raw = 0.0
            num_shots = len(shots) if fixed_shots else PINNED_MIN_SHOTS
            total = float(num_shots * SILENT_SHOT_SECONDS)
            per_clip = _even_clips(total, num_shots)
            seedance_durations = [_smallest_legal_duration(p) for p in per_clip]

        shots_used = list(shots[:num_shots])
        while len(shots_used) < num_shots:        # pad if the planner gave fewer
            shots_used.append(shots_used[-1])
        res.num_shots = num_shots
        res.video_seconds = total

        _log.info(
            "pinned_sized",
            slug=slug,
            fixed_shots=fixed_shots,
            vo_raw_seconds=round(raw, 3),
            total_video_seconds=round(total, 3),
            num_shots=num_shots,
            per_clip_seconds=[round(p, 3) for p in per_clip],
            seedance_durations=list(seedance_durations),
            has_vo=vo_url is not None,
        )

        # ── 2. Shot images — manual used as-is, else generate (chained) ──
        image_urls: list[str] = []
        for s, spec in enumerate(shots_used):
            if spec.manual_image_url:
                try:
                    raw_img = await download_image(spec.manual_image_url, timeout=60.0)
                    up = await clients.storage.upload_bytes(
                        raw_img,
                        key=f"bulkvid/pinned_images/{slug}/shot{s + 1}.png",
                        content_type="image/png",
                    )
                    res.cost_storage += up.cost_usd
                    image_urls.append(up.url)
                    continue
                except Exception as e:
                    if not image_urls:
                        raise
                    _log.warning(
                        "pinned_manual_image_failed_held",
                        slug=slug, shot=s + 1, error=str(e)[:200],
                    )
                    image_urls.append(image_urls[-1])
                    continue

            is_chained = s > 0 and bool(image_urls)
            prompt = image_prompt_for_shot(
                spec.scene, is_chained=is_chained, style=image_style
            )
            try:
                if is_chained:
                    url, cost = await nano_banana_2_image_to_image(
                        clients.kie, image_urls[0], prompt, aspect,
                        resolution=IMAGE_RESOLUTION,
                    )
                else:
                    url, cost = await nano_banana_2_text_to_image(
                        clients.kie, prompt, aspect, resolution=IMAGE_RESOLUTION,
                    )
                res.cost_image_gen += cost
                image_urls.append(url)
            except Exception as e:
                if not image_urls:
                    raise        # first shot must succeed
                _log.warning(
                    "pinned_shot_image_failed_held",
                    slug=slug, shot=s + 1, error=str(e)[:200],
                )
                image_urls.append(image_urls[-1])

        # ── 3. Animate each shot (concurrently), gap-fill failures ──
        async def _animate(s: int, image_url: str) -> tuple[int, str | None]:
            try:
                clip_url, cost = await seedance_image_to_video(
                    clients.kie, image_url, shots_used[s].motion, aspect,
                    duration=seedance_durations[s], resolution=SEEDANCE_RESOLUTION,
                )
                res.cost_seedance += cost
                return s, clip_url
            except Exception as e:
                _log.warning(
                    "pinned_shot_animate_failed",
                    slug=slug, shot=s + 1, error=str(e)[:200],
                )
                return s, None

        animated = await asyncio.gather(
            *[_animate(s, u) for s, u in enumerate(image_urls)]
        )
        clip_by_shot = {s: url for s, url in animated}
        ordered: list[str | None] = [clip_by_shot.get(s) for s in range(len(image_urls))]
        last_good: str | None = None
        for s in range(len(ordered)):
            if ordered[s]:
                last_good = ordered[s]
            elif last_good:
                ordered[s] = last_good
        next_good: str | None = None
        for s in range(len(ordered) - 1, -1, -1):
            if ordered[s]:
                next_good = ordered[s]
            elif next_good:
                ordered[s] = next_good
        clip_urls = [c for c in ordered if c]
        if not clip_urls:
            res.error = f"no Seedance clips produced for any of {len(image_urls)} shots"
            _log.error("pinned_no_clips", slug=slug)
            return res

        # Trim per_clip to the clips that survived (a fully-dropped tail shrinks
        # the video rather than holding a black slot).
        per_clip = per_clip[:len(clip_urls)]
        total = round(sum(per_clip), 3)

        # ── 4. Stitch + overlay VO at natural pace ──
        stitched = await clients.rendi.concat_clips_with_audio(
            clip_urls,
            vo_url,
            per_clip_seconds=per_clip,
            output_filename="pinned.mp4",
            aspect_ratio=aspect,
            total_video_seconds=total,
            atempo=PINNED_ATEMPO,
        )
        res.cost_rendi += stitched.cost_usd
        cleanup_ids: list[str] = [stitched.command_id]
        video_url_for_persist = stitched.url

        # ── 4b. Optional CTA overlay (non-fatal) ──
        if cta_overlay_url:
            try:
                overlaid = await clients.rendi.overlay_image_on_video(
                    video_url=stitched.url,
                    overlay_url=cta_overlay_url,
                    output_filename="pinned_cta.mp4",
                )
                res.cost_rendi += overlaid.cost_usd
                cleanup_ids.append(overlaid.command_id)
                video_url_for_persist = overlaid.url
            except Exception as cta_err:
                _log.error(
                    "pinned_cta_overlay_failed_kept_original",
                    slug=slug, error=str(cta_err)[:300],
                )

        # ── 5. Persist, free Rendi copies ──
        data = await download_image(video_url_for_persist, timeout=180.0)
        up = await clients.storage.upload_bytes(
            data,
            key=f"bulkvid/videos/{slug}/pinned.mp4",
            content_type="video/mp4",
        )
        res.cost_storage += up.cost_usd
        final_url = up.url
        await clients.rendi.cleanup_commands(cleanup_ids)

        # ── 6. Optional ZapCap (keep uncaptioned on failure) ──
        if zapcap_enabled and clients.zapcap is not None:
            try:
                cap_url, cost = await clients.zapcap.caption_video(
                    video_bytes=data,
                    language=language,
                    filename="pinned.mp4",
                    render_options=zapcap_render_options,
                    video_duration_seconds=total,
                )
                res.cost_zapcap += cost
                cap_bytes = await download_image(cap_url, timeout=180.0)
                cap_up = await clients.storage.upload_bytes(
                    cap_bytes,
                    key=f"bulkvid/videos_captioned/{slug}/pinned.mp4",
                    content_type="video/mp4",
                )
                res.cost_storage += cap_up.cost_usd
                final_url = cap_up.url
            except Exception as e:
                res.zapcap_failed = True
                _log.error("pinned_zapcap_failed_kept_original", slug=slug, error=str(e)[:200])

        res.final_url = final_url
        return res
    except Exception as e:
        res.error = str(e)[:300]
        _log.error("pinned_build_failed", slug=slug, error=res.error)
        return res
