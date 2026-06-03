"""kie.ai adapter — submit + poll, with key pool and per-key cooldown.

Used for the mandatory image pipeline:
  - ``google/nano-banana-edit``      — 2x2 collage generation from a seed image
  - ``recraft/crisp-upscale``        — upscale the collage before the local split

Pattern reused from ``refs/creativesbuilder.../`` ``_KiePool``: kie.ai tasks
are scoped to the submitting key's account, so we tag each task_id with the
last 12 chars of the submitting key and route polls back to the same key.

Public surface
--------------
- ``KiePool``               — round-robin keys with per-key cooldown
- ``KieClient``             — async submit + poll, key-pinning aware
- ``nano_banana_edit(...)`` — high-level wrapper, returns ``(url, cost_usd)``
- ``recraft_crisp_upscale(...)`` — high-level wrapper, returns ``(url, cost_usd)``
- ``build_client_from_settings()`` — wires the client from env

Plan: ``_plans/2026-06-02-aporia-bulk-video-tool.md`` §5 (Concurrency model,
"kie.ai key pool"), §11 (Cost model — refresh estimates before each release).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from bulkvid.config import Settings, get_settings
from bulkvid.logging import get_logger

_log = get_logger("kie")


# Cost estimates (USD). Verified live on kie.ai 2026-06-03. Override per-model
# via the admin panel once it ships (Phase 5).
COST_NANO_BANANA_EDIT_USD = 0.04
COST_NANO_BANANA_2_USD = 0.06        # nano-banana-2 @ 2K (kie: $0.04/1K, $0.06/2K)
COST_NANO_BANANA_2_1K_USD = 0.04     # nano-banana-2 @ 1K (cartoon mode default)
COST_GPT_IMAGE_2_USD = 0.08          # gpt-image-2 fallback, rough mid-tier estimate
COST_RECRAFT_UPSCALE_USD = 0.04
COST_SEEDANCE_PRO_720P_4S_USD = 0.07  # Seedance 1.5 Pro i2v @ 720p, 4s, no audio
COST_SEEDANCE_PRO_720P_8S_USD = 0.14  # Seedance 1.5 Pro i2v @ 720p, 8s, no audio

# Production model identifiers.
MODEL_NANO_BANANA_EDIT = "google/nano-banana-edit"
MODEL_NANO_BANANA_2 = "nano-banana-2"
MODEL_GPT_IMAGE_2 = "gpt-image-2-image-to-image"
MODEL_RECRAFT_UPSCALE = "recraft/crisp-upscale"
MODEL_SEEDANCE_PRO = "bytedance/seedance-1.5-pro"


# ── Errors ───────────────────────────────────────────────────────────────────


class KieError(RuntimeError):
    """Base class for kie.ai errors."""


class KieAuthError(KieError):
    """401 — invalid or revoked key."""


class KieRateLimitError(KieError):
    """429 — per-key rate limit (key is placed on cooldown by the caller)."""


class KieTaskFailedError(KieError):
    """Task reported ``state=fail`` during polling."""


class KieTimeoutError(KieError):
    """Task did not complete within ``max_attempts`` polls."""


# ── Task-ID pinning ──────────────────────────────────────────────────────────
# kie.ai task IDs are scoped to the account of the submitting key, so polls
# MUST use the same key. We wrap each returned task_id with the last 12 chars
# of the submitting key so the poller can re-select the same key, even if the
# pool has rotated in between.

_PIN_SEP = "::"
_KEY_SUFFIX_LEN = 12


def _key_suffix(key: str) -> str:
    return key[-_KEY_SUFFIX_LEN:]


def _pin_task_id(task_id: str, key: str) -> str:
    return f"{task_id}{_PIN_SEP}{_key_suffix(key)}"


def _unpin_task_id(pinned: str) -> tuple[str, str | None]:
    """Returns ``(real_task_id, key_suffix_or_None)``."""
    if _PIN_SEP not in pinned:
        return pinned, None
    real, suffix = pinned.rsplit(_PIN_SEP, 1)
    return real, suffix


# ── Pool ─────────────────────────────────────────────────────────────────────


@dataclass
class _KeyState:
    key: str
    cooldown_until: float = 0.0   # monotonic timestamp; 0 = available

    @property
    def suffix(self) -> str:
        return _key_suffix(self.key)

    def is_available(self, now: float) -> bool:
        return now >= self.cooldown_until


class KiePool:
    """Round-robin pool of kie.ai keys with per-key cooldown on 429.

    Concurrency-safe: a single ``asyncio.Lock`` guards cursor + cooldown writes.
    """

    def __init__(self, keys: list[str], cooldown_seconds: float = 60.0) -> None:
        if not keys:
            raise ValueError("KiePool requires at least one key")
        self._states: list[_KeyState] = [_KeyState(k) for k in keys]
        self._cooldown_seconds = cooldown_seconds
        self._cursor = 0
        self._lock = asyncio.Lock()
        _log.info(
            "kie_pool_init",
            key_count=len(keys),
            key_suffixes=[s.suffix for s in self._states],
            cooldown_seconds=cooldown_seconds,
        )

    async def acquire(self) -> str:
        """Return the next available key. Blocks (with backoff) if all are in cooldown."""
        backoff = 0.1
        while True:
            async with self._lock:
                now = time.monotonic()
                # Try every key in round-robin order before sleeping.
                for _ in range(len(self._states)):
                    state = self._states[self._cursor % len(self._states)]
                    self._cursor += 1
                    if state.is_available(now):
                        return state.key
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 5.0)

    async def mark_rate_limited(self, key: str) -> None:
        async with self._lock:
            for state in self._states:
                if state.key == key:
                    state.cooldown_until = time.monotonic() + self._cooldown_seconds
                    _log.warning(
                        "kie_key_cooldown",
                        key_suffix=state.suffix,
                        cooldown_seconds=self._cooldown_seconds,
                    )
                    return

    def find_by_suffix(self, suffix: str) -> str | None:
        """Return the full key matching a key suffix, or None."""
        for state in self._states:
            if state.suffix == suffix:
                return state.key
        return None


# ── Client ───────────────────────────────────────────────────────────────────


class KieClient:
    """Async kie.ai client: submit + poll with key pinning."""

    def __init__(
        self,
        pool: KiePool,
        base_url: str = "https://api.kie.ai",
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._pool = pool
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(timeout=self._timeout)

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    async def __aenter__(self) -> KieClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def create_task(self, model: str, input_params: dict[str, Any]) -> str:
        """Submit a task. Returns a pinned task_id (must be passed to ``poll_task``)."""
        key = await self._pool.acquire()
        url = f"{self._base_url}/api/v1/jobs/createTask"
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        payload = {"model": model, "input": input_params}
        _log.info(
            "kie_submit",
            model=model,
            key_suffix=_key_suffix(key),
            prompt_chars=len(str(input_params.get("prompt", ""))),
        )
        resp = await self._client.post(url, json=payload, headers=headers)
        if resp.status_code == 401:
            raise KieAuthError(f"kie.ai 401 for key {_key_suffix(key)}")
        if resp.status_code == 429:
            await self._pool.mark_rate_limited(key)
            raise KieRateLimitError(f"kie.ai 429 for key {_key_suffix(key)}")
        if resp.status_code != 200:
            raise KieError(
                f"kie.ai submit HTTP {resp.status_code}: {resp.text[:200]}"
            )
        body = resp.json()
        if body.get("code") != 200:
            raise KieError(f"kie.ai submit body code != 200: {body}")
        data = body.get("data") or {}
        task_id = data.get("taskId")
        if not task_id:
            raise KieError(f"kie.ai submit missing taskId: {body}")
        pinned = _pin_task_id(task_id, key)
        _log.info(
            "kie_submit_ok",
            model=model,
            task_id=task_id,
            key_suffix=_key_suffix(key),
        )
        return pinned

    async def poll_task(
        self,
        pinned_task_id: str,
        max_attempts: int = 60,
        delay_seconds: float = 5.0,
    ) -> list[str]:
        """Poll until success / fail / timeout. Returns the result URLs.

        Routes the poll back to the submitting key via the pinned suffix.
        """
        real_task_id, suffix = _unpin_task_id(pinned_task_id)
        key = self._pool.find_by_suffix(suffix) if suffix else None
        if key is None:
            # No pin (or the key is gone). Fall back to any pool key; kie.ai
            # will probably return "task not found" but at least we try.
            _log.warning(
                "kie_poll_no_pinned_key",
                task_id=real_task_id,
                requested_suffix=suffix,
            )
            key = await self._pool.acquire()

        url = f"{self._base_url}/api/v1/jobs/recordInfo"
        headers = {"Authorization": f"Bearer {key}"}
        params = {"taskId": real_task_id}

        for attempt in range(max_attempts):
            resp = await self._client.get(url, headers=headers, params=params)
            if resp.status_code != 200:
                if attempt == max_attempts - 1:
                    raise KieError(
                        f"kie.ai poll HTTP {resp.status_code} "
                        f"after {max_attempts} attempts (task {real_task_id})"
                    )
                await asyncio.sleep(delay_seconds)
                continue

            body = resp.json()
            data = body.get("data") or {}
            state = data.get("state")

            if state == "success":
                result_json_str = data.get("resultJson") or "{}"
                try:
                    result_json = json.loads(result_json_str)
                except json.JSONDecodeError as e:
                    raise KieError(f"kie.ai resultJson parse error: {e}") from e
                urls = result_json.get("resultUrls") or []
                if not urls:
                    raise KieError(
                        f"kie.ai task {real_task_id} success but resultUrls empty"
                    )
                _log.info(
                    "kie_poll_ok",
                    task_id=real_task_id,
                    key_suffix=_key_suffix(key),
                    attempts=attempt + 1,
                    url_count=len(urls),
                )
                return urls

            if state == "fail":
                msg = data.get("failMsg") or "unknown"
                _log.error(
                    "kie_poll_fail",
                    task_id=real_task_id,
                    key_suffix=_key_suffix(key),
                    fail_msg=msg,
                )
                raise KieTaskFailedError(
                    f"kie.ai task {real_task_id} failed: {msg}"
                )

            # waiting / queuing / generating -> keep polling
            _log.debug(
                "kie_poll_pending",
                task_id=real_task_id,
                state=state,
                attempt=attempt + 1,
            )
            if attempt < max_attempts - 1:
                await asyncio.sleep(delay_seconds)

        raise KieTimeoutError(
            f"kie.ai task {real_task_id} did not complete within {max_attempts} attempts"
        )


# ── High-level wrappers ──────────────────────────────────────────────────────


async def nano_banana_edit(
    client: KieClient,
    source_image_url: str,
    prompt: str,
    aspect_ratio: str,
    output_format: str = "png",
    max_attempts: int = 60,
    delay_seconds: float = 5.0,
) -> tuple[str, float]:
    """Generate a 2x2 collage from one seed image. Returns ``(url, cost_usd)``."""
    input_params: dict[str, Any] = {
        "prompt": prompt,
        "image_urls": [source_image_url],
        "output_format": output_format,
        "image_size": aspect_ratio,
    }
    task_id = await client.create_task(MODEL_NANO_BANANA_EDIT, input_params)
    urls = await client.poll_task(
        task_id, max_attempts=max_attempts, delay_seconds=delay_seconds
    )
    return urls[0], COST_NANO_BANANA_EDIT_USD


async def nano_banana_2(
    client: KieClient,
    source_image_url: str,
    prompt: str,
    aspect_ratio: str,
    resolution: str = "2K",
    output_format: str = "png",
    max_attempts: int = 60,
    delay_seconds: float = 5.0,
) -> tuple[str, float]:
    """Generate a 2x2 collage with Nano Banana 2 (Gemini 3.1 Flash Image).

    Honors ``aspect_ratio`` natively and renders legible text, so the collage
    comes out at the target shape with the marketing copy intact. Returns
    ``(url, cost_usd)``.
    """
    input_params: dict[str, Any] = {
        "prompt": prompt,
        "image_input": [source_image_url],
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "output_format": output_format,
    }
    task_id = await client.create_task(MODEL_NANO_BANANA_2, input_params)
    urls = await client.poll_task(
        task_id, max_attempts=max_attempts, delay_seconds=delay_seconds
    )
    return urls[0], COST_NANO_BANANA_2_USD


async def gpt_image_2(
    client: KieClient,
    source_image_url: str,
    prompt: str,
    aspect_ratio: str,
    resolution: str = "2K",
    max_attempts: int = 60,
    delay_seconds: float = 5.0,
) -> tuple[str, float]:
    """Fallback collage generation with GPT Image 2 (image-to-image).

    Different input-field name (``input_urls``) and no ``output_format`` —
    per the kie GPT Image 2 image-to-image schema. Returns ``(url, cost_usd)``.
    """
    input_params: dict[str, Any] = {
        "prompt": prompt,
        "input_urls": [source_image_url],
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
    }
    task_id = await client.create_task(MODEL_GPT_IMAGE_2, input_params)
    urls = await client.poll_task(
        task_id, max_attempts=max_attempts, delay_seconds=delay_seconds
    )
    return urls[0], COST_GPT_IMAGE_2_USD


async def recraft_crisp_upscale(
    client: KieClient,
    image_url: str,
    max_attempts: int = 120,
    delay_seconds: float = 3.0,
) -> tuple[str, float]:
    """Upscale an image with recraft/crisp-upscale. Returns ``(url, cost_usd)``."""
    input_params: dict[str, Any] = {"image": image_url}
    task_id = await client.create_task(MODEL_RECRAFT_UPSCALE, input_params)
    urls = await client.poll_task(
        task_id, max_attempts=max_attempts, delay_seconds=delay_seconds
    )
    return urls[0], COST_RECRAFT_UPSCALE_USD


def _nano_banana_2_cost(resolution: str) -> float:
    """Per-image cost for nano-banana-2 by resolution (kie: $0.04/1K, $0.06/2K)."""
    return COST_NANO_BANANA_2_1K_USD if resolution.strip().upper() == "1K" else COST_NANO_BANANA_2_USD


async def nano_banana_2_text_to_image(
    client: KieClient,
    prompt: str,
    aspect_ratio: str,
    resolution: str = "1K",
    output_format: str = "png",
    max_attempts: int = 60,
    delay_seconds: float = 5.0,
) -> tuple[str, float]:
    """Generate an image from text only (NO seed) with Nano Banana 2.

    Used by the cartoon pipeline for the first scene of each video. Returns
    ``(url, cost_usd)``.
    """
    input_params: dict[str, Any] = {
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "output_format": output_format,
    }
    task_id = await client.create_task(MODEL_NANO_BANANA_2, input_params)
    urls = await client.poll_task(
        task_id, max_attempts=max_attempts, delay_seconds=delay_seconds
    )
    return urls[0], _nano_banana_2_cost(resolution)


async def nano_banana_2_image_to_image(
    client: KieClient,
    source_image_url: str,
    prompt: str,
    aspect_ratio: str,
    resolution: str = "1K",
    output_format: str = "png",
    max_attempts: int = 60,
    delay_seconds: float = 5.0,
) -> tuple[str, float]:
    """Generate a new image conditioned on ``source_image_url`` with Nano Banana 2.

    The cartoon pipeline uses this to chain later scenes off the first one so
    the character, palette, and style carry across the cut. Returns
    ``(url, cost_usd)``.
    """
    input_params: dict[str, Any] = {
        "prompt": prompt,
        "image_input": [source_image_url],
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "output_format": output_format,
    }
    task_id = await client.create_task(MODEL_NANO_BANANA_2, input_params)
    urls = await client.poll_task(
        task_id, max_attempts=max_attempts, delay_seconds=delay_seconds
    )
    return urls[0], _nano_banana_2_cost(resolution)


async def seedance_image_to_video(
    client: KieClient,
    image_url: str,
    prompt: str,
    aspect_ratio: str,
    duration: int = 4,
    resolution: str = "720p",
    max_attempts: int = 120,
    delay_seconds: float = 5.0,
) -> tuple[str, float]:
    """Animate one still image into a short clip with Seedance 1.5 Pro.

    ``duration`` must be 4, 8, or 12 (the only values the model accepts) and is
    sent as a STRING — the API rejects an integer ("duration it must be a
    string"). Audio generation is left off (VO is added downstream). Returns
    ``(video_url, cost_usd)`` with the cost matching the duration tier.
    """
    input_params: dict[str, Any] = {
        "prompt": prompt,
        "input_urls": [image_url],
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "duration": str(duration),
    }
    task_id = await client.create_task(MODEL_SEEDANCE_PRO, input_params)
    urls = await client.poll_task(
        task_id, max_attempts=max_attempts, delay_seconds=delay_seconds
    )
    # 8s clips are billed roughly 2x the 4s tier (plan §11; verify next live run).
    # 12s is not used by cartoon mode today — fall through to the 8s cost rather
    # than under-reporting, with a TODO if a 12s path appears later.
    cost = (
        COST_SEEDANCE_PRO_720P_4S_USD if duration == 4
        else COST_SEEDANCE_PRO_720P_8S_USD
    )
    return urls[0], cost


# ── Construction from settings ───────────────────────────────────────────────


def build_client_from_settings(settings: Settings | None = None) -> KieClient:
    """Construct a KieClient with the configured key pool. Raises if no keys."""
    s = settings or get_settings()
    if not s.kie_key_list:
        raise ValueError("KIE_AI_KEYS is empty; cannot build KieClient")
    pool = KiePool(
        s.kie_key_list,
        cooldown_seconds=s.KIE_RATE_LIMIT_COOLDOWN_SECONDS,
    )
    return KieClient(
        pool=pool,
        base_url=s.KIE_BASE_URL,
        connect_timeout=s.KIE_CONNECT_TIMEOUT_SECONDS,
        read_timeout=s.KIE_TIMEOUT_SECONDS,
    )
