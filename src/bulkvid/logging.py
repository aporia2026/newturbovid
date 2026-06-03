"""Namespaced structured logger.

Every log entry is JSON to stdout, tagged with the active batch_id, row_num,
and user_email when available (via contextvars), and emitted under a
``[bulkvid <stage>]`` namespace so grep / log search by stage works.

Rule 14: log values, not just events. Booleans without their value are useless.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import structlog

from bulkvid.config import get_settings

_batch_id_var: ContextVar[str | None] = ContextVar("batch_id", default=None)
_row_num_var: ContextVar[int | None] = ContextVar("row_num", default=None)
_user_email_var: ContextVar[str | None] = ContextVar("user_email", default=None)


def set_context(
    *,
    batch_id: str | None = None,
    row_num: int | None = None,
    user_email: str | None = None,
) -> None:
    if batch_id is not None:
        _batch_id_var.set(batch_id)
    if row_num is not None:
        _row_num_var.set(row_num)
    if user_email is not None:
        _user_email_var.set(user_email)


def _inject_context(_: Any, __: Any, event_dict: dict[str, Any]) -> dict[str, Any]:
    if (b := _batch_id_var.get()) is not None:
        event_dict.setdefault("batch_id", b)
    if (r := _row_num_var.get()) is not None:
        event_dict.setdefault("row_num", r)
    if (u := _user_email_var.get()) is not None:
        event_dict.setdefault("user_email", u)
    return event_dict


class _JobLogFileHandler(logging.Handler):
    """Append each JSON log line to ``<logs_dir>/<batch_id>.log`` (per job).

    The line already carries ``row_num`` (from the context injector), so the
    admin panel can show logs filtered to a single row. Non-JSON lines (e.g.
    third-party libraries) and lines without a ``batch_id`` are ignored.
    """

    def __init__(self, logs_dir: Path) -> None:
        super().__init__()
        self._dir = logs_dir
        with contextlib.suppress(Exception):
            self._dir.mkdir(parents=True, exist_ok=True)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            batch_id = json.loads(msg).get("batch_id")
        except Exception:
            return
        if not batch_id:
            return
        safe = str(batch_id).replace("/", "_").replace("\\", "_").replace("..", "_")
        try:
            with open(self._dir / f"{safe}.log", "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass


def configure_logging() -> None:
    """Configure structlog + stdlib logging. Idempotent."""
    settings = get_settings()
    level = getattr(logging, settings.BULKVID_LOG_LEVEL.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    # Per-job log files (for the admin per-row log viewer). Replace any prior
    # instance so repeated configure_logging() calls don't duplicate writes.
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not isinstance(h, _JobLogFileHandler)]
    job_handler = _JobLogFileHandler(Path(settings.BULKVID_DATA_DIR) / "logs")
    job_handler.setLevel(level)
    root.addHandler(job_handler)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _inject_context,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def get_logger(namespace: str) -> structlog.stdlib.BoundLogger:
    """Return a logger bound to ``[bulkvid <namespace>]``.

    Use one namespace per stage. See the plan §8 for the canonical list.
    """
    return structlog.get_logger().bind(ns=f"bulkvid {namespace}")


# ── Reading per-job logs ──────────────────────────────────────────────────────


def _format_log_line(raw: str) -> str:
    """Turn one stored JSON log line into a compact readable string."""
    try:
        d = json.loads(raw)
    except Exception:
        return raw
    ts = str(d.get("timestamp", ""))[11:19]
    lvl = str(d.get("level", "")).upper()[:4]
    event = d.get("event", "")
    skip = {"timestamp", "level", "event", "ns", "batch_id", "row_num", "user_email"}
    kv = " ".join(f"{k}={v}" for k, v in d.items() if k not in skip)
    return f"{ts} {lvl:<4} {event} {kv}".rstrip()


def read_job_log_lines(
    job_id: str, *, row: int | None = None, tail: int = 300
) -> tuple[list[str], bool]:
    """Read a per-job log file, newest ``tail`` lines last, optionally filtered
    to a single ``row``. Returns ``(formatted_lines, file_exists)``.

    Shared by the admin log viewer and the token-gated sidebar log endpoint so
    the path-sanitising and JSON formatting live in one place.
    """
    safe = str(job_id).replace("/", "_").replace("\\", "_").replace("..", "_")
    path = Path(get_settings().BULKVID_DATA_DIR) / "logs" / f"{safe}.log"
    if not path.exists():
        return [], False
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if row is not None:
            try:
                if int(json.loads(raw).get("row_num", -1)) != row:
                    continue
            except Exception:
                continue
        lines.append(_format_log_line(raw))
    tail = max(1, min(tail, 2000))
    return lines[-tail:], True
