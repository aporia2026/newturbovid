"""Self-repair pass — turn the states that used to need a human into a no-op.

Why this exists (Plan
``_plans/2026-08-17-stuck-jobs-selfheal-and-restart.md``):

Production has no transactions. ``db.py`` translates BEGIN/COMMIT/ROLLBACK to
no-ops on both remote transports, and the Hrana transport makes every statement
an independent HTTP request with its own deadline, so a ``with _tx():`` block is
a sequence of independently-failing statements that ``_run_db`` re-runs whole on
any failure. Every helper in ``queue.py`` is now individually idempotent under
that model, which prevents NEW drift — but it cannot undo the state already
stranded by the old code, and it cannot cover a partial write in some future
multi-statement helper nobody has written yet.

This module is the net under all of that. Six actions, each of which asks a
question about ground truth in ``row_queue`` and corrects the answer:

  ``finalize_settled_jobs``  active job, nothing left to run  → completed
  ``promote_started_jobs``   queued job whose rows have begun → running
  ``release_stranded_rows``  processing past the lease        → pending
  ``abort_orphan_rows``      live rows under a dead parent    → failed
  ``resync_job_counters``    counters that disagree with rows → recomputed
  ``count_row_count_drift``  read-only: submits that partially failed

Two properties make the pass safe to run unattended on a timer:

  * **Every action is idempotent.** They assign derived values or move rows
    across a one-way boundary, so a pass that runs twice — or races another
    pass, or races the worker — converges instead of compounding.
  * **No action can invent or destroy work.** Nothing here resurrects a terminal
    job, marks an unfinished row done, or rewrites ``row_count`` (the one number
    that cannot be reconstructed; a mismatch is reported instead). The single
    action that moves a row BACKWARD, ``release_stranded_rows``, is the existing
    lease sweep, bounded by a cutoff well past the longest row budget.

Ordering matters and is asserted by ``ORDERED_ACTIONS``: release before
finalize, because a released row makes its job un-settled again and finalizing
first would close a job that still has work to do.

Runs from two places, sharing this one implementation:

  * the worker's stuck-queue watchdog thread, on its OWN fresh short-lived
    connection (already isolated from the shared DB pool so it cannot wedge with
    it) — the unattended path, and the one that matters while the operator is
    away;
  * ``POST /jobs/repair``, scoped to the caller's own jobs — the "fix it now"
    button.

The read path (``/jobs/poll``) deliberately does NOT run repairs. Mutating job
state as a side effect of the route the sidebar hits every few seconds means a
repair can fire concurrently with an in-progress submit, on a request nobody
asked to have side effects.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from bulkvid.logging import get_logger
from bulkvid.orchestrator.queue import (
    REPAIR_SOURCE_MANUAL,
    abort_orphan_rows,
    count_row_count_drift,
    finalize_settled_jobs,
    promote_started_jobs,
    record_repair_run,
    resync_job_counters,
    sweep_expired_processing_rows,
)

_log = get_logger("repair")


# How long a row may sit PROCESSING before the pass calls it stranded and returns
# it to PENDING for another attempt.
#
# This is the SAME knob (same env var, same default) that the stuck-queue
# watchdog's lease sweep used before this module existed — the sweep is now one
# action inside this pass rather than a second janitor on its own timer, so the
# constant moved here and the watchdog imports it. One hour is deliberately
# generous: it must clear the LONGEST per-row budget (30 min for the heaviest
# tabs) with room for an admin-raised override, because releasing a row that is
# still being worked on means paying a second time for the same video. This is
# the only action in the pass that can cost money, which is why its cutoff is the
# loosest thing here.
STRANDED_ROW_AFTER_SECONDS = float(
    os.environ.get("BULKVID_STUCK_ROW_SWEEP_AFTER_SECONDS") or 3_600.0
)


@dataclass
class RepairAction:
    """One action's outcome. ``changed`` is the count of rows/jobs actually
    corrected — zero on a healthy system, which is the normal case."""

    name: str
    changed: int
    notes: list[str] = field(default_factory=list)


@dataclass
class RepairReport:
    """The whole pass. ``lines`` is what the sidebar renders verbatim."""

    source: str
    actor: str
    scope: str
    actions: list[RepairAction]
    lines: list[str]
    elapsed_ms: int
    # Names of actions that raised instead of running. Tracked separately from
    # ``actions`` because a pass where NOTHING could run must never be summarised
    # as a clean bill of health — see ``summary``.
    failed: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return sum(a.changed for a in self.actions)

    def summary(self) -> str:
        """One plain sentence for the operator. No jargon: they asked for a
        button that fixes things, not a report on a state machine."""
        # Failures come first, and a pass where every action failed must NOT read
        # as healthy. A flapping DB makes every action raise, and "everything
        # looks healthy" would then be the most misleading thing we could say —
        # the operator would go looking somewhere else for a problem that is
        # right here.
        if self.failed and not self.actions:
            return (
                "Could not check anything. The database did not answer. "
                "Try again in a minute."
            )
        if not self.changed:
            healthy = "Nothing needed fixing. Everything looks healthy."
            if self.failed:
                return (
                    f"{healthy} ({len(self.failed)} of the checks could not run, "
                    "so this is not the full picture.)"
                )
            return healthy
        parts: list[str] = []
        by_name = {a.name: a.changed for a in self.actions if a.changed}
        if by_name.get(ACTION_FINALIZE):
            n = by_name[ACTION_FINALIZE]
            parts.append(
                f"{n} finished job{'s' if n != 1 else ''} that still showed as "
                "running " + ("are" if n != 1 else "is") + " now marked done"
            )
        if by_name.get(ACTION_PROMOTE):
            n = by_name[ACTION_PROMOTE]
            parts.append(f"{n} job{'s' if n != 1 else ''} now show live progress")
        if by_name.get(ACTION_RELEASE):
            n = by_name[ACTION_RELEASE]
            parts.append(f"{n} stalled row{'s' if n != 1 else ''} queued to retry")
        if by_name.get(ACTION_ORPHANS):
            n = by_name[ACTION_ORPHANS]
            parts.append(f"{n} leftover row{'s' if n != 1 else ''} cleared")
        if by_name.get(ACTION_COUNTERS):
            n = by_name[ACTION_COUNTERS]
            parts.append(f"{n} job progress count{'s' if n != 1 else ''} corrected")
        if not parts:
            parts.append(f"{self.changed} item(s) corrected")
        fixed = "Fixed: " + "; ".join(parts) + ". No videos were lost."
        if self.failed:
            return (
                f"{fixed} ({len(self.failed)} of the checks could not run, so "
                "there may be more.)"
            )
        return fixed


# Action names. Module constants because they are written into the durable audit
# log and read back by the sidebar — renaming one is a data migration, not a
# refactor.
ACTION_RELEASE = "release_stranded_rows"
ACTION_ORPHANS = "abort_orphan_rows"
ACTION_COUNTERS = "resync_job_counters"
ACTION_FINALIZE = "finalize_settled_jobs"
ACTION_PROMOTE = "promote_started_jobs"
ACTION_DRIFT = "report_row_count_drift"


def _iso_cutoff(seconds: float) -> str:
    """``now - seconds`` as an ISO-8601 UTC string, comparable with ``<`` against
    the fixed-width same-offset timestamps ``_now_iso`` writes."""
    return (
        datetime.now(UTC) - timedelta(seconds=max(0.0, seconds))
    ).isoformat(timespec="seconds")


# ── Individual actions ───────────────────────────────────────────────────────
#
# Each takes ``(conn, user_email)`` and returns a ``RepairAction``, so
# ``run_repairs`` can iterate them in a fixed order with no per-action special
# casing. ``user_email=None`` means fleet-wide (the watchdog and admins).


def _release_stranded_rows(
    conn: Any, user_email: str | None, *, include_job_ids: bool,
) -> RepairAction:
    """Return rows stuck PROCESSING past the lease to PENDING so they run again.

    Wraps the existing ``sweep_expired_processing_rows`` rather than
    reimplementing it — that helper is already the watchdog's lease sweep and is
    already bounded to active jobs, so a killed or completed job can never have
    rows resurrected here.

    Note this is the one action that moves work BACKWARD, and the only one that
    can cost money (a released row is processed again). The cutoff is sized
    accordingly."""
    n = sweep_expired_processing_rows(
        conn,
        cutoff_iso=_iso_cutoff(STRANDED_ROW_AFTER_SECONDS),
        user_email=user_email,
    )
    notes: list[str] = []
    if n:
        notes.append(
            "no worker had touched them for over "
            f"{int(STRANDED_ROW_AFTER_SECONDS // 60)} minutes"
        )
    return RepairAction(name=ACTION_RELEASE, changed=n, notes=notes)


def _abort_orphan_rows(
    conn: Any, user_email: str | None, *, include_job_ids: bool,
) -> RepairAction:
    """Fail rows still pending/processing under a job that already ended.

    Unreachable state: nothing claims rows under a terminal job and nothing
    sweeps them either, so without this they sit in the table forever and keep
    the job's counters wrong. Left behind when a kill's row-abort statement was
    lost after the job's own status change landed."""
    n = abort_orphan_rows(conn, user_email=user_email)
    return RepairAction(name=ACTION_ORPHANS, changed=n)


def _resync_job_counters(
    conn: Any, user_email: str | None, *, include_job_ids: bool,
) -> RepairAction:
    """Recompute progress counters from ``row_queue`` where they disagree."""
    n = resync_job_counters(conn, user_email=user_email)
    return RepairAction(name=ACTION_COUNTERS, changed=n)


def _finalize_settled_jobs(
    conn: Any, user_email: str | None, *, include_job_ids: bool,
) -> RepairAction:
    """Close active jobs that have nothing left to run.

    THE action that clears the reported bug: a job whose every row finished but
    whose status stayed ``running``, so the sidebar kept an active card the
    operator could not get rid of."""
    n = finalize_settled_jobs(conn, user_email=user_email)
    return RepairAction(name=ACTION_FINALIZE, changed=n)


def _promote_started_jobs(
    conn: Any, user_email: str | None, *, include_job_ids: bool,
) -> RepairAction:
    """Promote queued jobs whose rows have actually started, so the sidebar shows
    their live progress instead of "waiting in queue"."""
    n = promote_started_jobs(conn, user_email=user_email)
    return RepairAction(name=ACTION_PROMOTE, changed=n)


def _report_row_count_drift(
    conn: Any, user_email: str | None, *, include_job_ids: bool,
) -> RepairAction:
    """Read-only: finished jobs holding fewer rows than they were submitted with.

    Deliberately reports instead of repairing. The missing rows were never
    inserted (a submit that partially failed), so they cannot be reconstructed —
    and rewriting ``row_count`` down to hide the gap would erase the only
    evidence that those sheet rows still need resubmitting. ``changed`` stays 0
    because nothing was written; the notes carry the finding.

    Job ids are omitted on the automatic fleet-wide pass, whose log every bulk
    user can read — see ``list_repair_runs``."""
    drift = count_row_count_drift(conn, user_email=user_email)
    notes: list[str] = []
    for job_id, row_count, present in drift:
        if include_job_ids:
            notes.append(
                f"{job_id}: submitted {row_count} rows but only {present} were "
                "queued, so the missing rows never ran and need resubmitting"
            )
        else:
            notes.append(
                f"a job submitted {row_count} rows but only {present} were "
                "queued, so the missing rows never ran and need resubmitting"
            )
    return RepairAction(name=ACTION_DRIFT, changed=0, notes=notes)


# Fixed execution order. Release BEFORE finalize: a released row makes its job
# un-settled again, and finalizing first would close a job that still has work
# to do. Counters before finalize so a finalized job's numbers are already right
# when the card moves to the archive. Drift report last — it is read-only and
# describes the state the writes above have already settled.
ORDERED_ACTIONS: tuple[
    Callable[..., RepairAction], ...
] = (
    _release_stranded_rows,
    _abort_orphan_rows,
    _resync_job_counters,
    _finalize_settled_jobs,
    _promote_started_jobs,
    _report_row_count_drift,
)


# ── The pass ─────────────────────────────────────────────────────────────────


def run_repairs(
    conn: Any,
    *,
    source: str,
    actor: str,
    user_email: str | None = None,
    record: bool = True,
) -> RepairReport:
    """Run every repair action in order and return a report.

    ``user_email=None`` is fleet-wide (the worker's watchdog, and admins);
    otherwise every action is bounded to that user's jobs, so a bulk user's
    button can never touch anyone else's work.

    One action failing must not cost us the others: each is wrapped, and a
    failure becomes a log line plus a WARNING rather than an exception. That
    matters most on the unattended path, where the alternative is a watchdog
    thread that dies quietly and takes the self-healing with it.

    Passes that changed nothing are NOT recorded (``record`` still true) — at a
    2-minute cadence that would be ~700 empty rows a day, drowning the handful
    that carry the actual story.
    """
    started = time.monotonic()
    # Job ids go in the log ONLY on a manual pass, which is already scoped to the
    # caller's own jobs. The automatic pass is fleet-wide and its log is readable
    # by every bulk user (that is the point — it is the "what happened while I was
    # away" record), so it stays counts-only and leaks nobody's job ids.
    include_job_ids = source == REPAIR_SOURCE_MANUAL
    actions: list[RepairAction] = []
    failed: list[str] = []
    lines: list[str] = []

    for fn in ORDERED_ACTIONS:
        name = getattr(fn, "__name__", "action").lstrip("_")
        try:
            action = fn(conn, user_email, include_job_ids=include_job_ids)
        except Exception as e:    # noqa: BLE001 — one failure must not abort the pass
            _log.warning(
                "repair_action_failed",
                action=name,
                error=str(e)[:200],
                error_type=type(e).__name__,
            )
            failed.append(name)
            lines.append(f"[!] {name}: could not run ({type(e).__name__})")
            continue
        actions.append(action)
        if action.changed or action.notes:
            lines.append(f"{action.name}: {action.changed} fixed")
            lines.extend(f"    - {note}" for note in action.notes)

    if not lines:
        lines.append("Checked everything. Nothing needed fixing.")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    report = RepairReport(
        source=source,
        actor=actor,
        scope=user_email or "",
        actions=actions,
        lines=lines,
        elapsed_ms=elapsed_ms,
        failed=failed,
    )

    if failed:
        _log.warning(
            "repair_pass_incomplete",
            source=source,
            actor=actor,
            failed=",".join(failed),
            ran=len(actions),
        )
    if report.changed:
        _log.info(
            "repair_pass",
            source=source,
            actor=actor,
            scope=user_email or "ALL",
            changed=report.changed,
            elapsed_ms=elapsed_ms,
            **{a.name: a.changed for a in actions},
        )
    if record and report.changed:
        try:
            record_repair_run(
                conn,
                source=source,
                actor=actor,
                scope=user_email or "",
                changed=report.changed,
                log="\n".join(lines),
            )
        except Exception as e:    # noqa: BLE001 — the audit must never fail the repair
            _log.warning(
                "repair_audit_write_failed",
                source=source,
                error=str(e)[:200],
            )
    return report
