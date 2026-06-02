"""Tests for the coalesced sheet writer.

Covers:
  - submit accumulates into the buffer
  - flush empties the buffer and calls the callback with all pending
  - flush no-op when buffer is empty
  - flush failures re-queue at the front (no drops)
  - run loop calls flush periodically until shutdown
  - shutdown triggers a final flush
  - Constructor rejects bad flush interval
"""

from __future__ import annotations

import asyncio

import pytest

from bulkvid.models.row import STATUS_SUCCESS, RowResult
from bulkvid.orchestrator.sheet_writer import CoalescedSheetWriter, PendingWrite


def _result(row_num: int, status: str = STATUS_SUCCESS) -> RowResult:
    return RowResult(
        row_num=row_num,
        status=status,
        video_urls=[f"https://storage/{row_num}.mp4"],
        cost_usd=0.1,
    )


def _write(job_id: str, row_num: int) -> PendingWrite:
    return PendingWrite(
        job_id=job_id,
        sheet_id="sheet-X",
        worksheet="Image-VO",
        tab_type="image_vo",
        row_num=row_num,
        video_urls=[f"https://storage/{row_num}.mp4"],
        status=STATUS_SUCCESS,
        error=None,
    )


# ── Constructor ─────────────────────────────────────────────────────────────


async def test_constructor_rejects_non_positive_interval() -> None:
    async def _noop(_writes: list[PendingWrite]) -> None: ...
    with pytest.raises(ValueError):
        CoalescedSheetWriter(_noop, flush_interval_seconds=0)
    with pytest.raises(ValueError):
        CoalescedSheetWriter(_noop, flush_interval_seconds=-1)


# ── submit + flush basics ───────────────────────────────────────────────────


async def test_submit_accumulates_writes() -> None:
    async def _noop(_writes: list[PendingWrite]) -> None: ...

    w = CoalescedSheetWriter(_noop, flush_interval_seconds=5.0)
    await w.submit(_write("job-1", 2))
    await w.submit(_write("job-1", 3))
    await w.submit(_write("job-2", 2))
    assert w.buffer_size == 3


async def test_flush_calls_callback_and_clears_buffer() -> None:
    received: list[list[PendingWrite]] = []

    async def _cb(writes: list[PendingWrite]) -> None:
        received.append(writes)

    w = CoalescedSheetWriter(_cb, flush_interval_seconds=5.0)
    await w.submit(_write("job-1", 2))
    await w.submit(_write("job-1", 3))

    written = await w.flush()
    assert written == 2
    assert w.buffer_size == 0
    assert len(received) == 1
    assert {p.row_num for p in received[0]} == {2, 3}


async def test_flush_noop_when_empty() -> None:
    calls = 0

    async def _cb(_writes: list[PendingWrite]) -> None:
        nonlocal calls
        calls += 1

    w = CoalescedSheetWriter(_cb, flush_interval_seconds=5.0)
    written = await w.flush()
    assert written == 0
    assert calls == 0


# ── Failure handling ────────────────────────────────────────────────────────


async def test_flush_failure_requeues_writes_no_drops() -> None:
    raised_once = {"done": False}

    async def _cb(_writes: list[PendingWrite]) -> None:
        if not raised_once["done"]:
            raised_once["done"] = True
            raise RuntimeError("sheets API down")
        # second call succeeds

    w = CoalescedSheetWriter(_cb, flush_interval_seconds=5.0)
    await w.submit(_write("job-1", 2))
    await w.submit(_write("job-1", 3))

    # First flush fails — buffer is preserved.
    n1 = await w.flush()
    assert n1 == 0
    assert w.buffer_size == 2

    # Second flush succeeds.
    n2 = await w.flush()
    assert n2 == 2
    assert w.buffer_size == 0


async def test_flush_failure_preserves_order_with_subsequent_submits() -> None:
    raised_once = {"done": False}
    received: list[list[PendingWrite]] = []

    async def _cb(writes: list[PendingWrite]) -> None:
        if not raised_once["done"]:
            raised_once["done"] = True
            raise RuntimeError("first flush dies")
        received.append(writes)

    w = CoalescedSheetWriter(_cb, flush_interval_seconds=5.0)
    await w.submit(_write("job-1", 2))
    await w.submit(_write("job-1", 3))
    await w.flush()                          # fails, buffer = [2, 3]

    await w.submit(_write("job-1", 4))       # buffer = [2, 3, 4]
    await w.flush()                          # second flush succeeds

    assert len(received) == 1
    assert [p.row_num for p in received[0]] == [2, 3, 4]


# ── run() loop + shutdown ───────────────────────────────────────────────────


async def test_run_loop_flushes_periodically_until_shutdown() -> None:
    received: list[list[PendingWrite]] = []

    async def _cb(writes: list[PendingWrite]) -> None:
        received.append(writes)

    w = CoalescedSheetWriter(_cb, flush_interval_seconds=0.05)

    async def _feed_and_shutdown() -> None:
        await w.submit(_write("job-1", 2))
        await asyncio.sleep(0.08)              # let one tick run
        await w.submit(_write("job-1", 3))
        await asyncio.sleep(0.08)              # another tick
        w.request_shutdown()

    await asyncio.wait_for(
        asyncio.gather(w.run(), _feed_and_shutdown()),
        timeout=2.0,
    )

    # We saw at least 2 flushes — could be more depending on timing.
    flushed_row_nums = [p.row_num for batch in received for p in batch]
    assert 2 in flushed_row_nums
    assert 3 in flushed_row_nums


async def test_shutdown_triggers_final_flush() -> None:
    received: list[list[PendingWrite]] = []

    async def _cb(writes: list[PendingWrite]) -> None:
        received.append(writes)

    w = CoalescedSheetWriter(_cb, flush_interval_seconds=10.0)

    async def _submit_and_kill() -> None:
        await w.submit(_write("job-1", 99))
        await asyncio.sleep(0.02)
        w.request_shutdown()

    await asyncio.wait_for(
        asyncio.gather(w.run(), _submit_and_kill()),
        timeout=2.0,
    )

    # The final-flush path ran exactly once with our row 99.
    assert len(received) >= 1
    flushed_row_nums = {p.row_num for batch in received for p in batch}
    assert 99 in flushed_row_nums
