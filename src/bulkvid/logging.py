"""Namespaced structured logger.

Every log entry is JSON to stdout, tagged with the active batch_id, row_num,
and user_email when available (via contextvars), and emitted under a
``[bulkvid <stage>]`` namespace so grep / log search by stage works.

Rule 14: log values, not just events. Booleans without their value are useless.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
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


def configure_logging() -> None:
    """Configure structlog + stdlib logging. Idempotent."""
    settings = get_settings()
    level = getattr(logging, settings.BULKVID_LOG_LEVEL.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

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
