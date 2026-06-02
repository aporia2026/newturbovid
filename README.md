# TurboVid — Aporia Bulk Video Tool

Simple bulk video creation for Facebook campaigns. Resized image + voiceover, optional ZapCap subtitles. Up to 4 videos per row, 200+ rows per batch in 12–20 minutes.

**Status:** Phase 0 (scaffolding) — see [_plans/2026-06-02-aporia-bulk-video-tool.md](_plans/2026-06-02-aporia-bulk-video-tool.md) for the full plan.

---

## What it is

The bulk team uses a Google Sheet to describe campaigns. Each row gets up to 4 finished videos written back. Two input modes:

- **Image-VO**: the user supplies one seed image. The tool generates a 2×2 collage via kie.ai `nano-banana-edit`, upscales it via `recraft/crisp-upscale`, splits it into 4 quadrants, and makes 4 videos.
- **4Images-VO2**: the user supplies up to 4 images and a "How Many" count. The tool resizes them to the target aspect ratio and makes that many videos.

Every video = resized image + voiceover, optionally with burned-in subtitles. **No animations, no motion effects.**

## Stack

- **Backend**: Python 3.12, FastAPI, async via `asyncio` + `aiohttp`
- **Image gen**: kie.ai (nano-banana-edit + recraft/crisp-upscale)
- **Script & vision**: OpenAI gpt-5.4-mini (script, prompt building, classifier) + gpt-4o (image description)
- **TTS**: Vertex AI Gemini TTS (project `amit-tts`)
- **Video assembly**: Rendi.dev (FFmpeg-as-a-service)
- **Subtitles**: ZapCap
- **Article fetch**: Tavily (primary) + ScrapingBee (fallback)
- **Storage**: AWS S3 `aporia-creative` primary, GCS `aporia-unleash` fallback
- **Deploy (primary)**: PythonAnywhere — Developer plan, ASGI web app + 1 always-on task
- **Deploy (migration target)**: Docker on Hetzner (sibling container next to `cb-remote-runner`)
- **Admin panel**: HTMX + Tailwind, same FastAPI service

## Setup (local dev)

```powershell
# 1. Clone
git clone https://github.com/aporia2026/newturbovid c:\Projects\turbovid-new
cd c:\Projects\turbovid-new

# 2. Create virtualenv
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Install deps
pip install -e ".[dev]"

# 4. Configure
cp .env.example .env
# fill in keys (see plan §14 Open Questions for what's still pending)

# 5. Run tests
pytest

# 6a. Run the web app (terminal 1)
python -m uvicorn bulkvid.main:app --reload --port 8788

# 6b. Run the worker (terminal 2)
python -m bulkvid.worker

# 7. Try it
curl http://localhost:8788/health
```

## Deploy

- **Primary (PythonAnywhere)** — see [pythonanywhere/README.md](pythonanywhere/README.md)
- **Migration target (Hetzner Docker)** — see [deploy/README.md](deploy/README.md)

Both run the **same code** from `src/bulkvid/`. The only difference is the launcher (PA always-on task vs Docker `docker-compose`) and the concurrency env var (`BULKVID_MAX_CONCURRENT_ROWS=10` on PA, `40` on Hetzner).

## Project layout

```
turbovid-new/
├── _plans/                    Approved plans (rule 7)
├── refs/                      Read-only reference scripts from existing Aporia tools
├── src/bulkvid/
│   ├── main.py                FastAPI entry (web app)
│   ├── worker.py              Always-on task entry (orchestrator loop)
│   ├── config.py              Settings loader (env → pydantic Settings)
│   ├── auth.py                Google OAuth ID token verification
│   ├── adapters/              One file per external service
│   ├── pipeline/              Per-stage logic (article, script, image, TTS, video, zapcap)
│   ├── orchestrator/          Queue, concurrency, row state machine
│   ├── image_ops.py           Lifted from refs (mandatory collage method)
│   ├── routes/                FastAPI routes
│   └── admin/                 HTMX admin panel
├── tests/                     unit / integration / e2e
├── apps_script/               Google Sheet Apps Script (custom menu + sidebar)
├── pythonanywhere/            PythonAnywhere deploy runbook + WSGI fallback (primary)
├── docker/                    Dockerfile + docker-compose.hetzner.yml (migration target)
└── deploy/                    Hetzner deploy runbook
```

## Standing rules

This project follows the standing rules in `C:\Users\Yoav\.claude\CLAUDE.md`. The most load-bearing for code reviewers:

- **Rule 1 (Verify, don't guess)** — Context7 + live web check before recommending any library API
- **Rule 2 (Order)** — Code style must match existing patterns in the file; no drive-by edits
- **Rule 12 (Brutal honesty)** — No softening hard messages; flag flaws first
- **Rule 13 (Security from day one)** — Validate at boundaries, never trust the client, no secrets in code
- **Rule 14 (Observability from day one)** — Namespaced logs at every stage; values, not "X happened"
- **Rule 17 (No model bias)** — Pick models on cost+capability+fit, not on vendor

## Where to look

- **The plan**: [_plans/2026-06-02-aporia-bulk-video-tool.md](_plans/2026-06-02-aporia-bulk-video-tool.md) — goals, architecture, security, observability, testing, costs, rollout phases
- **The mandatory collage method**: [_plans/2026-06-02-aporia-bulk-video-tool.md §5 Per-row pipeline (Image-VO)](_plans/2026-06-02-aporia-bulk-video-tool.md)
- **Existing reference scripts** to study: [refs/](refs/)
- **Env template**: [.env.example](.env.example)
