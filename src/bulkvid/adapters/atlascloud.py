"""AtlasCloud adapter — image generation fallback for kie.ai.

AtlasCloud (https://api.atlascloud.ai) is a multi-model image generation
service we use as a fallback when kie.ai is rate-limited or down. The API
is async (submit + poll), shaped similar to kie.ai but with different
endpoint paths.

Pattern adapted from the existing AtlasCloud integration in
``refs/creativesbuilder...``:

  - POST ``/api/v1/model/generateImage``
        body: { model, prompt, image_urls?, size?, quality, output_format }
        returns: { prediction_id }
  - GET  ``/api/v1/model/prediction/{prediction_id}``
        returns: { status, outputs?[0], error? }
        status: completed | failed | (pending shapes)

Two convenience wrappers match the kie.ai surface:

  - ``text_to_image``  -> equivalent to kie.ai text-to-image models
  - ``edit_image``     -> equivalent to ``google/nano-banana-edit`` (1 seed image)

Plan §5 (Image generation), §11 (cost — refresh before each release).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from bulkvid.config import Settings, get_settings
from bulkvid.logging import get_logger

_log = get_logger("atlas")


# Cost estimate (USD). Refresh from AtlasCloud pricing page before release.
COST_ATLAS_GENERATE_USD = 0.04
COST_ATLAS_EDIT_USD = 0.05


# ── Errors ───────────────────────────────────────────────────────────────────


class AtlasError(RuntimeError):
    """Base class for AtlasCloud errors."""


class AtlasAuthError(AtlasError):
    """401 — invalid API key."""


class AtlasTaskFailedError(AtlasError):
    """Prediction reported status=failed."""


class AtlasTimeoutError(AtlasError):
    """Did not complete within ``max_attempts`` polls."""


# ── Aspect ratio → AtlasCloud size string ───────────────────────────────────
# AtlasCloud expects discrete size labels. Map the same Sheet aspect-ratio
# strings the rest of the pipeline uses.

_SIZE_BY_RATIO = {
    "9:16": "1024x1792",
    "16:9": "1792x1024",
    "1:1": "1024x1024",
    "4:5": "1024x1280",
    "5:4": "1280x1024",
    "3:4": "1024x1365",
    "4:3": "1365x1024",
}


def size_for_ratio(aspect_ratio: str) -> str:
    """Return the AtlasCloud size label for a Sheet aspect-ratio string."""
    s = (aspect_ratio or "").strip().lower()
    if not s or s == "auto":
        return _SIZE_BY_RATIO["9:16"]
    # Normalise "09:16" -> "9:16".
    if ":" in s:
        parts = s.split(":")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            normalised = f"{int(parts[0])}:{int(parts[1])}"
            if normalised in _SIZE_BY_RATIO:
                return _SIZE_BY_RATIO[normalised]
    # Direct pixel format pass-through.
    if "x" in s:
        parts = s.split("x")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            return f"{int(parts[0])}x{int(parts[1])}"
    return _SIZE_BY_RATIO["9:16"]


# ── Client ───────────────────────────────────────────────────────────────────


class AtlasCloudClient:
    """Async AtlasCloud client. Submit + poll."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.atlascloud.ai",
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        default_quality: str = "low",
        default_output_format: str = "jpeg",
        default_model: str = "nano-banana",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("AtlasCloudClient requires an api_key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
        self._default_quality = default_quality
        self._default_output_format = default_output_format
        self._default_model = default_model
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

    async def __aenter__(self) -> AtlasCloudClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    # ── Submit ──────────────────────────────────────────────────────────

    async def submit(
        self,
        prompt: str,
        *,
        model: str | None = None,
        image_urls: list[str] | None = None,
        size: str | None = None,
        quality: str | None = None,
        output_format: str | None = None,
    ) -> str:
        """Submit an image task. Returns the prediction_id."""
        url = f"{self._base_url}/api/v1/model/generateImage"
        body: dict[str, Any] = {
            "model": model or self._default_model,
            "prompt": prompt,
            "quality": quality or self._default_quality,
            "output_format": output_format or self._default_output_format,
        }
        if size:
            body["size"] = size
        if image_urls:
            body["image_urls"] = image_urls

        _log.info(
            "atlas_submit",
            model=body["model"],
            prompt_chars=len(prompt),
            image_count=len(image_urls or []),
            quality=body["quality"],
        )
        resp = await self._client.post(url, json=body, headers=self._headers)
        if resp.status_code == 401:
            raise AtlasAuthError("AtlasCloud 401 — invalid API key")
        if resp.status_code != 200:
            raise AtlasError(
                f"AtlasCloud submit HTTP {resp.status_code}: {resp.text[:200]}"
            )
        result = resp.json()
        prediction_id = (
            result.get("prediction_id")
            or result.get("id")
            or (result.get("data") or {}).get("prediction_id")
        )
        if not prediction_id:
            raise AtlasError(f"AtlasCloud submit missing prediction_id: {result}")
        _log.info("atlas_submit_ok", prediction_id=prediction_id)
        return prediction_id

    # ── Poll ────────────────────────────────────────────────────────────

    async def poll(
        self,
        prediction_id: str,
        max_attempts: int = 60,
        delay_seconds: float = 5.0,
    ) -> str:
        """Poll a prediction. Returns the first output URL."""
        url = f"{self._base_url}/api/v1/model/prediction/{prediction_id}"
        for attempt in range(max_attempts):
            resp = await self._client.get(url, headers=self._headers)
            if resp.status_code != 200:
                if attempt == max_attempts - 1:
                    raise AtlasError(
                        f"AtlasCloud poll HTTP {resp.status_code} after {max_attempts} attempts"
                    )
                await asyncio.sleep(delay_seconds)
                continue

            result = resp.json()
            status = (result.get("status") or "").lower()

            if status in ("completed", "succeeded", "success"):
                outputs = result.get("outputs") or []
                if not outputs:
                    raise AtlasError(
                        f"AtlasCloud prediction {prediction_id} completed but no outputs"
                    )
                first = outputs[0]
                output_url = first if isinstance(first, str) else first.get("url")
                if not output_url:
                    raise AtlasError(
                        f"AtlasCloud prediction {prediction_id} outputs[0] missing url"
                    )
                _log.info(
                    "atlas_poll_ok", prediction_id=prediction_id, attempts=attempt + 1
                )
                return output_url

            if status in ("failed", "error"):
                err = result.get("error") or result.get("message") or "unknown"
                _log.error("atlas_poll_failed", prediction_id=prediction_id, error=err)
                raise AtlasTaskFailedError(
                    f"AtlasCloud prediction {prediction_id} failed: {err}"
                )

            if attempt < max_attempts - 1:
                await asyncio.sleep(delay_seconds)

        raise AtlasTimeoutError(
            f"AtlasCloud prediction {prediction_id} did not complete within {max_attempts} attempts"
        )

    # ── High-level wrappers ────────────────────────────────────────────

    async def text_to_image(
        self,
        prompt: str,
        aspect_ratio: str,
        *,
        model: str | None = None,
        max_attempts: int = 60,
        delay_seconds: float = 5.0,
    ) -> tuple[str, float]:
        prediction_id = await self.submit(
            prompt,
            model=model,
            size=size_for_ratio(aspect_ratio),
        )
        url = await self.poll(
            prediction_id, max_attempts=max_attempts, delay_seconds=delay_seconds
        )
        return url, COST_ATLAS_GENERATE_USD

    async def edit_image(
        self,
        source_image_url: str,
        prompt: str,
        aspect_ratio: str,
        *,
        model: str | None = None,
        max_attempts: int = 60,
        delay_seconds: float = 5.0,
    ) -> tuple[str, float]:
        """Match the kie.ai ``nano-banana-edit`` surface (single seed image)."""
        prediction_id = await self.submit(
            prompt,
            model=model,
            image_urls=[source_image_url],
            size=size_for_ratio(aspect_ratio),
        )
        url = await self.poll(
            prediction_id, max_attempts=max_attempts, delay_seconds=delay_seconds
        )
        return url, COST_ATLAS_EDIT_USD


def build_client_from_settings(settings: Settings | None = None) -> AtlasCloudClient | None:
    """Return an AtlasCloudClient or ``None`` when no key is configured."""
    s = settings or get_settings()
    if not s.ATLAS_API_KEY:
        return None
    return AtlasCloudClient(
        api_key=s.ATLAS_API_KEY,
        base_url=s.ATLAS_BASE_URL,
        default_quality=s.ATLAS_DEFAULT_QUALITY,
        default_output_format=s.ATLAS_DEFAULT_OUTPUT_FORMAT,
        default_model=s.ATLAS_DEFAULT_MODEL,
    )
