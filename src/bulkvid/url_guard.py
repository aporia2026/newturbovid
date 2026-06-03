"""URL safety guard for user-supplied URLs (plan §7).

Enforces HTTPS-only and blocks SSRF to private/internal/loopback/link-local
addresses (e.g. ``http://169.254.169.254/`` cloud metadata, ``http://10.x``,
``http://localhost``). Applied at the submission boundary (``routes/jobs.py``)
so unsafe URLs never enter the job queue.

Known residuals (documented, not yet closed):
  - DNS-rebinding (TOCTOU) between this check and the eventual fetch.
  - A second guard at the download layer for any future ingestion path that
    does not pass through ``/jobs``.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_IpAddr = ipaddress.IPv4Address | ipaddress.IPv6Address


class UnsafeUrlError(Exception):
    """A user-supplied URL is not a safe, public HTTPS URL."""


def _addr_is_public(addr: _IpAddr) -> bool:
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _resolve_host(host: str, port: int) -> list[str]:
    """Resolve a hostname to its IP strings. Module-level so tests can patch it."""
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


def assert_safe_url(url: str, *, what: str = "URL") -> None:
    """Raise ``UnsafeUrlError`` unless ``url`` is HTTPS and points only at public IPs."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme != "https":
        raise UnsafeUrlError(
            f"{what} must use https (got {parsed.scheme or 'no scheme'!r})"
        )
    host = parsed.hostname
    if not host:
        raise UnsafeUrlError(f"{what} has no host")

    # Literal IP in the URL — check directly, no DNS.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _addr_is_public(literal):
            raise UnsafeUrlError(f"{what} points at a non-public address ({host})")
        return

    # Hostname — resolve and reject if ANY resolved IP is non-public.
    try:
        ips = _resolve_host(host, parsed.port or 443)
    except OSError as e:
        raise UnsafeUrlError(f"{what} host does not resolve ({host})") from e
    if not ips:
        raise UnsafeUrlError(f"{what} host did not resolve ({host})")
    for ip in ips:
        if not _addr_is_public(ipaddress.ip_address(ip)):
            raise UnsafeUrlError(f"{what} resolves to a non-public address ({ip})")
