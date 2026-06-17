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
    AvatarRow,
    CartoonRow,
    FourImagesVO2Row,
    ImageVORow,
    RowResult,
    SimpleRow,
    SimpleX4Row,
    TextOnImgRow,
    YtCartoonRow,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.queue import JobQueue, QueuedRow, payload_to_row
from bulkvid.orchestrator.row_processor_4images import process_4images_vo2_row
from bulkvid.orchestrator.row_processor_avatar import process_avatar_row
from bulkvid.orchestrator.row_processor_cartoon import process_cartoon_row
from bulkvid.orchestrator.row_processor_image_vo import process_image_vo_row
from bulkvid.orchestrator.row_processor_simple import process_simple_row
from bulkvid.orchestrator.row_processor_simple_x4 import process_simple_x4_row
from bulkvid.orchestrator.row_processor_text_on_img import process_text_on_img_row
from bulkvid.orchestrator.row_processor_yt_cartoon import process_yt_cartoon_row
from bulkvid.orchestrator.runtime_settings import (
    SETTING_ROW_TIMEOUT_4IMAGES,
    SETTING_ROW_TIMEOUT_CARTOON,
    SETTING_ROW_TIMEOUT_IMAGE_VO,
    SETTING_ROW_TIMEOUT_SIMPLE,
    SETTING_ROW_TIMEOUT_YT_CARTOON,
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
_TAB_YT_CARTOON = "yt_cartoon"
_TAB_SIMPLE_X4 = "simple_x4"
_TAB_TEXT_ON_IMG = "text_on_img"
_TAB_AVATAR = "avatar"

_DEFAULT_ROW_TIMEOUTS_SECONDS: dict[str, float] = {
    _TAB_SIMPLE: 720.0,         # 12 min
    _TAB_IMAGE_VO: 900.0,       # 15 min — image gen is heavier
    _TAB_4IMAGES: 720.0,        # 12 min
    _TAB_CARTOON: 1200.0,       # 20 min — planner + multi-shot
    # yt-cartoon runs cartoon's pipeline but with up to 5 shots (20s bucket),
    # so it gets more headroom than the flat-8s cartoon tab.
    _TAB_YT_CARTOON: 1500.0,    # 25 min
    # simple_x4 shares image_vo's heavy image-gen path + adds one small
    # headline GPT call + CPU-bound overlay work. Same budget — overlay
    # work is sub-second, headline call is <2s.
    _TAB_SIMPLE_X4: 900.0,
    # text_on_img is essentially simple + one Pillow text-overlay step
    # (CPU, sub-second). Same budget as simple.
    _TAB_TEXT_ON_IMG: 720.0,
    # avatar adds a TikTok Symphony call (30-90s typical) to the cartoon
    # flow. Same 20-min ceiling — multi-shot planner + image-gen + the
    # parallel avatar call all fit in the cartoon budget.
    _TAB_AVATAR: 1200.0,
}

_TIMEOUT_SETTING_KEY_BY_TAB: dict[str, str] = {
    _TAB_SIMPLE: SETTING_ROW_TIMEOUT_SIMPLE,
    _TAB_IMAGE_VO: SETTING_ROW_TIMEOUT_IMAGE_VO,
    _TAB_4IMAGES: SETTING_ROW_TIMEOUT_4IMAGES,
    _TAB_CARTOON: SETTING_ROW_TIMEOUT_CARTOON,
    _TAB_YT_CARTOON: SETTING_ROW_TIMEOUT_YT_CARTOON,
    # simple_x4 reuses the image_vo timeout setting — same shape of work.
    _TAB_SIMPLE_X4: SETTING_ROW_TIMEOUT_IMAGE_VO,
    # text_on_img reuses the simple timeout — same shape of work + overlay.
    _TAB_TEXT_ON_IMG: SETTING_ROW_TIMEOUT_SIMPLE,
    # avatar reuses cartoon's timeout — multi-shot pipeline + TikTok poll.
    _TAB_AVATAR: SETTING_ROW_TIMEOUT_CARTOON,
}

# Stuck-row detection: anything in flight longer than this is flagged in
# every heartbeat tick. Default 5 minutes; admin-editable.
DEFAULT_STUCK_ROW_THRESHOLD_SECONDS = 300.0


# Hard timeout around every libsql query the worker makes against the
# shared queue. Without this, a single stalled Turso HTTP call blocks
# the ``asyncio.to_thread`` worker thread INDEFINITELY, the runner's
# main ``await`` never returns, no heartbeat fires, and the worker
# silently hangs while jobs pile up in the queue. Chat 2026-06-09: the
# operator saw exactly this — ``job_enqueued`` + ``active_jobs=1`` but
# zero ``runner_heartbeat`` lines for 100+ seconds, so the worker was
# deadlocked on an unresponsive libsql connection.
#
# 30 s is loose enough to absorb a slow libsql roundtrip (we've seen
# cold-start spikes ~5-10 s) but tight enough that a genuinely stalled
# connection gets caught on the next ``poll_idle`` tick rather than
# forever. Override via env var on a per-deploy basis.
_WORKER_QUERY_TIMEOUT_SECONDS = float(
    os.environ.get("BULKVID_WORKER_QUERY_TIMEOUT_SECONDS") or 30.0
)
# Brief backoff after a query timeout so a flapping Turso doesn't get
# hammered. Independent of ``poll_idle_seconds`` (which controls the
# normal idle cadence).
_WORKER_QUERY_TIMEOUT_BACKOFF_SECONDS = 2.0


# Heartbeat cadence: number of consecutive empty polls before the runner
# emits a heartbeat. With the default ``poll_idle_seconds=1.0`` this is
# one heartbeat every ~30s — frequent enough to spot a stalled runner,
# infrequent enough not to flood logs. Module-level so the regression
# test in ``test_runner.py`` can patch it down to 1 for fast triggering
# without rewriting the runner loop.
_HEARTBEAT_EVERY = 30


# ── record_result retry queue ───────────────────────────────────────────────
#
# Plan ``_plans/2026-06-14-stuck-processing-rows.md`` §A. When the worker
# completes a row but ``record_result`` raises (TimeoutError on a Turso
# flap, any other exception), the result MUST NOT be silently lost — the
# old behavior left the row stuck in PROCESSING in the DB while the sheet
# write proceeded via the decoupled ``CoalescedSheetWriter`` buffer. The
# operator saw "Starting.." in the sidebar with a growing elapsed timer
# while the URLs were already in the sheet. Worker restart was the only
# workaround, and it re-spent every stuck row.
#
# Fix: push failed records onto an in-process ``asyncio.Queue``. A
# long-running drainer task retries each entry with exponential backoff
# (1 s → 2 s → 4 s → 8 s, capped at 30 s) until either it succeeds OR
# the per-entry budget below is exhausted. ``_record_result_sync`` is
# idempotent (it UPDATEs by row id), so retrying is always safe.
#
# Per-entry budget — total time we'll spend trying to land a single
# record_result before logging ``runner_pending_record_giveup`` and
# dropping the entry. 5 minutes covers a typical Turso flap; admin can
# raise to 30 min for a known multi-hour outage. Env override:
# ``BULKVID_RECORD_RESULT_RETRY_MAX_SECONDS``.
_RECORD_RESULT_RETRY_MAX_SECONDS = float(
    os.environ.get("BULKVID_RECORD_RESULT_RETRY_MAX_SECONDS") or 300.0
)
# Maximum backoff between retries — the exponential schedule (1, 2, 4, 8, 16)
# caps here so a long outage settles into a steady 30 s polling cadence.
_RECORD_RESULT_RETRY_MAX_BACKOFF_SECONDS = 30.0
# Time we wait, after ``request_shutdown``, for the drainer to flush the
# pending queue before logging anything still stuck as
# ``runner_pending_record_dropped``. Bounded so a clean shutdown isn't
# blocked indefinitely by a non-responsive Turso. Tuned to ~1.5×
# ``_WORKER_QUERY_TIMEOUT_SECONDS`` so the drainer has time to complete
# at least one full per-call attempt before we cancel it.
_RECORD_RESULT_SHUTDOWN_DRAIN_BUDGET_SECONDS = 45.0
# Buffer-size warning threshold — once the pending queue grows past this
# many entries, every additional N (``_RECORD_RESULT_BACKLOG_LOG_EVERY``)
# logs a ``runner_pending_records_backlog`` warning so the operator sees
# a Turso outage in their HF logs without having to dig.
_RECORD_RESULT_BACKLOG_THRESHOLD = 1000
_RECORD_RESULT_BACKLOG_LOG_EVERY = 100


def _tab_for_row(row: object) -> str:
    """Map a row instance to the timeout tab name."""
    if isinstance(row, SimpleRow):
        return _TAB_SIMPLE
    if isinstance(row, SimpleX4Row):
        return _TAB_SIMPLE_X4
    if isinstance(row, TextOnImgRow):
        return _TAB_TEXT_ON_IMG
    if isinstance(row, AvatarRow):
        return _TAB_AVATAR
    if isinstance(row, ImageVORow):
        return _TAB_IMAGE_VO
    if isinstance(row, FourImagesVO2Row):
        return _TAB_4IMAGES
    if isinstance(row, YtCartoonRow):
        return _TAB_YT_CARTOON
    if isinstance(row, CartoonRow):
        return _TAB_CARTOON
    # Unknown shape — give it the longest budget so we don't kill it prematurely.
    return _TAB_YT_CARTOON


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
    if isinstance(row, AvatarRow):
        return await process_avatar_row(row, clients, job_id=job_id)
    if isinstance(row, ImageVORow):
        return await process_image_vo_row(row, clients, job_id=job_id)
    if isinstance(row, FourImagesVO2Row):
        return await process_4images_vo2_row(row, clients, job_id=job_id)
    if isinstance(row, YtCartoonRow):
        return await process_yt_cartoon_row(row, clients, job_id=job_id)
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
        # Plan ``_plans/2026-06-14-stuck-processing-rows.md`` §A. Holds
        # ``(queued_id, result, attempt, total_wait_s)`` tuples whose
        # initial ``record_result`` raised. A background drainer task
        # (``_drain_pending_records``) retries each with exponential
        # backoff so a Turso flap doesn't strand rows in PROCESSING
        # forever. Unbounded queue — the typical operation never enqueues
        # here; only sustained Turso outages do. The drainer logs a
        # backlog warning past the threshold so the buildup is visible.
        self._pending_records: asyncio.Queue[
            tuple[int, RowResult, int, float]
        ] = asyncio.Queue()
        self._next_backlog_warn_at: int = _RECORD_RESULT_BACKLOG_THRESHOLD

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
        # Start the pending-records drainer alongside the main loop. It
        # runs until shutdown is set AND the queue is empty (with a
        # bounded extra drain budget on shutdown so a clean stop isn't
        # blocked indefinitely by a non-responsive Turso). Plan
        # ``_plans/2026-06-14-stuck-processing-rows.md`` §A.
        drainer = asyncio.create_task(self._drain_pending_records())
        _log.info("runner_pending_drainer_start")

        try:
            # Heartbeat counter: log every N polls (idle or busy) so a stalled
            # runner shows up in observability instead of looking identical to
            # a healthy idle one. Heartbeat now also flags any in-flight row
            # whose elapsed time exceeds the stuck-row threshold. The cadence
            # constant lives at module scope so tests can patch it.
            _polls_since_heartbeat = 0
            while not self._shutdown.is_set():
                # Hard timeout around the libsql claim query. A stalled
                # Turso HTTP roundtrip would otherwise hang this await
                # forever — no heartbeat, no recovery (chat 2026-06-09).
                try:
                    queued = await asyncio.wait_for(
                        self._queue.claim_next_row(),
                        timeout=_WORKER_QUERY_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    _log.warning(
                        "runner_claim_timeout",
                        timeout_s=_WORKER_QUERY_TIMEOUT_SECONDS,
                        backoff_s=_WORKER_QUERY_TIMEOUT_BACKOFF_SECONDS,
                    )
                    # Brief backoff so a flapping libsql doesn't get
                    # pummeled, then continue the loop. Shutdown still
                    # interrupts via the wait_for below.
                    try:
                        await asyncio.wait_for(
                            self._shutdown.wait(),
                            timeout=_WORKER_QUERY_TIMEOUT_BACKOFF_SECONDS,
                        )
                    except TimeoutError:
                        pass
                    continue
                except Exception as e:    # noqa: BLE001 — runner must keep running
                    _log.exception("runner_claim_error", error=str(e))
                    try:
                        await asyncio.wait_for(
                            self._shutdown.wait(),
                            timeout=_WORKER_QUERY_TIMEOUT_BACKOFF_SECONDS,
                        )
                    except TimeoutError:
                        pass
                    continue
                if queued is None:
                    _polls_since_heartbeat += 1
                    if _polls_since_heartbeat >= _HEARTBEAT_EVERY:
                        # Heartbeat is observational. A failure here (most
                        # likely a transient settings_store read error, e.g.
                        # the libsql ``no column named 'key'`` outage on
                        # 2026-06-09) must NOT escape the loop, because that
                        # would crash the worker process and force a
                        # supervisord restart cycle. Log the failure and
                        # carry on; one missing heartbeat is fine.
                        try:
                            await self._emit_heartbeat(idle=True)
                        except Exception as e:
                            _log.warning(
                                "runner_heartbeat_failed",
                                error=str(e)[:200],
                                error_type=type(e).__name__,
                            )
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
            # Give the pending-records drainer a bounded window to flush
            # its queue (in-flight rows that finished post-shutdown may
            # still have buffered failed records). Beyond the budget, log
            # each leftover so an operator can reconcile manually.
            if not drainer.done():
                try:
                    await asyncio.wait_for(
                        drainer,
                        timeout=_RECORD_RESULT_SHUTDOWN_DRAIN_BUDGET_SECONDS,
                    )
                except TimeoutError:
                    drainer.cancel()
                    try:
                        await drainer
                    except (asyncio.CancelledError, Exception):
                        pass
                    # Drain whatever is still queued into the log so the
                    # results are at least recoverable from observability.
                    while not self._pending_records.empty():
                        try:
                            queued_id, result, attempts, total_wait = (
                                self._pending_records.get_nowait()
                            )
                        except asyncio.QueueEmpty:
                            break
                        _log.error(
                            "runner_pending_record_dropped",
                            queued_id=queued_id,
                            row_num=result.row_num,
                            status=result.status,
                            video_urls=list(result.video_urls),
                            error=result.error,
                            attempts=attempts,
                            total_wait_s=round(total_wait, 1),
                        )
            _log.info(
                "runner_pending_drainer_stop",
                pending_at_stop=self._pending_records.qsize(),
            )
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

        # Same timeout discipline as the claim above: a stalled libsql
        # write would otherwise pin this task forever and silently leak a
        # semaphore slot. ``record_result`` is idempotent on retry because
        # the SET writes the same status; getting through eventually is
        # more important than waiting indefinitely on a hung roundtrip.
        #
        # Plan ``_plans/2026-06-14-stuck-processing-rows.md`` §A: when the
        # primary attempt fails (TimeoutError or any other exception), we
        # MUST NOT drop the result on the floor. The old behaviour left
        # the row stuck in PROCESSING in the DB while the sheet still got
        # the URLs via the decoupled CoalescedSheetWriter. Buffer onto
        # ``_pending_records`` so the drainer task can retry later;
        # ``_write_back`` continues to fire so the sheet still lands.
        record_failed = False
        record_error: str = ""
        try:
            await asyncio.wait_for(
                self._queue.record_result(queued.id, result),
                timeout=_WORKER_QUERY_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            record_failed = True
            record_error = f"timeout after {_WORKER_QUERY_TIMEOUT_SECONDS}s"
            _log.warning(
                "runner_record_result_timeout",
                queued_id=queued.id,
                timeout_s=_WORKER_QUERY_TIMEOUT_SECONDS,
            )
        except Exception as e:
            record_failed = True
            record_error = f"{type(e).__name__}: {e!s}"[:200]
            _log.exception("runner_record_result_failed", error=str(e))

        if record_failed:
            self._buffer_pending_record(queued.id, result, reason=record_error)

        # Look up the job's sheet routing so the writer can flush without
        # a second queue hit. If the job vanished mid-run, skip silently.
        try:
            job = await asyncio.wait_for(
                self._queue.get_job(queued.job_id),
                timeout=_WORKER_QUERY_TIMEOUT_SECONDS,
            )
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

    # ── Pending-record retry queue (Plan §A) ────────────────────────────────

    def _buffer_pending_record(
        self, queued_id: int, result: RowResult, *, reason: str,
    ) -> None:
        """Enqueue a failed ``record_result`` for the background drainer.

        Called from ``_handle_row`` when the primary attempt raised. The
        drainer task (``_drain_pending_records``) walks this queue with
        exponential backoff until each entry either succeeds or hits the
        per-entry budget. ``_record_result_sync`` is idempotent (UPDATE by
        ``id``), so retrying is always safe.

        Logs a backlog warning past ``_RECORD_RESULT_BACKLOG_THRESHOLD`` so a
        sustained Turso outage shows up in observability without an
        operator having to dig through DB metrics.
        """
        self._pending_records.put_nowait((queued_id, result, 0, 0.0))
        qsize = self._pending_records.qsize()
        _log.warning(
            "runner_record_result_failed_buffered",
            queued_id=queued_id,
            row_num=result.row_num,
            status=result.status,
            queue_size_after=qsize,
            reason=reason,
        )
        if qsize >= self._next_backlog_warn_at:
            _log.warning(
                "runner_pending_records_backlog",
                pending=qsize,
                threshold=_RECORD_RESULT_BACKLOG_THRESHOLD,
            )
            self._next_backlog_warn_at = (
                qsize + _RECORD_RESULT_BACKLOG_LOG_EVERY
            )

    async def _drain_pending_records(self) -> None:
        """Background loop. Pull failed record_result entries and retry
        with exponential backoff until each lands or its per-entry budget
        is exhausted. Returns when shutdown is set AND the queue is empty.

        Backoff schedule per attempt: 1 s → 2 s → 4 s → 8 s → 16 s →
        ``_RECORD_RESULT_RETRY_MAX_BACKOFF_SECONDS`` cap (30 s). During
        shutdown we skip backoff entirely AND give up immediately on any
        retry failure — the bounded drain window in ``run()``'s finally
        block would preempt a stuck retry anyway, so logging the
        give-up now gives observability complete provenance.

        ``_record_result_sync`` is idempotent (UPDATE by row id), so any
        previously-partial attempt is safe to repeat. Plan
        ``_plans/2026-06-14-stuck-processing-rows.md`` §A.
        """
        while True:
            if self._shutdown.is_set() and self._pending_records.empty():
                return
            try:
                entry = await asyncio.wait_for(
                    self._pending_records.get(), timeout=0.25,
                )
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                # Honor cancellation from the outer shutdown drain budget.
                raise
            except Exception as e:    # noqa: BLE001 — drainer must keep running
                _log.exception("runner_pending_drainer_loop_error", error=str(e))
                # Tiny breather so a pathological get() failure doesn't
                # busy-loop the drainer at 100% CPU.
                await asyncio.sleep(0.5)
                continue
            queued_id, result, attempts, total_wait = entry

            # Backoff between attempts. attempts==0 means this is the
            # first try (no prior backoff yet) — attempt immediately.
            # During shutdown we skip backoff so the drain window isn't
            # wasted on sleeps.
            if attempts > 0 and not self._shutdown.is_set():
                backoff = min(
                    2.0 ** (attempts - 1),
                    _RECORD_RESULT_RETRY_MAX_BACKOFF_SECONDS,
                )
                # Honor shutdown promptly even mid-backoff.
                try:
                    await asyncio.wait_for(
                        self._shutdown.wait(), timeout=backoff,
                    )
                except TimeoutError:
                    pass
                total_wait += backoff

            attempts += 1
            try:
                await asyncio.wait_for(
                    self._queue.record_result(queued_id, result),
                    timeout=_WORKER_QUERY_TIMEOUT_SECONDS,
                )
                _log.info(
                    "runner_record_result_recovered",
                    queued_id=queued_id,
                    row_num=result.row_num,
                    attempts=attempts,
                    total_wait_s=round(total_wait, 1),
                )
            except Exception as e:    # noqa: BLE001 — drainer must keep running
                # Give up when the per-entry budget is exhausted OR we're
                # shutting down (re-enqueueing during shutdown risks
                # spinning forever inside the bounded drain window).
                shutting_down = self._shutdown.is_set()
                if (
                    total_wait >= _RECORD_RESULT_RETRY_MAX_SECONDS
                    or shutting_down
                ):
                    _log.error(
                        "runner_pending_record_giveup",
                        queued_id=queued_id,
                        row_num=result.row_num,
                        status=result.status,
                        video_urls=list(result.video_urls),
                        error=result.error,
                        attempts=attempts,
                        total_wait_s=round(total_wait, 1),
                        last_error=f"{type(e).__name__}: {e!s}"[:200],
                        shutdown=shutting_down,
                    )
                    continue
                # Re-enqueue for the next round.
                self._pending_records.put_nowait(
                    (queued_id, result, attempts, total_wait)
                )

    # ── Test hooks ──────────────────────────────────────────────────────────

    @property
    def in_flight_count(self) -> int:
        return len(self._in_flight)

    @property
    def pending_records_count(self) -> int:
        """Live size of the pending record_result retry queue. Used by the
        admin panel + tests to monitor a Turso outage's blast radius."""
        return self._pending_records.qsize()
