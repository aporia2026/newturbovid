# Resilient HTTP image download

**Date**: 2026-06-11
**Owner**: Yoav
**Status**: Approved

## Problem

Operator reports the `simple_x4` batch is mostly failing on Manual-image
rows whose URLs come from Facebook's ad redirect endpoint, e.g.

```
https://www.facebook.com/ads/image/?d=AQICTm38UhL0p6D8...
```

The row error surfaces as `[SSL] unknown error (_ssl.c:1010)` — a TLS
handshake failure during the `manual_image_url` download.

Three things conspire:

1. **No retry on transient errors.** Every row processor
   ([`row_processor_simple_x4.py`](../src/bulkvid/orchestrator/row_processor_simple_x4.py#L92-L96),
   plus avatar / image_vo / cartoon / simple / 4images / text_on_img)
   duplicates a one-shot `_download` helper. A single SSL handshake blip
   from FB's CDN kills the row outright.
2. **Default httpx User-Agent.** Facebook's ad CDN frequently refuses,
   resets, or 403s requests carrying the default `python-httpx/<ver>`
   UA. The handshake never completes cleanly.
3. **Duplicated helper.** Six near-identical `_download` definitions —
   any fix has to touch all of them, and the divergence risk grows
   every time a row processor is edited.

## Goals

- Stop transient SSL/network errors from killing rows.
- When download truly fails, surface a clear error to the operator
  (host + last reason), not a bare `_ssl.c:1010`.
- Don't slow down the happy path on healthy URLs.
- Keep the surgery surgical — same call signatures so the row
  processors barely change.

## Approach

One new module: [`src/bulkvid/http_download.py`](../src/bulkvid/http_download.py).
Public surface:

```python
async def download_image(
    url: str,
    *,
    timeout: float = 60.0,
    max_retries: int = 3,
    user_agent: str = DEFAULT_USER_AGENT,
) -> bytes: ...

class ImageDownloadError(RuntimeError): ...
```

Behavior:

- **Browser User-Agent** — current Chrome stable string. FB/IG/Pinterest
  CDNs respond normally to that and refuse defaults under load.
- **Exponential backoff** — `0.5s → 1.0s → 2.0s` between attempts (3
  attempts total by default).
- **Retried errors** — `httpx.ConnectError`, `ConnectTimeout`, `ReadError`,
  `ReadTimeout`, `RemoteProtocolError`, `WriteError`, `ssl.SSLError`, AND
  5xx status codes.
- **NOT retried** — 4xx status codes. A 403/404 is permanent; burning
  three attempts wastes batch latency. Raise immediately with status.
- **Clear error** — `ImageDownloadError` always carries the host and the
  last error message. Row-failure metadata stays readable.
- **Recovery log** — when retry succeeds, emit `image_download_recovered`
  so we can see which CDNs flake in production.

Replace the six duplicated `_download` definitions with a direct import
of `download_image`. Same call shape, drop-in.

### Alternatives rejected

- **Decode the `d=` blob to call fbcdn.net directly.** Reverse-engineering
  Facebook's redirect format. Brittle — they change it and every row
  breaks. The retry + UA approach gets ~90% of recovery at zero
  coupling to FB internals.
- **Enable HTTP/2 in httpx.** Adds dependency (`h2`), marginal gain on
  a 1-shot image fetch. Revisit only if we move to long-lived pooled
  clients.
- **Force a specific TLS version.** Fixes one symptom, breaks others
  (server-mandated cipher suites, ALPN). Retry handles transient
  handshake failure without locking us in.
- **Per-row processor fix.** Touched six places, same bug six times;
  guarantees future drift.

## Out of scope

- Retry/UA tuning for non-image downloads (article scrape, sheets
  metadata). The image download path is the one currently bleeding.
- `_is_valid_http_url` consolidation (lives in 3 row processors). Cosmetic.

## Security (rule 13)

- The Chrome UA we send is a public string; no fingerprinting concern.
- The helper does NOT log the full URL on retry — only the host (so an
  encoded `?d=` blob with personalization tokens doesn't land in logs).
  Same for the exhausted-retries error message.
- No new auth path; no secrets touched.
- 4xx is short-circuited: a bad URL can't burn three attempts of TLS
  setup against a misconfigured host (small DoS-amplification guard).

## Observability (rule 14)

Logs under existing `[http]` namespace:

- `image_download_retry` (warning) — fields: `host`, `attempt`,
  `max_attempts`, `error` (truncated, type-tagged).
- `image_download_recovered` (info) — fields: `host`, `attempt`.
  Emitted only when retry succeeded (i.e. happy path stays silent).

No log on first-try success (the row processor already logs
row-level lifecycle).

## Testing (rule 18)

`tests/unit/test_http_download.py` covers:

1. Happy path — returns bytes, **browser UA header sent**.
2. Retry on `httpx.ConnectError` → success on attempt 2 (with no real sleep).
3. Retry on `ssl.SSLError` → success on attempt 2.
4. Retry on 5xx → success on attempt 2.
5. Hard 4xx (403, 404) — single attempt, raises `ImageDownloadError`
   with HTTP status in the message.
6. Exhausted retries — raises `ImageDownloadError`, error message
   names the host and the last failure reason.
7. Custom `max_retries=1` honored (single attempt, no retry).

Sleep is patched (`asyncio.sleep` → `AsyncMock`) so retry tests don't
add real wall time.

Full unit suite (`pytest tests/unit`) must stay green after the row
processor migration.

## Settings (rule 15)

No user-facing settings. `max_retries` / `timeout` are call-site args
(row processors already pass per-call timeouts), and the default of 3
attempts is the right "operator never has to think about it" tradeoff.
A future per-tab download-retry knob can be added if FB tightens further
— flagged here so we remember the option.
