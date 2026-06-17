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
import hashlib
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from bulkvid.logging import get_logger
from bulkvid.models.row import (
    AvatarRow,
    CardChoice,
    CartoonRow,
    FourImagesVO2Row,
    ImageVORow,
    RowResult,
    SimpleRow,
    SimpleX4Row,
    TextOnImgRow,
    YtCartoonRow,
)
from bulkvid.orchestrator import db as _db

_log = get_logger("queue")

# Return type preserved across the _run_db resilience wrapper.
_T = TypeVar("_T")


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
TAB_YT_CARTOON = "yt_cartoon"
TAB_SIMPLE_X4 = "simple_x4"
TAB_TEXT_ON_IMG = "text_on_img"
TAB_AVATAR = "avatar"

# Idempotency-key replay window. A submit POST that PA's frontend dropped on
# the way back to the client gets retried by the Apps Script, with the SAME
# key — we use that to return the original job_id instead of double-enqueueing.
# 24h is comfortably larger than any plausible user retry interval; older rows
# are pruned opportunistically on every enqueue.
IDEMPOTENCY_TTL_SECONDS = 86_400


# ── Web-path DB resilience (Turso flap hardening) ───────────────────────────
#
# The submit/poll endpoints talk to Turso (remote libSQL) over HTTPS — every
# statement is a network round-trip that can flap. The WORKER side already
# survives this (hard ``asyncio.wait_for`` timeout + broad ``except`` + retry
# buffer, runner.py); the web path historically did not, so a single flap
# during ``enqueue`` bubbled as a bare HTTP 500 and a wedged connection stayed
# wedged until the process restarted. ``JobQueue._run_db`` mirrors the worker's
# discipline on the web path: time-box each call, and on ANY failure throw the
# connection away, open a fresh one, and retry before giving up with a 503.
# Plan ``_plans/2026-06-17-submit-500s-turso-resilience.md``.
#
# 15 s is loose enough to absorb a slow Turso roundtrip (cold-start spikes
# ~5-10 s) and tight enough to beat the Apps Script's 30 s UrlFetch cap. All
# three are env-overridable for a per-deploy tune without a code change.
_DB_CALL_TIMEOUT_SECONDS = float(
    os.environ.get("BULKVID_DB_CALL_TIMEOUT_SECONDS") or 15.0
)
_DB_MAX_ATTEMPTS = int(os.environ.get("BULKVID_DB_MAX_ATTEMPTS") or 3)
# Backoff between attempts (after a discard-and-reconnect). Clamped to the last
# entry if attempts ever exceed the schedule length.
_DB_RETRY_BACKOFF_SECONDS = (0.5, 1.0, 2.0)


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


class QueueUnavailable(QueueBusy):
    """Raised when a web-facing DB call still fails after the full
    timeout + reconnect + retry cycle in ``_run_db`` — i.e. Turso is
    genuinely unreachable right now, not merely busy. Subclasses
    ``QueueBusy`` so the existing route ``except QueueBusy`` blocks map it to
    HTTP 503 + ``Retry-After`` and the Apps Script retries it safely (the
    submit is idempotent via the idempotency key + deterministic job_id).
    This is the class that replaces the old bare HTTP 500 on a Turso flap.
    Plan ``_plans/2026-06-17-submit-500s-turso-resilience.md``.
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


def _deterministic_job_id(user_email: str, idempotency_key: str) -> str:
    """Stable ``job_id`` derived from (user, idempotency_key).

    A retried submit (same key) recomputes the SAME id, so the
    ``INSERT OR IGNORE`` writes in ``_enqueue_sync`` make replay exactly-once
    even if the prior attempt only partially wrote before a Turso flap. The
    email is included so two users can never collide on a shared key, and so a
    forged key still can't address another user's job (ownership is also
    enforced at the route). 16 hex chars = 64 bits — collision-safe for this
    table's lifetime. Plan ``_plans/2026-06-17-submit-500s-turso-resilience.md``."""
    digest = hashlib.sha256(f"{user_email}\n{idempotency_key}".encode()).hexdigest()
    return f"job-{digest[:16]}"


def _row_to_payload(
    row: ImageVORow | FourImagesVO2Row | SimpleRow | CartoonRow | YtCartoonRow | SimpleX4Row | TextOnImgRow | AvatarRow,
    tab: str,
) -> str:
    data = asdict(row)
    data["__tab__"] = tab
    return json.dumps(data, ensure_ascii=False)


def _hydrate_simple_x4(data: dict[str, Any]) -> SimpleX4Row:
    """Rebuild a SimpleX4Row, restoring the nested CardChoice dataclasses
    that ``asdict`` flattens to dicts."""
    raw_cards = data.pop("cards", []) or []
    cards = [
        CardChoice(
            template_id=str(c.get("template_id") or ""),
            cta=str(c.get("cta") or ""),
        )
        for c in raw_cards
    ]
    return SimpleX4Row(cards=cards, **data)


def _payload_to_row(
    payload_json: str,
) -> ImageVORow | FourImagesVO2Row | SimpleRow | CartoonRow | YtCartoonRow | SimpleX4Row | TextOnImgRow | AvatarRow:
    data = json.loads(payload_json)
    tab = data.pop("__tab__", TAB_IMAGE_VO)
    if tab == TAB_FOUR_IMAGES:
        return FourImagesVO2Row(**data)
    if tab == TAB_SIMPLE:
        return SimpleRow(**data)
    if tab == TAB_CARTOON:
        return CartoonRow(**data)
    if tab == TAB_YT_CARTOON:
        return YtCartoonRow(**data)
    if tab == TAB_SIMPLE_X4:
        return _hydrate_simple_x4(data)
    if tab == TAB_TEXT_ON_IMG:
        return TextOnImgRow(**data)
    if tab == TAB_AVATAR:
        return AvatarRow(**data)
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
        # Connection params stashed so ``_reconnect_sync`` can rebuild a fresh
        # connection after a Turso flap wedges the current one. ``_db.connect``
        # returns plain sqlite3 when ``sync_url`` is empty (dev/tests) and a
        # libsql remote connection otherwise (prod). The remainder of this
        # class uses only the DB-API 2.0 surface both backends support, so it
        # treats the connection identically. See
        # ``_plans/2026-06-04-migrate-to-hf-spaces-turso.md`` and
        # ``_plans/2026-06-17-submit-500s-turso-resilience.md``.
        self._sync_url = sync_url
        self._auth_token = auth_token
        self._sync_interval_seconds = sync_interval_seconds
        self._conn = self._open_connection()
        self._lock = asyncio.Lock()
        _log.info("queue_init", db_path=str(self._db_path))

    def _open_connection(self) -> Any:
        """Open and fully configure a DB connection (row factory, WAL, schema).

        Used at construction AND by ``_reconnect_sync`` — keeping the setup in
        one place means a reconnected handle is configured identically to the
        original (same row factory, same schema/index guarantees)."""
        conn = _db.connect(
            self._db_path,
            sync_url=self._sync_url,
            auth_token=self._auth_token,
            sync_interval_seconds=self._sync_interval_seconds,
        )
        # ``row_factory = sqlite3.Row`` is a sqlite3-only extension; libsql
        # also exposes it (DB-API 2.0 + sqlite3 compat). If a future libsql
        # build drops it we fall back to the plain tuple cursor and adapt at
        # read sites — flagged so the failure is loud, not silent.
        try:
            conn.row_factory = sqlite3.Row
        except AttributeError:
            _log.warning("row_factory_unsupported", note="dict-like row access disabled")
        conn.executescript("PRAGMA journal_mode=WAL;")
        conn.executescript(_SCHEMA)
        # Unique index for idempotent enqueue (added 2026-06-17). Kept OUT of
        # ``_SCHEMA`` and guarded on its own: a legacy ``row_queue`` that
        # somehow holds a duplicate ``(job_id, row_num)`` would make
        # ``CREATE UNIQUE INDEX`` fail, and that MUST NOT take the whole
        # service down at boot. If it fails we log and continue — INSERT OR
        # IGNORE then degrades to the runtime ``_active_duplicate_row_nums``
        # guard (today's behaviour), no worse than before. Fresh DBs get the
        # index and thus DB-level exactly-once on retry. Plan
        # ``_plans/2026-06-17-submit-500s-turso-resilience.md`` §Change 2.
        try:
            conn.executescript(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_row_queue_job_rownum "
                "ON row_queue(job_id, row_num);"
            )
        except Exception as e:    # never fail boot on a legacy duplicate row
            _log.warning(
                "unique_index_create_failed",
                index="idx_row_queue_job_rownum",
                error=str(e)[:200],
            )
        return conn

    def _reconnect_sync(self, *, reason: str) -> None:
        """Throw away the current connection and open a fresh one.

        Cheap-and-dumb on purpose: we never try to *heal* a half-dead libsql
        socket, we replace it. ``self._conn`` is swapped to the new handle
        first so the old one can be closed best-effort (a timed-out worker
        thread may still hold a reference, so a close failure is swallowed).
        If opening fails (Turso fully down) the exception propagates and
        ``_run_db`` counts it as a failed attempt. Plan
        ``_plans/2026-06-17-submit-500s-turso-resilience.md`` §Change 1."""
        old = self._conn
        self._conn = self._open_connection()
        # Best-effort close: a timed-out worker thread may still hold the old
        # handle, so swallow any failure rather than crash the reconnect.
        with suppress(Exception):
            old.close()
        _log.warning("db_reconnect", reason=reason)

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
        ``idempotency_hit=True`` — no new rows are inserted.

        Idempotent BY CONSTRUCTION (plan 2026-06-17): when a key is present the
        ``job_id`` is derived deterministically from (user_email, key), and the
        jobs / row_queue / idempotency writes all use ``INSERT OR IGNORE``. So a
        retry of a submit whose response the backend dropped — OR whose first
        attempt only PARTIALLY wrote before a Turso flap — recomputes the same
        ids and simply fills in whatever is missing, with zero duplicate rows
        (hence zero duplicate videos / double paid-API spend). Without a key
        (legacy clients) the id is random and the runtime
        ``_active_duplicate_row_nums`` guard is the only dedup, as before.
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

            job_id = (
                _deterministic_job_id(user_email, idempotency_key)
                if idempotency_key
                else _new_job_id()
            )
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
                # INSERT OR IGNORE: a retry that recomputes the same
                # deterministic ``job_id`` no-ops on the PK instead of erroring,
                # and the row_count from the FIRST attempt is preserved (the
                # ignored re-insert doesn't clobber it). Plan 2026-06-17.
                self._conn.execute(
                    "INSERT OR IGNORE INTO jobs "
                    "(job_id, user_email, sheet_id, worksheet, tab_type, status, "
                    "row_count, created_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        job_id, user_email, sheet_id, worksheet, tab_type, status,
                        len(kept), now, finished,
                    ),
                )
                if kept:
                    self._insert_rows_batched(job_id, tab_type, kept)
                if idempotency_key:
                    # Recorded alongside the jobs/row_queue inserts. INSERT OR
                    # IGNORE so a retry (or a concurrent same-key submit racing
                    # past the lookups above) can't trip the PRIMARY KEY.
                    self._conn.execute(
                        "INSERT OR IGNORE INTO idempotency_keys "
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

    def _insert_rows_batched(
        self,
        job_id: str,
        tab_type: str,
        kept: list[Any],
    ) -> None:
        """Insert ``kept`` rows with a single multi-row statement per chunk.

        Why not ``executemany``: the libsql shim unrolls remote-mode
        ``executemany`` into N separate ``execute()`` calls (commit 5a74f62),
        so an N-row submit was N network round-trips — N chances for a Turso
        flap to fail the whole enqueue. A single
        ``INSERT OR IGNORE ... VALUES (...),(...),...`` is ONE round-trip per
        chunk and ONE statement (so it dodges the remote-executemany no-op
        bug). ``INSERT OR IGNORE`` on the ``(job_id, row_num)`` unique index
        makes a retry fill only the missing rows — never a duplicate. Chunked
        at 100 rows to stay clear of any statement-size / bind-param ceiling.
        Plan ``_plans/2026-06-17-submit-500s-turso-resilience.md`` §Change 3."""
        chunk = 100
        sql_head = (
            "INSERT OR IGNORE INTO row_queue (job_id, row_num, payload, status) "
            "VALUES "
        )
        for start in range(0, len(kept), chunk):
            part = kept[start:start + chunk]
            placeholders = ",".join("(?,?,?,?)" for _ in part)
            params: list[Any] = []
            for r in part:
                params.extend(
                    (job_id, r.row_num, _row_to_payload(r, tab_type), ROW_PENDING)
                )
            self._conn.execute(sql_head + placeholders, params)

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
            # Read current status as part of the SELECT so we can skip
            # the row entirely when it was killed mid-flight. Without
            # this guard, a processor that succeeded just after the
            # operator hit "Kill" would overwrite the killed row's
            # FAILED state back to DONE — silently undoing the user's
            # explicit action. Plan ``_plans/2026-06-14-stuck-processing-rows.md``
            # §B last-paragraph race note.
            cur = self._conn.execute(
                "SELECT job_id, status FROM row_queue WHERE id = ?",
                (queue_id,),
            )
            row = cur.fetchone()
            if row is None:
                return
            current_status = row["status"]
            if current_status in (ROW_DONE, ROW_FAILED):
                # Already terminal — either a duplicate retry of
                # record_result (Plan §A) or a kill landed between
                # processor start and result hand-back. Either way the
                # row is settled; do nothing.
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
            meta = result.get("metadata") or {}
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
                    # Surfaces which default-library template was picked
                    # for a blank-cell row. Empty when script_pattern was
                    # filled in or the selector didn't pick anything.
                    # Plan ``_plans/2026-06-07-overload-handling-and-template-defaults.md`` §B.
                    "chosen_template_id": meta.get("chosen_template_id") or "",
                }
            )
        return out

    def _user_queue_depth_sync(
        self, *, user_email: str | None,
    ) -> tuple[int, int, dict[str, int]]:
        """Count rows currently in-flight + waiting for one user (or all users
        when ``user_email`` is None — admin view).

        Returns ``(in_flight, queued, queued_per_tab)``:
          * ``in_flight``: rows whose status is ``ROW_PROCESSING`` and whose
            parent job is still active. These have a worker slot RIGHT NOW.
          * ``queued``: rows whose status is ``ROW_PENDING`` waiting for a
            worker slot. These will run as slots free.
          * ``queued_per_tab``: ``{tab_type: queued_count}`` over the queued
            set, so the route can compute a tab-weighted ETA from the median
            seconds-per-tab table.

        Cheap aggregate — two SELECTs against the indexed status column. Used
        by the sidebar's queue-status banner (chat 2026-06-09).
        """
        # We never log a user_email predicate when ``user_email is None`` so
        # admins see the entire fleet. Parameter list mirrors the existing
        # ``_kill_all_sync`` shape.
        if user_email is None:
            in_flight_cur = self._conn.execute(
                "SELECT COUNT(*) FROM row_queue rq "
                "JOIN jobs j ON j.job_id = rq.job_id "
                "WHERE rq.status = ? AND j.status IN (?, ?)",
                (ROW_PROCESSING, JOB_QUEUED, JOB_RUNNING),
            )
            queued_cur = self._conn.execute(
                "SELECT COUNT(*) FROM row_queue rq "
                "JOIN jobs j ON j.job_id = rq.job_id "
                "WHERE rq.status = ? AND j.status IN (?, ?)",
                (ROW_PENDING, JOB_QUEUED, JOB_RUNNING),
            )
            per_tab_cur = self._conn.execute(
                "SELECT j.tab_type, COUNT(*) AS n FROM row_queue rq "
                "JOIN jobs j ON j.job_id = rq.job_id "
                "WHERE rq.status = ? AND j.status IN (?, ?) "
                "GROUP BY j.tab_type",
                (ROW_PENDING, JOB_QUEUED, JOB_RUNNING),
            )
        else:
            in_flight_cur = self._conn.execute(
                "SELECT COUNT(*) FROM row_queue rq "
                "JOIN jobs j ON j.job_id = rq.job_id "
                "WHERE rq.status = ? AND j.status IN (?, ?) "
                "AND j.user_email = ?",
                (ROW_PROCESSING, JOB_QUEUED, JOB_RUNNING, user_email),
            )
            queued_cur = self._conn.execute(
                "SELECT COUNT(*) FROM row_queue rq "
                "JOIN jobs j ON j.job_id = rq.job_id "
                "WHERE rq.status = ? AND j.status IN (?, ?) "
                "AND j.user_email = ?",
                (ROW_PENDING, JOB_QUEUED, JOB_RUNNING, user_email),
            )
            per_tab_cur = self._conn.execute(
                "SELECT j.tab_type, COUNT(*) AS n FROM row_queue rq "
                "JOIN jobs j ON j.job_id = rq.job_id "
                "WHERE rq.status = ? AND j.status IN (?, ?) "
                "AND j.user_email = ? "
                "GROUP BY j.tab_type",
                (ROW_PENDING, JOB_QUEUED, JOB_RUNNING, user_email),
            )

        # COUNT(*) returns one row with one column — index access works for
        # both sqlite3.Row and the libsql ``_DictRow`` shim.
        in_flight_row = in_flight_cur.fetchone()
        queued_row = queued_cur.fetchone()
        in_flight = int(in_flight_row[0]) if in_flight_row is not None else 0
        queued = int(queued_row[0]) if queued_row is not None else 0

        queued_per_tab: dict[str, int] = {}
        for r in per_tab_cur.fetchall():
            tab = r["tab_type"] if hasattr(r, "keys") else r[0]
            n = r["n"] if hasattr(r, "keys") else r[1]
            try:
                queued_per_tab[str(tab)] = int(n)
            except (TypeError, ValueError):
                continue

        return in_flight, queued, queued_per_tab

    def _user_oldest_pending_row_age_seconds_sync(
        self, *, user_email: str | None,
    ) -> int | None:
        """Age in seconds of the OLDEST ROW_PENDING row for this user
        (or whole fleet when ``user_email is None``).

        Returns ``None`` when no rows are queued. Used by the sidebar's
        stuck-queued detector: if rows have been pending for longer than
        a few seconds AND the worker has nothing in flight, the worker
        has stalled out — we want to surface that visibly instead of the
        operator silently waiting.

        Row age = ``now() − jobs.created_at`` for the parent job, NOT the
        per-row ``started_at`` (which is ``NULL`` for pending rows by
        definition). Computed in SQL with ``strftime`` to avoid moving
        the row data into Python.
        """
        if user_email is None:
            cur = self._conn.execute(
                "SELECT MIN(strftime('%s', 'now') - strftime('%s', j.created_at)) "
                "AS age_s "
                "FROM row_queue rq JOIN jobs j ON j.job_id = rq.job_id "
                "WHERE rq.status = ? AND j.status IN (?, ?)",
                (ROW_PENDING, JOB_QUEUED, JOB_RUNNING),
            )
        else:
            cur = self._conn.execute(
                "SELECT MIN(strftime('%s', 'now') - strftime('%s', j.created_at)) "
                "AS age_s "
                "FROM row_queue rq JOIN jobs j ON j.job_id = rq.job_id "
                "WHERE rq.status = ? AND j.status IN (?, ?) "
                "AND j.user_email = ?",
                (ROW_PENDING, JOB_QUEUED, JOB_RUNNING, user_email),
            )
        row = cur.fetchone()
        if row is None:
            return None
        # MIN over an empty set yields NULL; column may also be None on
        # libsql when the join produced no rows.
        raw = row["age_s"] if hasattr(row, "keys") else row[0]
        if raw is None:
            return None
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return None

    def _eta_medians_sync(self, *, sample_size: int = 50) -> dict[str, float]:
        """Median ``finished_at - started_at`` in seconds, grouped by
        ``tab_type``, over the last ``sample_size`` successfully-done
        rows per tab. Used by the sidebar to show a rough ETA next to
        the elapsed counter.

        Plan: ``_plans/2026-06-04-sidebar-ux-overhaul.md`` §Phase 3.
        """
        # libSQL/sqlite both expose strftime, so we compute the delta
        # in SQL rather than parsing ISO strings in Python. The window
        # function lets us cap per-tab samples at ``sample_size``.
        cur = self._conn.execute(
            "WITH ranked AS ("
            "  SELECT "
            "    j.tab_type AS tab_type,"
            "    (strftime('%s', rq.finished_at) - strftime('%s', rq.started_at)) AS secs,"
            "    ROW_NUMBER() OVER ("
            "      PARTITION BY j.tab_type ORDER BY rq.finished_at DESC"
            "    ) AS rn"
            "  FROM row_queue rq JOIN jobs j ON j.job_id = rq.job_id "
            "  WHERE rq.status = ? "
            "    AND rq.started_at IS NOT NULL "
            "    AND rq.finished_at IS NOT NULL"
            ") "
            "SELECT tab_type, secs FROM ranked WHERE rn <= ? ORDER BY tab_type, secs",
            (ROW_DONE, sample_size),
        )
        # Bucket by tab_type and median client-side — SQLite has no
        # built-in MEDIAN aggregate.
        by_tab: dict[str, list[float]] = {}
        for r in cur.fetchall():
            tab = r["tab_type"]
            try:
                by_tab.setdefault(tab, []).append(float(r["secs"]))
            except (TypeError, ValueError):
                continue
        medians: dict[str, float] = {}
        for tab, secs in by_tab.items():
            if not secs:
                continue
            secs_sorted = sorted(secs)
            mid = len(secs_sorted) // 2
            if len(secs_sorted) % 2:
                medians[tab] = secs_sorted[mid]
            else:
                medians[tab] = (secs_sorted[mid - 1] + secs_sorted[mid]) / 2.0
        return medians

    def _kill_job_sync(self, job_id: str) -> tuple[bool, int]:
        """Kill a single job AND abort its PENDING/PROCESSING rows.

        Returns ``(jobs_killed_bool, rows_aborted_int)`` so the route can
        surface "Killed N rows" in the toast — before this fix, the kill
        only touched ``jobs.status`` and any in-flight row that had lost
        its ``record_result`` write (Turso flap) lingered in PROCESSING
        forever, showing "Starting.." in the sidebar with a growing
        elapsed timer. Plan ``_plans/2026-06-14-stuck-processing-rows.md``
        §B.

        Rows already DONE/FAILED are left alone — the kill only resolves
        in-flight uncertainty, not historical state. Bumps the parent
        job's ``failed_rows`` so the sidebar archive shows the right
        total ("75/100 killed by user" not "0 failed").
        """
        try:
            with self._tx():
                cur = self._conn.execute(
                    "UPDATE jobs SET status = ?, finished_at = ? "
                    "WHERE job_id = ? AND status IN (?, ?)",
                    (JOB_KILLED, _now_iso(), job_id, JOB_QUEUED, JOB_RUNNING),
                )
                jobs_killed = cur.rowcount > 0
                if not jobs_killed:
                    return False, 0
                rows_aborted = self._abort_rows_for_kill_sync(
                    job_id_filter=job_id, user_filter=None,
                )
                return True, rows_aborted
        except sqlite3.OperationalError as e:
            raise QueueBusy(str(e)) from e

    def _kill_all_sync(
        self, user_email: str | None = None
    ) -> tuple[int, int]:
        """Kill every active (queued/running) job — for one user, or all when
        ``user_email`` is None (admin) — AND abort their PENDING/PROCESSING
        rows. Returns ``(jobs_killed_count, rows_aborted_count)``. Plan
        ``_plans/2026-06-14-stuck-processing-rows.md`` §B."""
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
                jobs_killed = cur.rowcount
                if jobs_killed == 0:
                    return 0, 0
                rows_aborted = self._abort_rows_for_kill_sync(
                    job_id_filter=None, user_filter=user_email,
                )
                return jobs_killed, rows_aborted
        except sqlite3.OperationalError as e:
            raise QueueBusy(str(e)) from e

    def _abort_rows_for_kill_sync(
        self, *, job_id_filter: str | None, user_filter: str | None,
    ) -> int:
        """Mark every PENDING/PROCESSING row whose parent job is now KILLED
        as FAILED with a ``killed by user`` result payload. Caller is
        already inside ``_tx()``. Returns the count of rows touched.

        Mirrors the result-JSON shape ``_record_result_sync`` writes so
        ``_list_rows_sync`` and the sidebar's error renderer ("killed by
        user") read it without a second code path. Bumps ``failed_rows``
        on each parent job in the same UPDATE pass so the archive's
        ``done/total`` count adds up.

        The ``job_id_filter`` / ``user_filter`` mutually-exclusive pair
        mirrors ``_kill_job_sync`` vs ``_kill_all_sync`` — passing the
        same filter the parent UPDATE used means we only touch rows for
        jobs that JUST moved to KILLED in this transaction.
        """
        # Fetch row ids + numbers first so the UPDATE can embed each row's
        # ``row_num`` into the per-row result JSON. Cheap: indexed lookup
        # on ``rq.status`` + the parent join.
        now = _now_iso()
        if job_id_filter is not None:
            cur = self._conn.execute(
                "SELECT rq.id, rq.row_num FROM row_queue rq "
                "WHERE rq.job_id = ? AND rq.status IN (?, ?)",
                (job_id_filter, ROW_PENDING, ROW_PROCESSING),
            )
        elif user_filter is not None:
            cur = self._conn.execute(
                "SELECT rq.id, rq.row_num, rq.job_id FROM row_queue rq "
                "JOIN jobs j ON j.job_id = rq.job_id "
                "WHERE j.user_email = ? AND j.status = ? "
                "AND rq.status IN (?, ?)",
                (user_filter, JOB_KILLED, ROW_PENDING, ROW_PROCESSING),
            )
        else:
            cur = self._conn.execute(
                "SELECT rq.id, rq.row_num, rq.job_id FROM row_queue rq "
                "JOIN jobs j ON j.job_id = rq.job_id "
                "WHERE j.status = ? AND rq.status IN (?, ?)",
                (JOB_KILLED, ROW_PENDING, ROW_PROCESSING),
            )
        affected_ids: list[tuple[int, int, str]] = []
        for r in cur.fetchall():
            try:
                row_id = int(r["id"])
                row_num = int(r["row_num"])
            except (TypeError, ValueError, IndexError):
                continue
            # ``job_id_filter`` path didn't select rq.job_id (we already
            # have it as the filter). Fall back to it explicitly.
            if job_id_filter is not None:
                jid = job_id_filter
            else:
                try:
                    jid = str(r["job_id"])
                except (TypeError, ValueError, IndexError):
                    continue
            affected_ids.append((row_id, row_num, jid))
        if not affected_ids:
            return 0
        # Per-row UPDATE with the row's own row_num baked into the result
        # JSON. executemany would be cleaner but the result string varies
        # per row — and the typical kill touches O(10–100) rows, well
        # inside one libsql roundtrip's budget.
        per_job_failed_increment: dict[str, int] = {}
        for row_id, row_num, jid in affected_ids:
            result_json = json.dumps(
                {
                    "row_num": row_num,
                    "status": "KILLED_BY_USER",
                    "video_urls": [],
                    "cost_usd": 0.0,
                    "elapsed_seconds": 0.0,
                    "error": "killed by user",
                    "metadata": {},
                },
                ensure_ascii=False,
            )
            self._conn.execute(
                "UPDATE row_queue SET status = ?, finished_at = ?, "
                "result = ? WHERE id = ?",
                (ROW_FAILED, now, result_json, row_id),
            )
            per_job_failed_increment[jid] = (
                per_job_failed_increment.get(jid, 0) + 1
            )
        for jid, n in per_job_failed_increment.items():
            self._conn.execute(
                "UPDATE jobs SET failed_rows = failed_rows + ? "
                "WHERE job_id = ?",
                (n, jid),
            )
        return len(affected_ids)

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

    async def _run_db(
        self, fn: Callable[..., _T], *args: Any, op: str, **kwargs: Any
    ) -> _T:
        """Run a sync DB helper in a thread, time-boxed, with discard-and-
        reconnect retry — the web path's mirror of the worker's resilience.

        Each attempt runs ``fn`` under ``self._lock`` (so a reconnect can never
        race a concurrent op on the shared connection) with a hard
        ``asyncio.wait_for`` timeout. On ANY failure (libsql throws an
        undocumented grab-bag, so we catch broad ``Exception`` exactly like the
        worker does) we throw the connection away, open a fresh one, back off,
        and retry. After ``_DB_MAX_ATTEMPTS`` we raise ``QueueUnavailable`` (a
        ``QueueBusy``) → HTTP 503, which the Apps Script retries safely. A
        ``QueueBusy`` raised by ``fn`` itself (local sqlite OperationalError) is
        already classified and surfaces immediately. Plan
        ``_plans/2026-06-17-submit-500s-turso-resilience.md`` §Change 1."""
        last_exc: BaseException | None = None
        for attempt in range(_DB_MAX_ATTEMPTS):
            async with self._lock:
                try:
                    return await asyncio.wait_for(
                        asyncio.to_thread(fn, *args, **kwargs),
                        timeout=_DB_CALL_TIMEOUT_SECONDS,
                    )
                except QueueBusy:
                    raise
                except Exception as e:    # libsql throws an undocumented grab-bag
                    last_exc = e
                    attempts_left = _DB_MAX_ATTEMPTS - attempt - 1
                    _log.warning(
                        "db_call_retry",
                        op=op,
                        attempt=attempt + 1,
                        of=_DB_MAX_ATTEMPTS,
                        reason=type(e).__name__,
                        error=str(e)[:200],
                        attempts_left=attempts_left,
                    )
                    if attempts_left <= 0:
                        break
                    try:
                        await asyncio.wait_for(
                            asyncio.to_thread(
                                self._reconnect_sync,
                                reason=f"{op}:{type(e).__name__}",
                            ),
                            timeout=_DB_CALL_TIMEOUT_SECONDS,
                        )
                    except Exception as re:    # reconnect may itself flap
                        _log.warning(
                            "db_reconnect_failed", op=op, error=str(re)[:200]
                        )
            # Back off OUTSIDE the lock so other web ops aren't blocked while we
            # wait out the flap.
            await asyncio.sleep(
                _DB_RETRY_BACKOFF_SECONDS[
                    min(attempt, len(_DB_RETRY_BACKOFF_SECONDS) - 1)
                ]
            )
        raise QueueUnavailable(
            f"{op} failed after {_DB_MAX_ATTEMPTS} attempts: {last_exc}"
        )

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
        whose response the backend dropped without creating a duplicate job
        (see ``_plans/2026-06-04-submit-500-defensive-fix.md``). Wrapped in
        ``_run_db`` so a Turso flap mid-enqueue self-heals instead of bubbling
        a 500 (``_plans/2026-06-17-submit-500s-turso-resilience.md``).
        """
        job_id, hit = await self._run_db(
            self._enqueue_sync,
            user_email=user_email,
            sheet_id=sheet_id,
            worksheet=worksheet,
            tab_type=tab_type,
            rows=rows,
            idempotency_key=idempotency_key,
            op="enqueue",
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
        return await self._run_db(self._get_job_sync, job_id, op="get_job")

    async def list_jobs(
        self, *, user_email: str | None = None, limit: int = 50
    ) -> list[Job]:
        return await self._run_db(
            self._list_jobs_sync, user_email=user_email, limit=limit, op="list_jobs"
        )

    async def list_rows(self, job_id: str) -> list[dict[str, Any]]:
        return await self._run_db(self._list_rows_sync, job_id, op="list_rows")

    async def eta_medians(self, *, sample_size: int = 50) -> dict[str, float]:
        """Median per-tab row processing time in seconds. See
        ``_eta_medians_sync``."""
        return await self._run_db(
            self._eta_medians_sync, sample_size=sample_size, op="eta_medians"
        )

    async def user_queue_depth(
        self, *, user_email: str | None,
    ) -> tuple[int, int, dict[str, int]]:
        """Async wrapper for ``_user_queue_depth_sync``. Returns
        ``(in_flight, queued, queued_per_tab)``. ``user_email=None`` is the
        admin view (whole fleet)."""
        return await self._run_db(
            self._user_queue_depth_sync, user_email=user_email, op="user_queue_depth"
        )

    async def user_oldest_pending_row_age_seconds(
        self, *, user_email: str | None,
    ) -> int | None:
        """Async wrapper for ``_user_oldest_pending_row_age_seconds_sync``."""
        return await self._run_db(
            self._user_oldest_pending_row_age_seconds_sync,
            user_email=user_email,
            op="oldest_pending_age",
        )

    async def kill_job(self, job_id: str) -> tuple[bool, int]:
        """Kill ``job_id`` AND abort its pending/processing rows.

        Returns ``(jobs_killed_bool, rows_aborted_int)``. Plan
        ``_plans/2026-06-14-stuck-processing-rows.md`` §B.
        """
        async with self._lock:
            return await asyncio.to_thread(self._kill_job_sync, job_id)

    async def kill_all_jobs(
        self, *, user_email: str | None = None
    ) -> tuple[int, int]:
        """Kill all active jobs (one user, or everyone for admins) AND abort
        their pending/processing rows. Returns
        ``(jobs_killed_count, rows_aborted_count)``. Backs the sidebar's
        "Stop all / Clear queue".

        Plan ``_plans/2026-06-14-stuck-processing-rows.md`` §B.
        """
        async with self._lock:
            n_jobs, n_rows = await asyncio.to_thread(
                self._kill_all_sync, user_email=user_email,
            )
        _log.info(
            "kill_all_jobs",
            user_email=user_email or "ALL",
            killed=n_jobs,
            rows_aborted=n_rows,
        )
        return n_jobs, n_rows

    async def recover_orphaned_rows(self) -> int:
        """Call on worker startup: rows stuck in PROCESSING are released."""
        async with self._lock:
            n = await asyncio.to_thread(self._recover_orphaned_rows_sync)
        if n > 0:
            _log.warning("orphaned_rows_recovered", count=n)
        return n


def payload_to_row(
    payload: dict[str, Any],
) -> ImageVORow | FourImagesVO2Row | SimpleRow | CartoonRow | YtCartoonRow | SimpleX4Row | TextOnImgRow | AvatarRow:
    """Reconstruct the typed row dataclass from a queue payload dict."""
    tab = payload.pop("__tab__", TAB_IMAGE_VO)
    if tab == TAB_FOUR_IMAGES:
        return FourImagesVO2Row(**payload)
    if tab == TAB_SIMPLE:
        return SimpleRow(**payload)
    if tab == TAB_CARTOON:
        return CartoonRow(**payload)
    if tab == TAB_YT_CARTOON:
        return YtCartoonRow(**payload)
    if tab == TAB_SIMPLE_X4:
        return _hydrate_simple_x4(payload)
    if tab == TAB_TEXT_ON_IMG:
        return TextOnImgRow(**payload)
    if tab == TAB_AVATAR:
        return AvatarRow(**payload)
    return ImageVORow(**payload)
