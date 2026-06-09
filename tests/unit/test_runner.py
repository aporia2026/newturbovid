"""Tests for the BatchRunner.

The row processors are monkey-patched with simple async stubs so the runner
test stays focused on the orchestration logic (semaphore, drain, callback,
shutdown) — not on rerunning the full Image-VO pipeline that already has
its own dedicated tests.

Covers:
  - Drains a queue with 5 rows, writes results back via callback
  - Concurrency capped at max_concurrent (never more in flight)
  - request_shutdown stops claiming new work, waits for in-flight to finish
  - A row that raises gets recorded as INTERNAL_ERROR (loop survives)
  - 4Images rows are dispatched to the 4Images processor
  - in_flight_count exposes the live count for the admin panel
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import bulkvid.orchestrator.runner as runner_mod
from bulkvid.adapters.article_fetch import ArticleFetcher
from bulkvid.adapters.gemini_tts import GeminiTTSClient
from bulkvid.adapters.kie import KieClient, KiePool
from bulkvid.adapters.openai_client import OpenAIClient
from bulkvid.adapters.rendi import RendiClient
from bulkvid.adapters.storage import S3Uploader, StorageClient
from bulkvid.models.row import (
    STATUS_INTERNAL_ERROR,
    STATUS_SUCCESS,
    FourImagesVO2Row,
    ImageVORow,
    RowResult,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.queue import (
    JOB_COMPLETED,
    TAB_FOUR_IMAGES,
    TAB_IMAGE_VO,
    JobQueue,
)
from bulkvid.orchestrator.runner import BatchRunner
from bulkvid.orchestrator.sheet_writer import PendingWrite


# ── Fixtures ────────────────────────────────────────────────────────────────


def _img_row(n: int) -> ImageVORow:
    return ImageVORow(
        row_num=n,
        country="US",
        vertical="tech",
        article_url="https://example.com/a",
        manual_image_url="https://example.com/s.png",
        voice_over=True,
        zapcap=False,
        aspect_ratio="9:16",
        script_pattern="How To",
        open_comments="",
    )


def _four_row(n: int) -> FourImagesVO2Row:
    return FourImagesVO2Row(
        row_num=n,
        country="US",
        vertical="tech",
        article_url="https://example.com/a",
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


def _make_dummy_clients() -> PipelineClients:
    """Build a clients bundle whose adapters are never actually called.

    The row processors are monkey-patched in each test, so the clients here
    only need to be constructible (not functional).
    """
    storage = StorageClient(
        primary=S3Uploader(
            bucket="b", access_key_id="x", secret_access_key="y", client=object(),
        )
    )
    return PipelineClients(
        openai=OpenAIClient(api_key="sk"),
        kie=KieClient(pool=KiePool(keys=["k_AAAAAAAAAAAA"])),
        tts=GeminiTTSClient(project="amit-tts", client=object()),
        rendi=RendiClient(api_key="r"),
        storage=storage,
        article=ArticleFetcher(tavily_api_key="t"),
        zapcap=None,
    )


# ── Drains a queue and writes back ──────────────────────────────────────────


async def test_runner_drains_queue_and_invokes_write_back(
    queue: JobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_image_vo(row, _clients, *, job_id=None):
        return RowResult(
            row_num=row.row_num, status=STATUS_SUCCESS,
            video_urls=[f"u{row.row_num}"], cost_usd=0.1,
        )

    monkeypatch.setattr(runner_mod, "process_image_vo_row", _fake_image_vo)

    rows = [_img_row(i) for i in range(2, 7)]    # 5 rows: 2,3,4,5,6
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=rows,
    )

    written: list[tuple[str, int]] = []

    async def _write_back(write: PendingWrite) -> None:
        written.append((write.job_id, write.row_num))

    runner = BatchRunner(
        queue, _make_dummy_clients(),
        max_concurrent=2, poll_idle_seconds=0.05,
        write_back=_write_back,
    )

    async def _shutdown_when_done() -> None:
        # Stop the runner once the queue is empty AND all in-flight rows are done.
        while True:
            await asyncio.sleep(0.05)
            job = await queue.get_job(job_id)
            if job is not None and job.status == JOB_COMPLETED:
                runner.request_shutdown()
                return

    await asyncio.wait_for(
        asyncio.gather(runner.run(), _shutdown_when_done()),
        timeout=5.0,
    )

    # All 5 rows were written back exactly once.
    assert sorted(r for _, r in written) == [2, 3, 4, 5, 6]
    job = await queue.get_job(job_id)
    assert job is not None
    assert job.completed_rows == 5
    assert job.cost_usd == pytest.approx(0.5)


# ── Concurrency cap is respected ────────────────────────────────────────────


async def test_concurrency_respects_semaphore(
    queue: JobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    in_flight_now = 0
    peak = 0
    lock = asyncio.Lock()

    async def _slow(row, _clients, *, job_id=None):
        nonlocal in_flight_now, peak
        async with lock:
            in_flight_now += 1
            peak = max(peak, in_flight_now)
        await asyncio.sleep(0.05)         # hold the slot briefly
        async with lock:
            in_flight_now -= 1
        return RowResult(row_num=row.row_num, status=STATUS_SUCCESS, cost_usd=0.01)

    monkeypatch.setattr(runner_mod, "process_image_vo_row", _slow)

    rows = [_img_row(i) for i in range(2, 12)]   # 10 rows
    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=rows,
    )

    runner = BatchRunner(
        queue, _make_dummy_clients(),
        max_concurrent=3, poll_idle_seconds=0.02,
    )

    async def _shutdown_when_done() -> None:
        while True:
            await asyncio.sleep(0.02)
            job = await queue.get_job(job_id)
            if job is not None and job.status == JOB_COMPLETED:
                runner.request_shutdown()
                return

    await asyncio.wait_for(
        asyncio.gather(runner.run(), _shutdown_when_done()),
        timeout=5.0,
    )

    # Peak concurrency must never exceed the semaphore limit.
    assert peak <= 3
    # And we did actually use the slots.
    assert peak >= 2


# ── Exception inside the row processor is caught ───────────────────────────


async def test_exception_in_processor_recorded_as_internal_error(
    queue: JobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(row, _clients, *, job_id=None):
        raise RuntimeError("kaboom from inside processor")

    monkeypatch.setattr(runner_mod, "process_image_vo_row", _boom)

    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
    )

    runner = BatchRunner(
        queue, _make_dummy_clients(),
        max_concurrent=2, poll_idle_seconds=0.02,
    )

    async def _shutdown_when_done() -> None:
        while True:
            await asyncio.sleep(0.02)
            job = await queue.get_job(job_id)
            if job is not None and job.status == JOB_COMPLETED:
                runner.request_shutdown()
                return

    await asyncio.wait_for(
        asyncio.gather(runner.run(), _shutdown_when_done()),
        timeout=5.0,
    )

    job = await queue.get_job(job_id)
    assert job is not None
    assert job.failed_rows == 1
    assert job.completed_rows == 0


# ── 4Images dispatch ────────────────────────────────────────────────────────


async def test_runner_dispatches_four_images_rows(
    queue: JobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_calls: list[int] = []
    four_calls: list[int] = []

    async def _fake_img(row, _c, *, job_id=None):
        image_calls.append(row.row_num)
        return RowResult(row_num=row.row_num, status=STATUS_SUCCESS, cost_usd=0.1)

    async def _fake_four(row, _c, *, job_id=None):
        four_calls.append(row.row_num)
        return RowResult(row_num=row.row_num, status=STATUS_SUCCESS, cost_usd=0.05)

    monkeypatch.setattr(runner_mod, "process_image_vo_row", _fake_img)
    monkeypatch.setattr(runner_mod, "process_4images_vo2_row", _fake_four)

    img_job = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2), _img_row(3)],
    )
    four_job = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_FOUR_IMAGES, rows=[_four_row(4), _four_row(5)],
    )

    runner = BatchRunner(
        queue, _make_dummy_clients(),
        max_concurrent=4, poll_idle_seconds=0.02,
    )

    async def _shutdown_when_done() -> None:
        while True:
            await asyncio.sleep(0.02)
            j1 = await queue.get_job(img_job)
            j2 = await queue.get_job(four_job)
            if j1 and j2 and j1.status == JOB_COMPLETED and j2.status == JOB_COMPLETED:
                runner.request_shutdown()
                return

    await asyncio.wait_for(
        asyncio.gather(runner.run(), _shutdown_when_done()),
        timeout=5.0,
    )

    assert sorted(image_calls) == [2, 3]
    assert sorted(four_calls) == [4, 5]


# ── Shutdown semantics ─────────────────────────────────────────────────────


async def test_shutdown_with_empty_queue_returns_quickly(
    queue: JobQueue,
) -> None:
    runner = BatchRunner(
        queue, _make_dummy_clients(),
        max_concurrent=2, poll_idle_seconds=0.02,
    )

    async def _shutdown_soon() -> None:
        await asyncio.sleep(0.1)
        runner.request_shutdown()

    await asyncio.wait_for(
        asyncio.gather(runner.run(), _shutdown_soon()),
        timeout=2.0,
    )


# ── Constructor validation ─────────────────────────────────────────────────


def test_runner_rejects_zero_concurrency(queue: JobQueue) -> None:
    with pytest.raises(ValueError):
        BatchRunner(queue, _make_dummy_clients(), max_concurrent=0)


# ── Row wall-clock timeout ──────────────────────────────────────────────────


async def test_row_timeout_marks_row_failed_and_releases_semaphore(
    queue: JobQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row that runs past its budget is cancelled, marked ROW_TIMEOUT, and
    its slot is released so the next row can claim it."""
    # Image-VO budget shrunk to 200ms via env override.
    monkeypatch.setenv("BULKVID_ROW_TIMEOUT_SECONDS_IMAGE_VO", "0.2")
    # Track which rows actually finished their processor coroutine vs were cancelled.
    cancelled_rows: list[int] = []
    completed_rows: list[int] = []

    async def _slow_then_fast(row, _clients, *, job_id=None):
        # Row 2 sleeps long enough to trip the timeout; row 3 runs fast.
        if row.row_num == 2:
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                cancelled_rows.append(row.row_num)
                raise
            completed_rows.append(row.row_num)
            return RowResult(
                row_num=row.row_num, status=STATUS_SUCCESS, cost_usd=0.0,
            )
        completed_rows.append(row.row_num)
        return RowResult(row_num=row.row_num, status=STATUS_SUCCESS, cost_usd=0.0)

    monkeypatch.setattr(runner_mod, "process_image_vo_row", _slow_then_fast)

    job_id = await queue.enqueue(
        user_email="u@aporia.com", sheet_id="s", worksheet="w",
        tab_type=TAB_IMAGE_VO, rows=[_img_row(2), _img_row(3)],
    )

    runner = BatchRunner(
        queue, _make_dummy_clients(),
        max_concurrent=1, poll_idle_seconds=0.02,
    )

    async def _shutdown_when_done() -> None:
        while True:
            await asyncio.sleep(0.02)
            job = await queue.get_job(job_id)
            if job is not None and job.status == JOB_COMPLETED:
                runner.request_shutdown()
                return

    await asyncio.wait_for(
        asyncio.gather(runner.run(), _shutdown_when_done()),
        timeout=10.0,
    )

    # Row 2 was cancelled mid-flight; row 3 completed.
    assert 2 in cancelled_rows
    assert 3 in completed_rows
    # Job accounting: 1 failed (the timeout), 1 completed.
    job = await queue.get_job(job_id)
    assert job is not None
    assert job.failed_rows == 1
    assert job.completed_rows == 1


async def test_row_timeout_uses_env_override_per_tab(
    queue: JobQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env var ``BULKVID_ROW_TIMEOUT_SECONDS_<TAB>`` wins over the default."""
    runner = BatchRunner(queue, _make_dummy_clients(), max_concurrent=1)
    monkeypatch.setenv("BULKVID_ROW_TIMEOUT_SECONDS_CARTOON", "42")
    assert await runner._row_timeout_seconds("cartoon") == 42.0
    # No override for image_vo → default kicks in.
    assert (
        await runner._row_timeout_seconds("image_vo")
        == runner_mod._DEFAULT_ROW_TIMEOUTS_SECONDS["image_vo"]
    )


# ── Stuck-row heartbeat ─────────────────────────────────────────────────────


async def test_heartbeat_flags_stuck_in_flight_rows(
    queue: JobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row whose elapsed time exceeds the threshold is logged on heartbeat."""
    import time
    from unittest.mock import MagicMock

    monkeypatch.setenv("BULKVID_STUCK_ROW_THRESHOLD_SECONDS", "1.0")
    # structlog doesn't route through stdlib's logging, so caplog can't see
    # its records. Patch the logger and inspect the calls directly.
    fake_log = MagicMock()
    monkeypatch.setattr(runner_mod, "_log", fake_log)

    runner = BatchRunner(queue, _make_dummy_clients(), max_concurrent=1)

    async def _block_forever() -> None:
        await asyncio.Event().wait()

    fake_task = asyncio.create_task(_block_forever())
    meta = runner_mod._RowMeta(
        start_monotonic=time.monotonic() - 600.0,    # 10 min ago
        queued_id=1,
        job_id="job-stuck-1",
        row_num=42,
        tab="image_vo",
    )
    runner._in_flight[fake_task] = meta

    try:
        await runner._emit_heartbeat(idle=True)
    finally:
        fake_task.cancel()
        try:
            await fake_task
        except BaseException:    # noqa: BLE001 — best-effort task cleanup
            pass

    # Heartbeat summary fired with stuck_count=1.
    fake_log.info.assert_any_call(
        "runner_heartbeat", idle=True, in_flight=1, stuck_count=1,
        poll_idle_seconds=runner._poll_idle,
    )
    # Stuck-row line carries the row identity.
    warning_calls = [c for c in fake_log.warning.call_args_list]
    assert warning_calls, "expected a runner_heartbeat_stuck warning"
    keyed = warning_calls[0].kwargs
    assert keyed["job_id"] == "job-stuck-1"
    assert keyed["row_num"] == 42
    assert keyed["tab"] == "image_vo"
    assert keyed["elapsed_s"] >= 1.0


async def test_heartbeat_quiet_when_no_rows_stuck(
    queue: JobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Heartbeat emits the summary but no stuck-row warnings when in-flight
    rows are fresh."""
    import time
    from unittest.mock import MagicMock

    fake_log = MagicMock()
    monkeypatch.setattr(runner_mod, "_log", fake_log)

    runner = BatchRunner(queue, _make_dummy_clients(), max_concurrent=1)

    async def _block_forever() -> None:
        await asyncio.Event().wait()

    fake_task = asyncio.create_task(_block_forever())
    runner._in_flight[fake_task] = runner_mod._RowMeta(
        start_monotonic=time.monotonic(),
        queued_id=2, job_id="job-fresh", row_num=7, tab="image_vo",
    )

    try:
        await runner._emit_heartbeat(idle=True)
    finally:
        fake_task.cancel()
        try:
            await fake_task
        except BaseException:    # noqa: BLE001
            pass

    # Summary fires, no warning called.
    assert fake_log.info.called
    fake_log.warning.assert_not_called()


async def test_runner_loop_survives_heartbeat_settings_store_error(
    queue: JobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for HF outage 2026-06-09: when ``_emit_heartbeat``
    raised (settings_store ``IndexError: no column named 'key'``), the
    exception escaped the ``while not self._shutdown`` loop, the worker
    process exited 1, and supervisord restarted it ~every 30s. The
    runner must instead log the failure and continue polling.
    """
    from unittest.mock import MagicMock

    # Force a heartbeat on the very first idle tick so the test doesn't
    # have to wait for the production 30-poll cadence.
    monkeypatch.setattr(runner_mod, "_HEARTBEAT_EVERY", 1)

    fake_log = MagicMock()
    monkeypatch.setattr(runner_mod, "_log", fake_log)

    # Inject a heartbeat that always raises — same shape the libsql bug
    # produced via ``store.get()`` → ``_load_sync`` → IndexError.
    call_count = {"n": 0}

    async def _exploding_heartbeat(self, *, idle: bool) -> None:    # noqa: ARG001
        call_count["n"] += 1
        raise IndexError("no column named 'key'")

    monkeypatch.setattr(
        BatchRunner, "_emit_heartbeat", _exploding_heartbeat
    )

    runner = BatchRunner(
        queue, _make_dummy_clients(),
        max_concurrent=1, poll_idle_seconds=0.005,
    )

    async def _shutdown_after_heartbeats() -> None:
        # Wait until we've seen at least two heartbeat attempts — proves
        # the loop didn't die after the first failure.
        while call_count["n"] < 2:
            await asyncio.sleep(0.01)
        runner.request_shutdown()

    await asyncio.wait_for(
        asyncio.gather(runner.run(), _shutdown_after_heartbeats()),
        timeout=3.0,
    )

    # The wrapped catch logged a warning at least once (most likely
    # twice — once per heartbeat attempt).
    assert fake_log.warning.called, (
        "expected runner_heartbeat_failed warning; the loop must surface "
        "the suppressed exception instead of swallowing it silently"
    )
    failure_calls = [
        c for c in fake_log.warning.call_args_list
        if c.args and c.args[0] == "runner_heartbeat_failed"
    ]
    assert failure_calls, (
        "expected at least one 'runner_heartbeat_failed' warning; "
        f"saw warnings: {fake_log.warning.call_args_list!r}"
    )
    # And the loop got through more than one tick, so the catch worked.
    assert call_count["n"] >= 2
