# Migrate TurboVid backend to HuggingFace Spaces + Turso

Step-by-step companion to [_plans/2026-06-04-migrate-to-hf-spaces-turso.md](../_plans/2026-06-04-migrate-to-hf-spaces-turso.md).

Goal: stop paying the PythonAnywhere uWSGI flakiness tax. New target is
$0/mo, permanent, no company purchasing approval.

Total time: **half a day end-to-end**.

---

## 0 — Prerequisites

- A HuggingFace account (free signup, GitHub OAuth fine).
- A Turso account (free signup, GitHub OAuth fine, no credit card).
- The current PA `.env` file, opened locally — you'll paste secrets out of
  it into HF Space Secrets.
- Apps Script editor access to the sheet that owns `Code.gs`.

---

## 1 — Provision Turso (~10 min)

### 1a. Install the CLI (optional but recommended)

```bash
# macOS
brew install tursodatabase/tap/turso

# Linux
curl -sSfL https://get.tur.so/install.sh | bash
```

Or do the whole thing in the web dashboard at https://turso.tech — same
end state.

### 1b. Sign in

```bash
turso auth signup    # opens browser, GitHub OAuth, no CC
```

### 1c. Create the two databases (EU-West)

```bash
turso db create aporia-bulkvid-jobs --location fra
turso db create aporia-bulkvid-settings --location fra
```

`fra` = Frankfurt (EU-West). If your team is mostly US, swap for `iad`
(Virginia) or whatever's closest.

### 1d. Grab the URLs + tokens

```bash
turso db show aporia-bulkvid-jobs --url
turso db tokens create aporia-bulkvid-jobs

turso db show aporia-bulkvid-settings --url
turso db tokens create aporia-bulkvid-settings
```

Save all four values — you'll paste them into HF Secrets in step 3d.

The URLs look like `libsql://aporia-bulkvid-jobs-<youruser>.aws-eu-west-1.turso.io`.
The tokens are long JWT strings.

---

## 2 — Create the HuggingFace Space (~5 min)

1. Go to https://huggingface.co/spaces → **+ New Space**.
2. **Owner**: your user or an org (whichever you want to own the deploy).
3. **Space name**: `aporia-bulkvid` (matches the URL —
   `https://<owner>-aporia-bulkvid.hf.space`).
4. **License**: leave default.
5. **Space SDK**: **Docker** → **Blank**.
6. **Hardware**: **CPU basic - 2 vCPU - 16 GB - FREE**.
7. **Visibility**: **Public**.
   - The Space URL is world-reachable but every `/jobs*` route is gated by
     our existing Google OAuth + email allowlist. Public ≠ insecure.
   - The repo code is also visible — never commit secrets. We use HF
     Secrets for that.
8. Click **Create Space**. It boots with a placeholder; we'll push the
   real code in step 4.

---

## 3 — Add Space Secrets (~15 min)

In the new Space: **Settings → Variables and secrets → New secret**.

Add each of the following. The first two blocks are the new Turso vars
(paste from step 1d). The rest are copy-paste from your current PA `.env`
— **don't paraphrase, paste verbatim**.

### 3a. Turso (new)

| Secret name | Value |
|---|---|
| `BULKVID_DB_URL` | `libsql://aporia-bulkvid-jobs-<owner>.aws-eu-west-1.turso.io` |
| `BULKVID_DB_AUTH_TOKEN` | the long JWT from `turso db tokens create aporia-bulkvid-jobs` |
| `BULKVID_SETTINGS_DB_URL` | `libsql://aporia-bulkvid-settings-<owner>.aws-eu-west-1.turso.io` |
| `BULKVID_SETTINGS_DB_AUTH_TOKEN` | the JWT from `turso db tokens create aporia-bulkvid-settings` |

### 3b. Service config

| Secret name | Value |
|---|---|
| `BULKVID_ENV` | `prod` |
| `BULKVID_LOG_LEVEL` | `INFO` |
| `BULKVID_DATA_DIR` | `/tmp/data` (the Dockerfile already sets this — only set as a Space env if you want to override) |

### 3c. Auth

| Secret name | Value |
|---|---|
| `BULK_TEAM_ALLOWLIST` | comma-separated list, same as PA `.env` |
| `BULK_TEAM_DOMAINS` | comma-separated list, same as PA `.env` |
| `ADMIN_ALLOWLIST` | comma-separated, same as PA `.env` |
| `ALLOWED_HD` | e.g. `aporianetworks.com` |
| `ADMIN_PANEL_USERNAME` | same as PA |
| `ADMIN_PANEL_PASSWORD` | same as PA |

Do **not** set `BULKVID_DEV_AUTH_BYPASS_EMAIL` in production. Leave it
unset.

### 3d. Vendor API keys (copy verbatim from PA `.env`)

| Secret name |
|---|
| `OPENAI_API_KEY` |
| `KIE_AI_KEYS` |
| `ATLAS_API_KEY` |
| `RENDI_API_KEY` |
| `ZAPCAP_API_KEY` |
| `TAVILY_API_KEY` |
| `SCRAPINGBEE_API_KEY` |
| `AWS_ACCESS_KEY_ID` |
| `AWS_SECRET_ACCESS_KEY` |

### 3e. Google credentials (inline form — no JSON file path)

| Secret name | Value |
|---|---|
| `GOOGLE_PROJECT_ID` | same as PA |
| `GOOGLE_PRIVATE_KEY_ID` | same as PA |
| `GOOGLE_PRIVATE_KEY` | **paste the whole `-----BEGIN PRIVATE KEY-----…END PRIVATE KEY-----\n` block as-is. HF accepts multiline secrets.** |
| `GOOGLE_CLIENT_EMAIL` | same as PA |
| `GOOGLE_CLIENT_ID` | same as PA |
| `VERTEX_AI_PROJECT_ID` | same as PA, default `amit-tts` |
| `VERTEX_AI_LOCATION` | `us-central1` |
| `VERTEX_AI_PRIVATE_KEY_ID` | same as PA |
| `VERTEX_AI_PRIVATE_KEY` | same as PA (multiline) |
| `VERTEX_AI_CLIENT_EMAIL` | same as PA |
| `VERTEX_AI_CLIENT_ID` | same as PA |
| `GCS_BUCKET_NAME` | `aporia-unleash` |

Do **not** set `GOOGLE_APPLICATION_CREDENTIALS` or `GCS_CREDENTIALS_FILE`
or `SHEETS_SERVICE_ACCOUNT_FILE` — those are file paths from the PA
deploy and won't exist in the container. Inline credentials cover both
Sheets and TTS.

### 3f. Domain/sheet ID + observability

| Secret name | Value |
|---|---|
| `SYMPHONY_DB_SHEET_ID` | same as PA (the metadata log sheet) |
| `SYMPHONY_DB_SHEET_TAB` | `SYMPHONY_DB` |
| `SENTRY_DSN` | same as PA, or leave empty |
| `SLACK_ALERT_WEBHOOK` | same as PA, or leave empty |

### 3g. Concurrency knobs (optional — defaults are fine)

You can override the `BULKVID_MAX_CONCURRENT_ROWS`,
`BULKVID_SHEET_WRITE_INTERVAL_SECONDS`, and the cost-cap settings here
if you want different values from the in-code defaults.

---

## 4 — Push the repo to the Space (~5 min)

The Space has its own git remote. Add it alongside the GitHub one:

```bash
cd c:/Projects/turbovid-new

# One-time:
git remote add hf https://huggingface.co/spaces/<owner>/aporia-bulkvid

# Every deploy:
git push hf main
```

HF immediately starts building the Docker image — watch progress in the
Space's **Logs** tab.

First build takes 3–5 min (libsql wheel + pip deps). Subsequent builds
are fast (Docker layer caching).

When the build finishes, the Space goes **"Running"**.

---

## 5 — Smoke test the new endpoint (~10 min)

### 5a. Public health check

```bash
curl https://<owner>-aporia-bulkvid.hf.space/health
# → {"status":"ok","version":"0.1.0"}
```

### 5b. Deep health (admin-only, needs a Bearer token)

Easiest from the Apps Script editor:

```javascript
function pingDeepHealth() {
  const url = 'https://<owner>-aporia-bulkvid.hf.space/health/deep';
  const resp = UrlFetchApp.fetch(url, {
    headers: { Authorization: 'Bearer ' + ScriptApp.getIdentityToken() },
    muteHttpExceptions: true,
  });
  console.log(resp.getResponseCode(), resp.getContentText());
}
```

Expected response includes:

```json
{
  "service": "bulkvid",
  "db": {
    "backend": "libsql_embedded_replica",
    "ping_ms": <something_under_50>
  },
  ...
}
```

If `db.backend` is `"sqlite_local"` instead, your `BULKVID_DB_URL` Secret
didn't take. If `ping_ms` is missing and `ping_error` is present, your
Turso URL/token is wrong. Fix in HF Secrets → restart the Space.

### 5c. Container logs

In the Space **Logs** tab, you should see lines like:

```
[bulkvid db]    db_backend  backend=libsql_embedded_replica  path=/tmp/data/jobs.db ...
[bulkvid queue] queue_init  db_path=/tmp/data/jobs.db
[bulkvid boot]  service_start  env=prod ...
```

---

## 6 — Cut over (~5 min)

In the Apps Script editor:

1. **File → Project properties → Script properties** (or the new UI:
   gear → Project properties).
2. Find the `BACKEND_URL` property.
3. Change it from `https://jenia_video-aporianetworks.pythonanywhere.com`
   to `https://<owner>-aporia-bulkvid.hf.space`.
4. Save.

That's the entire client-side cut-over. The new retry + idempotency
layer we shipped earlier works unchanged.

### 6a. Submit a real 1-row job

From the sheet, select one row → **Aporia Bulk Video → Generate selected
rows**. Watch:

- HF Space **Logs** tab for `job_enqueued` + `job_submit` lines.
- The sidebar for live row status.
- The sheet for the **Ready Video 1** URL when the worker finishes.

If the round-trip is green, congrats — you're off PA.

---

## 7 — Watch for 24 hours

Things to look for in the HF Logs tab:

- `queue_busy_503` → should be **zero**. If non-zero, Turso is contending
  somehow; raise `BULKVID_DB_SYNC_INTERVAL_SECONDS` slightly.
- `idempotency_hit` → should **drop to zero** (no PA dispatch flakiness
  to drop responses).
- Python tracebacks → there should be none. If anything fires, paste it
  into a fresh session.

---

## 8 — Decommission PA (after 7 days green)

1. PA Bash → `pa stop-always-on-task <ID>` (or the web tab's Always-On
   page).
2. PA Web tab → leave the web app running but un-pointed-at; it costs
   nothing on free tier.
3. After another month with no surprises, you can fully delete the PA
   `bulkvid` directory.

---

## Rollback

Instant — change `BACKEND_URL` in Apps Script back to the PA URL. PA's
web app and always-on task stay alive during the 7-day watch precisely
so this is one click away.

---

## Common gotchas

- **`db.backend = sqlite_local` in /health/deep** → `BULKVID_DB_URL`
  Secret is empty or misspelled. HF Secrets are case-sensitive.
- **Container restarts every few hours** → you forgot to set Hardware to
  CPU basic and got assigned an ephemeral runner. Re-check Hardware in
  the Space settings.
- **"Build failed: failed to fetch libsql"** → libsql doesn't have a
  wheel for the Python in the base image. The Dockerfile pins
  `python:3.12-slim` because libsql ships wheels for it; don't change
  that line without verifying first.
- **`GOOGLE_PRIVATE_KEY` lines come back as literal `\n`** → paste with
  actual newlines, not backslash-n. HF Secrets preserves multiline
  input. If it doesn't, the credential parser at boot will log a
  Google-API auth error.
- **502 from HF on first request after a long idle** → the Space slept
  (48h inactivity). Just hit it again to wake it. Sidebar polling
  prevents this in practice.
