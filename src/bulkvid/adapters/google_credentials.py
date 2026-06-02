"""Shared Google service-account credentials helper.

Supports two configuration modes:

  1. **File path** — set ``GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json``.
     Used when the host has a real filesystem and the JSON is on disk
     (Hetzner Docker volume mounts, local dev).

  2. **Inline env vars** — paste each field from the service account JSON
     directly into ``.env``:
        ``GOOGLE_ACCOUNT_TYPE``, ``GOOGLE_PROJECT_ID``,
        ``GOOGLE_PRIVATE_KEY_ID``, ``GOOGLE_PRIVATE_KEY``,
        ``GOOGLE_CLIENT_EMAIL``, ``GOOGLE_CLIENT_ID``.

     PythonAnywhere-friendly: no need to upload + manage a JSON file.

The file path wins when both are set. Returns ``None`` when neither
mode is configured — the caller decides whether that's a soft no-op or
a hard error.

Plan §7 (Security: keys in env only, never in code).
"""

from __future__ import annotations

from typing import Any

from bulkvid.config import Settings, get_settings
from bulkvid.logging import get_logger

_log = get_logger("gcreds")


def build_credentials_info(settings: Settings | None = None) -> dict[str, Any] | None:
    """Return a service-account ``info`` dict (the parsed JSON shape).

    Returns ``None`` when neither mode is configured. The dict can be passed
    to ``service_account.Credentials.from_service_account_info(info)`` or
    ``google.cloud.X.Client(credentials=...)`` after wrapping.
    """
    s = settings or get_settings()

    if not s.GOOGLE_PRIVATE_KEY:
        return None

    if not s.GOOGLE_CLIENT_EMAIL:
        _log.warning(
            "google_creds_partial",
            note="GOOGLE_PRIVATE_KEY set but GOOGLE_CLIENT_EMAIL missing",
        )
        return None

    # Some env loaders preserve literal ``\n`` instead of expanding them;
    # service-account JSON requires real newlines in the PEM.
    private_key = s.GOOGLE_PRIVATE_KEY.replace("\\n", "\n")

    client_email = s.GOOGLE_CLIENT_EMAIL
    info: dict[str, Any] = {
        "type": s.GOOGLE_ACCOUNT_TYPE or "service_account",
        "project_id": s.GOOGLE_PROJECT_ID,
        "private_key_id": s.GOOGLE_PRIVATE_KEY_ID,
        "private_key": private_key,
        "client_email": client_email,
        "client_id": s.GOOGLE_CLIENT_ID,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": (
            "https://www.googleapis.com/robot/v1/metadata/x509/"
            + client_email.replace("@", "%40")
        ),
        "universe_domain": "googleapis.com",
    }
    return info


def have_credentials_configured(settings: Settings | None = None) -> bool:
    """True if EITHER a file path or full inline env vars are set."""
    s = settings or get_settings()
    if s.GOOGLE_APPLICATION_CREDENTIALS:
        return True
    return build_credentials_info(s) is not None


def build_vertex_credentials_info(
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """Return credentials for Vertex AI (Gemini TTS), or None when unset.

    Vertex AI and storage are often in DIFFERENT GCP projects. This helper
    prefers the ``VERTEX_AI_*`` env vars when set; otherwise it falls back
    to the general ``GOOGLE_*`` set (legacy/single-project deploys).
    """
    s = settings or get_settings()

    if s.VERTEX_AI_PRIVATE_KEY and s.VERTEX_AI_CLIENT_EMAIL:
        client_email = s.VERTEX_AI_CLIENT_EMAIL
        private_key = s.VERTEX_AI_PRIVATE_KEY.replace("\\n", "\n")
        return {
            "type": "service_account",
            "project_id": s.VERTEX_AI_PROJECT_ID,
            "private_key_id": s.VERTEX_AI_PRIVATE_KEY_ID,
            "private_key": private_key,
            "client_email": client_email,
            "client_id": s.VERTEX_AI_CLIENT_ID,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url": (
                "https://www.googleapis.com/robot/v1/metadata/x509/"
                + client_email.replace("@", "%40")
            ),
            "universe_domain": "googleapis.com",
        }

    # Fallback: share the storage credentials. Works when both are in the
    # same project OR when cross-project IAM has been granted.
    return build_credentials_info(s)
