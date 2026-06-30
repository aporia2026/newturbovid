# Facebook image download — TLS-fingerprint bypass via curl_cffi

**Date**: 2026-06-30
**Owner**: Yoav
**Status**: Approved

## Problem

Operator reports the `simple_x4` batch (and every other Manual-image flow)
is now failing 100% on rows whose `Manual Image` URL points at Facebook's
ad redirect endpoint:

```
https://www.facebook.com/ads/image/?d=AQJw06H2tDfjf4YNV85MKwtVFnqECIwNt9S31iGkeylmQNoXN7...
```

The row error in the job log:

```
download from www.facebook.com failed after 3 attempts: ConnectTimeout
download from www.facebook.com failed after 3 attempts: ConnectError: [SSL] unknown error (_ssl.c:1010)
```

This is the same failure mode the
[2026-06-11 plan](2026-06-11-resilient-image-download.md) was supposed to
fix — and it *did*, until recently. The retry + Chrome User-Agent path is
exercising every attempt (the message says "after 3 attempts"); none of
the attempts complete the TLS handshake. The operator has confirmed
**every** FB URL fails today, not intermittent — which rules out flaky
network and points squarely at Facebook's edge rejecting our TLS
fingerprint regardless of UA header.

The Chrome User-Agent string is a header, not a fingerprint. Facebook's
WAF can (and now does) inspect the TLS ClientHello — cipher order, ALPN,
extensions, JA3 hash — and refuse the handshake before any HTTP request
header is read. Python's `httpx`/`ssl` defaults produce a distinctive
fingerprint that does not match any real browser, and FB has decided to
drop it.

## Goals

- Stop the bleeding: FB `/ads/image/?d=...` URLs download successfully again.
- Keep non-FB downloads unchanged (`httpx` stays the default path).
- Degrade gracefully if the new dependency is missing (logged warning,
  fall back to existing httpx behavior — never harder failure than today).
- Preserve the existing `download_image` signature so all six row
  processors stay drop-in.

## Approach

Add `curl_cffi` as a dependency and route **only FB/IG hosts** through
its `AsyncSession` with `impersonate="chrome"`. `curl_cffi` is a Python
binding for `curl-impersonate`, a curl fork that produces the exact TLS
ClientHello a real Chrome browser produces — JA3, ALPN, extension order
included. FB's edge cannot distinguish it from a real browser.

### Host routing

A small set of hosts uses the impersonate backend:

- `facebook.com`, `*.facebook.com` (ad image redirect, scontent fallbacks)
- `fbcdn.net`, `*.fbcdn.net` (FB's CDN origin)
- `instagram.com`, `*.instagram.com`
- `cdninstagram.com`, `*.cdninstagram.com`

Everything else (Rendi.dev result URLs, GCS, S3, custom CDNs, article
scrapes) keeps using `httpx` — no behavior change, no risk of regression.

### Public API

`download_image(url, *, timeout, max_retries, user_agent)` stays the same.
Internal dispatch:

```python
async def download_image(url, *, timeout, max_retries, user_agent):
    host = _host_of(url)
    if _IMPERSONATE_AVAILABLE and _should_impersonate(host):
        return await _download_via_impersonate(url, host, timeout, max_retries)
    return await _download_via_httpx(url, host, timeout, max_retries, user_agent)
```

### Retry semantics — identical to today

Both backends share the same exponential-backoff retry loop
(`0.5 → 1.0 → 2.0 s`, 3 attempts default). What changes is the
exception classification per backend:

- httpx backend: same as today (ConnectError, ConnectTimeout, ReadError,
  ReadTimeout, RemoteProtocolError, WriteError, ssl.SSLError, 5xx).
- impersonate backend: `curl_cffi.requests.exceptions` ConnectionError,
  SSLError, Timeout, ConnectTimeout, HTTPError-on-5xx, etc.

4xx is short-circuited in both (no retry on permanent client errors).

### Graceful degradation

`curl_cffi` is imported at module load. If the import raises
(missing wheel, corrupt install, unsupported platform), the module logs
a one-time warning at info level and `_IMPERSONATE_AVAILABLE` stays
`False`. FB downloads then go through httpx exactly like today — no
crash, no worse outcome than the status quo.

## Alternatives rejected

- **Decode the `d=` blob to call `scontent.fbcdn.net` directly.** Same
  reason as in the 2026-06-11 plan — reverse-engineering FB's redirect
  format is brittle and breaks every time they rotate. `curl_cffi`
  treats the redirect chain like any browser would.
- **Tune `httpx`'s SSL context (cipher list, ALPN, TLS 1.3 GREASE).**
  Got us 90% of the way once and has now stopped working. The TLS
  fingerprint surface area is too large to maintain by hand;
  `curl-impersonate` exists exactly because nobody can keep up with
  Chrome's stable channel drift in their own SSL code.
- **Route ALL downloads through curl_cffi.** Unnecessary risk for
  non-FB hosts (different connect-pool semantics, no respx mock
  compatibility, ~3x our test surface). Scope the change to where it's
  actually needed.
- **Use a third-party proxy (ScraperAPI, Bright Data).** Recurring cost
  per request, adds a hop, introduces a vendor dependency on a path
  that currently has none. Wrong tradeoff for an image download.
- **Pin to an older Chrome impersonate target.** `curl_cffi`'s default
  `"chrome"` tracks a recent stable; pinning would make us re-pick
  every few months. Use the default.

## Out of scope

- Migrating non-FB downloads (article scrape, sheets metadata) to
  curl_cffi. Same scoping reason as in the 2026-06-11 plan.
- Adding an operator-visible "retry image download" knob in Settings —
  if FB tightens further we revisit; today's defaults are fine.
- Decoding the `d=` blob as a defense-in-depth fallback. Worth doing
  later if FB blocks `curl-impersonate` chrome fingerprints too, but
  premature now.

## Cost (rule 8)

`curl_cffi` is open source (MIT). No API fee, no subscription. Wheel
size is ~10-15 MB compressed, contributes roughly **20 MB** to the
final Docker image. No paid service touched by the change.

## Security (rule 13)

- `curl_cffi.AsyncSession()` defaults to `verify=True` — TLS cert
  verification stays on. We do not change this.
- We do not log the full URL on retry or on exhaustion (same as
  today). FB redirect tokens (`?d=...`) carry personalization data;
  only the host is logged.
- The Chrome TLS fingerprint we impersonate is a public artifact
  (every Chrome user on the planet sends the same one). No new
  identification or fingerprinting concern from our side.
- No new credentials, secrets, or auth surface. We pass through
  whatever public URL the operator put in the sheet.
- 4xx short-circuit preserved in both backends — a misconfigured URL
  still cannot burn three TLS handshakes against a dead host (mild
  DoS-amplification guard kept).
- Graceful-degradation log on import failure does NOT include the
  exception traceback — we log the exception type + message only,
  so a stack-trace through `_ssl.c` cannot leak path or build info.

## Observability (rule 14)

Existing `[http]` namespace gets two new tags and one keeps the same
shape:

- `image_download_backend` (info, **first call only**) — emitted once
  at module load time with fields `impersonate_available` and (if
  False) `import_error` (truncated). Tells us in prod logs whether
  curl_cffi loaded.
- `image_download_route` (debug) — fields: `host`, `backend`
  (`"impersonate"` or `"httpx"`). Emitted on each call so we can
  audit routing decisions if FB URLs ever start failing again.
- `image_download_retry` / `image_download_recovered` — unchanged.
  Both backends emit through the same shared retry loop.

No log on first-try success (the row processor already logs row-level
lifecycle).

## Testing (rule 18)

`tests/unit/test_http_download.py` is reorganized into two clearly
labeled sections:

1. **Generic / httpx-backend tests** — pivot from `www.facebook.com`
   to a non-impersonate host (`https://cdn.example.com/image.jpg`).
   These keep verifying: UA header, retry on SSL/Connect/5xx,
   4xx short-circuit (403, 404), exhausted-retries error message,
   `max_retries=1` honored. Coverage: same as today.

2. **Impersonate-backend tests** — new. Use a `www.facebook.com` URL,
   mock `curl_cffi.requests.AsyncSession` to control the behavior of
   `.get()`. Cover:
   - Happy path returns bytes (FB host routes through impersonate).
   - Retry on `curl_cffi.requests.exceptions.SSLError` → success on
     attempt 2.
   - Retry on `ConnectTimeout` → success on attempt 2.
   - 4xx response surfaces an `ImageDownloadError` with no retry.
   - Exhausted retries names the host + last error reason.

3. **Routing tests** — new, small. Verify that `_should_impersonate()`
   matches FB/IG hosts and rejects everything else (covers domain
   suffix logic, case insensitivity, IP literals).

4. **Graceful degradation** — new. Simulate
   `_IMPERSONATE_AVAILABLE=False` and confirm an FB URL routes through
   the httpx backend with no exception.

`asyncio.sleep` is patched as today so retry tests do not add real
wall time.

Full unit suite (`pytest tests/unit`) must stay green after the change.

## Settings (rule 15)

No new user-facing settings. The whole change is an internal backend
swap for two specific host families. Defaults are correct ("operator
never has to think about it"). If we later add a per-deploy override
("force httpx for FB"), it would be a `BULKVID_DISABLE_IMPERSONATE`
env var rather than a sheet/UI knob — flagged here for memory.

## Deploy (rule 19)

Branch flow (current project convention from `.github/workflows/` and
recent `main` history):

- Branch off `main` → push to a feature branch → open PR into `main`
  → merge → HF Spaces redeploys from `main` via the `hf` remote.
- Production tracks `main`. We do **NOT** push or force-push to `main`
  directly. We do **NOT** manually promote anywhere — the HF Space
  picks up `main` automatically.
- Rollback path: revert the merge commit on `main` and push the
  revert. HF Space redeploys to the prior good state on the next
  build.

Pre-flight: confirm `curl_cffi` installs cleanly in the Docker build
locally (`docker build .`) before pushing — abi3 wheel covers
`python:3.12-slim`, but a manylinux wheel mismatch would only surface
in CI/HF logs. Cheaper to catch on the laptop.
