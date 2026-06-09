"""Rendi.dev adapter — FFmpeg-as-a-service.

Used for all video assembly. We do NOT bundle ffmpeg in the container
(plan §6 "Alternatives rejected"). Each ffmpeg command runs on Rendi's
infrastructure (4 vCPUs default, up to 32 by request, 10 min runtime cap on
the Pro tier).

Three command templates (plan §15 Appendix C):
  - ``resize``           "blurred background fit" for any aspect ratio
  - ``stills_to_video``  image + audio -> MP4
  - ``music_mix``        existing recipe lifted from stage_5_add_music

API quirks
----------
- Header is ``X-API-KEY`` (NOT ``Authorization: Bearer``)
- Placeholders ``{{in_N}}`` / ``{{out_N}}`` are Rendi's — keep them literal
- Failed responses carry ``error.stderr`` + ``ffmpeg_stderr`` — surface both
- Poll cadence per plan: 10s default, up to 60 polls (10 min)

Plan: ``_plans/2026-06-02-aporia-bulk-video-tool.md`` §5, §11, §15 Appendix C.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any

import httpx

from bulkvid.config import Settings, get_settings
from bulkvid.logging import get_logger

_log = get_logger("rendi")


# Cost estimate (USD). Rendi Pro is $25/mo flat + per-GB processing; per-command
# cost is small. This is the rough amortized estimate for one ~10s job
# (plan §11; refresh before each release).
COST_RENDI_COMMAND_USD = 0.01

# Auto-retry transient Rendi failures (timeouts / network) this many extra times.
# Genuine ffmpeg failures and auth errors are NOT retried.
RENDI_RETRIES = 2

# Per-provider concurrency cap. Rendi is a multi-tenant FFmpeg-as-a-service —
# the account-wide vCPU pool is finite. When 10 concurrent rows each fire a
# Rendi command at peak burst (one per ``simple`` row), Rendi's scheduler can
# kill commands silently (observed 2026-06-07: 71 of 277 rows received
# ``status=FAILED`` with empty error+stderr in an 11-minute window). The
# semaphore caps in-flight commands across ALL row processors regardless of
# the runner's ``BULKVID_MAX_CONCURRENT_ROWS``. Default 6 is intentional:
#   - 6 × 4 vCPU/command = 24 vCPU asked of Rendi at peak — meaningful
#     reduction from the today-peak of 10 (i.e. ~40 vCPU) without strangling
#     throughput on small batches.
#   - Labelled a **v1 guess**: retune from production semaphore-wait data once
#     Phase 1 ships. The right number is "small enough that Rendi never returns
#     empty-error-FAILED, large enough that semaphore waits stay <5s on the
#     P95 row."
# Plan ``_plans/2026-06-08-200-row-batch-failures.md`` §Phase 1 / Part 3.
RENDI_DEFAULT_MAX_CONCURRENT = 6

# Threshold above which we emit a semaphore-wait log line. Below this, the
# cap isn't biting — no need to log. Above this, the cap is the bottleneck
# and we want to see it. Per the post-council "instrument it" requirement.
RENDI_SEMAPHORE_WAIT_LOG_THRESHOLD_SECONDS = 1.0


# ── Errors ───────────────────────────────────────────────────────────────────


class RendiError(RuntimeError):
    """Base class for Rendi.dev errors."""


class RendiAuthError(RendiError):
    """401 — invalid or revoked X-API-KEY."""


class RendiCommandFailedError(RendiError):
    """Rendi returned status=FAILED. Message carries ffmpeg stderr (truncated)."""


class RendiTimeoutError(RendiError):
    """Command did not complete within ``max_attempts`` polls."""


# ── Aspect ratio → target dimensions ─────────────────────────────────────────
# Defaults — admin panel overrides per ratio in Phase 5.

DEFAULT_DIMENSIONS_BY_RATIO: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
    "4:5": (1080, 1350),
    "5:4": (1350, 1080),
    "3:4": (1080, 1440),
    "4:3": (1440, 1080),
    "2:3": (1080, 1620),
    "3:2": (1620, 1080),
    "21:9": (2520, 1080),
}


def dimensions_for_ratio(aspect_ratio: str) -> tuple[int, int]:
    """Return ``(width, height)`` for a Sheet aspect-ratio string.

    Handles ``9:16``, ``09:16`` (Sheets time-cast: a leading-zero like cell),
    and ``WxH`` pixel format. Falls back to 9:16 for unrecognised inputs.
    """
    s = (aspect_ratio or "").strip().lower()
    if not s or s == "auto":
        return DEFAULT_DIMENSIONS_BY_RATIO["9:16"]

    # W:H — normalise by stripping leading zeros on each side.
    if ":" in s:
        parts = s.split(":")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            normalised = f"{int(parts[0])}:{int(parts[1])}"
            if normalised in DEFAULT_DIMENSIONS_BY_RATIO:
                return DEFAULT_DIMENSIONS_BY_RATIO[normalised]

    # WxH pixel format — use as-is if both positive ints.
    if "x" in s:
        parts = s.split("x")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            w, h = int(parts[0]), int(parts[1])
            if w > 0 and h > 0:
                return (w, h)

    return DEFAULT_DIMENSIONS_BY_RATIO["9:16"]


# Aspect-ratio strings accepted by the kie image models (nano-banana-2 / gpt-image-2).
VALID_RATIO_STRINGS: frozenset[str] = frozenset(
    {"1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"}
)


def normalize_aspect_ratio(aspect_ratio: str, default: str = "9:16") -> str:
    """Map a sheet-entered size to a valid model ``aspect_ratio`` string.

    Handles ``09:16`` (Sheets time-cast), ``9:16``, and ``WxH`` pixel inputs
    (reduced via GCD). Falls back to ``default`` for anything unrecognised, so
    the image model never receives a value it would reject or treat as ``auto``.
    """
    s = (aspect_ratio or "").strip().lower()
    if not s or s == "auto":
        return default

    if ":" in s:
        parts = s.split(":")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            norm = f"{int(parts[0])}:{int(parts[1])}"
            return norm if norm in VALID_RATIO_STRINGS else default

    if "x" in s:
        parts = s.split("x")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            w, h = int(parts[0]), int(parts[1])
            if w > 0 and h > 0:
                g = math.gcd(w, h)
                norm = f"{w // g}:{h // g}"
                return norm if norm in VALID_RATIO_STRINGS else default

    return default


# ── FFmpeg command templates ─────────────────────────────────────────────────
# ``__W__`` / ``__H__`` are OUR placeholders (substituted before send).
# ``{{in_N}}`` / ``{{out_N}}`` are Rendi's (kept literal so Rendi can substitute).

_RESIZE_TEMPLATE = (
    "-i {{in_1}} "
    '-filter_complex "[0:v]split=2[bg][fg];'
    "[bg]scale=__W__:__H__:force_original_aspect_ratio=increase,"
    "crop=__W__:__H__,boxblur=30:5[bg2];"
    "[fg]scale=__W__:__H__:force_original_aspect_ratio=decrease[fg2];"
    '[bg2][fg2]overlay=(W-w)/2:(H-h)/2" '
    "{{out_1}}"
)


# Voiceover playback speed. Gemini TTS has no rate control and reads slowly, so
# we speed the audio up (pitch-preserved via ffmpeg ``atempo``). 1.0 = original;
# 1.3 ≈ 30% faster. Tune here (admin-surfaced later).
SPEECH_ATEMPO = 1.3

# Length of a video when Voice Over = No (silent image video). Tunable.
NO_VO_VIDEO_SECONDS = 10

# Image-only -> silent video of __SECS__ seconds at the target aspect. No audio
# (-an), no atempo (nothing to speed up).
_SILENT_VIDEO_TEMPLATE = (
    "-loop 1 -framerate 30 -t __SECS__ -i {{in_1}} "
    '-vf "scale=__W__:__H__:force_original_aspect_ratio=increase,crop=__W__:__H__" '
    "-c:v libx264 -tune stillimage -pix_fmt yuv420p -an {{out_1}}"
)

_STILLS_TO_VIDEO_TEMPLATE = (
    "-loop 1 -framerate 30 -i {{in_1}} -i {{in_2}} "
    # Force the requested aspect (cover + center-crop to __W__x__H__) and cap
    # the duration at 15s (trims trailing silence; -shortest stops earlier).
    '-vf "scale=__W__:__H__:force_original_aspect_ratio=increase,crop=__W__:__H__" '
    '-filter:a "atempo=__TEMPO__" '
    "-c:v libx264 -tune stillimage -pix_fmt yuv420p "
    "-c:a aac -b:a 192k -t 15 -shortest {{out_1}}"
)


# One-shot "fit" video: the blurred-background-fit composition (whole image
# visible, NOTHING cropped) PLUS optional voiceover, in a single ffmpeg command.
# Replaces the old resize-then-stills two-call path for the simple/4Images tabs,
# where the source image carries marketing text/CTA that must not be cropped.
# (Image-VO keeps the cover-crop stills template — its quadrants are generated
# to fill the frame.) Halves the Rendi round-trips: one queue wait, not two.
_FIT_VIDEO_FILTER = (
    "[0:v]split=2[bg][fg];"
    "[bg]scale=__W__:__H__:force_original_aspect_ratio=increase,"
    "crop=__W__:__H__,boxblur=30:5[bg2];"
    "[fg]scale=__W__:__H__:force_original_aspect_ratio=decrease[fg2];"
    "[bg2][fg2]overlay=(W-w)/2:(H-h)/2[v]"
)

_FIT_VIDEO_TEMPLATE = (
    "-loop 1 -framerate 30 -i {{in_1}} -i {{in_2}} "
    '-filter_complex "' + _FIT_VIDEO_FILTER + ';[1:a]atempo=__TEMPO__[a]" '
    '-map "[v]" -map "[a]" '
    "-c:v libx264 -tune stillimage -pix_fmt yuv420p "
    "-c:a aac -b:a 192k -t 15 -shortest {{out_1}}"
)

_FIT_SILENT_TEMPLATE = (
    "-loop 1 -framerate 30 -t __SECS__ -i {{in_1}} "
    '-filter_complex "' + _FIT_VIDEO_FILTER + '" '
    '-map "[v]" '
    "-c:v libx264 -tune stillimage -pix_fmt yuv420p -an {{out_1}}"
)


# Voice at 100%, background music at 30% — lifted from stage_5_add_music.
_MUSIC_MIX_TEMPLATE = (
    "-i {{in_1}} -i {{in_2}} "
    '-filter_complex "[1:a]volume=0.3[music];'
    '[0:a][music]amix=inputs=2:duration=shortest[mixed]" '
    '-map 0:v -map "[mixed]" -c:v copy -c:a aac -shortest {{out_1}}'
)


# Composite a transparent overlay PNG on top of an existing video at (0, 0).
# Used by the cartoon CTA path (``pipeline/cartoon_cta.py``): the overlay PNG
# is the same dimensions as the video with everything transparent except a
# yellow CTA pill at the bottom — the result is the cartoon video with a
# permanent CTA pill burned in.
#
# Yoav 2026-06-08: previous drafts kept failing silently on Rendi. Stripped
# the command back to the most canonical ffmpeg-docs "overlay an image on a
# video" pattern — no named filter outputs, no explicit stream mapping, no
# format pre-conversion. ffmpeg's filter graph auto-pairs the overlay
# output with the only video output, and audio is auto-mapped from input 0
# (the video) since input 1 (PNG) has none.
_OVERLAY_IMAGE_TEMPLATE = (
    "-i {{in_1}} -i {{in_2}} "
    '-filter_complex "[0:v][1:v]overlay=0:0" '
    "-c:v libx264 -pix_fmt yuv420p "
    "-c:a aac -b:a 192k -shortest {{out_1}}"
)


# Overlay a video on top of another video, with the OVERLAY's audio used
# for output. Used by the avatar tab: in_1 is the AI-generated background
# (silent), in_2 is the TikTok avatar video (carries the narration).
# Avatar is scaled to a fixed width preserving aspect ratio, then placed
# at (margin_x, height - overlay_h - margin_y) — bottom-left corner.
# ``-shortest`` means the output ends when the shorter of the two inputs
# ends; the avatar narration usually drives total length (the background
# is padded to its expected ~8 s, and audio truncation would clip mid-word).
_OVERLAY_VIDEO_BOTTOM_LEFT_TEMPLATE = (
    "-i {{in_1}} -i {{in_2}} "
    '-filter_complex "[1:v]scale=__OVERLAY_W__:-1[av];'
    '[0:v][av]overlay=__MARGIN_X__:H-h-__MARGIN_Y__" '
    "-map 0:v -map 1:a "
    "-c:v libx264 -pix_fmt yuv420p "
    "-c:a aac -b:a 192k -shortest {{out_1}}"
)


# Still-image background + avatar video overlay, in ONE ffmpeg call.
# Used by the simplified avatar tab (chat 2026-06-09 /
# ``_plans/2026-06-09-avatar-static-image-pipeline.md``): the avatar
# now sits over a STATIC image (Manual Image as-is, or a single
# kie text-to-image), not a Seedance-animated background. The image
# is held for the entire avatar audio duration via ``-loop 1`` +
# ``-shortest``. Same scale/crop logic as the existing silent-video
# template so the background fills the target aspect cleanly; same
# overlay positioning as ``_OVERLAY_VIDEO_BOTTOM_LEFT_TEMPLATE`` so
# the visual result matches the old animated path exactly.
#
# Two avatar-shape variants live below — the rectangle path is the
# default and matches the original 2026-06-09 behaviour; the circle
# path adds a centre-square crop + a yuva alpha mask (``geq``) so the
# visible overlay region is a hard-edged circle. Per-row size lives
# in ``__OVERLAY_W__`` (pixel width — caller does the canvas-fraction
# math). Plan: ``_plans/2026-06-09-avatar-overlay-size-shape.md``.

# Avatar prep — rectangle (default): just scale to the requested
# pixel width preserving aspect ratio. Same as before.
_AVATAR_PREP_RECT = "[1:v]scale=__OVERLAY_W__:-1[av]"

# Avatar prep — circle: centre-crop to a square (``min(iw,ih)``),
# scale to the target overlay width (square output), convert to
# yuva420p (gains an alpha channel), and ``geq`` the alpha as
# ``255 inside the disc of radius W/2, 0 outside``. The result is a
# circular overlay with transparent corners; ffmpeg's ``overlay``
# filter respects the alpha during composite, so corner pixels don't
# paint onto the background.
_AVATAR_PREP_CIRCLE = (
    "[1:v]crop='min(iw,ih)':'min(iw,ih)',"
    "scale=__OVERLAY_W__:__OVERLAY_W__,"
    "format=yuva420p,"
    "geq="
    "r='r(X,Y)':"
    "g='g(X,Y)':"
    "b='b(X,Y)':"
    "a='if(lte(hypot(X-W/2,Y-H/2),W/2),255,0)'"
    "[av]"
)


def _build_still_image_avatar_overlay_template(*, shape: str) -> str:
    """Assemble the full Rendi ffmpeg command for the avatar composite.

    ``shape`` is ``"rectangle"`` (default) or ``"circle"`` — anything else
    falls back to rectangle so a malformed cell never blows up the
    ffmpeg parse.
    """
    prep = _AVATAR_PREP_CIRCLE if shape == "circle" else _AVATAR_PREP_RECT
    return (
        "-loop 1 -framerate 30 -i {{in_1}} -i {{in_2}} "
        '-filter_complex "'
        "[0:v]scale=__W__:__H__:force_original_aspect_ratio=increase,"
        "crop=__W__:__H__[bg];"
        f"{prep};"
        "[bg][av]overlay=__MARGIN_X__:H-h-__MARGIN_Y__[v]"
        '" '
        '-map "[v]" -map 1:a '
        "-c:v libx264 -tune stillimage -pix_fmt yuv420p "
        "-c:a aac -b:a 192k -shortest {{out_1}}"
    )


def render_resize_command(width: int, height: int) -> str:
    return _RESIZE_TEMPLATE.replace("__W__", str(width)).replace("__H__", str(height))


def render_stills_to_video_command(
    width: int = 1080, height: int = 1920, tempo: float = SPEECH_ATEMPO
) -> str:
    return (
        _STILLS_TO_VIDEO_TEMPLATE
        .replace("__W__", str(width))
        .replace("__H__", str(height))
        .replace("__TEMPO__", str(tempo))
    )


def render_silent_video_command(
    width: int = 1080, height: int = 1920, seconds: int = NO_VO_VIDEO_SECONDS
) -> str:
    return (
        _SILENT_VIDEO_TEMPLATE
        .replace("__W__", str(width))
        .replace("__H__", str(height))
        .replace("__SECS__", str(seconds))
    )


def render_fit_video_command(
    width: int = 1080, height: int = 1920, tempo: float = SPEECH_ATEMPO
) -> str:
    return (
        _FIT_VIDEO_TEMPLATE
        .replace("__W__", str(width))
        .replace("__H__", str(height))
        .replace("__TEMPO__", str(tempo))
    )


def render_fit_silent_command(
    width: int = 1080, height: int = 1920, seconds: int = NO_VO_VIDEO_SECONDS
) -> str:
    return (
        _FIT_SILENT_TEMPLATE
        .replace("__W__", str(width))
        .replace("__H__", str(height))
        .replace("__SECS__", str(seconds))
    )


def render_music_mix_command() -> str:
    return _MUSIC_MIX_TEMPLATE


def render_overlay_image_command() -> str:
    return _OVERLAY_IMAGE_TEMPLATE


def render_overlay_video_bottom_left_command(
    *, overlay_width_px: int, margin_x: int, margin_y: int,
) -> str:
    """Bottom-left video-on-video overlay with audio from the overlay.

    Pixel-precise pin positions (no W*0.30 expressions — ffmpeg's scale
    filter rejects those). Used by the ``video with avatar`` row
    processor: caller computes ``overlay_width_px`` from the row's
    aspect ratio (e.g. 30 % of 1080 = 324 px).
    """
    return (
        _OVERLAY_VIDEO_BOTTOM_LEFT_TEMPLATE
        .replace("__OVERLAY_W__", str(overlay_width_px))
        .replace("__MARGIN_X__", str(margin_x))
        .replace("__MARGIN_Y__", str(margin_y))
    )


def render_still_image_avatar_overlay_command(
    *,
    width: int,
    height: int,
    overlay_width_px: int,
    margin_x: int,
    margin_y: int,
    shape: str = "rectangle",
) -> str:
    """Still-image background + avatar video overlay (bottom-left),
    with the avatar's audio as the only audio track.

    Used by the simplified avatar tab. The background image is held
    for the avatar audio's full length (``-loop 1`` + ``-shortest``);
    no Seedance, no concat. Output dimensions are ``width`` x ``height``
    derived from the row's aspect ratio.

    ``shape``: ``"rectangle"`` (default — today's behaviour) or
    ``"circle"``. The circle variant centre-crops the avatar to a
    square and applies a yuva ``geq`` alpha mask so the visible
    overlay region is a hard-edged disc. Plan
    ``_plans/2026-06-09-avatar-overlay-size-shape.md``.
    """
    template = _build_still_image_avatar_overlay_template(shape=shape)
    return (
        template
        .replace("__W__", str(width))
        .replace("__H__", str(height))
        .replace("__OVERLAY_W__", str(overlay_width_px))
        .replace("__MARGIN_X__", str(margin_x))
        .replace("__MARGIN_Y__", str(margin_y))
    )


def render_cartoon_concat_command(
    num_clips: int,
    per_clip_seconds: float | list[float],
    width: int = 1080,
    height: int = 1920,
    *,
    audio: bool = True,
    tempo: float = SPEECH_ATEMPO,
    total_video_seconds: float | None = None,
) -> str:
    """Build the cartoon-mode stitch command for a VARIABLE number of clips.

    Each input clip is trimmed to its own duration, forced to ``width`` x
    ``height`` (cover + center-crop), and concatenated in order. When ``audio``
    is True the voiceover is the LAST input (``in_{num_clips+1}``) and sped up
    via ``atempo``. ``{{in_N}}`` / ``{{out_1}}`` stay literal for Rendi to
    substitute.

    ``per_clip_seconds`` accepts either:
      - a single ``float`` (every clip trimmed to the same duration), or
      - a ``list[float]`` of length ``num_clips`` (each clip trimmed to its own
        duration — used by the cartoon row processor when the last shot is
        rendered at Seedance 8s to fit a long VO and the first shot stays 4s).

    Length handling:
      - ``total_video_seconds`` set: output is forced to exactly that many
        seconds via ``-t``, with a 0.3s audio fade-out so a sped-up VO that
        overruns the target is cut smoothly.
      - ``total_video_seconds`` is None: legacy behavior — output runs
        ``-shortest`` so the video tracks the (sped-up) VO length exactly. Kept
        for callers that haven't migrated.
    """
    if num_clips < 1:
        raise ValueError("render_cartoon_concat_command needs at least one clip")
    if isinstance(per_clip_seconds, list):
        if len(per_clip_seconds) != num_clips:
            raise ValueError(
                f"per_clip_seconds list length {len(per_clip_seconds)} "
                f"does not match num_clips {num_clips}"
            )
        per_durations = [float(d) for d in per_clip_seconds]
    else:
        per_durations = [float(per_clip_seconds)] * num_clips
    inputs = "".join(f"-i {{{{in_{i + 1}}}}} " for i in range(num_clips))
    if audio:
        inputs += f"-i {{{{in_{num_clips + 1}}}}} "

    trims = "".join(
        f"[{i}:v]trim=start=0:duration={per_durations[i]:.3f},setpts=PTS-STARTPTS,"
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1[v{i}];"
        for i in range(num_clips)
    )
    concat_inputs = "".join(f"[v{i}]" for i in range(num_clips))
    graph = f"{trims}{concat_inputs}concat=n={num_clips}:v=1:a=0[outv]"
    if audio:
        if total_video_seconds is not None:
            fade_start = max(0.0, float(total_video_seconds) - 0.3)
            graph += (
                f";[{num_clips}:a]atempo={tempo},"
                f"afade=t=out:st={fade_start:.3f}:d=0.300[outa]"
            )
        else:
            graph += f";[{num_clips}:a]atempo={tempo}[outa]"

    maps = '-map "[outv]" '
    codecs = "-c:v libx264 -pix_fmt yuv420p "
    if audio:
        maps += '-map "[outa]" '
        if total_video_seconds is not None:
            codecs += f"-c:a aac -b:a 192k -t {float(total_video_seconds):.3f} "
        else:
            codecs += "-c:a aac -b:a 192k -shortest "
    else:
        codecs += "-an "

    return f'{inputs}-filter_complex "{graph}" {maps}{codecs}{{{{out_1}}}}'


# ── Result ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RendiOutput:
    """Output of a high-level Rendi helper (``resize_image`` etc.).

    ``command_id`` is exposed so callers can delete the command's stored files
    once the output has been copied into our own storage. Rendi keeps outputs
    indefinitely and counts them against the account storage quota, so the
    Rendi copy is dead weight the moment a row is persisted (see
    ``delete_command_files`` / ``cleanup_commands``).
    """

    url: str
    cost_usd: float
    command_id: str


# ── Client ───────────────────────────────────────────────────────────────────


class RendiClient:
    """Async Rendi.dev client. Submit + poll, with three high-level helpers."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.rendi.dev",
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        default_vcpu: int = 4,
        default_max_run_seconds: int = 300,
        max_concurrent: int = RENDI_DEFAULT_MAX_CONCURRENT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("RendiClient requires an api_key")
        if max_concurrent < 1:
            raise ValueError("RendiClient max_concurrent must be >= 1")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
        self._default_vcpu = default_vcpu
        self._default_max_run_seconds = default_max_run_seconds
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(timeout=self._timeout)
        # Per-provider concurrency cap — see RENDI_DEFAULT_MAX_CONCURRENT.
        # Built lazily on first acquire so a client instantiated outside the
        # running asyncio loop (tests, sync import) doesn't blow up.
        self._max_concurrent = max_concurrent
        self._sem: asyncio.Semaphore | None = None

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-API-KEY": self._api_key, "Content-Type": "application/json"}

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    async def __aenter__(self) -> RendiClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def submit(
        self,
        ffmpeg_command: str,
        input_files: dict[str, str],
        output_files: dict[str, str],
        *,
        vcpu_count: int | None = None,
        max_command_run_seconds: int | None = None,
    ) -> str:
        url = f"{self._base_url}/v1/run-ffmpeg-command"
        payload = {
            "ffmpeg_command": ffmpeg_command,
            "input_files": input_files,
            "output_files": output_files,
            "vcpu_count": vcpu_count or self._default_vcpu,
            "max_command_run_seconds": max_command_run_seconds
            or self._default_max_run_seconds,
        }
        _log.info(
            "rendi_submit",
            input_count=len(input_files),
            output_count=len(output_files),
            vcpu_count=payload["vcpu_count"],
            max_run_seconds=payload["max_command_run_seconds"],
        )
        resp = await self._client.post(url, json=payload, headers=self._headers)
        if resp.status_code == 401:
            raise RendiAuthError("Rendi 401 — invalid X-API-KEY")
        if resp.status_code != 200:
            raise RendiError(
                f"Rendi submit HTTP {resp.status_code}: {resp.text[:200]}"
            )
        body = resp.json()
        command_id = body.get("command_id")
        if not command_id:
            raise RendiError(f"Rendi submit missing command_id: {body}")
        _log.info("rendi_submit_ok", command_id=command_id)
        return command_id

    async def poll(
        self,
        command_id: str,
        max_attempts: int = 60,
        delay_seconds: float = 10.0,
        output_key: str = "out_1",
    ) -> str:
        """Poll a command until success / fail / timeout. Returns the output URL."""
        url = f"{self._base_url}/v1/commands/{command_id}"

        for attempt in range(max_attempts):
            resp = await self._client.get(url, headers=self._headers)
            if resp.status_code != 200:
                if attempt == max_attempts - 1:
                    raise RendiError(
                        f"Rendi poll HTTP {resp.status_code} after {max_attempts} attempts"
                    )
                await asyncio.sleep(delay_seconds)
                continue

            body = resp.json()
            status = (body.get("status") or "").upper()

            if status == "SUCCESS":
                output_files = body.get("output_files") or {}
                output = output_files.get(output_key) or {}
                output_url = output.get("storage_url")
                if not output_url:
                    raise RendiError(
                        f"Rendi {command_id} SUCCESS but no storage_url for {output_key}: {body}"
                    )
                _log.info(
                    "rendi_poll_ok", command_id=command_id, attempts=attempt + 1
                )
                return output_url

            if status == "FAILED":
                err = body.get("error") or {}
                if isinstance(err, dict):
                    msg = err.get("message") or str(err)
                    stderr = err.get("stderr") or body.get("ffmpeg_stderr") or ""
                else:
                    msg = str(err)
                    stderr = body.get("ffmpeg_stderr") or ""
                # Capture the full body so a future FAILED shape we haven't seen
                # before (e.g. Rendi platform overload returning empty error+
                # stderr — observed 2026-06-07 on Evgeny's 277-row batch) lands
                # in the log instead of being collapsed to "{}". See
                # _plans/2026-06-08-200-row-batch-failures.md §Phase 1 / Part 1.
                _log.error(
                    "rendi_poll_failed",
                    command_id=command_id,
                    full_body=body,
                    error_message=msg,
                    ffmpeg_stderr=stderr[:500],
                )
                raise RendiCommandFailedError(
                    f"Rendi command {command_id} failed: {msg} | stderr: {stderr[:500]}"
                )

            _log.debug(
                "rendi_poll_pending",
                command_id=command_id,
                status=status,
                attempt=attempt + 1,
            )
            if attempt < max_attempts - 1:
                await asyncio.sleep(delay_seconds)

        raise RendiTimeoutError(
            f"Rendi command {command_id} did not complete within {max_attempts} attempts"
        )

    # ── High-level helpers ─────────────────────────────────────────────────

    def _get_sem(self) -> asyncio.Semaphore:
        """Lazy-init the per-provider semaphore so import-time construction
        outside an event loop is safe (tests, sync wiring code)."""
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_concurrent)
        return self._sem

    async def _submit_and_poll(
        self,
        ffmpeg_command: str,
        input_files: dict[str, str],
        output_files: dict[str, str],
        *,
        max_attempts: int,
        delay_seconds: float,
        retries: int = RENDI_RETRIES,
        retry_backoff_seconds: float = 5.0,
    ) -> tuple[str, str]:
        """Submit + poll one command, auto-retrying transient failures.

        Re-submits a fresh command on timeouts / transient submit-poll errors.
        Does NOT retry genuine ffmpeg failures or auth errors (a retry would
        just repeat them). Returns ``(output_url, command_id)``.

        Wrapped in the per-provider ``asyncio.Semaphore`` so the in-flight
        command count against Rendi is capped across the whole worker, not
        just per-row. The slot is held for the full submit+poll+retry cycle.
        """
        sem = self._get_sem()
        wait_start = time.monotonic()
        async with sem:
            waited = time.monotonic() - wait_start
            if waited >= RENDI_SEMAPHORE_WAIT_LOG_THRESHOLD_SECONDS:
                # Cap is biting — visible signal that we're rate-limited by
                # the semaphore (not by Rendi itself). Helps tune the cap.
                _log.info("rendi_semaphore_wait", queued_for_s=round(waited, 2))

            last_exc: Exception | None = None
            for attempt in range(retries + 1):
                try:
                    command_id = await self.submit(
                        ffmpeg_command, input_files, output_files
                    )
                    url = await self.poll(
                        command_id, max_attempts=max_attempts, delay_seconds=delay_seconds
                    )
                    return url, command_id
                except (RendiAuthError, RendiCommandFailedError):
                    raise
                except (RendiTimeoutError, RendiError) as e:
                    last_exc = e
                    if attempt < retries:
                        _log.warning(
                            "rendi_retry", attempt=attempt + 1, total=retries + 1,
                            error=str(e)[:200],
                        )
                        await asyncio.sleep(retry_backoff_seconds)
                        continue
                    raise
            assert last_exc is not None
            raise last_exc

    async def resize_image(
        self,
        source_url: str,
        aspect_ratio: str,
        output_filename: str = "out.png",
        *,
        max_attempts: int = 120,
        delay_seconds: float = 5.0,
    ) -> RendiOutput:
        """Apply blurred-background-fit resize to ``aspect_ratio`` (auto-retried)."""
        width, height = dimensions_for_ratio(aspect_ratio)
        url, command_id = await self._submit_and_poll(
            render_resize_command(width, height),
            {"in_1": source_url},
            {"out_1": output_filename},
            max_attempts=max_attempts,
            delay_seconds=delay_seconds,
        )
        return RendiOutput(url=url, cost_usd=COST_RENDI_COMMAND_USD, command_id=command_id)

    async def stills_to_video(
        self,
        image_url: str,
        audio_url: str,
        output_filename: str = "out.mp4",
        *,
        aspect_ratio: str = "9:16",
        max_attempts: int = 120,
        delay_seconds: float = 5.0,
    ) -> RendiOutput:
        """Build an MP4 from one image + one audio file, forced to ``aspect_ratio`` (auto-retried)."""
        width, height = dimensions_for_ratio(aspect_ratio)
        url, command_id = await self._submit_and_poll(
            render_stills_to_video_command(width, height),
            {"in_1": image_url, "in_2": audio_url},
            {"out_1": output_filename},
            max_attempts=max_attempts,
            delay_seconds=delay_seconds,
        )
        return RendiOutput(url=url, cost_usd=COST_RENDI_COMMAND_USD, command_id=command_id)

    async def image_to_silent_video(
        self,
        image_url: str,
        output_filename: str = "out.mp4",
        *,
        aspect_ratio: str = "9:16",
        seconds: int = NO_VO_VIDEO_SECONDS,
        max_attempts: int = 120,
        delay_seconds: float = 5.0,
    ) -> RendiOutput:
        """Build a SILENT MP4 from one image (Voice Over = No), forced to ``aspect_ratio`` (auto-retried)."""
        width, height = dimensions_for_ratio(aspect_ratio)
        url, command_id = await self._submit_and_poll(
            render_silent_video_command(width, height, seconds),
            {"in_1": image_url},
            {"out_1": output_filename},
            max_attempts=max_attempts,
            delay_seconds=delay_seconds,
        )
        return RendiOutput(url=url, cost_usd=COST_RENDI_COMMAND_USD, command_id=command_id)

    async def image_to_video_fit(
        self,
        image_url: str,
        audio_url: str | None = None,
        output_filename: str = "out.mp4",
        *,
        aspect_ratio: str = "9:16",
        seconds: int = NO_VO_VIDEO_SECONDS,
        max_attempts: int = 120,
        delay_seconds: float = 5.0,
    ) -> RendiOutput:
        """One-shot image -> video with blurred-background fit (no cropping).

        Scales the whole image to fit the target aspect over a blurred, zoomed
        copy of itself, then (if ``audio_url`` is given) muxes the voiceover
        sped up via atempo; ``audio_url=None`` yields a silent ``seconds`` clip.
        Replaces the resize-then-stills two-call path — one Rendi command, one
        queue wait. Auto-retried.
        """
        width, height = dimensions_for_ratio(aspect_ratio)
        if audio_url is None:
            command = render_fit_silent_command(width, height, seconds)
            inputs = {"in_1": image_url}
        else:
            command = render_fit_video_command(width, height)
            inputs = {"in_1": image_url, "in_2": audio_url}
        url, command_id = await self._submit_and_poll(
            command,
            inputs,
            {"out_1": output_filename},
            max_attempts=max_attempts,
            delay_seconds=delay_seconds,
        )
        return RendiOutput(url=url, cost_usd=COST_RENDI_COMMAND_USD, command_id=command_id)

    async def mix_music(
        self,
        video_url: str,
        music_url: str,
        output_filename: str = "out.mp4",
        *,
        max_attempts: int = 120,
        delay_seconds: float = 5.0,
    ) -> RendiOutput:
        """Mix background music (30%) under existing video audio (100%) (auto-retried)."""
        url, command_id = await self._submit_and_poll(
            render_music_mix_command(),
            {"in_1": video_url, "in_2": music_url},
            {"out_1": output_filename},
            max_attempts=max_attempts,
            delay_seconds=delay_seconds,
        )
        return RendiOutput(url=url, cost_usd=COST_RENDI_COMMAND_USD, command_id=command_id)

    async def overlay_image_on_video(
        self,
        video_url: str,
        overlay_url: str,
        output_filename: str = "out.mp4",
        *,
        max_attempts: int = 120,
        delay_seconds: float = 5.0,
    ) -> RendiOutput:
        """Composite a transparent overlay PNG on top of a video at (0, 0).

        The overlay PNG should be the SAME dimensions as the video frame —
        the alpha channel decides what passes through. Used by the cartoon
        CTA path to burn a yellow CTA pill onto the bottom of each cartoon
        video (the overlay PNG is mostly transparent with the pill at the
        bottom). Audio is copied through unchanged. Auto-retried.
        """
        url, command_id = await self._submit_and_poll(
            render_overlay_image_command(),
            {"in_1": video_url, "in_2": overlay_url},
            {"out_1": output_filename},
            max_attempts=max_attempts,
            delay_seconds=delay_seconds,
        )
        return RendiOutput(url=url, cost_usd=COST_RENDI_COMMAND_USD, command_id=command_id)

    async def overlay_video_bottom_left(
        self,
        background_video_url: str,
        overlay_video_url: str,
        output_filename: str = "out.mp4",
        *,
        overlay_width_px: int,
        margin_x: int = 40,
        margin_y: int = 40,
        max_attempts: int = 120,
        delay_seconds: float = 5.0,
    ) -> RendiOutput:
        """Composite ``overlay_video_url`` at the bottom-left of
        ``background_video_url`` and use the OVERLAY's audio for output.

        Used by the ``video with avatar`` tab: the background is the
        silent 8 s concatenated kie/Seedance clip, the overlay is the
        TikTok Symphony avatar video (carries narration audio). Scales
        the overlay to ``overlay_width_px`` preserving aspect ratio.
        Auto-retried.
        """
        url, command_id = await self._submit_and_poll(
            render_overlay_video_bottom_left_command(
                overlay_width_px=overlay_width_px,
                margin_x=margin_x,
                margin_y=margin_y,
            ),
            {"in_1": background_video_url, "in_2": overlay_video_url},
            {"out_1": output_filename},
            max_attempts=max_attempts,
            delay_seconds=delay_seconds,
        )
        return RendiOutput(url=url, cost_usd=COST_RENDI_COMMAND_USD, command_id=command_id)

    async def still_image_with_avatar_overlay(
        self,
        background_image_url: str,
        overlay_video_url: str,
        output_filename: str = "out.mp4",
        *,
        aspect_ratio: str,
        overlay_width_px: int,
        margin_x: int = 40,
        margin_y: int = 40,
        shape: str = "rectangle",
        max_attempts: int = 120,
        delay_seconds: float = 5.0,
    ) -> RendiOutput:
        """Render a video with a STILL image background and the TikTok
        avatar video composited at the bottom-left.

        Used by the avatar tab's simplified pipeline (chat 2026-06-09).
        The background image is looped for the avatar audio's full
        duration; output length = avatar duration. Avatar's audio is
        the only audio track. Scales background to the row's aspect
        ratio via cover+center-crop; scales the avatar to
        ``overlay_width_px`` preserving its aspect ratio.

        ``shape``: ``"rectangle"`` (default) keeps the avatar's native
        video aspect; ``"circle"`` centre-crops to a square and masks
        the corners with a yuva alpha so only a disc is visible.
        Per-row knob from the sheet's ``Avatar Shape`` column.

        Replaces the old (concat_clips_with_audio + overlay_video_bottom_left)
        pair for this tab — one Rendi command instead of two.
        """
        width, height = dimensions_for_ratio(aspect_ratio)
        url, command_id = await self._submit_and_poll(
            render_still_image_avatar_overlay_command(
                width=width, height=height,
                overlay_width_px=overlay_width_px,
                margin_x=margin_x, margin_y=margin_y,
                shape=shape,
            ),
            {"in_1": background_image_url, "in_2": overlay_video_url},
            {"out_1": output_filename},
            max_attempts=max_attempts,
            delay_seconds=delay_seconds,
        )
        return RendiOutput(url=url, cost_usd=COST_RENDI_COMMAND_USD, command_id=command_id)

    async def concat_clips_with_audio(
        self,
        clip_urls: list[str],
        audio_url: str | None,
        per_clip_seconds: float | list[float],
        output_filename: str = "out.mp4",
        *,
        aspect_ratio: str = "9:16",
        total_video_seconds: float | None = None,
        atempo: float = SPEECH_ATEMPO,
        max_attempts: int = 120,
        delay_seconds: float = 5.0,
    ) -> RendiOutput:
        """Stitch N clips (each trimmed to ``per_clip_seconds``) into one video.

        Used by the cartoon pipeline: each Seedance clip is trimmed and forced to
        the target aspect, the clips are concatenated in order, and — when
        ``audio_url`` is given — the voiceover is sped up by ``atempo`` and
        muxed in. Output length is controlled by ``total_video_seconds``: when
        set the video is forced to exactly that duration (audio cut with a short
        fade-out when the VO overruns), otherwise the legacy ``-shortest`` mode
        is used and the video tracks the VO length. ``audio_url=None`` yields a
        silent stitch. ``atempo`` defaults to ``SPEECH_ATEMPO`` (1.3) but the
        cartoon row processor passes a per-row value derived from the raw TTS
        length, so a short VO plays at natural speed (1.0×) instead of being
        artificially rushed. Auto-retried.
        """
        if not clip_urls:
            raise ValueError("concat_clips_with_audio requires at least one clip")
        width, height = dimensions_for_ratio(aspect_ratio)
        command = render_cartoon_concat_command(
            len(clip_urls), per_clip_seconds, width, height,
            audio=audio_url is not None,
            tempo=atempo,
            total_video_seconds=total_video_seconds,
        )
        inputs = {f"in_{i + 1}": url for i, url in enumerate(clip_urls)}
        if audio_url is not None:
            inputs[f"in_{len(clip_urls) + 1}"] = audio_url
        url, command_id = await self._submit_and_poll(
            command,
            inputs,
            {"out_1": output_filename},
            max_attempts=max_attempts,
            delay_seconds=delay_seconds,
        )
        return RendiOutput(url=url, cost_usd=COST_RENDI_COMMAND_USD, command_id=command_id)

    # ── Cleanup ────────────────────────────────────────────────────────────

    async def delete_command_files(self, command_id: str) -> None:
        """Delete all stored output files for one command.

        Rendi keeps command outputs indefinitely and counts them against the
        account storage quota (``DELETE /v1/commands/{id}/files``). We re-upload
        every finished asset to our own storage, so the Rendi copy is dead
        weight once a row is persisted. A 404 is treated as already-gone.
        Raises ``RendiError`` on any other unexpected status.
        """
        url = f"{self._base_url}/v1/commands/{command_id}/files"
        resp = await self._client.delete(url, headers=self._headers)
        if resp.status_code in (200, 204, 404):
            _log.info(
                "rendi_files_deleted", command_id=command_id, status=resp.status_code
            )
            return
        if resp.status_code == 401:
            raise RendiAuthError("Rendi 401 — invalid X-API-KEY")
        raise RendiError(
            f"Rendi delete files HTTP {resp.status_code}: {resp.text[:200]}"
        )

    async def cleanup_commands(self, command_ids: list[str]) -> None:
        """Best-effort: free each command's stored files. Never raises.

        Called by the row processors once a row's assets are safely in our own
        storage. A cleanup failure must never fail an otherwise-successful row,
        so every error here is logged and swallowed.
        """

        async def _one(command_id: str) -> None:
            try:
                await self.delete_command_files(command_id)
            except Exception as e:    # best-effort — never propagate
                _log.warning(
                    "rendi_cleanup_failed", command_id=command_id, error=str(e)[:200]
                )

        await asyncio.gather(*[_one(c) for c in command_ids])


def build_client_from_settings(settings: Settings | None = None) -> RendiClient:
    s = settings or get_settings()
    if not s.RENDI_API_KEY:
        raise ValueError("RENDI_API_KEY is empty; cannot build RendiClient")
    return RendiClient(
        api_key=s.RENDI_API_KEY,
        base_url=s.RENDI_BASE_URL,
        default_vcpu=s.RENDI_DEFAULT_VCPU,
        default_max_run_seconds=s.RENDI_MAX_COMMAND_RUN_SECONDS,
        max_concurrent=s.BULKVID_RENDI_MAX_CONCURRENT,
    )
