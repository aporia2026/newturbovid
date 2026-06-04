"""Tests for ``JobQueue.eta_medians`` — the per-tab median row processing
time the sidebar uses to render a rough ETA next to the live elapsed
counter.

Plan: ``_plans/2026-06-04-sidebar-ux-overhaul.md`` §Phase 3.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bulkvid.orchestrator.queue import (
    JOB_COMPLETED,
    ROW_DONE,
    TAB_CARTOON,
    TAB_IMAGE_VO,
    TAB_SIMPLE,
    JobQueue,
)


@pytest.fixture
def queue(tmp_path: Path) -> JobQueue:
    q = JobQueue(tmp_path / "jobs.db")
    yield q
    q.close()


def _seed_done_row(
    q: JobQueue,
    *,
    job_id: str,
    tab_type: str,
    row_num: int,
    started_at: str,
    finished_at: str,
) -> None:
    """Insert a job + done row directly so the median query has data."""
    # Job row (status='completed' so it doesn't show as active anywhere).
    q._conn.execute(
        "INSERT OR IGNORE INTO jobs "
        "(job_id, user_email, sheet_id, worksheet, tab_type, status, "
        " row_count, created_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            job_id, "tester@aporia.com", "sheet-X", "X",
            tab_type, JOB_COMPLETED, 1, started_at, finished_at,
        ),
    )
    q._conn.execute(
        "INSERT INTO row_queue "
        "(job_id, row_num, payload, status, started_at, finished_at, result) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            job_id, row_num, "{}", ROW_DONE,
            started_at, finished_at, json.dumps({"status": "SUCCESS"}),
        ),
    )


# ── Happy paths ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_db_returns_no_medians(queue: JobQueue) -> None:
    medians = await queue.eta_medians()
    assert medians == {}


@pytest.mark.asyncio
async def test_single_row_returns_that_row_as_median(queue: JobQueue) -> None:
    _seed_done_row(
        queue,
        job_id="job-a",
        tab_type=TAB_SIMPLE,
        row_num=1,
        started_at="2026-06-04T12:00:00+00:00",
        finished_at="2026-06-04T12:00:50+00:00",    # 50 sec
    )
    medians = await queue.eta_medians()
    assert medians == {TAB_SIMPLE: 50.0}


@pytest.mark.asyncio
async def test_odd_count_picks_middle(queue: JobQueue) -> None:
    """3 rows of 10, 20, 30 sec → median 20."""
    for i, (start, finish) in enumerate(
        [
            ("2026-06-04T12:00:00+00:00", "2026-06-04T12:00:10+00:00"),    # 10
            ("2026-06-04T12:01:00+00:00", "2026-06-04T12:01:20+00:00"),    # 20
            ("2026-06-04T12:02:00+00:00", "2026-06-04T12:02:30+00:00"),    # 30
        ],
        start=1,
    ):
        _seed_done_row(
            queue,
            job_id=f"job-{i}",
            tab_type=TAB_IMAGE_VO,
            row_num=i,
            started_at=start,
            finished_at=finish,
        )
    medians = await queue.eta_medians()
    assert medians == {TAB_IMAGE_VO: 20.0}


@pytest.mark.asyncio
async def test_even_count_averages_two_middles(queue: JobQueue) -> None:
    """4 rows of 10, 20, 30, 40 → median (20+30)/2 = 25."""
    for i, (start, finish) in enumerate(
        [
            ("2026-06-04T12:00:00+00:00", "2026-06-04T12:00:10+00:00"),    # 10
            ("2026-06-04T12:01:00+00:00", "2026-06-04T12:01:20+00:00"),    # 20
            ("2026-06-04T12:02:00+00:00", "2026-06-04T12:02:30+00:00"),    # 30
            ("2026-06-04T12:03:00+00:00", "2026-06-04T12:03:40+00:00"),    # 40
        ],
        start=1,
    ):
        _seed_done_row(
            queue,
            job_id=f"job-{i}",
            tab_type=TAB_CARTOON,
            row_num=i,
            started_at=start,
            finished_at=finish,
        )
    medians = await queue.eta_medians()
    assert medians == {TAB_CARTOON: 25.0}


@pytest.mark.asyncio
async def test_groups_by_tab_type(queue: JobQueue) -> None:
    """Two tabs with different runtimes — medians must not mix."""
    _seed_done_row(
        queue,
        job_id="job-s1",
        tab_type=TAB_SIMPLE,
        row_num=1,
        started_at="2026-06-04T12:00:00+00:00",
        finished_at="2026-06-04T12:00:50+00:00",    # 50
    )
    _seed_done_row(
        queue,
        job_id="job-c1",
        tab_type=TAB_CARTOON,
        row_num=1,
        started_at="2026-06-04T13:00:00+00:00",
        finished_at="2026-06-04T13:08:00+00:00",    # 480
    )
    medians = await queue.eta_medians()
    assert medians[TAB_SIMPLE] == 50.0
    assert medians[TAB_CARTOON] == 480.0


@pytest.mark.asyncio
async def test_sample_cap_limits_per_tab(queue: JobQueue) -> None:
    """``sample_size=3`` over 5 rows must use only the 3 newest."""
    # Insert in order from oldest to newest; finished_at increases.
    for i, (start, finish) in enumerate(
        [
            ("2026-06-04T10:00:00+00:00", "2026-06-04T10:01:40+00:00"),    # 100 (oldest)
            ("2026-06-04T11:00:00+00:00", "2026-06-04T11:01:40+00:00"),    # 100
            ("2026-06-04T12:00:00+00:00", "2026-06-04T12:00:10+00:00"),    # 10
            ("2026-06-04T13:00:00+00:00", "2026-06-04T13:00:20+00:00"),    # 20
            ("2026-06-04T14:00:00+00:00", "2026-06-04T14:00:30+00:00"),    # 30 (newest)
        ],
        start=1,
    ):
        _seed_done_row(
            queue,
            job_id=f"job-{i}",
            tab_type=TAB_SIMPLE,
            row_num=i,
            started_at=start,
            finished_at=finish,
        )
    # With sample_size=3 the newest three are 10, 20, 30 → median 20.
    medians = await queue.eta_medians(sample_size=3)
    assert medians == {TAB_SIMPLE: 20.0}


@pytest.mark.asyncio
async def test_rows_without_finished_at_excluded(queue: JobQueue) -> None:
    """A still-processing row must NOT pollute the median (no finished_at)."""
    _seed_done_row(
        queue,
        job_id="job-good",
        tab_type=TAB_SIMPLE,
        row_num=1,
        started_at="2026-06-04T12:00:00+00:00",
        finished_at="2026-06-04T12:00:50+00:00",
    )
    # Insert a half-finished row manually — no finished_at.
    queue._conn.execute(
        "INSERT OR IGNORE INTO jobs "
        "(job_id, user_email, sheet_id, worksheet, tab_type, status, "
        " row_count, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            "job-pending", "tester@aporia.com", "sheet-X", "X",
            TAB_SIMPLE, "running", 1, "2026-06-04T12:01:00+00:00",
        ),
    )
    queue._conn.execute(
        "INSERT INTO row_queue "
        "(job_id, row_num, payload, status, started_at) "
        "VALUES (?,?,?,?,?)",
        ("job-pending", 1, "{}", "processing", "2026-06-04T12:01:00+00:00"),
    )
    medians = await queue.eta_medians()
    assert medians == {TAB_SIMPLE: 50.0}
