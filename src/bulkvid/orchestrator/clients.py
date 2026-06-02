"""Pipeline-wide client bundle.

The row processors take a single ``PipelineClients`` so call sites stay
short (a single positional arg instead of seven). The bundle is also easy
to construct in tests with mocks injected per-client.
"""

from __future__ import annotations

from dataclasses import dataclass

from bulkvid.adapters.article_fetch import ArticleFetcher
from bulkvid.adapters.atlascloud import AtlasCloudClient
from bulkvid.adapters.gemini_tts import GeminiTTSClient
from bulkvid.adapters.kie import KieClient
from bulkvid.adapters.openai_client import OpenAIClient
from bulkvid.adapters.rendi import RendiClient
from bulkvid.adapters.storage import StorageClient
from bulkvid.adapters.zapcap import ZapCapClient
from bulkvid.orchestrator.settings_store import SettingsStore


@dataclass
class PipelineClients:
    openai: OpenAIClient
    kie: KieClient
    tts: GeminiTTSClient
    rendi: RendiClient
    storage: StorageClient
    article: ArticleFetcher
    zapcap: ZapCapClient | None = None       # may be unconfigured if no rows ever use it
    atlas: AtlasCloudClient | None = None    # fallback for kie.ai image generation
    settings_store: SettingsStore | None = None    # admin-editable runtime settings
