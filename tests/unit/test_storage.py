"""Tests for the storage adapter.

The real boto3 / google-cloud-storage clients are injected as fakes so we never
touch AWS or GCS. The adapter just orchestrates them.

Covers:
  - S3Uploader: bytes uploaded, URL has correct shape, ContentType set, ACL fallback survives
  - GCSUploader: bytes uploaded, public URL returned, make_public failure tolerated
  - StorageClient: primary success path, primary fail -> fallback used, both fail -> StorageError
  - URL encoding of keys with spaces
  - Constructor validation
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bulkvid.adapters.storage import (
    COST_PER_UPLOAD_USD,
    GCSUploader,
    S3Uploader,
    StorageClient,
    StorageError,
    UploadResult,
)


# ── S3Uploader (with mock boto3 client) ─────────────────────────────────────


def _make_fake_s3() -> MagicMock:
    fake = MagicMock()
    fake.upload_fileobj = MagicMock(return_value=None)
    fake.put_object_acl = MagicMock(return_value=None)
    return fake


async def test_s3_upload_bytes_returns_public_url() -> None:
    fake = _make_fake_s3()
    uploader = S3Uploader(
        bucket="my-bucket",
        access_key_id="x",
        secret_access_key="y",
        client=fake,
    )
    url = await uploader.upload_bytes(b"data", "folder/file.mp4", "video/mp4")
    assert url == "https://my-bucket.s3.amazonaws.com/folder/file.mp4"

    # upload_fileobj was called once with the right bucket+key+ContentType.
    fake.upload_fileobj.assert_called_once()
    call_args = fake.upload_fileobj.call_args
    assert call_args.args[1] == "my-bucket"
    assert call_args.args[2] == "folder/file.mp4"
    assert call_args.kwargs["ExtraArgs"]["ContentType"] == "video/mp4"


async def test_s3_url_encodes_special_characters_in_key() -> None:
    fake = _make_fake_s3()
    uploader = S3Uploader(
        bucket="b",
        access_key_id="x",
        secret_access_key="y",
        client=fake,
    )
    url = await uploader.upload_bytes(b"d", "folder name/with spaces.mp4", "video/mp4")
    assert "%20" in url       # spaces encoded
    assert "folder%20name" in url


async def test_s3_acl_failure_does_not_break_upload() -> None:
    from botocore.exceptions import ClientError

    fake = _make_fake_s3()
    fake.put_object_acl.side_effect = ClientError(
        {"Error": {"Code": "AccessControlListNotSupported", "Message": "ACLs off"}},
        "PutObjectAcl",
    )
    uploader = S3Uploader(
        bucket="b",
        access_key_id="x",
        secret_access_key="y",
        client=fake,
    )
    # Upload still returns the URL even though put_object_acl fails.
    url = await uploader.upload_bytes(b"d", "k", "video/mp4")
    assert url.startswith("https://b.s3.amazonaws.com/")


def test_s3_constructor_rejects_empty_bucket() -> None:
    with pytest.raises(ValueError):
        S3Uploader(bucket="", access_key_id="x", secret_access_key="y", client=MagicMock())


def test_s3_constructor_rejects_missing_credentials_when_no_client_injected() -> None:
    with pytest.raises(ValueError):
        S3Uploader(bucket="b", access_key_id="", secret_access_key="")


# ── GCSUploader (with mock storage.Client) ──────────────────────────────────


def _make_fake_gcs_client(public_url: str = "https://storage.googleapis.com/b/k") -> MagicMock:
    fake_blob = MagicMock()
    fake_blob.public_url = public_url
    fake_blob.upload_from_string = MagicMock(return_value=None)
    fake_blob.make_public = MagicMock(return_value=None)

    fake_bucket = MagicMock()
    fake_bucket.name = "b"
    fake_bucket.blob = MagicMock(return_value=fake_blob)

    fake_client = MagicMock()
    fake_client.bucket = MagicMock(return_value=fake_bucket)
    return fake_client


async def test_gcs_upload_returns_public_url() -> None:
    fake_client = _make_fake_gcs_client("https://storage.googleapis.com/b/k1.mp4")
    uploader = GCSUploader(bucket="b", client=fake_client)
    url = await uploader.upload_bytes(b"data", "k1.mp4", "video/mp4")
    assert url == "https://storage.googleapis.com/b/k1.mp4"


async def test_gcs_make_public_failure_tolerated() -> None:
    fake_blob = MagicMock()
    fake_blob.public_url = "https://storage.googleapis.com/b/k.mp4"
    fake_blob.upload_from_string = MagicMock(return_value=None)
    fake_blob.make_public.side_effect = RuntimeError("ACL not supported on UBLA bucket")

    fake_bucket = MagicMock(name="bucket")
    fake_bucket.name = "b"
    fake_bucket.blob = MagicMock(return_value=fake_blob)

    fake_client = MagicMock()
    fake_client.bucket = MagicMock(return_value=fake_bucket)

    uploader = GCSUploader(bucket="b", client=fake_client)
    url = await uploader.upload_bytes(b"d", "k.mp4", "video/mp4")
    # Still returns the URL.
    assert url.startswith("https://storage.googleapis.com/")


def test_gcs_constructor_rejects_empty_bucket() -> None:
    with pytest.raises(ValueError):
        GCSUploader(bucket="", client=MagicMock())


# ── StorageClient (primary + fallback orchestration) ────────────────────────


class _FakeUploader:
    """A minimal in-memory uploader matching the _AsyncUploader protocol."""

    def __init__(self, backend_name: str, raise_exc: Exception | None = None) -> None:
        self.backend_name = backend_name
        self._raise = raise_exc
        self.calls: list[tuple[bytes, str, str]] = []

    async def upload_bytes(self, data: bytes, key: str, content_type: str) -> str:
        self.calls.append((data, key, content_type))
        if self._raise is not None:
            raise self._raise
        return f"https://{self.backend_name}.test/{key}"


async def test_primary_success_skips_fallback() -> None:
    primary = _FakeUploader("s3")
    fallback = _FakeUploader("gcs")
    client = StorageClient(primary=primary, fallback=fallback)

    result = await client.upload_bytes(b"data", "k.mp4", "video/mp4")

    assert isinstance(result, UploadResult)
    assert result.backend == "s3"
    assert result.url == "https://s3.test/k.mp4"
    assert result.bytes_written == 4
    assert result.cost_usd == COST_PER_UPLOAD_USD
    # Primary was called once; fallback was NOT.
    assert len(primary.calls) == 1
    assert len(fallback.calls) == 0


async def test_primary_failure_falls_back_to_secondary() -> None:
    primary = _FakeUploader("s3", raise_exc=RuntimeError("s3 down"))
    fallback = _FakeUploader("gcs")
    client = StorageClient(primary=primary, fallback=fallback)

    result = await client.upload_bytes(b"data", "k.mp4", "video/mp4")

    assert result.backend == "gcs"
    assert result.url == "https://gcs.test/k.mp4"
    assert len(primary.calls) == 1
    assert len(fallback.calls) == 1


async def test_both_fail_raises_storage_error() -> None:
    primary = _FakeUploader("s3", raise_exc=RuntimeError("s3 down"))
    fallback = _FakeUploader("gcs", raise_exc=RuntimeError("gcs down"))
    client = StorageClient(primary=primary, fallback=fallback)

    with pytest.raises(StorageError) as exc:
        await client.upload_bytes(b"data", "k.mp4", "video/mp4")
    # Both backends mentioned in the error so operators can debug from the log.
    assert "s3 down" in str(exc.value)
    assert "gcs down" in str(exc.value)


async def test_no_fallback_propagates_primary_error_as_storage_error() -> None:
    primary = _FakeUploader("s3", raise_exc=RuntimeError("s3 down"))
    client = StorageClient(primary=primary, fallback=None)

    with pytest.raises(StorageError):
        await client.upload_bytes(b"data", "k.mp4", "video/mp4")


# ── Cost constant sanity ────────────────────────────────────────────────────


def test_cost_constant_positive() -> None:
    assert COST_PER_UPLOAD_USD > 0
