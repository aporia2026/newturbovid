"""Tests for the Google service-account credentials helper."""

from __future__ import annotations

from bulkvid.adapters.google_credentials import (
    build_credentials_info,
    have_credentials_configured,
)
from bulkvid.config import Settings


def _s(**overrides) -> Settings:
    base = dict(
        GOOGLE_ACCOUNT_TYPE="service_account",
        GOOGLE_PROJECT_ID="",
        GOOGLE_PRIVATE_KEY_ID="",
        GOOGLE_PRIVATE_KEY="",
        GOOGLE_CLIENT_EMAIL="",
        GOOGLE_CLIENT_ID="",
        GOOGLE_APPLICATION_CREDENTIALS="",
    )
    base.update(overrides)
    return Settings(**base)


def test_returns_none_when_nothing_configured() -> None:
    assert build_credentials_info(_s()) is None


def test_returns_none_when_private_key_present_but_email_missing() -> None:
    assert build_credentials_info(
        _s(GOOGLE_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\nABC\n-----END PRIVATE KEY-----\n")
    ) is None


def test_builds_full_dict_from_inline_env_vars() -> None:
    info = build_credentials_info(
        _s(
            GOOGLE_PROJECT_ID="amit-tts",
            GOOGLE_PRIVATE_KEY_ID="key-id-abc",
            GOOGLE_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\nABCDEF\n-----END PRIVATE KEY-----\n",
            GOOGLE_CLIENT_EMAIL="geminiapi@amit-tts.iam.gserviceaccount.com",
            GOOGLE_CLIENT_ID="1234567890",
        )
    )
    assert info is not None
    assert info["type"] == "service_account"
    assert info["project_id"] == "amit-tts"
    assert info["private_key_id"] == "key-id-abc"
    assert info["client_email"] == "geminiapi@amit-tts.iam.gserviceaccount.com"
    assert info["client_id"] == "1234567890"
    # PEM newlines preserved.
    assert "BEGIN PRIVATE KEY" in info["private_key"]
    assert "END PRIVATE KEY" in info["private_key"]


def test_normalises_escaped_newlines_in_private_key() -> None:
    # Some env loaders preserve literal \n instead of expanding them.
    raw_with_escaped = "-----BEGIN PRIVATE KEY-----\\nABC\\n-----END PRIVATE KEY-----\\n"
    info = build_credentials_info(
        _s(
            GOOGLE_PROJECT_ID="p",
            GOOGLE_CLIENT_EMAIL="x@y.iam.gserviceaccount.com",
            GOOGLE_CLIENT_ID="1",
            GOOGLE_PRIVATE_KEY=raw_with_escaped,
        )
    )
    assert info is not None
    # No literal backslash-n sequences remain in the output.
    assert "\\n" not in info["private_key"]
    # Real newlines are present.
    assert "\n" in info["private_key"]


def test_client_x509_cert_url_encodes_at_sign() -> None:
    info = build_credentials_info(
        _s(
            GOOGLE_PROJECT_ID="p",
            GOOGLE_CLIENT_EMAIL="svc@proj.iam.gserviceaccount.com",
            GOOGLE_CLIENT_ID="1",
            GOOGLE_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
        )
    )
    assert info is not None
    assert "%40" in info["client_x509_cert_url"]
    assert "@" not in info["client_x509_cert_url"]


def test_have_credentials_configured_file_path_wins() -> None:
    s = _s(GOOGLE_APPLICATION_CREDENTIALS="/path/to/key.json")
    assert have_credentials_configured(s) is True


def test_have_credentials_configured_inline_env_works() -> None:
    s = _s(
        GOOGLE_PROJECT_ID="p",
        GOOGLE_CLIENT_EMAIL="x@y.iam.gserviceaccount.com",
        GOOGLE_CLIENT_ID="1",
        GOOGLE_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
    )
    assert have_credentials_configured(s) is True


def test_have_credentials_configured_neither_returns_false() -> None:
    assert have_credentials_configured(_s()) is False
