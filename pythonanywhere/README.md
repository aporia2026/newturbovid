# Deploy — TurboVid on PythonAnywhere

Primary deploy target per management directive (2026-06-02). Hetzner remains the documented migration target if PA proves insufficient (see [../deploy/README.md](../deploy/README.md)).

Plan: [_plans/2026-06-02-aporia-bulk-video-tool.md](../_plans/2026-06-02-aporia-bulk-video-tool.md) §5 + Phase 7.

## Prerequisites

- PythonAnywhere account (Developer plan or higher — Beginner won't work, no always-on tasks)
- Rotated API keys (kie.ai, OpenAI, Vertex AI service account JSON, Tavily, ZapCap, Rendi)
- AWS S3 credentials for `aporia-creative`
- Bulk team email allowlist finalized
- Yoav's email registered as admin

## First-time setup

### 1. Push the repo to PA

```bash
# In a PA Bash console
cd ~
git clone https://github.com/aporia2026/newturbovid.git bulkvid
cd bulkvid
```

### 2. Create the virtualenv

```bash
mkvirtualenv --python=python3.12 bulkvid
pip install -e .
```

### 3. Configure secrets

```bash
cp .env.example .env
nano .env   # paste real values; NEVER commit
```

Also upload the Vertex AI service account JSON to `~/bulkvid/secrets/amit-tts.json` and set:
```
GOOGLE_APPLICATION_CREDENTIALS=/home/<USERNAME>/bulkvid/secrets/amit-tts.json
```

Set strict file permissions:
```bash
chmod 600 ~/bulkvid/.env ~/bulkvid/secrets/*.json
```

### 4. Configure the ASGI web app

PythonAnywhere ASGI is **beta** (their words). Use their `pa` CLI:

```bash
pa website create --domain <USERNAME>.pythonanywhere.com \
                  --command 'cd ~/bulkvid && /home/<USERNAME>/.virtualenvs/bulkvid/bin/uvicorn bulkvid.main:app --host 127.0.0.1 --port 8000'
```

Reload after every code change:
```bash
pa website reload --domain <USERNAME>.pythonanywhere.com
```

### 5. Set up the always-on task (the worker)

Web tab → Always-on tasks → Add task:

```
Command: /home/<USERNAME>/.virtualenvs/bulkvid/bin/python -m bulkvid.worker
Description: bulkvid worker — drains job queue
```

The worker is the long-running orchestrator. It reads jobs from `~/bulkvid/data/jobs.db` and processes them concurrently.

### 6. Verify

```bash
# Web app
curl https://<USERNAME>.pythonanywhere.com/health

# Worker (check it's running)
# Web tab → Always-on tasks → status should be "running"

# Logs (web app)
tail -f /var/log/<USERNAME>.pythonanywhere.com.error.log

# Logs (worker)
tail -f /var/log/<USERNAME>.pythonanywhere.com.always_on_task_<ID>.log

# Job-level logs (our own)
tail -f ~/bulkvid/data/logs/<job_id>.log
```

## Updates (after a git pull)

```bash
ssh <USERNAME>@ssh.pythonanywhere.com   # or use a PA Bash console
cd ~/bulkvid
git pull origin main
workon bulkvid
pip install -e .     # only if pyproject.toml changed
pa website reload --domain <USERNAME>.pythonanywhere.com
# Always-on task picks up changes on its next restart cycle; force restart from the Web tab if urgent
```

## CPU quota monitoring

Developer plan: **5,000 CPU seconds / day**.

Empirical cost per row (verified in plan §11):
- ~2–4 CPU seconds per row (PIL image processing + orchestration overhead)
- 200 rows ≈ 400–800 CPU seconds (8–16% of daily quota)
- 1,000 rows ≈ 2,000–4,000 CPU seconds (40–80% of daily quota)

Monitor at: `https://www.pythonanywhere.com/user/<USERNAME>/account/`

If approaching the cap consistently:
- Upgrade to Custom plan ($20–50/mo) for up to 100,000 CPU sec/day
- Or trigger Hetzner migration (see [../deploy/README.md](../deploy/README.md))

## Disk quota

Developer plan: **5 GB**.

Project + virtualenv: ~500MB.
SQLite DB: grows with job history (~100KB per 1,000 jobs).
Per-job logs: rotated; cap at ~1GB.

## Rollback

```bash
cd ~/bulkvid
git log --oneline -n 5
git checkout <known-good-commit>
pa website reload --domain <USERNAME>.pythonanywhere.com
# Always-on task: stop + start from Web tab
```

## When to trigger Hetzner migration

Per plan §5, migrate if any of these become true:
- PA CPU quota hit on a typical batch
- PA ASGI beta breaks or pricing changes unfavorably
- Need for >1 always-on task (e.g. separate scheduler / monitor)
- Need for >5GB disk for log retention

Same code runs on Hetzner with zero changes — only the launcher and concurrency env vars change. See [../deploy/README.md](../deploy/README.md) for the migration runbook.
