"""MuAPI OpenAI-compatible image-generation adapter.

MuAPI's OpenAI-compatible image endpoint is synchronous from the caller's
perspective: the service waits for its internal task and returns an OpenAI
``data[].url`` response. This adapter is intentionally text-to-image only;
the current compatibility surface does not accept reference images.
"""

from __future__ import annotations

from typing import Any

import httpx

from bulkvid.config import Settings, get_settings
from bulkvid.logging import get_logger

_log = get_logger("muapi")


# Public model metadata reports this current default at $0.003 per image.
# Keep it configurable because MuAPI pricing is model-dependent and dynamic.
COST_MUAPI_FLUX_SCHNELL_USD = 0.003
DEFAULT_MUAPI_MODEL = "flux-schnell"

_SIZE_BY_RATIO = {
    "9:16": "1024x1792",
    "16:9": "1792x1024",
    "1:1": "1024x1024",
}


class MuAPIError(RuntimeError):
    """Base class for MuAPI request and response failures."""


class MuAPIAuthError(MuAPIError):
    """MuAPI rejected the configured API key."""


def size_for_ratio(aspect_ratio: str) -> str:
    """Map a pipeline aspect ratio to a documented OpenAI-compatible size."""
    value = (aspect_ratio or "").strip().lower()
    if ":" in value:
        parts = value.split(":")
        if len(parts) == 2 and all(part.isdigit() for part in parts):
            value = f"{int(parts[0])}:{int(parts[1])}"
    if value in _SIZE_BY_RATIO:
        return _SIZE_BY_RATIO[value]

    if "x" in value:
        parts = value.split("x")
        if len(parts) == 2 and all(part.isdigit() for part in parts):
            width, height = (int(part) for part in parts)
            if width > 0 and height > 0:
                value = f"{width}:{height}"

    try:
        ratio_width, ratio_height = (float(part) for part in value.split(":", 1))
        ratio = ratio_width / ratio_height
    except (ValueError, ZeroDivisionError):
        return _SIZE_BY_RATIO["9:16"]

    if ratio >= 1.18:
        return _SIZE_BY_RATIO["16:9"]
    if ratio <= 0.85:
        return _SIZE_BY_RATIO["9:16"]
    return _SIZE_BY_RATIO["1:1"]


class MuAPIClient:
    """Small async client for MuAPI's OpenAI-compatible image endpoint."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.muapi.ai/v1",
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        default_model: str = DEFAULT_MUAPI_MODEL,
        cost_per_image_usd: float = COST_MUAPI_FLUX_SCHNELL_USD,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("MuAPIClient requires an api_key")
        if cost_per_image_usd < 0:
            raise ValueError("cost_per_image_usd must be non-negative")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
        self._default_model = default_model
        self._cost_per_image_usd = cost_per_image_usd
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(timeout=self._timeout)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    async def __aenter__(self) -> MuAPIClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def text_to_image(
        self,
        prompt: str,
        aspect_ratio: str,
        *,
        model: str | None = None,
        n: int = 1,
    ) -> tuple[str, float]:
        """Generate one image and return ``(https_url, cost_usd)``."""
        prompt = (prompt or "").strip()
        if not 2 <= len(prompt) <= 3000:
            raise MuAPIError("MuAPI prompt must contain 2 to 3000 characters")
        if n != 1:
            raise MuAPIError("MuAPI fallback only supports one image per request")

        body = {
            "model": model or self._default_model,
            "prompt": prompt,
            "n": n,
            "size": size_for_ratio(aspect_ratio),
        }
        _log.info(
            "muapi_image_submit",
            model=body["model"],
            aspect_ratio=aspect_ratio,
            prompt_chars=len(prompt),
        )
        try:
            response = await self._client.post(
                f"{self._base_url}/images/generations",
                json=body,
                headers=self._headers,
            )
        except httpx.HTTPError as exc:
            raise MuAPIError(f"MuAPI request failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise MuAPIAuthError(f"MuAPI authentication failed (HTTP {response.status_code})")
        if response.status_code >= 400:
            raise MuAPIError(
                f"MuAPI image request HTTP {response.status_code}: {response.text[:200]}"
            )

        try:
            result = response.json()
        except ValueError as exc:
            raise MuAPIError("MuAPI returned invalid JSON") from exc

        items = result.get("data") if isinstance(result, dict) else None
        if not isinstance(items, list) or not items:
            raise MuAPIError("MuAPI response did not contain an image")
        first = items[0]
        url = first.get("url") if isinstance(first, dict) else first
        if not isinstance(url, str) or not url.startswith("https://"):
            raise MuAPIError("MuAPI response did not contain an HTTPS image URL")

        _log.info("muapi_image_ok", model=body["model"], output_url_host=url.split("/", 3)[2])
        return url, self._cost_per_image_usd


def build_client_from_settings(settings: Settings | None = None) -> MuAPIClient | None:
    """Return a MuAPI client when ``MUAPI_API_KEY`` is configured."""
    s = settings or get_settings()
    if not s.MUAPI_API_KEY:
        return None
    return MuAPIClient(
        api_key=s.MUAPI_API_KEY,
        base_url=s.MUAPI_BASE_URL,
        connect_timeout=s.MUAPI_CONNECT_TIMEOUT_SECONDS,
        read_timeout=s.MUAPI_TIMEOUT_SECONDS,
        default_model=s.MUAPI_DEFAULT_MODEL,
        cost_per_image_usd=s.MUAPI_COST_PER_IMAGE_USD,
    )
