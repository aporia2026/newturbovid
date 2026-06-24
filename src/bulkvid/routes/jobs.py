"""Job submission and status routes (auth-gated).

POST /jobs            — submit a batch of rows; returns ``job_id``
GET  /jobs            — list jobs (bulk user: their own; admin: all)
GET  /jobs/{job_id}   — single job status
POST /jobs/{job_id}/kill — abort a queued/running job

All routes verify the ``Authorization: Bearer <jwt>`` header against the
``GoogleIdentityVerifier`` attached to ``app.state.verifier``. The queue is
fetched from ``app.state.queue``.

Plan §7 (Security), §13 Phase 4.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from bulkvid.auth import AuthError, ForbiddenError, GoogleIdentityVerifier, Identity
from bulkvid.config import get_settings
from bulkvid.logging import get_logger, read_job_log_lines
from bulkvid.models.row import (
    CardChoice,
    CartoonRow,
    AvatarRow,
    FourImagesVO2Row,
    ImageVORow,
    SimpleMotionRow,
    SimpleRow,
    SimpleX4Row,
    TextOnImgRow,
    YtCartoonRow,
)
from bulkvid.orchestrator.queue import (
    JOB_QUEUED,
    JOB_RUNNING,
    TAB_AVATAR,
    TAB_CARTOON,
    TAB_FOUR_IMAGES,
    TAB_IMAGE_VO,
    TAB_SIMPLE,
    TAB_SIMPLE_MOTION,
    TAB_SIMPLE_X4,
    TAB_TEXT_ON_IMG,
    TAB_YT_CARTOON,
    Job,
    JobQueue,
    QueueBusy,
    QueueUnavailable,
)
from bulkvid.step_extractor import extract_current_step

# Idempotency keys come from the Apps Script and are opaque to us. The format
# guard rejects oversized / non-ASCII payloads so a malicious client cannot
# bloat the idempotency table with a giant key. UUID-ish: alnum, dash,
# underscore, 1-64 chars.
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Hard timeout around the kill DB call. Mirrors the runner's
# ``_WORKER_QUERY_TIMEOUT_SECONDS`` (commit 62c0950) on the route side:
# without it, a stalled libsql roundtrip blocks the kill POST forever, the
# Apps Script's UrlFetch hits its own 30 s cap, and the operator sees
# "Could not kill" with no diagnostic. 10 s is loose enough to absorb a
# slow Turso roundtrip (cold-start spikes ~5 s) and tight enough that the
# Apps Script cap doesn't trip. Plan
# ``_plans/2026-06-14-stuck-processing-rows.md`` §B.
_KILL_CALL_TIMEOUT_SECONDS = float(
    os.environ.get("BULKVID_KILL_CALL_TIMEOUT_SECONDS") or 10.0
)

_log = get_logger("route.jobs")


router = APIRouter(prefix="/jobs", tags=["jobs"])


# ── Pydantic IO models ──────────────────────────────────────────────────────


class ImageVORowIn(BaseModel):
    row_num: int = Field(ge=1)
    country: str = ""
    vertical: str = ""
    article_url: str
    manual_image_url: str
    voice_over: bool = True
    zapcap: bool = False
    aspect_ratio: str = "9:16"
    script_pattern: str = ""
    open_comments: str = ""


class FourImagesVO2RowIn(BaseModel):
    row_num: int = Field(ge=1)
    country: str = ""
    vertical: str = ""
    article_url: str
    how_many: int = Field(ge=1, le=4)
    voice_over: bool = True
    image_urls: list[str]
    zapcap: bool = False
    aspect_ratio: str = "9:16"
    script_pattern: str = ""
    open_comments: str = ""


class CartoonRowIn(BaseModel):
    row_num: int = Field(ge=1)
    country: str = ""
    vertical: str = ""
    article_url: str
    voice_over: bool = True
    zapcap: bool = False
    aspect_ratio: str = "9:16"
    script_pattern: str = ""
    cta_enabled: bool = False    # Yoav 2026-06-08 — Sheet column CTA Yes/No
    cta_text: str = ""           # operator text; empty → per-language fallback
    open_comments: str = ""


class SimpleMotionRowIn(BaseModel):
    """Wire shape for the ``simple-motion`` tab — CartoonRowIn plus two manual
    image columns (D / E). A blank image cell is auto-generated (realistic
    style); a filled cell is animated as-is. Manual image URLs are passed
    through (same as the avatar / image_vo tabs); ``cta_text`` is bounded
    server-side. Plan ``_plans/2026-06-22-simple-motion-tab.md``."""

    row_num: int = Field(ge=1)
    country: str = ""
    vertical: str = ""
    article_url: str
    manual_image_1: str = ""     # col D — blank → generate; filled → as-is
    manual_image_2: str = ""     # col E — blank → generate; filled → as-is
    voice_over: bool = True
    zapcap: bool = False
    aspect_ratio: str = "9:16"
    script_pattern: str = ""
    cta_enabled: bool = False
    cta_text: str = ""
    open_comments: str = ""


class YtCartoonRowIn(BaseModel):
    """Wire shape for the ``yt-cartoon`` tab — CartoonRowIn plus the four new
    per-row knobs (Tone, Cap Position, CTA Position, Vid Length). The string
    knobs are coerced/normalised defensively downstream
    (``pipeline.yt_cartoon``), so a blank or garbage cell never 400s the batch."""

    row_num: int = Field(ge=1)
    country: str = ""
    vertical: str = ""
    article_url: str
    voice_over: bool = True
    zapcap: bool = False
    aspect_ratio: str = "9:16"
    script_pattern: str = ""
    cta_enabled: bool = False
    cta_text: str = ""
    open_comments: str = ""
    # New yt-cartoon knobs — blank = today's defaults.
    tone: str = ""
    cap_position: str = ""
    cta_position: str = ""
    vid_length: str = ""


class CardChoiceIn(BaseModel):
    template_id: str = ""    # validated to "" | "1" | "2" | "3" in the server-side coercion below
    cta: str = ""


class SimpleX4RowIn(BaseModel):
    """Shape submitted by Apps Script for ``simple x4`` rows. Same as
    ImageVORowIn plus 4 per-video CardChoice picks."""

    row_num: int = Field(ge=1)
    country: str = ""
    vertical: str = ""
    article_url: str
    manual_image_url: str
    voice_over: bool = True
    zapcap: bool = False
    aspect_ratio: str = "9:16"
    script_pattern: str = ""
    cards: list[CardChoiceIn] = Field(default_factory=list)
    open_comments: str = ""


class AvatarRowIn(BaseModel):
    """Wire shape for the ``video with avatar`` tab — Image-VO inputs
    plus a per-row ``avatar_id`` (TikTok Symphony) and CTA columns
    mirroring cartoon. Plan ``_plans/2026-06-09-video-with-avatar-tab.md``.

    ``avatar_size`` / ``avatar_shape`` were added 2026-06-09
    (``_plans/2026-06-09-avatar-overlay-size-shape.md``). Both default
    to empty so older Apps Script clients that don't send them get
    today's behaviour automatically; invalid values fall through to
    the same defaults rather than 400-ing the whole batch."""

    row_num: int = Field(ge=1)
    country: str = ""
    vertical: str = ""
    article_url: str
    manual_image_url: str = ""   # optional — empty = text-to-image
    avatar_id: str               # required — pick from /admin/avatars
    voice_over: bool = True
    zapcap: bool = False
    aspect_ratio: str = "9:16"
    script_pattern: str = ""
    cta_enabled: bool = False
    cta_text: str = ""
    open_comments: str = ""
    # New 2026-06-09 — operator-facing overlay knobs.
    avatar_size: str = ""        # "" | "small" | "medium" | "large"
    avatar_shape: str = ""       # "" | "rectangle" | "circle"


class TextOnImgRowIn(BaseModel):
    """Wire shape for the ``paste text on img`` tab — Image-VO inputs plus
    the operator-typed ``text`` to overlay on the manual image. Produces
    a still PNG (no video). ``article_url`` / ``voice_over`` / ``zapcap`` /
    ``script_pattern`` / ``open_comments`` are accepted for Apps Script
    payload compatibility but ignored by the processor (2026-06-09:
    video pipeline stripped per user direction)."""

    row_num: int = Field(ge=1)
    country: str = ""
    vertical: str = ""
    article_url: str
    manual_image_url: str
    text: str = ""              # bounded server-side at 240 chars below
    voice_over: bool = True
    zapcap: bool = False
    aspect_ratio: str = "9:16"
    script_pattern: str = ""
    open_comments: str = ""


class SubmitJobIn(BaseModel):
    sheet_id: str
    worksheet: str
    tab_type: str
    rows_image_vo: list[ImageVORowIn] | None = None
    rows_four_images: list[FourImagesVO2RowIn] | None = None
    # The simple tab reuses the Image-VO input shape (one video, no image gen).
    rows_simple: list[ImageVORowIn] | None = None
    # The cartoon tab generates animated videos from text (no seed image).
    rows_cartoon: list[CartoonRowIn] | None = None
    # simple-motion: animate super-realistic images (manual D/E or generated).
    rows_simple_motion: list[SimpleMotionRowIn] | None = None
    # yt-cartoon: engaging, variable-length cartoon videos (plan 2026-06-17).
    rows_yt_cartoon: list[YtCartoonRowIn] | None = None
    # Simple x4: per-video card template + CTA picks (plan 2026-06-08).
    rows_simple_x4: list[SimpleX4RowIn] | None = None
    # paste text on img: manual image + center-overlay text (plan 2026-06-09).
    rows_text_on_img: list[TextOnImgRowIn] | None = None
    # video with avatar: 2-shot kie/Seedance + TikTok avatar overlay (plan 2026-06-09).
    rows_avatar: list[AvatarRowIn] | None = None
    # Client-generated opaque key (UUID-ish) that lets the Apps Script retry
    # the POST safely when PA's frontend drops the response — the server
    # returns the SAME job_id for a key it has already seen for this user.
    # Optional for backward compat with old Apps Script clients.
    idempotency_key: str | None = None


class SubmitJobOut(BaseModel):
    job_id: str
    status: str
    # Number of rows actually enqueued for processing. May be LESS than the
    # submitted ``len(rows)`` if the queue dedup-suppressed some of them
    # (those row numbers are already running in another active job for the
    # same sheet+worksheet). Apps Script should warn the user when this is
    # zero — a 0/0 job is indistinguishable from "succeeded with no output"
    # otherwise. Plan: ``_plans/2026-06-09-libsql-key-column-bug.md`` §Fix 3.
    row_count: int
    # Count of rows the server dropped as duplicates of an in-flight job.
    # ``submitted_count - row_count`` (i.e. always ``>= 0``). Default 0 so
    # older Apps Script clients that don't read this field keep working.
    dropped_count: int = 0
    # The original count from the Apps Script payload, echoed so the client
    # can show "queued N of M (D skipped as duplicates)" without re-counting.
    submitted_count: int = 0


def _build_simple_x4_row(r: SimpleX4RowIn) -> SimpleX4Row:
    """Coerce a SimpleX4RowIn from the wire into a SimpleX4Row dataclass.

    Server-side hardening (defense in depth — Apps Script also validates):
      * ``template_id`` clamped to {"", "1", "2", "3"} — invalid values are
        silently downgraded to "" (no overlay) so a typo doesn't fail the row.
      * ``cta`` truncated to 80 chars (matches the renderer's pill max-width).
      * ``cards`` padded / trimmed to exactly 4 entries — Apps Script always
        sends 4 but a forged payload could send any count.
    """
    raw_cards = list(r.cards or [])
    while len(raw_cards) < 4:
        raw_cards.append(CardChoiceIn())
    raw_cards = raw_cards[:4]
    cards = [
        CardChoice(
            template_id=(c.template_id if c.template_id in ("", "1", "2", "3") else ""),
            cta=(c.cta or "")[:80],
        )
        for c in raw_cards
    ]
    return SimpleX4Row(
        row_num=r.row_num,
        country=r.country,
        vertical=r.vertical,
        article_url=r.article_url,
        manual_image_url=r.manual_image_url,
        voice_over=r.voice_over,
        zapcap=r.zapcap,
        aspect_ratio=r.aspect_ratio,
        script_pattern=r.script_pattern,
        cards=cards,
        open_comments=r.open_comments,
    )


_AVATAR_SIZE_ALLOWED = frozenset({"", "small", "medium", "large"})
_AVATAR_SHAPE_ALLOWED = frozenset({"", "rectangle", "circle"})


def _coerce_enum(value: str | None, *, allowed: frozenset[str]) -> str:
    """Lowercase + trim ``value``; return it when in ``allowed``, else ``""``.

    Used for the avatar tab's optional dropdown columns: a typo or an
    older Apps Script that sends "Med" / "round" should fall back to the
    default (empty string → today's behaviour) rather than 400 the
    whole batch. The processor's enum resolution is also defensive, so
    this is belt-and-suspenders.
    """
    cleaned = (value or "").strip().lower()
    return cleaned if cleaned in allowed else ""


def _build_avatar_row(r: AvatarRowIn) -> AvatarRow:
    """Coerce an AvatarRowIn into an AvatarRow.

    Server-side hardening:
      * ``avatar_id`` trimmed; row is rejected if blank (the row
        processor also guards this — defense in depth).
      * ``cta_text`` truncated at 80 chars (matches cartoon's bound).
      * ``avatar_size`` / ``avatar_shape`` coerced to one of the
        allowed enum values; unknown values become ``""`` (default)
        so the processor's per-row defaults take over.
    """
    return AvatarRow(
        row_num=r.row_num,
        country=r.country,
        vertical=r.vertical,
        article_url=r.article_url,
        manual_image_url=r.manual_image_url,
        avatar_id=(r.avatar_id or "").strip()[:64],
        voice_over=r.voice_over,
        zapcap=r.zapcap,
        aspect_ratio=r.aspect_ratio,
        script_pattern=r.script_pattern,
        cta_enabled=r.cta_enabled,
        cta_text=(r.cta_text or "")[:80],
        open_comments=r.open_comments,
        avatar_size=_coerce_enum(r.avatar_size, allowed=_AVATAR_SIZE_ALLOWED),
        avatar_shape=_coerce_enum(r.avatar_shape, allowed=_AVATAR_SHAPE_ALLOWED),
    )


def _build_text_on_img_row(r: TextOnImgRowIn) -> TextOnImgRow:
    """Coerce a TextOnImgRowIn from the wire into a TextOnImgRow.

    Server-side hardening: trim the overlay ``text`` at 240 chars. A
    longer string would auto-shrink to the floor font size and become
    unreadable anyway — 240 chars is ~3 long sentences, comfortably
    above any realistic ad headline.

    article_url / voice_over / zapcap / script_pattern / open_comments
    are passed through but ignored downstream — the processor produces
    a still PNG with no script or VO.
    """
    return TextOnImgRow(
        row_num=r.row_num,
        country=r.country,
        vertical=r.vertical,
        article_url=r.article_url,
        manual_image_url=r.manual_image_url,
        text=(r.text or "").strip()[:240],
        voice_over=r.voice_over,
        zapcap=r.zapcap,
        aspect_ratio=r.aspect_ratio,
        script_pattern=r.script_pattern,
        open_comments=r.open_comments,
    )


def _build_simple_motion_row(r: SimpleMotionRowIn) -> SimpleMotionRow:
    """Coerce a SimpleMotionRowIn into a SimpleMotionRow.

    Server-side hardening: ``cta_text`` bounded at 80 chars (matches cartoon);
    manual image URLs trimmed and passed through (the processor downloads + re-
    uploads them, same as the avatar tab). Blank image cells stay blank so the
    processor generates a realistic image for that shot.
    """
    return SimpleMotionRow(
        row_num=r.row_num,
        country=r.country,
        vertical=r.vertical,
        article_url=r.article_url,
        manual_image_1=(r.manual_image_1 or "").strip(),
        manual_image_2=(r.manual_image_2 or "").strip(),
        voice_over=r.voice_over,
        zapcap=r.zapcap,
        aspect_ratio=r.aspect_ratio,
        script_pattern=r.script_pattern,
        cta_enabled=r.cta_enabled,
        cta_text=(r.cta_text or "")[:80],
        open_comments=r.open_comments,
    )


def _build_yt_cartoon_row(r: YtCartoonRowIn) -> YtCartoonRow:
    """Coerce a YtCartoonRowIn into a YtCartoonRow.

    Server-side hardening: ``cta_text`` bounded at 80 chars (matches cartoon),
    and the four knob strings trimmed + length-bounded so a forged payload
    can't bloat them. Their SEMANTIC normalisation (tone registry, Vid Length
    bucket, position nudges) happens once in the row processor's pure helpers —
    passing the raw label through keeps a single source of truth for each map.
    """
    return YtCartoonRow(
        row_num=r.row_num,
        country=r.country,
        vertical=r.vertical,
        article_url=r.article_url,
        voice_over=r.voice_over,
        zapcap=r.zapcap,
        aspect_ratio=r.aspect_ratio,
        script_pattern=r.script_pattern,
        open_comments=r.open_comments,
        cta_enabled=r.cta_enabled,
        cta_text=(r.cta_text or "")[:80],
        tone=(r.tone or "").strip()[:40],
        cap_position=(r.cap_position or "").strip()[:40],
        cta_position=(r.cta_position or "").strip()[:40],
        vid_length=(r.vid_length or "").strip()[:40],
    )


class JobOut(BaseModel):
    job_id: str
    user_email: str
    sheet_id: str
    worksheet: str
    tab_type: str
    status: str
    row_count: int
    completed_rows: int
    failed_rows: int
    cost_usd: float
    created_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None


class JobRowOut(BaseModel):
    row_num: int
    status: str
    error: str | None = None
    video_urls: list[str] = []
    # New for sidebar UX overhaul (plan §Phase 1): the human-readable
    # current pipeline step (e.g. "Synthesizing voice") for rows in
    # ``processing``, plus the ISO timestamp the worker claimed the row
    # so the client can render a live elapsed counter without an extra
    # round-trip. Both are None for ``pending`` / ``done`` / ``failed``
    # rows where there's nothing meaningful to render.
    current_step: str | None = None
    started_at: str | None = None
    # Default-template selector pick. Set when the row had a blank
    # script_pattern and the selector chose a library entry. Empty
    # otherwise. Plan
    # ``_plans/2026-06-07-overload-handling-and-template-defaults.md`` §B.
    chosen_template_id: str | None = None


class JobRowsOut(BaseModel):
    job_id: str
    rows: list[JobRowOut]


class JobLogOut(BaseModel):
    job_id: str
    exists: bool
    lines: list[str]


class PollLogOut(BaseModel):
    exists: bool
    lines: list[str]


class QueueStatusOut(BaseModel):
    """Row-level queue snapshot for the sidebar's status banner.

    ``in_flight`` is the row count currently being PROCESSED (one worker
    slot each); ``queued`` is the row count WAITING for a slot.
    ``max_concurrent`` is the worker's ``BULKVID_MAX_CONCURRENT_ROWS`` —
    operators want to see this alongside in_flight to spot when they're
    saturating the cap.

    ``eta_seconds`` is a rough wall-clock estimate of when the queued
    rows will finish, weighted by each tab's median row time. Returns
    ``None`` when no medians are available yet (cold start) so the
    sidebar can hide the "ETA" line instead of showing a misleading 0.

    ``stuck_queued_seconds`` is non-None only when the worker has rows
    pending but NOTHING in flight AND the oldest pending row has been
    waiting longer than ``STUCK_QUEUED_THRESHOLD_SECONDS``. Surfaces a
    "worker not claiming" warning in the sidebar so the operator stops
    silently waiting and clicks "Stop all jobs" / pings on-call. The
    threshold is intentionally generous (default 60 s) so a brief libsql
    blip or a slow cold-start doesn't false-trigger.

    Plan: chat 2026-06-09; queue depth + ETA banner in the sidebar."""

    in_flight: int
    queued: int
    max_concurrent: int
    eta_seconds: int | None = None
    stuck_queued_seconds: int | None = None


# Worker is considered "stuck" if the oldest pending row has been waiting
# longer than this with nothing else in flight. Threshold deliberately
# loose: the runner polls every 1 s in normal operation, so 60 s = 60
# missed claims, which is well past any plausible libsql hiccup. Tunable
# without a redeploy via env override below if we need to dial in.
import os as _os    # noqa: E402 — used only by the env override

STUCK_QUEUED_THRESHOLD_SECONDS = int(
    _os.environ.get("BULKVID_STUCK_QUEUED_THRESHOLD_SECONDS") or 60
)


class PollOut(BaseModel):
    """Single-response bundle for the sidebar.

    Replaces what used to take three separate auth-gated requests per poll
    cycle (``list_jobs`` + ``get_job_rows`` per running job + ``get_job_log``
    per open log pane). See ``_plans/2026-06-04-fix-sidebar-500s.md``.

    ``eta_medians_by_tab`` carries the median elapsed-seconds per
    ``tab_type`` over the last ~50 successful rows; the sidebar uses it
    to render a rough "~3:30 est" next to the live elapsed counter so
    the user has a sense of how long a row is going to take. Plan:
    ``_plans/2026-06-04-sidebar-ux-overhaul.md`` §Phase 3.

    ``queue_status`` carries the row-level queue depth + worker capacity
    + tab-weighted ETA so the sidebar can render
    "20 / 20 in flight · 480 queued · ETA ~50 min" at a glance.
    """

    jobs: list[JobOut]
    rows_by_job: dict[str, list[JobRowOut]]
    logs_by_job: dict[str, PollLogOut]
    eta_medians_by_tab: dict[str, float] = {}
    queue_status: QueueStatusOut | None = None


def _job_to_out(job: Job) -> JobOut:
    return JobOut(
        job_id=job.job_id,
        user_email=job.user_email,
        sheet_id=job.sheet_id,
        worksheet=job.worksheet,
        tab_type=job.tab_type,
        status=job.status,
        row_count=job.row_count,
        completed_rows=job.completed_rows,
        failed_rows=job.failed_rows,
        cost_usd=job.cost_usd,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        error=job.error,
    )


# Per-row statuses where the human-readable step is meaningful. ``pending``
# rows haven't started; ``done`` / ``failed`` rows have a final status that
# the sidebar already surfaces directly. Limiting the extractor call to
# ``processing`` keeps the poll cycle from doing a log-tail parse for every
# row in a 200-row batch.
_ROW_STATUSES_WITH_STEP = {"processing"}


def _row_to_out(job_id: str, raw: dict[str, Any]) -> JobRowOut:
    """Build a ``JobRowOut`` from the queue's row dict, enriching with
    the current pipeline step for rows actively being processed.

    See ``_plans/2026-06-04-sidebar-ux-overhaul.md`` §Phase 1.
    """
    step: str | None = None
    if raw.get("status") in _ROW_STATUSES_WITH_STEP:
        try:
            step = extract_current_step(job_id, raw["row_num"])
        except Exception as e:    # noqa: BLE001 — never let a UI nicety 500 a poll
            _log.warning(
                "step_extract_failed",
                job_id=job_id,
                row_num=raw.get("row_num"),
                err=str(e)[:200],
            )
    return JobRowOut(
        row_num=raw["row_num"],
        status=raw["status"],
        started_at=raw.get("started_at"),
        error=raw.get("error"),
        video_urls=raw.get("video_urls", []),
        current_step=step,
        chosen_template_id=raw.get("chosen_template_id") or None,
    )


# ── Dependencies ────────────────────────────────────────────────────────────


async def get_identity(
    request: Request,
    authorization: str = Header(default=""),
) -> Identity:
    # LOCAL-DEV ONLY: BULKVID_DEV_AUTH_BYPASS_EMAIL accepts ANY request and
    # treats the caller as that email. Loud warning on every use.
    bypass_email = getattr(
        request.app.state, "dev_auth_bypass_email", ""
    ) or get_settings().BULKVID_DEV_AUTH_BYPASS_EMAIL
    if bypass_email:
        _log.warning(
            "dev_auth_bypass_active",
            email=bypass_email,
            note="UNSAFE in production",
        )
        email = bypass_email.strip().lower()
        admins = get_settings().admin_emails
        return Identity(
            email=email,
            hd=email.split("@", 1)[1] if "@" in email else None,
            name="dev-bypass",
            is_admin=email in admins,
        )

    verifier: GoogleIdentityVerifier | None = getattr(request.app.state, "verifier", None)
    if verifier is None:
        raise HTTPException(500, "auth not configured (app.state.verifier missing)")

    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(401, "missing or malformed Authorization header")

    try:
        return await verifier.verify(parts[1].strip())
    except AuthError as e:
        raise HTTPException(401, str(e))
    except ForbiddenError as e:
        raise HTTPException(403, str(e))


def get_queue(request: Request) -> JobQueue:
    queue: JobQueue | None = getattr(request.app.state, "queue", None)
    if queue is None:
        raise HTTPException(500, "queue not configured (app.state.queue missing)")
    return queue


async def _require_owned_job(job_id: str, identity: Identity, queue: JobQueue) -> Job:
    """Fetch a job or raise 404; raise 403 unless the caller owns it (admins see
    all). Shared by every per-job route."""
    job = await queue.get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if not identity.is_admin and job.user_email != identity.email:
        raise HTTPException(403, "not your job")
    return job


# ── Routes ──────────────────────────────────────────────────────────────────


@router.post("", response_model=SubmitJobOut)
async def submit_job(
    payload: SubmitJobIn,
    identity: Identity = Depends(get_identity),
    queue: JobQueue = Depends(get_queue),
) -> SubmitJobOut:
    if payload.tab_type == TAB_IMAGE_VO:
        if not payload.rows_image_vo:
            raise HTTPException(400, "rows_image_vo is required for tab_type=image_vo")
        rows: list[Any] = [
            ImageVORow(**r.model_dump()) for r in payload.rows_image_vo
        ]
    elif payload.tab_type == TAB_FOUR_IMAGES:
        if not payload.rows_four_images:
            raise HTTPException(
                400, "rows_four_images is required for tab_type=four_images_vo2"
            )
        rows = [FourImagesVO2Row(**r.model_dump()) for r in payload.rows_four_images]
    elif payload.tab_type == TAB_SIMPLE:
        if not payload.rows_simple:
            raise HTTPException(400, "rows_simple is required for tab_type=simple")
        rows = [SimpleRow(**r.model_dump()) for r in payload.rows_simple]
    elif payload.tab_type == TAB_CARTOON:
        if not payload.rows_cartoon:
            raise HTTPException(400, "rows_cartoon is required for tab_type=cartoon")
        rows = [CartoonRow(**r.model_dump()) for r in payload.rows_cartoon]
    elif payload.tab_type == TAB_SIMPLE_MOTION:
        if not payload.rows_simple_motion:
            raise HTTPException(
                400, "rows_simple_motion is required for tab_type=simple_motion"
            )
        rows = [_build_simple_motion_row(r) for r in payload.rows_simple_motion]
    elif payload.tab_type == TAB_YT_CARTOON:
        if not payload.rows_yt_cartoon:
            raise HTTPException(
                400, "rows_yt_cartoon is required for tab_type=yt_cartoon"
            )
        rows = [_build_yt_cartoon_row(r) for r in payload.rows_yt_cartoon]
    elif payload.tab_type == TAB_SIMPLE_X4:
        if not payload.rows_simple_x4:
            raise HTTPException(
                400, "rows_simple_x4 is required for tab_type=simple_x4"
            )
        rows = [
            _build_simple_x4_row(r) for r in payload.rows_simple_x4
        ]
    elif payload.tab_type == TAB_TEXT_ON_IMG:
        if not payload.rows_text_on_img:
            raise HTTPException(
                400, "rows_text_on_img is required for tab_type=text_on_img"
            )
        rows = [
            _build_text_on_img_row(r) for r in payload.rows_text_on_img
        ]
    elif payload.tab_type == TAB_AVATAR:
        if not payload.rows_avatar:
            raise HTTPException(
                400, "rows_avatar is required for tab_type=avatar"
            )
        rows = [
            _build_avatar_row(r) for r in payload.rows_avatar
        ]
    else:
        raise HTTPException(400, f"unknown tab_type: {payload.tab_type}")

    if not rows:
        raise HTTPException(400, "no rows provided")

    # Optional idempotency key — guard format before it touches the DB so a
    # malformed/oversized key can't bloat the idempotency table.
    idem_key = payload.idempotency_key
    if idem_key is not None:
        if not _IDEMPOTENCY_KEY_RE.match(idem_key):
            _log.info(
                "idempotency_key_rejected",
                user_email=identity.email,
                reason="malformed",
                length=len(idem_key),
            )
            raise HTTPException(
                400,
                "idempotency_key must match [A-Za-z0-9_-]{1,64}",
            )

    try:
        job_id = await queue.enqueue(
            user_email=identity.email,
            sheet_id=payload.sheet_id,
            worksheet=payload.worksheet,
            tab_type=payload.tab_type,
            rows=rows,
            idempotency_key=idem_key,
        )
    except QueueUnavailable as e:
        # Turso was unreachable through the full timeout + reconnect + retry
        # cycle in JobQueue._run_db. This is what used to surface as a bare
        # HTTP 500; now it's a 503 the Apps Script retries safely (the submit
        # is idempotent via the key + deterministic job_id). Plan
        # ``_plans/2026-06-17-submit-500s-turso-resilience.md``. MUST precede
        # the QueueBusy handler — QueueUnavailable subclasses it.
        _log.warning(
            "queue_unavailable_503",
            endpoint="submit_job",
            user_email=identity.email,
            original_error=str(e),
        )
        raise HTTPException(
            503,
            "queue temporarily unavailable, please retry",
            headers={"Retry-After": "5"},
        ) from e
    except QueueBusy as e:
        _log.warning(
            "queue_busy_503",
            endpoint="submit_job",
            user_email=identity.email,
            original_error=str(e),
        )
        raise HTTPException(
            503, "queue temporarily busy", headers={"Retry-After": "5"}
        ) from e

    # Resolve the actual kept row count by reading the job back. ``enqueue``
    # silently drops rows whose ``row_num`` is already pending/processing in
    # another active job for the same sheet+worksheet (the dedup guard in
    # ``_enqueue_sync``). The route caller (Apps Script) needs to see the
    # difference, otherwise a fully-dropped submit looks like "instantly
    # completed, no video" — exactly the trap from chat 2026-06-09.
    submitted_count = len(rows)
    job = await queue.get_job(job_id)
    kept_count = job.row_count if job is not None else submitted_count
    dropped_count = max(0, submitted_count - kept_count)

    if dropped_count:
        _log.warning(
            "job_submit_dropped_rows",
            job_id=job_id,
            user_email=identity.email,
            tab_type=payload.tab_type,
            submitted_count=submitted_count,
            kept_count=kept_count,
            dropped_count=dropped_count,
        )

    _log.info(
        "job_submit",
        job_id=job_id,
        user_email=identity.email,
        tab_type=payload.tab_type,
        row_count=kept_count,
        dropped_count=dropped_count,
    )
    return SubmitJobOut(
        job_id=job_id,
        status="queued" if kept_count else "completed",
        row_count=kept_count,
        dropped_count=dropped_count,
        submitted_count=submitted_count,
    )


@router.get("/poll", response_model=PollOut)
async def poll_jobs(
    limit: int = 100,
    logs: str = "",
    log_tail: int = 200,
    identity: Identity = Depends(get_identity),
    queue: JobQueue = Depends(get_queue),
) -> PollOut:
    """Single-call sidebar poll.

    Returns the same data as ``list_jobs`` + per-row status for *running* jobs
    + log tails for the panes the caller has open — in **one** authenticated
    request. This is what the Apps Script sidebar should call on every poll
    cycle instead of firing three separate requests per cycle. Cuts backend
    auth/request volume by ~5x. Ownership filtering is identical to the
    individual endpoints (bulk user sees own; admin sees all).

    Plan: ``_plans/2026-06-04-fix-sidebar-500s.md``.
    """
    started = time.monotonic()

    limit = max(1, min(limit, 500))
    log_tail = max(1, min(log_tail, 2000))

    requested_log_ids = [j.strip() for j in logs.split(",") if j.strip()]
    # Cap the requested log list so a malformed/abusive caller cannot trigger
    # an unbounded number of log file reads inside a single request.
    if len(requested_log_ids) > 50:
        raise HTTPException(400, "too many log ids requested (max 50)")

    filter_email = None if identity.is_admin else identity.email
    jobs = await queue.list_jobs(user_email=filter_email, limit=limit)
    owned_job_ids = {j.job_id for j in jobs}

    # Per-row detail only for running jobs — matches the sidebar's existing
    # behavior (queued jobs show "waiting in queue", archive rows don't need
    # refresh). Cuts per-poll DB work when there's a long archive list.
    rows_by_job: dict[str, list[JobRowOut]] = {}
    for job in jobs:
        if job.status == JOB_RUNNING:
            raw_rows = await queue.list_rows(job.job_id)
            rows_by_job[job.job_id] = [
                _row_to_out(job.job_id, r) for r in raw_rows
            ]

    # Log tails: only for job IDs the caller owns AND requested. A malicious
    # ``logs=<other_user_job_id>`` is silently ignored (not 403'd) — matches
    # the policy of never confirming the existence of other users' jobs.
    logs_by_job: dict[str, PollLogOut] = {}
    for jid in requested_log_ids:
        if jid in owned_job_ids:
            lines, exists = read_job_log_lines(jid, row=None, tail=log_tail)
            logs_by_job[jid] = PollLogOut(exists=exists, lines=lines)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    _log.info(
        "poll_request",
        user_email=identity.email,
        active_jobs=sum(1 for j in jobs if j.status in (JOB_QUEUED, JOB_RUNNING)),
        archive_jobs=sum(1 for j in jobs if j.status not in (JOB_QUEUED, JOB_RUNNING)),
        logs_requested=len(requested_log_ids),
        logs_returned=len(logs_by_job),
        elapsed_ms=elapsed_ms,
    )

    # ETA medians: cheap aggregate query against rows already in the
    # queue, hand it back as part of the poll bundle so the sidebar
    # doesn't need a second round-trip. ETA only renders when the
    # median is well-defined (≥10 samples) — the client handles that
    # filtering.
    try:
        eta_medians = await queue.eta_medians()
    except Exception as e:    # noqa: BLE001 — never let an ETA aggregate 500 a poll
        _log.warning("eta_medians_failed", err=str(e)[:200])
        eta_medians = {}

    # Queue depth banner: in-flight + queued row counts for this user
    # (admin sees the whole fleet), plus a tab-weighted ETA. Any failure
    # here drops the field to ``None`` — the sidebar hides the banner
    # in that case rather than showing stale numbers.
    queue_status: QueueStatusOut | None = None
    try:
        in_flight, queued, queued_per_tab = await queue.user_queue_depth(
            user_email=filter_email,
        )
        settings = get_settings()
        max_concurrent = max(1, int(settings.BULKVID_MAX_CONCURRENT_ROWS))
        eta_seconds: int | None
        if not queued:
            eta_seconds = 0
        elif not eta_medians:
            # No samples yet (fresh deploy) — surface "unknown" rather
            # than 0 so the sidebar shows queue depth without a wrong ETA.
            eta_seconds = None
        else:
            # Weighted: sum(per_tab_queued × median_per_tab_seconds) over
            # all queued tabs, divided by the parallelism cap. Tabs with
            # no median (never run before) fall back to the global mean
            # of the known medians so we never silently zero them out.
            mean_median = (
                sum(eta_medians.values()) / len(eta_medians)
                if eta_medians else 0.0
            )
            weighted = 0.0
            for tab, n in queued_per_tab.items():
                weighted += n * eta_medians.get(tab, mean_median)
            # The in-flight rows are already partway through; charge them
            # at ~half their tab's median so the banner doesn't double-
            # count seconds that have already elapsed.
            eta_seconds = max(0, int(weighted / max_concurrent))

        # Stuck-queued detector: only fires when the worker has rows
        # pending AND nothing in flight AND the oldest pending row has
        # been waiting longer than the threshold. In normal operation
        # (worker actively claiming) ``in_flight > 0``, so this stays
        # None and the sidebar shows no warning. Plan: chat 2026-06-09.
        stuck_queued_seconds: int | None = None
        if queued > 0 and in_flight == 0:
            try:
                oldest_age = await queue.user_oldest_pending_row_age_seconds(
                    user_email=filter_email,
                )
            except Exception as e:    # noqa: BLE001
                _log.warning("stuck_queued_age_failed", err=str(e)[:200])
                oldest_age = None
            if oldest_age is not None and oldest_age >= STUCK_QUEUED_THRESHOLD_SECONDS:
                stuck_queued_seconds = oldest_age
                _log.warning(
                    "stuck_queued_detected",
                    user_email=identity.email,
                    queued=queued,
                    oldest_age_seconds=oldest_age,
                    threshold_seconds=STUCK_QUEUED_THRESHOLD_SECONDS,
                )

        queue_status = QueueStatusOut(
            in_flight=in_flight,
            queued=queued,
            max_concurrent=max_concurrent,
            eta_seconds=eta_seconds,
            stuck_queued_seconds=stuck_queued_seconds,
        )
    except Exception as e:    # noqa: BLE001 — never let queue-depth 500 a poll
        _log.warning("queue_status_failed", err=str(e)[:200])

    return PollOut(
        jobs=[_job_to_out(j) for j in jobs],
        rows_by_job=rows_by_job,
        logs_by_job=logs_by_job,
        eta_medians_by_tab=eta_medians,
        queue_status=queue_status,
    )


# ── Avatar catalog (for Apps Script's in-sheet picker) ───────────────────────
#
# The ``video with avatar`` tab needs a way for operators to pick avatars
# WITHOUT leaving the sheet. This endpoint returns the catalog as JSON,
# bearer-authed against the user's Google OAuth ID token — same auth
# pattern as every other /jobs/* endpoint. Apps Script calls it via
# ``getAvatarCatalog()`` to populate the in-sheet picker modal.
#
# IMPORTANT: this route MUST be declared BEFORE ``GET /{job_id}`` so
# FastAPI matches the literal ``/avatars`` path first. The earlier
# version was registered at the bottom of the file and the dynamic
# ``/{job_id}`` swallowed the request, returning "job not found" with
# 404 (chat 2026-06-09).


class AvatarPickerEntry(BaseModel):
    avatar_id: str
    name: str
    gender: str
    preview_url: str


class AvatarPickerOut(BaseModel):
    avatars: list[AvatarPickerEntry]
    source: str               # "live" | "cache" | "empty"
    error: str | None


@router.get("/avatars", response_model=AvatarPickerOut)
async def list_avatars_for_picker(
    request: Request,
    identity: Identity = Depends(get_identity),
) -> AvatarPickerOut:
    """Return the avatar catalog as JSON for the Apps Script picker.
    Same fetch-with-cache-fallback strategy as /admin/avatars."""
    from bulkvid.pipeline.avatar_catalog import fetch_or_load_catalog

    store = getattr(request.app.state, "settings_store", None)
    if store is None:
        raise HTTPException(500, "settings_store not configured")
    avatars, source, error = await fetch_or_load_catalog(
        store, updated_by=identity.email or "sheet-picker",
    )
    return AvatarPickerOut(
        avatars=[AvatarPickerEntry(**a) for a in avatars],
        source=source,
        error=error,
    )


@router.get("/{job_id}", response_model=JobOut)
async def get_one_job(
    job_id: str,
    identity: Identity = Depends(get_identity),
    queue: JobQueue = Depends(get_queue),
) -> JobOut:
    job = await _require_owned_job(job_id, identity, queue)
    return _job_to_out(job)


@router.get("/{job_id}/rows", response_model=JobRowsOut)
async def get_job_rows(
    job_id: str,
    identity: Identity = Depends(get_identity),
    queue: JobQueue = Depends(get_queue),
) -> JobRowsOut:
    """Per-row status for one job: row_num, status, error, video URLs. Lets the
    sidebar show live per-row progress instead of a single job-level counter."""
    await _require_owned_job(job_id, identity, queue)
    rows = await queue.list_rows(job_id)
    return JobRowsOut(
        job_id=job_id,
        rows=[_row_to_out(job_id, r) for r in rows],
    )


@router.get("/{job_id}/log", response_model=JobLogOut)
async def get_job_log(
    job_id: str,
    row: int | None = None,
    tail: int = 200,
    identity: Identity = Depends(get_identity),
    queue: JobQueue = Depends(get_queue),
) -> JobLogOut:
    """Tail of a job's log (optionally one row), token-gated for the sidebar.
    Mirrors the admin log viewer without needing an admin session cookie."""
    await _require_owned_job(job_id, identity, queue)
    lines, exists = read_job_log_lines(job_id, row=row, tail=tail)
    return JobLogOut(job_id=job_id, exists=exists, lines=lines)


@router.get("", response_model=list[JobOut])
async def list_jobs(
    limit: int = 50,
    identity: Identity = Depends(get_identity),
    queue: JobQueue = Depends(get_queue),
) -> list[JobOut]:
    limit = max(1, min(limit, 500))
    # Admins see everyone; bulk users see their own.
    filter_email = None if identity.is_admin else identity.email
    jobs = await queue.list_jobs(user_email=filter_email, limit=limit)
    return [_job_to_out(j) for j in jobs]


@router.post("/kill-all")
async def kill_all_jobs(
    identity: Identity = Depends(get_identity),
    queue: JobQueue = Depends(get_queue),
) -> dict[str, Any]:
    """Clear the queue: kill all active jobs. Bulk users clear their own;
    admins clear everyone's. Pending and in-flight rows are aborted with
    a ``killed by user`` result so the sidebar reflects the kill
    immediately. Plan ``_plans/2026-06-14-stuck-processing-rows.md`` §B."""
    scope = None if identity.is_admin else identity.email
    try:
        killed, rows_aborted = await asyncio.wait_for(
            queue.kill_all_jobs(user_email=scope),
            timeout=_KILL_CALL_TIMEOUT_SECONDS,
        )
    except TimeoutError as e:
        _log.warning(
            "kill_call_timeout",
            endpoint="kill_all_jobs",
            user_email=identity.email,
            timeout_s=_KILL_CALL_TIMEOUT_SECONDS,
        )
        raise HTTPException(
            504,
            "kill timed out — worker may be hung; restart the backend",
        ) from e
    except QueueBusy as e:
        _log.warning(
            "queue_busy_503",
            endpoint="kill_all_jobs",
            user_email=identity.email,
            original_error=str(e),
        )
        raise HTTPException(
            503, "queue temporarily busy", headers={"Retry-After": "5"}
        ) from e
    _log.info(
        "jobs_kill_all",
        by=identity.email,
        scope=scope or "ALL",
        killed=killed,
        rows_aborted=rows_aborted,
    )
    return {"killed": killed, "rows_aborted": rows_aborted}


@router.post("/{job_id}/kill")
async def kill_job(
    job_id: str,
    identity: Identity = Depends(get_identity),
    queue: JobQueue = Depends(get_queue),
) -> dict[str, Any]:
    """Kill ``job_id`` and abort its pending/in-flight rows. Bounded by
    a hard 10 s timeout so a hung libsql roundtrip surfaces as a 504
    instead of pinning the request forever (the symptom the operator
    saw on 2026-06-14 as "doesn't let killing this process"). Plan
    ``_plans/2026-06-14-stuck-processing-rows.md`` §B."""
    # Ownership check uses ``queue.get_job`` (another libsql call) —
    # wrap it in the same timeout so a hung Turso doesn't pin the
    # request before we even get to the kill itself.
    try:
        await asyncio.wait_for(
            _require_owned_job(job_id, identity, queue),
            timeout=_KILL_CALL_TIMEOUT_SECONDS,
        )
        killed, rows_aborted = await asyncio.wait_for(
            queue.kill_job(job_id),
            timeout=_KILL_CALL_TIMEOUT_SECONDS,
        )
    except TimeoutError as e:
        _log.warning(
            "kill_call_timeout",
            endpoint="kill_job",
            job_id=job_id,
            user_email=identity.email,
            timeout_s=_KILL_CALL_TIMEOUT_SECONDS,
        )
        raise HTTPException(
            504,
            "kill timed out — worker may be hung; restart the backend",
        ) from e
    except QueueBusy as e:
        _log.warning(
            "queue_busy_503",
            endpoint="kill_job",
            user_email=identity.email,
            original_error=str(e),
        )
        raise HTTPException(
            503, "queue temporarily busy", headers={"Retry-After": "5"}
        ) from e
    _log.info(
        "job_kill",
        job_id=job_id,
        by=identity.email,
        killed=killed,
        rows_aborted=rows_aborted,
    )
    return {"job_id": job_id, "killed": killed, "rows_aborted": rows_aborted}
