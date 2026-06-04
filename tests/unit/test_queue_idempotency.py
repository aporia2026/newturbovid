"""Idempotency-key correctness on the JobQueue.

These are queue-layer tests for the idempotency primitive used by the submit
endpoint to make a retried POST safe when PA's frontend drops the response.
Route-level integration is covered in ``test_routes_jobs.py``.

Plan: ``_plans/2026-06-04-submit-500-defensive-fix.md`` §"Change 1".
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from bulkvid.models.row import ImageVORow
from bulkvid.orchestrator.queue import (
    IDEMPOTENCY_TTL_SECONDS,
    TAB_IMAGE_VO,
    JobQueue,
)


def _row(row_num: int = 2) -> ImageVORow:
    return ImageVORow(
        row_num=row_num,
        country="US",
        vertical="tech",
        article_url="https://example.com/a",
        manual_image_url="https://example.com/img.png",
        voice_over=True,
        zapcap=False,
        aspect_ratio="9:16",
        script_pattern="How To",
        open_comments="",
    )


@pytest.fixture
def queue(tmp_path: Path) -> JobQueue:
    q = JobQueue(tmp_path / "jobs.db")
    yield q
    q.close()


# ── Lookup ──────────────────────────────────────────────────────────────────


def test_lookup_unknown_key_returns_none(queue: JobQueue) -> None:
    assert queue._lookup_idempotency_sync("a@b.com", "never-seen") is None


def test_lookup_after_enqueue_returns_job_id(queue: JobQueue) -> None:
    async def _go() -> str:
        return await queue.enqueue(
            user_email="a@b.com",
            sheet_id="s1",
            worksheet="w",
            tab_type=TAB_IMAGE_VO,
            rows=[_row()],
            idempotency_key="key-1",
        )

    job_id = asyncio.run(_go())
    assert queue._lookup_idempotency_sync("a@b.com", "key-1") == job_id


# ── Replay (the core promise) ───────────────────────────────────────────────


def test_enqueue_same_key_twice_returns_same_job_id(queue: JobQueue) -> None:
    async def _go() -> tuple[str, str]:
        first = await queue.enqueue(
            user_email="a@b.com",
            sheet_id="s1",
            worksheet="w",
            tab_type=TAB_IMAGE_VO,
            rows=[_row()],
            idempotency_key="replay-key",
        )
        second = await queue.enqueue(
            user_email="a@b.com",
            sheet_id="s1",
            worksheet="w",
            tab_type=TAB_IMAGE_VO,
            rows=[_row()],
            idempotency_key="replay-key",
        )
        return first, second

    first, second = asyncio.run(_go())
    assert first == second


def test_enqueue_same_key_different_user_is_separate(queue: JobQueue) -> None:
    """The idempotency table is keyed by (user_email, key) — user B replaying
    user A's key must not surface user A's job."""

    async def _go() -> tuple[str, str]:
        a = await queue.enqueue(
            user_email="a@b.com",
            sheet_id="s1",
            worksheet="w",
            tab_type=TAB_IMAGE_VO,
            rows=[_row()],
            idempotency_key="shared",
        )
        b = await queue.enqueue(
            user_email="other@b.com",
            sheet_id="s2",
            worksheet="w",
            tab_type=TAB_IMAGE_VO,
            rows=[_row()],
            idempotency_key="shared",
        )
        return a, b

    a, b = asyncio.run(_go())
    assert a != b


# ── TTL prune ───────────────────────────────────────────────────────────────


def test_prune_removes_only_old_rows(queue: JobQueue) -> None:
    """Insert one fresh row + one row stamped with an old ``created_ts``;
    prune must drop the old one and keep the fresh one."""
    now = time.time()
    queue._conn.execute(
        "INSERT INTO idempotency_keys "
        "(key, user_email, job_id, created_at, created_ts) VALUES (?,?,?,?,?)",
        ("fresh", "a@b.com", "job-fresh", "now", now),
    )
    queue._conn.execute(
        "INSERT INTO idempotency_keys "
        "(key, user_email, job_id, created_at, created_ts) VALUES (?,?,?,?,?)",
        ("ancient", "a@b.com", "job-old", "old", now - IDEMPOTENCY_TTL_SECONDS - 10),
    )
    removed = queue._prune_idempotency_sync(ttl_seconds=IDEMPOTENCY_TTL_SECONDS)
    assert removed == 1
    assert queue._lookup_idempotency_sync("a@b.com", "fresh") == "job-fresh"
    assert queue._lookup_idempotency_sync("a@b.com", "ancient") is None
