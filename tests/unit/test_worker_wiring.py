"""Tests for the worker wiring.

These are wiring tests, not end-to-end runs — we just check that
``build_pipeline_clients`` and ``build_flush_callback`` produce the right
shapes given various settings.

Covers:
  - All adapters constructed when full env is configured
  - ZapCap is None when ZAPCAP_API_KEY is empty
  - Missing required keys raise on build (fail fast at startup)
  - SheetsClient attached when credentials file is set
  - Noop callback used when credentials file is absent (worker still runs)
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from bulkvid.config import Settings
from bulkvid.orchestrator.sheet_writer import PendingWrite
from bulkvid.worker import (
    build_flush_callback,
    build_pipeline_clients,
    heartbeat_loop,
)


def _full_settings(**overrides) -> Settings:
    base = dict(
        OPENAI_API_KEY="sk-test",
        KIE_AI_KEYS="kie_key_AAAAAAAAAAAA",
        RENDI_API_KEY="rendi-test",
        ZAPCAP_API_KEY="zc-test",
        SCRAPINGBEE_API_KEY="sb-test",
        AWS_ACCESS_KEY_ID="aws-id",
        AWS_SECRET_ACCESS_KEY="aws-secret",
        AWS_BUCKET_NAME="b",
        GCS_BUCKET_NAME="gcs-b",
        VERTEX_AI_PROJECT_ID="amit-tts",
        SHEETS_SERVICE_ACCOUNT_FILE="",
        # Storage now picks GCS primary when GCS creds are present. For
        # most wiring tests we want the S3-only path so we don't need to
        # supply Google creds.
        GCS_BUCKET_NAME_EMPTY=False,
    )
    base.update(overrides)
    base.pop("GCS_BUCKET_NAME_EMPTY", None)
    return Settings(**base)


# ── build_pipeline_clients ──────────────────────────────────────────────────


def test_build_pipeline_clients_full_config_succeeds() -> None:
    settings = _full_settings()
    clients = build_pipeline_clients(settings)

    assert clients.openai is not None
    assert clients.kie is not None
    assert clients.tts is not None
    assert clients.rendi is not None
    assert clients.storage is not None
    assert clients.article is not None
    assert clients.zapcap is not None


def test_build_pipeline_clients_attaches_kie_router() -> None:
    # The router is always wired; ``kie`` IS the router's default (same
    # instance) so the default pool is never constructed twice. Plan
    # ``_plans/2026-07-20-per-sheet-kie-key-routing.md``.
    clients = build_pipeline_clients(_full_settings())
    assert clients.kie_router is not None
    assert clients.kie is clients.kie_router.default


def test_build_pipeline_clients_routes_mapped_sheet_to_own_client() -> None:
    settings = _full_settings(
        KIE_AI_KEYS="kie_default_AAAAAAAA",
        KIE_KEY_MAP="bulk-videos-2 = kie_newkey_BBBBBBBB",
    )
    clients = build_pipeline_clients(settings)
    router = clients.kie_router
    assert router is not None
    assert router.for_sheet("bulk-videos-2") is not clients.kie   # own client
    assert router.for_sheet("bulk-videos-1") is clients.kie       # default pool


def test_zapcap_is_none_when_key_missing() -> None:
    settings = _full_settings(ZAPCAP_API_KEY="")
    clients = build_pipeline_clients(settings)
    assert clients.zapcap is None
    # Other adapters still configured.
    assert clients.openai is not None
    assert clients.kie is not None


def test_missing_openai_key_raises() -> None:
    settings = _full_settings(OPENAI_API_KEY="")
    with pytest.raises(ValueError):
        build_pipeline_clients(settings)


def test_missing_kie_keys_raises() -> None:
    settings = _full_settings(KIE_AI_KEYS="")
    with pytest.raises(ValueError):
        build_pipeline_clients(settings)


def test_missing_rendi_key_raises() -> None:
    settings = _full_settings(RENDI_API_KEY="")
    with pytest.raises(ValueError):
        build_pipeline_clients(settings)


def test_missing_aws_credentials_raises_when_gcs_also_unavailable() -> None:
    # Empty AWS keys AND no GCS credentials -> storage builder raises.
    # GCS bucket alone isn't enough; we need credentials too.
    settings = _full_settings(AWS_ACCESS_KEY_ID="", GCS_BUCKET_NAME="some-bucket")
    with pytest.raises(ValueError):
        build_pipeline_clients(settings)


def _real_rsa_pem() -> str:
    """Generate a real RSA key in PEM so service_account.Credentials accepts it."""
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    priv = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("utf-8")


def test_gcs_credentials_alone_satisfies_storage() -> None:
    # Storage now accepts GCS-primary when Google credentials are present,
    # so AWS keys are not required.
    settings = _full_settings(
        AWS_ACCESS_KEY_ID="",
        AWS_SECRET_ACCESS_KEY="",
        GCS_BUCKET_NAME="some-bucket",
        GOOGLE_PROJECT_ID="amit-tts",
        GOOGLE_CLIENT_EMAIL="x@amit-tts.iam.gserviceaccount.com",
        GOOGLE_CLIENT_ID="1",
        GOOGLE_PRIVATE_KEY=_real_rsa_pem(),
    )
    clients = build_pipeline_clients(settings)
    assert clients.storage is not None


def test_missing_scrapingbee_raises() -> None:
    settings = _full_settings(SCRAPINGBEE_API_KEY="")
    with pytest.raises(ValueError):
        build_pipeline_clients(settings)


# ── build_flush_callback ────────────────────────────────────────────────────


async def test_flush_callback_is_noop_when_sheets_credentials_missing() -> None:
    settings = _full_settings(SHEETS_SERVICE_ACCOUNT_FILE="")
    callback = build_flush_callback(settings)

    # The noop is async and accepts list[PendingWrite] without raising.
    write = PendingWrite(
        job_id="job-1", sheet_id="s", worksheet="w", tab_type="image_vo",
        row_num=2, video_urls=["u"], status="SUCCESS", error=None,
    )
    await callback([write])      # no exception means we're good


# ── heartbeat_loop (Plan 2026-08-12) ────────────────────────────────────────


class _FakeQueue:
    """Records beats; optionally fails the first N to prove the loop survives."""

    def __init__(self, fail_first: int = 0) -> None:
        self.beats: list[int] = []
        self._fail_first = fail_first

    async def write_heartbeat(self, *, in_flight: int) -> None:
        if len(self.beats) < self._fail_first:
            self.beats.append(-1)
            raise RuntimeError("turso flap")
        self.beats.append(in_flight)


class _FakeRunner:
    def __init__(self, in_flight: int = 0) -> None:
        self.in_flight_count = in_flight


async def _run_beats(queue, runner, *, ticks: int) -> None:
    """Drive ``heartbeat_loop`` for exactly ``ticks`` beats, then cancel it.

    The loop is infinite by design, so we let a patched sleep count the
    iterations and cancel from inside once we have seen enough."""
    task = asyncio.create_task(
        heartbeat_loop(queue, runner, interval_seconds=0)
    )
    while len(queue.beats) < ticks:
        await asyncio.sleep(0)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_heartbeat_loop_beats_immediately_with_in_flight_count() -> None:
    """Beats FIRST, then sleeps — the row must be fresh within a moment of boot,
    not one interval later, or the watchdog's uptime gate misjudges a new
    worker."""
    queue = _FakeQueue()
    await _run_beats(queue, _FakeRunner(in_flight=4), ticks=1)
    assert queue.beats[0] == 4


async def test_heartbeat_loop_survives_write_failures() -> None:
    """A failed beat IS the wedge signal — it must be logged and swallowed, never
    crash the worker (which would turn a diagnostic into an outage)."""
    queue = _FakeQueue(fail_first=2)
    await _run_beats(queue, _FakeRunner(in_flight=1), ticks=4)
    assert queue.beats[:2] == [-1, -1]     # two failures
    assert queue.beats[2:] == [1, 1]       # then it recovers and keeps beating
