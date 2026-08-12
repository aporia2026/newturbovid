"""Stateless Hrana-over-HTTP transport (Plan 2026-08-12).

This transport exists to kill an entire failure class, so the tests are written
against the two properties that matter rather than against implementation
detail:

  1. **Wire-format fidelity.** Every request we send and every response we
     decode must match the Hrana 3 spec exactly (integers as strings, floats as
     numbers, blobs under ``base64``, ``baton: null`` plus a trailing ``close``
     on every pipeline). A protocol mistake here would corrupt the queue, so the
     request bodies are asserted literally.
  2. **Bounded failure.** A stalled or failing server must produce an exception
     on a deadline, never a hang. That is the whole point: the old client could
     only block forever.

``respx`` intercepts httpx at the transport layer, so these exercise the real
``httpx.Client`` including its timeout plumbing.
"""

from __future__ import annotations

import base64

import httpx
import pytest
import respx

from bulkvid.orchestrator.hrana import (
    HranaClient,
    HranaError,
    HranaTransportError,
    to_http_url,
)

_URL = "libsql://db-org.turso.io"
_ENDPOINT = "https://db-org.turso.io/v2/pipeline"


def _ok(result: dict) -> dict:
    """A well-formed pipeline response: our execute, then the close we append."""
    return {
        "baton": None,
        "base_url": None,
        "results": [
            {"type": "ok", "response": {"type": "execute", "result": result}},
            {"type": "ok", "response": {"type": "close"}},
        ],
    }


def _result(cols=(), rows=(), affected=0, rowid=None) -> dict:
    return {
        "cols": [{"name": c, "decltype": None} for c in cols],
        "rows": list(rows),
        "affected_row_count": affected,
        "last_insert_rowid": rowid,
        "rows_read": 0,
        "rows_written": 0,
        "query_duration_ms": 0.1,
    }


# ── URL normalisation ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("libsql://db-org.turso.io", "https://db-org.turso.io"),
        ("wss://db-org.turso.io", "https://db-org.turso.io"),
        ("ws://localhost:8080", "http://localhost:8080"),
        ("https://db-org.turso.io/", "https://db-org.turso.io"),
        ("db-org.turso.io", "https://db-org.turso.io"),
    ],
)
def test_to_http_url_normalises_every_spelling(given: str, expected: str) -> None:
    assert to_http_url(given) == expected


# ── Request wire format ──────────────────────────────────────────────────────


@respx.mock
def test_execute_sends_stateless_pipeline_with_close() -> None:
    """``baton: null`` + a trailing ``close`` is what makes each call stateless,
    which is what removes the stale-read failure mode."""
    route = respx.post(_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_ok(_result()))
    )
    client = HranaClient(_URL, "tok")
    try:
        client.execute("SELECT 1")
    finally:
        client.close()

    body = route.calls.last.request.read()
    import json

    sent = json.loads(body)
    assert sent["baton"] is None
    assert [r["type"] for r in sent["requests"]] == ["execute", "close"]
    assert sent["requests"][0]["stmt"]["sql"] == "SELECT 1"
    assert sent["requests"][0]["stmt"]["want_rows"] is True
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"


@respx.mock
def test_execute_encodes_every_parameter_type() -> None:
    """Integers travel as STRINGS (64-bit precision through JSON), floats as
    numbers, blobs under ``base64`` not ``value``. Getting any of these wrong
    would silently corrupt queue rows."""
    route = respx.post(_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_ok(_result()))
    )
    client = HranaClient(_URL, "tok")
    try:
        client.execute(
            "INSERT INTO t VALUES (?,?,?,?,?,?)",
            (None, 42, 1.5, "text", b"\x00\x01", True),
        )
    finally:
        client.close()

    import json

    args = json.loads(route.calls.last.request.read())["requests"][0]["stmt"]["args"]
    assert args == [
        {"type": "null"},
        {"type": "integer", "value": "42"},
        {"type": "float", "value": 1.5},
        {"type": "text", "value": "text"},
        {"type": "blob", "base64": base64.b64encode(b"\x00\x01").decode()},
        # bool is an int subclass; it must encode as 1, not as `true`.
        {"type": "integer", "value": "1"},
    ]


@respx.mock
def test_execute_encodes_named_parameters() -> None:
    route = respx.post(_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_ok(_result()))
    )
    client = HranaClient(_URL, "tok")
    try:
        client.execute("SELECT :a", {"a": 7})
    finally:
        client.close()

    import json

    stmt = json.loads(route.calls.last.request.read())["requests"][0]["stmt"]
    assert stmt["named_args"] == [{"name": "a", "value": {"type": "integer", "value": "7"}}]


@respx.mock
def test_execute_script_uses_sequence_not_semicolon_splitting() -> None:
    """The protocol's own ``sequence`` runs a multi-statement script, so a
    ``;`` inside a string literal can never be mis-split by us."""
    route = respx.post(_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "baton": None,
                "results": [
                    {"type": "ok", "response": {"type": "sequence"}},
                    {"type": "ok", "response": {"type": "close"}},
                ],
            },
        )
    )
    client = HranaClient(_URL, "tok")
    try:
        client.execute_script("CREATE TABLE a(x);\nCREATE TABLE b(y);")
    finally:
        client.close()

    import json

    sent = json.loads(route.calls.last.request.read())
    assert sent["requests"][0]["type"] == "sequence"
    assert "CREATE TABLE b(y);" in sent["requests"][0]["sql"]


# ── Response decoding ────────────────────────────────────────────────────────


@respx.mock
def test_execute_decodes_every_value_type() -> None:
    respx.post(_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_ok(
                _result(
                    cols=("n", "f", "t", "b", "z"),
                    rows=[[
                        {"type": "integer", "value": "9007199254740993"},
                        {"type": "float", "value": 2.5},
                        {"type": "text", "value": "hi"},
                        {"type": "blob", "base64": base64.b64encode(b"\xff").decode()},
                        {"type": "null"},
                    ]],
                )
            ),
        )
    )
    client = HranaClient(_URL, "tok")
    try:
        res = client.execute("SELECT *")
    finally:
        client.close()

    assert res.cols == ("n", "f", "t", "b", "z")
    # Beyond float53 — proof the string encoding preserves 64-bit integers.
    assert res.rows[0][0] == 9007199254740993
    assert res.rows[0][1] == 2.5
    assert res.rows[0][2] == "hi"
    assert res.rows[0][3] == b"\xff"
    assert res.rows[0][4] is None


@respx.mock
def test_execute_reports_affected_rows_and_lastrowid() -> None:
    respx.post(_ENDPOINT).mock(
        return_value=httpx.Response(
            200, json=_ok(_result(affected=3, rowid="123"))
        )
    )
    client = HranaClient(_URL, "tok")
    try:
        res = client.execute("UPDATE t SET x = 1")
    finally:
        client.close()
    assert res.affected_row_count == 3
    assert res.last_insert_rowid == 123    # carried as a string in JSON


# ── Bounded failure: the entire point of this transport ──────────────────────


@respx.mock
def test_read_timeout_raises_instead_of_hanging() -> None:
    """The old client had no way to produce this: it blocked its thread forever
    and only a process restart recovered."""
    respx.post(_ENDPOINT).mock(side_effect=httpx.ReadTimeout("timed out"))
    client = HranaClient(_URL, "tok", read_timeout=0.05)
    try:
        with pytest.raises(HranaTransportError, match="ReadTimeout"):
            client.execute("SELECT 1")
    finally:
        client.close()


@respx.mock
def test_connect_error_raises_transport_error() -> None:
    respx.post(_ENDPOINT).mock(side_effect=httpx.ConnectError("refused"))
    client = HranaClient(_URL, "tok")
    try:
        with pytest.raises(HranaTransportError):
            client.execute("SELECT 1")
    finally:
        client.close()


@respx.mock
def test_http_error_status_raises_transport_error() -> None:
    respx.post(_ENDPOINT).mock(
        return_value=httpx.Response(401, text="unauthorized")
    )
    client = HranaClient(_URL, "tok")
    try:
        with pytest.raises(HranaTransportError, match="HTTP 401"):
            client.execute("SELECT 1")
    finally:
        client.close()


@respx.mock
def test_sql_error_in_results_raises_hrana_error() -> None:
    """A per-statement SQL failure comes back INSIDE a 200 response. Missing
    this would let a failed write look like success."""
    respx.post(_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "baton": None,
                "results": [
                    {
                        "type": "error",
                        "error": {
                            "message": "no such table: nope",
                            "code": "SQLITE_UNKNOWN",
                        },
                    }
                ],
            },
        )
    )
    client = HranaClient(_URL, "tok")
    try:
        with pytest.raises(HranaError, match="no such table"):
            client.execute("SELECT * FROM nope")
    finally:
        client.close()


@respx.mock
def test_malformed_body_raises_transport_error() -> None:
    respx.post(_ENDPOINT).mock(return_value=httpx.Response(200, text="not json"))
    client = HranaClient(_URL, "tok")
    try:
        with pytest.raises(HranaTransportError, match="not JSON"):
            client.execute("SELECT 1")
    finally:
        client.close()


@respx.mock
def test_missing_results_array_raises_transport_error() -> None:
    respx.post(_ENDPOINT).mock(return_value=httpx.Response(200, json={"baton": None}))
    client = HranaClient(_URL, "tok")
    try:
        with pytest.raises(HranaTransportError, match="no results array"):
            client.execute("SELECT 1")
    finally:
        client.close()


def test_unsupported_parameter_type_is_rejected_before_sending() -> None:
    client = HranaClient(_URL, "tok")
    try:
        with pytest.raises(HranaError, match="cannot bind parameter"):
            client.execute("SELECT ?", (object(),))
    finally:
        client.close()
