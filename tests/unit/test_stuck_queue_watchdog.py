"""Stuck-queue restart watchdog (Plans 2026-08-06 and 2026-08-12).

Covers the pieces that automate the manual "restart the Space":
  * ``_probe_time_boxed`` returns the probe result, propagates its error, and
    raises ``TimeoutError`` when the probe overruns (Turso unresponsive).
  * ``_watch`` force-exits on a sustained, independently-proven stuck-but-idle
    queue (``pending>0 && processing==0`` past the threshold) — condition 2.
  * ``_watch`` force-exits when the worker's heartbeat goes stale while work is
    waiting, INCLUDING mid-batch (``processing>0``) — condition 1, the universal
    net added after the 2026-08-12 freeze that every idle-only guard missed.
  * ``_watch`` does NOT exit before the threshold, when rows are in flight with a
    live heartbeat, when the queue is empty, when the probe fails, when a stuck
    blip recovers, when no heartbeat row exists, or before the uptime gate.
  * The lease sweep runs on its own cadence and never sinks the loop.
  * Every exit path captures forensics first.
  * ``start_stuck_queue_watchdog`` is idempotent, honours the env kill switch,
    and no-ops without a ``sync_url`` (local sqlite backend).

All ``_watch`` tests drive a fake monotonic clock (advanced by the patched
``time.sleep``) so elapsed-time logic is exercised deterministically without real
waiting — the same shape as ``test_db_watchdog.py``. Wall-clock ``time.time`` is
faked alongside it because heartbeat age is measured across processes.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bulkvid.orchestrator import stuck_queue_watchdog as swd
from bulkvid.orchestrator.queue import WorkerHeartbeat

# Wall-clock origin for the fake clocks. ``time.time`` and ``time.monotonic``
# advance together so a heartbeat epoch can be expressed as "now minus N".
_T0 = 1000.0


def _probe(pending, processing, *, heartbeat_age=0.0, now=_T0):
    """Build a ``_Probe`` whose heartbeat is ``heartbeat_age`` seconds old.
    ``heartbeat_age=None`` means no worker has ever beaten."""
    beat = (
        None
        if heartbeat_age is None
        else WorkerHeartbeat(
            epoch=now - heartbeat_age, pid=123, in_flight=processing,
            updated_at="2026-08-12T08:00:00+00:00",
        )
    )
    return swd._Probe(pending=pending, processing=processing, heartbeat=beat)


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


def _drive(
    monkeypatch,
    *,
    probe,
    threshold,
    interval=30.0,
    max_ticks=40,
    heartbeat_stale=1e9,
    sweep_interval=1e9,
):
    """Patch the watchdog's clocks + probe and return (state, exit_mock).

    ``probe`` is a callable invoked in place of ``_probe_time_boxed`` each poll.
    It receives the current fake wall-clock time so a test can express a
    heartbeat age relative to "now", and returns a ``_Probe`` (or raises).

    ``sleep`` advances both fake clocks by ``interval`` per tick and stops the
    loop after ``max_ticks`` so a logic bug fails loudly instead of hanging.

    ``heartbeat_stale`` and ``sweep_interval`` default to effectively-infinite so
    each test opts in to exactly the condition it is exercising."""
    state = {"t": _T0, "n": 0, "sweeps": 0}

    def _fake_sleep(_seconds: float) -> None:
        state["n"] += 1
        state["t"] += interval
        if state["n"] > max_ticks:
            raise _Stop

    def _fake_sweep(*_a, **_k) -> int:
        state["sweeps"] += 1
        return 0

    monkeypatch.setattr(swd, "_STUCK_SECONDS", threshold)
    monkeypatch.setattr(swd, "_HEARTBEAT_STALE_SECONDS", heartbeat_stale)
    monkeypatch.setattr(swd, "_SWEEP_INTERVAL_SECONDS", sweep_interval)
    monkeypatch.setattr(swd, "_CHECK_INTERVAL_SECONDS", interval)
    monkeypatch.setattr(swd.time, "sleep", _fake_sleep)
    monkeypatch.setattr(swd.time, "monotonic", lambda: state["t"])
    monkeypatch.setattr(swd.time, "time", lambda: state["t"])
    monkeypatch.setattr(swd, "_probe_time_boxed", lambda *a, **k: probe(state["t"]))
    monkeypatch.setattr(swd, "_sweep_time_boxed", _fake_sweep)
    # Forensics hits the DB and stderr; every _watch test wants the exit
    # decision, not the autopsy plumbing (which has its own tests below).
    monkeypatch.setattr(swd, "_write_forensics", lambda *a, **k: None)
    monkeypatch.setattr(swd.forensics, "dump_to_stderr", lambda *a, **k: None)
    exit_mock = MagicMock()
    monkeypatch.setattr(swd, "_exit_process", exit_mock)
    return state, exit_mock


# ── _probe_time_boxed ────────────────────────────────────────────────────────


def test_probe_time_boxed_returns_result(monkeypatch):
    monkeypatch.setattr(
        swd, "_probe_active_queue", lambda *a, **k: _probe(7, 0)
    )
    got = swd._probe_time_boxed("p", "url", "tok", 1.0)
    assert (got.pending, got.processing) == (7, 0)
    assert got.heartbeat is not None


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
        return _probe(1, 0)

    monkeypatch.setattr(swd, "_probe_active_queue", _hang)
    try:
        with pytest.raises(TimeoutError):
            swd._probe_time_boxed("p", "url", "tok", 1.0)
    finally:
        gate.set()    # let the abandoned probe thread finish


# ── _watch: condition 2 (stuck + idle) fires ─────────────────────────────────


def test_watch_exits_on_sustained_stuck(monkeypatch):
    # threshold 100s, +30s per tick: stuck_since set tick1 (elapsed 0), fires
    # when elapsed >= 100 -> tick 5 (elapsed 120).
    state, exit_mock = _drive(
        monkeypatch, probe=lambda now: _probe(5, 0, now=now),
        threshold=100.0, interval=30.0,
    )
    swd._watch("p", "url", "tok", 1.0)    # returns after the patched exit
    exit_mock.assert_called_once()
    assert state["n"] == 5


# ── _watch: condition 1 (stale heartbeat) fires ──────────────────────────────


def test_watch_exits_on_stale_heartbeat_mid_batch(monkeypatch):
    # THE 2026-08-12 REGRESSION: rows frozen in flight (processing=3) with a
    # backlog behind them. The idle-only condition can NEVER fire here, so this
    # asserts the universal net. Heartbeat epoch is frozen at boot, so its age
    # tracks uptime: both cross the 100s threshold at tick 4 (t=1120).
    state, exit_mock = _drive(
        monkeypatch,
        probe=lambda now: _probe(109, 3, heartbeat_age=now - _T0, now=now),
        threshold=1e9,            # idle-only condition disabled
        heartbeat_stale=100.0,
        interval=30.0,
    )
    swd._watch("p", "url", "tok", 1.0)
    exit_mock.assert_called_once()
    assert state["n"] == 4


def test_watch_exits_on_stale_heartbeat_with_only_pending_work(monkeypatch):
    # Same net with work waiting but nothing claimed — a worker that died before
    # it could claim. ``work_waiting`` is pending>0 OR processing>0.
    _state, exit_mock = _drive(
        monkeypatch,
        probe=lambda now: _probe(7, 0, heartbeat_age=now - _T0, now=now),
        threshold=1e9,
        heartbeat_stale=100.0,
        interval=30.0,
    )
    swd._watch("p", "url", "tok", 1.0)
    exit_mock.assert_called_once()


# ── _watch: does not fire ────────────────────────────────────────────────────


def test_watch_does_not_exit_before_threshold(monkeypatch):
    # Stuck the whole time but the loop is stopped (max_ticks=3 => 90s elapsed)
    # before the 240s threshold — must not fire.
    _state, exit_mock = _drive(
        monkeypatch, probe=lambda now: _probe(5, 0, now=now),
        threshold=240.0, interval=30.0, max_ticks=3,
    )
    with pytest.raises(_Stop):
        swd._watch("p", "url", "tok", 1.0)
    exit_mock.assert_not_called()


def test_watch_does_not_exit_when_rows_in_flight(monkeypatch):
    # pending>0 but processing>0 (worker is busy / at its cap) with a LIVE
    # heartbeat — the worker is genuinely working, so neither condition fires no
    # matter how long it persists. This is the false-positive the council
    # flagged: a legitimate batch of slow renders must never be killed.
    _state, exit_mock = _drive(
        monkeypatch, probe=lambda now: _probe(5, 3, now=now),
        threshold=100.0, heartbeat_stale=100.0, interval=30.0, max_ticks=10,
    )
    with pytest.raises(_Stop):
        swd._watch("p", "url", "tok", 1.0)
    exit_mock.assert_not_called()


def test_watch_does_not_exit_when_queue_empty(monkeypatch):
    # Stale heartbeat but NO work waiting: restarting would churn for nothing,
    # and the moment work arrives the next poll fires.
    _state, exit_mock = _drive(
        monkeypatch,
        probe=lambda now: _probe(0, 0, heartbeat_age=now - _T0, now=now),
        threshold=100.0, heartbeat_stale=100.0, interval=30.0, max_ticks=10,
    )
    with pytest.raises(_Stop):
        swd._watch("p", "url", "tok", 1.0)
    exit_mock.assert_not_called()


def test_watch_does_not_exit_without_heartbeat_row(monkeypatch):
    # No beat has ever landed (old worker build / fresh DB). An ABSENT row is
    # not evidence of a wedge, so the heartbeat condition must stay inert.
    _state, exit_mock = _drive(
        monkeypatch,
        probe=lambda now: _probe(9, 4, heartbeat_age=None, now=now),
        threshold=1e9, heartbeat_stale=100.0, interval=30.0, max_ticks=10,
    )
    with pytest.raises(_Stop):
        swd._watch("p", "url", "tok", 1.0)
    exit_mock.assert_not_called()


def test_watch_uptime_gate_suppresses_early_heartbeat_exit(monkeypatch):
    # Right after boot the row may still hold the PREVIOUS process's epoch. The
    # heartbeat reads as ancient (500s) but uptime only reaches 90s before the
    # loop stops, so a healthy fresh worker is never restarted.
    _state, exit_mock = _drive(
        monkeypatch,
        probe=lambda now: _probe(9, 4, heartbeat_age=500.0, now=now),
        threshold=1e9, heartbeat_stale=100.0, interval=30.0, max_ticks=3,
    )
    with pytest.raises(_Stop):
        swd._watch("p", "url", "tok", 1.0)
    exit_mock.assert_not_called()


def test_watch_does_not_exit_when_probe_fails(monkeypatch):
    # A failing probe = Turso unreachable = can't confirm a backlog. A restart
    # wouldn't help a down DB, so never fire.
    def _boom(_now):
        raise RuntimeError("probe failed")

    _state, exit_mock = _drive(
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

    def _seq_probe(now):
        try:
            pending, processing = next(seq)
        except StopIteration:
            pending, processing = 5, 0    # stuck again; the streak restarts here
        return _probe(pending, processing, now=now)

    # threshold 100s, interval 30s. Stuck streak restarts at tick 4 (after the
    # healthy tick 3). Stop at tick 6 => only 60s into the new streak (<100).
    _state, exit_mock = _drive(
        monkeypatch, probe=_seq_probe, threshold=100.0, interval=30.0,
        max_ticks=6,
    )
    with pytest.raises(_Stop):
        swd._watch("p", "url", "tok", 1.0)
    exit_mock.assert_not_called()


def test_watch_resets_streak_on_probe_error(monkeypatch):
    # stuck, stuck, error (reset), then stuck — the error breaks the streak the
    # same way a healthy poll does, so a fresh full threshold is required.
    seq = iter([(5, 0), (5, 0), "ERR"])

    def _seq_probe(now):
        try:
            val = next(seq)
        except StopIteration:
            return _probe(5, 0, now=now)
        if val == "ERR":
            raise RuntimeError("blip")
        return _probe(val[0], val[1], now=now)

    _state, exit_mock = _drive(
        monkeypatch, probe=_seq_probe, threshold=100.0, interval=30.0,
        max_ticks=6,
    )
    with pytest.raises(_Stop):
        swd._watch("p", "url", "tok", 1.0)
    exit_mock.assert_not_called()


# ── _watch: lease sweep ──────────────────────────────────────────────────────


def test_watch_sweeps_on_its_own_cadence(monkeypatch):
    # Sweep every 60s with 30s polls => one sweep every other tick, independent
    # of any restart decision. 6 ticks (180s) => 3 sweeps.
    state, exit_mock = _drive(
        monkeypatch, probe=lambda now: _probe(0, 0, now=now),
        threshold=1e9, sweep_interval=60.0, interval=30.0, max_ticks=6,
    )
    with pytest.raises(_Stop):
        swd._watch("p", "url", "tok", 1.0)
    assert state["sweeps"] == 3
    exit_mock.assert_not_called()


def test_watch_survives_sweep_failure(monkeypatch):
    # The janitor is best-effort: a failing sweep must not sink the loop or
    # block the restart conditions that follow it.
    state, exit_mock = _drive(
        monkeypatch, probe=lambda now: _probe(5, 0, now=now),
        threshold=100.0, sweep_interval=30.0, interval=30.0,
    )

    def _boom_sweep(*_a, **_k):
        raise RuntimeError("sweep failed")

    monkeypatch.setattr(swd, "_sweep_time_boxed", _boom_sweep)
    swd._watch("p", "url", "tok", 1.0)
    exit_mock.assert_called_once()    # condition 2 still fired
    assert state["n"] == 5


# ── _watch: forensics before every exit ──────────────────────────────────────


def test_watch_captures_forensics_before_exit(monkeypatch):
    state, exit_mock = _drive(
        monkeypatch,
        probe=lambda now: _probe(109, 3, heartbeat_age=now - _T0, now=now),
        threshold=1e9, heartbeat_stale=100.0, interval=30.0,
    )
    written = MagicMock()
    monkeypatch.setattr(swd, "_write_forensics", written)
    swd._watch("p", "url", "tok", 1.0)

    exit_mock.assert_called_once()
    written.assert_called_once()
    kwargs = written.call_args.kwargs
    assert kwargs["reason"] == "worker_heartbeat_stale"
    assert (kwargs["pending"], kwargs["processing"]) == (109, 3)
    assert kwargs["heartbeat_age_s"] >= 100.0
    assert "thread" in kwargs["stacks"]    # a real stack dump was captured
    assert state["n"] == 4


def test_watch_exits_even_when_forensics_write_fails(monkeypatch):
    # The autopsy is strictly best-effort — losing it must never cost us the
    # recovery it was documenting.
    _state, exit_mock = _drive(
        monkeypatch,
        probe=lambda now: _probe(109, 3, heartbeat_age=now - _T0, now=now),
        threshold=1e9, heartbeat_stale=100.0, interval=30.0,
    )

    def _boom(*_a, **_k):
        raise RuntimeError("turso down")

    monkeypatch.setattr(swd, "_write_forensics", _boom)
    swd._watch("p", "url", "tok", 1.0)
    exit_mock.assert_called_once()


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
