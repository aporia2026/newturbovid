# Stop the SettingsStore + JobQueue worker connection from wedging on a dead Hrana stream

Date: 2026-06-24
Status: approved (Yoav: dual fix — SettingsStore reconnect + JobQueue worker-side reconnect)
Owner: Yoav
Investigation: 2026-06-24 chat — Yoav sent two live screenshots from job-61d63442da7e6b25 ("paste text on img", 691 rows). First showed 125/125 row failures with `unhandled: Hrana: api error: status=404 Not Found, body={"error":"stream not found: 14edb5a7:1454bfd"}` — the SAME stream id repeated across every row. Second (from Omer Nuriel) showed rows 286–298 stuck "Starting.." for 10–26 minutes, well past the 12-minute TextOnImg timeout — and the in-product "Kill" button can't help because kill goes through the same wedged connection.

## Problem (evidence, not guess)

We hold ONE libsql connection per store, opened in `__init__` and never refreshed. Turso's server evicts idle/long-lived Hrana streams; once the stream id the Python client holds is dead, every subsequent call against that connection 404s with `stream not found: <id>` — forever, until the process restarts.

Two stores fall through this hole right now:

1. **SettingsStore** (`src/bulkvid/orchestrator/settings_store.py`). Read on every row via `BatchRunner._row_timeout_seconds(tab)` → `store.get(...)` → `_ensure_cache` → `_load_sync_with_retry`. The existing retry sleeps 0.5 s and retries the **same** dead connection. Result: every row fails fast with the "unhandled: Hrana: stream not found" message (screenshot 1, 125/125 rows).
2. **JobQueue worker-side** (`src/bulkvid/orchestrator/queue.py`). `claim_next_row`, `record_result`, `kill_job`, `kill_all_jobs`, `recover_orphaned_rows` use bare `asyncio.to_thread(...)` against the same single connection. When the worker connection's stream dies between provider work, `record_result` fails forever; the in-process retry buffer (`_pending_records`) keeps retrying against the same dead connection until the 5-minute per-entry budget expires; rows stay PROCESSING in the DB; sidebar shows "Starting.." indefinitely (screenshot 2). The kill path goes through the same connection, so the operator can't even abort.

The JobQueue **web** path (`_run_db`) was already hardened against this on 2026-06-17 (`_plans/2026-06-17-submit-500s-turso-resilience.md`) — that's why `submit_job` survives. Worker-side methods never got the same treatment.

## Approach (chosen)

Mirror the existing `_run_db` discard-and-reconnect pattern across both holes:

1. **SettingsStore**: add `_open_connection` + `_reconnect_sync(reason)`, plus a `_run_sync_with_reconnect_retry(fn, *, op, attempts=2)` helper. Wire it into `_load_sync_with_retry`, `set()` (around `_set_sync`), and `audit()` (around `_list_audit_sync`). Default to 2 attempts (one fresh try after one reconnect) — a single Hrana stream death heals after one swap; persistent Turso outages should still propagate so the row processor reports a real failure.
2. **JobQueue worker-side**: route the async wrappers for `claim_next_row`, `record_result`, `kill_job`, `kill_all_jobs`, and `recover_orphaned_rows` through the existing `_run_db` (which already does timeout + lock + discard-and-reconnect + 3 attempts). This drops their `async with self._lock` block — `_run_db` already holds the lock per attempt. Net diff per method is one-line.

### Alternatives considered, rejected

- **Centralize reconnect inside `_db.connect`'s wrapper.** Wrap libsql at the driver level so every `execute()` auto-reconnects on Hrana stream errors. Structurally correct, but: (a) bigger refactor that touches every consumer with one risk surface, (b) the libsql Python client's error taxonomy is undocumented — guessing which exceptions mean "stream evicted, retry me" vs "real failure, propagate" risks silently masking real bugs. Park it as a follow-up after this stops the bleeding.
- **Just bump the SettingsStore retry sleep from 0.5 s to 5 s.** Doesn't fix anything — the client clings to the dead stream id; sleeping more doesn't rotate it. Already half-fix territory; rejected.

## Security (rule 13)

No new attack surface. Reconnect uses the same auth_token + URL that the original connection used. No new logged data — error strings are truncated to 200 chars (same as the existing `_run_db` warnings).

## Observability (rule 14)

- `db_reconnect` warning log on every SettingsStore + JobQueue reconnect, with `reason=` carrying the original exception class name. JobQueue already emits this (queue.py:356); SettingsStore gets the same shape.
- `settings_store_db_call_retry` warning log per attempt (mirror of `db_call_retry` from `_run_db`).
- `settings_store_reconnect_failed` warning log when the reconnect call itself raises (Turso fully down).

## Testing (rule 18)

- Regression test: SettingsStore with a `_load_sync` that always raises a Hrana-shaped error → assert the retry attempts a reconnect AND fails. Then a variant where `_load_sync` flips behavior after `_reconnect_sync` swaps the connection → assert load succeeds on attempt 2.
- Regression test: same shape for `_set_sync` and `_list_audit_sync`.
- Regression test: JobQueue worker-side `record_result` against a connection that always raises after a single failed write; assert reconnect fires and the second attempt lands.
- Run the full test suite to confirm the existing `_run_db` web-path tests + `test_settings_store` tests still pass.

## Settings (rule 15)

No new admin-editable settings. The retry counts + backoff are already env-overridable for JobQueue (`BULKVID_DB_MAX_ATTEMPTS`, `BULKVID_DB_CALL_TIMEOUT_SECONDS`); SettingsStore's 2-attempt cycle is a hardcoded default. If we ever need a knob there, expose `BULKVID_SETTINGS_DB_MAX_ATTEMPTS` then — premature now.

## Followups

- Centralize reconnect inside `_db.connect`'s wrapper once we've watched this fix in prod for a week and learned the libsql error taxonomy from the new logs. Tracked in this plan only; no separate ticket yet.
- The drainer's per-entry budget (5 minutes) was set assuming reconnect *would* heal a transient outage. With this fix in place, a 5-minute budget will surface a true outage faster — review after the first real-world Turso flap with this fix in place.
