"""ZapCap adapter — burned-in subtitle captions.

Three-step workflow lifted from ``refs/stage_6_zapcap_processing.py``:
  1. ``POST /videos`` (multipart) — upload video, get back a ``video_id``
  2. ``POST /videos/{video_id}/task`` (json) — create captioning task with
     ``templateId``, ``language``, ``autoApprove``, render options
  3. ``GET /videos/{video_id}/task/{task_id}`` — poll until status ``completed``,
     read ``downloadUrl``

API quirks
----------
- Header is ``x-api-key`` (NOT ``Authorization: Bearer``)
- Upload returns HTTP 201 (not 200) with ``{"id": ...}``
- Task creation returns 200 or 201 with ``{"taskId": ...}`` or ``{"id": ...}``
- Status strings are lowercase: ``pending``, ``transcribing``,
  ``transcriptionCompleted``, ``rendering``, ``completed``, ``failed``
- Plan throttle: 1 new task / 10s, opt-in fast mode to 1s
  (``BULKVID_FAST_ZAPCAP_SUBMIT``)

Plan: ``_plans/2026-06-02-aporia-bulk-video-tool.md`` §5, §8, §11.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from bulkvid.config import Settings, get_settings
from bulkvid.logging import get_logger

_log = get_logger("zapcap")


# ZapCap bills per second of RENDERED video (https://zapcap.ai/pricing/ —
# $0.10/min public rate). The adapter multiplies this by the rendered length
# the caller passes into ``caption_video()``. Verified against real invoices
# 2026-06-08: an 8.1s clip bills $0.0112-$0.0131, a 10.5s clip $0.0178 — both
# inside 5% of the formula. Prior code shipped a flat ``$0.05/video``
# placeholder, which over-stated typical 8-10s clips by ~4×.
ZAPCAP_USD_PER_SECOND = 0.10 / 60.0


# ── Errors ───────────────────────────────────────────────────────────────────


class ZapCapError(RuntimeError):
    """Base class for ZapCap errors."""


class ZapCapAuthError(ZapCapError):
    """401 — invalid x-api-key."""


class ZapCapTaskFailedError(ZapCapError):
    """Task reported status=failed during polling."""


class ZapCapTimeoutError(ZapCapError):
    """Task did not complete within ``max_attempts`` polls."""


# ── Render options ───────────────────────────────────────────────────────────
# Defaults lifted from refs/stage_6_zapcap_processing.py:888-916. Admin panel
# overrides these in Phase 5; the adapter signature stays the same.


@dataclass
class ZapCapSubsOptions:
    emoji: bool = True
    emoji_animation: bool = True
    emphasize_keywords: bool = True


@dataclass
class ZapCapStyleOptions:
    top: int = 70                     # vertical position (% from top); 70 = lower-third
    font_uppercase: bool = False
    font_size: int = 42
    font_weight: int = 700
    font_color: str = "#FFFFFF"
    font_shadow: str = "m"
    stroke: str = "s"
    stroke_color: str = "#000000"


@dataclass
class ZapCapRenderOptions:
    subs: ZapCapSubsOptions = field(default_factory=ZapCapSubsOptions)
    style: ZapCapStyleOptions = field(default_factory=ZapCapStyleOptions)


def _render_options_to_api(opts: ZapCapRenderOptions) -> dict[str, Any]:
    """Convert dataclass options to the camelCase shape ZapCap expects."""
    s = opts.subs
    t = opts.style
    return {
        "subsOptions": {
            "emoji": s.emoji,
            "emojiAnimation": s.emoji_animation,
            "emphasizeKeywords": s.emphasize_keywords,
        },
        "styleOptions": {
            "top": t.top,
            "fontUppercase": t.font_uppercase,
            "fontSize": t.font_size,
            "fontWeight": t.font_weight,
            "fontColor": t.font_color,
            "fontShadow": t.font_shadow,
            "stroke": t.stroke,
            "strokeColor": t.stroke_color,
        },
    }


# ── Client ───────────────────────────────────────────────────────────────────


class ZapCapClient:
    """Async ZapCap client. Upload, task, poll."""

    def __init__(
        self,
        api_key: str,
        template_id: str,
        base_url: str = "https://api.zapcap.ai",
        connect_timeout: float = 10.0,
        read_timeout: float = 120.0,            # uploads can be slow
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("ZapCapClient requires an api_key")
        if not template_id:
            raise ValueError("ZapCapClient requires a template_id")
        self._api_key = api_key
        self._template_id = template_id
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(timeout=self._timeout)

    @property
    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key}

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    async def __aenter__(self) -> ZapCapClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    # ── Step 1: upload bytes ─────────────────────────────────────────────

    async def upload_video(self, video_bytes: bytes, filename: str = "video.mp4") -> str:
        """Upload video bytes via multipart. Returns the ZapCap ``video_id``."""
        url = f"{self._base_url}/videos"
        files = {"file": (filename, video_bytes, "video/mp4")}
        _log.info("zapcap_upload", filename=filename, size_bytes=len(video_bytes))
        resp = await self._client.post(url, headers=self._headers, files=files)
        if resp.status_code == 401:
            raise ZapCapAuthError("ZapCap 401 — invalid x-api-key")
        if resp.status_code != 201:
            raise ZapCapError(
                f"ZapCap upload HTTP {resp.status_code}: {resp.text[:200]}"
            )
        body = resp.json()
        video_id = body.get("id")
        if not video_id:
            raise ZapCapError(f"ZapCap upload missing id: {body}")
        _log.info("zapcap_upload_ok", video_id=video_id)
        return video_id

    # ── Step 2: create task ──────────────────────────────────────────────

    async def create_task(
        self,
        video_id: str,
        language: str = "en",
        render_options: ZapCapRenderOptions | None = None,
        auto_approve: bool = True,
    ) -> str:
        """Create a captioning task. Returns the ZapCap ``task_id``."""
        url = f"{self._base_url}/videos/{video_id}/task"
        body = {
            "templateId": self._template_id,
            "language": (language or "en").lower(),
            "autoApprove": auto_approve,
            "renderOptions": _render_options_to_api(
                render_options or ZapCapRenderOptions()
            ),
        }
        headers = {**self._headers, "Content-Type": "application/json"}
        _log.info(
            "zapcap_task_create",
            video_id=video_id,
            language=body["language"],
            template_id=self._template_id,
        )
        resp = await self._client.post(url, json=body, headers=headers)
        if resp.status_code == 401:
            raise ZapCapAuthError("ZapCap 401 — invalid x-api-key")
        if resp.status_code not in (200, 201):
            raise ZapCapError(
                f"ZapCap task create HTTP {resp.status_code}: {resp.text[:200]}"
            )
        result = resp.json()
        task_id = result.get("taskId") or result.get("id")
        if not task_id:
            raise ZapCapError(f"ZapCap task create missing taskId: {result}")
        _log.info("zapcap_task_create_ok", video_id=video_id, task_id=task_id)
        return task_id

    # ── Step 3: poll ─────────────────────────────────────────────────────

    async def poll_task(
        self,
        video_id: str,
        task_id: str,
        max_attempts: int = 60,
        delay_seconds: float = 10.0,
    ) -> str:
        """Poll until the task completes. Returns the ``downloadUrl``."""
        url = f"{self._base_url}/videos/{video_id}/task/{task_id}"

        for attempt in range(max_attempts):
            resp = await self._client.get(url, headers=self._headers)
            if resp.status_code != 200:
                if attempt == max_attempts - 1:
                    raise ZapCapError(
                        f"ZapCap poll HTTP {resp.status_code} after {max_attempts} attempts"
                    )
                await asyncio.sleep(delay_seconds)
                continue

            data = resp.json()
            status = (data.get("status") or "").lower()

            if status == "completed":
                download_url = data.get("downloadUrl")
                if not download_url:
                    raise ZapCapError(
                        f"ZapCap task {task_id} completed but no downloadUrl: {data}"
                    )
                _log.info(
                    "zapcap_poll_ok",
                    video_id=video_id,
                    task_id=task_id,
                    attempts=attempt + 1,
                )
                return download_url

            if status == "failed":
                err = data.get("error") or data.get("message") or "unknown"
                _log.error(
                    "zapcap_poll_failed",
                    video_id=video_id,
                    task_id=task_id,
                    error=err,
                )
                raise ZapCapTaskFailedError(
                    f"ZapCap task {task_id} failed: {err}"
                )

            _log.debug(
                "zapcap_poll_pending",
                video_id=video_id,
                task_id=task_id,
                status=status,
                attempt=attempt + 1,
            )
            if attempt < max_attempts - 1:
                await asyncio.sleep(delay_seconds)

        raise ZapCapTimeoutError(
            f"ZapCap task {task_id} did not complete within {max_attempts} attempts"
        )

    # ── High-level: full caption pipeline ────────────────────────────────

    async def caption_video(
        self,
        video_bytes: bytes,
        language: str = "en",
        render_options: ZapCapRenderOptions | None = None,
        filename: str = "video.mp4",
        *,
        video_duration_seconds: float,
        max_attempts: int = 60,
        delay_seconds: float = 10.0,
    ) -> tuple[str, float]:
        """End-to-end: upload + task + poll. Returns ``(download_url, cost_usd)``.

        ``video_duration_seconds`` is the length of the RENDERED output.
        ZapCap bills per second (see ``ZAPCAP_USD_PER_SECOND``), so the
        caller must supply it for an honest per-row cost. Cartoon flows pass
        the flat ``TARGET_VIDEO_SECONDS`` (8.0s); VO-driven flows pass
        ``tts.duration_seconds`` (or ``NO_VO_VIDEO_SECONDS`` when VO=False).
        """
        video_id = await self.upload_video(video_bytes, filename=filename)
        task_id = await self.create_task(
            video_id, language=language, render_options=render_options
        )
        download_url = await self.poll_task(
            video_id, task_id, max_attempts=max_attempts, delay_seconds=delay_seconds
        )
        cost = round(max(0.0, float(video_duration_seconds)) * ZAPCAP_USD_PER_SECOND, 6)
        return download_url, cost


def build_client_from_settings(settings: Settings | None = None) -> ZapCapClient:
    s = settings or get_settings()
    if not s.ZAPCAP_API_KEY:
        raise ValueError("ZAPCAP_API_KEY is empty; cannot build ZapCapClient")
    return ZapCapClient(
        api_key=s.ZAPCAP_API_KEY,
        template_id=s.ZAPCAP_TEMPLATE_ID,
        base_url=s.ZAPCAP_BASE_URL,
    )


# ── Helper accessors (handy for the admin panel later) ───────────────────────


def default_subs_options() -> ZapCapSubsOptions:
    return ZapCapSubsOptions()


def default_style_options() -> ZapCapStyleOptions:
    return ZapCapStyleOptions()


def render_options_dict_for_inspection(opts: ZapCapRenderOptions) -> dict[str, Any]:
    """Flat dict view of render options. Useful for admin-panel display."""
    return {"subs": asdict(opts.subs), "style": asdict(opts.style)}
