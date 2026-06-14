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
    CartoonRow,
    FourImagesVO2Row,
    ImageVORow,
    RowResult,
)
from bulkvid.orchestrator.queue import (
    JOB_COMPLETED,
    JOB_KILLED,
    JOB_QUEUED,
    JOB_RUNNING,
    TAB_CARTOON,
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


def _cartoon_row(n: int = 4) -> CartoonRow:
    return CartoonRow(
        row_num=n,
        country="MX",
        vertical="automotive",
        article_url="https://example.com/article",
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


async def test_claim_payload_round_trips_cartoon(queue: JobQueue) -> None:
    await queue.enqueue(
        user_email="u@aporia.com",
        sheet_id="s", worksheet="w", tab_type=TAB_CARTOON,
        rows=[_cartoon_row(4)],
    )
    queued = await queue.claim_next_row()
    assert queued is not None
    row = payload_to_row(queued.payload)
    assert isinstance(row, CartoonRow)
    assert row.row_num == 4
    assert row.vertical == "automotive"


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
    killed, rows_aborted = await queue.kill_job(job_id)
    assert killed is True
    # Plan ``_plans/2026-06-14-stuck-processing-rows.md`` §B: kill_job now
    # also aborts the job's pending/processing rows. One pending row enqueued,
    # so exactly one should have been touched.
    assert rows_aborted == 1

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
    killed, rows_aborted = await queue.kill_job(job_id)
    assert killed is False
    assert rows_aborted == 0
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


# ── Claim guard: killed jobs stop draining ──────────────────────────────────


async def test_killed_job_pending_rows_are_not_claimed(queue: JobQueue) -> None:
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2), _img_row(3)],
    )
    killed, rows_aborted = await queue.kill_job(job_id)
    assert killed is True
    # Plan ``_plans/2026-06-14-stuck-processing-rows.md`` §B: both pending
    # rows are aborted (status -> ROW_FAILED), not left as ROW_PENDING.
    assert rows_aborted == 2
    # A killed job's rows must NOT be handed to the worker.
    assert await queue.claim_next_row() is None


# ── Enqueue dedup: a row can't run twice at once ────────────────────────────


async def test_enqueue_skips_rows_already_active(queue: JobQueue) -> None:
    await queue.enqueue(
        user_email="u@aporia.com", sheet_id="S", worksheet="W",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    job2 = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="S", worksheet="W",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2), _img_row(3)],
    )
    j2 = await queue.get_job(job2)
    assert j2 is not None and j2.row_count == 1            # row 2 deduped away
    assert [r["row_num"] for r in await queue.list_rows(job2)] == [3]


async def test_enqueue_all_duplicates_marks_job_completed(queue: JobQueue) -> None:
    await queue.enqueue(
        user_email="u@aporia.com", sheet_id="S", worksheet="W",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    job2 = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="S", worksheet="W",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    j2 = await queue.get_job(job2)
    assert j2 is not None and j2.row_count == 0 and j2.status == JOB_COMPLETED


async def test_enqueue_dedup_is_scoped_to_sheet_and_worksheet(queue: JobQueue) -> None:
    await queue.enqueue(
        user_email="u@aporia.com", sheet_id="S", worksheet="W1",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    # Same row number, DIFFERENT worksheet -> not a duplicate.
    job2 = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="S", worksheet="W2",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    j2 = await queue.get_job(job2)
    assert j2 is not None and j2.row_count == 1


async def test_enqueue_allows_rerun_after_job_no_longer_active(queue: JobQueue) -> None:
    # A finished job's row is NOT an active duplicate — a deliberate rerun works.
    job1 = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="S", worksheet="W",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    killed1, _aborted1 = await queue.kill_job(job1)        # job1 no longer active
    assert killed1 is True
    job2 = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="S", worksheet="W",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    j2 = await queue.get_job(job2)
    assert j2 is not None and j2.row_count == 1


# ── kill_all_jobs ───────────────────────────────────────────────────────────


async def test_kill_all_jobs_kills_every_active_job(queue: JobQueue) -> None:
    j1 = await queue.enqueue(
        user_email="a@x.com", sheet_id="A", worksheet="W",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    j2 = await queue.enqueue(
        user_email="b@x.com", sheet_id="B", worksheet="W",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    killed, rows_aborted = await queue.kill_all_jobs()
    assert killed == 2
    # Both jobs' single pending rows get aborted.
    assert rows_aborted == 2
    assert (await queue.get_job(j1)).status == JOB_KILLED
    assert (await queue.get_job(j2)).status == JOB_KILLED


async def test_kill_all_jobs_scoped_to_one_user(queue: JobQueue) -> None:
    j_a = await queue.enqueue(
        user_email="a@x.com", sheet_id="A", worksheet="W",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    j_b = await queue.enqueue(
        user_email="b@x.com", sheet_id="B", worksheet="W",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    killed, rows_aborted = await queue.kill_all_jobs(user_email="a@x.com")
    assert killed == 1
    # Only a@x.com's row was aborted; b@x.com's row stays pending.
    assert rows_aborted == 1
    assert (await queue.get_job(j_a)).status == JOB_KILLED
    assert (await queue.get_job(j_b)).status == JOB_QUEUED


# ── Kill cleans up rows (Plan §B) ───────────────────────────────────────────


async def test_kill_job_aborts_processing_and_pending_rows(queue: JobQueue) -> None:
    """Plan ``_plans/2026-06-14-stuck-processing-rows.md`` §B: kill must
    also resolve in-flight uncertainty — any PROCESSING or PENDING row
    for the killed job is marked FAILED with a ``killed by user`` result.
    Before this fix the sidebar showed "Starting.." indefinitely on rows
    whose ``record_result`` write was lost to a Turso flap."""
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2), _img_row(3), _img_row(4)],
    )
    # Claim two rows (PROCESSING), leave one PENDING.
    r1 = await queue.claim_next_row()
    r2 = await queue.claim_next_row()
    assert r1 is not None and r2 is not None

    killed, rows_aborted = await queue.kill_job(job_id)
    assert killed is True
    assert rows_aborted == 3        # 2 processing + 1 pending

    job = await queue.get_job(job_id)
    assert job is not None
    assert job.status == JOB_KILLED
    # Aborted rows count against failed_rows so the sidebar archive's
    # "done/total" tally adds up to row_count.
    assert job.failed_rows == 3

    rows = await queue.list_rows(job_id)
    assert {r["row_num"] for r in rows} == {2, 3, 4}
    for r in rows:
        assert r["status"] == "failed"
        # ``_list_rows_sync`` pulls ``error`` out of the result JSON;
        # the kill writer puts the sidebar-friendly message there.
        assert r["error"] == "killed by user"


async def test_kill_job_leaves_done_and_already_failed_rows_alone(
    queue: JobQueue,
) -> None:
    """Kill resolves UNCERTAINTY (pending/processing). Rows that already
    settled to a terminal state stay where they are — overwriting their
    history would lose forensic information about what actually happened.
    """
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2), _img_row(3), _img_row(4)],
    )
    r1 = await queue.claim_next_row()
    r2 = await queue.claim_next_row()
    assert r1 is not None and r2 is not None
    await queue.record_result(
        r1.id,
        RowResult(
            row_num=2, status=STATUS_SUCCESS,
            video_urls=["https://example.com/v.mp4"], cost_usd=0.1,
        ),
    )
    await queue.record_result(
        r2.id,
        RowResult(
            row_num=3, status=STATUS_ARTICLE_FETCH_FAILED,
            error="real failure", cost_usd=0.0,
        ),
    )

    killed, rows_aborted = await queue.kill_job(job_id)
    assert killed is True
    # Only the one still-PENDING row (row 4) is aborted.
    assert rows_aborted == 1

    rows = {r["row_num"]: r for r in await queue.list_rows(job_id)}
    assert rows[2]["status"] == "done"
    assert rows[2]["error"] is None
    assert rows[3]["status"] == "failed"
    assert rows[3]["error"] == "real failure"         # NOT overwritten
    assert rows[4]["status"] == "failed"
    assert rows[4]["error"] == "killed by user"


async def test_kill_all_jobs_aborts_rows_across_multiple_jobs(
    queue: JobQueue,
) -> None:
    j1 = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="S", worksheet="W1",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2), _img_row(3)],
    )
    j2 = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="S", worksheet="W2",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    # Claim one from j1 so it's PROCESSING; the rest stay PENDING.
    await queue.claim_next_row()

    killed, rows_aborted = await queue.kill_all_jobs(user_email="u@aporia.com")
    assert killed == 2
    assert rows_aborted == 3       # 2 from j1 + 1 from j2

    # Each parent's failed_rows is bumped to match its share.
    assert (await queue.get_job(j1)).failed_rows == 2
    assert (await queue.get_job(j2)).failed_rows == 1


async def test_record_result_does_not_overwrite_killed_row(
    queue: JobQueue,
) -> None:
    """Race guard: kill landed between processor start and result
    hand-back. The worker still calls ``record_result`` when its row
    finally completes, but the row is already FAILED — we must not
    overwrite it back to DONE and silently undo the operator's kill.
    Plan ``_plans/2026-06-14-stuck-processing-rows.md`` §B."""
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    queued = await queue.claim_next_row()
    assert queued is not None
    # Operator killed mid-flight — row is now FAILED with "killed by user".
    killed, rows_aborted = await queue.kill_job(job_id)
    assert killed is True and rows_aborted == 1
    # Processor finished after the kill; result lands successfully.
    await queue.record_result(
        queued.id,
        RowResult(
            row_num=2, status=STATUS_SUCCESS,
            video_urls=["https://example.com/v.mp4"], cost_usd=0.1,
        ),
    )
    rows = {r["row_num"]: r for r in await queue.list_rows(job_id)}
    assert rows[2]["status"] == "failed"
    assert rows[2]["error"] == "killed by user"
    job = await queue.get_job(job_id)
    # Cost MUST NOT have been added to the job (we did not actually
    # accept the SUCCESS) — the kill is the authoritative truth.
    assert job.completed_rows == 0
    assert job.cost_usd == 0.0


# ── _tx() transaction boundaries ────────────────────────────────────────────


def test_tx_commits_multi_statement_block(queue: JobQueue) -> None:
    """Two inserts inside one _tx() block must both be visible after commit."""
    with queue._tx():
        queue._conn.execute(
            "INSERT INTO jobs (job_id, user_email, sheet_id, worksheet, "
            "tab_type, status, row_count, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("tx-test-1", "u@x.com", "s", "w", TAB_IMAGE_VO, JOB_QUEUED, 1, "2026-06-04T00:00:00+00:00"),
        )
        queue._conn.execute(
            "INSERT INTO row_queue (job_id, row_num, payload, status) "
            "VALUES (?,?,?,?)",
            ("tx-test-1", 2, '{"x": 1}', "pending"),
        )
    job_rows = queue._conn.execute(
        "SELECT job_id FROM jobs WHERE job_id = ?", ("tx-test-1",)
    ).fetchall()
    queue_rows = queue._conn.execute(
        "SELECT row_num FROM row_queue WHERE job_id = ?", ("tx-test-1",)
    ).fetchall()
    assert len(job_rows) == 1
    assert len(queue_rows) == 1


def test_tx_rolls_back_on_exception(queue: JobQueue) -> None:
    """An exception inside _tx() must undo every statement in the block —
    confirms the new explicit BEGIN IMMEDIATE / ROLLBACK actually works
    instead of the autocommit no-op the code had before."""
    with pytest.raises(RuntimeError, match="boom"):
        with queue._tx():
            queue._conn.execute(
                "INSERT INTO jobs (job_id, user_email, sheet_id, worksheet, "
                "tab_type, status, row_count, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("tx-rollback", "u@x.com", "s", "w", TAB_IMAGE_VO, JOB_QUEUED, 1, "2026-06-04T00:00:00+00:00"),
            )
            raise RuntimeError("boom")

    rows = queue._conn.execute(
        "SELECT job_id FROM jobs WHERE job_id = ?", ("tx-rollback",)
    ).fetchall()
    assert rows == []                          # rolled back, never persisted
