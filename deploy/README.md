# Deploy — TurboVid on Hetzner (Migration Target)

**Primary deploy target is PythonAnywhere** — see [../pythonanywhere/README.md](../pythonanywhere/README.md).

This document covers Hetzner as the **migration target**: same code, different deploy. Use when one of the migration trigger conditions from plan §5 becomes true (CPU quota hit, ASGI beta breaks, need for >1 always-on task, disk pressure).

Sibling deployment to `cb-remote-runner`. Same host (`46.225.88.89`), different port (8788), different volume (`bulkvid_runner_data`).

Plan: [_plans/2026-06-02-aporia-bulk-video-tool.md](../_plans/2026-06-02-aporia-bulk-video-tool.md) §5 + Phase 7.

## Prerequisites (gating items, Phase 7)

- Hetzner SSH access (`ssh aporia` from Yoav's laptop)
- Rotated API keys in `/root/aporia/turbovid-new/.env` on the host
- AWS S3 credentials for `aporia-creative` bucket
- Bulk team email allowlist finalized
- Cloudflare tunnel hostname created (proposed `bulkvid.shinez.io`)

## First-time setup on Hetzner

```bash
ssh aporia
cd /root/aporia

# Pull the repo
git clone https://github.com/aporia2026/newturbovid.git turbovid-new
cd turbovid-new

# Configure secrets (NEVER commit this)
cp .env.example .env
$EDITOR .env

# Build + start
docker compose -f docker/docker-compose.hetzner.yml up -d --build

# Verify
docker compose -f docker/docker-compose.hetzner.yml ps
curl -sS http://127.0.0.1:8788/health
```

## Update (after a git pull)

```bash
ssh aporia
cd /root/aporia/turbovid-new
git pull origin main
docker compose -f docker/docker-compose.hetzner.yml up -d --build
```

## Operations

```bash
# Restart
docker compose -f docker/docker-compose.hetzner.yml restart bulkvid-runner

# Stop / start
docker compose -f docker/docker-compose.hetzner.yml stop bulkvid-runner
docker compose -f docker/docker-compose.hetzner.yml start bulkvid-runner

# Logs (tail)
docker compose -f docker/docker-compose.hetzner.yml logs -f --tail=200 bulkvid-runner

# Per-job logs (inside the container volume)
ls /var/lib/docker/volumes/bulkvid_runner_data/_data/logs/
```

## Rollback

```bash
ssh aporia
cd /root/aporia/turbovid-new
git log --oneline -n 5
git checkout <known-good-commit>
docker compose -f docker/docker-compose.hetzner.yml up -d --build
```

## Health checks

```bash
# Liveness (process is up)
curl -sS http://127.0.0.1:8788/health

# Deep (vendor connectivity — Phase 1)
curl -sS -H "Authorization: Bearer <admin-jwt>" http://127.0.0.1:8788/health/deep
```
