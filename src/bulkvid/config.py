"""Settings loader.

Reads from `.env` and the process environment via pydantic-settings, applies
typed defaults, and exposes a single `get_settings()` accessor. Mutable runtime
overrides (admin panel) live in the SQLite settings store, not here — this
module is for boot-time / immutable values only.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ── Service ──────────────────────────────────────────────────────────
    BULKVID_ENV: str = "local"
    BULKVID_HOST: str = "0.0.0.0"
    BULKVID_PORT: int = 8788
    BULKVID_LOG_LEVEL: str = "INFO"
    BULKVID_DATA_DIR: Path = Path("./data")

    # ── Database backend ─────────────────────────────────────────────────
    # When BULKVID_DB_URL is empty (default) the queue and settings store
    # use plain sqlite3 at <BULKVID_DATA_DIR>/jobs.db and settings.db. This
    # is local-dev and the test-suite path.
    #
    # When set, both stores switch to libsql embedded-replica mode against
    # the Turso URL — reads are local-SQLite-fast, writes sync to Turso
    # every BULKVID_DB_SYNC_INTERVAL_SECONDS so container restarts on
    # ephemeral hosts (HuggingFace Spaces, etc.) never lose state.
    # Plan: _plans/2026-06-04-migrate-to-hf-spaces-turso.md.
    BULKVID_DB_URL: str = ""
    BULKVID_DB_AUTH_TOKEN: str = ""
    # A separate Turso DB for the admin-editable settings, so we can rotate
    # tokens independently. Falls back to BULKVID_DB_* when empty so a
    # single-DB deploy still works.
    BULKVID_SETTINGS_DB_URL: str = ""
    BULKVID_SETTINGS_DB_AUTH_TOKEN: str = ""
    BULKVID_DB_SYNC_INTERVAL_SECONDS: float = 1.0

    # ── Auth ─────────────────────────────────────────────────────────────
    # Specific emails (comma-separated) — useful for individual exceptions.
    BULK_TEAM_ALLOWLIST: str = ""
    # Whole-domain allowlist (comma-separated, no @). Any email ending in
    # one of these is accepted. e.g. "aporianetworks.com,teaminternet.com".
    BULK_TEAM_DOMAINS: str = ""

    ADMIN_ALLOWLIST: str = ""

    # Google Workspace ``hd`` claim. Comma-separated list of allowed
    # Workspace domains. Empty disables the hd check entirely.
    ALLOWED_HD: str = ""

    # Admin panel uses HTTP Basic auth (separate from the API's OAuth flow).
    # Leave empty to disable the admin panel on a given deploy.
    ADMIN_PANEL_USERNAME: str = ""
    ADMIN_PANEL_PASSWORD: str = ""

    # LOCAL-DEV ONLY. When set, ``/jobs`` accepts requests WITHOUT any
    # bearer token and treats the caller as this email. Production deploys
    # MUST leave this empty — there is a loud warning on every request when
    # it's active. Lets you ``curl`` the API without setting up OAuth.
    BULKVID_DEV_AUTH_BYPASS_EMAIL: str = ""

    # ── OpenAI ───────────────────────────────────────────────────────────
    OPENAI_API_KEY: str = ""

    # ── kie.ai ───────────────────────────────────────────────────────────
    KIE_AI_KEYS: str = ""
    KIE_BASE_URL: str = "https://api.kie.ai"
    KIE_CONNECT_TIMEOUT_SECONDS: float = 10.0
    KIE_TIMEOUT_SECONDS: float = 120.0
    KIE_RATE_LIMIT_COOLDOWN_SECONDS: float = 60.0
    KIE_MAX_IN_FLIGHT_IMAGE_TASKS: int = 40

    # ── AtlasCloud (fallback for kie.ai image generation) ────────────────
    ATLAS_API_KEY: str = ""
    ATLAS_BASE_URL: str = "https://api.atlascloud.ai"
    ATLAS_DEFAULT_MODEL: str = "nano-banana"   # closest to kie.ai's google/nano-banana-edit
    ATLAS_DEFAULT_QUALITY: str = "low"          # low | medium | high
    ATLAS_DEFAULT_OUTPUT_FORMAT: str = "jpeg"   # jpeg | png

    # ── Google Cloud ─────────────────────────────────────────────────────
    # Option A (file path): GOOGLE_APPLICATION_CREDENTIALS = /path/to/json
    GOOGLE_APPLICATION_CREDENTIALS: str = ""
    # Option B (inline env, PythonAnywhere-friendly): paste each field
    # straight from the service account JSON. Used when no file path is set.
    GOOGLE_ACCOUNT_TYPE: str = "service_account"
    GOOGLE_PROJECT_ID: str = ""
    GOOGLE_PRIVATE_KEY_ID: str = ""
    GOOGLE_PRIVATE_KEY: str = ""               # may contain literal "\n"; we normalise
    GOOGLE_CLIENT_EMAIL: str = ""
    GOOGLE_CLIENT_ID: str = ""

    VERTEX_AI_PROJECT_ID: str = "amit-tts"
    VERTEX_AI_LOCATION: str = "us-central1"

    # Vertex AI / Gemini TTS uses a SEPARATE service account from storage
    # because storage and TTS often live in different GCP projects. Falls
    # back to the GOOGLE_* set when these are empty.
    VERTEX_AI_PRIVATE_KEY_ID: str = ""
    VERTEX_AI_PRIVATE_KEY: str = ""
    VERTEX_AI_CLIENT_EMAIL: str = ""
    VERTEX_AI_CLIENT_ID: str = ""

    GCS_BUCKET_NAME: str = "aporia-unleash"
    GCS_CREDENTIALS_FILE: str = ""

    # ── AWS S3 ───────────────────────────────────────────────────────────
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = "us-east-1"
    AWS_BUCKET_NAME: str = "aporia-creative"

    # ── Rendi.dev ────────────────────────────────────────────────────────
    RENDI_API_KEY: str = ""
    RENDI_BASE_URL: str = "https://api.rendi.dev"
    RENDI_DEFAULT_VCPU: int = 4
    RENDI_MAX_COMMAND_RUN_SECONDS: int = 300

    # ── ZapCap ───────────────────────────────────────────────────────────
    ZAPCAP_API_KEY: str = ""
    ZAPCAP_BASE_URL: str = "https://api.zapcap.ai"
    ZAPCAP_TEMPLATE_ID: str = "46d20d67-255c-4c6a-b971-31fddcfea7f0"
    ZAPCAP_SUBMIT_INTERVAL_SECONDS: float = 1.0
    ZAPCAP_POLL_INTERVAL_SECONDS: float = 10.0

    # ── Article fetching ─────────────────────────────────────────────────
    TAVILY_API_KEY: str = ""
    TAVILY_TIMEOUT_SECONDS: float = 15.0
    SCRAPINGBEE_API_KEY: str = ""
    SCRAPINGBEE_TIMEOUT_SECONDS: float = 30.0
    ARTICLE_MAX_CONTENT_CHARS: int = 50000
    ARTICLE_CACHE_TTL_SECONDS: int = 3600

    # ── Google Sheets ────────────────────────────────────────────────────
    SHEETS_SERVICE_ACCOUNT_FILE: str = ""
    SYMPHONY_DB_SHEET_ID: str = "10NgBy7DGUOkW15HHAQC4B-BP0dcCIX5yTnRCDowMkgc"
    SYMPHONY_DB_SHEET_TAB: str = "SYMPHONY_DB"

    # The bulk-team spreadsheet the local runner (``tools/run_local.py``)
    # defaults to when ``--sheet-id`` is omitted. PA itself doesn't read
    # this — Apps Script passes the sheet ID per-request. Set only on
    # machines running the local CLI so the bulk user doesn't have to
    # retype the ID every invocation.
    BULKVID_DEFAULT_SHEET_ID: str = ""

    # ── Concurrency ──────────────────────────────────────────────────────
    # Default tuned for PythonAnywhere (CPU-quota-aware). Override to 40 on Hetzner.
    # See plan §5 "Concurrency model" and §12 "Performance targets".
    BULKVID_MAX_CONCURRENT_ROWS: int = 10
    BULKVID_SHEET_WRITE_INTERVAL_SECONDS: float = 5.0
    BULKVID_MAX_ROWS_PER_BATCH: int = 5000

    # Per-provider concurrency caps — cap in-flight requests against ONE
    # external provider across the WHOLE worker, independent of the runner-
    # level row cap. Added 2026-06-08 after a 277-row batch hit 46% failures
    # from Rendi platform overload + Gemini TTS per-minute quota bursts.
    # Plan: ``_plans/2026-06-08-200-row-batch-failures.md`` §Phase 1.
    # Both values are v1 guesses to be retuned from production semaphore-wait
    # data; see each adapter for default-choice reasoning.
    BULKVID_RENDI_MAX_CONCURRENT: int = 6
    BULKVID_GEMINI_TTS_MAX_CONCURRENT: int = 4

    # ── Feature flags ────────────────────────────────────────────────────
    BULKVID_INTERNAL_PARALLEL: int = 1
    BULKVID_ARTICLE_CACHE: int = 1
    BULKVID_LANGUAGE_CACHE: int = 1
    BULKVID_SHEET_BATCH_WRITES: int = 1
    BULKVID_KIE_KEY_POOL: int = 1
    BULKVID_FAST_ZAPCAP_SUBMIT: int = 0

    # ── Cost guards ──────────────────────────────────────────────────────
    BULKVID_COST_PER_BATCH_USD_CAP: float = 200.0
    BULKVID_COST_PER_DAY_USD_CAP: float = 500.0
    BULKVID_COST_PER_MONTH_USD_CAP: float = 5000.0
    BULKVID_KILL_SWITCH: int = 0

    # ── Observability ────────────────────────────────────────────────────
    SENTRY_DSN: str = ""
    SLACK_ALERT_WEBHOOK: str = ""

    # ── Derived accessors ────────────────────────────────────────────────
    @property
    def bulk_team_emails(self) -> list[str]:
        return [e.strip().lower() for e in self.BULK_TEAM_ALLOWLIST.split(",") if e.strip()]

    @property
    def bulk_team_domains_list(self) -> list[str]:
        return [
            d.strip().lower().lstrip("@")
            for d in self.BULK_TEAM_DOMAINS.split(",")
            if d.strip()
        ]

    @property
    def admin_emails(self) -> list[str]:
        return [e.strip().lower() for e in self.ADMIN_ALLOWLIST.split(",") if e.strip()]

    @property
    def allowed_hd_list(self) -> list[str]:
        return [d.strip().lower() for d in self.ALLOWED_HD.split(",") if d.strip()]

    @property
    def kie_key_list(self) -> list[str]:
        return [k.strip() for k in self.KIE_AI_KEYS.split(",") if k.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
