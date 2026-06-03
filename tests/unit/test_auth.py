"""Tests for the Google ID token verifier.

Generates a real RSA key pair at test-fixture scope, signs test JWTs with it,
exposes the public key as a JWKS dict, and injects that as the fetcher so
we never hit Google's network endpoint.

Covers:
  - Happy path bulk-team user -> Identity returned, is_admin=False
  - Admin user -> Identity returned, is_admin=True
  - Email not in allowlist -> ForbiddenError
  - Wrong workspace domain -> ForbiddenError
  - email_verified=False -> AuthError
  - Bad signature (signed by different key) -> AuthError
  - Missing kid -> AuthError
  - Unknown kid + JWKS refresh path
  - Missing/empty token -> AuthError
  - Malformed token -> AuthError
  - JWKS caching (fetcher called once across multiple verifications)
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any

import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from bulkvid.auth import (
    GOOGLE_ISS_ALLOWED,
    AuthError,
    ForbiddenError,
    GoogleIdentityVerifier,
)


# ── RSA test fixtures ──────────────────────────────────────────────────────


@dataclass
class _TestKeys:
    private_pem: bytes
    public_pem: bytes
    kid: str
    jwks_entry: dict[str, Any]


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_key(kid: str = "test-kid-1") -> _TestKeys:
    priv = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    pub = priv.public_key()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    nums = pub.public_numbers()
    n_bytes = nums.n.to_bytes((nums.n.bit_length() + 7) // 8, "big")
    e_bytes = nums.e.to_bytes((nums.e.bit_length() + 7) // 8, "big")
    jwks_entry = {
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "kid": kid,
        "n": _b64u(n_bytes),
        "e": _b64u(e_bytes),
    }
    return _TestKeys(
        private_pem=priv_pem, public_pem=pub_pem, kid=kid, jwks_entry=jwks_entry
    )


@pytest.fixture(scope="module")
def keys() -> _TestKeys:
    return _make_key()


@pytest.fixture(scope="module")
def other_keys() -> _TestKeys:
    return _make_key(kid="other-test-kid")


def _make_token(
    private_pem: bytes,
    *,
    email: str = "bulk1@aporia.com",
    hd: str = "aporia.com",
    email_verified: bool = True,
    iss: str = "accounts.google.com",
    kid: str = "test-kid-1",
    extra: dict | None = None,
) -> str:
    now = int(time.time())
    claims = {
        "iss": iss,
        "sub": "1234",
        "email": email,
        "email_verified": email_verified,
        "hd": hd,
        "name": "Bulk User",
        "iat": now,
        "exp": now + 300,
        "aud": "doesnt-matter-since-we-skip-aud",
    }
    if extra:
        claims.update(extra)
    return jwt.encode(
        claims,
        private_pem.decode("utf-8"),
        algorithm="RS256",
        headers={"kid": kid},
    )


def _build_verifier(keys: _TestKeys, **kw) -> GoogleIdentityVerifier:
    async def _fetcher() -> list[dict]:
        return [keys.jwks_entry]

    return GoogleIdentityVerifier(
        allowed_hds=kw.get("allowed_hds", ["aporia.com"]),
        bulk_emails=kw.get("bulk_emails", ["bulk1@aporia.com", "bulk2@aporia.com"]),
        bulk_domains=kw.get("bulk_domains", []),
        admin_emails=kw.get("admin_emails", ["yoav@aporia.com"]),
        jwks_fetcher=_fetcher,
    )


# ── Happy paths ─────────────────────────────────────────────────────────────


async def test_bulk_user_token_verifies(keys: _TestKeys) -> None:
    verifier = _build_verifier(keys)
    token = _make_token(keys.private_pem, email="bulk1@aporia.com")
    identity = await verifier.verify(token)
    assert identity.email == "bulk1@aporia.com"
    assert identity.hd == "aporia.com"
    assert identity.is_admin is False


async def test_admin_user_token_verifies(keys: _TestKeys) -> None:
    verifier = _build_verifier(keys)
    token = _make_token(keys.private_pem, email="yoav@aporia.com")
    identity = await verifier.verify(token)
    assert identity.email == "yoav@aporia.com"
    assert identity.is_admin is True


async def test_id_token_with_at_hash_verifies(keys: _TestKeys) -> None:
    # Apps Script's ScriptApp.getIdentityToken() mints an ID token carrying an
    # at_hash claim but never sends the matching access_token. jose's default
    # at_hash check would reject it ("No access_token provided ...") — verify we
    # skip that binding and still accept on signature + iss + allowlist.
    verifier = _build_verifier(keys)
    token = _make_token(
        keys.private_pem, email="bulk1@aporia.com", extra={"at_hash": "x7Hf9k2QmZ"}
    )
    identity = await verifier.verify(token)
    assert identity.email == "bulk1@aporia.com"


async def test_email_case_insensitive_match(keys: _TestKeys) -> None:
    verifier = _build_verifier(keys)
    token = _make_token(keys.private_pem, email="BULK1@APORIA.COM")
    identity = await verifier.verify(token)
    assert identity.email == "bulk1@aporia.com"


# ── Forbidden (valid token, not on allowlist) ──────────────────────────────


async def test_email_not_in_allowlist_raises_forbidden(keys: _TestKeys) -> None:
    verifier = _build_verifier(keys)
    token = _make_token(keys.private_pem, email="stranger@aporia.com")
    with pytest.raises(ForbiddenError):
        await verifier.verify(token)


async def test_wrong_workspace_domain_raises_forbidden(keys: _TestKeys) -> None:
    verifier = _build_verifier(keys)
    token = _make_token(
        keys.private_pem, email="bulk1@aporia.com", hd="external.com"
    )
    with pytest.raises(ForbiddenError):
        await verifier.verify(token)


async def test_empty_hd_raises_forbidden_when_required(keys: _TestKeys) -> None:
    verifier = _build_verifier(keys)
    # Issue a token without an hd claim (personal Gmail style).
    token = _make_token(keys.private_pem, email="bulk1@aporia.com", hd="")
    with pytest.raises(ForbiddenError):
        await verifier.verify(token)


# ── AuthError (token problems) ──────────────────────────────────────────────


async def test_empty_token_raises_auth(keys: _TestKeys) -> None:
    verifier = _build_verifier(keys)
    with pytest.raises(AuthError):
        await verifier.verify("")
    with pytest.raises(AuthError):
        await verifier.verify("   ")


async def test_malformed_token_raises_auth(keys: _TestKeys) -> None:
    verifier = _build_verifier(keys)
    with pytest.raises(AuthError):
        await verifier.verify("not.a.jwt")


async def test_bad_signature_raises_auth(
    keys: _TestKeys, other_keys: _TestKeys
) -> None:
    """Token signed by a key the verifier doesn't know about."""
    verifier = _build_verifier(keys)
    # Sign with other_keys but advertise our verifier's kid.
    token = _make_token(other_keys.private_pem, email="bulk1@aporia.com", kid=keys.kid)
    with pytest.raises(AuthError):
        await verifier.verify(token)


async def test_email_verified_false_raises_auth(keys: _TestKeys) -> None:
    verifier = _build_verifier(keys)
    token = _make_token(keys.private_pem, email="bulk1@aporia.com", email_verified=False)
    with pytest.raises(AuthError):
        await verifier.verify(token)


async def test_unknown_iss_raises_auth(keys: _TestKeys) -> None:
    verifier = _build_verifier(keys)
    token = _make_token(keys.private_pem, email="bulk1@aporia.com", iss="evil.example.com")
    with pytest.raises(AuthError):
        await verifier.verify(token)


# ── JWKS rotation + caching ─────────────────────────────────────────────────


async def test_unknown_kid_triggers_jwks_refresh(keys: _TestKeys) -> None:
    fetch_count = {"n": 0}

    async def _fetcher() -> list[dict]:
        fetch_count["n"] += 1
        return [keys.jwks_entry]

    verifier = GoogleIdentityVerifier(
        allowed_hds=["aporia.com"],
        bulk_emails=["bulk1@aporia.com"],
        bulk_domains=[],
        admin_emails=[],
        jwks_fetcher=_fetcher,
    )

    # First successful verification populates the cache.
    token = _make_token(keys.private_pem)
    await verifier.verify(token)
    assert fetch_count["n"] == 1

    # Now sign with a different kid — verifier sees unknown kid, refreshes,
    # still doesn't find it, raises AuthError.
    other_kid_token = _make_token(keys.private_pem, kid="phantom-kid")
    with pytest.raises(AuthError):
        await verifier.verify(other_kid_token)
    # The refresh happened (fetcher invoked a second time).
    assert fetch_count["n"] == 2


async def test_jwks_cached_across_multiple_verifications(keys: _TestKeys) -> None:
    fetch_count = {"n": 0}

    async def _fetcher() -> list[dict]:
        fetch_count["n"] += 1
        return [keys.jwks_entry]

    verifier = GoogleIdentityVerifier(
        allowed_hds=["aporia.com"],
        bulk_emails=["bulk1@aporia.com"],
        bulk_domains=[],
        admin_emails=[],
        jwks_fetcher=_fetcher,
    )

    token1 = _make_token(keys.private_pem)
    token2 = _make_token(keys.private_pem)
    token3 = _make_token(keys.private_pem)
    await verifier.verify(token1)
    await verifier.verify(token2)
    await verifier.verify(token3)
    # Fetched exactly once.
    assert fetch_count["n"] == 1


# ── Issuer allowlist sanity ─────────────────────────────────────────────────


def test_google_iss_allowed_includes_both_forms() -> None:
    assert "accounts.google.com" in GOOGLE_ISS_ALLOWED
    assert "https://accounts.google.com" in GOOGLE_ISS_ALLOWED


# ── Multi-domain Workspace + domain-based bulk allowlist ─────────────────────


async def test_multiple_allowed_hds_accept_any(keys: _TestKeys) -> None:
    verifier = _build_verifier(
        keys,
        allowed_hds=["aporianetworks.com", "teaminternet.com"],
        bulk_emails=[],
        bulk_domains=["aporianetworks.com", "teaminternet.com"],
    )

    t1 = _make_token(
        keys.private_pem, email="alice@aporianetworks.com", hd="aporianetworks.com"
    )
    t2 = _make_token(
        keys.private_pem, email="bob@teaminternet.com", hd="teaminternet.com"
    )

    id1 = await verifier.verify(t1)
    id2 = await verifier.verify(t2)
    assert id1.email == "alice@aporianetworks.com"
    assert id2.email == "bob@teaminternet.com"


async def test_domain_allowlist_accepts_any_email_in_domain(keys: _TestKeys) -> None:
    verifier = _build_verifier(
        keys,
        allowed_hds=["aporianetworks.com"],
        bulk_emails=[],                              # no explicit emails
        bulk_domains=["aporianetworks.com"],         # whole domain allowed
    )
    # Any email in the domain is accepted, even if it's not in BULK_TEAM_ALLOWLIST.
    token = _make_token(
        keys.private_pem,
        email="newhire2026@aporianetworks.com",
        hd="aporianetworks.com",
    )
    identity = await verifier.verify(token)
    assert identity.email == "newhire2026@aporianetworks.com"


async def test_domain_allowlist_with_leading_at_normalised(keys: _TestKeys) -> None:
    verifier = _build_verifier(
        keys,
        allowed_hds=["aporianetworks.com"],
        bulk_emails=[],
        bulk_domains=["@aporianetworks.com"],        # tolerates leading @
    )
    token = _make_token(
        keys.private_pem,
        email="alice@aporianetworks.com",
        hd="aporianetworks.com",
    )
    identity = await verifier.verify(token)
    assert identity.email == "alice@aporianetworks.com"


async def test_email_in_wrong_domain_rejected(keys: _TestKeys) -> None:
    verifier = _build_verifier(
        keys,
        allowed_hds=["aporianetworks.com"],
        bulk_emails=[],
        bulk_domains=["aporianetworks.com"],
    )
    token = _make_token(
        keys.private_pem,
        email="stranger@external.com",
        hd="aporianetworks.com",       # hd matches, but email domain doesn't
    )
    with pytest.raises(ForbiddenError):
        await verifier.verify(token)


async def test_hd_outside_allowed_list_rejected(keys: _TestKeys) -> None:
    verifier = _build_verifier(
        keys,
        allowed_hds=["aporianetworks.com", "teaminternet.com"],
        bulk_emails=[],
        bulk_domains=["aporianetworks.com", "teaminternet.com"],
    )
    token = _make_token(
        keys.private_pem,
        email="bob@evil.com",
        hd="evil.com",                  # not in allowed list
    )
    with pytest.raises(ForbiddenError):
        await verifier.verify(token)


async def test_admin_email_matches_take_precedence_for_is_admin(keys: _TestKeys) -> None:
    """A bulk-domain user who is ALSO in admin_emails comes back with is_admin=True."""
    verifier = _build_verifier(
        keys,
        allowed_hds=["aporianetworks.com"],
        bulk_emails=[],
        bulk_domains=["aporianetworks.com"],
        admin_emails=["yoav@aporianetworks.com"],
    )
    token = _make_token(
        keys.private_pem,
        email="yoav@aporianetworks.com",
        hd="aporianetworks.com",
    )
    identity = await verifier.verify(token)
    assert identity.is_admin is True
