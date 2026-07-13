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
# 12s tier — linear from the verified 4s/8s anchors ($0.0175/s). Used by the
# Motion_Ads tab (always-12s silent clip). Verify on the next live run.
COST_SEEDANCE_PRO_720P_12S_USD = 0.21  # Seedance 1.5 Pro i2v @ 720p, 12s, no audio

# Production model identifiers.
MODEL_NANO_BANANA_EDIT = "google/nano-banana-edit"
MODEL_NANO_BANANA_2 = "nano-banana-2"
MODEL_GPT_IMAGE_2 = "gpt-image-2-image-to-image"
MODEL_RECRAFT_UPSCALE = "recraft/crisp-upscale"
MODEL_SEEDANCE_PRO = "bytedance/seedance-1.5-pro"

# Seedance 1.5 Pro accepts ONLY these aspect ratios (verified on kie.ai
# 2026-07-07). A value outside the set is rejected at SUBMIT with HTTP 200 +
# body ``{"code": 500, "msg": "aspect_ratio is not within the range of allowed
# options"}``. Crucially this is a STRICT SUBSET of the image models' / Rendi's
# valid ratios (``rendi.VALID_RATIO_STRINGS`` also allows 2:3, 3:2, 4:5, 5:4):
# nano-banana happily generates a 2:3 image, so the images succeed and ONLY the
# Seedance video submit dies — surfacing as the row's "no Seedance clips
# produced for any of N shots". Every caller's aspect is clamped to the nearest
# entry here at the wrapper boundary so no tab can trip the rejection.
SEEDANCE_ALLOWED_ASPECT_RATIOS: dict[str, float] = {
    "9:16": 9 / 16,
    "3:4": 3 / 4,
    "1:1": 1.0,
    "4:3": 4 / 3,
    "16:9": 16 / 9,
    "21:9": 21 / 9,
}
SEEDANCE_DEFAULT_ASPECT_RATIO = "9:16"


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


# Throttle for the "all keys cooling" warning so a sustained rate-limit
# starvation shows up in logs once every N seconds instead of on every
# acquire-spin. Plan ``_plans/2026-07-06-stuck-runs-worker-wedge.md`` §Fix 4.
_ALL_COOLING_WARN_INTERVAL_SECONDS = 30.0


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
        # Last time we logged "all keys cooling" (monotonic; throttled).
        self._last_all_cooling_warn = 0.0
        _log.info(
            "kie_pool_init",
            key_count=len(keys),
            key_suffixes=[s.suffix for s in self._states],
            cooldown_seconds=cooldown_seconds,
        )

    @property
    def key_count(self) -> int:
        """Number of keys in the pool (cooled or not). Stable for the pool's lifetime."""
        return len(self._states)

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
                # Every key is in cooldown. A throttled warning makes KIE
                # throughput starvation — a prime "runs feel stuck" cause with a
                # single key under high row concurrency — visible in the logs
                # without flooding them. Plan
                # ``_plans/2026-07-06-stuck-runs-worker-wedge.md`` §Fix 4.
                if now - self._last_all_cooling_warn >= _ALL_COOLING_WARN_INTERVAL_SECONDS:
                    self._last_all_cooling_warn = now
                    soonest = min(s.cooldown_until for s in self._states)
                    _log.warning(
                        "kie_pool_all_keys_cooling",
                        key_count=len(self._states),
                        wait_seconds=round(max(0.0, soonest - now), 1),
                    )
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

    # Hard cap on the in-`create_task` retry loop. kie.ai signals rate-limit
    # in two ways (HTTP 429 OR body code 429); on either, the offending key is
    # placed on cooldown and we re-acquire the next available key. Cap at 3 so
    # a pool with many keys can't burn the whole batch of attempts on a single
    # row before the outer fallback chain (gpt-image-2, AtlasCloud) kicks in.
    # Plan: ``_plans/2026-06-11-kie-body-code-429.md``.
    _MAX_RATE_LIMIT_RETRIES = 3

    async def create_task(self, model: str, input_params: dict[str, Any]) -> str:
        """Submit a task. Returns a pinned task_id (must be passed to ``poll_task``).

        On rate-limit (HTTP 429 OR body ``{"code": 429, ...}`` over HTTP 200),
        cools the key down and retries with the next available pool key, up to
        ``min(pool.key_count, _MAX_RATE_LIMIT_RETRIES)`` attempts. After the
        cap, the last ``KieRateLimitError`` propagates so the caller's fallback
        chain can run. See ``_plans/2026-06-11-kie-body-code-429.md``.
        """
        max_attempts = min(self._pool.key_count, self._MAX_RATE_LIMIT_RETRIES)
        last_err: KieRateLimitError | None = None
        for attempt in range(max_attempts):
            key = await self._pool.acquire()
            try:
                return await self._submit_once(model, input_params, key)
            except KieRateLimitError as e:
                last_err = e
                if attempt < max_attempts - 1:
                    _log.info(
                        "kie_submit_retry_after_429",
                        model=model,
                        key_suffix=_key_suffix(key),
                        attempt=attempt + 1,
                        max_attempts=max_attempts,
                    )
        assert last_err is not None    # max_attempts >= 1 because pool has >=1 key
        raise last_err

    async def _submit_once(
        self, model: str, input_params: dict[str, Any], key: str
    ) -> str:
        """Single-attempt submission with one specific key. Returns a pinned task_id.

        Raises ``KieRateLimitError`` on HTTP 429 OR body-code 429 (after
        cooling the key); ``KieAuthError`` on 401; ``KieError`` on anything
        else non-200. Extracted from ``create_task`` so the rate-limit retry
        loop above can iterate cleanly.
        """
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
        body_code = body.get("code")
        # kie.ai signals per-key rate-limit by returning HTTP 200 with body
        # ``{"code": 429, "msg": ...}`` (verified 2026-06-11 on prod batch).
        # Treat it identically to an HTTP-status 429 so the cooldown + retry
        # logic kicks in instead of slamming the same tripped key.
        if body_code == 429:
            await self._pool.mark_rate_limited(key)
            msg = str(body.get("msg") or "rate limit exceeded")[:200]
            _log.warning(
                "kie_submit_body_code_429",
                model=model,
                key_suffix=_key_suffix(key),
                body_msg=msg,
            )
            raise KieRateLimitError(
                f"kie.ai body 429 for key {_key_suffix(key)}: {msg}"
            )
        if body_code != 200:
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
            last_attempt = attempt == max_attempts - 1

            # ── Read-back resilience (cartoon "no Seedance clips" bug) ──
            # The task may already have SUCCEEDED on kie's side; a flaky
            # read-back must NOT be reported as a failed clip. Every transient
            # condition below re-polls within the same attempt budget and only
            # surfaces (as ``KieTimeoutError`` — "couldn't retrieve in time",
            # which the seedance wrapper retries) once attempts are exhausted.

            # (a) Network flap on the GET: read/pool/connect timeout, reset,
            #     protocol error. Uncaught, this instantly killed a finished
            #     clip. httpx.TransportError is the base of all of these.
            try:
                resp = await self._client.get(url, headers=headers, params=params)
            except httpx.TransportError as e:
                if last_attempt:
                    raise KieTimeoutError(
                        f"kie.ai poll transport error after {max_attempts} "
                        f"attempts (task {real_task_id}): {type(e).__name__}: {e}"
                    ) from e
                _log.warning(
                    "kie_poll_transient_error",
                    task_id=real_task_id,
                    key_suffix=_key_suffix(key),
                    error_type=type(e).__name__,
                    attempt=attempt + 1,
                )
                await asyncio.sleep(delay_seconds)
                continue

            # (b) Rate-limited poll, HTTP-status form. kie signals a per-key
            #     poll rate-limit as HTTP 429; cool the key (backpressure on
            #     other callers) and keep polling THIS pinned task — a 429 on
            #     recordInfo is never a terminal result.
            if resp.status_code == 429:
                await self._pool.mark_rate_limited(key)
                if last_attempt:
                    raise KieTimeoutError(
                        f"kie.ai poll rate-limited (HTTP 429) through "
                        f"{max_attempts} attempts (task {real_task_id})"
                    )
                _log.warning(
                    "kie_poll_rate_limited",
                    task_id=real_task_id,
                    key_suffix=_key_suffix(key),
                    signal="http_429",
                    attempt=attempt + 1,
                )
                await asyncio.sleep(delay_seconds)
                continue

            if resp.status_code != 200:
                if last_attempt:
                    raise KieError(
                        f"kie.ai poll HTTP {resp.status_code} "
                        f"after {max_attempts} attempts (task {real_task_id})"
                    )
                await asyncio.sleep(delay_seconds)
                continue

            # (c) Unparseable 200 body (transient edge/proxy HTML, truncated
            #     JSON). Treat as transient rather than crashing the clip.
            try:
                body = resp.json()
            except (json.JSONDecodeError, ValueError) as e:
                if last_attempt:
                    raise KieTimeoutError(
                        f"kie.ai poll unparseable body after {max_attempts} "
                        f"attempts (task {real_task_id}): {e}"
                    ) from e
                _log.warning(
                    "kie_poll_bad_json",
                    task_id=real_task_id,
                    key_suffix=_key_suffix(key),
                    attempt=attempt + 1,
                )
                await asyncio.sleep(delay_seconds)
                continue

            # (d) Rate-limited poll, HTTP-200 body-code form. kie ALSO signals
            #     rate-limit as HTTP 200 + ``{"code": 429, ...}`` (same dual
            #     pattern as submit — see ``_submit_once``). Without this the
            #     body has no ``data``, ``state`` reads as None, the poll spins
            #     to KieTimeoutError 10 minutes later, and the operator sees
            #     "no Seedance clips produced" for a clip that was READY. This
            #     is the reported bug.
            if body.get("code") == 429:
                await self._pool.mark_rate_limited(key)
                if last_attempt:
                    raise KieTimeoutError(
                        f"kie.ai poll rate-limited (body code 429) through "
                        f"{max_attempts} attempts (task {real_task_id})"
                    )
                _log.warning(
                    "kie_poll_rate_limited",
                    task_id=real_task_id,
                    key_suffix=_key_suffix(key),
                    signal="body_429",
                    attempt=attempt + 1,
                )
                await asyncio.sleep(delay_seconds)
                continue

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


# Substrings in a kie ``failMsg`` that mark a RETRYABLE server-side blip (vs a
# deterministic rejection — content-policy block, bad input — that a resubmit
# would only repeat). Matched case-insensitively. The reported upscale outage
# was ``failMsg='internal error, please try again later.'`` — both phrases are
# covered here. Plan: ``_plans/2026-07-12-upscale-resilience-tavily-removal.md``.
_TRANSIENT_KIE_FAIL_MARKERS = (
    "internal error",
    "try again",
    "timeout",
    "timed out",
    "server error",
    "temporarily",
    "please retry",
)


def _is_transient_kie_fail(message: str) -> bool:
    """True when a ``KieTaskFailedError`` message looks like a server-side blip."""
    m = message.lower()
    return any(marker in m for marker in _TRANSIENT_KIE_FAIL_MARKERS)


async def recraft_crisp_upscale(
    client: KieClient,
    image_url: str,
    max_attempts: int = 120,
    delay_seconds: float = 3.0,
    retries: int = 2,
    retry_backoff_seconds: float = 2.0,
) -> tuple[str, float]:
    """Upscale an image with recraft/crisp-upscale. Returns ``(url, cost_usd)``.

    Resilience: the whole submit+poll is retried up to ``retries`` extra times
    (default 2) on a TRANSIENT kie failure — a ``KieTaskFailedError`` whose
    ``failMsg`` looks server-side (the reported one was "internal error, please
    try again later."), a ``KieTimeoutError`` (task never landed), or a
    submit-time network flap. A single transient recraft blip used to kill the
    whole row at ``IMAGE_GEN_FAILED`` even though kie itself said "please try
    again later"; the caller ALSO keeps the un-upscaled collage as a last
    resort. Not retried: ``KieRateLimitError`` (every key is cooling — an
    immediate resubmit just hits cooled keys) and a NON-transient
    ``KieTaskFailedError`` (a deterministic rejection). Mirrors
    ``seedance_image_to_video``'s timeout-resubmit. Plan:
    ``_plans/2026-07-12-upscale-resilience-tavily-removal.md``.
    """
    input_params: dict[str, Any] = {"image": image_url}
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            task_id = await client.create_task(MODEL_RECRAFT_UPSCALE, input_params)
            urls = await client.poll_task(
                task_id, max_attempts=max_attempts, delay_seconds=delay_seconds
            )
            return urls[0], COST_RECRAFT_UPSCALE_USD
        except KieTaskFailedError as e:
            # Only a transient server-side blip is worth a resubmit; a
            # deterministic rejection is re-raised as-is so we don't burn
            # attempts (and money) repeating a failure that won't change.
            if not _is_transient_kie_fail(str(e)):
                raise
            last_exc = e
        except (KieTimeoutError, httpx.TransportError) as e:
            last_exc = e
        if attempt < retries:
            _log.warning(
                "recraft_upscale_retry",
                attempt=attempt + 1,
                total=retries + 1,
                error=f"{type(last_exc).__name__}: {str(last_exc)[:150]}",
            )
            await asyncio.sleep(retry_backoff_seconds)
    # Retries exhausted — surface the last transient error so the caller can
    # fall back to the un-upscaled collage rather than failing the row.
    assert last_exc is not None
    raise last_exc


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


def _aspect_to_float(aspect: str) -> float | None:
    """Parse ``"W:H"`` (ratio) or ``"WxH"`` (pixels) into a numeric ratio.

    Returns None for empty / unparseable / non-positive input. Tolerates the
    Sheets leading-zero cast (``"09:16"``) since ``float`` ignores it.
    """
    s = (aspect or "").strip().lower()
    for sep in (":", "x"):
        if sep in s:
            left, _, right = s.partition(sep)
            try:
                w, h = float(left), float(right)
            except ValueError:
                return None
            if w <= 0 or h <= 0:
                return None
            return w / h
    return None


def nearest_seedance_aspect_ratio(aspect: str) -> str:
    """Snap any aspect string to the nearest Seedance-allowed ratio.

    An operator-picked ``2:3`` (or a native-probed ``1080x1620``) is valid for
    the image models but rejected by Seedance at submit. Returns the allowed
    ratio numerically closest to the input; passes an already-allowed value
    through unchanged; falls back to ``SEEDANCE_DEFAULT_ASPECT_RATIO`` when the
    input can't be parsed. See ``SEEDANCE_ALLOWED_ASPECT_RATIOS``.
    """
    s = (aspect or "").strip()
    if s in SEEDANCE_ALLOWED_ASPECT_RATIOS:
        return s
    target = _aspect_to_float(s)
    if target is None:
        return SEEDANCE_DEFAULT_ASPECT_RATIO
    return min(
        SEEDANCE_ALLOWED_ASPECT_RATIOS,
        key=lambda r: abs(SEEDANCE_ALLOWED_ASPECT_RATIOS[r] - target),
    )


async def seedance_image_to_video(
    client: KieClient,
    image_url: str,
    prompt: str,
    aspect_ratio: str,
    duration: int = 4,
    resolution: str = "720p",
    generate_audio: bool = False,
    max_attempts: int = 120,
    delay_seconds: float = 5.0,
    retries: int = 1,
) -> tuple[str, float]:
    """Animate one still image into a short clip with Seedance 1.5 Pro.

    ``duration`` must be 4, 8, or 12 (the only values the model accepts) and is
    sent as a STRING — the API rejects an integer ("duration it must be a
    string"). Returns ``(video_url, cost_usd)`` with the cost matching the
    duration tier.

    ``generate_audio`` defaults to False. Seedance **1.5 Pro** is a native
    audio-visual model (unlike 1.0), so it will synthesize a soundtrack unless
    told not to; every caller here adds its own audio downstream (or wants pure
    silence — Motion_Ads), so we send ``generate_audio=false`` explicitly rather
    than trust the provider default, which is both cheaper and guaranteed silent.

    Resilience: submit + poll are retried ``retries`` extra times (default 1)
    on a genuine ``KieTimeoutError`` (task never finished / poll couldn't
    retrieve it in the window) or a submit-time network flap. ``poll_task``
    already survives transient read-back errors internally, so a retry here
    only fires when the whole clip truly didn't land — the difference between
    dropping the clip (and the row's "no Seedance clips produced" failure) and
    re-driving it once. Mirrors Rendi's ``_submit_and_poll`` timeout-resubmit.
    Not retried: ``KieRateLimitError`` on submit (every key is cooling — an
    immediate retry just hits cooled keys), ``KieTaskFailedError`` and other
    ``KieError`` (a resubmit would only repeat a deterministic failure).
    """
    # Clamp to a Seedance-allowed ratio (2:3 / 3:2 / 4:5 / WxH would be rejected
    # at submit — the "no Seedance clips produced" bug). Loud when it changes so
    # an operator picking an unsupported size sees WHY the video shape shifted.
    seedance_aspect = nearest_seedance_aspect_ratio(aspect_ratio)
    if seedance_aspect != aspect_ratio:
        _log.info(
            "seedance_aspect_clamped",
            requested=aspect_ratio,
            used=seedance_aspect,
        )
    input_params: dict[str, Any] = {
        "prompt": prompt,
        "input_urls": [image_url],
        "aspect_ratio": seedance_aspect,
        "resolution": resolution,
        "duration": str(duration),
        "generate_audio": generate_audio,
    }
    # Cost by duration tier (plan §11; verify next live run). The 12s tier is
    # used by the Motion_Ads tab (always-12s silent clip); billing it at the 8s
    # cost used to under-report by ~$0.07/clip.
    cost = (
        COST_SEEDANCE_PRO_720P_4S_USD if duration == 4
        else COST_SEEDANCE_PRO_720P_12S_USD if duration == 12
        else COST_SEEDANCE_PRO_720P_8S_USD
    )
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            task_id = await client.create_task(MODEL_SEEDANCE_PRO, input_params)
            urls = await client.poll_task(
                task_id, max_attempts=max_attempts, delay_seconds=delay_seconds
            )
            return urls[0], cost
        except (KieTimeoutError, httpx.TransportError) as e:
            last_exc = e
            if attempt < retries:
                _log.warning(
                    "seedance_retry_after_timeout",
                    attempt=attempt + 1,
                    total=retries + 1,
                    error=f"{type(e).__name__}: {str(e)[:150]}",
                )
                continue
            raise
    # Unreachable: the loop runs at least once and always returns or raises.
    assert last_exc is not None
    raise last_exc


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
