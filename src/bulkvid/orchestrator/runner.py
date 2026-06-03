"""BatchRunner — drains the SQLite queue with a concurrency semaphore.

The worker (``bulkvid.worker``) instantiates one ``BatchRunner`` and calls
``runner.run()``. The runner:

  1. Recovers orphaned PROCESSING rows on startup
  2. Loops forever, pulling pending rows from the SQLite queue
  3. Dispatches each row to the matching row processor (Image-VO or 4Images-VO2)
  4. Caps concurrent in-flight rows at ``max_concurrent`` (admin-tunable)
  5. Records each result back to the queue + invokes the sheet-write callback
  6. Stops claiming new work when the shutdown event is set, waits for
     in-flight rows to finish, then returns

Sheet integration (the actual gspread call) is decoupled via a callback —
the runner doesn't know or care how results get back to the user's sheet.
That keeps the runner test-friendly and lets the sheet adapter ship in Phase 4.

Plan §5 ("Concurrency model"), §13 Phase 3.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from bulkvid.logging import get_logger, set_context
from bulkvid.models.row import (
    STATUS_INTERNAL_ERROR,
    FourImagesVO2Row,
    ImageVORow,
    RowResult,
    SimpleRow,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.queue import JobQueue, QueuedRow, payload_to_row
from bulkvid.orchestrator.row_processor_4images import process_4images_vo2_row
from bulkvid.orchestrator.row_processor_image_vo import process_image_vo_row
from bulkvid.orchestrator.row_processor_simple import process_simple_row
from bulkvid.orchestrator.sheet_writer import PendingWrite

_log = get_logger("runner")


# Callback the runner calls after every row completes. The Phase 4 sheet
# adapter implements it as "buffer this write and flush in 5s".
WriteBackCallback = Callable[[PendingWrite], Awaitable[None]]


async def _noop_write_back(_write: PendingWrite) -> None:
    """Default write-back: do nothing. Tests inject something real."""


class BatchRunner:
    """Concurrent row drainer with shutdown semantics."""

    def __init__(
        self,
        queue: JobQueue,
        clients: PipelineClients,
        *,
        max_concurrent: int = 10,
        poll_idle_seconds: float = 1.0,
        write_back: WriteBackCallback | None = None,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self._queue = queue
        self._clients = clients
        self._sem = asyncio.Semaphore(max_concurrent)
        self._poll_idle = poll_idle_seconds
        self._write_back = write_back or _noop_write_back
        self._shutdown = asyncio.Event()
        self._in_flight: set[asyncio.Task] = set()

    def request_shutdown(self) -> None:
        _log.info("runner_shutdown_requested")
        self._shutdown.set()

    async def run(self) -> None:
        """Main loop. Blocks until ``request_shutdown`` is called AND in-flight rows finish."""
        recovered = await self._queue.recover_orphaned_rows()
        _log.info(
            "runner_start",
            max_concurrent=self._sem._value,           # type: ignore[attr-defined]
            poll_idle_seconds=self._poll_idle,
            recovered_orphans=recovered,
        )

        try:
            while not self._shutdown.is_set():
                queued = await self._queue.claim_next_row()
                if queued is None:
                    # Empty queue. Wait briefly, or exit if shutdown fired.
                    try:
                        await asyncio.wait_for(
                            self._shutdown.wait(), timeout=self._poll_idle
                        )
                    except asyncio.TimeoutError:
                        pass
                    continue

                await self._sem.acquire()       # block if at max_concurrent
                task = asyncio.create_task(self._handle_row(queued))
                self._in_flight.add(task)
                task.add_done_callback(self._on_task_done)

            _log.info("runner_draining", in_flight=len(self._in_flight))
            if self._in_flight:
                await asyncio.gather(*self._in_flight, return_exceptions=True)
        finally:
            _log.info("runner_stop", final_in_flight=len(self._in_flight))

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._in_flight.discard(task)
        self._sem.release()

    async def _handle_row(self, queued: QueuedRow) -> None:
        set_context(batch_id=queued.job_id, row_num=queued.row_num)
        result: RowResult
        try:
            row = payload_to_row(dict(queued.payload))
            if isinstance(row, SimpleRow):
                result = await process_simple_row(
                    row, self._clients, job_id=queued.job_id
                )
            elif isinstance(row, ImageVORow):
                result = await process_image_vo_row(
                    row, self._clients, job_id=queued.job_id
                )
            elif isinstance(row, FourImagesVO2Row):
                result = await process_4images_vo2_row(
                    row, self._clients, job_id=queued.job_id
                )
            else:    # defensive
                result = RowResult(
                    row_num=queued.row_num,
                    status=STATUS_INTERNAL_ERROR,
                    error=f"Unknown row type: {type(row).__name__}",
                )
        except Exception as e:    # last-line defense — never let the loop die
            _log.exception("runner_row_unhandled_error", error=str(e))
            result = RowResult(
                row_num=queued.row_num,
                status=STATUS_INTERNAL_ERROR,
                error=f"unhandled: {e!s}",
            )

        try:
            await self._queue.record_result(queued.id, result)
        except Exception as e:
            _log.exception("runner_record_result_failed", error=str(e))

        # Look up the job's sheet routing so the writer can flush without
        # a second queue hit. If the job vanished mid-run, skip silently.
        try:
            job = await self._queue.get_job(queued.job_id)
            if job is None:
                return
            write = PendingWrite(
                job_id=job.job_id,
                sheet_id=job.sheet_id,
                worksheet=job.worksheet,
                tab_type=job.tab_type,
                row_num=result.row_num,
                video_urls=list(result.video_urls),
                status=result.status,
                error=result.error,
            )
            await self._write_back(write)
        except Exception as e:
            _log.exception("runner_write_back_failed", error=str(e))

    # ── Test hooks ──────────────────────────────────────────────────────────

    @property
    def in_flight_count(self) -> int:
        return len(self._in_flight)
