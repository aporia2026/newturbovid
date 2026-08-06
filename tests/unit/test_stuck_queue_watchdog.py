"""Stuck-queue restart watchdog (Plan 2026-08-06).

Covers the pieces that automate the manual "restart the Space":
  * ``_probe_time_boxed`` returns the probe result, propagates its error, and
    raises ``TimeoutError`` when the probe overruns (Turso unresponsive).
  * ``_watch`` force-exits ONLY on a sustained, independently-proven
    stuck-but-idle queue (``pending>0 && processing==0`` past the threshold).
  * ``_watch`` does NOT exit before the threshold, when rows are in flight, when
    the queue is empty, when the probe fails, or when a stuck blip recovers.
  * ``start_stuck_queue_watchdog`` is idempotent, honours the env kill switch,
    and no-ops without a ``sync_url`` (local sqlite backend).

All ``_watch`` tests drive a fake monotonic clock (advanced by the patched
``time.sleep``) so elapsed-time logic is exercised deterministically without real
waiting — the same shape as ``test_db_watchdog.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bulkvid.orchestrator import stuck_queue_watchdog as swd


@pytest.fixture(autouse=True)
def _reset_started():
    """Each test starts with a fresh 'started' flag so ordering can't leak the
    idempotency latch between cases."""
    swd._started = False
    yield
    swd._started = False


class _Stop(Exception):
    """Sentinel raised by a patched ``sleep`` to break the infinite loop in the
    non-firing tests."""


def _drive(monkeypatch, *, probe, threshold, interval=30.0, max_ticks=40):
    """Patch the watchdog's clock + probe and return (ticks, exit_mock).

    ``probe`` is a zero-arg callable invoked in place of ``_probe_time_boxed``
    each poll (return ``(pending, processing)`` or raise). ``sleep`` advances a
    fake monotonic clock by ``interval`` per tick and stops the loop after
    ``max_ticks`` so a logic bug fails loudly instead of hanging."""
    clock = {"t": 1000.0}
    ticks = {"n": 0}

    def _fake_sleep(_seconds: float) -> None:
        ticks["n"] += 1
        clock["t"] += interval
        if ticks["n"] > max_ticks:
            raise _Stop

    monkeypatch.setattr(swd, "_STUCK_SECONDS", threshold)
    monkeypatch.setattr(swd, "_CHECK_INTERVAL_SECONDS", interval)
    monkeypatch.setattr(swd.time, "sleep", _fake_sleep)
    monkeypatch.setattr(swd.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(swd, "_probe_time_boxed", lambda *a, **k: probe())
    exit_mock = MagicMock()
    monkeypatch.setattr(swd, "_exit_process", exit_mock)
    return ticks, exit_mock


# ── _probe_time_boxed ────────────────────────────────────────────────────────


def test_probe_time_boxed_returns_result(monkeypatch):
    monkeypatch.setattr(
        swd, "_probe_active_queue", lambda *a, **k: (7, 0)
    )
    assert swd._probe_time_boxed("p", "url", "tok", 1.0) == (7, 0)


def test_probe_time_boxed_propagates_error(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("turso unreachable")

    monkeypatch.setattr(swd, "_probe_active_queue", _boom)
    with pytest.raises(RuntimeError, match="turso unreachable"):
        swd._probe_time_boxed("p", "url", "tok", 1.0)


def test_probe_time_boxed_times_out_on_hung_probe(monkeypatch):
    import threading

    monkeypatch.setattr(swd, "_PROBE_TIMEOUT_SECONDS", 0.1)
    gate = threading.Event()

    def _hang(*_a, **_k):
        gate.wait()    # never returns until released — simulates a wedged libsql call
        return (1, 0)

    monkeypatch.setattr(swd, "_probe_active_queue", _hang)
    try:
        with pytest.raises(TimeoutError):
            swd._probe_time_boxed("p", "url", "tok", 1.0)
    finally:
        gate.set()    # let the abandoned probe thread finish


# ── _watch: fires ────────────────────────────────────────────────────────────


def test_watch_exits_on_sustained_stuck(monkeypatch):
    # threshold 100s, +30s per tick: stuck_since set tick1 (elapsed 0), fires
    # when elapsed >= 100 -> tick 5 (elapsed 120).
    ticks, exit_mock = _drive(
        monkeypatch, probe=lambda: (5, 0), threshold=100.0, interval=30.0
    )
    swd._watch("p", "url", "tok", 1.0)    # returns after the patched exit
    exit_mock.assert_called_once()
    assert ticks["n"] == 5


# ── _watch: does not fire ────────────────────────────────────────────────────


def test_watch_does_not_exit_before_threshold(monkeypatch):
    # Stuck the whole time but the loop is stopped (max_ticks=3 => 90s elapsed)
    # before the 240s threshold — must not fire.
    _ticks, exit_mock = _drive(
        monkeypatch, probe=lambda: (5, 0), threshold=240.0, interval=30.0,
        max_ticks=3,
    )
    with pytest.raises(_Stop):
        swd._watch("p", "url", "tok", 1.0)
    exit_mock.assert_not_called()


def test_watch_does_not_exit_when_rows_in_flight(monkeypatch):
    # pending>0 but processing>0 (worker is busy / at its cap) — idle-only gate
    # means this is never "stuck", no matter how long it persists.
    _ticks, exit_mock = _drive(
        monkeypatch, probe=lambda: (5, 3), threshold=100.0, interval=30.0,
        max_ticks=10,
    )
    with pytest.raises(_Stop):
        swd._watch("p", "url", "tok", 1.0)
    exit_mock.assert_not_called()


def test_watch_does_not_exit_when_queue_empty(monkeypatch):
    _ticks, exit_mock = _drive(
        monkeypatch, probe=lambda: (0, 0), threshold=100.0, interval=30.0,
        max_ticks=10,
    )
    with pytest.raises(_Stop):
        swd._watch("p", "url", "tok", 1.0)
    exit_mock.assert_not_called()


def test_watch_does_not_exit_when_probe_fails(monkeypatch):
    # A failing probe = Turso unreachable = can't confirm a backlog. A restart
    # wouldn't help a down DB, so never fire.
    def _boom():
        raise RuntimeError("probe failed")

    _ticks, exit_mock = _drive(
        monkeypatch, probe=_boom, threshold=100.0, interval=30.0, max_ticks=10,
    )
    with pytest.raises(_Stop):
        swd._watch("p", "url", "tok", 1.0)
    exit_mock.assert_not_called()


def test_watch_resets_streak_on_recovery(monkeypatch):
    # stuck, stuck, healthy (reset), then stuck forever after — but each reset
    # restarts the clock, and the loop is stopped before a fresh full streak
    # accumulates. A naive cumulative counter would wrongly fire; the monotonic
    # first-seen reset must not.
    seq = iter([(5, 0), (5, 0), (0, 0)])

    def _probe():
        try:
            return next(seq)
        except StopIteration:
            return (5, 0)    # stuck again, but the streak restarts here

    # threshold 100s, interval 30s. Stuck streak restarts at tick 4 (after the
    # healthy tick 3). Stop at tick 6 => only 60s into the new streak (<100).
    _ticks, exit_mock = _drive(
        monkeypatch, probe=_probe, threshold=100.0, interval=30.0, max_ticks=6,
    )
    with pytest.raises(_Stop):
        swd._watch("p", "url", "tok", 1.0)
    exit_mock.assert_not_called()


def test_watch_resets_streak_on_probe_error(monkeypatch):
    # stuck, stuck, error (reset), then stuck — the error breaks the streak the
    # same way a healthy poll does, so a fresh full threshold is required.
    seq = iter([(5, 0), (5, 0), "ERR"])

    def _probe():
        try:
            val = next(seq)
        except StopIteration:
            return (5, 0)
        if val == "ERR":
            raise RuntimeError("blip")
        return val

    _ticks, exit_mock = _drive(
        monkeypatch, probe=_probe, threshold=100.0, interval=30.0, max_ticks=6,
    )
    with pytest.raises(_Stop):
        swd._watch("p", "url", "tok", 1.0)
    exit_mock.assert_not_called()


# ── start_stuck_queue_watchdog ───────────────────────────────────────────────


def test_start_is_idempotent(monkeypatch):
    monkeypatch.setenv("BULKVID_STUCK_QUEUE_WATCHDOG_ENABLED", "1")
    monkeypatch.setattr(swd, "_watch", lambda *a, **k: None)
    kwargs = dict(
        db_path="jobs.db", sync_url="libsql://x", auth_token="tok",
        sync_interval_seconds=1.0,
    )
    assert swd.start_stuck_queue_watchdog(**kwargs) is True
    assert swd.start_stuck_queue_watchdog(**kwargs) is False


def test_start_respects_kill_switch(monkeypatch):
    monkeypatch.setenv("BULKVID_STUCK_QUEUE_WATCHDOG_ENABLED", "0")
    monkeypatch.setattr(swd, "_watch", lambda *a, **k: None)
    assert (
        swd.start_stuck_queue_watchdog(
            db_path="jobs.db", sync_url="libsql://x", auth_token="tok",
            sync_interval_seconds=1.0,
        )
        is False
    )


def test_start_skips_without_sync_url(monkeypatch):
    # Local sqlite backend: there is no remote stale-read to guard against, and
    # reopening a local file every poll would be pointless churn.
    monkeypatch.delenv("BULKVID_STUCK_QUEUE_WATCHDOG_ENABLED", raising=False)
    monkeypatch.setattr(swd, "_watch", lambda *a, **k: None)
    assert (
        swd.start_stuck_queue_watchdog(
            db_path="jobs.db", sync_url="", auth_token="",
            sync_interval_seconds=1.0,
        )
        is False
    )


def test_start_enabled_by_default(monkeypatch):
    monkeypatch.delenv("BULKVID_STUCK_QUEUE_WATCHDOG_ENABLED", raising=False)
    monkeypatch.setattr(swd, "_watch", lambda *a, **k: None)
    assert (
        swd.start_stuck_queue_watchdog(
            db_path="jobs.db", sync_url="libsql://x", auth_token="tok",
            sync_interval_seconds=1.0,
        )
        is True
    )
