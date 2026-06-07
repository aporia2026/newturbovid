# Overload handling + smart template defaults

Date: 2026-06-07
Status: draft — pending Yoav approval (and option-picking)
Owner: Yoav
Source: Evgeny ask on 2026-06-07 (Hebrew Slack/Telegram screenshot relayed by Yoav)
Builds on: `_plans/2026-06-02-aporia-bulk-video-tool.md` (master), `_plans/2026-06-04-sensitive-apparel-safeguard-and-per-tab-prompts.md` (per-tab prompt settings already in place)

## Context

Evgeny raised two asks. Translated and disambiguated:

1. **Overload handling.** When too many requests pile up — whether on us (a 1000-row batch dropped into the sheet) or on a paid provider (OpenAI 429s) — the system has to have a real process for absorbing it, not just hope. Today some adapters do this well (kie.ai key pool, Rendi exponential backoff), but the highest-volume path — OpenAI — has no retry at all.

2. **Templates with smart defaults.** The `script_pattern` column lets the operator tell GPT what kind of script to write. When that cell is blank today, we fall back to one hardcoded string (`"natural conversational opener"`). Evgeny wants:
   - a small library of **default templates** stored on the admin side, not a single string;
   - good admin control over that library;
   - when the column is blank, **GPT picks the best template** for the row (vertical, country, article topic) and **adapts the macros** in it.

This plan covers both asks. They're independent — either part can ship on its own.

## Goals

- **G1.** A 1000-row batch dropped onto a busy day does not produce mass row failures. Transient provider issues (429, 5xx, network flake) are absorbed automatically up to a sane retry budget; only persistent failures bubble up.
- **G2.** A stuck row (provider hangs, never responds) is detected and either timed out or made visible in the sidebar within a known SLA, instead of silently holding a runner slot.
- **G3.** When `script_pattern` is blank, the operator gets a script that feels chosen for the row, not the same generic opener every time.
- **G4.** Operators (Aporia editors) can edit the default template library from the admin panel without a redeploy, in the same place per-tab prompts already live.
- **G5.** Observability for both: when a retry happens, when a template is auto-selected, both are logged with the `[ns step]` shape we already use, so debugging takes seconds, not minutes.

## Constraints

- **No infrastructure changes.** Same FastAPI on PythonAnywhere + Turso/libsql + Google Sheets backend. No Redis, no queue rewrite.
- **No new paid services** without an explicit cost sign-off (rule 8). Specifically: the template-selector call is an *additional* OpenAI request per row when the column is blank — flagged below in §Cost.
- **Backwards compatible.** Rows whose `script_pattern` is filled today must produce identical output. The only behavior change is for blank cells.
- **Single deploy unit.** Backend changes must work in HF Spaces deploy mode and the local-runner deploy mode (we have both per `_plans/2026-06-04-local-runner-script.md` and `2026-06-04-migrate-to-hf-spaces-turso.md`).
- **No `--no-verify` shortcuts.** Test gate must pass before either part ships.

## Requirements

In scope:
- Retry + backoff on the LLM and TTS paths.
- Wall-clock timeout per row.
- Stuck-row visibility in heartbeat + sidebar.
- A "Default Script Template Library" admin section: name, body, macros, optional vertical/country hints.
- A GPT-based selector that picks one library template when `script_pattern` is blank, with the existing literal fallback if the selector fails.
- Unit tests for both, with the bug-fix-as-test discipline (rule 18).

Out of scope (this plan):
- A request-level rate limiter on inbound HTTP. Worth doing eventually but not what Evgeny asked for; we'll spec it separately if `/poll` hammering becomes a real problem.
- Per-provider semaphore orchestration (e.g. globally cap concurrent OpenAI calls across rows). Defer; the per-call retry layer is the cheaper first step.
- A per-user/per-batch dollar cap. Useful but a separate scope (financial-controls plan).
- Changing the cartoon planner pipeline's prompt structure. Templates here apply to the **script** prompt only; cartoon's planner is a different surface.

## Current state — gaps to close

(Full audit in chat reply; only the actionable gaps are listed here.)

### Overload side

| Gap | File | Severity |
|---|---|---|
| OpenAI throws 429 immediately, no retry | [openai_client.py:163-172](../src/bulkvid/adapters/openai_client.py#L163-L172) | high |
| Gemini TTS no observable retry | [gemini_tts.py:276-280](../src/bulkvid/adapters/gemini_tts.py#L276-L280) | medium |
| Sheets read/write single-attempt | [sheets.py:227-341](../src/bulkvid/adapters/sheets.py#L227-L341), [sheets.py:386-440](../src/bulkvid/adapters/sheets.py#L386-L440) | medium |
| No per-row wall-clock timeout — stuck row holds the semaphore slot indefinitely | [runner.py:55-197](../src/bulkvid/orchestrator/runner.py#L55-L197) | high |
| Heartbeat only logs idle waits, not stuck in-flight rows | [runner.py:96-107](../src/bulkvid/orchestrator/runner.py#L96-L107) | medium |
| ZapCap, AtlasCloud: raise on HTTP error, no retry | [zapcap.py:156-278](../src/bulkvid/adapters/zapcap.py#L156-L278), [atlascloud.py:145-245](../src/bulkvid/adapters/atlascloud.py#L145-L245) | low |

### Template side

| Gap | File |
|---|---|
| Blank `script_pattern` → single hardcoded string `"natural conversational opener"` | [script_gen.py:100](../src/bulkvid/pipeline/script_gen.py#L100) |
| Cartoon: blank `script_pattern` → no opener guidance at all | [cartoon_prompt.py:442-443](../src/bulkvid/pipeline/cartoon_prompt.py#L442-L443) |
| Only one admin-editable prompt per tab; no concept of a template library | [runtime_settings.py:162-167](../src/bulkvid/orchestrator/runtime_settings.py#L162-L167) |
| No GPT-based selection step | (does not exist) |

---

## Part A — Overload handling

### Alternatives

**A1 — Minimum viable retry layer (recommended).**
Wrap OpenAI, Gemini TTS, and Sheets in a shared retry helper (3 attempts, exponential backoff with jitter, honor `Retry-After` when present, classify errors as retryable vs. terminal). Add a `BULKVID_ROW_TIMEOUT_SECONDS` env (default 12min) enforced in `BatchRunner` so a stuck row gets cancelled and marked failed instead of holding the semaphore slot. Add stuck-row detection to the existing idle heartbeat — log every in-flight row whose `started_at` is older than N minutes. Small, surgical, low risk, fixes the actual top complaints.

**A2 — A1 + per-provider semaphores.**
A1, plus a global `Semaphore(N)` per provider (OpenAI=20, Gemini=10, kie=6, etc.) so a runaway burst on one provider doesn't drag the others down. Adds one layer of bookkeeping; useful but speculative — we haven't seen evidence the runner-level semaphore (10 concurrent rows) is too coarse.

**A3 — Full overload framework.**
A2 + token-bucket rate limiter scoped to provider+model, persistent backoff state across runner restarts (Turso row), inbound `/poll` rate limit, per-user volume cap. This is the "do it right once" option. Bigger lift; harder to test; postpones shipping.

### Chosen: A1

Reason: it directly fixes what Evgeny called out (OpenAI 429, stuck rows) with the smallest surface area and the cleanest rollback story. A2 and A3 are real improvements but each adds machinery before we have evidence it's the actual bottleneck. Principle 1 (verify, don't guess) — pile in the surgical fixes, measure, then decide if A2/A3 are needed. The retry helper and the row-timeout pattern unblock building A2/A3 later anyway.

### Design

#### A.1 Shared retry helper

New file: `src/bulkvid/adapters/_retry.py`.

```python
async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    op: str,                  # "[openai chat]" / "[gemini tts]" / "[sheets write]"
    attempts: int = 3,
    base_seconds: float = 1.0,
    max_seconds: float = 30.0,
    retryable: tuple[type[Exception], ...] = (...),
    extract_retry_after: Callable[[Exception], float | None] | None = None,
) -> T:
    ...
```

Behavior:
- Attempt 1 → fail → wait `base * 2^0 + jitter` (clamped to `max_seconds`).
- Honor `Retry-After` when the exception carries one (httpx surfaces it via `response.headers`).
- Logs every attempt at `info` with namespaced shape:
  - `[openai chat] retry attempt=1 of=3 wait_s=2.4 reason=429 retry_after=2`
- Final failure raises the last exception unchanged so the existing error mapping is preserved.

Wired into:
- `OpenAIClient.chat` ([openai_client.py:131-195](../src/bulkvid/adapters/openai_client.py#L131-L195)). Retryable: 429, 502, 503, 504, `httpx.ReadTimeout`, `httpx.ConnectTimeout`. Terminal: 400, 401, 403, 422 (model-side bug — retry won't help).
- `GeminiTTSClient.synthesize` ([gemini_tts.py:229-310](../src/bulkvid/adapters/gemini_tts.py#L229-L310)). Retryable: same shape + `google.api_core.exceptions.ResourceExhausted`. Terminal: `InvalidArgument`.
- `SheetsClient.read_rows` and `write_results` ([sheets.py:227-440](../src/bulkvid/adapters/sheets.py#L227-L440)). Retryable: `googleapiclient.errors.HttpError` with status 429/500/502/503/504 and transient `socket.timeout`. Single retry only on writes to avoid double-write surprises if a write succeeded silently.

ZapCap and AtlasCloud — same helper but lower priority, queued behind tests.

#### A.2 Row wall-clock timeout

In `BatchRunner._run_row` (or whatever wraps the per-row coroutine — confirm at implementation time): wrap the row processor call in `asyncio.wait_for(coro, timeout=...)`. Default 12min for simple/4images, 20min for cartoon (read from per-tab settings, since cartoon legitimately runs longer). Env override: `BULKVID_ROW_TIMEOUT_SECONDS_<TAB>`.

On timeout:
- Cancel the row coroutine; let `finally` blocks clean up.
- Mark the row failed in Turso with reason `row_timeout`.
- Release the semaphore slot.
- Log `[runner row] timeout job_id=... row_index=... after_s=720 last_step=script_gen`.

The "last step" comes from the same `extract_current_step()` the sidebar already uses ([step_extractor.py](../src/bulkvid/step_extractor.py)).

#### A.3 Stuck-row visibility in heartbeat

Extend the idle heartbeat ([runner.py:96-107](../src/bulkvid/orchestrator/runner.py#L96-L107)) to also enumerate in-flight rows on every heartbeat tick and log a line for each row whose start time is older than `BULKVID_STUCK_ROW_THRESHOLD_SECONDS` (default 5min). Shape:

```
[runner heartbeat] in_flight=4 stuck_count=1
[runner heartbeat] stuck job_id=abc row_index=37 step=script_gen elapsed_s=412
```

This makes a hang visible in logs without any user action.

#### A.4 Status side-effects in sidebar

The sidebar already shows a per-row step (Phase 1 of the sidebar UX plan). When `row_timeout` fires, surface the reason inline ("Timed out after 12m at: script_gen — open log") so the operator sees it without clicking. No new UI work; reuse the inline-error pattern from `_plans/2026-06-04-sidebar-ux-overhaul.md` §Phase 2.

---

## Part B — Template defaults + GPT pick

### Alternatives

**B1 — Static library + GPT-pick (recommended).**
Add a "Default Script Templates" admin section. Each entry: id, name, body (with macros), optional vertical/country hints, weight. When `script_pattern` is blank, do **one** extra OpenAI call: pass the library (compact: id + name + one-line hint) plus row context (vertical, country, article title), ask for the best id. Look that template up, use its body as the substituted `script_pattern`, then call the existing `generate_script` path. If the selector call fails or returns an invalid id, fall back to the current literal `"natural conversational opener"`.

**B2 — Single-shot generation (no library).**
Skip the library. When `script_pattern` is blank, change the system prompt to "you also pick the best opening style for this row and adapt accordingly", and let the existing script call do both jobs at once. No extra call, no admin surface, lowest cost. Trade-off: no operator control (violates Evgeny's "good control and templates"); not transparent (we can't tell *which* style was used).

**B3 — Deterministic routing rules.**
Operator defines rules like `vertical=fashion → template_apparel`, `country=IL → template_il_native`. No LLM in the picker. Cheapest, most predictable. Trade-off: doesn't scale — you can't write a rule for every (vertical × country × article topic). Doesn't satisfy Evgeny's "GPT will know to choose".

**B4 — GPT picks + adapts macros in one call (library still exists).**
B1 but the selector call also rewrites the chosen template's macros for the row in the same turn — returning a final substituted `script_pattern` string ready to inject. Single LLM hop instead of "pick → substitute → generate". Trade-off: harder to debug (one call doing two things), but lower latency.

### Chosen: B1, with B4 as an opt-in mode flag

Reason: B1 keeps the moving parts separable and observable — we can log which template id was selected, the operator can see the library in admin, and rolling back is "delete the template list". B4 is a clean optimization once B1 is stable; we should ship B1 first and add B4 behind a flag if the second LLM hop becomes a latency complaint. B2 and B3 each fail one of Evgeny's explicit requirements ("good control" / "GPT will know to choose") and are rejected.

### Design

#### B.1 Data model

New runtime setting: `script_template_library` (JSON in the settings_store). Shape:

```json
{
  "version": 1,
  "templates": [
    {
      "id": "warm_friendly_opener",
      "name": "Warm friendly opener",
      "hint": "Conversational, light, podcast-host energy. Good for lifestyle, wellness, fashion.",
      "body": "Warm conversational opener with a specific concrete detail from the article; close with a soft CTA suitable for {country}.",
      "match_hints": {"vertical_any": ["lifestyle","wellness","fashion"], "country_any": []}
    },
    {
      "id": "urgent_news_opener",
      "name": "Urgent news opener",
      "hint": "High-energy news angle. Good for trending, breaking news, viral topics.",
      "body": "Punchy news-anchor opener with the most surprising fact first; urgency without sensationalism, no clickbait.",
      "match_hints": {"vertical_any": ["news","trending"], "country_any": []}
    }
    /* …4 to 6 entries seeded by Yoav + Evgeny */
  ]
}
```

`match_hints` are *advisory* — they go into the selector prompt as soft hints, not hard filters, because we want GPT to override them when the article suggests otherwise.

Register the setting in [runtime_settings.py](../src/bulkvid/orchestrator/runtime_settings.py) alongside the existing prompt keys. Default value: a 4-entry seed library Yoav and I will write together before merging.

#### B.2 Selector

New module: `src/bulkvid/pipeline/template_selector.py`.

```python
async def select_default_template(
    client: OpenAIClient,
    *,
    library: list[Template],
    vertical: str,
    country: str,
    article_title: str,
    article_excerpt: str,
    safety: SafetyContext,
) -> Template | None:
    ...
```

- Model: `gpt-5.4-mini` (same as `generate_script` — already the cost-efficient default).
- Mode: JSON, response schema `{"template_id": str, "reason": str}`.
- Prompt: short — "given the row context and the candidate templates, return the id of the best fit and a one-line reason". Includes the sensitive-apparel safety block when `safety.matched` is true (consistent with `script_gen.py`).
- Token budget: ~200 input + ~80 output per call.
- Cost: ~$0.00018 per blank-cell row at current gpt-5.4-mini pricing — **flagged in §Cost below; verify live before merging per rule 8**.
- Failure handling: any exception → return `None`; caller falls back to the existing literal `"natural conversational opener"` so we never block a row on the selector.

#### B.3 Integration in `script_gen.generate_script`

Single change at [script_gen.py:198-205](../src/bulkvid/pipeline/script_gen.py#L198-L205). When `script_pattern.strip()` is empty AND `settings_store` is provided AND the library has entries:

1. Call `select_default_template(...)`.
2. If a template is returned, use its `body` as the effective `script_pattern`. Log `[script template] selected id=warm_friendly_opener reason="…" vertical=fashion`.
3. If `None`, fall through to the existing literal fallback. Log `[script template] fallback_to_literal reason=selector_failed`.

Macros: the chosen template's body is run through `_substitute()` the same as before — so existing `{country}`, `{vertical}` etc. work for free. If we want template-specific macros later (e.g. `{competitor}`), we add them when needed; not now.

#### B.4 Admin UI

In the existing admin settings panel ([src/bulkvid/admin/](../src/bulkvid/admin/)), add a section "Default script template library". Operators can:
- See the list with id, name, hint.
- Edit body / hints inline.
- Add a new template (id auto-slug from name).
- Reorder (drag, optional — text-input order works fine v1).
- Toggle individual templates on/off.

The shape mirrors the existing per-tab prompt editor — same component, JSON-list-of-objects edit pattern. No new design system work; reuse §Rule 16 from CLAUDE.md.

### What happens per tab

- **Simple, 4Images-VO2, Image-VO**: blank `script_pattern` → selector → template body substituted → existing pipeline.
- **Cartoon**: same selector but template body is appended to `cartoon_prompt`'s system prompt (mirror the existing `if script_pattern.strip()` branch at [cartoon_prompt.py:442-443](../src/bulkvid/pipeline/cartoon_prompt.py#L442-L443)).

---

## Security (CLAUDE.md rule 13)

Threat model is narrow because the new code paths don't touch auth or external user input — operators are trusted users editing settings via the existing authenticated admin.

- **Prompt injection via template body.** An operator (or an attacker who compromised an operator account) could put `Ignore previous instructions…` into a template body. Mitigation: same as the existing per-tab prompt editor — bodies are operator-trusted, but we keep the OpenAI system prompt assembled server-side, so a hostile body can't escape the system role. No PII flows through templates.
- **Selector output.** GPT returns a `template_id`. We **strict-validate** the returned id against the library (id must exist + be enabled); never trust raw string back into the prompt assembly. Anything else → fallback.
- **Retry helper backoff time.** Cap at `max_seconds=30` so an attacker can't use a flood of 429s to wedge a worker for hours. Also cap total attempt count at 3.
- **Row timeout** is itself a safety mechanism — no row holds resources indefinitely.
- **Sheets retry.** Writes get only 1 retry. A successful-but-network-dropped write is hard to detect — we'd rather double-log than double-write. (`write_results` is currently not idempotent at the sheet level — flag for later.)
- **No new secrets** introduced. The selector reuses the existing OpenAI key from `clients.py`.

## Observability (CLAUDE.md rule 14)

Namespaced log lines, in the existing format. Every step gets one.

### Part A
- `[openai chat] retry attempt={n} of={max} wait_s={s} reason={status_or_exc} retry_after={s_or_null}`
- `[openai chat] retry_exhausted attempts={n} final_reason={…}`
- `[gemini tts] retry …` (same shape)
- `[sheets read] retry …` / `[sheets write] retry …`
- `[runner row] timeout job_id={…} row_index={…} after_s={…} last_step={…}`
- `[runner heartbeat] in_flight={n} stuck_count={n}` (every heartbeat tick)
- `[runner heartbeat] stuck job_id={…} row_index={…} step={…} elapsed_s={…}` (one line per stuck row)

### Part B
- `[script template] library_loaded count={n}` (once per process, at first use)
- `[script template] selected id={…} reason="{…}" vertical={…} country={…}` (per blank-cell row)
- `[script template] fallback_to_literal reason={selector_failed | empty_library | invalid_id} error={…}`

Existing log namespaces in the codebase already use the `[ns op]` shape (`script_submit`, `script_ok`, `safety_applied`) so these slot in cleanly.

## Settings audit (CLAUDE.md rule 15)

New admin-visible knobs, all in the existing settings panel:

| Setting | Group | Default | Notes |
|---|---|---|---|
| `script_template_library` | Templates | seeded 4 entries | Edit, add, disable individual templates |
| `template_selector_enabled` | Templates | `true` | Master switch — `false` reverts to literal fallback |
| `row_timeout_simple_seconds` | Runner | `720` | 12min |
| `row_timeout_4images_seconds` | Runner | `720` | |
| `row_timeout_image_vo_seconds` | Runner | `900` | 15min — image work is heavier |
| `row_timeout_cartoon_seconds` | Runner | `1200` | 20min — planner + multi-shot |
| `stuck_row_threshold_seconds` | Runner | `300` | 5min — when heartbeat starts flagging |

Env overrides exist for the runner ones to allow per-deploy tuning without admin access.

Hardcoded retry parameters (3 attempts, 1s→30s backoff) are NOT exposed as settings. Rule 15 says "ask: does the user want to control this?" — for retry counts, no. They're an implementation detail; if we tune them, it's in code with a PR, not in admin. Reasoning called out so future-Yoav doesn't ask why.

## Testing (CLAUDE.md rule 18)

Project's test framework: pytest with `pytest-asyncio` (visible in `tests/` and `pyproject.toml` — confirm at implementation time).

### Part A — tests to add

- `tests/adapters/test_retry.py`
  - Retries on 429, succeeds on attempt 3. Asserts backoff timing within tolerance.
  - Honors `Retry-After` when set.
  - Does NOT retry on 400 / 401.
  - Exhausts attempts on persistent 500.
- `tests/adapters/test_openai_client_retry.py`
  - Mocks the httpx layer to return 429 once then 200. Asserts a single retry.
  - Mocks 429 forever, asserts `OpenAIRateLimitError` raised after 3 attempts.
- `tests/adapters/test_sheets_retry.py` — same shape, single-retry on writes.
- `tests/orchestrator/test_runner_timeout.py`
  - Submits a row whose processor sleeps past the timeout; asserts row marked failed with reason `row_timeout`, semaphore released, log line emitted.
- `tests/orchestrator/test_heartbeat_stuck.py`
  - With in-flight row older than threshold, asserts heartbeat log shape.

For each retry: include a regression test that fails on the pre-change code and passes on the post-change code (rule 18 bug-fix-as-test).

### Part B — tests to add

- `tests/pipeline/test_template_selector.py`
  - Library with 3 templates, mocked OpenAI returns `{"template_id": "X"}`. Asserts the right template body is returned.
  - Selector returns invalid id → `None` returned, fallback used.
  - Selector raises → `None` returned, no exception propagated.
- `tests/pipeline/test_script_gen_with_library.py`
  - Blank `script_pattern` + library present → template body substituted into final prompt.
  - Blank `script_pattern` + empty library → existing literal fallback used (regression guard).
  - Non-blank `script_pattern` → selector NOT called (existing behavior unchanged).
- `tests/admin/test_template_library_settings.py`
  - Save / load round-trip through `settings_store`.
  - Reject invalid JSON shape.

### Full suite

Run `pytest` over the whole project after both parts land. The bar is green, not "the new tests pass".

## Cost (CLAUDE.md rule 8)

The template selector adds **one** extra OpenAI call per row whose `script_pattern` is blank. At current `gpt-5.4-mini` pricing — **verify live at models.dev before merging** — ~$0.00018 per call (200 in + 80 out tokens). A 1000-row batch where every cell is blank is roughly **$0.18 extra**.

Not free, not significant. Worth flagging so we're not surprised by an OpenAI bill spike, and so we know the knob (`template_selector_enabled = false`) if costs ever balloon.

No new paid services. No subscription. No infra cost change.

## Open questions (need Yoav's call before implementation)

1. **Library seed.** Who writes the 4 to 6 initial templates? I can draft a starting set based on the existing `script_gen` system prompt; you and Evgeny refine. Or you want to write them yourselves and I just wire the plumbing?
2. **Per-tab timeout values.** The defaults above (12 / 15 / 20 min) are guesses based on the ETA medians work already done. If you have the median runtimes from production, use those × 3 instead.
3. **Cartoon template surface.** Confirm cartoon should use the same library, or have a separate "Default cartoon planner templates" list? The cartoon planner prompt is structurally different (it generates shot lists, not a finished script).
4. **Selector enabled by default in initial deploy?** Recommendation: yes (`true`), so Evgeny sees it working immediately. You can flip it off in admin within seconds if it misbehaves.
5. **Should I run this plan through `llm-council` before we lock it in?** Per CLAUDE.md rule 11, this qualifies as an important decision. My read: the plan is solid but pressure-testing the **B1 vs B4 split** and the **A1 vs A2 cut-line** would benefit from independent perspectives. Up to you.

## Phased rollout

- **Phase 1 — retry layer (A.1).** Lowest risk, biggest immediate win. Ship behind no flag (always on).
- **Phase 2 — row timeout + stuck heartbeat (A.2, A.3, A.4).** Independent of Phase 1; can ship same PR or next.
- **Phase 3 — template selector backend (B.1, B.2, B.3).** No UI yet; library editable via raw JSON in admin.
- **Phase 4 — template library UI (B.4).** Polished admin surface, same component shape as per-tab prompts.

Each phase is independently shippable and reversible. None requires data migration.
