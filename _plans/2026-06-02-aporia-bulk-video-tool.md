# Aporia Bulk Video Tool — TurboVid

**Plan version:** 1.0
**Date:** 2026-06-02
**Author:** Yoav + Claude
**Status:** Approved — ready to execute
**Repo:** github.com/aporia2026/newturbovid → `c:\Projects\turbovid-new`

---

## 1. Overview

TurboVid is a bulk video creation tool for Aporia's bulk team. The team uploads campaigns to Facebook (later Taboola, Outbrain, Google) and needs to produce **simple videos at scale**: a resized image plus a voiceover, optionally with burned-in subtitles. Up to 4 videos per row, up to several thousand rows per batch.

The input surface is a Google Sheet with two tabs: **Image-VO** (AI-generated images from a single seed image, using the mandatory collage-split method) and **4Images-VO2** (user-supplied images). The output is video URLs written back to the sheet.

The tool is built as a Python/FastAPI service deployed as a sibling Docker container on the existing Aporia Hetzner host (`46.225.88.89`, alongside `cb-remote-runner`). An admin panel served by the same service lets Yoav control every model, default, and feature flag without redeploying. The bulk team interacts only with the sheet — a custom menu and sidebar talk to the backend.

---

## 2. Goals

1. Produce simple videos (resized image + voiceover, optionally subtitles) in bulk for Facebook campaigns.
2. Two input tabs: Image-VO (AI image gen via mandatory collage-split) and 4Images-VO2 (user-supplied images).
3. 200 rows complete in 12–20 minutes; 1,000 rows in under 70 minutes.
4. Article language drives voiceover language (not Country).
5. Open Comments column is the highest-priority signal for the script generator.
6. Admin panel for Yoav controls all models, defaults, and feature flags.
7. Reuse the existing Aporia infrastructure patterns (`cb-remote-runner`-style) and the battle-tested collage-split image method.

---

## 3. Constraints

- **Primary deploy target: PythonAnywhere** (management directive, confirmed by Karmel 2026-06-02). Hetzner Docker remains a documented migration target if PA proves insufficient.
- Architecture must be **deploy-agnostic**: same code runs as a PA always-on task today, swaps to a Hetzner Docker container later with no code changes — only entrypoint and concurrency tuning differ.
- Python only (no TypeScript rewrite).
- Collage-split image method is **mandatory** (one 2×2 collage → upscale → split into 4 with edge crop).
- Simple videos only: still image + voiceover. **Zero animations.**
- Script generation model: **gpt-5.4-mini** (Yoav directive).
- TTS: **Vertex AI Gemini TTS** (existing project `amit-tts`, with rotated key).
- Storage primary: **S3 `aporia-creative`**; fallback **GCS `aporia-unleash`**.
- Metadata log: **`SYMPHONY_DB` master sheet + S3 `/Symphony/Meta Data/` JSON files** (match existing Symphony pattern).
- All API keys live in `.env` only, never in code.
- Auth via Google OAuth ID token → JWT verify on the backend → Aporia Workspace domain + bulk-team email allowlist.

---

## 4. Requirements

### In scope

- Image-VO tab end-to-end: article fetch → script → image gen (collage-split) → TTS → 4 videos → optional ZapCap → sheet write-back
- 4Images-VO2 tab end-to-end: article fetch → script → resize user images → TTS → up to 4 videos → optional ZapCap → sheet write-back
- Open Comments handling in 4 modes (tone bias / content directive / full override / mixed)
- Multi-language script generation and TTS (language detected from article)
- Aspect ratio handling for any user-typed value, with "blurred background fit" via Rendi.dev
- Admin panel with model selection, defaults, cost guards, feature flags, audit log
- Apps Script in the Sheet (custom menu + sidebar for live status)
- Per-row independent completion (write back immediately, no batch wait)
- Cost tracking per row, per batch, per month with kill switch
- Health endpoints + deep vendor connectivity check
- Comprehensive observability with namespaced logs

### Out of scope (explicit)

- Animated videos, image-to-video, motion effects, Ken Burns
- Multi-platform expansion (Taboola, Outbrain, Google) — Facebook first, others later
- Mobile UI for the bulk team
- A separate web app for batch monitoring (sidebar in the sheet is enough)
- Real-time collaborative editing
- Custom font / template uploads in v1 (use existing ZapCap default template)

---

## 5. Architecture

### Process split (deploy-agnostic)
The codebase has two entrypoints sharing the same modules:

1. **Web app** (`bulkvid.main:app`) — FastAPI ASGI. Handles `/jobs` submit, `/jobs/{id}` status, `/admin/*`, `/health`. Reads + writes SQLite. Stateless between requests. Restarts cheap.
2. **Worker** (`bulkvid.worker:run`) — long-running async loop. Drains the SQLite job queue, processes rows concurrently via semaphore, writes results back to Sheets and storage. Lives as long as the host lets it.

Both share `bulkvid.config`, `bulkvid.adapters`, `bulkvid.pipeline`, `bulkvid.orchestrator`. Migration between hosts = redeploy with a different launcher; zero code changes.

### Deployment topology — primary (PythonAnywhere)
- Web app on PythonAnywhere ASGI (beta) — serves `/jobs`, `/admin`, `/health`
- Always-on task (1 slot, Developer plan) runs `python -m bulkvid.worker`
- SQLite at `~/bulkvid/data/jobs.db` and `~/bulkvid/data/settings.db`
- Per-job logs at `~/bulkvid/data/logs/<job_id>.log`
- Public URL: PA-provided subdomain (e.g. `aporiavideo.pythonanywhere.com`)
- Plan: **Developer ($10/mo)** to start; upgrade to **Custom ($20–50/mo)** if CPU quota or always-on slot count constrains.

### Deployment topology — migration target (Hetzner Docker)
- Sibling Docker container `bulkvid-runner` on existing host `46.225.88.89` (port 8788)
- Same web app and worker, launched via `docker compose`
- Cloudflare tunnel `bulkvid.shinez.io` → `127.0.0.1:8788`
- Data dir `/data/bulkvid/`
- `docker/` already scaffolded; deploy when triggered.

### Migration trigger conditions
Move to Hetzner if any of these become true:
- PA CPU quota hit on a typical batch
- PA ASGI beta breaks or pricing changes unfavorably
- Need for >1 always-on task (e.g. separate scheduler / monitor)
- Need for >5GB disk for log retention

### Per-row pipeline (Image-VO tab)

```
1. Receive row payload (auth verified)
2. Parallel kickoff:
   2a. Article fetch (Tavily → ScrapingBee fallback)
   2b. Pre-upload source image to storage (also capture base64)
3. After 2b → GPT-4o visual description of the source image
4. After 3  → gpt-5.4-mini collage prompt build
5. After 4  → kie.ai nano-banana-edit collage generation (acquires kie pool slot)
6. After 5  → kie.ai recraft/crisp-upscale (acquires kie pool slot)
7. After 6  → PIL split into 4 quadrants with edge_crop_pixels (default 10)
8. After 7 parallel → optimize each + upload each to S3
9. After 2a → language detect → gpt-5.4-mini script gen (Open Comments mode A/B/C/D logic)
10. After 9 → Gemini TTS voice gen
11. After 8 and 10 → Rendi.dev "image + audio → MP4" × 4 (parallel)
12. After 11 parallel → 4 S3 uploads
13. If ZapCap=Yes → ZapCap submit + poll for each video (parallel)
14. Metadata: write to SYMPHONY_DB sheet + upload JSON to S3 /Symphony/Meta Data/
15. Write Ready Video 1-4 columns back to sheet (immediate, per-row)
```

### Per-row pipeline (4Images-VO2 tab)

```
1. Receive row payload (auth verified)
2. Parallel kickoff:
   2a. Article fetch (Tavily → ScrapingBee fallback)
   2b. For each of N user-supplied images (N = "How Many"):
       - Validate URL
       - Rendi.dev "blurred background fit" resize to target aspect ratio
3. After 2a → language detect → gpt-5.4-mini script gen
4. After 3  → Gemini TTS voice gen
5. After 2b and 4 → Rendi.dev "image + audio → MP4" × N (parallel)
6. After 5 parallel → N S3 uploads
7. If ZapCap=Yes → ZapCap submit + poll for each video (parallel)
8. Metadata: write to SYMPHONY_DB sheet + upload JSON to S3 /Symphony/Meta Data/
9. Write Ready Video 1..N columns back to sheet
```

### Models locked in

| Step | Model | Reasoning |
|---|---|---|
| Script generation | gpt-5.4-mini | Yoav directive. Fast, cheap, capable. $0.75/M in, $4.50/M out, TTFT 0.67s |
| Image description (Image-VO) | gpt-4o | Existing working choice with vision |
| Collage prompt build (Image-VO) | gpt-5.4-mini | Upgraded from gpt-4.1-mini for speed + cost |
| Open Comments mode classifier | gpt-5.4-mini | Cheap small classifier |
| Music style selector | gpt-5.4-mini | Match script gen |
| Image generation | kie.ai `google/nano-banana-edit` | **Mandatory collage method** |
| Upscale | kie.ai `recraft/crisp-upscale` | **Mandatory** |
| TTS | Vertex AI Gemini TTS (project `amit-tts`) | Existing infrastructure, multilingual |
| Video assembly | Rendi.dev (Pro tier) | FFmpeg-as-a-service, no in-container ffmpeg |
| Subtitles | ZapCap API | Existing infrastructure |
| Article fetch primary | Tavily | Existing key, full-content extraction |
| Article fetch fallback | ScrapingBee | Existing key, blocked-site recovery |

### Concurrency model
- Outer semaphore: **10 rows in flight on PythonAnywhere** (tuned for CPU-second quota); **40 on Hetzner** (no quota). Admin-tunable.
- kie.ai key pool: round-robin across all available keys with per-key cooldown
- Internal per-row: `asyncio.gather` where safe
- Sheet writes: coalesced every 5s
- aiohttp connector: `limit=200, limit_per_host=30`
- PIL work runs in a thread pool (avoid blocking the event loop) but still counts against PA CPU quota.

### Storage and naming

Primary S3 `aporia-creative`, fallback GCS `aporia-unleash`. Bucket and prefix surfaced as admin settings.

| Asset | Path |
|---|---|
| Source images | `bulkvid/sources/src-{ddmmyy}-{8-random}.png` |
| Quadrants | `bulkvid/images/{row_slug}/q{1-4}.jpg` |
| Voiceovers | `bulkvid/vo/{row_slug}/vo.mp3` |
| Videos (final) | `bulkvid/videos/{row_slug}/v{1-4}.mp4` |
| Videos with captions | `bulkvid/videos_captioned/{row_slug}/v{1-4}.mp4` |
| Metadata JSON | `Symphony/Meta Data/{video_name}.json` (match existing) |

`row_slug` = sanitized `{job_id}_{row_num}_{ts}`.

---

## 6. Alternatives rejected

### Vercel + Next.js + Node/TS
Rejected. Would require translating Python `image_ops`, prompt logic, and orchestration into TypeScript, introducing translation risk. Hetzner pattern is already battle-tested across 8+ team members. Single ops paradigm wins.

### Apps Script doing everything
Rejected. Apps Script has a 6-minute execution cap and 30-second URL fetch timeout. Cannot handle the 30s–5min image generation latency, let alone bulk batches.

### Real image-to-video models (Runway, Veo, Kling, Pika)
Rejected. Yoav directive: simple stills + VO, zero animations. Also: $0.10–$1+ per 10s clip × 800 clips per 200-row batch = $80–$800 per batch.

### In-process ffmpeg in the Docker container
Rejected. Bundling ffmpeg adds 50MB+, CPU contention with orchestrator, disk I/O on the host. Rendi.dev solves this cleanly at $25/mo Pro tier.

### Four separate image generations per row
Rejected. The mandatory collage-split method (1 generation + 1 upscale + local split into 4) is 4× cheaper AND produces stylistically-coherent panels — better for Facebook ad creative.

### Vercel for the admin panel only, Hetzner for the backend
Rejected. Adds a second deployment paradigm, second auth surface, second observability stack. Same host serves both via FastAPI.

---

## 7. Security & Safety (rule 13)

### Authentication
- Apps Script obtains Google OAuth ID token via `ScriptApp.getIdentityToken()`.
- Token sent to FastAPI as `Authorization: Bearer <jwt>`.
- FastAPI verifies signature against Google JWKS (`jose` library).
- Domain restriction: `hd` claim must equal `aporia.com`.
- Email allowlist: `email` claim must be in `BULK_TEAM_ALLOWLIST` env var.
- Admin panel: separate `ADMIN_ALLOWLIST` env var (Yoav only at v1).
- Every auth attempt logged with email + result + reason.

### API keys
- All in `.env` (loaded via env vars at container start).
- Never logged, never returned in error responses.
- Admin panel masks keys when displayed (last 4 chars only).
- Rotation procedure documented; existing keys leaked in chat have been rotated per Yoav.

### Input validation
- Article URL: HTTPS only; reject internal/private IPs (SSRF guard).
- Image URL: HTTPS only; same SSRF guard; file-type check on download.
- Aspect ratio: regex `^(\d+):(\d+)$` or `^\d+x\d+$` or `auto`; default 9:16 on invalid.
- Script Pattern / Open Comments: length-capped at 4000 chars each.
- Row count per batch: capped at 5000 (admin-tunable).
- Country / Vertical: alphanumeric + spaces, length-capped at 100.

### Rate limits (admin-tunable)
- Per-user: 10 batches/hour, 50 batches/day
- Per-batch: 5000 rows max
- Global: 80 concurrent rows in flight (hard ceiling)

### Cost kill switch (admin panel)
- Per-batch cap: refuse if estimate exceeds (default $200)
- Per-day cap: refuse new batches if exceeded (default $500)
- Monthly cap: refuse + Slack alert (default $5000)
- Manual kill switch: pause all new jobs immediately

### Audit log
- Every admin change recorded: who, what, when, before, after.
- Every batch submission: who, when, sheet_id, row_count, batch_id.
- Every external API call: which key (last 4 chars), service, response_code, cost_usd.
- Retained 90 days; queryable via admin panel.

### Failure modes (defined responses)
- Tavily down → ScrapingBee fallback → if both fail, row marked `ARTICLE_FETCH_FAILED`, no proceed.
- kie.ai 429 → mark key cooldown, retry with next key.
- kie.ai down (all keys) → row marked `IMAGE_GEN_FAILED`, no proceed.
- Gemini TTS down → row marked `TTS_FAILED`, no proceed (no silent fallback — quality matters).
- Rendi.dev down → retry 3× with backoff; mark `VIDEO_ASSEMBLY_FAILED` if exhausted.
- ZapCap down → write video without captions; flag `ZAPCAP_FAILED_KEPT_NO_CAPTIONS`.
- S3 down → fall back to GCS, set `STORAGE_FALLBACK_USED`.
- Sheet API quota → exponential backoff, coalesce writes.

---

## 8. Observability (rule 14)

### Logging
- Format: structured JSON to stdout.
- Namespace: `[bulkvid <stage>] description { values }`.
- Every log line tagged with `batch_id`, `row_num`, `user_email`.
- Log levels: DEBUG / INFO / WARN / ERROR.

### Required namespaces
| Namespace | Logged events |
|---|---|
| `[bulkvid auth]` | Every auth attempt: email, hd, allowlist match, result, reason |
| `[bulkvid job]` | Batch submit / start / complete with metrics |
| `[bulkvid row]` | Row start / per-stage timing / complete |
| `[bulkvid kie]` | Submit / poll / complete with task_id, key suffix, cost |
| `[bulkvid openai]` | Call / response / token counts / cost |
| `[bulkvid tts]` | Generation / voice / duration / cost |
| `[bulkvid rendi]` | Command_id / status / vCPU / cost |
| `[bulkvid zapcap]` | Task_id / status / cost |
| `[bulkvid storage]` | Upload / destination / size / URL |
| `[bulkvid sheet]` | Read / write / cells / values |
| `[bulkvid admin]` | Settings change / who / before / after |

### Endpoints
- `GET /health` — liveness (returns 200 if process up)
- `GET /health/deep` — vendor connectivity check (mirrors `cb_health_check.py`)
- `GET /metrics` — Prometheus format, per-batch and per-row counters
- `GET /jobs` — list jobs with status
- `GET /jobs/{id}?tail=N` — job status + last N log lines

### Cost tracking
- Every adapter call returns `(result, cost_usd)`.
- Per-row cost summed and logged.
- Per-batch cost summed and written to job record + metadata JSON.
- Admin panel shows live cumulative cost + projected total.

---

## 9. Settings / Admin Panel (rule 15)

Single source of truth: SQLite `settings.db` overrides `.env` defaults. Hot-reloaded; no restart needed.

### Settings categories

**Models** (each with hot-swap + per-row override capability):
- Script gen: model, temperature, max_tokens, system prompt
- Image description: model, prompt template
- Collage prompt build: model, temperature, prompt template
- Open Comments mode classifier: model
- Music style selector: model

**Image generation**:
- kie.ai model name (`google/nano-banana-edit`, `nano-banana-2`, `nano-banana-pro`, `midjourney`, `grok-imagine/text-to-image`)
- Default aspect ratio
- Edge crop pixels (default 10)
- Min dimensions for upscale (default 600×600)
- Max output size bytes (default 2,097,152)
- Upscale on/off

**TTS**:
- Voice pool per language (he-IL, en-US, ar, fr, es, de, ...)
- Default voice per language
- Style direction prompt template
- Output format (WAV vs MP3)

**Video assembly (Rendi)**:
- vCPU per command (default 4, max 32 on Pro)
- Max command run seconds (default 300)
- Resize filter chain
- Crop strategy (blurred-bg-fit / center-crop / letterbox)

**ZapCap**:
- Template ID (default `46d20d67-255c-4c6a-b971-31fddcfea7f0`)
- Font size, weight, color, stroke
- Position (top/center/low, percent)
- Emoji on/off, emphasis on/off
- Language overrides

**Article fetch**:
- Tavily timeout (default 15s)
- ScrapingBee timeout (default 30s)
- Max content length (default 50,000 chars)
- Retry count (default 2)
- In-process cache TTL (default 1 hour)

**Cost guards**:
- Per-row cap ($, soft warn at submit)
- Per-batch cap ($, hard refuse at submit)
- Per-day cap ($, hard refuse new batches)
- Monthly cap ($, kill switch + alert)
- Manual kill switch (global pause)

**Per-team controls**:
- Bulk team email allowlist
- Per-user rate limits (batches/hour, batches/day)
- Sheet ID allowlist

**Feature flags** (mirroring existing `KIE_CB_*` pattern):
- `BULKVID_INTERNAL_PARALLEL` — per-row asyncio.gather (default ON)
- `BULKVID_ARTICLE_CACHE` — in-process article cache (default ON)
- `BULKVID_LANGUAGE_CACHE` — language detection cache (default ON)
- `BULKVID_SHEET_BATCH_WRITES` — coalesce sheet writes 5s (default ON)
- `BULKVID_KIE_KEY_POOL` — round-robin keys (default ON)
- `BULKVID_FAST_ZAPCAP_SUBMIT` — drop 10s throttle to 1s (default OFF, opt-in)

**Observability**:
- Log level
- Sentry DSN
- Per-row JSON metadata upload (on/off)

**Audit log view**:
- Last 200 admin changes (searchable by user/date)
- Last 200 batch submissions
- Last 200 errors

---

## 10. Testing (rule 18)

### Unit tests (~60% of test surface)
- `image_ops`:
  - `split_collage_2x2` for square, wide, tall sources
  - Various `edge_crop_pixels` values (0, 10, 50, edge cases)
  - `crop_to_ratio` upscale and downcrop
  - `optimize_image_for_size` at 1.99MB, 2.00MB, 2.01MB boundaries
- Prompt construction:
  - Open Comments mode A (tone bias) — token present in output
  - Mode B (content directive) — required tokens present
  - Mode C (full override) — script equals Open Comments
  - Mode D (mixed) — classifier picks correctly
- Language detection: 10+ languages with short and long samples
- Aspect ratio parsing: `9:16`, `09:16` (Sheets time-cast), `1.91:1`, `1080x1080`, `auto`, garbage
- Cost computation: every adapter

### Integration tests (~30%)
- kie.ai adapter (recorded fixtures via `vcrpy`)
- Gemini TTS adapter
- Rendi.dev adapter
- ZapCap adapter
- Tavily + ScrapingBee adapters
- S3 + GCS upload adapters
- Sheet read/write adapter

### E2E tests (~10%)
- 1 row Image-VO end-to-end against staging APIs
- 1 row 4Images-VO2 end-to-end
- 1 row with ZapCap
- 1 row with auth failure (expect 403)
- 1 small batch of 5 rows (concurrency smoke)

### Performance regression test
- 200-row batch timing baseline locked in CI
- Alert if next batch exceeds baseline by >20%

---

## 11. Cost model (rule 8 — verified live 2026-06-02, refresh before each major release)

### Per-row estimate (Image-VO, VO=Yes, ZapCap=No)
| Item | Unit cost | Per row |
|---|---|---|
| kie.ai nano-banana-edit (1 collage) | ~$0.04 | $0.040 |
| kie.ai recraft/crisp-upscale | ~$0.04 | $0.040 |
| Tavily article fetch | $0.008 | $0.008 |
| gpt-4o image description (~2K tokens out) | $5/M out | $0.010 |
| gpt-5.4-mini collage prompt (~2K tokens out) | $4.50/M out | $0.009 |
| gpt-5.4-mini script gen (~1K tokens out) | $4.50/M out | $0.005 |
| Gemini TTS (~10s audio) | TBD verify | ~$0.003 |
| Rendi.dev × 4 videos (10s each) | TBD verify | ~$0.040 |
| S3 storage + bandwidth | ~$0.001 | $0.001 |
| **Per-row total** | | **~$0.16** |

### Batch estimates
- 200 rows ≈ **$30–40**
- 1,000 rows ≈ **$150–200**
- With ZapCap=Yes: +$0.10/row (verify ZapCap pricing)

### Fixed monthly costs (PythonAnywhere primary)
- PythonAnywhere Developer: **$10/mo** (5,000 CPU sec/day, 1 always-on task, 5GB)
  - Upgrade to Custom **$20–50/mo** if quota gets tight
- Rendi.dev Pro: **$25/mo**
- Sentry: free tier
- AWS S3: existing
- GCS: existing
- **Total fixed: ~$35–75/mo**

### Fixed monthly costs (Hetzner migration target, for reference)
- Hetzner host: $0 incremental (shared with `cb-remote-runner`)
- Rendi.dev Pro: $25/mo
- **Total fixed: $25/mo**

### Cost alerts (admin-tunable)
- Per-batch >$50 estimated → warn before start
- Per-day >$300 actual → pause new batches
- Monthly >$3000 → kill switch + Slack alert

---

## 12. Performance targets

### PythonAnywhere (10 concurrent, CPU-quota-aware)

| Workload | Target | Worst case |
|---|---|---|
| 50 rows Image-VO no ZapCap | <12 min | <20 min |
| 200 rows Image-VO no ZapCap | <30 min | <50 min |
| 1000 rows Image-VO no ZapCap | <2.5 hours | <4 hours (may need Custom plan) |
| 200 rows Image-VO + ZapCap | <45 min | <70 min |
| 200 rows 4Images-VO2 | <15 min | <25 min |

### Hetzner (40 concurrent, no quota — for after migration)

| Workload | Target | Worst case |
|---|---|---|
| 50 rows Image-VO no ZapCap | <6 min | <12 min |
| 200 rows Image-VO no ZapCap | <18 min | <30 min |
| 1000 rows Image-VO no ZapCap | <70 min | <2 hours |
| 200 rows Image-VO + ZapCap | <30 min | <45 min |
| 200 rows 4Images-VO2 | <8 min | <15 min |

---

## 13. Rollout phases

| Phase | Description | Estimate |
|---|---|---|
| 0 | Project scaffolding (pyproject, structure, .env.example, README, gitignore, basic FastAPI hello) | 1-2 hours |
| 1 | Adapters (kie/openai/gemini_tts/rendi/zapcap/tavily/scrapingbee/s3/gcs) with unit tests | 1-2 days |
| 2 | `image_ops.py` lift from refs with full unit tests for collage method | 4-6 hours |
| 3 | Pipeline + orchestrator (per-row state machines, semaphore queue, sheet I/O) | 2-3 days |
| 4 | Auth + FastAPI routes (`/jobs`, `/health`, `/metrics`) | 1 day |
| 5 | Admin panel (HTMX + Tailwind, settings CRUD, audit log, live dashboard) | 2-3 days |
| 6 | Apps Script + sheet integration (menu, sidebar) | 4-6 hours |
| 7 | Deploy to PythonAnywhere (WSGI/ASGI config, always-on task setup, 50/200-row staging tests) | 1 day |
| 8 | Extreme QA pass per rule 6 (golden path, edge cases, error paths, regressions) | 1 day |

**Total: 10–14 working days. Calendar: 2–3 weeks if uninterrupted.**

---

## 13a. Current status (end of 2026-06-02)

Phases 0–7 complete + Phase 5 minimal admin panel. **395 tests passing.**

### What's been built and verified locally

- ✅ Web app (`bulkvid.main:app`) boots, all routes mounted: `/health`, `/jobs`, `/health/deep`, `/admin/`, `/admin/settings`
- ✅ Worker (`bulkvid.worker`) boots and drains the queue
- ✅ End-to-end pipeline run for one row: article → vision → script → kie.ai collage → upscale → 4 GCS uploads → **TTS produced 19.6s of audio** → audio uploaded to GCS → ❌ blocked at Rendi.dev (vendor-side issue, see below)
- ✅ GCS bucket `aporia-unleash` accepts writes from `videocreator@uplift-283910.iam.gserviceaccount.com`
- ✅ Vertex AI Gemini TTS works from `geminiapi@amit-tts.iam.gserviceaccount.com` after applying cloud-platform OAuth scope (`adapters/gemini_tts.py` line ~165)
- ✅ Settings store + admin CRUD work (yoav / tenta20)
- ✅ Apps Script ready (`apps_script/`) — needs ngrok or deploy before it can be tested

### Open vendor issues (NOT code issues)

- **Tavily account disabled** (HTTP 402 "Your account is currently disabled. This is likely due to unpaid pay-as-you-go balance"). ScrapingBee fallback handles this. Either top up Tavily or remove `TAVILY_API_KEY` from `.env`.
- **Rendi.dev storage quota exhausted** (HTTP 403 "Account has passed its' storage quota"). Either delete files at https://app.rendi.dev/ or upgrade to Pro ($25/mo, https://app.rendi.dev/plans). Blocks video assembly step.

### Local dev quirks

- **Port 8000, not 8788** on Yoav's main machine — Hyper-V reserved 8788. May or may not apply to other machines.
- `BULKVID_DEV_AUTH_BYPASS_EMAIL=yoav@aporianetworks.com` is set in `.env` — local `POST /jobs` accepts requests with no Authorization header. Loud warning on every request. **Must be cleared before any deploy.**
- Sheets write-back is noop locally (no `SHEETS_SERVICE_ACCOUNT_FILE`) — worker still drains queue, results stay in SQLite only

### Architectural decisions worth knowing

- **Two separate Google service accounts**: storage (`videocreator@uplift-283910`) ≠ TTS (`geminiapi@amit-tts`). Different projects. Inline env vars: `GOOGLE_*` for storage, `VERTEX_AI_*` for TTS. Helper at `adapters/google_credentials.py`.
- **Storage flipped to GCS primary**, S3 fallback. Was originally S3 primary; Yoav corrected ("we store in google cloud").
- **AtlasCloud is wired as fallback for kie.ai image generation** (`pipeline/image_gen.py`). Activates automatically when `ATLAS_API_KEY` is set and kie.ai fails.
- **OpenAI `max_tokens` → `max_completion_tokens`** on the wire (gpt-5.x rejects the old name). Python kwarg is still `max_tokens` for caller convenience.

### Most recent test run outcome

Single row, BBC tech article + Unsplash photo, ~$0.10 spent, ~2 minutes elapsed. All stages worked except Rendi (vendor quota). When Rendi is unblocked, expect ~$0.15-0.20 per row and `row_done` with 4 video URLs in `aporia-unleash/bulkvid/videos/...`.

---

## 14. Open Questions

1. **PythonAnywhere account** — does Aporia have an existing account, or do we create one? Username / login.
2. **PythonAnywhere plan** — start on Developer ($10/mo)? Or pre-emptively Custom?
3. **AWS S3 credentials** for `aporia-creative` bucket — needed before deploy.
4. **Rotated API keys** — kie.ai, OpenAI, Vertex AI service account, Tavily, ZapCap, Rendi — all flagged for rotation; need fresh values in `.env` at deploy time.
5. **Bulk team email allowlist** — final list of email addresses.
6. **Bulk team Sheet ID(s)** — which spreadsheet(s) the tool reads from.
7. **Music URL source** — if music mixing is wanted (Symphony Music_Url pattern), where do the tracks live?
8. **ZapCap template ID** — bulk team brand style. Default to existing `46d20d67-255c-4c6a-b971-31fddcfea7f0` until told otherwise.
9. **kie.ai key pool** — confirm Yoav has access to all 6+ existing keys or just one.
10. **Article URL allowlist?** — should we restrict which domains article fetch can hit (security)?
11. **Migration trigger** — who decides "PA isn't cutting it, move to Hetzner"? Yoav, Karmel, Alex?
12. **Hetzner SSH access** — needed when migration is triggered, not before. Karmel said Alex handles the transfer.

---

## 15. Appendix

### A. Sheet column maps

**Image-VO tab** (matching `video-pj` spreadsheet):
| Col | Field | Type | Notes |
|---|---|---|---|
| A | Country | text | e.g. `IL`, `US` |
| B | Vertical | text | campaign vertical |
| C | Article | URL | scraped for VO script |
| D | Manual Image | URL | seed for collage |
| E | Voice Over | Yes/No | default Yes |
| F | ZapCap | Yes/No | default No on this tab |
| G | Change Size | aspect ratio | default 9:16; beware Sheets time-cast |
| H | Script Pattern | text | e.g. "How To" |
| I | Open Comments | text | **highest priority signal** |
| J–M | Ready Video 1–4 | URL (output) | written back |

**4Images-VO2 tab**:
| Col | Field | Type | Notes |
|---|---|---|---|
| A | Country | text | |
| B | Vertical | text | |
| C | Article | URL | |
| D | How Many | 1–4 | how many of the 4 supplied images to use |
| E | Voice Over | Yes/No | default Yes |
| F–I | Image 1–4 | URL | user-supplied |
| J | ZapCap | Yes/No | |
| K | Change Size | aspect ratio | |
| L | Script Pattern | text | |
| M | Open Comments | text | **highest priority** |
| N–Q | Ready Video 1–4 | URL (output) | |

### B. Open Comments mode classifier
gpt-5.4-mini call classifies Open Comments text into one of:
- `tone`: stylistic bias only ("urgent", "casual", "luxury")
- `directive`: specific content requirements ("mention $9.99", "CTA = Learn More")
- `override`: full script provided in the cell
- `mixed`: combination

Mode determines how the script generator weights it.

### C. Rendi.dev ffmpeg templates (initial set)

**Resize (blurred background fit) — aspect-aware:**
```
-i {{in_1}}
-filter_complex "[0:v]split=2[bg][fg];
[bg]scale=W:H:force_original_aspect_ratio=increase,crop=W:H,boxblur=30:5[bg2];
[fg]scale=W:H:force_original_aspect_ratio=decrease[fg2];
[bg2][fg2]overlay=(W-w)/2:(H-h)/2"
{{out_1}}
```
W and H derived from aspect ratio + target dimensions at job assembly time. Target dimensions are admin-tunable per ratio (default 1080×1920 for 9:16, 1080×1080 for 1:1, 1920×1080 for 16:9).

**Stills → video (image + VO):**
```
-loop 1 -framerate 30 -i {{in_1}} -i {{in_2}}
-c:v libx264 -tune stillimage -pix_fmt yuv420p
-c:a aac -b:a 192k -shortest {{out_1}}
```

**Music mix (existing recipe from stage_5):**
```
-i {{in_1}} -i {{in_2}}
-filter_complex "[1:a]volume=0.3[music];[0:a][music]amix=inputs=2:duration=shortest[mixed]"
-map 0:v -map "[mixed]" -c:v copy -c:a aac -shortest {{out_1}}
```

### D. Existing code references (lift / port from)
| File | What we use |
|---|---|
| `refs/creative_builder_dev/core/image/image_ops.py` | `split_collage_2x2`, `split_collage_with_processing`, `upscale_collage_with_api`, `optimize_image_for_size`, `crop_to_ratio` (lift verbatim) |
| `refs/voiceover.py` | Gemini TTS wrapper pattern (rewrite using current SDK) |
| `refs/CBImageNoText (1).py` | Overall image gen flow (study, don't lift wholesale) |
| `refs/stage_5_add_music (3).py` | Rendi.dev wrapper, music mix recipe, S3Uploader class |
| `refs/stage_6_zapcap_processing.py` | ZapCap flow, S3Uploader, metadata pattern, write-on-completion |
| `refs/creative_builder_dev/cb_health_check.py` | `/health/deep` pattern |
| `refs/creative_builder_dev/core/orchestrator/runner.py` | Semaphore-based concurrency, batch-then-wait avoidance pattern |
| `refs/creative_builder_dev/remote/docker-compose.hetzner.yml` | Docker compose pattern (port mapping, volumes, env) |

### E. Repository structure
```
turbovid-new/
├── _plans/
│   └── 2026-06-02-aporia-bulk-video-tool.md   ← this file
├── refs/                                       ← read-only reference scripts
├── src/
│   └── bulkvid/
│       ├── __init__.py
│       ├── main.py                             ← FastAPI app entry
│       ├── config.py                           ← env + settings loader
│       ├── auth.py                             ← Google OAuth ID token verify
│       ├── logging.py                          ← namespaced logger
│       ├── adapters/                           ← one file per external service
│       │   ├── kie.py
│       │   ├── openai_client.py
│       │   ├── gemini_tts.py
│       │   ├── rendi.py
│       │   ├── zapcap.py
│       │   ├── tavily.py
│       │   ├── scrapingbee.py
│       │   ├── gcs.py
│       │   └── s3.py
│       ├── pipeline/
│       │   ├── article_fetch.py
│       │   ├── language.py
│       │   ├── script_gen.py
│       │   ├── open_comments.py
│       │   ├── image_gen.py
│       │   ├── image_resize.py
│       │   ├── tts.py
│       │   ├── video_assembly.py
│       │   ├── zapcap_processing.py
│       │   ├── storage.py
│       │   └── metadata.py
│       ├── orchestrator/
│       │   ├── row_processor.py                ← per-row state machine
│       │   ├── batch_runner.py                 ← batch coordinator
│       │   ├── queue.py                        ← SQLite job queue
│       │   ├── concurrency.py                  ← semaphores + kie pool
│       │   └── sheet_writer.py                 ← coalesced writes
│       ├── image_ops.py                        ← lifted + adapted from refs
│       ├── models/
│       │   ├── job.py
│       │   ├── row.py
│       │   └── settings.py
│       ├── routes/
│       │   ├── jobs.py
│       │   ├── admin.py
│       │   └── health.py
│       └── admin/
│           ├── templates/                      ← HTMX templates
│           └── static/                         ← Tailwind
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
├── apps_script/
│   ├── Code.gs                                 ← Sheet menu + auth + job submit
│   └── README.md
├── docker/
│   ├── Dockerfile
│   └── docker-compose.hetzner.yml
├── deploy/
│   └── README.md                               ← Hetzner deploy runbook
├── .env.example
├── .gitignore
├── pyproject.toml
├── README.md
└── LICENSE
```

### F. Deployment topology
- Host: `46.225.88.89` (existing Hetzner)
- Service: `bulkvid-runner` (Docker Compose, in `docker/docker-compose.hetzner.yml`)
- Internal: `http://127.0.0.1:8788`
- Public direct: `http://46.225.88.89:8788`
- Cloudflare tunnel: `https://bulkvid.shinez.io`
- Data: `/data/bulkvid/` (separate from `cb-remote-runner`'s `/data/`)
- Logs: `docker compose logs -f bulkvid-runner` + per-job at `/data/bulkvid/logs/<job_id>.log`
