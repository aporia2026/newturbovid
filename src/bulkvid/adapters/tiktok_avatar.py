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
  * ``TIKTOK_ADVERTISER_ID``    — Business API advertiser id; passed as a
                                  query parameter on every call. Most
                                  Business endpoints reject requests
                                  without it with a 403 Forbidden.
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
# All three endpoints live under ``/creative/digital_avatar/`` — same
# namespace as the operator's confirmed-working ``Avatar.py`` list call.
# The previous ``/business/symphony/avatar/`` guess returned 404 and
# killed the avatar row instantly when it tried to create a narration
# task (chat 2026-06-09). Override per-deploy via env vars if TikTok
# bumps the API version.
_DEFAULT_CREATE_URL = f"{_DEFAULT_BASE}/creative/digital_avatar/create/"
_DEFAULT_GET_URL = f"{_DEFAULT_BASE}/creative/digital_avatar/task/get/"
_DEFAULT_AVATAR_LIST_URL = f"{_DEFAULT_BASE}/creative/digital_avatar/get/"

# Pagination + retry constants for the list endpoint, copied verbatim
# from the operator's working Avatar.py.
_LIST_PAGE_SIZE = 100
_LIST_MAX_PAGES = 100
# TikTok occasionally returns code=51010 ("internal service timed out").
# It's transient; retry instead of failing the whole list.
_LIST_TRANSIENT_API_CODES: frozenset[int] = frozenset({51010})
_LIST_API_MAX_RETRIES = 4

# Polling cadence. Avatar generation typically lands in 30–90 s; we poll
# every 5 s so a fast render reports within 5 s of completion, and 120
# attempts × 5 s = 10 min ceiling matches TikTok's published timeout band.
_DEFAULT_POLL_INTERVAL_SECONDS = 5.0
_DEFAULT_POLL_MAX_ATTEMPTS = 120


# ── Errors ──────────────────────────────────────────────────────────────────


class TikTokAvatarError(RuntimeError):
    """Raised when the Symphony API returns a non-success or times out."""


def _raise_for_status_with_body(resp: "httpx.Response", *, where: str) -> None:
    """Like ``resp.raise_for_status()`` but includes TikTok's actual error
    body in the raised message.

    Default httpx.HTTPStatusError prints only the URL and status — useless
    when the body says e.g. ``{"code":40002,"message":"missing advertiser_id"}``.
    Surfacing the body in the message saves a round-trip through HF logs to
    figure out which env var to set.
    """
    if resp.is_success:
        return
    # Keep the body short — TikTok's error messages are small, but a
    # mis-routed endpoint might return a multi-KB HTML page.
    body_preview = (resp.text or "")[:500]
    raise TikTokAvatarError(
        f"TikTok {where} returned HTTP {resp.status_code} "
        f"for {resp.url}: {body_preview or '(empty body)'}"
    )


# ── Data ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AvatarListEntry:
    """One avatar from the TikTok avatar list endpoint."""

    avatar_id: str
    name: str
    gender: str          # "female" | "male" | "" if unknown
    preview_url: str     # thumbnail URL (TikTok-hosted, expires ~6h)


def _extract_tag(tag_groups: list[Any], tag_type: str) -> str:
    """Pull the first tag value for ``tag_type`` out of the
    ``tag_groups`` array. Returns "" if not found. Mirrors
    Avatar.py's ``flatten_avatar`` logic."""
    if not isinstance(tag_groups, list):
        return ""
    for g in tag_groups:
        if not isinstance(g, dict):
            continue
        ttype = str(g.get("tag_type") or "").strip().lower()
        if ttype != tag_type:
            continue
        tags = g.get("tags") or []
        if isinstance(tags, list) and tags:
            return str(tags[0]).strip()
    return ""


def _parse_list_entry(it: dict[str, Any]) -> AvatarListEntry | None:
    """Parse one item from ``data.list``. Returns ``None`` for entries
    missing an avatar_id (those can't be used anyway)."""
    avatar_id = str(it.get("avatar_id") or "").strip()
    if not avatar_id:
        return None
    return AvatarListEntry(
        avatar_id=avatar_id,
        name=str(it.get("avatar_name") or "").strip(),
        gender=_extract_tag(it.get("tag_groups") or [], "gender").lower(),
        # avatar_thumbnail is the small square preview; avatar_preview_url
        # is the video preview. Prefer the thumbnail for the admin grid
        # because <img> renders it inline. Fall back to preview_url if
        # the thumbnail isn't provided.
        preview_url=str(
            it.get("avatar_thumbnail")
            or it.get("avatar_preview_url")
            or ""
        ).strip(),
    )


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
        advertiser_id: str | None = None,
        create_url: str | None = None,
        get_url: str | None = None,
        list_url: str | None = None,
        poll_interval_seconds: float | None = None,
        poll_max_attempts: int | None = None,
        http_timeout_seconds: float = 30.0,
    ) -> None:
        # Poll cadence: matches the operator's existing Stage 4 pattern —
        # ``TIKTOK_POLL_INTERVAL`` / ``TIKTOK_POLL_MAX`` env vars override the
        # defaults. Empty / unparseable values fall back to the defaults.
        if poll_interval_seconds is None:
            try:
                poll_interval_seconds = float(
                    os.environ.get("TIKTOK_POLL_INTERVAL")
                    or _DEFAULT_POLL_INTERVAL_SECONDS
                )
            except ValueError:
                poll_interval_seconds = _DEFAULT_POLL_INTERVAL_SECONDS
        if poll_max_attempts is None:
            try:
                poll_max_attempts = int(
                    os.environ.get("TIKTOK_POLL_MAX")
                    or _DEFAULT_POLL_MAX_ATTEMPTS
                )
            except ValueError:
                poll_max_attempts = _DEFAULT_POLL_MAX_ATTEMPTS
        token = access_token or os.environ.get("TIKTOK_ACCESS_TOKEN", "")
        if not token:
            raise TikTokAvatarError(
                "TIKTOK_ACCESS_TOKEN env var is not set — the avatar tab "
                "cannot generate narration without it."
            )
        self._token = token
        # advertiser_id is required by most Business API endpoints. When
        # set, every call appends it as a query parameter. When None,
        # calls omit it — useful for the rare endpoint that doesn't take
        # it, or for non-Business deployments.
        self._advertiser_id = (
            advertiser_id
            if advertiser_id is not None
            else os.environ.get("TIKTOK_ADVERTISER_ID", "")
        ).strip()
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

    def _params_with_advertiser(
        self, extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build the query-string dict, automatically including
        ``advertiser_id`` when configured."""
        params: dict[str, str] = {}
        if self._advertiser_id:
            params["advertiser_id"] = self._advertiser_id
        if extra:
            params.update(extra)
        return params

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
            advertiser_id_set=bool(self._advertiser_id),
        )
        async with httpx.AsyncClient(timeout=self._http_timeout) as c:
            resp = await c.post(
                self._create_url,
                headers=self._headers,
                params=self._params_with_advertiser(),
                json=payload,
            )
            _raise_for_status_with_body(resp, where="create")
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
        params = self._params_with_advertiser({
            "task_ids": json.dumps([task_id]),
        })
        for attempt in range(1, self._poll_max + 1):
            await asyncio.sleep(self._poll_interval)
            async with httpx.AsyncClient(timeout=self._http_timeout) as c:
                resp = await c.get(
                    self._get_url, headers=self._headers, params=params
                )
                _raise_for_status_with_body(resp, where=f"get (attempt {attempt})")
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
        """GET the avatar list endpoint, paginating until every avatar
        is fetched. Returns one entry per available avatar.

        Mirrors the operator's confirmed-working ``Avatar.py`` script
        (chat 2026-06-09):

          * Endpoint: ``/creative/digital_avatar/get/`` (not
            ``/business/symphony/avatar/list/`` — that's a 403).
          * Query params: only ``page`` + ``page_size`` (1..100). No
            ``advertiser_id`` — confirmed unneeded.
          * Response: each item has ``avatar_id`` + ``avatar_name`` +
            ``avatar_thumbnail`` + ``avatar_preview_url`` + ``tag_groups``.
            Gender lives in the ``tag_groups`` array under
            ``tag_type == "gender"``.

        Preview thumbnails expire ~6 hours after fetch — that's why the
        admin page re-fetches on every load (the cache is only the
        fallback for a failed fetch).
        """
        out: list[AvatarListEntry] = []
        seen_ids: set[str] = set()
        page = 1
        total_page_hint: int | None = None

        while page <= _LIST_MAX_PAGES:
            body = await self._list_one_page(page)
            data = body.get("data") or {}
            items = data.get("list") or []
            page_info = data.get("page_info") or {}

            for it in items:
                entry = _parse_list_entry(it)
                if entry is None or entry.avatar_id in seen_ids:
                    continue
                seen_ids.add(entry.avatar_id)
                out.append(entry)

            # Update total-page hint from the response when available.
            tp = page_info.get("total_page")
            if isinstance(tp, int) and tp > 0:
                total_page_hint = tp

            # Stop conditions: empty page, short page, or known last page.
            if not items:
                break
            if len(items) < _LIST_PAGE_SIZE:
                break
            if total_page_hint is not None and page >= total_page_hint:
                break

            page += 1

        _log.info(
            "tiktok_avatar_list_ok",
            count=len(out),
            pages=page,
            total_page=total_page_hint,
        )
        return out

    async def _list_one_page(self, page: int) -> dict[str, Any]:
        """Fetch one page of the avatar list, retrying transient TikTok
        API codes (51010 = internal service timed out, per Avatar.py)."""
        params = {"page": page, "page_size": _LIST_PAGE_SIZE}
        for attempt in range(1, _LIST_API_MAX_RETRIES + 1):
            async with httpx.AsyncClient(timeout=self._http_timeout) as c:
                resp = await c.get(
                    self._list_url, headers=self._headers, params=params,
                )
                _raise_for_status_with_body(
                    resp, where=f"avatar list page {page}",
                )
                body: dict[str, Any] = resp.json()

            code = body.get("code")
            if code == 0:
                return body

            message = body.get("message") or "no message"
            if code in _LIST_TRANSIENT_API_CODES and attempt < _LIST_API_MAX_RETRIES:
                _log.warning(
                    "tiktok_avatar_list_transient_retry",
                    code=code,
                    message=message,
                    page=page,
                    attempt=attempt,
                )
                await asyncio.sleep(min(8.0, 1.6 ** attempt))
                continue
            raise TikTokAvatarError(
                f"TikTok avatar list returned code={code} on page {page}: "
                f"{message}"
            )

        raise TikTokAvatarError(
            f"TikTok avatar list exhausted retries on page {page}"
        )
