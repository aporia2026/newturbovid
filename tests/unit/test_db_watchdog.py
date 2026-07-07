"""DB-pool wedge tracking + watchdog (Plan 2026-07-07 §Phase 1).

Covers the pieces that make the "restart the HF Space" wedge self-heal:
  * ``_tracked_call`` records a running body and clears it on return/raise.
  * A body that never returns (simulated wedged libsql call) stays visible —
    the fault-injection harness the council asked for, since the real bug only
    reproduces under a genuine Turso flap.
  * ``db_pool_stats`` counts running vs wedged correctly.
  * ``_watch`` force-exits on a sustained FULL wedge and NOT on a healthy /
    partial-wedge pool.
  * ``start_db_wedge_watchdog`` is idempotent and honours the env kill switch.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from bulkvid.orchestrator import db, db_watchdog


@pytest.fixture(autouse=True)
def _clean_inflight():
    """Every test starts with an empty in-flight registry and a fresh watchdog
    'started' flag so ordering can't leak state between cases."""
    with db._inflight_lock:
        db._inflight.clear()
    db_watchdog._started = False
    yield
    with db._inflight_lock:
        db._inflight.clear()
    db_watchdog._started = False


# ── _tracked_call bookkeeping ────────────────────────────────────────────────


def test_tracked_call_returns_value_and_clears_on_success():
    result = db._tracked_call(1, lambda x: x + 1, (41,), {})
    assert result == 42
    assert db._inflight == {}


def test_tracked_call_clears_on_exception():
    def boom() -> None:
        raise ValueError("db blew up")

    with pytest.raises(ValueError, match="db blew up"):
        db._tracked_call(7, boom, (), {})
    assert db._inflight == {}


def test_tracked_call_forwards_kwargs():
    result = db._tracked_call(2, lambda a, b=0: a * b, (6,), {"b": 7})
    assert result == 42


# ── db_pool_stats ────────────────────────────────────────────────────────────


def test_db_pool_stats_empty():
    stats = db.db_pool_stats()
    assert stats["running"] == 0
    assert stats["wedged"] == 0
    assert stats["pool_size"] >= 1


def test_db_pool_stats_counts_running_and_wedged(monkeypatch):
    monkeypatch.setattr(db, "_DB_WEDGE_SECONDS", 60.0)
    now = time.monotonic()
    with db._inflight_lock:
        db._inflight[1] = now             # fresh — running, not wedged
        db._inflight[2] = now - 61.0      # older than threshold — wedged
        db._inflight[3] = now - 5.0       # running, not wedged
    stats = db.db_pool_stats()
    assert stats["running"] == 3
    assert stats["wedged"] == 1


# ── Fault-injection harness: a body that never returns stays visible ─────────


def test_wedged_call_stays_visible_until_it_returns(monkeypatch):
    """Simulate a libsql call stuck on a dead socket: it never returns, so its
    ``_tracked_call`` entry persists and crosses the wedge threshold. This is
    the exact signal the watchdog trips on — validated without a real flap."""
    monkeypatch.setattr(db, "_DB_WEDGE_SECONDS", 0.05)
    gate = threading.Event()
    t = threading.Thread(
        target=db._tracked_call, args=(1, gate.wait, (), {}), daemon=True
    )
    t.start()
    try:
        # Wait until the body has registered itself as running.
        for _ in range(200):
            if db.db_pool_stats()["running"] >= 1:
                break
            time.sleep(0.005)
        assert db.db_pool_stats()["running"] == 1

        # After the wedge threshold elapses it counts as wedged.
        time.sleep(0.08)
        stats = db.db_pool_stats()
        assert stats["running"] == 1
        assert stats["wedged"] == 1
    finally:
        gate.set()
        t.join(timeout=2)
    # Once it returns, it's gone from the registry.
    assert db.db_pool_stats()["running"] == 0


# ── _watch trip behaviour ────────────────────────────────────────────────────


def test_watch_exits_on_sustained_full_wedge(monkeypatch):
    monkeypatch.setattr(db_watchdog, "_WEDGE_CONFIRM_CHECKS", 2)
    # Bound the loop so a logic bug fails the test instead of hanging.
    ticks = {"n": 0}

    def _fake_sleep(_seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] > 20:
            raise AssertionError("watchdog did not exit within 20 checks")

    monkeypatch.setattr(db_watchdog.time, "sleep", _fake_sleep)
    monkeypatch.setattr(
        db_watchdog.db,
        "db_pool_stats",
        lambda: {"pool_size": 4, "running": 4, "wedged": 4},
    )
    exit_mock = MagicMock()
    monkeypatch.setattr(db_watchdog, "_exit_process", exit_mock)

    db_watchdog._watch("test")    # returns after the patched exit

    exit_mock.assert_called_once()
    # Tripped on the 2nd consecutive full-wedge observation, not the 1st.
    assert ticks["n"] == 2


def test_watch_does_not_exit_when_healthy(monkeypatch):
    class _Stop(Exception):
        pass

    calls = {"n": 0}

    def _sleep_then_stop(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] >= 5:
            raise _Stop

    monkeypatch.setattr(db_watchdog.time, "sleep", _sleep_then_stop)
    monkeypatch.setattr(
        db_watchdog.db,
        "db_pool_stats",
        lambda: {"pool_size": 4, "running": 1, "wedged": 0},
    )
    exit_mock = MagicMock()
    monkeypatch.setattr(db_watchdog, "_exit_process", exit_mock)

    with pytest.raises(_Stop):
        db_watchdog._watch("test")
    exit_mock.assert_not_called()


def test_watch_does_not_exit_on_partial_wedge(monkeypatch):
    """3 of 4 threads wedged is bad but the pool can still serve — do not
    restart on a partial wedge (would false-positive on a merely busy pool)."""

    class _Stop(Exception):
        pass

    calls = {"n": 0}

    def _sleep_then_stop(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] >= 5:
            raise _Stop

    monkeypatch.setattr(db_watchdog.time, "sleep", _sleep_then_stop)
    monkeypatch.setattr(
        db_watchdog.db,
        "db_pool_stats",
        lambda: {"pool_size": 4, "running": 4, "wedged": 3},
    )
    exit_mock = MagicMock()
    monkeypatch.setattr(db_watchdog, "_exit_process", exit_mock)

    with pytest.raises(_Stop):
        db_watchdog._watch("test")
    exit_mock.assert_not_called()


def test_watch_resets_on_recovery_before_confirm(monkeypatch):
    """A single full-wedge blip that recovers before the confirm window must
    NOT trip — the consecutive counter resets on any healthy observation."""
    monkeypatch.setattr(db_watchdog, "_WEDGE_CONFIRM_CHECKS", 2)

    class _Stop(Exception):
        pass

    # wedged, then healthy (reset), then wedged once — never 2 in a row.
    seq = iter(
        [
            {"pool_size": 4, "running": 4, "wedged": 4},
            {"pool_size": 4, "running": 1, "wedged": 0},
            {"pool_size": 4, "running": 4, "wedged": 4},
        ]
    )

    def _stats():
        try:
            return next(seq)
        except StopIteration:
            return {"pool_size": 4, "running": 0, "wedged": 0}

    calls = {"n": 0}

    def _sleep_then_stop(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] >= 6:
            raise _Stop

    monkeypatch.setattr(db_watchdog.time, "sleep", _sleep_then_stop)
    monkeypatch.setattr(db_watchdog.db, "db_pool_stats", _stats)
    exit_mock = MagicMock()
    monkeypatch.setattr(db_watchdog, "_exit_process", exit_mock)

    with pytest.raises(_Stop):
        db_watchdog._watch("test")
    exit_mock.assert_not_called()


# ── start_db_wedge_watchdog ──────────────────────────────────────────────────


def test_start_is_idempotent(monkeypatch):
    monkeypatch.setenv("BULKVID_DB_WEDGE_WATCHDOG_ENABLED", "1")
    monkeypatch.setattr(db_watchdog, "_watch", lambda _label: None)
    assert db_watchdog.start_db_wedge_watchdog("web") is True
    assert db_watchdog.start_db_wedge_watchdog("web") is False


def test_start_respects_kill_switch(monkeypatch):
    monkeypatch.setenv("BULKVID_DB_WEDGE_WATCHDOG_ENABLED", "0")
    monkeypatch.setattr(db_watchdog, "_watch", lambda _label: None)
    assert db_watchdog.start_db_wedge_watchdog("worker") is False


def test_start_enabled_by_default(monkeypatch):
    monkeypatch.delenv("BULKVID_DB_WEDGE_WATCHDOG_ENABLED", raising=False)
    monkeypatch.setattr(db_watchdog, "_watch", lambda _label: None)
    assert db_watchdog.start_db_wedge_watchdog("web") is True
