"""Google OAuth ID token verification.

Apps Script obtains an ID token via ``ScriptApp.getIdentityToken()`` and sends
it in the ``Authorization: Bearer <jwt>`` header. This module:

  1. Verifies the JWT signature against Google's published JWKS
  2. Checks the ``iss`` claim
  3. Checks the Workspace ``hd`` claim against ``ALLOWED_HD``
  4. Checks ``email`` against ``BULK_TEAM_ALLOWLIST`` (or ``ADMIN_ALLOWLIST``)
  5. Returns an ``Identity`` to the route handler

For tests, the JWKS fetcher is injectable so we never touch the network.

Plan §7 (Security & Safety -> Authentication).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx
from jose import jwt
from jose.exceptions import JWTError

from bulkvid.config import Settings, get_settings
from bulkvid.logging import get_logger

_log = get_logger("auth")


GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISS_ALLOWED = ("accounts.google.com", "https://accounts.google.com")
JWKS_CACHE_SECONDS = 3600


# ── Result ───────────────────────────────────────────────────────────────────


@dataclass
class Identity:
    email: str
    hd: str | None
    name: str | None
    is_admin: bool


# ── Errors ───────────────────────────────────────────────────────────────────


class AuthError(Exception):
    """Token failed verification. Maps to HTTP 401 in routes."""


class ForbiddenError(Exception):
    """Token valid but user not on allowlist. Maps to HTTP 403 in routes."""


# ── JWKS cache (per process, refreshed on miss) ──────────────────────────────


JWKSFetcher = Callable[[], Awaitable[list[dict]]]


async def _default_jwks_fetcher() -> list[dict]:
    async with httpx.AsyncClient(timeout=10.0) as c:
        resp = await c.get(GOOGLE_JWKS_URL)
        resp.raise_for_status()
        return resp.json().get("keys", [])


class _JWKSCache:
    def __init__(self, fetcher: JWKSFetcher) -> None:
        self._fetcher = fetcher
        self._keys: list[dict] = []
        self._expires_at = 0.0

    async def get_keys(self) -> list[dict]:
        now = time.monotonic()
        if not self._keys or now >= self._expires_at:
            self._keys = await self._fetcher()
            self._expires_at = now + JWKS_CACHE_SECONDS
        return self._keys

    def invalidate(self) -> None:
        self._expires_at = 0.0


# ── Verifier ─────────────────────────────────────────────────────────────────


class GoogleIdentityVerifier:
    """Verify a Google ID token and check it's on the allowlist."""

    def __init__(
        self,
        *,
        allowed_hds: list[str],
        bulk_emails: list[str],
        bulk_domains: list[str],
        admin_emails: list[str],
        jwks_fetcher: JWKSFetcher | None = None,
    ) -> None:
        self._allowed_hds = {d.lower().strip() for d in allowed_hds if d.strip()}
        self._bulk = {e.lower() for e in bulk_emails}
        self._bulk_domains = {d.lower().strip().lstrip("@") for d in bulk_domains if d.strip()}
        self._admin = {e.lower() for e in admin_emails}
        self._jwks = _JWKSCache(jwks_fetcher or _default_jwks_fetcher)

    async def verify(self, bearer_token: str) -> Identity:
        """Verify token + allowlist. Raises ``AuthError`` or ``ForbiddenError``."""
        if not bearer_token or not bearer_token.strip():
            raise AuthError("missing token")

        # ── Look up the signing key by kid ───────────────────────────────
        try:
            unverified_header = jwt.get_unverified_header(bearer_token)
        except JWTError as e:
            raise AuthError(f"malformed token header: {e}") from e

        kid = unverified_header.get("kid")
        if not kid:
            raise AuthError("token missing kid")

        signing_key = await self._find_key(kid)
        if signing_key is None:
            # JWKS rotation — refresh once.
            self._jwks.invalidate()
            signing_key = await self._find_key(kid)
        if signing_key is None:
            raise AuthError(f"unknown signing key kid={kid}")

        # ── Verify signature + standard claims ───────────────────────────
        try:
            claims = jwt.decode(
                bearer_token,
                signing_key,
                algorithms=[signing_key.get("alg", "RS256")],
                options={
                    "verify_aud": False,        # plan §7: aud is the Apps Script project,
                                                # not a value the backend can pre-register
                    "verify_at_hash": False,    # Apps Script sends the ID token only (no
                                                # access_token), so the at_hash binding check
                                                # cannot run — signature + iss + allowlist stand.
                },
                issuer=list(GOOGLE_ISS_ALLOWED),
            )
        except JWTError as e:
            raise AuthError(f"signature/claim verification failed: {e}") from e

        # ── App-level allowlist checks ──────────────────────────────────
        email = (claims.get("email") or "").lower().strip()
        if not email:
            raise AuthError("token missing email claim")
        if not claims.get("email_verified", False):
            raise AuthError("email not verified")

        hd = (claims.get("hd") or "").lower().strip() or None
        if self._allowed_hds and hd not in self._allowed_hds:
            _log.warning(
                "verify_hd_mismatch",
                email=email,
                hd=hd,
                expected=sorted(self._allowed_hds),
            )
            raise ForbiddenError(
                f"hd '{hd}' does not match allowed domains "
                f"{sorted(self._allowed_hds)}"
            )

        domain = email.split("@", 1)[1] if "@" in email else ""
        is_admin = email in self._admin
        in_bulk_email = email in self._bulk
        in_bulk_domain = domain in self._bulk_domains
        if not (in_bulk_email or in_bulk_domain or is_admin):
            _log.warning(
                "verify_not_in_allowlist", email=email, domain=domain
            )
            raise ForbiddenError(f"email '{email}' not in allowlist")

        identity = Identity(
            email=email, hd=hd, name=claims.get("name"), is_admin=is_admin
        )
        _log.info(
            "verify_ok",
            email=email,
            hd=hd,
            is_admin=is_admin,
            kid=kid,
        )
        return identity

    async def _find_key(self, kid: str) -> dict | None:
        keys = await self._jwks.get_keys()
        return next((k for k in keys if k.get("kid") == kid), None)


def build_verifier_from_settings(
    settings: Settings | None = None,
    *,
    jwks_fetcher: JWKSFetcher | None = None,
) -> GoogleIdentityVerifier:
    s = settings or get_settings()
    return GoogleIdentityVerifier(
        allowed_hds=s.allowed_hd_list,
        bulk_emails=s.bulk_team_emails,
        bulk_domains=s.bulk_team_domains_list,
        admin_emails=s.admin_emails,
        jwks_fetcher=jwks_fetcher,
    )
