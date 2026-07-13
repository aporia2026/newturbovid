"""hook_card row processor — a 9:16 hook-box slideshow with per-cell media,
optional voiceover, and background music.

The Hook_Card tab turns an article (or pasted media) into the competitor's
faceless short-video format: 1-5 background scenes cut in sequence under a FIXED
lower-third black rounded box holding one bold white hook line.

Per-cell media (cols H-L, "Manual Media 1-5") — each cell independently:
  * image URL  -> Ken Burns zoom.
  * video URL  -> used as a clip (trimmed/cropped to the scene).
  * "AI"       -> AI-generated image -> Ken Burns.
  * "AI Video" -> AI-generated image -> Seedance animation.
  * all blank  -> ``Num of Images`` (col D) AI images.

Hook (col E "Text"): verbatim, else AI-generated in the market language.
Voiceover (col F): Yes -> a script narrated from the article (like the other
tabs); the video length follows the narration and music is ducked under it.
Music (col G): a named track, "None" (silent), or blank (random).
Output name: ``Country-Vertical-<lang>-Music-RowNumber``.

Fail-soft: never raises; returns a RowResult with a status on every path.

Plan: ``_plans/2026-07-13-hook-card-tab.md``.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bulkvid.adapters.kie import (
    nano_banana_2_text_to_image,
    nearest_seedance_aspect_ratio,
    seedance_image_to_video,
)
from bulkvid.adapters.rendi import (
    SPEECH_ATEMPO,
    dimensions_for_ratio,
    normalize_aspect_ratio,
)
from bulkvid.http_download import download_image
from bulkvid.logging import get_logger, set_context
from bulkvid.models.row import (
    STATUS_ARTICLE_FETCH_FAILED,
    STATUS_INTERNAL_ERROR,
    STATUS_STORAGE_FAILED,
    STATUS_SUCCESS,
    STATUS_TTS_FAILED,
    STATUS_VIDEO_ASSEMBLY_FAILED,
    HookCardRow,
    RowResult,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.runtime_settings import (
    SETTING_SENSITIVE_APPAREL_RULES,
    SETTING_SIMPLE_SCRIPT_PROMPT,
)
from bulkvid.pipeline.card_renderer import render_hook_overlay_bytes
from bulkvid.pipeline.cartoon_prompt import NO_BRANDING, REALISTIC_STYLE
from bulkvid.pipeline.hook_card_copy import generate_hook_card_copy
from bulkvid.pipeline.hook_card_music import content_type_for, select_track
from bulkvid.pipeline.language import detect_language, reconcile_language
from bulkvid.pipeline.open_comments import classify_open_comments
from bulkvid.pipeline.safety import append_safety_block, resolve_safety
from bulkvid.pipeline.script_gen import generate_script

_log = get_logger("row")


# ── Tunables ─────────────────────────────────────────────────────────────────

HC_TOTAL_SECONDS = 8.0             # length when there is NO voiceover
HC_MAX_SCENES = 5                  # cols H-L cap
HC_IMAGE_RESOLUTION = "2K"         # crisp source frame for Ken Burns / Seedance
HC_DEFAULT_ASPECT = "9:16"
HC_FPS = 30                        # normalize every clip so mixed sources concat
HC_SEEDANCE_RESOLUTION = "720p"
HC_VO_MIN_SECONDS = 4.0            # clamp a very short/long narration
HC_VO_MAX_SECONDS = 40.0
_MIN_FINAL_BYTES = 10_000

# Gentle motion for an AI-Video scene (Seedance animates the generated still).
MOTION_PROMPT = (
    "Subtle, natural movement with a slow, gentle cinematic camera push-in. "
    "Keep the scene calm and stable — no fast motion, no cuts, no on-screen text."
)

_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv")
_AI_VIDEO_WORDS = frozenset({"aivideo", "aivid"})
_AI_IMAGE_WORDS = frozenset({"ai", "aiimage"})


def _slug(row_num: int, job_id: str | None = None) -> str:
    job_part = (job_id or "job").replace("/", "_")
    return f"{job_part}_r{row_num}_{int(time.time())}"


def _is_valid_http_url(url: str) -> bool:
    return isinstance(url, str) and url.strip().startswith(("http://", "https://"))


def _classify_media(cell: str) -> tuple[str, str | None]:
    """Map one Manual Media cell to ``(kind, url)``.

    kind ∈ {image, video, ai_image, ai_video}. A URL is classified image/video
    by extension; the keywords "AI" / "AI Video" (case/space-insensitive) force
    generation; anything else falls back to an AI image.
    """
    c = (cell or "").strip()
    key = "".join(ch for ch in c.lower() if ch.isalnum())
    if key in _AI_VIDEO_WORDS:
        return "ai_video", None
    if key in _AI_IMAGE_WORDS:
        return "ai_image", None
    if c.startswith(("http://", "https://")):
        path = c.split("?", 1)[0].lower()
        return ("video" if path.endswith(_VIDEO_EXTS) else "image"), c
    return "ai_image", None


def _compose_image_prompt(scene: str, *, safety: Any, safety_block: str) -> str:
    base = f"{REALISTIC_STYLE} {scene.strip()} {NO_BRANDING}"
    return append_safety_block(base, safety, safety_block)


def _seedance_duration(per_seconds: float) -> int:
    """Smallest Seedance-allowed duration (4/8/12) that covers a scene slot."""
    if per_seconds <= 4:
        return 4
    return 8 if per_seconds <= 8 else 12


def _music_label(track: Path | None) -> str:
    """Readable music tag for the video name: ``Piano1`` / ``NoMusic``."""
    if track is None:
        return "NoMusic"
    m = re.match(r"(.+?)_(\d+)$", track.stem)
    if m:
        base = m.group(1)
        return f"{base[:1].upper()}{base[1:]}{m.group(2)}"
    s = track.stem
    return f"{s[:1].upper()}{s[1:]}"


def _video_name(
    country: str, vertical: str, language: str, track: Path | None, row_num: int
) -> str:
    """``Country-Vertical-<lang>-Music-RowNumber``, filename-safe."""
    c = re.sub(r"[^A-Za-z0-9]", "", country) or "XX"
    v = "".join(w.capitalize() for w in re.findall(r"[A-Za-z0-9]+", vertical)) or "Video"
    lang = re.sub(r"[^A-Za-z0-9]", "", (language or "xx").lower()) or "xx"
    return f"{c}-{v}-{lang}-{_music_label(track)}-{row_num}"


@dataclass
class _Costs:
    article: float = 0.0
    language: float = 0.0
    copy: float = 0.0
    classify: float = 0.0
    script: float = 0.0
    tts: float = 0.0
    image_gen: float = 0.0
    seedance: float = 0.0
    rendi: float = 0.0
    storage: float = 0.0

    @property
    def total(self) -> float:
        return round(
            self.article + self.language + self.copy + self.classify
            + self.script + self.tts + self.image_gen + self.seedance
            + self.rendi + self.storage,
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
    seedance_aspect = nearest_seedance_aspect_ratio(aspect)
    hook_text = (row.text or "").strip()

    # ── Scene specs (per Manual Media cell; all blank -> Num of Images AI) ──
    raw = [c.strip() for c in row.manual_media if isinstance(c, str) and c.strip()]
    if raw:
        specs = [_classify_media(c) for c in raw][:HC_MAX_SCENES]
    else:
        n = max(1, min(int(row.num_images or 1), HC_MAX_SCENES))
        specs = [("ai_image", None)] * n
    num_scenes = len(specs)
    ai_scene_count = sum(1 for kind, _ in specs if kind in ("ai_image", "ai_video"))

    metadata: dict[str, Any] = {
        "row_num": row.row_num,
        "country": row.country,
        "vertical": row.vertical,
        "article_url": row.article_url,
        "aspect_ratio": aspect,
        "scenes": num_scenes,
        "scene_kinds": [k for k, _ in specs],
        "voice_over": row.voice_over,
        "music_requested": row.music or None,
        "text_provided": bool(hook_text),
        "tab": "hook_card",
    }
    _log.info(
        "row_start",
        country=row.country, vertical=row.vertical, aspect=aspect,
        scenes=num_scenes, voice_over=row.voice_over,
        scene_kinds=metadata["scene_kinds"], tab="hook_card",
    )

    try:
        want_hook = not hook_text
        need_article = want_hook or ai_scene_count > 0 or row.voice_over
        safety: Any = None
        safety_block = ""
        copy = None
        article_body = ""
        language = ""

        # ─── Stage 1: article -> language (+ safety, copy) when needed ───
        if need_article:
            try:
                art = await clients.article.fetch(row.article_url)
                costs.article += art.cost_usd
                metadata["article_source"] = art.source
                article_body = art.content
            except Exception as e:
                return _fail(row, STATUS_ARTICLE_FETCH_FAILED, str(e), t0, costs, metadata)
            try:
                lang = await detect_language(clients.openai, article_body)
                costs.language += lang.cost_usd
                lang = reconcile_language(lang, article_url=row.article_url, country=row.country)
                language = lang.language
                safety = await resolve_safety(clients.settings_store, row.vertical, row.row_num)
                metadata["safety_matched"] = safety.matched
                if safety.matched and clients.settings_store is not None:
                    safety_block = await clients.settings_store.get(SETTING_SENSITIVE_APPAREL_RULES)
                if want_hook or ai_scene_count:
                    copy = await generate_hook_card_copy(
                        clients.openai, article_body=article_body, language=language,
                        country=row.country, vertical=row.vertical,
                        open_comments=row.open_comments,
                        want_hook=want_hook, want_scenes=ai_scene_count,
                    )
                    costs.copy += copy.cost_usd
                    if want_hook:
                        hook_text = copy.hook
            except Exception as e:
                return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)
        else:
            # Fully manual (media + hook) -> still need the language for the name.
            try:
                lang = await detect_language(clients.openai, hook_text)
                costs.language += lang.cost_usd
                lang = reconcile_language(lang, article_url=row.article_url, country=row.country)
                language = lang.language
            except Exception:
                language = ""
        metadata["language"] = language
        metadata["hook_chars"] = len(hook_text)

        # ─── Stage 2: voiceover script + TTS (drives the length) ───
        vo_url: str | None = None
        total_seconds = HC_TOTAL_SECONDS
        if row.voice_over:
            try:
                analysis = await classify_open_comments(clients.openai, row.open_comments)
                costs.classify += analysis.cost_usd
                script = await generate_script(
                    clients.openai, article_body=article_body, country=row.country,
                    vertical=row.vertical, language=language, script_pattern="",
                    open_comments=analysis, settings_store=clients.settings_store,
                    prompt_setting_key=SETTING_SIMPLE_SCRIPT_PROMPT, safety=safety,
                )
                costs.script += script.cost_usd
            except Exception as e:
                return _fail(row, STATUS_INTERNAL_ERROR, f"script gen failed: {e}", t0, costs, metadata)
            try:
                tts = await clients.tts.synthesize(
                    text=script.script, language=language, voice=script.voice,
                    style_prompt=script.style_direction, country=row.country,
                )
                costs.tts += tts.cost_usd
                up = await clients.storage.upload_bytes(
                    tts.wav_bytes, key=f"bulkvid/vo/{slug}/vo.wav", content_type="audio/wav",
                )
                costs.storage += up.cost_usd
                vo_url = up.url
                # The narration is sped up by atempo at mux time -> effective len.
                total_seconds = max(
                    HC_VO_MIN_SECONDS,
                    min(HC_VO_MAX_SECONDS, tts.duration_seconds / SPEECH_ATEMPO),
                )
                metadata["vo_seconds"] = round(total_seconds, 2)
            except Exception as e:
                return _fail(row, STATUS_TTS_FAILED, str(e), t0, costs, metadata)

        per = total_seconds / num_scenes

        # ─── Stage 3: resolve each scene to a clip (parallel) ───
        # Pre-assign AI scene descriptions so the parallel builders are pure.
        ai_scenes = list(copy.scenes) if copy else []
        resolved: list[tuple[str, str | None, str | None]] = []
        ai_i = 0
        for kind, url in specs:
            scene = None
            if kind in ("ai_image", "ai_video"):
                scene = ai_scenes[ai_i] if ai_i < len(ai_scenes) else row.vertical
                ai_i += 1
            resolved.append((kind, url, scene))

        rendi_command_ids: list[str] = []

        async def _clip(idx: int, spec: tuple[str, str | None, str | None]) -> str:
            kind, url, scene = spec
            if kind == "video":
                return url or ""    # raw clip -> concat trims/normalizes it
            if kind == "image":
                assert url is not None    # classify() always sets a URL for image
                out = await clients.rendi.ken_burns_clip(
                    url, output_filename=f"kb{idx + 1}.mp4", aspect_ratio=aspect,
                    seconds=per, zoom_in=(idx % 2 == 0),
                )
                costs.rendi += out.cost_usd
                rendi_command_ids.append(out.command_id)
                return out.url
            # ai_image / ai_video: generate the still, then animate it.
            prompt = _compose_image_prompt(scene or row.vertical, safety=safety, safety_block=safety_block)
            image_url, img_cost = await nano_banana_2_text_to_image(
                clients.kie, prompt, aspect, resolution=HC_IMAGE_RESOLUTION,
            )
            costs.image_gen += img_cost
            if kind == "ai_image":
                out = await clients.rendi.ken_burns_clip(
                    image_url, output_filename=f"kb{idx + 1}.mp4", aspect_ratio=aspect,
                    seconds=per, zoom_in=(idx % 2 == 0),
                )
                costs.rendi += out.cost_usd
                rendi_command_ids.append(out.command_id)
                return out.url
            clip_url, clip_cost = await seedance_image_to_video(
                clients.kie, image_url, MOTION_PROMPT, seedance_aspect,
                duration=_seedance_duration(per), resolution=HC_SEEDANCE_RESOLUTION,
            )
            costs.seedance += clip_cost
            return clip_url

        try:
            clip_urls = list(await asyncio.gather(*[_clip(i, s) for i, s in enumerate(resolved)]))
        except Exception as e:
            return _fail(row, STATUS_VIDEO_ASSEMBLY_FAILED, f"scene build failed: {e}", t0, costs, metadata)
        if not all(clip_urls):
            return _fail(row, STATUS_VIDEO_ASSEMBLY_FAILED, "a scene produced no clip", t0, costs, metadata)

        # ─── Stage 4: concat -> one silent slideshow (fps-normalized) ───
        try:
            concat_out = await clients.rendi.concat_clips_with_audio(
                clip_urls, None, per_clip_seconds=per, output_filename="slideshow.mp4",
                aspect_ratio=aspect, fps=HC_FPS,
            )
            costs.rendi += concat_out.cost_usd
            rendi_command_ids.append(concat_out.command_id)
            slideshow_url = concat_out.url
        except Exception as e:
            return _fail(row, STATUS_VIDEO_ASSEMBLY_FAILED, f"concat failed: {e}", t0, costs, metadata)

        # ─── Stage 5: hook overlay PNG -> upload ───
        try:
            ov = await clients.storage.upload_bytes(
                render_hook_overlay_bytes(text=hook_text, width=width, height=height),
                key=f"bulkvid/hook_card_overlays/{slug}.png", content_type="image/png",
            )
            costs.storage += ov.cost_usd
            overlay_url = ov.url
        except Exception as e:
            return _fail(row, STATUS_VIDEO_ASSEMBLY_FAILED, f"overlay render failed: {e}", t0, costs, metadata)

        # ─── Stage 6: assemble audio (VO ducks music; else music; else silent) ───
        track = select_track(row.music)    # None => silent (picked "None" or empty pool)
        metadata["music_track"] = track.name if track else None
        try:
            music_url: str | None = None
            if track is not None:
                mu = await clients.storage.upload_bytes(
                    track.read_bytes(),
                    key=f"bulkvid/hook_card_music/{slug}/{track.name}",
                    content_type=content_type_for(track),
                )
                costs.storage += mu.cost_usd
                music_url = mu.url

            if row.voice_over and vo_url is not None:
                hooked = await clients.rendi.overlay_image_on_video(
                    slideshow_url, overlay_url, output_filename="hooked.mp4",
                )
                costs.rendi += hooked.cost_usd
                rendi_command_ids.append(hooked.command_id)
                if music_url is not None:
                    final_out = await clients.rendi.mix_vo_and_music(
                        hooked.url, vo_url, music_url, output_filename="final.mp4",
                    )
                else:
                    final_out = await clients.rendi.set_vo_audio(
                        hooked.url, vo_url, output_filename="final.mp4",
                    )
            elif music_url is not None:
                final_out = await clients.rendi.overlay_and_add_music(
                    slideshow_url, overlay_url, music_url, output_filename="final.mp4",
                )
            else:
                final_out = await clients.rendi.overlay_image_on_video(
                    slideshow_url, overlay_url, output_filename="final.mp4",
                )
            costs.rendi += final_out.cost_usd
            rendi_command_ids.append(final_out.command_id)
            rendi_final_url = final_out.url
        except Exception as e:
            return _fail(row, STATUS_VIDEO_ASSEMBLY_FAILED, f"final assembly failed: {e}", t0, costs, metadata)

        # ─── Stage 7: persist (name = Country-Vertical-lang-Music-RowNumber) ───
        try:
            data = await download_image(rendi_final_url, timeout=180.0)
            if len(data) < _MIN_FINAL_BYTES:
                raise ValueError(f"final video suspiciously small ({len(data)} bytes)")
            video_name = _video_name(row.country, row.vertical, language, track, row.row_num)
            up = await clients.storage.upload_bytes(
                data, key=f"bulkvid/videos/{slug}/{video_name}.mp4", content_type="video/mp4",
            )
            costs.storage += up.cost_usd
            final_url = up.url
            metadata["video_name"] = video_name
        except Exception as e:
            return _fail(row, STATUS_STORAGE_FAILED, f"video persist failed: {e}", t0, costs, metadata)

        await clients.rendi.cleanup_commands(rendi_command_ids)
        metadata["videos_produced"] = 1
        return _ok(row, [final_url], t0, costs, metadata)

    except Exception as e:
        _log.exception("row_internal_error", error=str(e))
        return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)


# ── Result builders ──────────────────────────────────────────────────────────


def _ok(row: HookCardRow, video_urls: list[str], t0: float, costs: _Costs, metadata: dict[str, Any]) -> RowResult:
    elapsed = round(time.monotonic() - t0, 3)
    metadata["cost_breakdown"] = costs.__dict__.copy()
    _log.info("row_done", status=STATUS_SUCCESS, cost_usd=costs.total, elapsed_seconds=elapsed, video_count=len(video_urls))
    return RowResult(
        row_num=row.row_num, status=STATUS_SUCCESS, video_urls=video_urls,
        cost_usd=costs.total, elapsed_seconds=elapsed, metadata=metadata,
    )


def _fail(row: HookCardRow, status: str, error: str, t0: float, costs: _Costs, metadata: dict[str, Any]) -> RowResult:
    elapsed = round(time.monotonic() - t0, 3)
    metadata["cost_breakdown"] = costs.__dict__.copy()
    _log.error("row_failed", status=status, error=error[:300], cost_usd=costs.total, elapsed_seconds=elapsed)
    return RowResult(
        row_num=row.row_num, status=status, video_urls=[], cost_usd=costs.total,
        elapsed_seconds=elapsed, error=error[:1000], metadata=metadata,
    )
