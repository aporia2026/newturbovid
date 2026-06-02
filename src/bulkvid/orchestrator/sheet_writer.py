"""Coalesced sheet writer.

Buffers per-row write-backs and flushes them as a single batch every
``flush_interval_seconds`` (default 5s, plan §9 ``BULKVID_SHEET_BATCH_WRITES``).

Decouples the runner from the actual Google Sheets adapter: the Phase 4
sheet adapter injects ``flush_callback``. This module just owns the
buffer + cadence + graceful shutdown.

Usage from the worker:
    writer = CoalescedSheetWriter(flush_callback=sheets.batch_write)
    runner = BatchRunner(queue, clients, write_back=writer.submit_from_runner)
    await asyncio.gather(runner.run(), writer.run())

``PendingWrite`` carries the full sheet routing info (sheet_id, worksheet,
tab_type) so the flush_callback can group by destination without an extra
queue lookup.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from bulkvid.logging import get_logger
from bulkvid.models.row import RowResult

_log = get_logger("sheetwriter")


@dataclass
class PendingWrite:
    job_id: str
    sheet_id: str
    worksheet: str
    tab_type: str
    row_num: int
    video_urls: list[str]
    status: str
    error: str | None


FlushCallback = Callable[[list[PendingWrite]], Awaitable[None]]


class CoalescedSheetWriter:
    """In-memory buffer + periodic flush."""

    def __init__(
        self,
        flush_callback: FlushCallback,
        *,
        flush_interval_seconds: float = 5.0,
    ) -> None:
        if flush_interval_seconds <= 0:
            raise ValueError("flush_interval_seconds must be > 0")
        self._cb = flush_callback
        self._interval = flush_interval_seconds
        self._buffer: list[PendingWrite] = []
        self._lock = asyncio.Lock()
        self._shutdown = asyncio.Event()

    async def submit(self, write: PendingWrite) -> None:
        """Queue a fully-routed write. Called by the runner."""
        async with self._lock:
            self._buffer.append(write)

    async def flush(self) -> int:
        """Flush whatever's in the buffer to the callback. Returns count written."""
        async with self._lock:
            if not self._buffer:
                return 0
            pending = self._buffer
            self._buffer = []

        _log.info("sheet_flush_start", count=len(pending))
        try:
            await self._cb(pending)
            _log.info("sheet_flush_ok", count=len(pending))
            return len(pending)
        except Exception as e:
            # Re-queue at the front so the next flush retries them. This is
            # the conservative choice: better to duplicate-write than to drop.
            _log.error("sheet_flush_failed", count=len(pending), error=str(e)[:200])
            async with self._lock:
                self._buffer = pending + self._buffer
            return 0

    def request_shutdown(self) -> None:
        _log.info("sheet_writer_shutdown_requested")
        self._shutdown.set()

    async def run(self) -> None:
        """Periodic flush loop. Returns after a final flush on shutdown."""
        _log.info("sheet_writer_start", interval_seconds=self._interval)
        try:
            while not self._shutdown.is_set():
                try:
                    await asyncio.wait_for(
                        self._shutdown.wait(), timeout=self._interval
                    )
                except asyncio.TimeoutError:
                    pass
                await self.flush()
        finally:
            # Final drain.
            remaining = await self.flush()
            _log.info("sheet_writer_stop", final_flushed=remaining)

    @property
    def buffer_size(self) -> int:
        return len(self._buffer)
