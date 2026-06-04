"""SQLite-backed job queue.

Two tables, WAL mode for safe concurrent reads from the web app while the
worker writes:

  - ``jobs``         — one row per batch submission (status, cost, counts)
  - ``row_queue``    — one row per Sheet row to process, with payload + result

Sync SQLite operations are wrapped in ``asyncio.to_thread`` so the event loop
stays responsive. The DB file lives at ``<BULKVID_DATA_DIR>/jobs.db``.

Plan §5 ("Process split"), §13 Phase 3.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bulkvid.logging import get_logger
from bulkvid.models.row import (
    CartoonRow,
    FourImagesVO2Row,
    ImageVORow,
    RowResult,
    SimpleRow,
)
from bulkvid.orchestrator import db as _db

_log = get_logger("queue")


# Job statuses
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"
JOB_KILLED = "killed"

# Row statuses
ROW_PENDING = "pending"
ROW_PROCESSING = "processing"
ROW_DONE = "done"
ROW_FAILED = "failed"

# Tab discriminators
TAB_IMAGE_VO = "image_vo"
TAB_FOUR_IMAGES = "four_images_vo2"
TAB_SIMPLE = "simple"
TAB_CARTOON = "cartoon"

# Idempotency-key replay window. A submit POST that PA's frontend dropped on
# the way back to the client gets retried by the Apps Script, with the SAME
# key — we use that to return the original job_id instead of double-enqueueing.
# 24h is comfortably larger than any plausible user retry interval; older rows
# are pruned opportunistically on every enqueue.
IDEMPOTENCY_TTL_SECONDS = 86_400


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id           TEXT PRIMARY KEY,
    user_email       TEXT NOT NULL,
    sheet_id         TEXT NOT NULL,
    worksheet        TEXT NOT NULL,
    tab_type         TEXT NOT NULL,
    status           TEXT NOT NULL,
    row_count        INTEGER NOT NULL,
    completed_rows   INTEGER NOT NULL DEFAULT 0,
    failed_rows      INTEGER NOT NULL DEFAULT 0,
    cost_usd         REAL    NOT NULL DEFAULT 0.0,
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,
    error            TEXT
);

CREATE TABLE IF NOT EXISTS row_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       TEXT NOT NULL,
    row_num      INTEGER NOT NULL,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    result       TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key          TEXT NOT NULL,
    user_email   TEXT NOT NULL,
    job_id       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    created_ts   REAL NOT NULL,
    PRIMARY KEY (user_email, key)
);

CREATE INDEX IF NOT EXISTS idx_row_queue_status      ON row_queue(status);
CREATE INDEX IF NOT EXISTS idx_row_queue_job         ON row_queue(job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status           ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_idempotency_keys_ts   ON idempotency_keys(created_ts);
"""


class QueueBusy(RuntimeError):
    """Raised when SQLite returns ``OperationalError`` while we hold (or are
    waiting on) the write lock — i.e. the queue is genuinely too busy to
    accept the write right now. Mapped to HTTP 503 + ``Retry-After`` in the
    route layer so the Apps Script client retries instead of bubbling a 500.

    Defense in depth: we have not actually observed ``OperationalError`` in
    prod (see ``_plans/2026-06-04-fix-sidebar-500s.md``), but the mapping
    means the day it does happen the user sees a polite retry-prompt rather
    than a cryptic toast.
    """


@dataclass
class Job:
    job_id: str
    user_email: str
    sheet_id: str
    worksheet: str
    tab_type: str
    status: str
    row_count: int
    completed_rows: int
    failed_rows: int
    cost_usd: float
    created_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None


@dataclass
class QueuedRow:
    id: int
    job_id: str
    row_num: int
    payload: dict[str, Any]
    status: str


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _new_job_id() -> str:
    return f"job-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _row_to_payload(
    row: ImageVORow | FourImagesVO2Row | SimpleRow | CartoonRow, tab: str
) -> str:
    data = asdict(row)
    data["__tab__"] = tab
    return json.dumps(data, ensure_ascii=False)


def _payload_to_row(
    payload_json: str,
) -> ImageVORow | FourImagesVO2Row | SimpleRow | CartoonRow:
    data = json.loads(payload_json)
    tab = data.pop("__tab__", TAB_IMAGE_VO)
    if tab == TAB_FOUR_IMAGES:
        return FourImagesVO2Row(**data)
    if tab == TAB_SIMPLE:
        return SimpleRow(**data)
    if tab == TAB_CARTOON:
        return CartoonRow(**data)
    return ImageVORow(**data)


class JobQueue:
    """SQLite job queue. Synchronous methods; async wrappers via ``asyncio.to_thread``."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        sync_url: str = "",
        auth_token: str = "",
        sync_interval_seconds: float = 1.0,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # Single shared connection per instance. ``_db.connect`` returns
        # plain sqlite3 when ``sync_url`` is empty (dev/tests) and a libsql
        # embedded replica otherwise (prod). The remainder of this class
        # uses only the DB-API 2.0 surface both backends support, so it
        # treats the connection identically. See
        # ``_plans/2026-06-04-migrate-to-hf-spaces-turso.md``.
        self._conn = _db.connect(
            self._db_path,
            sync_url=sync_url,
            auth_token=auth_token,
            sync_interval_seconds=sync_interval_seconds,
        )
        # ``row_factory = sqlite3.Row`` is a sqlite3-only extension; libsql
        # also exposes it (DB-API 2.0 + sqlite3 compat). If a future libsql
        # build drops it we fall back to the plain tuple cursor and adapt at
        # read sites — flagged so the failure is loud, not silent.
        try:
            self._conn.row_factory = sqlite3.Row
        except AttributeError:
            _log.warning("row_factory_unsupported", note="dict-like row access disabled")
        self._conn.executescript("PRAGMA journal_mode=WAL;")
        self._conn.executescript(_SCHEMA)
        self._lock = asyncio.Lock()
        _log.info("queue_init", db_path=str(self._db_path))

    # ── Transactions ────────────────────────────────────────────────────────

    @contextmanager
    def _tx(self) -> Iterator[None]:
        """Open an explicit write transaction.

        The connection is in autocommit mode (``isolation_level=None``), so the
        plain ``with self._conn:`` block does NOT issue ``BEGIN`` — it only
        runs an end-of-block commit/rollback that has no effect when no
        transaction is open. Multi-statement blocks were therefore committing
        per-statement, leaving the door open for another process (the worker)
        to interleave between our statements and observe torn intermediate
        state. Wrapping the block in ``BEGIN IMMEDIATE`` ... ``COMMIT`` closes
        that gap. IMMEDIATE acquires the write lock up front so we don't fail
        mid-transaction with SQLITE_BUSY on the first write.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # ── Sync helpers (called via to_thread) ─────────────────────────────────

    def _active_duplicate_row_nums(
        self, sheet_id: str, worksheet: str, row_nums: list[int]
    ) -> set[int]:
        """Of ``row_nums``, which are already pending/processing for this
        (sheet, worksheet) in a still-active job — i.e. would double-process and
        overwrite. Used to dedup at enqueue so a row never runs twice at once."""
        if not row_nums:
            return set()
        placeholders = ",".join("?" for _ in row_nums)
        cur = self._conn.execute(
            "SELECT DISTINCT rq.row_num FROM row_queue rq "
            "JOIN jobs j ON j.job_id = rq.job_id "
            "WHERE j.sheet_id = ? AND j.worksheet = ? "
            "AND j.status IN (?, ?) AND rq.status IN (?, ?) "
            f"AND rq.row_num IN ({placeholders})",
            (
                sheet_id, worksheet, JOB_QUEUED, JOB_RUNNING,
                ROW_PENDING, ROW_PROCESSING, *row_nums,
            ),
        )
        return {r["row_num"] for r in cur.fetchall()}

    def _lookup_idempotency_sync(
        self, user_email: str, key: str
    ) -> str | None:
        """Return the ``job_id`` previously recorded for this (user, key), or
        ``None`` if the pair has not been seen. Scoped per user so user A
        cannot replay user B's key."""
        cur = self._conn.execute(
            "SELECT job_id FROM idempotency_keys "
            "WHERE user_email = ? AND key = ?",
            (user_email, key),
        )
        row = cur.fetchone()
        return row["job_id"] if row is not None else None

    def _prune_idempotency_sync(self, *, ttl_seconds: int) -> int:
        """Delete idempotency rows older than ``ttl_seconds``. Cheap; one
        indexed range delete. Called opportunistically on each enqueue so the
        table can't grow unbounded even with no explicit maintenance."""
        cutoff = time.time() - ttl_seconds
        cur = self._conn.execute(
            "DELETE FROM idempotency_keys WHERE created_ts < ?", (cutoff,)
        )
        return cur.rowcount

    def _enqueue_sync(
        self,
        *,
        user_email: str,
        sheet_id: str,
        worksheet: str,
        tab_type: str,
        rows: list[ImageVORow] | list[FourImagesVO2Row] | list[SimpleRow] | list[CartoonRow],
        idempotency_key: str | None = None,
    ) -> tuple[str, bool]:
        """Enqueue ``rows`` and return ``(job_id, idempotency_hit)``.

        If ``idempotency_key`` is supplied and matches a previously recorded
        (user, key) pair, the prior ``job_id`` is returned with
        ``idempotency_hit=True`` — no new rows are inserted. This makes the
        submit POST safe to retry when PA's frontend drops the response on
        the way back to the Apps Script (see
        ``_plans/2026-06-04-submit-500-defensive-fix.md``).
        """
        try:
            if idempotency_key:
                # Cheap read OUTSIDE the write tx — if hit, we can short-circuit
                # without taking the write lock at all. Concurrent submits with
                # the same key are still safe: the PRIMARY KEY (user_email, key)
                # makes the second INSERT fail, and we re-read on conflict.
                prior = self._lookup_idempotency_sync(user_email, idempotency_key)
                if prior is not None:
                    return prior, True

            job_id = _new_job_id()
            now = _now_iso()
            with self._tx():
                # Re-check inside the tx — closes the race where two concurrent
                # retries of the same key both miss the pre-check above.
                if idempotency_key:
                    prior = self._lookup_idempotency_sync(user_email, idempotency_key)
                    if prior is not None:
                        return prior, True

                # Drop rows already queued/processing for this sheet+worksheet
                # in an active job, so a double-submit or overlapping batch
                # can't reprocess and overwrite a row.
                dupes = self._active_duplicate_row_nums(
                    sheet_id, worksheet, [r.row_num for r in rows]
                )
                kept = [r for r in rows if r.row_num not in dupes]
                # If nothing is left to do, record a finished job (0 rows)
                # rather than a job that would hang forever as queued 0/0.
                status = JOB_QUEUED if kept else JOB_COMPLETED
                finished = None if kept else now
                self._conn.execute(
                    "INSERT INTO jobs "
                    "(job_id, user_email, sheet_id, worksheet, tab_type, status, "
                    "row_count, created_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        job_id, user_email, sheet_id, worksheet, tab_type, status,
                        len(kept), now, finished,
                    ),
                )
                if kept:
                    self._conn.executemany(
                        "INSERT INTO row_queue (job_id, row_num, payload, status) "
                        "VALUES (?,?,?,?)",
                        [
                            (job_id, r.row_num, _row_to_payload(r, tab_type), ROW_PENDING)
                            for r in kept
                        ],
                    )
                if idempotency_key:
                    # Recorded INSIDE the same tx as the jobs/row_queue inserts:
                    # a crash mid-write rolls back both, so we never end up
                    # with an idempotency row pointing at a non-existent job.
                    self._conn.execute(
                        "INSERT INTO idempotency_keys "
                        "(key, user_email, job_id, created_at, created_ts) "
                        "VALUES (?,?,?,?,?)",
                        (idempotency_key, user_email, job_id, now, time.time()),
                    )
            # Opportunistic cleanup AFTER the tx commits, so a slow prune
            # never blocks an enqueue. Failures here are non-fatal.
            try:
                pruned = self._prune_idempotency_sync(
                    ttl_seconds=IDEMPOTENCY_TTL_SECONDS
                )
                if pruned:
                    _log.debug("idempotency_pruned", removed=pruned)
            except sqlite3.OperationalError:
                pass
        except sqlite3.OperationalError as e:
            raise QueueBusy(str(e)) from e

        if dupes:
            _log.info(
                "enqueue_skipped_duplicates",
                job_id=job_id,
                skipped=len(dupes),
                row_nums=sorted(dupes),
            )
        return job_id, False

    def _claim_next_row_sync(self) -> QueuedRow | None:
        """Atomically pull the next pending row and mark it processing.

        Only claims rows whose parent job is still active (queued/running), so
        killing a job — or clearing the queue — immediately stops its pending
        rows instead of the worker draining them anyway.
        """
        with self._tx():
            cur = self._conn.execute(
                "SELECT rq.id, rq.job_id, rq.row_num, rq.payload "
                "FROM row_queue rq JOIN jobs j ON j.job_id = rq.job_id "
                "WHERE rq.status = ? AND j.status IN (?, ?) "
                "ORDER BY rq.id ASC LIMIT 1",
                (ROW_PENDING, JOB_QUEUED, JOB_RUNNING),
            )
            row = cur.fetchone()
            if row is None:
                return None
            now = _now_iso()
            self._conn.execute(
                "UPDATE row_queue SET status = ?, started_at = ? WHERE id = ?",
                (ROW_PROCESSING, now, row["id"]),
            )
            # Mark the parent job as running on first claim if still queued.
            self._conn.execute(
                "UPDATE jobs SET status = ?, started_at = COALESCE(started_at, ?) "
                "WHERE job_id = ? AND status = ?",
                (JOB_RUNNING, now, row["job_id"], JOB_QUEUED),
            )
            return QueuedRow(
                id=row["id"],
                job_id=row["job_id"],
                row_num=row["row_num"],
                payload=json.loads(row["payload"]),
                status=ROW_PROCESSING,
            )

    def _record_result_sync(self, queue_id: int, result: RowResult) -> None:
        result_json = json.dumps(
            {
                "row_num": result.row_num,
                "status": result.status,
                "video_urls": result.video_urls,
                "cost_usd": result.cost_usd,
                "elapsed_seconds": result.elapsed_seconds,
                "error": result.error,
                "metadata": result.metadata,
            },
            ensure_ascii=False,
        )
        ok = result.status == "SUCCESS" or result.status == "ZAPCAP_FAILED_KEPT_NO_CAPTIONS"
        with self._tx():
            cur = self._conn.execute(
                "SELECT job_id FROM row_queue WHERE id = ?", (queue_id,)
            )
            row = cur.fetchone()
            if row is None:
                return
            job_id = row["job_id"]
            self._conn.execute(
                "UPDATE row_queue "
                "SET status = ?, finished_at = ?, result = ? WHERE id = ?",
                (ROW_DONE if ok else ROW_FAILED, _now_iso(), result_json, queue_id),
            )
            if ok:
                self._conn.execute(
                    "UPDATE jobs SET completed_rows = completed_rows + 1, "
                    "cost_usd = cost_usd + ? WHERE job_id = ?",
                    (result.cost_usd, job_id),
                )
            else:
                self._conn.execute(
                    "UPDATE jobs SET failed_rows = failed_rows + 1, "
                    "cost_usd = cost_usd + ? WHERE job_id = ?",
                    (result.cost_usd, job_id),
                )
            # Maybe finalize the job.
            self._conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ? "
                "WHERE job_id = ? AND completed_rows + failed_rows >= row_count "
                "AND status = ?",
                (JOB_COMPLETED, _now_iso(), job_id, JOB_RUNNING),
            )

    def _get_job_sync(self, job_id: str) -> Job | None:
        cur = self._conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return Job(**{k: row[k] for k in row.keys()})

    def _list_jobs_sync(
        self, *, user_email: str | None = None, limit: int = 50
    ) -> list[Job]:
        if user_email:
            cur = self._conn.execute(
                "SELECT * FROM jobs WHERE user_email = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (user_email, limit),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        return [Job(**{k: row[k] for k in row.keys()}) for row in cur.fetchall()]

    def _list_rows_sync(self, job_id: str) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT row_num, status, started_at, result FROM row_queue "
            "WHERE job_id = ? ORDER BY row_num",
            (job_id,),
        )
        out: list[dict[str, Any]] = []
        for r in cur.fetchall():
            result = json.loads(r["result"]) if r["result"] else {}
            out.append(
                {
                    "row_num": r["row_num"],
                    "status": r["status"],
                    # ``started_at`` is the moment the worker claimed the
                    # row — the sidebar uses it for the live elapsed
                    # counter. ``current_step`` is filled in at the route
                    # layer from the per-job log (it'd be wrong to import
                    # the log reader here, that's a presentation concern).
                    "started_at": r["started_at"],
                    "error": result.get("error"),
                    "video_urls": result.get("video_urls", []),
                }
            )
        return out

    def _kill_job_sync(self, job_id: str) -> bool:
        try:
            with self._tx():
                cur = self._conn.execute(
                    "UPDATE jobs SET status = ?, finished_at = ? "
                    "WHERE job_id = ? AND status IN (?, ?)",
                    (JOB_KILLED, _now_iso(), job_id, JOB_QUEUED, JOB_RUNNING),
                )
                return cur.rowcount > 0
        except sqlite3.OperationalError as e:
            raise QueueBusy(str(e)) from e

    def _kill_all_sync(self, user_email: str | None = None) -> int:
        """Kill every active (queued/running) job — for one user, or all when
        ``user_email`` is None (admin). In-flight rows finish; pending rows stop
        being claimed (see ``_claim_next_row_sync``)."""
        try:
            with self._tx():
                if user_email:
                    cur = self._conn.execute(
                        "UPDATE jobs SET status = ?, finished_at = ? "
                        "WHERE user_email = ? AND status IN (?, ?)",
                        (JOB_KILLED, _now_iso(), user_email, JOB_QUEUED, JOB_RUNNING),
                    )
                else:
                    cur = self._conn.execute(
                        "UPDATE jobs SET status = ?, finished_at = ? "
                        "WHERE status IN (?, ?)",
                        (JOB_KILLED, _now_iso(), JOB_QUEUED, JOB_RUNNING),
                    )
                return cur.rowcount
        except sqlite3.OperationalError as e:
            raise QueueBusy(str(e)) from e

    def _recover_orphaned_rows_sync(self) -> int:
        """On worker startup, return PROCESSING rows back to PENDING."""
        with self._tx():
            cur = self._conn.execute(
                "UPDATE row_queue SET status = ?, started_at = NULL "
                "WHERE status = ?",
                (ROW_PENDING, ROW_PROCESSING),
            )
            return cur.rowcount

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # ── Async API ───────────────────────────────────────────────────────────

    async def enqueue(
        self,
        *,
        user_email: str,
        sheet_id: str,
        worksheet: str,
        tab_type: str,
        rows: list[ImageVORow] | list[FourImagesVO2Row] | list[SimpleRow] | list[CartoonRow],
        idempotency_key: str | None = None,
    ) -> str:
        """Enqueue ``rows`` and return the resulting ``job_id``.

        When ``idempotency_key`` is supplied and the (user, key) pair has been
        recorded by a prior call, the **prior** ``job_id`` is returned and no
        new rows are inserted — so the Apps Script can safely retry a submit
        whose response PA's frontend dropped without creating a duplicate job
        (see ``_plans/2026-06-04-submit-500-defensive-fix.md``).
        """
        async with self._lock:
            job_id, hit = await asyncio.to_thread(
                self._enqueue_sync,
                user_email=user_email,
                sheet_id=sheet_id,
                worksheet=worksheet,
                tab_type=tab_type,
                rows=rows,
                idempotency_key=idempotency_key,
            )
        if hit:
            _log.info(
                "idempotency_hit",
                job_id=job_id,
                user_email=user_email,
                key=idempotency_key,
            )
        else:
            _log.info(
                "job_enqueued",
                job_id=job_id,
                user_email=user_email,
                tab_type=tab_type,
                row_count=len(rows),
                idempotency_key=idempotency_key or "",
            )
        return job_id

    async def claim_next_row(self) -> QueuedRow | None:
        async with self._lock:
            return await asyncio.to_thread(self._claim_next_row_sync)

    async def record_result(self, queue_id: int, result: RowResult) -> None:
        async with self._lock:
            await asyncio.to_thread(self._record_result_sync, queue_id, result)

    async def get_job(self, job_id: str) -> Job | None:
        return await asyncio.to_thread(self._get_job_sync, job_id)

    async def list_jobs(
        self, *, user_email: str | None = None, limit: int = 50
    ) -> list[Job]:
        return await asyncio.to_thread(self._list_jobs_sync, user_email=user_email, limit=limit)

    async def list_rows(self, job_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_rows_sync, job_id)

    async def kill_job(self, job_id: str) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._kill_job_sync, job_id)

    async def kill_all_jobs(self, *, user_email: str | None = None) -> int:
        """Kill all active jobs (one user, or everyone for admins). Returns the
        number of jobs killed. Backs the sidebar's "Stop all / Clear queue"."""
        async with self._lock:
            n = await asyncio.to_thread(self._kill_all_sync, user_email=user_email)
        _log.info("kill_all_jobs", user_email=user_email or "ALL", killed=n)
        return n

    async def recover_orphaned_rows(self) -> int:
        """Call on worker startup: rows stuck in PROCESSING are released."""
        async with self._lock:
            n = await asyncio.to_thread(self._recover_orphaned_rows_sync)
        if n > 0:
            _log.warning("orphaned_rows_recovered", count=n)
        return n


def payload_to_row(
    payload: dict[str, Any],
) -> ImageVORow | FourImagesVO2Row | SimpleRow | CartoonRow:
    """Reconstruct the typed row dataclass from a queue payload dict."""
    tab = payload.pop("__tab__", TAB_IMAGE_VO)
    if tab == TAB_FOUR_IMAGES:
        return FourImagesVO2Row(**payload)
    if tab == TAB_SIMPLE:
        return SimpleRow(**payload)
    if tab == TAB_CARTOON:
        return CartoonRow(**payload)
    return ImageVORow(**payload)
