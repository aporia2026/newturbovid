"""Operator-pasted URL normalization.

Sheet columns are filled by hand, and a hand-typed or copied URL routinely
arrives without a scheme — ``www.example.com/p?q=x`` rather than
``https://www.example.com/p?q=x``. Browsers add the scheme silently, so the
value looks perfectly fine in the cell, but every HTTP client rejects it.

Chat 2026-08-17: a whole batch failed with
``Invalid URL: 'www.drexur.com/dsr?q=...'`` before a single fetch was
attempted, because ``ArticleFetcher.fetch`` requires an ``http(s)://`` prefix.
No video was produced for any of those rows.

Kept stdlib-only so both ``models.row`` and the adapters can import it.

Plan: ``_plans/2026-08-17-schemeless-article-url.md``.
"""

from __future__ import annotations

# Schemes we can actually fetch. Anything else (``ftp://``, ``mailto:``) is
# left untouched so the caller still rejects it with its own clear error.
_FETCHABLE_SCHEMES = ("http://", "https://")


def normalize_url(url: str) -> str:
    """Add a missing ``https://`` to an operator-pasted URL.

    Only prefixes when what precedes the first ``/``, ``?`` or ``#`` actually
    looks like a hostname, so free text in the cell stays untouched and still
    fails loudly rather than turning into a bogus request. Whitespace is
    trimmed (cells are pasted, and a trailing space is invisible). Returns
    ``""`` for empty input, which callers already treat as "no URL".
    """
    text = (url or "").strip()
    if not text:
        return ""
    if text.startswith(_FETCHABLE_SCHEMES):
        return text
    if text.startswith("//"):                 # protocol-relative
        return "https:" + text
    if "://" in text:                         # some other scheme — leave alone
        return text
    host = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if _looks_like_host(host):
        return "https://" + text
    return text


def _looks_like_host(host: str) -> bool:
    """A dotted, whitespace-free label — ``www.drexur.com``, not ``seized cars``."""
    return (
        "." in host
        and not host.startswith((".", "-"))
        and not host.endswith((".", "-"))
        and not any(c.isspace() for c in host)
    )
