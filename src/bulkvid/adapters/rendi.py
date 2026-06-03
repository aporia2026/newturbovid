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


# Voice at 100%, background music at 30% — lifted from stage_5_add_music.
_MUSIC_MIX_TEMPLATE = (
    "-i {{in_1}} -i {{in_2}} "
    '-filter_complex "[1:a]volume=0.3[music];'
    '[0:a][music]amix=inputs=2:duration=shortest[mixed]" '
    '-map 0:v -map "[mixed]" -c:v copy -c:a aac -shortest {{out_1}}'
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


def render_music_mix_command() -> str:
    return _MUSIC_MIX_TEMPLATE


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
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("RendiClient requires an api_key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
        self._default_vcpu = default_vcpu
        self._default_max_run_seconds = default_max_run_seconds
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(timeout=self._timeout)

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
                _log.error(
                    "rendi_poll_failed",
                    command_id=command_id,
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
        """
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                command_id = await self.submit(ffmpeg_command, input_files, output_files)
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
    )
