"""Stuck-queue restart watchdog — automate the manual "restart the Space".

Why this exists (Plans ``_plans/2026-08-06-worker-stuck-queue-restart-watchdog.md``
and ``_plans/2026-08-12-worker-liveness-net-heartbeat-forensics.md``):

Users kept hitting "queued but nothing runs" wedges that only a manual HF restart
cleared. Four earlier self-healers each key off a specific FAILURE on the worker's
own connection (an exception, a hung thread, consecutive claim failures, or a
stale read the reconnect is trusted to cure). None keys off the ground-truth
OUTCOME — "real work is waiting and nothing is draining" — from an INDEPENDENT
vantage point. This watchdog does.

A plain daemon thread — deliberately NOT on the event loop and NOT on the shared
DB pool/connection, so it can never wedge with them — opens its OWN fresh
short-lived connection every ``_CHECK_INTERVAL_SECONDS`` and acts on what it
sees. It fires ``os._exit`` (supervisord ``autorestart`` relaunches a clean
worker; the web process stays up) on either of two independent conditions:

  1. **Worker heartbeat stale while work waits** — the UNIVERSAL net. The worker
     lands a liveness beat in the DB every ~30s; that write can only succeed if
     its event loop is still scheduling AND its DB path still works. When beats
     stop for ``_HEARTBEAT_STALE_SECONDS`` while the queue holds work, the worker
     is dead in some way we do not need to name, and a fresh process is the cure.
     This is the condition that covers the 2026-08-12 incident (6 rows frozen
     mid-flight, exactly ``max_concurrent``) which every earlier guard missed
     because they all gate on ``processing == 0`` / ``in_flight == 0``.
  2. **Queue stuck while provably idle** — the original condition: ``pending > 0
     AND processing == 0`` sustained past ``_STUCK_SECONDS``. Kept because it
     fires with zero in-flight work at risk, so it is the cheapest possible
     restart when it applies.

Two non-restarting duties round it out:

  * a **self-repair pass** (``repair.run_repairs``) corrects the stable wrong
    states that used to need a human: it returns PROCESSING rows stranded past
    ``repair.STRANDED_ROW_AFTER_SECONDS`` back to PENDING (the strand that
    ``runner_pending_record_giveup`` leaves behind would otherwise hold
    ``processing > 0`` forever and silently disarm condition 2), finalizes jobs
    whose every row has finished, promotes jobs whose rows have started, clears
    rows orphaned under a dead parent, and recomputes progress counters that
    drifted; and
  * **forensics**: every exit path dumps all thread stacks to stderr AND to the
    DB first, because the restart destroys the only process that knows why it
    wedged and HF retains no logs from before it.

The safety property that makes an auto-restart safe: it fires ONLY on a
SUCCESSFUL fresh read. A wedged worker (Turso reachable, worker's view or loop
broken) → fresh read succeeds, shows the truth → restart, and it works. Turso
genuinely down (nobody can read) → fresh read fails → no restart, since a restart
cannot help a down DB. That discrimination is what prevents restart-hammering
during an outage.

It is a backstop, not the cure — the real cure (a transport with per-request
deadlines) is the follow-up PR to this plan.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bulkvid.logging import get_logger
from bulkvid.orchestrator import db as _db
from bulkvid.orchestrator import forensics
from bulkvid.orchestrator.queue import (
    REPAIR_SOURCE_AUTO,
    WorkerHeartbeat,
    count_active_queue,
    read_worker_heartbeat,
    record_wedge_forensics,
)
from bulkvid.orchestrator.repair import (
    STRANDED_ROW_AFTER_SECONDS,
    RepairReport,
    run_repairs,
)

_log = get_logger("stuck queue watchdog")

# Recorded as the ``actor`` on every automatic ``repair_audit`` row, so the
# sidebar's self-heal pane can say "fixed automatically" rather than naming a
# person who was not involved.
_REPAIR_ACTOR = "worker-watchdog"


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
# How stale the worker's liveness beat may get, with work waiting, before we
# call the worker dead. Default 240s = 8 consecutive missed 30s beats: far past
# any plausible GC pause, Turso flap, or scheduling hiccup (each of which costs
# at most a beat or two), yet a fraction of the time a human takes to notice.
_HEARTBEAT_STALE_SECONDS = _positive_float(
    "BULKVID_STUCK_QUEUE_WATCHDOG_HEARTBEAT_STALE_SECONDS", 240.0
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
# Repair-pass cadence. This used to pace a standalone lease sweep; the sweep is
# now ONE ACTION inside ``repair.run_repairs``, which also finalizes settled
# jobs, promotes started ones, clears orphan rows and resyncs progress counters
# (Plan ``_plans/2026-08-17-stuck-jobs-selfheal-and-restart.md``). Same env var,
# same 5-minute default: one janitor on one clock beats two on two. The repair
# pass is the slow janitor for stable wrong states; the heartbeat condition above
# is the fast net for real wedges.
#
# The stranded-row age lives in ``repair.STRANDED_ROW_AFTER_SECONDS`` (still read
# from ``BULKVID_STUCK_ROW_SWEEP_AFTER_SECONDS``) so the pass and its one
# money-costing action are configured in the same place.
_REPAIR_INTERVAL_SECONDS = _positive_float(
    "BULKVID_STUCK_QUEUE_WATCHDOG_SWEEP_INTERVAL_SECONDS", 300.0
)
# Wall-clock budget for one repair pass. Looser than the probe's because the pass
# issues a handful of statements rather than one, and unlike the probe its result
# feeds no restart decision — a pass that times out just runs again next tick.
_REPAIR_TIMEOUT_SECONDS = _positive_float(
    "BULKVID_REPAIR_TIMEOUT_SECONDS", 60.0
)
# Budget for persisting the pre-exit autopsy. Short: the restart is the priority
# and the stderr dump has already landed by this point.
_FORENSICS_TIMEOUT_SECONDS = _positive_float(
    "BULKVID_STUCK_QUEUE_WATCHDOG_FORENSICS_TIMEOUT_SECONDS", 10.0
)
# Non-zero so the exit reads as abnormal in supervisord logs; ``autorestart``
# relaunches regardless of the code. Matches both existing watchdogs.
_EXIT_CODE = 1

_started = False
_started_lock = threading.Lock()


@dataclass(frozen=True)
class _Probe:
    """One consistent snapshot of ground truth, read on a fresh connection.

    Counts and heartbeat travel together because a restart decision must weigh
    them at the SAME instant — "work is waiting" and "the worker is dead" mean
    nothing apart."""

    pending: int
    processing: int
    heartbeat: WorkerHeartbeat | None


def _exit_process() -> None:
    """Force-exit so supervisord relaunches a clean process. ``os._exit`` (not
    ``sys.exit``) bypasses the atexit/cleanup path — those handlers would
    themselves try to touch the (possibly wedged) DB and hang. Isolated so tests
    can patch it and assert the trip without killing the test runner."""
    os._exit(_EXIT_CODE)


def _fresh_connection(
    db_path: Path | str,
    sync_url: str,
    auth_token: str,
    sync_interval_seconds: float,
) -> Any:
    """Open a FRESH short-lived connection.

    A fresh connection is the whole point: it reads ground truth independent of
    the worker's possibly-stale long-lived connection (the wedge's signature is
    the two disagreeing — the web app seeing a backlog the worker's connection
    hides)."""
    return _db.connect(
        db_path,
        sync_url=sync_url,
        auth_token=auth_token,
        sync_interval_seconds=sync_interval_seconds,
        # Fresh connection every poll — don't flood the logs we use to diagnose
        # the wedge with a ``db_backend`` line every 30s.
        quiet=True,
    )


def _probe_active_queue(
    db_path: Path | str,
    sync_url: str,
    auth_token: str,
    sync_interval_seconds: float,
) -> _Probe:
    """Read queue depth + worker liveness on a fresh connection, then close it.

    Raises on any failure so the caller treats an unreachable Turso as "cannot
    confirm anything" (no restart)."""
    conn = _fresh_connection(db_path, sync_url, auth_token, sync_interval_seconds)
    try:
        pending, processing = count_active_queue(conn)
        heartbeat = read_worker_heartbeat(conn)
        return _Probe(pending=pending, processing=processing, heartbeat=heartbeat)
    finally:
        # A half-dead handle must not mask the result or the probe's raise.
        with suppress(Exception):
            conn.close()


def _repair_pass(
    db_path: Path | str,
    sync_url: str,
    auth_token: str,
    sync_interval_seconds: float,
) -> RepairReport:
    """Run the full self-repair pass on a FRESH connection.

    This thread is the right home for it, and the only good one. It already holds
    the two properties the pass needs — its own short-lived connection (so it
    reads ground truth rather than the worker's possibly-stale view) and complete
    isolation from the shared DB pool (so it cannot wedge along with it) — and it
    is already the thread that runs unattended while nobody is watching, which is
    exactly when these states need fixing. Adding a second timer thread, or
    putting writes on the web app's read path, would buy nothing and cost
    supervision surface.

    Fleet-wide (``user_email=None``): the worker serves every user, and a repair
    scoped to one of them would leave the rest stranded."""
    conn = _fresh_connection(db_path, sync_url, auth_token, sync_interval_seconds)
    try:
        return run_repairs(
            conn, source=REPAIR_SOURCE_AUTO, actor=_REPAIR_ACTOR, user_email=None
        )
    finally:
        with suppress(Exception):
            conn.close()


def _write_forensics(
    db_path: Path | str,
    sync_url: str,
    auth_token: str,
    sync_interval_seconds: float,
    *,
    reason: str,
    pending: int,
    processing: int,
    heartbeat_age_s: float | None,
    stacks: str,
) -> None:
    """Persist the autopsy on a fresh connection (the worker's own may be the
    thing that is wedged)."""
    conn = _fresh_connection(db_path, sync_url, auth_token, sync_interval_seconds)
    try:
        record_wedge_forensics(
            conn,
            process="worker",
            reason=reason,
            pending=pending,
            processing=processing,
            heartbeat_age_s=heartbeat_age_s,
            stacks=stacks,
        )
    finally:
        with suppress(Exception):
            conn.close()


def _run_time_boxed[T](what: str, fn: Callable[[], T], timeout: float) -> T:
    """Run ``fn`` in a throwaway thread joined with a timeout.

    libsql calls are uncancellable, so any of these DB touches could block
    forever against an unresponsive Turso. Running each in a daemon side-thread
    and joining with a budget lets the watchdog abandon a hung call and stay
    alive (abandoned threads are bounded — one per hung call, and hung calls only
    happen when Turso is down, the don't-restart case). Re-raises the callable's
    own exception; raises ``TimeoutError`` when it overruns."""
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["result"] = fn()
        except BaseException as e:    # carried back to the watchdog thread
            box["error"] = e

    t = threading.Thread(target=_run, name=f"bulkvid-stuckq-{what}", daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        raise TimeoutError(f"{what} exceeded {timeout:.0f}s")
    if "error" in box:
        raise box["error"]
    return box["result"]    # type: ignore[no-any-return]


def _probe_time_boxed(
    db_path: Path | str,
    sync_url: str,
    auth_token: str,
    sync_interval_seconds: float,
) -> _Probe:
    """``_probe_active_queue`` under ``_PROBE_TIMEOUT_SECONDS``."""
    return _run_time_boxed(
        "probe",
        lambda: _probe_active_queue(
            db_path, sync_url, auth_token, sync_interval_seconds
        ),
        _PROBE_TIMEOUT_SECONDS,
    )


def _repair_time_boxed(
    db_path: Path | str,
    sync_url: str,
    auth_token: str,
    sync_interval_seconds: float,
) -> RepairReport:
    """``_repair_pass`` under ``_REPAIR_TIMEOUT_SECONDS``.

    Gets a looser budget than the probe because the pass is a handful of
    statements rather than one, and unlike the probe its result feeds no restart
    decision — a pass that times out simply retries on the next tick."""
    return _run_time_boxed(
        "repair",
        lambda: _repair_pass(
            db_path, sync_url, auth_token, sync_interval_seconds
        ),
        _REPAIR_TIMEOUT_SECONDS,
    )


def _exit_with_forensics(
    db_path: Path | str,
    sync_url: str,
    auth_token: str,
    sync_interval_seconds: float,
    *,
    reason: str,
    pending: int,
    processing: int,
    heartbeat_age_s: float | None,
) -> None:
    """Capture the autopsy, persist it, then force the restart.

    Order matters. Stacks are captured FIRST, at the moment of detection, before
    any of our own DB work muddies the picture. stderr comes next so the operator
    watching the HF log tab sees it even if the DB write fails. The DB write is
    last and strictly best-effort: it is the copy that survives the restart, but
    nothing about it may delay or prevent the recovery we came for."""
    stacks = forensics.capture_thread_stacks()
    forensics.dump_to_stderr(reason)
    try:
        _run_time_boxed(
            "forensics",
            lambda: _write_forensics(
                db_path,
                sync_url,
                auth_token,
                sync_interval_seconds,
                reason=reason,
                pending=pending,
                processing=processing,
                heartbeat_age_s=heartbeat_age_s,
                stacks=stacks,
            ),
            _FORENSICS_TIMEOUT_SECONDS,
        )
        _log.info("wedge_forensics_written", reason=reason)
    except Exception as e:    # noqa: BLE001 — never block the restart
        _log.warning(
            "wedge_forensics_failed", reason=reason, error=str(e)[:200]
        )
    _exit_process()


def _watch(
    db_path: Path | str,
    sync_url: str,
    auth_token: str,
    sync_interval_seconds: float,
) -> None:
    """Daemon-thread loop: run the repair pass, probe ground truth, restart a
    worker that is provably not doing its job.

    ``stuck_since`` is the monotonic time the CURRENT uninterrupted idle-stuck
    streak began, or ``None`` when the last poll was not-stuck (or could not be
    confirmed). Any healthy or unconfirmable poll resets it, so only a
    CONTINUOUS stretch past ``_STUCK_SECONDS`` fires."""
    stuck_since: float | None = None
    started_monotonic = time.monotonic()
    last_repair_monotonic = time.monotonic()
    warned_missing_heartbeat = False

    while True:
        time.sleep(_CHECK_INTERVAL_SECONDS)

        # ── Janitor: the self-repair pass ───────────────────────────────────
        # Runs BEFORE the probe so the counts below reflect post-repair truth
        # (releasing a stranded row turns ``processing`` into ``pending``, which
        # is exactly the distinction the restart conditions weigh), and on its own
        # cadence so it is independent of any restart decision.
        if time.monotonic() - last_repair_monotonic >= _REPAIR_INTERVAL_SECONDS:
            last_repair_monotonic = time.monotonic()
            try:
                report = _repair_time_boxed(
                    db_path, sync_url, auth_token, sync_interval_seconds
                )
                if report.changed:
                    _log.warning(
                        "auto_repair_applied",
                        changed=report.changed,
                        elapsed_ms=report.elapsed_ms,
                        detail=" | ".join(report.lines)[:1000],
                        note=(
                            "states that used to need a human were corrected "
                            "automatically; see repair_audit / the sidebar's "
                            "self-heal pane"
                        ),
                    )
            except Exception as e:    # noqa: BLE001 — janitor must not sink the loop
                _log.warning("auto_repair_failed", error=str(e)[:200])

        try:
            probe = _probe_time_boxed(
                db_path, sync_url, auth_token, sync_interval_seconds
            )
        except Exception as e:
            # Cannot confirm anything (Turso unreachable / probe hung). A
            # restart wouldn't help a down DB, so reset the streak and wait.
            _log.warning("stuck_queue_probe_error", error=str(e)[:200])
            stuck_since = None
            continue

        pending, processing = probe.pending, probe.processing
        work_waiting = pending > 0 or processing > 0
        uptime = time.monotonic() - started_monotonic

        # ── Condition 1: worker heartbeat stale while work waits ────────────
        # Wall-clock (not monotonic) because the beat is written by a different
        # process. A negative age means the clocks disagree; that reads as
        # "fresh" and cannot fire, which is the safe direction.
        heartbeat_age: float | None = None
        if probe.heartbeat is not None:
            heartbeat_age = time.time() - probe.heartbeat.epoch
        elif uptime >= _HEARTBEAT_STALE_SECONDS and not warned_missing_heartbeat:
            # No beat has EVER landed well after boot: the net is disarmed and
            # nobody would otherwise know. Never a restart trigger — an absent
            # row is not evidence of a wedge.
            warned_missing_heartbeat = True
            _log.warning(
                "worker_heartbeat_missing",
                uptime_s=round(uptime, 1),
                note=(
                    "no worker heartbeat row; the heartbeat restart condition "
                    "is inactive (old worker build or a failing beat writer)"
                ),
            )

        if heartbeat_age is not None:
            if heartbeat_age >= _HEARTBEAT_STALE_SECONDS * 0.5:
                # Breadcrumb trail toward a restart — visible whether or not
                # work happens to be waiting right now.
                _log.warning(
                    "worker_heartbeat_aging",
                    heartbeat_age_s=round(heartbeat_age, 1),
                    stale_at_s=_HEARTBEAT_STALE_SECONDS,
                    pending=pending,
                    processing=processing,
                )
            if (
                heartbeat_age >= _HEARTBEAT_STALE_SECONDS
                and work_waiting
                # Uptime gate: immediately after boot the row may still hold the
                # PREVIOUS process's epoch, which would otherwise read as stale
                # and restart a perfectly healthy fresh worker.
                and uptime >= _HEARTBEAT_STALE_SECONDS
            ):
                _log.error(
                    "stuck_queue_heartbeat_exit",
                    heartbeat_age_s=round(heartbeat_age, 1),
                    threshold_s=_HEARTBEAT_STALE_SECONDS,
                    pending=pending,
                    processing=processing,
                    note=(
                        "worker stopped beating while real work was waiting; "
                        "force-exit so supervisord relaunches a clean worker "
                        "(the manual restart, automated)"
                    ),
                )
                _exit_with_forensics(
                    db_path,
                    sync_url,
                    auth_token,
                    sync_interval_seconds,
                    reason="worker_heartbeat_stale",
                    pending=pending,
                    processing=processing,
                    heartbeat_age_s=heartbeat_age,
                )
                return    # unreachable outside tests (they patch _exit_process)

        # ── Condition 2: queue stuck while provably idle ────────────────────
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
            _exit_with_forensics(
                db_path,
                sync_url,
                auth_token,
                sync_interval_seconds,
                reason="stuck_queue_idle",
                pending=pending,
                processing=processing,
                heartbeat_age_s=heartbeat_age,
            )
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
    is a remote-libsql phenomenon, and reopening a local file every poll would be
    pointless churn."""
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
        heartbeat_stale_seconds=_HEARTBEAT_STALE_SECONDS,
        check_interval_seconds=_CHECK_INTERVAL_SECONDS,
        probe_timeout_seconds=_PROBE_TIMEOUT_SECONDS,
        repair_interval_seconds=_REPAIR_INTERVAL_SECONDS,
        stranded_row_after_seconds=STRANDED_ROW_AFTER_SECONDS,
    )
    return True
