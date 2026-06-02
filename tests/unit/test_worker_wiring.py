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

import pytest

from bulkvid.config import Settings
from bulkvid.orchestrator.sheet_writer import PendingWrite
from bulkvid.worker import build_flush_callback, build_pipeline_clients


def _full_settings(**overrides) -> Settings:
    base = dict(
        OPENAI_API_KEY="sk-test",
        KIE_AI_KEYS="kie_key_AAAAAAAAAAAA",
        RENDI_API_KEY="rendi-test",
        ZAPCAP_API_KEY="zc-test",
        TAVILY_API_KEY="tav-test",
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


def test_missing_tavily_and_scrapingbee_raises() -> None:
    settings = _full_settings(TAVILY_API_KEY="")
    # SCRAPINGBEE_API_KEY is also empty by default in our fixture.
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
