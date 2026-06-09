"""TikTok Symphony Creative API client — avatar narration videos.

Used by the ``video with avatar`` row processor to generate a talking-head
video from a script and an operator-picked ``avatar_id``. Mirrors the
submit-then-poll pattern used by the kie adapter:

  1. ``create_task(...)`` POSTs to TikTok's Symphony create endpoint and
     returns a ``task_id``.
  2. ``wait_for_result(task_id)`` polls TikTok's get endpoint at fixed
     interval until ``status == "SUCCESS"`` (returns ``preview_url``) or
     ``status == "FAIL"`` (raises with the failure detail).
  3. ``list_avatars()`` calls TikTok's avatar list endpoint so the admin
     page can render preview thumbnails for the operator.

Endpoints come from environment variables so they can be swapped without
a redeploy if TikTok bumps an API version:

  * ``TIKTOK_ACCESS_TOKEN``     — operator's Business API token (required)
  * ``TIKTOK_CREATE_URL``       — create endpoint (default below)
  * ``TIKTOK_GET_URL``          — poll endpoint (default below)
  * ``TIKTOK_AVATAR_LIST_URL``  — avatar listing (default below)

Plan: ``_plans/2026-06-09-video-with-avatar-tab.md``.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

import httpx

from bulkvid.logging import get_logger

_log = get_logger("tiktok_avatar")


# ── Defaults ────────────────────────────────────────────────────────────────
#
# Standard TikTok Business API v1.3 Symphony Creative endpoints. Operator
# can override per-environment via env var without touching code.

_DEFAULT_BASE = "https://business-api.tiktok.com/open_api/v1.3"
_DEFAULT_CREATE_URL = f"{_DEFAULT_BASE}/business/symphony/avatar/"
_DEFAULT_GET_URL = f"{_DEFAULT_BASE}/business/symphony/avatar/get/"
_DEFAULT_AVATAR_LIST_URL = f"{_DEFAULT_BASE}/business/symphony/avatar/list/"

# Polling cadence. Avatar generation typically lands in 30–90 s; we poll
# every 5 s so a fast render reports within 5 s of completion, and 120
# attempts × 5 s = 10 min ceiling matches TikTok's published timeout band.
_DEFAULT_POLL_INTERVAL_SECONDS = 5.0
_DEFAULT_POLL_MAX_ATTEMPTS = 120


# ── Errors ──────────────────────────────────────────────────────────────────


class TikTokAvatarError(RuntimeError):
    """Raised when the Symphony API returns a non-success or times out."""


# ── Data ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AvatarListEntry:
    """One avatar from the TikTok avatar list endpoint."""

    avatar_id: str
    name: str
    gender: str          # "female" | "male" | "" if unknown
    preview_url: str     # thumbnail URL (TikTok-hosted)


@dataclass(frozen=True)
class AvatarResult:
    """Successful avatar generation."""

    preview_url: str     # the rendered avatar video (TikTok signed URL)
    duration_seconds: float | None    # if TikTok returns it; otherwise None


# ── Client ──────────────────────────────────────────────────────────────────


class TikTokAvatarClient:
    """Async wrapper over the Symphony Creative API."""

    def __init__(
        self,
        access_token: str | None = None,
        *,
        create_url: str | None = None,
        get_url: str | None = None,
        list_url: str | None = None,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        poll_max_attempts: int = _DEFAULT_POLL_MAX_ATTEMPTS,
        http_timeout_seconds: float = 30.0,
    ) -> None:
        token = access_token or os.environ.get("TIKTOK_ACCESS_TOKEN", "")
        if not token:
            raise TikTokAvatarError(
                "TIKTOK_ACCESS_TOKEN env var is not set — the avatar tab "
                "cannot generate narration without it."
            )
        self._token = token
        self._create_url = (
            create_url
            or os.environ.get("TIKTOK_CREATE_URL")
            or _DEFAULT_CREATE_URL
        )
        self._get_url = (
            get_url
            or os.environ.get("TIKTOK_GET_URL")
            or _DEFAULT_GET_URL
        )
        self._list_url = (
            list_url
            or os.environ.get("TIKTOK_AVATAR_LIST_URL")
            or _DEFAULT_AVATAR_LIST_URL
        )
        self._poll_interval = poll_interval_seconds
        self._poll_max = poll_max_attempts
        self._http_timeout = http_timeout_seconds

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Access-Token": self._token,
            "Content-Type": "application/json",
        }

    # ── Create + poll ────────────────────────────────────────────────────

    async def create_task(
        self,
        *,
        avatar_id: str,
        script: str,
        video_name: str,
        package_id: str | None = None,
    ) -> str:
        """POST to the create endpoint. Returns ``task_id`` on success.

        ``package_id`` defaults to ``video_name`` (mirrors the user's
        existing stage_4 code — TikTok's API requires both fields but
        accepts equal values).
        """
        payload = {
            "material_packages": [
                {
                    "avatar_id": avatar_id,
                    "script": script,
                    "video_name": video_name,
                    "package_id": package_id or video_name,
                }
            ]
        }
        _log.info(
            "tiktok_avatar_submit",
            avatar_id=avatar_id,
            script_chars=len(script),
            video_name=video_name,
        )
        async with httpx.AsyncClient(timeout=self._http_timeout) as c:
            resp = await c.post(
                self._create_url, headers=self._headers, json=payload
            )
            resp.raise_for_status()
            body: dict[str, Any] = resp.json()

        code = body.get("code")
        if code != 0:
            message = body.get("message") or "no message"
            raise TikTokAvatarError(
                f"TikTok create returned code={code}: {message}"
            )
        try:
            task_id = body["data"]["list"][0]["task_id"]
        except (KeyError, IndexError, TypeError) as e:
            raise TikTokAvatarError(
                f"TikTok create response missing task_id: {e!s}"
            ) from e
        if not isinstance(task_id, str) or not task_id:
            raise TikTokAvatarError(
                f"TikTok create returned an empty task_id: {task_id!r}"
            )
        _log.info("tiktok_avatar_submit_ok", task_id=task_id)
        return task_id

    async def wait_for_result(self, task_id: str) -> AvatarResult:
        """Poll the get endpoint until SUCCESS / FAIL / timeout."""
        params = {"task_ids": json.dumps([task_id])}
        for attempt in range(1, self._poll_max + 1):
            await asyncio.sleep(self._poll_interval)
            async with httpx.AsyncClient(timeout=self._http_timeout) as c:
                resp = await c.get(
                    self._get_url, headers=self._headers, params=params
                )
                resp.raise_for_status()
                body: dict[str, Any] = resp.json()

            code = body.get("code")
            if code != 0:
                message = body.get("message") or "no message"
                raise TikTokAvatarError(
                    f"TikTok get returned code={code} on attempt {attempt}: "
                    f"{message}"
                )

            items = body.get("data", {}).get("list") or []
            if not items:
                _log.debug(
                    "tiktok_avatar_poll_pending",
                    attempt=attempt,
                    task_id=task_id,
                    state="no_items",
                )
                continue
            info = items[0]
            status = info.get("status")
            _log.debug(
                "tiktok_avatar_poll_pending",
                attempt=attempt,
                task_id=task_id,
                state=status,
            )

            if status == "SUCCESS":
                preview_url = info.get("preview_url") or ""
                if not preview_url:
                    raise TikTokAvatarError(
                        f"TikTok reported SUCCESS but no preview_url "
                        f"for task {task_id}"
                    )
                duration = info.get("duration") or info.get("video_duration")
                _log.info(
                    "tiktok_avatar_ok",
                    attempts=attempt,
                    task_id=task_id,
                    duration_seconds=duration,
                )
                return AvatarResult(
                    preview_url=preview_url,
                    duration_seconds=float(duration) if duration else None,
                )
            if status == "FAIL":
                reason = (
                    info.get("failure_reason")
                    or info.get("error_message")
                    or info.get("message")
                    or "no reason"
                )
                raise TikTokAvatarError(
                    f"TikTok avatar generation FAILED for task {task_id}: "
                    f"{reason}"
                )
            # else: still processing — keep polling

        raise TikTokAvatarError(
            f"TikTok avatar generation timed out after "
            f"{self._poll_max * self._poll_interval:.0f}s "
            f"(task {task_id})"
        )

    async def create_and_wait(
        self,
        *,
        avatar_id: str,
        script: str,
        video_name: str,
        package_id: str | None = None,
    ) -> AvatarResult:
        """Create + poll in a single call. Convenience for the row processor."""
        task_id = await self.create_task(
            avatar_id=avatar_id,
            script=script,
            video_name=video_name,
            package_id=package_id,
        )
        return await self.wait_for_result(task_id)

    # ── Avatar listing (admin page) ─────────────────────────────────────

    async def list_avatars(self) -> list[AvatarListEntry]:
        """GET the avatar list endpoint. Returns one entry per available
        avatar with the fields the admin page needs to render previews."""
        async with httpx.AsyncClient(timeout=self._http_timeout) as c:
            resp = await c.get(self._list_url, headers=self._headers)
            resp.raise_for_status()
            body: dict[str, Any] = resp.json()

        code = body.get("code")
        if code != 0:
            message = body.get("message") or "no message"
            raise TikTokAvatarError(
                f"TikTok avatar list returned code={code}: {message}"
            )

        items = body.get("data", {}).get("list") or []
        out: list[AvatarListEntry] = []
        for it in items:
            avatar_id = str(it.get("avatar_id") or it.get("id") or "").strip()
            if not avatar_id:
                continue
            out.append(
                AvatarListEntry(
                    avatar_id=avatar_id,
                    name=str(
                        it.get("name") or it.get("display_name") or ""
                    ).strip(),
                    gender=str(it.get("gender") or "").strip().lower(),
                    preview_url=str(
                        it.get("preview_url")
                        or it.get("avatar_url")
                        or it.get("image_url")
                        or ""
                    ).strip(),
                )
            )
        return out
