"""Stuck-queue restart watchdog — automate the manual "restart the Space".

Why this exists (Plan ``_plans/2026-08-06-worker-stuck-queue-restart-watchdog.md``):

Users still hit the "queued but nothing runs" wedge — the sidebar shows real rows
queued with 0 in flight and "Worker not claiming for Nm" — that the 2026-08-03
stale-read tripwire was meant to kill. The tripwire *detects* the wedge (recycle +
re-count) but has no escalation when its own cure, an **in-process** connection
recycle, fails to defeat it: if the fresh ``libsql.connect()`` is also stale, the
worker loops forever (reconnect → see backlog → log → idle → claim ``None`` →
repeat) and only a manual HF restart — a brand-new *process* with a genuinely fresh
libsql client — clears it.

The four existing self-healers all key off a specific FAILURE on the worker's own
connection (an exception, a hung thread, consecutive claim failures, or a stale read
the reconnect is trusted to cure). None keys off the ground-truth OUTCOME — "real
work is waiting and nothing is draining" — from an INDEPENDENT vantage point.

This watchdog does. A plain daemon thread — deliberately NOT on the event loop and
NOT on the shared DB pool/connection, so it can never wedge with them — opens its
OWN fresh short-lived connection every ``_CHECK_INTERVAL_SECONDS``, counts the
active queue, and ``os._exit``s (supervisord ``autorestart`` relaunches a clean
worker; the web process stays up) when it can INDEPENDENTLY PROVE, via a *successful*
fresh read, that ``pending > 0 AND processing == 0`` has held past
``_STUCK_SECONDS``. That is precisely the manual restart, automated.

The safety property that makes an auto-restart safe: it fires ONLY on a successful
fresh read. A stale worker connection (Turso reachable, worker's view wrong) →
fresh read succeeds, shows the backlog → restart, and it works. Turso genuinely down
(nobody can read) → fresh read fails → no restart (a restart can't help a down DB).
That discrimination is what prevents restart-hammering during an outage.

Idle-only (``processing == 0``) so no in-flight paid work is ever killed: a worker
at its concurrency cap has ``processing == max_concurrent > 0`` and a long-running
row has ``processing > 0``, so neither trips it. It is a backstop, not the cure —
the real cure (a transport-deadline async libsql client) stays on the roadmap.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import suppress
from pathlib import Path

from bulkvid.logging import get_logger
from bulkvid.orchestrator import db as _db
from bulkvid.orchestrator.queue import count_active_queue

_log = get_logger("stuck queue watchdog")


def _enabled() -> bool:
    """On by default — unattended recovery is the whole point. Env kill switch
    (``BULKVID_STUCK_QUEUE_WATCHDOG_ENABLED=0``) disables it without a code
    change."""
    raw = os.environ.get("BULKVID_STUCK_QUEUE_WATCHDOG_ENABLED")
    if raw is None or raw == "":
        return True
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _positive_float(env_name: str, default: float) -> float:
    """Read a positive float from ``env_name``; fall back to ``default`` on an
    empty/invalid/non-positive value (never crash the worker on a bad knob)."""
    raw = os.environ.get(env_name)
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return v if v > 0 else default


# How long a proven-stuck-but-idle queue must persist before we restart. Default
# 4 minutes: well past any real Turso flap (the stale-read reconnect heals those
# in seconds) yet fast enough that users rarely notice. Env-tunable.
_STUCK_SECONDS = _positive_float(
    "BULKVID_STUCK_QUEUE_WATCHDOG_THRESHOLD_SECONDS", 240.0
)
# Poll cadence. A fresh connection + two COUNTs is cheap; 30s keeps
# time-to-recovery a fraction of the threshold without meaningful cost.
_CHECK_INTERVAL_SECONDS = _positive_float(
    "BULKVID_STUCK_QUEUE_WATCHDOG_CHECK_INTERVAL_SECONDS", 30.0
)
# Per-probe wall-clock budget. A stale read is fast (it returns quickly with the
# wrong count); a probe that exceeds this is Turso being unresponsive — the
# don't-restart case — so a timeout is treated as a failed read, not a wedge.
_PROBE_TIMEOUT_SECONDS = _positive_float(
    "BULKVID_STUCK_QUEUE_WATCHDOG_PROBE_TIMEOUT_SECONDS", 15.0
)
# Non-zero so the exit reads as abnormal in supervisord logs; ``autorestart``
# relaunches regardless of the code. Matches both existing watchdogs.
_EXIT_CODE = 1

_started = False
_started_lock = threading.Lock()


def _exit_process() -> None:
    """Force-exit so supervisord relaunches a clean process. ``os._exit`` (not
    ``sys.exit``) bypasses the atexit/cleanup path — those handlers would
    themselves try to touch the (possibly wedged) DB and hang. Isolated so tests
    can patch it and assert the trip without killing the test runner."""
    os._exit(_EXIT_CODE)


def _probe_active_queue(
    db_path: Path | str,
    sync_url: str,
    auth_token: str,
    sync_interval_seconds: float,
) -> tuple[int, int]:
    """Open a FRESH short-lived connection, count the active queue, close it.

    A fresh connection is the whole point: it reads ground truth independent of
    the worker's possibly-stale long-lived connection (the wedge's signature is
    the two disagreeing — web sees a backlog the worker's connection hides).
    Raises on any failure so the caller treats an unreachable Turso as "cannot
    confirm a backlog" (no restart)."""
    conn = _db.connect(
        db_path,
        sync_url=sync_url,
        auth_token=auth_token,
        sync_interval_seconds=sync_interval_seconds,
        # Fresh connection every poll — don't flood the logs we use to diagnose
        # the wedge with a ``db_backend`` line every 30s.
        quiet=True,
    )
    try:
        return count_active_queue(conn)
    finally:
        # A half-dead handle must not mask the count result or the probe's raise.
        with suppress(Exception):
            conn.close()


def _probe_time_boxed(
    db_path: Path | str,
    sync_url: str,
    auth_token: str,
    sync_interval_seconds: float,
) -> tuple[int, int]:
    """Run ``_probe_active_queue`` in a throwaway thread joined with a timeout.

    libsql calls are uncancellable, so a probe against an unresponsive Turso
    could block forever. Running it in a daemon side-thread and joining with
    ``_PROBE_TIMEOUT_SECONDS`` lets the watchdog abandon a hung probe and stay
    alive (the abandoned thread is bounded — one per hung probe, and hung probes
    only happen when Turso is down, the don't-restart case). Re-raises the
    probe's own exception; raises ``TimeoutError`` when it overruns."""
    box: dict[str, object] = {}

    def _run() -> None:
        try:
            box["result"] = _probe_active_queue(
                db_path, sync_url, auth_token, sync_interval_seconds
            )
        except BaseException as e:    # carried back to the watchdog thread
            box["error"] = e

    t = threading.Thread(
        target=_run, name="bulkvid-stuckq-probe", daemon=True
    )
    t.start()
    t.join(timeout=_PROBE_TIMEOUT_SECONDS)
    if t.is_alive():
        raise TimeoutError(
            f"active-queue probe exceeded {_PROBE_TIMEOUT_SECONDS:.0f}s"
        )
    if "error" in box:
        raise box["error"]    # type: ignore[misc]
    return box["result"]    # type: ignore[return-value]


def _watch(
    db_path: Path | str,
    sync_url: str,
    auth_token: str,
    sync_interval_seconds: float,
) -> None:
    """Daemon-thread loop. Probes ground truth; exits on a confirmed, sustained
    stuck-but-idle queue.

    ``_stuck_since`` is the monotonic time the CURRENT uninterrupted stuck streak
    began, or ``None`` when the last poll was not-stuck (or could not be
    confirmed). Any healthy or unconfirmable poll resets it, so only a
    CONTINUOUS stretch past ``_STUCK_SECONDS`` fires."""
    stuck_since: float | None = None
    while True:
        time.sleep(_CHECK_INTERVAL_SECONDS)
        try:
            pending, processing = _probe_time_boxed(
                db_path, sync_url, auth_token, sync_interval_seconds
            )
        except Exception as e:
            # Cannot confirm a backlog (Turso unreachable / probe hung). A
            # restart wouldn't help a down DB, so reset the streak and wait.
            _log.warning("stuck_queue_probe_error", error=str(e)[:200])
            stuck_since = None
            continue

        is_stuck = pending > 0 and processing == 0
        if not is_stuck:
            stuck_since = None
            continue

        now = time.monotonic()
        if stuck_since is None:
            stuck_since = now
        stuck_seconds = now - stuck_since
        _log.warning(
            "stuck_queue_observed",
            pending=pending,
            processing=processing,
            stuck_seconds=round(stuck_seconds, 1),
            threshold_s=_STUCK_SECONDS,
        )
        if stuck_seconds >= _STUCK_SECONDS:
            _log.error(
                "stuck_queue_hard_exit",
                pending=pending,
                processing=processing,
                stuck_seconds=round(stuck_seconds, 1),
                threshold_s=_STUCK_SECONDS,
                note=(
                    "queue has real rows waiting with nothing in flight past the "
                    "threshold; force-exit so supervisord relaunches a clean "
                    "worker (the manual restart, automated)"
                ),
            )
            _exit_process()
            return    # unreachable outside tests (they patch _exit_process)


def start_stuck_queue_watchdog(
    *,
    db_path: Path | str,
    sync_url: str,
    auth_token: str,
    sync_interval_seconds: float,
) -> bool:
    """Start the stuck-queue watchdog as a daemon thread. Idempotent per process.

    Returns True if a watchdog thread was started, False if disabled or already
    running. No-op when ``sync_url`` is empty (local sqlite / tests): the wedge
    is a remote-libsql stale-read phenomenon, and reopening a local file every
    poll would be pointless churn."""
    global _started
    if not _enabled():
        _log.info("stuck_queue_watchdog_disabled")
        return False
    if not sync_url:
        _log.info(
            "stuck_queue_watchdog_skipped",
            reason="no sync_url (local sqlite backend)",
        )
        return False
    with _started_lock:
        if _started:
            return False
        _started = True
    thread = threading.Thread(
        target=_watch,
        args=(db_path, sync_url, auth_token, sync_interval_seconds),
        name="bulkvid-stuck-queue-watchdog",
        daemon=True,
    )
    thread.start()
    _log.info(
        "stuck_queue_watchdog_start",
        threshold_seconds=_STUCK_SECONDS,
        check_interval_seconds=_CHECK_INTERVAL_SECONDS,
        probe_timeout_seconds=_PROBE_TIMEOUT_SECONDS,
    )
    return True
