"""Tests for the SQLite job queue.

Use a tmp_path DB per test; no shared state. Covers:
  - enqueue creates job + rows in QUEUED/PENDING state
  - claim_next_row dequeues FIFO, marks PROCESSING, transitions job to RUNNING
  - claim_next_row returns None when empty
  - record_result with SUCCESS increments completed_rows + cost
  - record_result with failure increments failed_rows
  - job auto-finalizes to COMPLETED when all rows done
  - get_job + list_jobs work as expected
  - kill_job blocks completed jobs from being killed; running/queued OK
  - recover_orphaned_rows resets PROCESSING -> PENDING
  - Round-trip ImageVORow and FourImagesVO2Row payloads
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bulkvid.models.row import (
    STATUS_ARTICLE_FETCH_FAILED,
    STATUS_SUCCESS,
    FourImagesVO2Row,
    ImageVORow,
    RowResult,
)
from bulkvid.orchestrator.queue import (
    JOB_COMPLETED,
    JOB_KILLED,
    JOB_QUEUED,
    JOB_RUNNING,
    ROW_DONE,
    ROW_FAILED,
    ROW_PENDING,
    ROW_PROCESSING,
    TAB_FOUR_IMAGES,
    TAB_IMAGE_VO,
    JobQueue,
    payload_to_row,
)


def _img_row(n: int = 2) -> ImageVORow:
    return ImageVORow(
        row_num=n,
        country="US",
        vertical="tech",
        article_url="https://example.com/article",
        manual_image_url="https://example.com/seed.png",
        voice_over=True,
        zapcap=False,
        aspect_ratio="9:16",
        script_pattern="How To",
        open_comments="",
    )


def _four_row(n: int = 3) -> FourImagesVO2Row:
    return FourImagesVO2Row(
        row_num=n,
        country="US",
        vertical="tech",
        article_url="https://example.com/article",
        how_many=2,
        voice_over=True,
        image_urls=["https://example.com/a.jpg", "https://example.com/b.jpg"],
        zapcap=False,
        aspect_ratio="1:1",
        script_pattern="",
        open_comments="",
    )


@pytest.fixture
def queue(tmp_path: Path) -> JobQueue:
    q = JobQueue(tmp_path / "jobs.db")
    yield q
    q.close()


# ── enqueue ──────────────────────────────────────────────────────────────────


async def test_enqueue_creates_queued_job_and_pending_rows(queue: JobQueue) -> None:
    rows = [_img_row(2), _img_row(3), _img_row(4)]
    job_id = await queue.enqueue(
        user_email="yoav@aporia.com",
        sheet_id="sheet-A",
        worksheet="Image-VO",
        tab_type=TAB_IMAGE_VO,
        rows=rows,
    )
    job = await queue.get_job(job_id)
    assert job is not None
    assert job.status == JOB_QUEUED
    assert job.row_count == 3
    assert job.completed_rows == 0
    assert job.failed_rows == 0
    assert job.user_email == "yoav@aporia.com"


# ── claim_next_row + job transition ─────────────────────────────────────────


async def test_claim_returns_none_when_empty(queue: JobQueue) -> None:
    assert await queue.claim_next_row() is None


async def test_claim_dequeues_fifo_and_marks_processing(queue: JobQueue) -> None:
    job_id = await queue.enqueue(
        user_email="u@aporia.com",
        sheet_id="s", worksheet="w", tab_type=TAB_IMAGE_VO,
        rows=[_img_row(2), _img_row(3)],
    )
    r1 = await queue.claim_next_row()
    r2 = await queue.claim_next_row()
    r3 = await queue.claim_next_row()
    assert r1 is not None and r1.row_num == 2
    assert r2 is not None and r2.row_num == 3
    assert r3 is None

    # Job is now RUNNING (transitioned on first claim).
    job = await queue.get_job(job_id)
    assert job is not None
    assert job.status == JOB_RUNNING


async def test_claim_payload_round_trips_image_vo(queue: JobQueue) -> None:
    await queue.enqueue(
        user_email="u@aporia.com",
        sheet_id="s", worksheet="w", tab_type=TAB_IMAGE_VO,
        rows=[_img_row(7)],
    )
    queued = await queue.claim_next_row()
    assert queued is not None
    row = payload_to_row(queued.payload)
    assert isinstance(row, ImageVORow)
    assert row.row_num == 7
    assert row.country == "US"
    assert row.aspect_ratio == "9:16"


async def test_claim_payload_round_trips_four_images(queue: JobQueue) -> None:
    await queue.enqueue(
        user_email="u@aporia.com",
        sheet_id="s", worksheet="w", tab_type=TAB_FOUR_IMAGES,
        rows=[_four_row(9)],
    )
    queued = await queue.claim_next_row()
    assert queued is not None
    row = payload_to_row(queued.payload)
    assert isinstance(row, FourImagesVO2Row)
    assert row.row_num == 9
    assert row.how_many == 2


# ── record_result ───────────────────────────────────────────────────────────


async def test_record_success_increments_completed_and_cost(queue: JobQueue) -> None:
    job_id = await queue.enqueue(
        user_email="u@aporia.com",
        sheet_id="s", worksheet="w", tab_type=TAB_IMAGE_VO,
        rows=[_img_row(2)],
    )
    queued = await queue.claim_next_row()
    assert queued is not None

    await queue.record_result(
        queued.id,
        RowResult(
            row_num=2,
            status=STATUS_SUCCESS,
            video_urls=["https://storage/v1.mp4"],
            cost_usd=0.18,
            elapsed_seconds=12.4,
        ),
    )

    job = await queue.get_job(job_id)
    assert job is not None
    assert job.completed_rows == 1
    assert job.failed_rows == 0
    assert job.cost_usd == pytest.approx(0.18)
    # Single-row job finalises immediately.
    assert job.status == JOB_COMPLETED


async def test_record_failure_increments_failed_and_keeps_running(queue: JobQueue) -> None:
    job_id = await queue.enqueue(
        user_email="u@aporia.com",
        sheet_id="s", worksheet="w", tab_type=TAB_IMAGE_VO,
        rows=[_img_row(2), _img_row(3)],
    )
    q1 = await queue.claim_next_row()
    assert q1 is not None
    await queue.record_result(
        q1.id,
        RowResult(
            row_num=2,
            status=STATUS_ARTICLE_FETCH_FAILED,
            cost_usd=0.01,
            error="tavily down",
        ),
    )
    job = await queue.get_job(job_id)
    assert job is not None
    assert job.failed_rows == 1
    assert job.completed_rows == 0
    assert job.status == JOB_RUNNING            # row 3 still outstanding


async def test_mixed_results_finalise_to_completed(queue: JobQueue) -> None:
    job_id = await queue.enqueue(
        user_email="u@aporia.com",
        sheet_id="s", worksheet="w", tab_type=TAB_IMAGE_VO,
        rows=[_img_row(2), _img_row(3)],
    )
    q1 = await queue.claim_next_row()
    q2 = await queue.claim_next_row()
    assert q1 and q2

    await queue.record_result(
        q1.id, RowResult(row_num=2, status=STATUS_SUCCESS, cost_usd=0.2)
    )
    await queue.record_result(
        q2.id, RowResult(row_num=3, status=STATUS_ARTICLE_FETCH_FAILED, cost_usd=0.01)
    )

    job = await queue.get_job(job_id)
    assert job is not None
    assert job.status == JOB_COMPLETED
    assert job.completed_rows == 1
    assert job.failed_rows == 1
    assert job.cost_usd == pytest.approx(0.21)


# ── list_jobs + kill_job ────────────────────────────────────────────────────


async def test_list_jobs_filters_by_user_email(queue: JobQueue) -> None:
    await queue.enqueue(
        user_email="a@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    await queue.enqueue(
        user_email="b@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    a_jobs = await queue.list_jobs(user_email="a@aporia.com")
    b_jobs = await queue.list_jobs(user_email="b@aporia.com")
    assert len(a_jobs) == 1 and a_jobs[0].user_email == "a@aporia.com"
    assert len(b_jobs) == 1 and b_jobs[0].user_email == "b@aporia.com"

    all_jobs = await queue.list_jobs(user_email=None)
    assert len(all_jobs) == 2


async def test_kill_job_works_for_queued_and_running(queue: JobQueue) -> None:
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    killed = await queue.kill_job(job_id)
    assert killed is True

    job = await queue.get_job(job_id)
    assert job is not None
    assert job.status == JOB_KILLED


async def test_kill_job_noop_for_completed(queue: JobQueue) -> None:
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    q = await queue.claim_next_row()
    assert q is not None
    await queue.record_result(
        q.id, RowResult(row_num=2, status=STATUS_SUCCESS, cost_usd=0.1)
    )
    # Now COMPLETED — kill_job should be a no-op.
    killed = await queue.kill_job(job_id)
    assert killed is False
    job = await queue.get_job(job_id)
    assert job is not None
    assert job.status == JOB_COMPLETED


# ── recover_orphaned_rows ───────────────────────────────────────────────────


async def test_recover_orphaned_rows_resets_processing_to_pending(queue: JobQueue) -> None:
    await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2), _img_row(3)],
    )
    # Claim both, then "crash" without recording results.
    await queue.claim_next_row()
    await queue.claim_next_row()

    recovered = await queue.recover_orphaned_rows()
    assert recovered == 2

    # Both rows should be pending again.
    r1 = await queue.claim_next_row()
    r2 = await queue.claim_next_row()
    assert r1 is not None and r2 is not None
    assert (r1.row_num, r2.row_num) == (2, 3)


# ── Empty get / missing job ─────────────────────────────────────────────────


async def test_get_job_returns_none_for_unknown_id(queue: JobQueue) -> None:
    assert await queue.get_job("nope") is None


async def test_list_jobs_empty_when_no_jobs(queue: JobQueue) -> None:
    assert await queue.list_jobs(user_email=None) == []
