"""Storage adapter — GCS primary (``aporia-unleash``), S3 fallback (``aporia-creative``).

The underlying SDKs (``boto3`` for S3, ``google-cloud-storage`` for GCS) are
blocking. We wrap each call in ``asyncio.to_thread`` so the orchestrator's
event loop stays responsive — upload work happens on the default thread pool.

Pattern lifted from ``refs/stage_5_add_music.py`` ``S3Uploader`` (folder-prefix
normalisation, public-read ACL with graceful fallback when bucket ACLs are
disabled, ``BytesIO`` streaming).

Plan: ``_plans/2026-06-02-aporia-bulk-video-tool.md`` §5 (Storage), §8 (logs).
"""

from __future__ import annotations

import asyncio
import io
import urllib.parse
from dataclasses import dataclass
from typing import Any, Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from google.cloud import storage as gcs
from google.oauth2 import service_account

from bulkvid.adapters.google_credentials import build_credentials_info
from bulkvid.config import Settings, get_settings
from bulkvid.logging import get_logger

_log = get_logger("storage")


# ── Pricing ──────────────────────────────────────────────────────────────────
# S3 PUT ~$0.000005, GCS write similar; monthly storage ~$0.023/GB. Per-upload
# cost is tiny; treat it as ~$0.0001 amortized including bandwidth.
COST_PER_UPLOAD_USD = 0.0001


# ── Result ───────────────────────────────────────────────────────────────────


@dataclass
class UploadResult:
    url: str
    backend: str                      # "s3" | "gcs"
    bytes_written: int
    cost_usd: float


# ── Errors ───────────────────────────────────────────────────────────────────


class StorageError(RuntimeError):
    """All configured storage backends failed."""


# ── Uploader protocol ────────────────────────────────────────────────────────


class _AsyncUploader(Protocol):
    backend_name: str

    async def upload_bytes(
        self, data: bytes, key: str, content_type: str
    ) -> str: ...


# ── S3 uploader ──────────────────────────────────────────────────────────────


class S3Uploader:
    """Async wrapper over boto3's S3 client."""

    backend_name = "s3"

    def __init__(
        self,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        region: str = "us-east-1",
        client: Any | None = None,    # injected in tests
    ) -> None:
        if not bucket:
            raise ValueError("S3Uploader requires a bucket")
        self._bucket = bucket
        if client is not None:
            self._client = client
        else:
            if not access_key_id or not secret_access_key:
                raise ValueError("S3Uploader requires AWS credentials")
            session = boto3.session.Session(
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                region_name=region,
            )
            self._client = session.client("s3")

    def _public_url(self, key: str) -> str:
        return f"https://{self._bucket}.s3.amazonaws.com/{urllib.parse.quote(key)}"

    def _upload_sync(self, data: bytes, key: str, content_type: str) -> None:
        body = io.BytesIO(data)
        extra: dict[str, str] = {"ContentType": content_type}
        self._client.upload_fileobj(body, self._bucket, key, ExtraArgs=extra)
        try:
            self._client.put_object_acl(
                Bucket=self._bucket, Key=key, ACL="public-read"
            )
        except (BotoCoreError, ClientError) as e:
            # Some buckets disable ACLs; the object is still readable via the
            # bucket's own access policy. Don't fail the upload over this.
            _log.warning("s3_acl_skipped", key=key, error=str(e)[:200])

    async def upload_bytes(
        self, data: bytes, key: str, content_type: str
    ) -> str:
        _log.info(
            "s3_upload",
            bucket=self._bucket,
            key=key,
            size_bytes=len(data),
            content_type=content_type,
        )
        await asyncio.to_thread(self._upload_sync, data, key, content_type)
        url = self._public_url(key)
        _log.info("s3_upload_ok", url=url)
        return url


# ── GCS uploader ─────────────────────────────────────────────────────────────


class GCSUploader:
    """Async wrapper over google-cloud-storage.

    Accepts credentials in three ways, in priority order:
      1. Explicit ``client`` (tests inject a MagicMock)
      2. ``credentials_file`` path to a service-account JSON
      3. ``credentials_info`` dict (the parsed JSON shape — used by the
         inline-env-var ``GOOGLE_*`` configuration mode)
      4. Application Default Credentials (ADC) — only useful when
         ``GOOGLE_APPLICATION_CREDENTIALS`` is exported in the environment
    """

    backend_name = "gcs"

    def __init__(
        self,
        bucket: str,
        credentials_file: str = "",
        credentials_info: dict[str, Any] | None = None,
        client: Any | None = None,    # injected in tests
    ) -> None:
        if not bucket:
            raise ValueError("GCSUploader requires a bucket")
        if client is not None:
            self._client = client
        elif credentials_file:
            self._client = gcs.Client.from_service_account_json(credentials_file)
        elif credentials_info is not None:
            creds = service_account.Credentials.from_service_account_info(credentials_info)
            project = credentials_info.get("project_id")
            self._client = gcs.Client(project=project, credentials=creds)
        else:
            # Falls back to ADC. Will raise on first use if ADC isn't set up.
            self._client = gcs.Client()
        self._bucket = self._client.bucket(bucket)

    def _upload_sync(self, data: bytes, key: str, content_type: str) -> str:
        blob = self._bucket.blob(key)
        blob.upload_from_string(data, content_type=content_type)
        try:
            blob.make_public()
        except Exception as e:
            _log.warning("gcs_make_public_skipped", key=key, error=str(e)[:200])
        return blob.public_url

    async def upload_bytes(
        self, data: bytes, key: str, content_type: str
    ) -> str:
        _log.info(
            "gcs_upload",
            bucket=self._bucket.name,
            key=key,
            size_bytes=len(data),
            content_type=content_type,
        )
        url = await asyncio.to_thread(self._upload_sync, data, key, content_type)
        _log.info("gcs_upload_ok", url=url)
        return url


# ── Orchestrator: primary + fallback ────────────────────────────────────────


class StorageClient:
    """Try primary first; on any error, try fallback."""

    def __init__(
        self,
        primary: _AsyncUploader,
        fallback: _AsyncUploader | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback

    async def upload_bytes(
        self,
        data: bytes,
        key: str,
        content_type: str = "application/octet-stream",
    ) -> UploadResult:
        try:
            url = await self._primary.upload_bytes(data, key, content_type)
            return UploadResult(
                url=url,
                backend=self._primary.backend_name,
                bytes_written=len(data),
                cost_usd=COST_PER_UPLOAD_USD,
            )
        except Exception as primary_error:
            _log.error(
                "storage_primary_failed",
                backend=self._primary.backend_name,
                key=key,
                error=str(primary_error)[:200],
            )
            if self._fallback is None:
                raise StorageError(
                    f"Primary storage {self._primary.backend_name} failed and "
                    f"no fallback configured: {primary_error}"
                ) from primary_error

            try:
                url = await self._fallback.upload_bytes(data, key, content_type)
                _log.warning(
                    "storage_fallback_used",
                    primary=self._primary.backend_name,
                    fallback=self._fallback.backend_name,
                    key=key,
                )
                return UploadResult(
                    url=url,
                    backend=self._fallback.backend_name,
                    bytes_written=len(data),
                    cost_usd=COST_PER_UPLOAD_USD,
                )
            except Exception as fallback_error:
                raise StorageError(
                    f"Both storage backends failed. "
                    f"Primary={self._primary.backend_name} err={primary_error!s} | "
                    f"Fallback={self._fallback.backend_name} err={fallback_error!s}"
                ) from fallback_error


def build_client_from_settings(settings: Settings | None = None) -> StorageClient:
    """Wire storage. GCS primary, S3 fallback (matches our deploy story).

    Picks whichever backend is configured. If both are configured, GCS wins
    primary (videos live in Google Cloud per Yoav's directive 2026-06-02).
    """
    s = settings or get_settings()

    # GCS: configured when we have a bucket AND some form of Google credentials.
    creds_info = build_credentials_info(s)
    gcs_configured = bool(
        s.GCS_BUCKET_NAME
        and (s.GCS_CREDENTIALS_FILE or creds_info or s.GOOGLE_APPLICATION_CREDENTIALS)
    )
    gcs: GCSUploader | None = None
    if gcs_configured:
        gcs = GCSUploader(
            bucket=s.GCS_BUCKET_NAME,
            credentials_file=s.GCS_CREDENTIALS_FILE,
            credentials_info=creds_info if not s.GCS_CREDENTIALS_FILE else None,
        )

    # S3: configured when both AWS keys are set.
    s3_configured = bool(s.AWS_ACCESS_KEY_ID and s.AWS_SECRET_ACCESS_KEY)
    s3: S3Uploader | None = None
    if s3_configured:
        s3 = S3Uploader(
            bucket=s.AWS_BUCKET_NAME,
            access_key_id=s.AWS_ACCESS_KEY_ID,
            secret_access_key=s.AWS_SECRET_ACCESS_KEY,
            region=s.AWS_REGION,
        )

    if gcs and s3:
        return StorageClient(primary=gcs, fallback=s3)
    if gcs:
        return StorageClient(primary=gcs)
    if s3:
        return StorageClient(primary=s3)

    raise ValueError(
        "No storage configured. Need either GCS (GCS_BUCKET_NAME + "
        "GOOGLE_PRIVATE_KEY/GCS_CREDENTIALS_FILE) or AWS S3 "
        "(AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY)."
    )
