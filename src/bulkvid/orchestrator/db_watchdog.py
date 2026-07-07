"""DB-pool wedge watchdog — force a clean restart when the dedicated DB thread
pool is fully wedged on uncancellable libsql calls.

Why this exists (Plan ``_plans/2026-07-07-db-wedge-permanent-fix.md`` §Phase 1):

The sync ``libsql`` client's remote mode has no request timeout and no
``interrupt()`` (verified via Context7). A statement that stalls on a flapping
Turso connection blocks its pool thread FOREVER; ``asyncio.wait_for`` abandons
the awaiting coroutine but the thread is never reclaimed. Enough leaked threads
(~``pool_size``) and the pool serves nothing — the web submit hangs, the worker
stalls, and only a full container restart recovers. Because a hang is not a
crash, ``supervisord autorestart`` never fires on its own.

This watchdog closes that gap for BOTH processes (web has no other watchdog; the
worker's claim-failure watchdog only covers the idle path). A plain daemon
thread — deliberately NOT on the event loop or the DB pool, so it can never
wedge with them — polls ``db.db_pool_stats``. When every pool thread has been
stuck past ``_DB_WEDGE_SECONDS`` for a couple of consecutive checks, it logs
loudly and ``os._exit``s so supervisord relaunches a clean process.

It is a backstop, not the cure. It reads a counter (never issues a DB call), so
it cannot share the failure mode of the thing it watches — the flaw the council
flagged in the rejected "self-healing pool swap". The real cure (a transport
deadline via the async libsql/hrana client) is Phase 2; until then a bounded,
clean restart beats an indefinite wedge. The cost is identical to the manual
restart operators already do today: ``recover_orphaned_rows`` re-drives the
handful of in-flight rows on boot (idempotency keys keep that safe).
"""

from __future__ import annotations

import os
import threading
import time

from bulkvid.logging import get_logger
from bulkvid.orchestrator import db

_log = get_logger("db watchdog")


def _enabled() -> bool:
    """On by default — unattended recovery is the whole point. Env kill switch
    (``BULKVID_DB_WEDGE_WATCHDOG_ENABLED=0``) for a deploy that wants to disable
    it without a code change."""
    raw = os.environ.get("BULKVID_DB_WEDGE_WATCHDOG_ENABLED")
    if raw is None or raw == "":
        return True
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Poll cadence for the watchdog thread. Cheap (a dict snapshot), so a tight-ish
# interval keeps time-to-recovery low without meaningful cost.
_CHECK_INTERVAL_SECONDS = float(
    os.environ.get("BULKVID_DB_WEDGE_CHECK_INTERVAL_SECONDS") or 5.0
)
# Consecutive fully-wedged observations before we exit. A fully-wedged pool
# (every thread stuck past ``_DB_WEDGE_SECONDS``) does not un-wedge, so a small
# confirm window only guards against a one-off race, not a transient. With the
# defaults this is ~``_DB_WEDGE_SECONDS`` (60s) + 2x5s ~= 70s from wedge onset to
# restart.
_WEDGE_CONFIRM_CHECKS = int(
    os.environ.get("BULKVID_DB_WEDGE_CONFIRM_CHECKS") or 2
)
# Non-zero so the exit reads as abnormal in supervisord logs; ``autorestart``
# relaunches regardless of the code. Matches the runner watchdog's convention.
_EXIT_CODE = 1

_started = False
_started_lock = threading.Lock()


def _exit_process() -> None:
    """Force-exit so supervisord relaunches a clean process. ``os._exit`` (not
    ``sys.exit``) bypasses the atexit/cleanup path — those handlers would
    themselves try to touch the wedged DB and hang. Isolated so tests can patch
    it and assert the trip without killing the test runner."""
    os._exit(_EXIT_CODE)


def _watch(process_label: str) -> None:
    """Daemon-thread loop. Reads pool stats; exits on a confirmed full wedge."""
    consecutive = 0
    while True:
        time.sleep(_CHECK_INTERVAL_SECONDS)
        try:
            stats = db.db_pool_stats()
        except Exception as e:    # a broken probe must never kill the watch
            _log.warning("db_watchdog_probe_error", error=str(e)[:200])
            continue

        pool_size = stats["pool_size"]
        wedged = stats["wedged"]
        # Fully wedged = every pool thread stuck past the wedge threshold. Guard
        # ``pool_size >= 1`` so a misconfigured 0-size pool can't trip on 0>=0.
        fully_wedged = pool_size >= 1 and wedged >= pool_size
        if not fully_wedged:
            consecutive = 0
            continue

        consecutive += 1
        _log.warning(
            "db_watchdog_wedge_observed",
            process=process_label,
            wedged=wedged,
            pool_size=pool_size,
            running=stats["running"],
            consecutive=consecutive,
            of=_WEDGE_CONFIRM_CHECKS,
        )
        if consecutive >= _WEDGE_CONFIRM_CHECKS:
            _log.error(
                "db_watchdog_hard_exit",
                process=process_label,
                wedged=wedged,
                pool_size=pool_size,
                note="DB pool fully wedged; force-exit for supervisord restart",
            )
            _exit_process()
            return    # unreachable outside tests (they patch _exit_process)


def start_db_wedge_watchdog(process_label: str) -> bool:
    """Start the wedge watchdog as a daemon thread. Idempotent per process.

    Returns True if a watchdog thread was started, False if disabled or already
    running. ``process_label`` ("web" / "worker") is logged so a restart in the
    HF logs names which process self-exited."""
    global _started
    if not _enabled():
        _log.info("db_watchdog_disabled", process=process_label)
        return False
    with _started_lock:
        if _started:
            return False
        _started = True
    thread = threading.Thread(
        target=_watch,
        args=(process_label,),
        name="bulkvid-db-watchdog",
        daemon=True,
    )
    thread.start()
    _log.info(
        "db_watchdog_start",
        process=process_label,
        wedge_seconds=db._DB_WEDGE_SECONDS,
        check_interval_seconds=_CHECK_INTERVAL_SECONDS,
        confirm_checks=_WEDGE_CONFIRM_CHECKS,
    )
    return True
