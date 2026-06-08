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
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from bulkvid.logging import get_logger, set_context
from bulkvid.models.row import (
    STATUS_INTERNAL_ERROR,
    STATUS_ROW_TIMEOUT,
    CartoonRow,
    FourImagesVO2Row,
    ImageVORow,
    RowResult,
    SimpleRow,
    SimpleX4Row,
    TextOnImgRow,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.queue import JobQueue, QueuedRow, payload_to_row
from bulkvid.orchestrator.row_processor_4images import process_4images_vo2_row
from bulkvid.orchestrator.row_processor_cartoon import process_cartoon_row
from bulkvid.orchestrator.row_processor_image_vo import process_image_vo_row
from bulkvid.orchestrator.row_processor_simple import process_simple_row
from bulkvid.orchestrator.row_processor_simple_x4 import process_simple_x4_row
from bulkvid.orchestrator.row_processor_text_on_img import process_text_on_img_row
from bulkvid.orchestrator.runtime_settings import (
    SETTING_ROW_TIMEOUT_4IMAGES,
    SETTING_ROW_TIMEOUT_CARTOON,
    SETTING_ROW_TIMEOUT_IMAGE_VO,
    SETTING_ROW_TIMEOUT_SIMPLE,
    SETTING_STUCK_ROW_THRESHOLD,
)
from bulkvid.orchestrator.sheet_writer import PendingWrite

_log = get_logger("runner")


# ── Row wall-clock timeouts ─────────────────────────────────────────────────
#
# Plan ``_plans/2026-06-07-overload-handling-and-template-defaults.md`` §A.2.
# Each row is wrapped in ``asyncio.wait_for(...)`` so a stuck provider call
# never holds a semaphore slot indefinitely. Values are read with this
# precedence:
#
#   1. Env var ``BULKVID_ROW_TIMEOUT_SECONDS_<TAB>`` (per-deploy override)
#   2. Settings store key ``row_timeout_<tab>_seconds`` (admin-editable)
#   3. Hardcoded default below
#
# Cartoon legitimately runs ~3x longer than the others (planner + N shots).

_TAB_SIMPLE = "simple"
_TAB_IMAGE_VO = "image_vo"
_TAB_4IMAGES = "4images"
_TAB_CARTOON = "cartoon"
_TAB_SIMPLE_X4 = "simple_x4"
_TAB_TEXT_ON_IMG = "text_on_img"

_DEFAULT_ROW_TIMEOUTS_SECONDS: dict[str, float] = {
    _TAB_SIMPLE: 720.0,         # 12 min
    _TAB_IMAGE_VO: 900.0,       # 15 min — image gen is heavier
    _TAB_4IMAGES: 720.0,        # 12 min
    _TAB_CARTOON: 1200.0,       # 20 min — planner + multi-shot
    # simple_x4 shares image_vo's heavy image-gen path + adds one small
    # headline GPT call + CPU-bound overlay work. Same budget — overlay
    # work is sub-second, headline call is <2s.
    _TAB_SIMPLE_X4: 900.0,
    # text_on_img is essentially simple + one Pillow text-overlay step
    # (CPU, sub-second). Same budget as simple.
    _TAB_TEXT_ON_IMG: 720.0,
}

_TIMEOUT_SETTING_KEY_BY_TAB: dict[str, str] = {
    _TAB_SIMPLE: SETTING_ROW_TIMEOUT_SIMPLE,
    _TAB_IMAGE_VO: SETTING_ROW_TIMEOUT_IMAGE_VO,
    _TAB_4IMAGES: SETTING_ROW_TIMEOUT_4IMAGES,
    _TAB_CARTOON: SETTING_ROW_TIMEOUT_CARTOON,
    # simple_x4 reuses the image_vo timeout setting — same shape of work.
    _TAB_SIMPLE_X4: SETTING_ROW_TIMEOUT_IMAGE_VO,
    # text_on_img reuses the simple timeout — same shape of work + overlay.
    _TAB_TEXT_ON_IMG: SETTING_ROW_TIMEOUT_SIMPLE,
}

# Stuck-row detection: anything in flight longer than this is flagged in
# every heartbeat tick. Default 5 minutes; admin-editable.
DEFAULT_STUCK_ROW_THRESHOLD_SECONDS = 300.0


def _tab_for_row(row: object) -> str:
    """Map a row instance to the timeout tab name."""
    if isinstance(row, SimpleRow):
        return _TAB_SIMPLE
    if isinstance(row, SimpleX4Row):
        return _TAB_SIMPLE_X4
    if isinstance(row, TextOnImgRow):
        return _TAB_TEXT_ON_IMG
    if isinstance(row, ImageVORow):
        return _TAB_IMAGE_VO
    if isinstance(row, FourImagesVO2Row):
        return _TAB_4IMAGES
    if isinstance(row, CartoonRow):
        return _TAB_CARTOON
    # Unknown shape — give it the longest budget so we don't kill it prematurely.
    return _TAB_CARTOON


def _env_timeout_override(tab: str) -> float | None:
    raw = os.environ.get(f"BULKVID_ROW_TIMEOUT_SECONDS_{tab.upper()}")
    if not raw:
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except ValueError:
        return None


def _env_stuck_threshold_override() -> float | None:
    raw = os.environ.get("BULKVID_STUCK_ROW_THRESHOLD_SECONDS")
    if not raw:
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except ValueError:
        return None


def _parse_positive_float(raw: str) -> float | None:
    """Parse a settings-store string to a positive float, else ``None``."""
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    return v if v > 0 else None


@dataclass
class _RowMeta:
    """In-flight bookkeeping for stuck-row detection.

    ``start_monotonic`` is a ``time.monotonic()`` reading — immune to wall-
    clock jumps, which is what we want for an "elapsed seconds" calculation.
    """

    start_monotonic: float
    queued_id: int
    job_id: str
    row_num: int
    tab: str


# Callback the runner calls after every row completes. The Phase 4 sheet
# adapter implements it as "buffer this write and flush in 5s".
WriteBackCallback = Callable[[PendingWrite], Awaitable[None]]


async def _noop_write_back(_write: PendingWrite) -> None:
    """Default write-back: do nothing. Tests inject something real."""


async def _dispatch_to_processor(
    row: object, clients: PipelineClients, job_id: str
) -> RowResult:
    """Route a parsed row to the matching processor.

    Pulled out as a module-level coroutine so ``asyncio.wait_for`` wraps a
    single, clean coroutine — the cancellation semantics on timeout are
    simpler when ``wait_for`` only has to cancel one task.
    """
    if isinstance(row, SimpleRow):
        return await process_simple_row(row, clients, job_id=job_id)
    if isinstance(row, SimpleX4Row):
        return await process_simple_x4_row(row, clients, job_id=job_id)
    if isinstance(row, TextOnImgRow):
        return await process_text_on_img_row(row, clients, job_id=job_id)
    if isinstance(row, ImageVORow):
        return await process_image_vo_row(row, clients, job_id=job_id)
    if isinstance(row, FourImagesVO2Row):
        return await process_4images_vo2_row(row, clients, job_id=job_id)
    if isinstance(row, CartoonRow):
        return await process_cartoon_row(row, clients, job_id=job_id)
    # Defensive — payload survived parsing but matched no known shape.
    return RowResult(
        row_num=getattr(row, "row_num", 0),
        status=STATUS_INTERNAL_ERROR,
        error=f"Unknown row type: {type(row).__name__}",
    )


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
        # Map task -> metadata. Metadata is required for stuck-row detection
        # in the heartbeat and for the row-timeout failure path. A plain set
        # of tasks is no longer enough.
        self._in_flight: dict[asyncio.Task, _RowMeta] = {}

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
            # Heartbeat counter: log every N polls (idle or busy) so a stalled
            # runner shows up in observability instead of looking identical to
            # a healthy idle one. Heartbeat now also flags any in-flight row
            # whose elapsed time exceeds the stuck-row threshold.
            _polls_since_heartbeat = 0
            _HEARTBEAT_EVERY = 30
            while not self._shutdown.is_set():
                queued = await self._queue.claim_next_row()
                if queued is None:
                    _polls_since_heartbeat += 1
                    if _polls_since_heartbeat >= _HEARTBEAT_EVERY:
                        await self._emit_heartbeat(idle=True)
                        _polls_since_heartbeat = 0
                    # Empty queue. Wait briefly, or exit if shutdown fired.
                    try:
                        await asyncio.wait_for(
                            self._shutdown.wait(), timeout=self._poll_idle
                        )
                    except TimeoutError:
                        pass
                    continue
                _polls_since_heartbeat = 0    # reset on real work — busy runner
                # emits its heartbeat via the stuck-row check on idle ticks anyway.

                await self._sem.acquire()       # block if at max_concurrent
                meta = self._build_row_meta(queued)
                task = asyncio.create_task(self._handle_row(queued, meta))
                self._in_flight[task] = meta
                task.add_done_callback(self._on_task_done)

            _log.info("runner_draining", in_flight=len(self._in_flight))
            if self._in_flight:
                await asyncio.gather(*self._in_flight, return_exceptions=True)
        finally:
            _log.info("runner_stop", final_in_flight=len(self._in_flight))

    def _build_row_meta(self, queued: QueuedRow) -> _RowMeta:
        tab = str(queued.payload.get("__tab__") or "")
        return _RowMeta(
            start_monotonic=time.monotonic(),
            queued_id=queued.id,
            job_id=queued.job_id,
            row_num=queued.row_num,
            tab=tab,
        )

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._in_flight.pop(task, None)
        self._sem.release()

    async def _emit_heartbeat(self, *, idle: bool) -> None:
        """Log a heartbeat. Always enumerates stuck in-flight rows.

        Called from the idle path at most once every ~30 polls. The stuck-row
        section is the load-bearing part: it makes a hung row (e.g. a Gemini
        TTS call that never returns) visible in the log without anyone
        having to dig into ``ps``.
        """
        threshold = await self._stuck_threshold_seconds()
        now = time.monotonic()
        stuck: list[_RowMeta] = []
        for meta in self._in_flight.values():
            elapsed = now - meta.start_monotonic
            if elapsed >= threshold:
                stuck.append(meta)

        _log.info(
            "runner_heartbeat",
            idle=idle,
            in_flight=len(self._in_flight),
            stuck_count=len(stuck),
            poll_idle_seconds=self._poll_idle,
        )
        for meta in stuck:
            _log.warning(
                "runner_heartbeat_stuck",
                job_id=meta.job_id,
                row_num=meta.row_num,
                tab=meta.tab,
                elapsed_s=round(now - meta.start_monotonic, 1),
                threshold_s=threshold,
            )

    async def _stuck_threshold_seconds(self) -> float:
        # Resolution order: env var > admin settings store > module default.
        env = _env_stuck_threshold_override()
        if env is not None:
            return env
        store = self._clients.settings_store
        if store is not None:
            raw = await store.get(SETTING_STUCK_ROW_THRESHOLD, default="")
            parsed = _parse_positive_float(raw)
            if parsed is not None:
                return parsed
        return DEFAULT_STUCK_ROW_THRESHOLD_SECONDS

    async def _row_timeout_seconds(self, tab: str) -> float:
        env = _env_timeout_override(tab)
        if env is not None:
            return env
        store = self._clients.settings_store
        key = _TIMEOUT_SETTING_KEY_BY_TAB.get(tab)
        if store is not None and key is not None:
            raw = await store.get(key, default="")
            parsed = _parse_positive_float(raw)
            if parsed is not None:
                return parsed
        return _DEFAULT_ROW_TIMEOUTS_SECONDS.get(
            tab, _DEFAULT_ROW_TIMEOUTS_SECONDS[_TAB_CARTOON]
        )

    async def _handle_row(self, queued: QueuedRow, meta: _RowMeta) -> None:
        set_context(batch_id=queued.job_id, row_num=queued.row_num)
        result: RowResult
        try:
            row = payload_to_row(dict(queued.payload))
            # Pick the wall-clock budget from the row's actual tab (not the
            # ``__tab__`` payload string, which is the canonical source but
            # could be empty on a hand-crafted test payload).
            tab = _tab_for_row(row)
            timeout_seconds = await self._row_timeout_seconds(tab)
            processor_coro = _dispatch_to_processor(
                row, self._clients, queued.job_id
            )
            try:
                result = await asyncio.wait_for(
                    processor_coro, timeout=timeout_seconds
                )
            except TimeoutError:
                # ``asyncio.wait_for`` cancelled the processor task; its
                # ``finally`` blocks have already run. Record the failure
                # explicitly so the sidebar shows ``ROW_TIMEOUT`` rather than
                # "ran forever then disappeared."
                elapsed = round(time.monotonic() - meta.start_monotonic, 1)
                _log.warning(
                    "runner_row_timeout",
                    job_id=queued.job_id,
                    row_num=queued.row_num,
                    tab=tab,
                    timeout_s=timeout_seconds,
                    elapsed_s=elapsed,
                )
                result = RowResult(
                    row_num=queued.row_num,
                    status=STATUS_ROW_TIMEOUT,
                    error=(
                        f"Row timed out after {int(elapsed)}s "
                        f"(budget {int(timeout_seconds)}s)."
                    ),
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
