"""Stateless Hrana-over-HTTP client for Turso — a DB transport with real deadlines.

Why this exists (Plan
``_plans/2026-08-12-hrana-http-transport.md``):

The sync ``libsql`` client we have used until now has two properties that between
them caused every "the Space is stuck, restart it" incident:

  * **No transport timeout and no interrupt.** A stalled statement blocks its
    thread forever, uncancellably. ``asyncio.wait_for`` abandons the awaiting
    coroutine but cannot kill the thread, so each hiccup permanently leaks one
    DB-pool thread until the pool is dead.
  * **A long-lived logical session.** It can keep serving a stale read snapshot
    (``pending=0`` for a queue holding 100+ rows) with no error raised at all,
    which is invisible to every error-driven or hang-driven self-healer.

Both are properties of the CLIENT, not of Turso. Turso also speaks a plain
HTTP protocol (Hrana over ``POST /v2/pipeline``), which this module implements
directly on ``httpx``:

  * Every call carries a hard connect/read timeout, so a stalled statement
    RAISES on a deadline instead of parking a thread forever. That converts the
    unbounded failure class into an ordinary exception the existing
    ``JobQueue._run_db`` retry/reconnect machinery already knows how to handle.
  * Every call is **stateless**: ``baton: null`` opens a fresh server-side
    stream, and a trailing ``close`` request ends it in the same round-trip. No
    session survives between statements, so there is no snapshot to go stale.

The TCP/TLS connection IS pooled and reused (``httpx.Client``) — that is a
transport-level optimisation and is unrelated to the logical session that caused
stale reads. Pooling costs nothing in correctness and saves a handshake per
statement.

Deliberately NOT implemented: interactive transactions. Remote mode has run in
autocommit (``isolation_level=None``) since 2026-06-04 and the DB-API shim in
``db.py`` already translates BEGIN/COMMIT/ROLLBACK to no-ops, so multi-statement
atomicity is not something this transport takes away. See that module's history
note.

Protocol reference (verified 2026-08-12 against the Hrana 3 spec and Turso's
"SQL over HTTP" docs): integers travel as STRINGS to preserve 64-bit precision,
floats as JSON numbers, blobs as base64 under a ``base64`` key (not ``value``).
A 4xx/5xx is a transport failure; a per-statement SQL failure comes back as an
``{"type": "error"}`` entry inside ``results``.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any

import httpx

from bulkvid.logging import get_logger

_log = get_logger("hrana")


class HranaError(RuntimeError):
    """The server ran our statement and reported a SQL-level error.

    Distinct from ``HranaTransportError`` because this one is deterministic:
    retrying an invalid statement or a constraint violation will fail again, so
    the retry machinery should not treat it as a flap."""


class HranaTransportError(RuntimeError):
    """We could not obtain an answer: timeout, connection failure, HTTP 4xx/5xx,
    or an unparseable body.

    This class existing at all IS the point of the module. The old client had no
    way to produce it — it simply blocked forever — which is why a wedge could
    only be cleared by restarting the process."""


# Connect and read budgets. The read budget sits deliberately BELOW
# ``queue._DB_CALL_TIMEOUT_SECONDS`` (15s) so a stalled statement surfaces as a
# clean exception from this layer, which ``_run_db`` then retries with a fresh
# connection, rather than the outer ``wait_for`` abandoning a call that is still
# running underneath. Env-tunable for a per-deploy tune without a code change.
_CONNECT_TIMEOUT_SECONDS = 5.0
_READ_TIMEOUT_SECONDS = 10.0


def _positive_float(env_name: str, default: float) -> float:
    """Read a positive float from ``env_name``; fall back to ``default`` on an
    empty/invalid/non-positive value (never crash a deploy on a bad knob)."""
    raw = os.environ.get(env_name)
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return v if v > 0 else default


@dataclass(frozen=True)
class StatementResult:
    """One statement's outcome, already decoded into Python values.

    ``cols`` and ``rows`` are plain tuples so the DB-API shim in ``db.py`` can
    wrap them in ``_DictRow`` without another copy."""

    cols: tuple[str, ...]
    rows: list[tuple[Any, ...]]
    affected_row_count: int
    last_insert_rowid: int | None


def to_http_url(url: str) -> str:
    """Normalise a libsql/websocket/HTTP database URL to an ``https://`` origin.

    Turso hands out ``libsql://host`` URLs; the same host serves the HTTP
    protocol over ``https``. Accepts the websocket and plain-HTTP spellings too
    so a deploy that already uses one of those keeps working."""
    s = url.strip()
    for prefix, replacement in (
        ("libsql://", "https://"),
        ("wss://", "https://"),
        ("ws://", "http://"),
    ):
        if s.startswith(prefix):
            s = replacement + s[len(prefix):]
            break
    if not s.startswith(("http://", "https://")):
        s = "https://" + s
    return s.rstrip("/")


def _encode_value(v: Any) -> dict[str, Any]:
    """Python value -> Hrana ``Value``.

    ``bool`` MUST be tested before ``int``: it is an ``int`` subclass, and
    letting it fall through would still work numerically but is checked
    explicitly so the intent survives a refactor. Integers are stringified
    because the protocol carries them as strings to keep 64-bit precision
    through JSON."""
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, str):
        return {"type": "text", "value": v}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {
            "type": "blob",
            "base64": base64.b64encode(bytes(v)).decode("ascii"),
        }
    raise HranaError(
        f"cannot bind parameter of type {type(v).__name__}"
    )


def _decode_value(v: Any) -> Any:
    """Hrana ``Value`` -> Python value. Mirror of ``_encode_value``."""
    if not isinstance(v, dict):
        raise HranaTransportError(f"malformed value in response: {v!r:.80}")
    kind = v.get("type")
    if kind == "null":
        return None
    if kind == "integer":
        return int(v["value"])
    if kind == "float":
        return float(v["value"])
    if kind == "text":
        return str(v["value"])
    if kind == "blob":
        return base64.b64decode(v.get("base64") or "")
    raise HranaTransportError(f"unknown value type {kind!r} in response")


def _encode_params(params: Any) -> dict[str, Any]:
    """Build the ``args``/``named_args`` half of a ``Stmt``.

    Callers in this codebase pass positional tuples, but a mapping is accepted
    so the shim keeps full DB-API parity with the sqlite3 path it replaces."""
    if params is None:
        return {}
    if isinstance(params, dict):
        return {
            "named_args": [
                {"name": str(k), "value": _encode_value(val)}
                for k, val in params.items()
            ]
        }
    return {"args": [_encode_value(p) for p in params]}


class HranaClient:
    """Thin, stateless SQL-over-HTTP client for one Turso database.

    Thread-safe: ``httpx.Client`` supports concurrent requests, which matters
    because every call arrives on the shared 16-thread DB pool.
    """

    def __init__(
        self,
        url: str,
        auth_token: str,
        *,
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
    ) -> None:
        self._endpoint = to_http_url(url) + "/v2/pipeline"
        connect_s = connect_timeout if connect_timeout is not None else (
            _positive_float(
                "BULKVID_HRANA_CONNECT_TIMEOUT_SECONDS", _CONNECT_TIMEOUT_SECONDS
            )
        )
        read_s = read_timeout if read_timeout is not None else (
            _positive_float(
                "BULKVID_HRANA_READ_TIMEOUT_SECONDS", _READ_TIMEOUT_SECONDS
            )
        )
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {auth_token}",
                "Content-Type": "application/json",
            },
            # Every phase is bounded. ``pool`` included so waiting for a free
            # connection cannot become the new unbounded wait.
            timeout=httpx.Timeout(
                connect=connect_s, read=read_s, write=read_s, pool=connect_s
            ),
        )

    # ── Internals ───────────────────────────────────────────────────────────

    def _pipeline(self, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """POST one pipeline and return its ``results``, raising on any failure.

        ``baton: null`` plus a trailing ``close`` makes the whole exchange
        stateless: the server opens a stream, runs our statements against the
        latest committed state, and drops it — all in one round-trip."""
        body = {"baton": None, "requests": [*requests, {"type": "close"}]}
        try:
            resp = self._client.post(self._endpoint, json=body)
        except httpx.HTTPError as e:
            # Timeouts land here too (httpx.TimeoutException subclasses
            # HTTPError). This is the deadline the old client never had.
            raise HranaTransportError(
                f"{type(e).__name__}: {str(e)[:200]}"
            ) from e
        if resp.status_code >= 400:
            raise HranaTransportError(
                f"HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            payload = resp.json()
        except ValueError as e:
            raise HranaTransportError(
                f"response was not JSON: {resp.text[:200]}"
            ) from e
        results = payload.get("results")
        if not isinstance(results, list):
            raise HranaTransportError(
                f"response had no results array: {str(payload)[:200]}"
            )
        for entry in results:
            if isinstance(entry, dict) and entry.get("type") == "error":
                err = entry.get("error") or {}
                raise HranaError(
                    f"{err.get('code') or 'SQL_ERROR'}: "
                    f"{str(err.get('message'))[:300]}"
                )
        return results

    @staticmethod
    def _first_execute_result(results: list[dict[str, Any]]) -> dict[str, Any]:
        """Pull the ``StmtResult`` out of the first pipeline entry."""
        if not results:
            raise HranaTransportError("empty results array")
        response = (results[0] or {}).get("response") or {}
        result = response.get("result")
        if not isinstance(result, dict):
            raise HranaTransportError(
                f"missing execute result: {str(results[0])[:200]}"
            )
        return result

    # ── Public API ──────────────────────────────────────────────────────────

    def execute(self, sql: str, params: Any = ()) -> StatementResult:
        """Run ONE statement and return its decoded result."""
        stmt: dict[str, Any] = {"sql": sql, "want_rows": True}
        stmt.update(_encode_params(params))
        results = self._pipeline([{"type": "execute", "stmt": stmt}])
        result = self._first_execute_result(results)

        cols = tuple(
            str((c or {}).get("name") or "") for c in result.get("cols") or []
        )
        rows = [
            tuple(_decode_value(v) for v in row)
            for row in result.get("rows") or []
        ]
        rowid_raw = result.get("last_insert_rowid")
        return StatementResult(
            cols=cols,
            rows=rows,
            affected_row_count=int(result.get("affected_row_count") or 0),
            # Carried as a string in JSON (64-bit), and null for non-inserts.
            last_insert_rowid=(
                None if rowid_raw is None else int(rowid_raw)
            ),
        )

    def execute_script(self, sql: str) -> None:
        """Run a multi-statement SQL script (the ``executescript`` equivalent).

        Uses the protocol's own ``sequence`` request rather than splitting on
        semicolons ourselves, so string literals containing ``;`` can never be
        mis-split."""
        self._pipeline([{"type": "sequence", "sql": sql}])

    def close(self) -> None:
        """Release the pooled TCP/TLS connections. Safe to call twice."""
        self._client.close()
