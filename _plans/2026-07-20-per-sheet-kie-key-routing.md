# Per-sheet kie.ai key routing

Date: 2026-07-20
Status: approved (chat 2026-07-20) — implementing

## Goal

Let a specific spreadsheet use a specific kie.ai API key, without standing up a
second backend. Concretely: the duplicated sheet "Bulk Videos 2" must draw from
a NEW kie.ai key, while the original "Bulk Videos" (and every other sheet) keeps
using today's `KIE_AI_KEYS` pool. One backend, one deploy.

Hard acceptance criterion (user's words): "the new spreadsheet uses the new api
key." A misroute that silently falls back to the old key is a failure, so
verification has to be loud and easy.

## Why this is needed / the constraint that forced it

The kie key is a BACKEND env var (`KIE_AI_KEYS`), read once at worker start into
a single global `KieClient` (`kie.build_client_from_settings` ->
`worker.build_pipeline_clients`). The Apps Script sheet only knows a
`BACKEND_URL` — it can point at a backend, not at a key. So "assign a different
key to a sheet" has no home in today's wiring. Two ways to give it one:

  * second backend (its own `KIE_AI_KEYS`) — doubles deploy + duplicates DB /
    worker / every other key. Rejected: heavy, permanent maintenance tax.
  * teach the one backend to pick a key by the job's `sheet_id`. Chosen.

The seam is small because `sheet_id` already: (a) rides the submit payload
(`SubmitJobIn.sheet_id`), (b) is stored on the job row (`jobs.sheet_id`), and
(c) is reachable in the row-claim query, which ALREADY joins `jobs`
(`_claim_next_row_sync`), so routing costs zero extra DB round-trips.

## Approach

A `sheet_id -> KieClient` router, resolved once per row at the runner seam. Row
processors are untouched (all 14 keep calling `clients.kie`).

1. **config.py** — new `KIE_KEY_MAP: str = ""` plus a parsed property
   `kie_key_map -> dict[str, list[str]]`.
   Format (forgiving, HF-secret friendly): entries split on newlines AND commas;
   each entry `sheetId = key1|key2` split on the FIRST `=`; multiple keys per
   sheet split on `|`. Blank/malformed entries are skipped with a warning.

2. **kie.py** — `KieClientRouter` holding a `default` `KieClient` (the
   `KIE_AI_KEYS` pool) and a `dict[str, KieClient]` per mapped sheet. Method
   `for_sheet(sheet_id) -> KieClient` returns the mapped client or `default`.
   `build_router_from_settings(settings)` builds it; the default is the SAME
   instance used as `clients.kie`, so no pool is built twice. A build-time
   `kie_router_init` log records mapped-sheet count + key suffixes.

3. **clients.py** — `PipelineClients` gains `kie_router: KieClientRouter | None
   = None` (optional; tests that build the bundle directly are unaffected).

4. **worker.py** — `build_pipeline_clients` builds the router, sets `kie =
   router.default` and `kie_router = router`.

5. **queue.py** — `QueuedRow` gains `sheet_id: str = ""` (default keeps existing
   constructors working); `_claim_next_row_sync` selects `j.sheet_id` from the
   join it already does.

6. **runner.py** — in `_handle_row`, when `clients.kie_router` is set, resolve
   `router.for_sheet(queued.sheet_id)` and dispatch with
   `dataclasses.replace(self._clients, kie=<chosen>)`; otherwise dispatch
   `self._clients` unchanged. Log the routing decision ONCE per sheet per worker
   process (a small `seen` set) so a 200-row batch doesn't flood, but the
   operator can still confirm "sheet X -> key ...abcd" in the logs.

7. **health.py** (`/health/deep`, admin-only) — add a `kie_key_map` block:
   `{sheet_id: ["…sfx1"], …}`, suffixes only, matching the existing masking. The
   operator's one-glance verification that Bulk Videos 2 maps to the new key.

8. **.env.example** — document `KIE_KEY_MAP` with an example.

## Alternatives rejected

  * **Second backend.** Clean isolation but doubles deploys forever and
    duplicates DB/worker/other keys. Overkill for "one sheet, one key."
  * **Add the new key to the shared `KIE_AI_KEYS` pool.** Zero code, but both
    sheets round-robin across both keys — cannot guarantee "Bulk Videos 2 uses
    the new key," which is the explicit requirement.
  * **Thread `sheet_id` into every row-processor call.** Touches 14 processors
    and their signatures. The `dataclasses.replace` swap at the single runner
    seam achieves the same with one call site changed.

## Security / safety

  * Keys stay env-only (HF Space secret), never in code or logs. Logs and
    `/health/deep` show last-4 suffixes only, matching the existing kie block.
  * Routing key is the client-supplied `sheet_id`. This is NOT a trust boundary
    between untrusted tenants: every caller is an allowlisted operator, and all
    keys belong to the same org. A caller could set another sheet's id and use
    that key — acceptable here (same owner), and called out so no one later
    mistakes this for tenant isolation.
  * Fail-soft: an unmapped/blank/garbled sheet_id falls back to the default
    pool, so a typo degrades to "old key," never to "no video." The loud
    build-time log + `/health/deep` are how we catch a typo before it matters.

## QA plan

  * Unit: `kie_key_map` parser (single, multi-key, newline+comma mix, blank,
    malformed, whitespace); `KieClientRouter.for_sheet` (mapped, unmapped ->
    default, empty id -> default); `_claim_next_row_sync` returns `sheet_id`;
    runner dispatches the mapped client for a mapped sheet and the default
    otherwise.
  * Full `pytest` run (adjacent regressions: worker wiring, runner, kie).
  * Manual on the live Space after deploy: set `KIE_KEY_MAP`, hit
    `/health/deep`, run one Bulk Videos 2 row, confirm the routing log + that
    the render succeeds.

## Deploy / operator steps (after merge)

  1. On the HF Space, add secret `KIE_KEY_MAP` = `<BulkVideos2_sheetId>=<newKey>`.
     Leave `KIE_AI_KEYS` as-is (original sheet keeps using it).
  2. `git push hf main`; verify runtime.sha via the HF API.
  3. GET `/health/deep` as admin — confirm `kie_key_map` shows the new suffix
     under Bulk Videos 2's id.
  4. Run one Bulk Videos 2 row; confirm the `kie_router_selected` log names the
     new suffix.

## Open questions

  * Multi-key per sheet is supported (`|`) but the immediate need is one key.
    Left in because it is free (same `KiePool`) and gives per-sheet rate-limit
    headroom later.
