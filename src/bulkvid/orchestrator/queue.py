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
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bulkvid.logging import get_logger
from bulkvid.models.row import FourImagesVO2Row, ImageVORow, RowResult

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

CREATE INDEX IF NOT EXISTS idx_row_queue_status ON row_queue(status);
CREATE INDEX IF NOT EXISTS idx_row_queue_job    ON row_queue(job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status      ON jobs(status);
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
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_job_id() -> str:
    return f"job-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _row_to_payload(row: ImageVORow | FourImagesVO2Row, tab: str) -> str:
    data = asdict(row)
    data["__tab__"] = tab
    return json.dumps(data, ensure_ascii=False)


def _payload_to_row(payload_json: str) -> ImageVORow | FourImagesVO2Row:
    data = json.loads(payload_json)
    tab = data.pop("__tab__", TAB_IMAGE_VO)
    if tab == TAB_FOUR_IMAGES:
        return FourImagesVO2Row(**data)
    return ImageVORow(**data)


class JobQueue:
    """SQLite job queue. Synchronous methods; async wrappers via ``asyncio.to_thread``."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # Single shared connection per instance; SQLite is fine for our concurrency.
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            timeout=30.0,
            isolation_level=None,        # autocommit
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript("PRAGMA journal_mode=WAL;")
        self._conn.executescript(_SCHEMA)
        self._lock = asyncio.Lock()
        _log.info("queue_init", db_path=str(self._db_path))

    # ── Sync helpers (called via to_thread) ─────────────────────────────────

    def _enqueue_sync(
        self,
        *,
        user_email: str,
        sheet_id: str,
        worksheet: str,
        tab_type: str,
        rows: list[ImageVORow] | list[FourImagesVO2Row],
    ) -> str:
        job_id = _new_job_id()
        now = _now_iso()
        with self._conn:                    # implicit transaction
            self._conn.execute(
                "INSERT INTO jobs "
                "(job_id, user_email, sheet_id, worksheet, tab_type, status, "
                "row_count, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (job_id, user_email, sheet_id, worksheet, tab_type, JOB_QUEUED, len(rows), now),
            )
            self._conn.executemany(
                "INSERT INTO row_queue (job_id, row_num, payload, status) VALUES (?,?,?,?)",
                [
                    (job_id, r.row_num, _row_to_payload(r, tab_type), ROW_PENDING)
                    for r in rows
                ],
            )
        return job_id

    def _claim_next_row_sync(self) -> QueuedRow | None:
        """Atomically pull the next pending row and mark it processing."""
        with self._conn:
            cur = self._conn.execute(
                "SELECT id, job_id, row_num, payload FROM row_queue "
                "WHERE status = ? ORDER BY id ASC LIMIT 1",
                (ROW_PENDING,),
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
        with self._conn:
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

    def _kill_job_sync(self, job_id: str) -> bool:
        with self._conn:
            cur = self._conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ? "
                "WHERE job_id = ? AND status IN (?, ?)",
                (JOB_KILLED, _now_iso(), job_id, JOB_QUEUED, JOB_RUNNING),
            )
            return cur.rowcount > 0

    def _recover_orphaned_rows_sync(self) -> int:
        """On worker startup, return PROCESSING rows back to PENDING."""
        with self._conn:
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
        rows: list[ImageVORow] | list[FourImagesVO2Row],
    ) -> str:
        async with self._lock:
            job_id = await asyncio.to_thread(
                self._enqueue_sync,
                user_email=user_email,
                sheet_id=sheet_id,
                worksheet=worksheet,
                tab_type=tab_type,
                rows=rows,
            )
        _log.info(
            "job_enqueued",
            job_id=job_id,
            user_email=user_email,
            tab_type=tab_type,
            row_count=len(rows),
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

    async def kill_job(self, job_id: str) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._kill_job_sync, job_id)

    async def recover_orphaned_rows(self) -> int:
        """Call on worker startup: rows stuck in PROCESSING are released."""
        async with self._lock:
            n = await asyncio.to_thread(self._recover_orphaned_rows_sync)
        if n > 0:
            _log.warning("orphaned_rows_recovered", count=n)
        return n


def payload_to_row(payload: dict[str, Any]) -> ImageVORow | FourImagesVO2Row:
    """Reconstruct the typed row dataclass from a queue payload dict."""
    tab = payload.pop("__tab__", TAB_IMAGE_VO)
    if tab == TAB_FOUR_IMAGES:
        return FourImagesVO2Row(**payload)
    return ImageVORow(**payload)
