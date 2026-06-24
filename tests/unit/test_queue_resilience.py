"""Web-path DB resilience + idempotent-by-construction enqueue.

These cover ``JobQueue._run_db`` (timeout + discard-and-reconnect retry) and the
deterministic-job_id / INSERT OR IGNORE enqueue path that makes a retried or
partially-written submit exactly-once — the fix for the recurring
"Backend is busy / HTTP 500" submit popup.

Plan: ``_plans/2026-06-17-submit-500s-turso-resilience.md``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bulkvid.models.row import ImageVORow
from bulkvid.orchestrator.queue import (
    JOB_QUEUED,
    ROW_PENDING,
    TAB_IMAGE_VO,
    JobQueue,
    QueueBusy,
    QueueUnavailable,
    _deterministic_job_id,
    _now_iso,
    _row_to_payload,
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


async def _enqueue(queue: JobQueue, rows: list[ImageVORow], key: str | None) -> str:
    return await queue.enqueue(
        user_email="a@b.com",
        sheet_id="s",
        worksheet="w",
        tab_type=TAB_IMAGE_VO,
        rows=rows,
        idempotency_key=key,
    )


# ── _run_db: retry + reconnect ───────────────────────────────────────────────


def test_enqueue_retries_then_succeeds(
    queue: JobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient flap on the first attempt triggers a reconnect and the
    retry lands the job — no error surfaces to the caller."""
    monkeypatch.setattr(
        "bulkvid.orchestrator.queue._DB_RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0)
    )
    real_enqueue = queue._enqueue_sync
    calls = {"n": 0}

    def flaky(**kwargs: object) -> tuple[str, bool]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("turso flap")
        return real_enqueue(**kwargs)  # type: ignore[arg-type]

    reconnects = {"n": 0}
    real_reconnect = queue._reconnect_sync

    def spy_reconnect(*, reason: str) -> None:
        reconnects["n"] += 1
        real_reconnect(reason=reason)

    monkeypatch.setattr(queue, "_enqueue_sync", flaky)
    monkeypatch.setattr(queue, "_reconnect_sync", spy_reconnect)

    job_id = asyncio.run(_enqueue(queue, [_row(2)], "k1"))

    assert job_id
    assert calls["n"] == 2          # failed once, succeeded on retry
    assert reconnects["n"] == 1     # discarded the (notionally wedged) conn
    job = asyncio.run(queue.get_job(job_id))
    assert job is not None
    assert job.row_count == 1


def test_enqueue_exhausts_raises_queue_unavailable(
    queue: JobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persistent flap raises QueueUnavailable (a QueueBusy) — the route maps
    that to 503, not a bare 500."""
    monkeypatch.setattr(
        "bulkvid.orchestrator.queue._DB_RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0)
    )

    def always_fail(**kwargs: object) -> tuple[str, bool]:
        raise RuntimeError("turso down")

    monkeypatch.setattr(queue, "_enqueue_sync", always_fail)
    monkeypatch.setattr(queue, "_reconnect_sync", lambda *, reason: None)

    with pytest.raises(QueueUnavailable):
        asyncio.run(_enqueue(queue, [_row(2)], "k"))

    assert issubclass(QueueUnavailable, QueueBusy)


def test_enqueue_timeout_then_succeeds(
    queue: JobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call that blows the hard timeout is abandoned, the conn is recycled,
    and the retry succeeds. Verifies TimeoutError flows through the same
    reconnect+retry path as any other exception."""
    import time as _time

    monkeypatch.setattr(
        "bulkvid.orchestrator.queue._DB_CALL_TIMEOUT_SECONDS", 0.2
    )
    monkeypatch.setattr(
        "bulkvid.orchestrator.queue._DB_RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0)
    )
    real_enqueue = queue._enqueue_sync
    calls = {"n": 0}

    def slow_once(**kwargs: object) -> tuple[str, bool]:
        calls["n"] += 1
        if calls["n"] == 1:
            _time.sleep(0.5)               # exceeds the 0.2s timeout
            raise RuntimeError("late")     # orphaned thread; result discarded
        return real_enqueue(**kwargs)      # type: ignore[arg-type]

    monkeypatch.setattr(queue, "_enqueue_sync", slow_once)

    job_id = asyncio.run(_enqueue(queue, [_row(2)], "k-timeout"))

    assert job_id
    assert calls["n"] >= 2
    job = asyncio.run(queue.get_job(job_id))
    assert job is not None and job.row_count == 1


# ── Idempotent-by-construction enqueue ───────────────────────────────────────


def test_enqueue_deterministic_job_id(queue: JobQueue) -> None:
    """Same (user, key) → same job_id, independent of when it runs."""
    a = _deterministic_job_id("a@b.com", "key-1")
    b = _deterministic_job_id("a@b.com", "key-1")
    c = _deterministic_job_id("a@b.com", "key-2")
    d = _deterministic_job_id("z@b.com", "key-1")
    assert a == b
    assert a != c          # different key
    assert a != d          # different user (cross-user collision impossible)
    assert a.startswith("job-")


def test_enqueue_idempotent_replay_no_dupes(queue: JobQueue) -> None:
    """Submitting the same batch+key twice yields one job and one row per
    row_num — no duplicate row_queue entries (so no duplicate videos)."""

    async def _go() -> tuple[str, str]:
        j1 = await _enqueue(queue, [_row(2), _row(3)], "dup")
        j2 = await _enqueue(queue, [_row(2), _row(3)], "dup")
        return j1, j2

    j1, j2 = asyncio.run(_go())
    assert j1 == j2

    rows = asyncio.run(queue.list_rows(j1))
    assert sorted(r["row_num"] for r in rows) == [2, 3]

    cur = queue._conn.execute(
        "SELECT COUNT(*) AS c FROM row_queue WHERE job_id = ?", (j1,)
    )
    assert cur.fetchone()["c"] == 2


def test_enqueue_partial_then_retry_fills_missing(queue: JobQueue) -> None:
    """A first attempt that wrote the jobs row + only SOME rows before a flap
    is completed by the retry (same deterministic id), filling the missing
    rows without duplicating the ones already there."""
    email, key = "a@b.com", "partial"
    jid = _deterministic_job_id(email, key)
    now = _now_iso()

    # Simulate attempt-1 partial write: jobs row + row 2 only (row 3 lost).
    # The idempotency_keys row was NOT written (the flap hit before it), so the
    # retry's fast-path lookup misses and it re-runs the full enqueue.
    queue._conn.execute(
        "INSERT INTO jobs (job_id, user_email, sheet_id, worksheet, tab_type, "
        "status, row_count, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (jid, email, "s", "w", TAB_IMAGE_VO, JOB_QUEUED, 2, now),
    )
    queue._conn.execute(
        "INSERT INTO row_queue (job_id, row_num, payload, status) VALUES (?,?,?,?)",
        (jid, 2, _row_to_payload(_row(2), TAB_IMAGE_VO), ROW_PENDING),
    )

    jid2 = asyncio.run(_enqueue(queue, [_row(2), _row(3)], key))

    assert jid2 == jid
    rows = asyncio.run(queue.list_rows(jid))
    assert sorted(r["row_num"] for r in rows) == [2, 3]

    cur = queue._conn.execute(
        "SELECT row_num, COUNT(*) AS c FROM row_queue WHERE job_id = ? "
        "GROUP BY row_num",
        (jid,),
    )
    counts = {r["row_num"]: r["c"] for r in cur.fetchall()}
    assert counts == {2: 1, 3: 1}    # row 2 not duplicated


def test_enqueue_large_batch_single_insert(queue: JobQueue) -> None:
    """A batch enqueues all rows via the chunked multi-row insert (correctness
    of the round-trip-reducing path)."""
    rows = [_row(n) for n in range(2, 52)]    # 50 rows
    job_id = asyncio.run(_enqueue(queue, rows, "batch"))
    out = asyncio.run(queue.list_rows(job_id))
    assert sorted(r["row_num"] for r in out) == list(range(2, 52))


# ── Worker-path Hrana stream resilience ──────────────────────────────────────


def test_record_result_reconnects_after_stream_eviction(
    queue: JobQueue, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for job-61d63442da7e6b25 (2026-06-24, screenshot 2).

    The worker's libsql connection's Hrana stream got evicted server-side.
    ``record_result`` was using a bare ``asyncio.to_thread`` against that
    dead connection and bricked — every row stayed PROCESSING in the DB
    forever, and the operator saw rows stuck "Starting.." for 25+ minutes.
    The runner's ``_pending_records`` drainer kept retrying the same dead
    connection (5-min per-entry budget then giveup), and ``kill_job`` was
    also dead because IT went through the same wedged connection.

    Fix: route ``record_result`` (and the other worker-side queue ops)
    through ``_run_db`` so a stale Hrana stream id heals on retry — same
    discipline the web ``submit`` path got on 2026-06-17. This test
    simulates the pathology: ``_record_result_sync`` raises a stream-shaped
    error once, then succeeds; the row ends DONE and ``_reconnect_sync``
    fires between attempts. Plan
    ``_plans/2026-06-24-libsql-hrana-stream-resilience.md``.
    """
    from bulkvid.models.row import STATUS_SUCCESS, RowResult

    monkeypatch.setattr(
        "bulkvid.orchestrator.queue._DB_RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0)
    )

    # Land one row, claim it so there's something to record against.
    asyncio.run(_enqueue(queue, [_row(2)], "stream-eviction"))
    claimed = asyncio.run(queue.claim_next_row())
    assert claimed is not None

    real_record = queue._record_result_sync
    calls = {"n": 0}

    def stream_dead_then_recover(queue_id: int, result: RowResult) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError(
                'Hrana: `api error: `status=404 Not Found, body='
                '{"error":"stream not found: 14edb5a7:1454bfd"}``'
            )
        real_record(queue_id, result)

    reconnects = {"n": 0}
    real_reconnect = queue._reconnect_sync

    def spy_reconnect(*, reason: str) -> None:
        reconnects["n"] += 1
        real_reconnect(reason=reason)

    monkeypatch.setattr(queue, "_record_result_sync", stream_dead_then_recover)
    monkeypatch.setattr(queue, "_reconnect_sync", spy_reconnect)

    asyncio.run(
        queue.record_result(
            claimed.id,
            RowResult(
                row_num=claimed.row_num,
                status=STATUS_SUCCESS,
                video_urls=["https://example.com/v1.mp4"],
                cost_usd=0.01,
            ),
        )
    )

    assert calls["n"] == 2          # failed once, recovered on retry
    assert reconnects["n"] == 1     # connection swapped between attempts

    # And the row actually landed as DONE — not stuck PROCESSING.
    rows = asyncio.run(queue.list_rows(claimed.job_id))
    assert len(rows) == 1
    assert rows[0]["status"] == "done"
