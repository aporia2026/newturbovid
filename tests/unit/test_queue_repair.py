"""Tests for derived job state and the self-repair pass.

Plan ``_plans/2026-08-17-stuck-jobs-selfheal-and-restart.md``.

The bug these lock down: production has NO transactions (``db.py`` no-ops
BEGIN/COMMIT/ROLLBACK on both remote transports, and the Hrana transport makes
every statement an independent HTTP request), so a multi-statement helper can
partially apply and ``_run_db`` then re-runs it whole. ``_record_result_sync``
used to increment ``completed_rows`` and finalize on counter arithmetic, and it
returns early when the row is already terminal — so a lost counter statement was
lost FOREVER and the job stayed ``running`` with every row finished.

Every test here simulates that partial application directly on the connection,
because that is the only honest way to reproduce it: the failure is not in what
one statement does, it is in which SUBSET of them survives.

Covers:
  - a retry after a partial write converges instead of drifting (the reported bug)
  - the same strand healed by the repair pass when no retry is coming
  - finalize refuses a job whose rows have not been inserted yet
  - finalize waits out the grace period for a partially-inserted submit
  - queued jobs whose rows have started get promoted
  - rows orphaned under a terminal parent get cleared
  - ``row_count`` is reported, never rewritten
  - the set-based kill writes byte-identical JSON to the old per-row path
  - a kill retry finishes the work its lost first attempt started
  - every action is a no-op on a clean DB and idempotent when repeated
  - repair scoping cannot cross between users
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bulkvid.models.row import STATUS_SUCCESS, ImageVORow, RowResult
from bulkvid.orchestrator.queue import (
    JOB_COMPLETED,
    JOB_KILLED,
    JOB_QUEUED,
    JOB_RUNNING,
    REPAIR_SOURCE_AUTO,
    REPAIR_SOURCE_MANUAL,
    ROW_DONE,
    ROW_PENDING,
    ROW_PROCESSING,
    TAB_IMAGE_VO,
    JobQueue,
    abort_orphan_rows,
    count_row_count_drift,
    finalize_settled_jobs,
    killed_result_json,
    list_repair_runs,
    orphan_result_json,
    promote_started_jobs,
    resync_job_counters,
)
from bulkvid.orchestrator.repair import (
    ACTION_COUNTERS,
    ACTION_DRIFT,
    ACTION_FINALIZE,
    ACTION_ORPHANS,
    ACTION_PROMOTE,
    run_repairs,
)


def _img_row(n: int) -> ImageVORow:
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


@pytest.fixture
def queue(tmp_path: Path) -> JobQueue:
    q = JobQueue(tmp_path / "jobs.db")
    yield q
    q.close()


def _iso_ago(seconds: float) -> str:
    return (
        datetime.now(UTC) - timedelta(seconds=seconds)
    ).isoformat(timespec="seconds")


def _land_row_only(queue: JobQueue, queue_id: int, row_num: int) -> None:
    """Apply ONLY the row half of a ``record_result``, exactly as a partial
    write does: the row reaches DONE while the job's counters and status are
    left untouched."""
    queue._conn.execute(
        "UPDATE row_queue SET status = ?, finished_at = ?, result = ? WHERE id = ?",
        (
            ROW_DONE,
            _iso_ago(0),
            json.dumps({"row_num": row_num, "status": STATUS_SUCCESS,
                        "video_urls": ["https://example.com/v.mp4"],
                        "cost_usd": 0.0, "elapsed_seconds": 1.0,
                        "error": None, "metadata": {}}),
            queue_id,
        ),
    )


# ── The reported bug ────────────────────────────────────────────────────────


async def test_retry_after_partial_write_finalizes_the_job(
    queue: JobQueue,
) -> None:
    """THE regression test. Row UPDATE landed, the job-level statements did not,
    and ``_run_db`` re-runs the helper. The old code hit the terminal-row guard
    and returned, leaving the counter permanently short and the job pinned at
    ``running`` forever — every row done, card stuck in the sidebar, Kill the
    only way out."""
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    queued = await queue.claim_next_row()
    assert queued is not None

    # Partial application: the row is done, the job knows nothing about it.
    _land_row_only(queue, queued.id, 2)
    mid = await queue.get_job(job_id)
    assert mid is not None
    assert mid.status == JOB_RUNNING
    assert mid.completed_rows == 0        # the drift

    # The retry. Under the old code this returned early and changed nothing.
    await queue.record_result(
        queued.id,
        RowResult(
            row_num=2, status=STATUS_SUCCESS,
            video_urls=["https://example.com/v.mp4"], cost_usd=0.1,
        ),
    )

    job = await queue.get_job(job_id)
    assert job is not None
    assert job.status == JOB_COMPLETED
    assert job.completed_rows == 1
    assert job.failed_rows == 0
    assert job.finished_at


async def test_repair_finalizes_strand_with_no_retry_coming(
    queue: JobQueue,
) -> None:
    """The other permanent strand: the LAST row's finalize statement is lost, so
    no further ``record_result`` will ever fire for that job and nothing
    re-attempts it. Only an out-of-band pass can clear this one."""
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2), _img_row(3)],
    )
    for row_num in (2, 3):
        queued = await queue.claim_next_row()
        assert queued is not None
        _land_row_only(queue, queued.id, row_num)
    # Counters correct, status wrong — i.e. statement 3 landed and 4 did not.
    resync_job_counters(queue._conn, job_id=job_id)
    assert (await queue.get_job(job_id)).status == JOB_RUNNING

    report = run_repairs(
        queue._conn, source=REPAIR_SOURCE_AUTO, actor="test", user_email=None,
    )

    job = await queue.get_job(job_id)
    assert job is not None
    assert job.status == JOB_COMPLETED
    assert job.completed_rows == 2
    assert {a.name: a.changed for a in report.actions}[ACTION_FINALIZE] == 1


# ── Finalize guards ─────────────────────────────────────────────────────────


async def test_finalize_refuses_job_with_no_rows_inserted_yet(
    queue: JobQueue,
) -> None:
    """The dangerous edge: a job created milliseconds ago whose ``row_queue``
    rows have not been inserted yet has "no non-terminal rows", and a naive
    check would mark it complete with zero output. Nothing may finalize a job
    that has no rows at all — ever, at any age."""
    now = _iso_ago(0)
    queue._conn.execute(
        "INSERT INTO jobs (job_id, user_email, sheet_id, worksheet, tab_type, "
        "status, row_count, created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("job-empty", "u@aporia.com", "s", "w", TAB_IMAGE_VO, JOB_QUEUED, 5, now),
    )

    assert finalize_settled_jobs(queue._conn, job_id="job-empty") == 0
    # Still zero even long past the grace period — age is not the escape hatch
    # for "no rows", only for "fewer rows than row_count".
    queue._conn.execute(
        "UPDATE jobs SET created_at = ? WHERE job_id = ?",
        (_iso_ago(86_400), "job-empty"),
    )
    assert finalize_settled_jobs(queue._conn, job_id="job-empty") == 0
    assert (await queue.get_job("job-empty")).status == JOB_QUEUED


async def test_finalize_waits_out_grace_for_partially_inserted_submit(
    queue: JobQueue,
) -> None:
    """A submit whose row inserts partially failed leaves ``row_count`` above the
    rows present. Those rows can all finish while the rest are (as far as we can
    tell) still arriving, so finalize holds off — then goes ahead once the gap is
    provably not a race, because refusing forever would recreate the very wedge
    this work removes."""
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2), _img_row(3)],
    )
    # Pretend the submit meant to insert 5 rows and only landed 2.
    queue._conn.execute(
        "UPDATE jobs SET row_count = 5 WHERE job_id = ?", (job_id,)
    )
    for row_num in (2, 3):
        queued = await queue.claim_next_row()
        assert queued is not None
        _land_row_only(queue, queued.id, row_num)

    assert finalize_settled_jobs(queue._conn, job_id=job_id) == 0
    assert (await queue.get_job(job_id)).status == JOB_RUNNING

    queue._conn.execute(
        "UPDATE jobs SET created_at = ? WHERE job_id = ?",
        (_iso_ago(600), job_id),
    )
    assert finalize_settled_jobs(queue._conn, job_id=job_id) == 1

    job = await queue.get_job(job_id)
    assert job.status == JOB_COMPLETED
    # row_count is NOT rewritten: the 3 rows that never got inserted are gone,
    # and an honest "2 / 5" is the only truthful thing to show.
    assert job.row_count == 5
    # Finalize decides STATUS and nothing else — the counters are
    # ``resync_job_counters``' job, which is why the pass runs both (in that
    # order) and why this helper is safe to call from either.
    assert resync_job_counters(queue._conn, job_id=job_id) == 1
    assert (await queue.get_job(job_id)).completed_rows == 2


async def test_row_count_drift_is_reported_never_repaired(
    queue: JobQueue,
) -> None:
    """``row_count`` is the one column that cannot be reconstructed, so the pass
    surfaces the gap instead of hiding it — those sheet rows still need
    resubmitting and the number is the only evidence."""
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    queued = await queue.claim_next_row()
    assert queued is not None
    _land_row_only(queue, queued.id, 2)
    queue._conn.execute(
        "UPDATE jobs SET row_count = 4, created_at = ? WHERE job_id = ?",
        (_iso_ago(600), job_id),
    )

    report = run_repairs(
        queue._conn, source=REPAIR_SOURCE_MANUAL, actor="u@aporia.com",
        user_email="u@aporia.com",
    )

    drift = next(a for a in report.actions if a.name == ACTION_DRIFT)
    assert drift.changed == 0                       # read-only by design
    assert len(drift.notes) == 1
    assert job_id in drift.notes[0]                 # manual pass names the job
    assert "resubmitting" in drift.notes[0]
    assert (await queue.get_job(job_id)).row_count == 4
    # The raw helper agrees.
    assert count_row_count_drift(queue._conn) == [(job_id, 4, 1)]


# ── Promote / orphans ───────────────────────────────────────────────────────


async def test_promote_started_job_stuck_at_queued(queue: JobQueue) -> None:
    """``_claim_next_row_sync`` marks the row PROCESSING and THEN promotes the
    job — two independent statements. With the second lost, the job sits at
    ``queued`` while its rows run, and because ``poll_jobs`` only fetches row
    detail for RUNNING jobs the card reads "waiting in queue" with no progress
    until the whole batch ends."""
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    queued = await queue.claim_next_row()
    assert queued is not None
    queue._conn.execute(
        "UPDATE jobs SET status = ?, started_at = NULL WHERE job_id = ?",
        (JOB_QUEUED, job_id),
    )

    assert promote_started_jobs(queue._conn, job_id=job_id) == 1

    job = await queue.get_job(job_id)
    assert job.status == JOB_RUNNING
    assert job.started_at                    # backfilled so elapsed renders
    # Idempotent: nothing left to promote.
    assert promote_started_jobs(queue._conn, job_id=job_id) == 0


async def test_abort_orphan_rows_under_terminal_parent(queue: JobQueue) -> None:
    """Rows left PENDING/PROCESSING under a job that already ended are
    unreachable: nothing claims them (claims require an active parent) and the
    lease sweep skips them too, so without this they sit there forever holding
    the job's counters wrong."""
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2), _img_row(3)],
    )
    await queue.claim_next_row()
    # Parent goes terminal WITHOUT its rows being resolved — what a kill whose
    # row-abort statement was lost leaves behind.
    queue._conn.execute(
        "UPDATE jobs SET status = ? WHERE job_id = ?", (JOB_KILLED, job_id)
    )

    assert abort_orphan_rows(queue._conn, user_email="u@aporia.com") == 2
    assert abort_orphan_rows(queue._conn, user_email="u@aporia.com") == 0

    rows = await queue.list_rows(job_id)
    assert {r["status"] for r in rows} == {"failed"}
    for r in rows:
        assert r["error"] == "cancelled because the job had already finished"


# ── Set-based kill ─────────────────────────────────────────────────────────


async def test_set_based_kill_json_matches_python_builder(
    queue: JobQueue,
) -> None:
    """The kill builds each row's result JSON in SQL so one statement can abort
    the whole batch — under the HTTP transport the old per-row loop cost one
    round-trip per row and blew the kill route's 10s budget on exactly the big
    jobs the operator most wanted stopped. This asserts the SQL twin and the
    Python builder cannot drift apart."""
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2), _img_row(3), _img_row(4)],
    )
    await queue.claim_next_row()

    killed, rows_aborted = await queue.kill_job(job_id)
    assert killed is True
    assert rows_aborted == 3

    cur = queue._conn.execute(
        "SELECT row_num, result FROM row_queue WHERE job_id = ? ORDER BY row_num",
        (job_id,),
    )
    for row in cur.fetchall():
        assert json.loads(row["result"]) == json.loads(
            killed_result_json(int(row["row_num"]))
        )
    job = await queue.get_job(job_id)
    assert job.status == JOB_KILLED
    assert job.failed_rows == 3


async def test_orphan_result_sql_matches_python_builder(queue: JobQueue) -> None:
    """Same twin-check for the orphan payload."""
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(7)],
    )
    queue._conn.execute(
        "UPDATE jobs SET status = ? WHERE job_id = ?", (JOB_COMPLETED, job_id)
    )
    assert abort_orphan_rows(queue._conn) == 1

    cur = queue._conn.execute(
        "SELECT row_num, result FROM row_queue WHERE job_id = ?", (job_id,)
    )
    row = cur.fetchone()
    assert json.loads(row["result"]) == json.loads(
        orphan_result_json(int(row["row_num"]))
    )


async def test_kill_retry_finishes_what_the_lost_attempt_started(
    queue: JobQueue,
) -> None:
    """A kill is several independent statements. When the job's status change
    lands and the row abort does not, the retry used to bail at "nothing active
    to kill" and report failure, leaving the rows stranded in-flight under a dead
    parent where nothing would ever touch them again."""
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2), _img_row(3)],
    )
    await queue.claim_next_row()
    # Simulate the first attempt: only the job's own status change survived.
    queue._conn.execute(
        "UPDATE jobs SET status = ?, finished_at = ? WHERE job_id = ?",
        (JOB_KILLED, _iso_ago(0), job_id),
    )

    killed, rows_aborted = await queue.kill_job(job_id)

    # ``killed`` stays False because THIS call did not perform the transition —
    # the kill audit's ``no_active_job`` outcome is defined that way and is not
    # ours to redefine. What matters is that the strand got cleaned up anyway,
    # which the old code refused to do.
    assert killed is False
    assert rows_aborted == 2
    rows = await queue.list_rows(job_id)
    assert {r["status"] for r in rows} == {"failed"}
    assert (await queue.get_job(job_id)).failed_rows == 2


async def test_kill_still_reports_nothing_to_kill_for_finished_job(
    queue: JobQueue,
) -> None:
    """The resumable-kill change must not turn "already finished on its own" into
    a false "killed" — the toast and the kill audit both depend on that
    distinction."""
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    queue._conn.execute(
        "UPDATE jobs SET status = ? WHERE job_id = ?", (JOB_COMPLETED, job_id)
    )

    killed, rows_aborted = await queue.kill_job(job_id)
    assert killed is False
    assert rows_aborted == 0


# ── Pass-level properties ───────────────────────────────────────────────────


async def test_repair_is_noop_on_a_healthy_queue(queue: JobQueue) -> None:
    """The normal case. A pass that changes nothing must write nothing — at a
    5-minute cadence, recording quiet passes would bury the handful that carry
    the actual story."""
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    queued = await queue.claim_next_row()
    assert queued is not None
    await queue.record_result(
        queued.id,
        RowResult(row_num=2, status=STATUS_SUCCESS,
                  video_urls=["https://example.com/v.mp4"], cost_usd=0.1),
    )
    assert (await queue.get_job(job_id)).status == JOB_COMPLETED

    report = run_repairs(
        queue._conn, source=REPAIR_SOURCE_AUTO, actor="test", user_email=None,
    )

    assert report.changed == 0
    assert "Nothing needed fixing" in report.summary()
    assert list_repair_runs(queue._conn) == []


async def test_repair_is_idempotent(queue: JobQueue) -> None:
    """Run it twice back to back: the second pass must find nothing left. Any
    action that re-fired here would be one that compounds instead of converging,
    which on a 5-minute unattended timer is how you corrupt a queue."""
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    queued = await queue.claim_next_row()
    assert queued is not None
    _land_row_only(queue, queued.id, 2)

    first = run_repairs(
        queue._conn, source=REPAIR_SOURCE_AUTO, actor="test", user_email=None,
    )
    second = run_repairs(
        queue._conn, source=REPAIR_SOURCE_AUTO, actor="test", user_email=None,
    )

    assert first.changed > 0
    assert second.changed == 0
    assert (await queue.get_job(job_id)).status == JOB_COMPLETED


async def test_repair_records_only_changed_passes_and_is_readable(
    queue: JobQueue,
) -> None:
    """The audit table is the whole reason unattended repairs are visible days
    later — HF Spaces keeps no container logs from before a restart."""
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    queued = await queue.claim_next_row()
    assert queued is not None
    _land_row_only(queue, queued.id, 2)

    run_repairs(
        queue._conn, source=REPAIR_SOURCE_AUTO, actor="worker-watchdog",
        user_email=None,
    )

    runs = list_repair_runs(queue._conn)
    assert len(runs) == 1
    assert runs[0].source == REPAIR_SOURCE_AUTO
    assert runs[0].actor == "worker-watchdog"
    assert runs[0].changed > 0
    assert ACTION_FINALIZE in runs[0].log
    # A bulk user sees automatic passes (they are counts-only, so there is
    # nothing of another user's to leak).
    assert len(list_repair_runs(queue._conn, user_email="someone@aporia.com")) == 1
    assert job_id                                   # job still addressable


async def test_repair_scope_cannot_cross_users(queue: JobQueue) -> None:
    """Blast radius. A bulk user's button is bounded to their own jobs; another
    user's identical strand must be left exactly as it was."""
    mine = await queue.enqueue(
        user_email="me@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    theirs = await queue.enqueue(
        user_email="them@aporia.com", sheet_id="s2", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    for _ in range(2):
        queued = await queue.claim_next_row()
        assert queued is not None
        _land_row_only(queue, queued.id, 2)

    run_repairs(
        queue._conn, source=REPAIR_SOURCE_MANUAL, actor="me@aporia.com",
        user_email="me@aporia.com",
    )

    assert (await queue.get_job(mine)).status == JOB_COMPLETED
    assert (await queue.get_job(theirs)).status == JOB_RUNNING


async def test_repair_survives_one_failing_action(
    queue: JobQueue, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One broken action must not cost us the others. On the unattended path the
    alternative is a watchdog thread that dies quietly and takes the whole
    self-healing story with it."""
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    queued = await queue.claim_next_row()
    assert queued is not None
    _land_row_only(queue, queued.id, 2)

    def _boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("simulated DB flap")

    monkeypatch.setattr("bulkvid.orchestrator.repair.abort_orphan_rows", _boom)

    report = run_repairs(
        queue._conn, source=REPAIR_SOURCE_AUTO, actor="test", user_email=None,
    )

    assert any("could not run" in line for line in report.lines)
    assert ACTION_ORPHANS not in {a.name for a in report.actions}
    # The finalize still ran, which is the point.
    assert (await queue.get_job(job_id)).status == JOB_COMPLETED


async def test_total_failure_is_never_reported_as_healthy(
    queue: JobQueue, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flapping DB makes every action raise. Reporting "everything looks
    healthy" there would be the most misleading thing we could say — the operator
    would go hunting somewhere else for a problem that is right in front of
    them."""
    def _boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("simulated DB flap")

    for target in (
        "sweep_expired_processing_rows",
        "abort_orphan_rows",
        "resync_job_counters",
        "finalize_settled_jobs",
        "promote_started_jobs",
        "count_row_count_drift",
    ):
        monkeypatch.setattr(f"bulkvid.orchestrator.repair.{target}", _boom)

    report = run_repairs(
        queue._conn, source=REPAIR_SOURCE_AUTO, actor="test", user_email=None,
    )

    assert len(report.failed) == 6
    assert report.changed == 0
    assert "Could not check anything" in report.summary()
    assert "healthy" not in report.summary()


async def test_partial_failure_is_flagged_in_the_summary(
    queue: JobQueue, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some checks ran, one did not: the operator must be told the result is
    incomplete rather than shown a confident all-clear."""
    def _boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("simulated DB flap")

    monkeypatch.setattr("bulkvid.orchestrator.repair.abort_orphan_rows", _boom)

    report = run_repairs(
        queue._conn, source=REPAIR_SOURCE_AUTO, actor="test", user_email=None,
    )

    assert report.failed == [ACTION_ORPHANS]
    assert "could not run" in report.summary()


async def test_counter_resync_only_touches_wrong_jobs(queue: JobQueue) -> None:
    """``rowcount`` has to mean "repaired", not "matched" — the repair report
    shows that number to the operator, and a healthy pass must write nothing."""
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2), _img_row(3)],
    )
    queued = await queue.claim_next_row()
    assert queued is not None
    await queue.record_result(
        queued.id,
        RowResult(row_num=2, status=STATUS_SUCCESS, video_urls=[], cost_usd=0.0),
    )

    assert resync_job_counters(queue._conn, job_id=job_id) == 0

    queue._conn.execute(
        "UPDATE jobs SET completed_rows = 99 WHERE job_id = ?", (job_id,)
    )
    assert resync_job_counters(queue._conn, job_id=job_id) == 1
    assert (await queue.get_job(job_id)).completed_rows == 1


async def test_summary_is_plain_language(queue: JobQueue) -> None:
    """The operator asked for a button, not a report on a state machine. The
    default line must be readable without knowing what "finalize" means."""
    await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    queued = await queue.claim_next_row()
    assert queued is not None
    _land_row_only(queue, queued.id, 2)

    report = run_repairs(
        queue._conn, source=REPAIR_SOURCE_MANUAL, actor="u@aporia.com",
        user_email="u@aporia.com",
    )
    summary = report.summary()

    assert "now marked done" in summary
    assert "No videos were lost." in summary
    for jargon in ("finalize", "PROCESSING", "row_queue", "reconcile", "None"):
        assert jargon not in summary


async def test_stranded_row_release_respects_the_lease(queue: JobQueue) -> None:
    """The one action that moves work backward, and the only one that can cost
    money (a released row is rendered again). A row inside its lease must be left
    alone no matter how the pass is invoked."""
    await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )
    queued = await queue.claim_next_row()
    assert queued is not None

    report = run_repairs(
        queue._conn, source=REPAIR_SOURCE_AUTO, actor="test", user_email=None,
    )
    assert report.changed == 0
    cur = queue._conn.execute(
        "SELECT status FROM row_queue WHERE id = ?", (queued.id,)
    )
    assert cur.fetchone()["status"] == ROW_PROCESSING

    # Backdate the claim past the lease: now it is provably stranded.
    queue._conn.execute(
        "UPDATE row_queue SET started_at = ? WHERE id = ?",
        (_iso_ago(86_400), queued.id),
    )
    report = run_repairs(
        queue._conn, source=REPAIR_SOURCE_AUTO, actor="test", user_email=None,
    )
    assert report.changed >= 1
    cur = queue._conn.execute(
        "SELECT status FROM row_queue WHERE id = ?", (queued.id,)
    )
    assert cur.fetchone()["status"] == ROW_PENDING


async def test_release_before_finalize_keeps_unfinished_job_open(
    queue: JobQueue,
) -> None:
    """Ordering, asserted. A released row makes its job un-settled again; if
    finalize ran first the job would be closed while a row it just re-queued was
    about to run, and the sidebar would report a job complete that is not."""
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2), _img_row(3)],
    )
    first = await queue.claim_next_row()
    second = await queue.claim_next_row()
    assert first is not None and second is not None
    _land_row_only(queue, first.id, 2)
    # The other row is stranded past its lease, so the pass will re-queue it.
    queue._conn.execute(
        "UPDATE row_queue SET started_at = ? WHERE id = ?",
        (_iso_ago(86_400), second.id),
    )

    run_repairs(
        queue._conn, source=REPAIR_SOURCE_AUTO, actor="test", user_email=None,
    )

    job = await queue.get_job(job_id)
    assert job.status == JOB_RUNNING       # NOT completed: row 3 runs again
    assert job.completed_rows == 1
    cur = queue._conn.execute(
        "SELECT status FROM row_queue WHERE id = ?", (second.id,)
    )
    assert cur.fetchone()["status"] == ROW_PENDING


async def test_promote_reported_through_the_pass(queue: JobQueue) -> None:
    """The promote action is wired into the pass, not just callable directly."""
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2), _img_row(3)],
    )
    claimed = await queue.claim_next_row()
    assert claimed is not None
    queue._conn.execute(
        "UPDATE jobs SET status = ? WHERE job_id = ?", (JOB_QUEUED, job_id)
    )

    report = run_repairs(
        queue._conn, source=REPAIR_SOURCE_AUTO, actor="test", user_email=None,
    )

    by_name = {a.name: a.changed for a in report.actions}
    assert by_name[ACTION_PROMOTE] == 1
    assert by_name[ACTION_COUNTERS] == 0    # counters were already right
    assert (await queue.get_job(job_id)).status == JOB_RUNNING
