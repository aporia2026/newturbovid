# Migrate TurboVid backend off PythonAnywhere to HuggingFace Spaces + Turso

Date: 2026-06-04
Status: proposed (awaiting Yoav's approval before execution)
Owner: Yoav
Replaces: PA-hosted backend (the cause of the chronic dispatch-flake 500 toasts)
Builds on: `_plans/2026-06-04-submit-500-defensive-fix.md` (the band-aid that
made the failures less visible — this plan removes the cause)

## Problem (already verified)

- PythonAnywhere's uWSGI frontend persistently returns HTTP 500 to a non-trivial
  fraction of `/jobs` POSTs even with the new 31-second retry window. Logs prove
  no Python traceback — PA's frontend is rejecting requests before they reach
  the app. (See `_plans/2026-06-04-fix-sidebar-500s.md` for the original
  diagnosis and `_plans/2026-06-04-submit-500-defensive-fix.md` for the
  client-side mitigation already shipped.)
- The PA account is shared with other Aporia projects; upgrading to PA's paid
  Hacker/Developer plan is not a decision Yoav owns. **Hard constraint: $0/mo,
  no company purchasing approval needed.**
- Status quo on free PA is unacceptable — the toasts persist and the team's
  trust in the tool is eroding.

## Goal

Move the FastAPI web app AND the always-on worker to a host that:

1. **Costs $0/mo, permanently** — no trials, no "free for 30 days", no credit
   card requirement that risks a surprise charge.
2. **Supports both processes natively** — a web server AND a long-lived
   worker. Not serverless. Not "spins down after 15 min".
3. **Doesn't have PA's flaky dispatch** — a real Linux container we control.
4. **Requires no sysadmin appetite** — managed environment, push code via git
   like we do today.
5. **One-time half-day migration**, not weeks of rewriting.

## Approach: HuggingFace Spaces (Docker) + Turso

Two free managed services, glued together. Both verified live in 2026:

- **HuggingFace Spaces** — Docker container hosting. Free CPU Basic tier:
  2 vCPU + 16 GB RAM, ports 7860 exposed, public Space (auth-gated by our
  existing Google OAuth so the public URL is harmless). Sleeps after 48 h of
  inactivity — our sidebar polling at 3-30 s cadence prevents this forever.
  Filesystem is ephemeral except `/tmp`. Source:
  https://huggingface.co/docs/hub/en/spaces-overview ,
  https://huggingface.co/docs/hub/spaces-sdks-docker
- **Turso (libSQL)** — managed SQLite. Free tier: ~9 GB storage, 500 M-1 B
  row reads/mo, 500 databases, no credit card. libSQL is a fork of SQLite —
  same file format, same SQL dialect. Source: https://turso.tech/pricing

### Architecture (new)

```
                  ┌──────────────────────────────────────────────┐
                  │  HF Spaces Docker container (2vCPU/16GB)      │
                  │                                                │
   Google Sheet ──┤  ┌──────────────────┐    ┌──────────────────┐ │
   (Apps Script) →│  │  uvicorn         │    │  bulkvid.worker  │ │
   POST /jobs     │  │  bulkvid.main:app│    │  (asyncio loop)  │ │
                  │  │  :7860           │    │                  │ │
                  │  └────────┬─────────┘    └────────┬─────────┘ │
                  │           │ libsql_client          │           │
                  │           └────────┬───────────────┘           │
                  │                    ▼                            │
                  │           ┌──────────────────┐                  │
                  │           │ /tmp/job_logs/   │  (ephemeral —    │
                  │           │ <job-id>.log     │   ok for live    │
                  │           └──────────────────┘   tail; archive  │
                  │                                  is in DB)      │
                  └─────────────┬─────────────────────┘
                                │ HTTPS (libsql wire)
                                ▼
                       ┌──────────────────┐
                       │ Turso (libSQL)   │
                       │  bulkvid-jobs    │
                       │  bulkvid-settings│
                       └──────────────────┘
```

Both processes run inside the SAME container via a minimal supervisor (either
`supervisord` or a 5-line entrypoint shell script). Both share the Turso
client via the same connection URL and auth token. Container restarts → DB
state intact in Turso; in-flight `/tmp` log files are lost (acceptable).

### Code changes (small, focused)

1. **New `Dockerfile`** at repo root (~25 lines). Python 3.12-slim base, pip
   install from `pyproject.toml`, supervisord launches uvicorn + worker. HF
   Spaces honors a Dockerfile in the repo root automatically.

2. **New `supervisord.conf`** at repo root (~15 lines). Two `[program:]`
   stanzas: `web` (uvicorn) and `worker` (`python -m bulkvid.worker`). Both
   `autorestart=true`. Logs to stdout/stderr so they land in HF's log viewer.

3. **New thin DB adapter** in `src/bulkvid/orchestrator/db.py`. Provides the
   small subset of the sqlite3 API our queue + settings store actually use:
   `connect`, `cursor`, `execute`, `executemany`, `executescript`, `row_factory
   = Row`, `BEGIN IMMEDIATE / COMMIT / ROLLBACK`. Backed by `libsql_client`
   when `BULKVID_DB_URL` is set (Turso); falls back to plain `sqlite3` when
   not (local dev + tests). Smallest possible delta — `queue.py` and
   `settings_store.py` change one import line each.

4. **Config** — `src/bulkvid/config.py` gets `BULKVID_DB_URL` and
   `BULKVID_DB_AUTH_TOKEN` settings. Empty/unset = local SQLite (today's
   behaviour). Set = Turso. Both empty in tests so the suite stays hermetic.

5. **Logging path** — `src/bulkvid/logging.py` currently writes job log files
   to `<BULKVID_DATA_DIR>/logs/`. On HF, `BULKVID_DATA_DIR=/tmp/data`. Log
   files survive as long as the container does (typically days to weeks
   between restarts); a container restart loses live logs but completed-job
   results are in Turso. We accept this trade-off for now (flagged as a
   follow-up — could stream to Turso later if it becomes painful).

6. **HF Secrets** — `.env` values that today live on PA disk move to HF
   Space "Secrets" (encrypted env vars). No code changes; settings already
   read from env.

7. **Apps Script** — change ONE line: `BACKEND_URL` script property points
   at `https://<yoav>-<spacename>.hf.space`. The whole new retry/idempotency
   layer we just shipped works unchanged.

### What does NOT change

- Apps Script logic (other than the URL).
- The job orchestration code, the pipeline adapters, the row processors,
  the safety filter, the script generator — all untouched.
- The test suite — `BULKVID_DB_URL` empty in tests means the existing 560
  tests run against the same local SQLite they always have.

## Alternatives considered and rejected

1. **Render free tier.** Web service spins down at 15 min idle and
   background workers are explicitly NOT in the free tier
   (https://render.com/docs/free). Hard rejection — we'd lose the worker.
2. **Koyeb free tier.** 0.1 vCPU + 512 MB RAM, scales to zero after 1 h, and
   explicitly "can't be used as a Worker Service" on free. Same hard
   rejection.
3. **Fly.io.** Removed the free tier for new customers in Oct 2024
   (`fly.io/docs/about/pricing`). Doesn't meet the $0 constraint.
4. **Vercel.** Serverless only — no long-lived worker. Would require a full
   rewrite of the orchestrator into Vercel Workflow DevKit. Weeks of work for
   a worse fit.
5. **Oracle Cloud Always Free Tier.** Genuinely free, very generous VM, but
   Yoav explicitly turned this down — Oracle's signup friction and reputation
   are non-starters in this case.
6. **Cloudflare Tunnel + an always-on machine at the office.** Free + total
   control, but requires a 24/7 host machine and Yoav doesn't currently have
   one allocated. Kept on the shelf as a fallback if the HF/Turso path runs
   into ToS trouble.
7. **HuggingFace Spaces with bundled SQLite + periodic backup to a private
   HF Dataset.** Avoids the Turso dependency but adds a custom backup/restore
   script and accepts up-to-N-minutes of data loss on restart. Rejected:
   Turso is the cleaner primitive — SQL-compatible managed storage with no
   write-loss window.
8. **Switch to Postgres on Supabase/Neon.** Both also free, both very mature.
   Rejected because the migration delta from current code is much bigger:
   every SQL query touches the schema, the `INSERT ... ON CONFLICT` syntax
   needs porting, the `BEGIN IMMEDIATE` semantics don't translate, the
   in-process WAL behavior changes. Turso/libSQL is essentially the same SQL
   we already write.

## Costs (Rule 8)

Verified live, 2026-06-04:

| Service | Free allowance | Our expected usage | Headroom |
|---|---|---|---|
| HuggingFace Space (CPU Basic) | 2 vCPU, 16 GB RAM, unlimited time, sleeps at 48 h idle | ~5-10 % CPU continuous, ~500 MB RAM, never idle (sidebar polls) | Massive |
| HuggingFace Datasets (logs backup, optional) | Public datasets unlimited, private 1 dataset on free | 1 private dataset, <100 MB/mo | Massive |
| Turso (libSQL) | 5-9 GB storage, 500 M-1 B row reads/mo, 500 databases | 2 databases, <10 MB, <100 K row reads/mo | Massive |

**Real risk**: HF Spaces free tier is for "AI demos" by intent. Generic Docker
apps (n8n, ComfyUI, internal tools) are tolerated in practice and explicitly
supported by the Docker Spaces docs, but HF could change posture. If they do,
we move to Cloudflare Tunnel + an always-on machine (alternative 6 above) or
revisit Oracle. Migrating between Docker hosts is hours, not days, because
the Dockerfile is portable.

**Real risk**: Turso is a startup. Storage limits have shifted (5 → 9 GB over
the past year). Worst case: they kill the free tier or shut down → migrate
to self-hosted `sqld` or Postgres. Our DB is <10 MB; export/import is
trivial.

**No credit card on either service.** No risk of accidental billing.

## Security & safety (Rule 13)

- **Public Space, private logic.** The Space URL is world-reachable, but
  every `/jobs*` route is auth-gated by the existing Google OAuth + email
  allowlist. `/health` is public on PA today too. No change in attack
  surface.
- **No secrets in code.** All keys (Turso auth token, ZapCap key, kie.ai
  key, etc.) move from PA's filesystem `.env` to HF Space Secrets
  (encrypted env vars). The repo never sees them. Existing `bulkvid.config`
  reads from env, so no code change.
- **Public Space repo code is visible.** This is fine — our security model
  was never "obscurity". The auth check is the only line of defense and
  always has been.
- **Turso auth token scoped to one database.** Two databases (jobs +
  settings) get two separate tokens, both with read+write only, no admin.
  Lets us rotate one without disrupting the other.
- **TLS handled by HF**; we never expose our own cert. Same for Turso
  client connections (libsql over HTTPS).
- **No new exfil surface**: outbound calls from the Space to Turso, OpenAI,
  kie.ai, Rendi, Gemini, ZapCap, Storage — same set we already hit from PA.
- **Rate-limiting is unchanged.** We rely on Google OAuth + allowlist; HF
  doesn't add a layer here. If we ever need WAF-like rate limits, that's
  Cloudflare in front (cheap follow-up).

## Observability (Rule 14)

- **Container stdout/stderr → HF Space "Logs" tab.** uvicorn and worker
  both log to stdout; supervisord forwards. Same `[bulkvid <ns>]` namespaces
  we already use — log code itself does not change.
- **Per-job log files** still land in `<DATA_DIR>/logs/<job_id>.log` (now
  `/tmp/data/logs/<job_id>.log`), still served by the existing
  `/jobs/{id}/log` endpoint. Sidebar log pane works unchanged.
- **New log lines on boot** so we can spot misconfig from the HF logs
  tab without SSH:
  - `boot_db_backend  backend=turso`  vs  `backend=sqlite_local`
  - `boot_db_url      host=<turso-host>` (host only, never the auth token)
  - `boot_data_dir    path=/tmp/data`
- **Health endpoint** stays at `GET /health`. Add `GET /health/deep`
  (admin-gated) that pings Turso with `SELECT 1` and reports the round-trip
  ms. Lets us notice DB latency degradation before users do.
- **Existing `idempotency_hit` / `queue_busy_503` / `verify_ok` logs**
  continue to work — they're observability we already have, now flowing
  through HF's log viewer instead of PA's.

## Settings (Rule 15)

- New ENV vars (not user-facing settings; they're deploy-time config):
  - `BULKVID_DB_URL` — libsql:// URL of the Turso jobs DB (or empty for
    local SQLite fallback).
  - `BULKVID_DB_AUTH_TOKEN` — Turso jobs DB token.
  - `BULKVID_SETTINGS_DB_URL`, `BULKVID_SETTINGS_DB_AUTH_TOKEN` — same for
    the settings store.
  - `BULKVID_DATA_DIR` — `/tmp/data` on HF; defaults to `./data` locally.
- No new admin-panel settings. The retry/backoff knobs we considered in the
  band-aid plan are still internal constants — nothing exposed.
- Audit: the existing settings store (script prompts, model overrides)
  migrates to Turso unchanged — admin edits persist exactly as today.

## Testing (Rule 18)

The existing 560-test suite must stay green throughout. Key additions:

- `test_db_adapter_sqlite_fallback` — when `BULKVID_DB_URL` is empty, adapter
  uses plain `sqlite3` and existing queue tests pass identically.
- `test_db_adapter_libsql_basic` — when `BULKVID_DB_URL` points at a mocked
  libsql, adapter forwards execute/executemany/executescript correctly.
  Mock-only (we don't want test runs hitting a real Turso DB).
- `test_queue_with_libsql_adapter` — re-runs a small subset of the existing
  queue tests through the libsql adapter (mocked) to confirm `BEGIN
  IMMEDIATE` / `COMMIT` / `ROLLBACK` semantics survive the adapter.
- `test_health_deep_pings_db` — `/health/deep` returns ok + `db_ping_ms`
  when the DB is reachable.

Out of scope for tests (manual verification on first deploy):
- Real Turso round-trip latency from HF Space (will measure on first boot).
- supervisord process restart on uvicorn crash.

## Rollout

1. **Day 1 — Code (~3 h, this session if you say go).**
   - Write `db.py` adapter + thin tests.
   - Swap imports in `queue.py` and `settings_store.py`.
   - Add `Dockerfile`, `supervisord.conf`, `.dockerignore`.
   - Add the new ENV settings to `config.py`.
   - Add `boot_db_backend` / `boot_data_dir` boot logs.
   - Run the full suite, confirm 560+ still green.
   - One commit. Don't push yet — let's smoke test locally first.

2. **Day 1 — Provisioning (~30 min, Yoav).**
   - Sign up for Turso (GitHub OAuth, no CC). Create two databases:
     `bulkvid-jobs` and `bulkvid-settings`. Generate scoped tokens for
     each. Pick the region closest to HF's eu-west or us-east region.
   - Sign up for HuggingFace (if not already). Create a new Docker Space:
     name `aporia-bulkvid`, public, CPU Basic. Add Secrets for every var
     in our `.env` plus the two `BULKVID_DB_URL` / `_AUTH_TOKEN` pairs.

3. **Day 1 — Local smoke (~30 min).**
   - Set the new env vars locally pointing at Turso.
   - Boot uvicorn + worker locally; submit a 1-row job from a sheet pointed
     at `http://localhost:8000`. Confirm the job completes end-to-end and
     the row writes back to the sheet.
   - Check the logs in the HF Space "Logs" tab once we push.

4. **Day 1 — Deploy (~30 min).**
   - Push the repo to the HF Space's git remote.
   - HF auto-builds the Docker image (~3-5 min). Watch the build log.
   - Once the Space is "running", hit `/health` from a browser — expect
     `{"status":"ok"}`.
   - Hit `/health/deep` with an admin bearer token — expect
     `db_ping_ms < 500`.

5. **Day 1 — Cut over (~10 min).**
   - In the Apps Script editor, change `BACKEND_URL` script property to
     the HF Space URL.
   - Submit a real 1-row job from the sheet. Watch:
     - HF Space logs for `job_submit` / `job_enqueued`.
     - The sidebar for live row status.
     - The sheet for the Ready Video URL.
   - If green: leave PA running for 48 h as a rollback option (just don't
     point traffic at it).

6. **Day 2-3 — Watch.**
   - Watch HF logs for any `queue_busy_503` (should be zero — no flaky
     dispatch on HF), `idempotency_hit` (should drop to ~zero — no PA to
     drop responses), and any Python tracebacks.
   - If any user reports a regression, flip `BACKEND_URL` back to PA, fix,
     redeploy.

7. **Day 7 — Decommission PA.**
   - Once we've been green for a week, turn off the PA always-on task and
     wipe the PA `data/` directory. PA web app stays alive but unused — it
     costs nothing on the free tier.

## Rollback

Cheap, single-step: change `BACKEND_URL` in Apps Script back to the PA URL.
PA stays online and idle during the watch window precisely so this is
instant. After day 7 we lose the instant rollback, but by then we'll know
HF/Turso is stable.

## Open questions (need a quick answer before execution)

1. **Region.** Turso has eu-west and us-east. HF Spaces hardware location
   varies. Closest pair minimizes DB latency. Worth picking on the way in.
2. **Space name.** `aporia-bulkvid`? `turbovid-prod`? Yoav's call — it
   becomes part of the URL.
3. **Domain.** Plain `*.hf.space` is fine to start. Custom domain is a
   later step (free if we already own one and proxy via Cloudflare).
4. **Sensitive Secrets list.** I'll prepare the full list from the current
   `.env` so you can paste them into HF Secrets in one go.

## Out of scope (flagged, not done here)

- Custom domain on the Space.
- Streaming logs to Turso for restart durability.
- Multi-region failover.
- Migrating to paid HF Spaces if free hits limits (it won't, given our
  usage profile — but if it ever does, the upgrade is ~$0.05/h = $36/mo).
