"""Global test fixtures.

Isolates every test from the project's real ``.env`` file. Without this,
running the suite while a live ``.env`` exists (containing the dev auth
bypass, real API keys, custom allowlists, etc.) makes assertions about
defaults and unconfigured states flaky and misleading.

The mechanism: replace ``bulkvid.config.get_settings`` with a function that
constructs ``Settings`` with ``_env_file=None``, which is pydantic-settings'
documented way to skip dotenv loading. Also wipe the env vars themselves so
test-local ``Settings(**kwargs)`` constructions aren't contaminated by the
process env that was loaded at uvicorn boot time.

Individual tests that need to inject their own settings can still
monkeypatch ``bulkvid.routes.xxx.get_settings`` directly — that
overrides the autouse fixture for the duration of the test.
"""

from __future__ import annotations

import pytest

from bulkvid import config as _config_mod
from bulkvid.config import Settings


# Env vars that the local .env may set — clear them per test.
_CONTAMINATING_ENV_VARS = (
    "BULKVID_DEV_AUTH_BYPASS_EMAIL",
    "ALLOWED_HD",
    "BULK_TEAM_ALLOWLIST",
    "BULK_TEAM_DOMAINS",
    "ADMIN_ALLOWLIST",
    "ADMIN_PANEL_USERNAME",
    "ADMIN_PANEL_PASSWORD",
    "OPENAI_API_KEY",
    "KIE_AI_KEYS",
    "ATLAS_API_KEY",
    "RENDI_API_KEY",
    "ZAPCAP_API_KEY",
    "TAVILY_API_KEY",
    "SCRAPINGBEE_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "GCS_CREDENTIALS_FILE",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_ACCOUNT_TYPE",
    "GOOGLE_PROJECT_ID",
    "GOOGLE_PRIVATE_KEY_ID",
    "GOOGLE_PRIVATE_KEY",
    "GOOGLE_CLIENT_EMAIL",
    "GOOGLE_CLIENT_ID",
    "SHEETS_SERVICE_ACCOUNT_FILE",
    "VERTEX_AI_PROJECT_ID",
)


def _clean_settings_factory() -> Settings:
    # ``_env_file=None`` tells pydantic-settings to skip dotenv loading;
    # the constructor still respects explicit kwargs and the (now-cleared)
    # process env.
    return Settings(_env_file=None)    # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _isolate_from_project_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    # 1. Wipe contaminating env vars from the process environment.
    for name in _CONTAMINATING_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    # 2. Chdir to a clean tmp dir so pydantic-settings can't find the
    #    project's real ``.env`` (it reads from CWD by default).
    monkeypatch.chdir(tmp_path_factory.mktemp("no_env"))

    # 3. Preserve the ``.cache_clear`` attribute interface for any tests
    #    that still call ``get_settings.cache_clear()``.
    _clean_settings_factory.cache_clear = lambda: None    # type: ignore[attr-defined]
    monkeypatch.setattr(_config_mod, "get_settings", _clean_settings_factory)
