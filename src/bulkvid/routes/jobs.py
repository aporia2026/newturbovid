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

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from bulkvid.auth import AuthError, ForbiddenError, GoogleIdentityVerifier, Identity
from bulkvid.config import get_settings
from bulkvid.logging import get_logger, read_job_log_lines
from bulkvid.models.row import CartoonRow, FourImagesVO2Row, ImageVORow, SimpleRow
from bulkvid.orchestrator.queue import (
    TAB_CARTOON,
    TAB_FOUR_IMAGES,
    TAB_IMAGE_VO,
    TAB_SIMPLE,
    Job,
    JobQueue,
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


class SubmitJobOut(BaseModel):
    job_id: str
    status: str
    row_count: int


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


class JobRowsOut(BaseModel):
    job_id: str
    rows: list[JobRowOut]


class JobLogOut(BaseModel):
    job_id: str
    exists: bool
    lines: list[str]


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
    else:
        raise HTTPException(400, f"unknown tab_type: {payload.tab_type}")

    if not rows:
        raise HTTPException(400, "no rows provided")

    job_id = await queue.enqueue(
        user_email=identity.email,
        sheet_id=payload.sheet_id,
        worksheet=payload.worksheet,
        tab_type=payload.tab_type,
        rows=rows,
    )
    _log.info(
        "job_submit",
        job_id=job_id,
        user_email=identity.email,
        tab_type=payload.tab_type,
        row_count=len(rows),
    )
    return SubmitJobOut(job_id=job_id, status="queued", row_count=len(rows))


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
    return JobRowsOut(job_id=job_id, rows=[JobRowOut(**r) for r in rows])


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
    admins clear everyone's. In-flight rows finish; pending rows stop."""
    scope = None if identity.is_admin else identity.email
    killed = await queue.kill_all_jobs(user_email=scope)
    _log.info("jobs_kill_all", by=identity.email, scope=scope or "ALL", killed=killed)
    return {"killed": killed}


@router.post("/{job_id}/kill")
async def kill_job(
    job_id: str,
    identity: Identity = Depends(get_identity),
    queue: JobQueue = Depends(get_queue),
) -> dict[str, Any]:
    await _require_owned_job(job_id, identity, queue)
    killed = await queue.kill_job(job_id)
    _log.info("job_kill", job_id=job_id, by=identity.email, killed=killed)
    return {"job_id": job_id, "killed": killed}
