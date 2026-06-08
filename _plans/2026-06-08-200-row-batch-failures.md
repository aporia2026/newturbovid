# 200-row batch failures: Rendi platform overload + Gemini TTS quota bursts

Date: 2026-06-08
Status: approved (post-council, revised) — Phase 1 cleared for implementation 2026-06-08
Owner: Yoav
Source: Evgeny ran ~277 rows on the `simple` tab on 2026-06-07 morning (UTC ≤ 07:00); 127 of 277 (46%) failed. Diagnosed live from Turso `row_queue` on 2026-06-08.
Builds on: `_plans/2026-06-07-overload-handling-and-template-defaults.md` (Part A retry layer shipped at 14:39 UTC on 2026-06-07 — but did NOT cover Rendi and capped Gemini TTS `Retry-After` at 30s)

## Council revisions (2026-06-08, post llm-council pass)

The original four-point fix went through the council. Verdict: substance is right, three real catches:

1. **Log-before-classify is a hard dependency.** You cannot write a classifier (Part 2) for an empty-error response shape until at least one is captured in prod logs (Part 1). The council called this out as the strongest single critique. Plan resequenced: Phase 1 = logging + semaphores + Turso `failed_rows` view. Phase 2 = classifier, after Phase 1 captures a real FAILED in the wild.
2. **Idempotency of re-runs is a precondition, not future hygiene.** The plan implied "re-run the 127 failed rows" as a verification step but didn't check whether partial-success rows would double-charge providers (Kie/ZapCap/Gemini) or duplicate Sheet writes. Plan now requires a Turso query check BEFORE any re-run.
3. **The operational gap ("retry failed rows" button) is the real user-facing bug.** Without it, every 5% failure becomes a manual re-export rodeo. Out of scope for THIS plan, but tracked as the next plan (`_plans/2026-06-08-retry-failed-rows.md` — to write after Phase 1 ships).

Council-rejected expansions kept out of scope: multi-provider abstraction layer (right destination, wrong moment); lowering global `BULKVID_MAX_CONCURRENT_ROWS=10→4` (blunt where the per-provider caps are precise); pre-emptive wrap of OpenAI/Kie/ZapCap (no data justifies it yet). All can be added later if data appears.

Council-adopted additions:
- Semaphore wait-time logging (emit `[rendi submit] semaphore_wait queued_for_s=…` only when wait > 1s) so we can SEE whether the cap is biting. Numbers (6, 4) explicitly labelled v1 guesses to be retuned from real data.
- Turso `failed_rows` view (just a saved SQL query) so we never hand-diagnose batches again.

## Context

Diagnostic queries against Turso (results captured in chat 2026-06-08) showed:

| Failure status | Count | Earliest | Latest |
|---|---|---|---|
| `VIDEO_ASSEMBLY_FAILED` | 71 | 2026-06-07 05:25 UTC | 2026-06-07 06:59 UTC |
| `TTS_FAILED` | 54 | 2026-06-07 05:21 UTC | 2026-06-07 06:43 UTC |
| `ARTICLE_FETCH_FAILED` | 2 | 2026-06-07 05:32 UTC | 2026-06-07 06:46 UTC |

Two distinct jobs from `evgeny@aporianetworks.com`:
- `job-1780809689` — `simple` tab, 200 rows, 124 succeeded, **76 failed** (38%)
- `job-1780814540` — `simple` tab, 77 rows, 26 succeeded, **51 failed** (66%)

### Failure mode A — TTS_FAILED (54 rows)

Sample error: `429 RESOURCE_EXHAUSTED. {'error'...}`. Gemini TTS per-minute quota exhaustion under burst load.

**Status as of today:** Partially fixed by yesterday's 14:39 UTC deploy. `GeminiTTSClient.synthesize` is now wrapped in `with_retry` (3 attempts, exp backoff with jitter, honors `Retry-After`). But the wrapper's `max_seconds=30` ceiling clamps `Retry-After` to 30s — while Gemini's quota window is per-minute. On a 200-row burst this can still fail.

### Failure mode B — VIDEO_ASSEMBLY_FAILED (71 rows)

Sample errors (8 of 8 in an 11-minute window all identical shape):

```
Rendi command 978f88d2-16c7-414b-a35d-20fd03248c8c failed: {} | stderr:
Rendi command 0853d166-51d5-4c13-b5c5-a43ba02d0cf7 failed: {} | stderr:
Rendi command 04315545-7728-410e-a19f-de6e6c3cf19e failed: {} | stderr:
… (8 of 8 same shape)
```

That `{}` is `str({})` — Rendi returned `status=FAILED` with an **empty `error` dict and empty `ffmpeg_stderr`**. ffmpeg never ran. This is Rendi platform capacity (account-wide vCPU pool exhausted), not an ffmpeg syntax error.

**Status as of today:** NOT fixed. Our `RendiCommandFailedError` is explicitly excluded from `_submit_and_poll` retry on the (correct) reasoning that ffmpeg failures are deterministic — but that reasoning **does not apply** when ffmpeg didn't run. The retry exclusion is exactly wrong for the empty-error case.

### Failure mode C — ARTICLE_FETCH_FAILED (2 rows)

`All article fetch strategies failed`. Two rows total — tail noise, not a systemic issue. Out of scope.

## Goals

- **G1.** A 200-row `simple` batch produces ≤5% failure rate end-to-end on a re-run (down from 46%), absorbing Gemini TTS quota bursts and Rendi platform hiccups automatically.
- **G2.** When Rendi DOES fail in a way we don't recognise, the next debug session takes minutes, not hours — the full Rendi response body is in the log.
- **G3.** No silent throughput regression on small batches (1-20 rows). Per-provider caps must be ≥ today's effective concurrent rate on small batches.
- **G4.** Same code path works on HF Spaces (10-row concurrency) and the local runner (40-row concurrency). Per-provider caps are env-overridable per deploy.

## Constraints

- **No infrastructure changes.** Same FastAPI on HF Spaces + Turso + Sheets. No Redis, no queue rewrite, no new tier on Rendi.
- **No new paid services.** Rule 8. This plan only changes how we use existing services.
- **Backwards compatible.** Existing tests must continue to pass; per-provider caps default to values ≥ current effective rate so small batches see zero behavior change.
- **Single deploy unit.** Backend changes work in both HF Spaces and local-runner modes.
- **Tests required.** Rule 18 — every change ships with unit tests including a bug-fix-as-test for the Rendi empty-error retry.

## Requirements

In scope:
- Rendi: capture full FAILED response body in logs.
- Rendi: new `RendiTransientFailureError` for empty-error/empty-stderr FAILED responses; add it to the existing `_submit_and_poll` retry classification.
- Rendi: per-provider concurrency cap via `asyncio.Semaphore` inside `RendiClient`.
- Gemini TTS: per-provider concurrency cap inside `GeminiTTSClient`.
- Gemini TTS: lift the `with_retry` `max_seconds` cap for Gemini specifically (per-minute quota windows want a 60-65s honor window, not 30s).
- Unit tests for each change, including a regression test for Rendi empty-error retry.
- Admin-settable knobs for both per-provider caps + env overrides.

Out of scope (this plan):
- Per-provider caps on OpenAI, Kie, ZapCap, AtlasCloud. Data does not yet justify it for the simple-tab failure mode. We add when data shows the need.
- Token-bucket rate limiter or persistent backoff state across worker restarts. Plan §A3 from 2026-06-07 — bigger lift, not justified by today's data.
- Per-user / per-batch dollar caps. Separate scope.
- Changing the runner-level `BULKVID_MAX_CONCURRENT_ROWS` default of 10. Per-provider caps are the right knob; the runner's 10 stays.

## Verified current state

| Verified fact | Evidence |
|---|---|
| `PipelineClients` is built ONCE at worker startup | `worker.py:58-77`, single `build_client_from_settings` call per provider |
| Both `RendiClient` and `GeminiTTSClient` are singletons across all rows | Same — passed via dataclass to every row processor |
| Gemini TTS retry helper is wired correctly | `gemini_tts.py:332-341` `with_retry(...)` around the actual SDK call |
| OpenAI retry helper is wired correctly | `openai_client.py:207-217` |
| Rendi `_submit_and_poll` retries `RendiTimeoutError` + `RendiError` base, NOT `RendiCommandFailedError` | `rendi.py:552-563` |
| Rendi `poll` collapses empty error+stderr into `RendiCommandFailedError` with no marker | `rendi.py:494-510` — bug source |
| `with_retry` `max_seconds` default is 30.0 | `_retry.py:42` |
| Worker uses `BULKVID_MAX_CONCURRENT_ROWS=10` on HF Spaces | `.env.example:129`, README §"Both run the **same code**" |

So: an `asyncio.Semaphore` placed inside `RendiClient.__init__` and `GeminiTTSClient.__init__` will be a TRUE cross-row cap.

---

## Alternatives

**Option 1 — Surgical four-point fix (recommended).**

Four small changes:
1. Rendi diagnostic logging: capture full FAILED body
2. Rendi new transient-failure exception + add to retry classification
3. Rendi per-provider `asyncio.Semaphore` (default 6)
4. Gemini TTS per-provider `asyncio.Semaphore` (default 4) + lift `max_seconds` to 65 for Gemini calls

Total: ~120 LOC change + tests. Each piece independent, independently revertable, each justified by the actual data.

**Option 2 — Per-provider semaphores for every adapter (OpenAI, Kie, ZapCap, Rendi, Gemini).**

Same as Option 1 but adds caps for OpenAI/Kie/ZapCap too. Wider safety net. Trade-off: speculative — we have zero data showing OpenAI/Kie/ZapCap caused failures in the morning runs, so caps would be guesses. Per rule 1 (verify, don't guess) and the principle of not adding things "beyond what the task requires", reject for now. Add per-provider if/when their data appears.

**Option 3 — Single global "burst dampener" instead of per-provider caps.**

Lower `BULKVID_MAX_CONCURRENT_ROWS` from 10 to 4. One knob, one change. Trade-off: kills throughput uniformly even when the bottleneck is one specific provider — and the failure modes here ARE one specific provider (Rendi) and one specific quota (Gemini). Hammering all rows for a Rendi-shaped problem is wasteful. Reject.

**Option 4 — Switch off Rendi.**

Bundle ffmpeg in the HF Space container and run it locally. Eliminates Rendi entirely. Trade-off: HF Spaces have 2 vCPU on the free tier and 16 vCPU on the paid tier; an ffmpeg per row would saturate the box fast and contend with the FastAPI worker. Plan §6 of the original `_plans/2026-06-02-aporia-bulk-video-tool.md` explicitly rejected this for a reason. Reject.

### Chosen: Option 1

Smallest surface area, directly justified by today's data, fully reversible (each of the 4 pieces independently), no speculative work. Lays the per-provider-semaphore pattern down once; future adapters that show data justification can copy it in a single PR.

---

## Design

### Part 1 — Rendi diagnostic logging (5 LOC + test)

In `rendi.py:494-510` (`poll` method, `status == "FAILED"` branch), add the full response body to the log:

```python
_log.error(
    "rendi_poll_failed",
    command_id=command_id,
    full_body=body,                  # NEW — captures Rendi's complete response
    error_message=msg,
    ffmpeg_stderr=stderr[:500],
)
```

This is pure observability — no behavior change. Next time a FAILED response shape we haven't seen lands, grep the log.

### Part 2 — Rendi transient-failure exception + retry (25 LOC + tests)

New exception class in `rendi.py`, alongside the existing ones:

```python
class RendiTransientFailureError(RendiError):
    """Rendi returned status=FAILED with an empty error dict and empty stderr —
    the platform marked the command failed without ffmpeg running.

    Treated as TRANSIENT (retryable) because the empty diagnostic signals a
    platform-side issue (account vCPU pool exhausted, internal scheduler kill),
    NOT a deterministic ffmpeg failure. Distinct from RendiCommandFailedError
    (which carries stderr and IS terminal).
    """
```

In `poll` (rendi.py:494-510), classify the empty-error case BEFORE raising `RendiCommandFailedError`:

```python
if status == "FAILED":
    err = body.get("error") or {}
    if isinstance(err, dict):
        msg = err.get("message") or (str(err) if err else "")
        stderr = err.get("stderr") or body.get("ffmpeg_stderr") or ""
    else:
        msg = str(err)
        stderr = body.get("ffmpeg_stderr") or ""

    _log.error(
        "rendi_poll_failed", command_id=command_id, full_body=body,
        error_message=msg, ffmpeg_stderr=stderr[:500],
    )

    # NEW: empty diagnostic → platform issue → retryable
    if not msg and not stderr:
        raise RendiTransientFailureError(
            f"Rendi command {command_id} FAILED with no diagnostic — likely platform"
        )

    raise RendiCommandFailedError(
        f"Rendi command {command_id} failed: {msg} | stderr: {stderr[:500]}"
    )
```

In `_submit_and_poll` (rendi.py:552-563), add the new exception to the retryable set:

```python
except (RendiAuthError, RendiCommandFailedError):
    raise
except (RendiTimeoutError, RendiTransientFailureError, RendiError) as e:
    # ... existing retry logic unchanged
```

Existing `RENDI_RETRIES = 2` (so 3 attempts total) is the right cap — Rendi platform issues usually clear within seconds, and capping at 3 attempts means a real persistent platform outage still surfaces within ~15s instead of hanging forever.

### Part 3 — Rendi per-provider semaphore (15 LOC + test)

In `RendiClient.__init__` (rendi.py:385-403), add an asyncio.Semaphore:

```python
def __init__(
    self,
    api_key: str,
    base_url: str = "https://api.rendi.dev",
    connect_timeout: float = 10.0,
    read_timeout: float = 60.0,
    default_vcpu: int = 4,
    default_max_run_seconds: int = 300,
    max_concurrent: int = 6,                      # NEW
    client: httpx.AsyncClient | None = None,
) -> None:
    ...
    self._concurrency = asyncio.Semaphore(max_concurrent)
```

Wrap the entire `_submit_and_poll` cycle so the slot is held for submit+poll+retry, not just submit:

```python
async def _submit_and_poll(self, ...) -> tuple[str, str]:
    async with self._concurrency:
        # existing logic
```

This caps in-flight Rendi commands at 6 across the whole worker, regardless of `BULKVID_MAX_CONCURRENT_ROWS`. The 7th row's Rendi step blocks until one completes; the row stays alive (no timeout).

In `build_client_from_settings` (rendi.py:774-783):

```python
return RendiClient(
    api_key=s.RENDI_API_KEY,
    ...
    max_concurrent=s.BULKVID_RENDI_MAX_CONCURRENT,
)
```

In `config.py`:

```python
BULKVID_RENDI_MAX_CONCURRENT: int = 6
```

Default 6 is intentional:
- HF Spaces has `BULKVID_MAX_CONCURRENT_ROWS=10` and each simple-tab row uses 1 Rendi call → today's peak burst is 10 → 6 is a meaningful reduction without strangling throughput.
- 6 concurrent × 4 vCPU/command = 24 vCPU asked of Rendi at peak. Rendi Pro tier's account-wide ceiling is not publicly documented; 24 fits comfortably inside typical Pro allocations.
- Configurable per-deploy via env var, per-deploy via admin (see §Settings audit).

### Part 4 — Gemini TTS semaphore + lifted retry ceiling (15 LOC + test)

In `GeminiTTSClient.__init__` (gemini_tts.py:233-247), add semaphore:

```python
def __init__(
    self,
    project: str,
    location: str = "us-central1",
    model: str = DEFAULT_MODEL,
    credentials_info: dict[str, Any] | None = None,
    max_concurrent: int = 4,                  # NEW
    client: Any | None = None,
) -> None:
    ...
    self._concurrency = asyncio.Semaphore(max_concurrent)
```

In `synthesize` (gemini_tts.py:272-341), wrap the `with_retry` call with the semaphore AND lift `max_seconds`:

```python
async with self._concurrency:
    response = await with_retry(
        _call,
        op="gemini tts",
        retryable=(
            GeminiTTSRateLimitError,
            GeminiTTSServerError,
            GeminiTTSTimeoutError,
            GeminiTTSConnectionError,
        ),
        max_seconds=65.0,    # NEW — Gemini quota window is per-minute
    )
```

The 65s cap is intentional: Gemini's `Retry-After` for a per-minute quota commonly says 30-60s; we want to honor it fully. The semaphore is the first line of defense (prevents the quota trip in the first place); the lifted retry cap is the safety net for when the quota trips anyway (e.g. when other Vertex services on the same project are consuming quota).

In `build_client_from_settings` (gemini_tts.py:401-409):

```python
return GeminiTTSClient(
    project=s.VERTEX_AI_PROJECT_ID,
    location=s.VERTEX_AI_LOCATION,
    credentials_info=build_vertex_credentials_info(s),
    max_concurrent=s.BULKVID_GEMINI_TTS_MAX_CONCURRENT,
)
```

In `config.py`:

```python
BULKVID_GEMINI_TTS_MAX_CONCURRENT: int = 4
```

Default 4 chosen because Gemini's TTS preview models have tighter per-minute quotas than text models, and 4 concurrent calls × ~3-5s synth each = ~50-80 calls/min peak — well under a typical 200-RPM Vertex quota with headroom for the retry layer.

---

## Security (CLAUDE.md rule 13)

Narrow threat surface — this plan touches concurrency and retry behavior on existing trusted services. No new auth surface, no new external data ingress.

- **Retry amplification on Rendi.** With `RENDI_RETRIES=2` (3 attempts total) and per-attempt poll of up to ~10min, a worst case is ~30 min per row before we give up. Bounded. Combined with the per-row wall-clock timeout from yesterday's plan (12-20min depending on tab), the row will time out from above first. Safe.
- **Retry storms.** The new semaphores cap concurrent calls; the existing `with_retry` uses full jitter on backoff. The combination DECREASES burst probability vs today.
- **Empty-error retry classification.** A real ffmpeg failure that ALSO returns empty stderr would now be retried (2 extra attempts). Wasteful but not unsafe — the retries hit the same deterministic failure and surface. Acceptable.
- **Semaphore deadlock.** Each client method acquires its semaphore exactly once and releases on return/exception via `async with`. No nested acquisition. Safe.

## Observability (CLAUDE.md rule 14)

New / changed log lines (existing `[ns step]` shape):

- `[rendi poll] failed full_body={...} error_message={…} ffmpeg_stderr={…}` (extended — full body added)
- `[rendi poll] transient_platform_fail command_id={…}` (new — explicit signal that this branch fired vs a real ffmpeg failure)
- `[rendi submit] semaphore_wait queued_for_s={…}` (new — emitted ONLY when a caller waited >1s for a slot, so we can see "X rows blocked at the Rendi cap" without log spam on healthy runs)
- `[gemini tts] semaphore_wait queued_for_s={…}` (same shape)

Existing `[retry] retry attempt=N of=MAX wait_s=… reason=… retry_after=…` from `with_retry` will now fire for `RendiTransientFailureError` events too — useful, no change needed.

## Settings audit (CLAUDE.md rule 15)

New admin-visible knobs in the existing settings panel (registry-driven, auto-appear):

| Setting | Group | Default | Notes |
|---|---|---|---|
| `rendi_max_concurrent` | Runner | `6` | Cap on in-flight Rendi commands across all rows |
| `gemini_tts_max_concurrent` | Runner | `4` | Cap on in-flight Gemini TTS calls across all rows |

Env overrides via `BULKVID_RENDI_MAX_CONCURRENT` / `BULKVID_GEMINI_TTS_MAX_CONCURRENT` so per-deploy tuning is possible without admin access.

NOT exposed (justified):
- `max_seconds` retry ceiling — implementation detail; if we tune it, it's a PR.
- `RENDI_RETRIES` — same.
- The transient/terminal classification rules — same.

## Testing (CLAUDE.md rule 18)

New tests:

**`tests/adapters/test_rendi.py` — extend:**
- `test_poll_empty_error_raises_transient` — body `{"status": "FAILED", "error": {}}` → `RendiTransientFailureError`. Bug-fix-as-test (rule 18): fails on pre-change code, passes on post-change.
- `test_poll_nonempty_error_still_terminal` — body with non-empty `error.message` → still `RendiCommandFailedError` (no regression).
- `test_submit_and_poll_retries_transient_failure` — first attempt raises `RendiTransientFailureError`, second succeeds. Asserts exactly 2 attempts.
- `test_submit_and_poll_does_not_retry_command_failed` — `RendiCommandFailedError` exhausts on first attempt (no regression on existing behavior).
- `test_semaphore_caps_concurrent_submits` — instantiate `RendiClient(max_concurrent=2)`, kick off 5 parallel `_submit_and_poll` calls, assert only 2 are in-flight at any moment via a probe.
- `test_full_body_logged_on_failure` — assert the log call kwargs include `full_body=body`.

**`tests/adapters/test_gemini_tts.py` — extend:**
- `test_semaphore_caps_concurrent_synth` — same shape as Rendi semaphore test.
- `test_retry_max_seconds_lifted_to_65` — assert the `with_retry` call passes `max_seconds=65.0`.

**`tests/orchestrator/test_clients_singleton.py` — new:**
- `test_rendi_semaphore_shared_across_rows` — build one PipelineClients, simulate two row processors calling `clients.rendi._submit_and_poll` in parallel, assert they share the same semaphore (not per-call instances).
- `test_gemini_tts_semaphore_shared_across_rows` — same.

**Full suite:** `pytest` must pass green over the whole project, not just the new tests. Bar from rule 18.

## Cost (CLAUDE.md rule 8)

Zero new spend. This plan:
- Adds retries on Rendi empty-error events. Each retry is one extra Rendi command, costing $0.01 each (per `COST_RENDI_COMMAND_USD`). With current data showing ~25% of rows hitting Rendi platform failures, retrying 2 extra times for that subset costs ~$0.005/row extra at the failure rate observed, ~$1.50 for a 300-row batch worst case. Negligible.
- Lowers concurrent Gemini TTS calls and lowers concurrent Rendi calls. May INCREASE elapsed time of large batches by ~10-20% (fewer in-flight slots), but DECREASES failed-row spend (today, failed Rendi commands still bill — the platform charges for the command slot regardless of stderr).

Net: likely cost-neutral to cost-positive (fewer wasted commands on failed rows).

No new paid services. No subscription. No infra cost change.

## Open questions (need Yoav's call before implementation)

1. **Default for `BULKVID_RENDI_MAX_CONCURRENT`.** Recommendation: `6`. If you know Rendi's actual account-wide concurrent-command ceiling (Pro tier docs / dashboard), use `min(6, that_ceiling - 2)` to leave room for occasional foreground actions like cleanup_commands. If you don't know, 6 is the safe pick.
2. **Default for `BULKVID_GEMINI_TTS_MAX_CONCURRENT`.** Recommendation: `4`. If you know the Vertex per-minute quota for your project, the math is `quota_rpm / (60 / avg_call_seconds)`. Without that, 4 is the safe pick.
3. **Should the local-runner deploy (`BULKVID_MAX_CONCURRENT_ROWS=40`) get higher per-provider caps?** Probably yes — Rendi=12, Gemini=8 on local. Env overrides give us that for free, but I want explicit confirmation we want different defaults per deploy or whether a single default is fine.
4. **Backfill: should we also wrap Kie / ZapCap / OpenAI in per-provider semaphores while we're here?** Recommendation: NO this round (no data justifies it for these tabs), but the pattern lands here so future adapters are a 15-LOC copy when data shows the need. Reconfirm.
5. **Re-run plan for Evgeny's morning batch.** Recommendation: ship this, ask Evgeny to re-run a 200-row simple batch on a quiet morning hour, query Turso 30min later, confirm <5% failure rate. If we can't get Evgeny to re-run, I can run a synthetic 200-row test through the local runner instead.

## Phased rollout (revised post-council)

The original "ship all four in one PR" was wrong: Part 2 (classifier) literally cannot be written until Part 1 (logging) has captured one real FAILED in prod. Resequenced:

### Phase 1 — ship today (this PR)

Three of the four original parts, plus the council's additions:

- **Part 1 — Rendi full-body logging.** 5 LOC. Zero risk. Captures what Rendi actually says on FAILED.
- **Part 3 — Rendi per-provider `asyncio.Semaphore(6)`** + semaphore-wait logging.
- **Part 4 — Gemini TTS per-provider `asyncio.Semaphore(4)`** + lift `with_retry` `max_seconds` to 65s + semaphore-wait logging.
- **Council addition: Turso `failed_rows` saved view.** Just SQL — paste into Turso console once. Refreshable any time. Stops the hand-diagnostic.
- **Council addition: Idempotency precondition documented** in the §"Operational re-run" section below — must be verified before any re-run of the 127 rows.

Phase 1 tests: every code change shipped with unit tests (per rule 18, bug-fix-as-test where applicable). Full suite must run green.

### Phase 2 — after Phase 1 captures one real FAILED (separate small PR)

- **Part 2 — `RendiTransientFailureError` + retry classification.** Write the classifier against the actual payload shape we logged in Phase 1, not against yesterday's anecdote. Two tests: empty-error → transient (retried); non-empty error → terminal (no retry, regression guard).

### Phase 3 — operational "retry just the failed rows" (separate plan)

Out of scope here. To be written as `_plans/2026-06-08-retry-failed-rows.md`. Adds:
- Apps Script sidebar "Retry failed rows" button.
- Backend `/jobs/{job_id}/retry-failed` endpoint that re-queues only `status='failed'` rows from a given job.
- Idempotency guarantee on a per-row basis (depends on the Operational re-run §below for the data contract).

### Phase 4 — if Phase 1+2 still leave >5% failure rate (separate plan)

- Wrap OpenAI/Kie/ZapCap in semaphores (15 LOC each, no design work) — only if their failures appear in data.
- Token-bucket rate limiter + persistent backoff state from `_plans/2026-06-07-…` §A3 — only if simple semaphores prove insufficient.

Each phase independently shippable and revertible. No data migration in any phase. Full rollback for Phase 1 = revert the commit.

## Operational re-run (council requirement, before any retry of the 127)

The plan validates itself by re-running Evgeny's 127 failed rows against the now-deployed retry helper. Before that re-run can safely fire, confirm partial-success rows don't double-charge:

```sql
SELECT
  job_id,
  row_num,
  json_extract(result, '$.status')                  AS status,
  json_array_length(json_extract(result, '$.video_urls')) AS video_url_count,
  json_extract(result, '$.cost_usd')                AS cost_charged,
  json_extract(result, '$.metadata.zapcap_applied') AS zapcap_applied
FROM row_queue
WHERE job_id IN ('job-1780809689-74e8b1f0','job-1780814540-d3f80583')
  AND status = 'failed'
ORDER BY job_id, row_num
LIMIT 30;
```

Decision tree:
- **`cost_charged = 0` AND `video_url_count = 0` for all failed rows** → re-run is safe (failures happened before any paid call landed). Proceed.
- **`cost_charged > 0` OR `video_url_count > 0` for any failed row** → that row had partial work. Re-running double-bills. Stop, write a per-row idempotency story BEFORE re-running. This becomes a Phase 1.5 plan.

When the safe path holds, the re-run is: click "generate selected" on the 127 rows in the sheet. The existing idempotency-key flow on `/jobs` POST already prevents the SUBMIT from double-creating jobs. The per-row work itself is the concern.
