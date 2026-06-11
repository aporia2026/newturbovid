# TikTok avatar poll — treat code=40100 (rate-limit) as transient

**Date**: 2026-06-11
**Owner**: Yoav
**Status**: Approved

## Problem

Operator reports row 3 failing with:

```
TikTok avatar failed: TikTok get returned code=40100 on attempt 12:
Too many requests. Please retry in some time.
```

The poll endpoint (`wait_for_result` in
[`src/bulkvid/adapters/tiktok_avatar.py:336-342`](../src/bulkvid/adapters/tiktok_avatar.py#L336-L342))
treats every non-zero `code` as a fatal error and raises. Code `40100`
("Too many requests") is **transient** — it's a per-account rate-limit
that clears in seconds — but we kill the row anyway.

The list endpoint already does this right
([tiktok_avatar.py:498-507](../src/bulkvid/adapters/tiktok_avatar.py#L498-L507)):
it has a `_LIST_TRANSIENT_API_CODES = {51010}` set and a backoff retry
when a transient code lands. The poll endpoint needs the same pattern.

## Goal

Stop transient TikTok API codes — specifically `40100` — from killing
avatar rows. If TikTok rate-limits the poll, log it, keep polling, and
let the row complete when the limit clears.

## Approach

Add `_POLL_TRANSIENT_API_CODES = frozenset({40100, 51010})` and a
short-circuit branch in `wait_for_result`:

```python
code = body.get("code")
if code in _POLL_TRANSIENT_API_CODES:
    _log.warning(
        "tiktok_avatar_poll_transient_retry",
        code=code,
        message=...,
        attempt=attempt,
        task_id=task_id,
    )
    continue    # next iteration sleeps poll_interval, then re-polls
if code != 0:
    raise TikTokAvatarError(...)    # unchanged: terminal codes still fatal
```

Each retry already costs one `poll_interval` (5s default) of wait via
the existing top-of-loop sleep — that's enough spacing for TikTok's
per-minute window to roll over. With the default `poll_max=120`
× 5s = 10 min total ceiling unchanged, so a sustained outage still
gets caught by the timeout path.

Codes included in the transient set:
- **40100** — "Too many requests" (the user's reported failure).
- **51010** — "internal service timed out" (same code the list
  endpoint already treats as transient; harmless to include for poll
  symmetry).

Terminal API codes (auth, malformed request, unknown avatar_id, etc.)
still raise immediately so the operator hears about them.

### Alternatives rejected

- **Exponential backoff specifically on 40100.** TikTok's rate-limit
  window is per-minute, not per-request — backing off harder than the
  5s poll_interval wastes time without helping. Operator can bump
  `TIKTOK_POLL_INTERVAL` env var if they really want longer spacing.
- **Per-account global cooldown across all rows.** Would require shared
  state in the client. Defer until we see multiple rows tripping the
  same window simultaneously — for now, individual rows recovering is
  enough.
- **Retry the CREATE endpoint too.** The user's failure is on poll;
  CREATE rate-limit is plausible but unreported. Adding both grows
  scope. Flag as a follow-up if seen in production.

## Out of scope

- TikTok CREATE endpoint transient-code handling (flagged above).
- Gemini TTS quota (separate ticket — Vertex AI per-minute quota; the
  existing TTS semaphore + retry already partially handles it).
- Other TikTok error codes beyond 40100 / 51010. Add as we observe them.

## Security (rule 13)

- No new auth surface. Adapter-internal change.
- Logged fields (`code`, `message`, `attempt`, `task_id`) are TikTok's
  operator-safe metadata; no token / advertiser_id leaks.
- The `task_id` was already logged in the existing
  `tiktok_avatar_poll_pending` line, so no new exposure.

## Observability (rule 14)

New log line under existing `[tiktok_avatar]` namespace:

- `tiktok_avatar_poll_transient_retry` (warning) — fields: `code`,
  `message` (TikTok-supplied), `attempt`, `task_id`. Mirrors the
  list-endpoint `tiktok_avatar_list_transient_retry` line so operator
  can grep for either consistently.

When the row eventually completes, the existing `tiktok_avatar_ok`
line still fires with `attempts=<final>` — so we can see "X retries
before completion" in the audit log.

## Testing (rule 18)

`tests/unit/test_tiktok_avatar.py`:

1. **Add** `test_wait_for_result_retries_on_rate_limit_code_then_succeeds`
   — first poll returns `{"code": 40100, ...}`, second returns SUCCESS.
   Asserts: `preview_url` returned, two GET calls made.
2. **Add** `test_wait_for_result_retries_on_internal_timeout_51010_then_succeeds`
   — same shape with `code=51010`, for symmetry with the list endpoint.
3. **Add** `test_wait_for_result_terminal_code_still_raises` — e.g.
   `code=40000` (bad request) still raises, confirming the transient
   set didn't accidentally swallow real errors.

Full unit suite (`pytest tests/unit/`) stays green.

## Settings (rule 15)

No new user-facing setting. The existing `TIKTOK_POLL_INTERVAL` and
`TIKTOK_POLL_MAX` env vars already let the operator tune cadence /
ceiling — both apply to transient retries automatically.
