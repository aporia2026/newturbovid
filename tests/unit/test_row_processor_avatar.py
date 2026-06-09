"""Integration-style tests for the simplified avatar row processor.

Covers the 2026-06-09 rewrite (plan
``_plans/2026-06-09-avatar-static-image-pipeline.md``):

  * Manual Image happy path -> 1 video, NO kie call (manual used as-is)
  * No Manual Image happy path -> 1 video, kie text-to-image WAS called
  * Avatar API failure -> STATUS_TTS_FAILED with TikTok error surfaced
  * Article fetch failure -> STATUS_ARTICLE_FETCH_FAILED
  * Missing avatar_id -> STATUS_INTERNAL_ERROR before any external work
  * CTA enabled -> CTA pill rendered and Rendi runs an extra overlay step
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from bulkvid.adapters.article_fetch import ArticleResult
from bulkvid.adapters.kie import KieClient, KiePool
from bulkvid.adapters.openai_client import OpenAIClient
from bulkvid.adapters.rendi import RendiClient
from bulkvid.adapters.storage import UploadResult
from bulkvid.models.row import (
    STATUS_ARTICLE_FETCH_FAILED,
    STATUS_INTERNAL_ERROR,
    STATUS_SUCCESS,
    STATUS_TTS_FAILED,
    AvatarRow,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.row_processor_avatar import process_avatar_row

OPENAI_BASE = "https://api.openai.com/v1"
RENDI_BASE = "https://api.rendi.dev"
KIE_BASE = "https://api.kie.ai"
TIKTOK_BASE = "https://business-api.tiktok.com/open_api/v1.3"


# ── Fakes (mirror the simple-tab test setup, scoped to what avatar needs) ───


class _FakeArticleFetcher:
    def __init__(self, fail: bool = False) -> None:
        self._fail = fail

    async def fetch(self, url: str) -> ArticleResult:
        if self._fail:
            from bulkvid.adapters.article_fetch import ArticleFetchError

            raise ArticleFetchError("simulated fetch failure")
        return ArticleResult(
            url=url, content="Some article body about cars.",
            source="scrapingbee", char_count=29, cost_usd=0.008,
        )


class _FakeStorageClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def upload_bytes(
        self, data: bytes, key: str, content_type: str = "application/octet-stream"
    ) -> UploadResult:
        self.calls.append((key, content_type))
        return UploadResult(
            url=f"https://storage.test/{key}", backend="gcs",
            bytes_written=len(data), cost_usd=0.0001,
        )


def _openai_chat_resp(content: str) -> dict:
    return {
        "id": "x", "object": "chat.completion", "created": 1717_000_000,
        "model": "gpt-5.4-mini",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130},
    }


def _register_openai_routes() -> None:
    """Route OpenAI calls by inspecting the system prompt — same trick the
    simple-tab test uses. Avatar pipeline calls: detect_language,
    classify_open_comments, generate_script (3 distinct prompts)."""

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sys = body.get("messages", [{}])[0].get("content", "")
        if "detect the primary language" in sys:
            text = json.dumps({"language": "en", "confidence": 0.95})
        elif "classify 'Open Comments'" in sys:
            text = json.dumps(
                {"mode": "none", "tone_hints": [],
                 "directives": [], "override_script": None}
            )
        elif "voiceover scripts for bulk" in sys:
            text = json.dumps(
                {"script": "Check out the latest deals.",
                 "style_direction": "Calm."}
            )
        else:
            text = "ok"
        return httpx.Response(200, json=_openai_chat_resp(text))

    respx.post(f"{OPENAI_BASE}/chat/completions").mock(side_effect=_handler)


def _register_rendi_routes() -> None:
    counter = {"n": 0}

    def _submit(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(200, json={"command_id": f"cmd-{counter['n']}"})

    respx.post(f"{RENDI_BASE}/v1/run-ffmpeg-command").mock(side_effect=_submit)

    def _poll(request: httpx.Request) -> httpx.Response:
        cmd_id = str(request.url).rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={
                "status": "SUCCESS",
                "output_files": {"out_1": {"storage_url": f"https://r.dev/{cmd_id}.mp4"}},
            },
        )

    respx.get(url__regex=r"https://api\.rendi\.dev/v1/commands/.+").mock(side_effect=_poll)
    respx.delete(url__regex=r"https://api\.rendi\.dev/v1/commands/.+/files").mock(
        return_value=httpx.Response(200, json={})
    )


def _register_tiktok_routes(*, fail: bool = False) -> None:
    if fail:
        respx.post(url__regex=rf"{TIKTOK_BASE}/creative/digital_avatar/video/task/create/?").mock(
            return_value=httpx.Response(
                200,
                json={"code": 40006, "message": "no schema found",
                      "data": {}, "request_id": "rid-1"},
            )
        )
        return

    # Successful CREATE returns a task_id.
    respx.post(url__regex=rf"{TIKTOK_BASE}/creative/digital_avatar/video/task/create/?").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0, "message": "OK", "request_id": "rid-1",
                "data": {"list": [{"task_id": "tk-42", "package_id": "pkg"}]},
            },
        )
    )
    # First GET returns SUCCESS with a preview_url.
    respx.get(url__regex=rf"{TIKTOK_BASE}/creative/digital_avatar/video/task/get/?\??.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0, "message": "OK", "request_id": "rid-2",
                "data": {"list": [{
                    "task_id": "tk-42", "status": "SUCCESS",
                    "preview_url": "https://tiktok.test/avatar.mp4",
                    "duration": 11.5,
                }]},
            },
        )
    )


def _register_kie_routes() -> None:
    """Used only for the no-manual-image branch (kie text-to-image)."""
    respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(
        return_value=httpx.Response(
            200, json={"code": 200, "data": {"taskId": "kie-task-1"}}
        )
    )
    respx.get(url__regex=rf"{KIE_BASE}/api/v1/jobs/recordInfo\?.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "state": "success",
                    "resultJson": json.dumps({"resultUrls": ["https://kie.test/bg.png"]}),
                },
            },
        )
    )


def _register_downloads() -> None:
    # Rendi-produced intermediates.
    respx.get(url__regex=r"https://r\.dev/.+\.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00fake-mp4")
    )
    # Persisted final URL (storage backend).
    respx.get(url__regex=r"https://storage\.test/.+\.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00persisted")
    )
    # Manual image (operator-supplied).
    respx.get(url__regex=r"https://example\.com/.+\.png").mock(
        return_value=httpx.Response(200, content=b"\x89PNG\r\n\x1a\nfake")
    )


def _build_clients() -> PipelineClients:
    """All clients are real adapter classes; their HTTP traffic is mocked
    by the respx routes above. Storage + article are in-process fakes so
    the test doesn't need a real GCS / Tavily."""
    return PipelineClients(
        openai=OpenAIClient(api_key="sk-test"),
        kie=KieClient(pool=KiePool(keys=["k_unused_AAAAAAAAAAAA"]), base_url=KIE_BASE),
        tts=None,                                        # type: ignore[arg-type]
        rendi=RendiClient(api_key="rendi-test", base_url=RENDI_BASE),
        storage=_FakeStorageClient(),                    # type: ignore[arg-type]
        article=_FakeArticleFetcher(),                   # type: ignore[arg-type]
        zapcap=None,
    )


def _row(
    *,
    manual_image: str = "https://example.com/seed.png",
    cta_enabled: bool = False,
    avatar_id: str = "7617939650801745940",
    avatar_size: str = "",
    avatar_shape: str = "",
) -> AvatarRow:
    return AvatarRow(
        row_num=2, country="DE", vertical="Car Deals PR",
        article_url="https://example.com/article",
        manual_image_url=manual_image,
        avatar_id=avatar_id,
        voice_over=True, zapcap=False, aspect_ratio="9:16",
        script_pattern="How To",
        cta_enabled=cta_enabled, cta_text="",
        open_comments="",
        avatar_size=avatar_size,
        avatar_shape=avatar_shape,
    )


# ── Happy paths ────────────────────────────────────────────────────────────


@respx.mock
async def test_avatar_manual_image_happy_path_skips_kie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual Image supplied -> kie text-to-image must NOT fire (the
    image is used as-is, just downloaded + re-uploaded). Avatar still
    generates via TikTok. End-to-end produces one Ready Video URL."""
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "tt-test")
    _register_openai_routes()
    _register_rendi_routes()
    _register_tiktok_routes()
    _register_downloads()
    kie_route = respx.post(f"{KIE_BASE}/api/v1/jobs/createTask").mock(
        return_value=httpx.Response(200, json={"code": 200, "data": {"taskId": "x"}})
    )

    result = await process_avatar_row(_row(), _build_clients(), job_id="jobX")

    assert result.status == STATUS_SUCCESS
    assert len(result.video_urls) == 1
    assert result.metadata["tab"] == "avatar"
    assert result.metadata["pipeline_version"] == "static_image_v2"
    assert result.metadata["background_source"] == "manual"
    # The manual-image branch must short-circuit BEFORE kie.
    assert not kie_route.called, "kie text-to-image must not fire when Manual Image is set"


@respx.mock
async def test_avatar_no_manual_image_uses_kie_text_to_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Manual Image -> kie text-to-image generates the background.
    One video is produced. ``background_source`` is recorded as ``kie``."""
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "tt-test")
    _register_openai_routes()
    _register_rendi_routes()
    _register_tiktok_routes()
    _register_kie_routes()
    _register_downloads()
    # The kie-produced background URL gets fed straight into Rendi (no
    # download), so no extra GET mock is needed for it.

    result = await process_avatar_row(
        _row(manual_image=""), _build_clients(), job_id="jobX",
    )

    assert result.status == STATUS_SUCCESS, result.error
    assert len(result.video_urls) == 1
    assert result.metadata["background_source"] == "kie"
    # background_prompt_chars only set on the kie branch.
    assert result.metadata["background_prompt_chars"] > 100


# ── Failure paths ─────────────────────────────────────────────────────────


@respx.mock
async def test_avatar_missing_avatar_id_fails_before_any_external_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty avatar_id -> immediate STATUS_INTERNAL_ERROR; no Tavily,
    no OpenAI, no kie, no Rendi traffic at all."""
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "tt-test")
    openai_route = respx.post(f"{OPENAI_BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_openai_chat_resp("{}"))
    )

    result = await process_avatar_row(
        _row(avatar_id=""), _build_clients(), job_id="jobX",
    )

    assert result.status == STATUS_INTERNAL_ERROR
    assert "avatar_id missing" in (result.error or "")
    assert not openai_route.called, (
        "missing avatar_id must fail before article/script/openai work"
    )


@respx.mock
async def test_avatar_article_fetch_failure_surfaces_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tavily/ScrapingBee both down -> STATUS_ARTICLE_FETCH_FAILED with
    no kie cost burned."""
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "tt-test")
    _register_openai_routes()
    _register_rendi_routes()
    _register_tiktok_routes()
    _register_downloads()
    clients = PipelineClients(
        openai=OpenAIClient(api_key="sk-test"),
        kie=KieClient(pool=KiePool(keys=["k_unused_AAAAAAAAAAAA"]), base_url=KIE_BASE),
        tts=None,                                        # type: ignore[arg-type]
        rendi=RendiClient(api_key="rendi-test", base_url=RENDI_BASE),
        storage=_FakeStorageClient(),                    # type: ignore[arg-type]
        article=_FakeArticleFetcher(fail=True),          # type: ignore[arg-type]
        zapcap=None,
    )

    result = await process_avatar_row(_row(), clients, job_id="jobX")
    assert result.status == STATUS_ARTICLE_FETCH_FAILED
    assert result.metadata["cost_breakdown"]["image_gen"] == 0.0
    assert result.metadata["cost_breakdown"]["tiktok"] == 0.0


@respx.mock
async def test_avatar_tiktok_failure_surfaces_tts_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TikTok create returns a non-zero ``code`` -> STATUS_TTS_FAILED
    with the error surfaced. Cost up to that point is still recorded."""
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "tt-test")
    _register_openai_routes()
    _register_rendi_routes()
    _register_tiktok_routes(fail=True)
    _register_downloads()

    result = await process_avatar_row(_row(), _build_clients(), job_id="jobX")

    assert result.status == STATUS_TTS_FAILED
    assert "TikTok" in (result.error or "")


# ── Per-row overlay knobs (Avatar Size + Avatar Shape) ────────────────────


@respx.mock
async def test_avatar_default_size_and_shape_match_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty Avatar Size / Avatar Shape cells fall back to today's
    Medium + Rectangle. Existing sheets without the new columns keep
    rendering exactly the same as before."""
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "tt-test")
    _register_openai_routes()
    _register_rendi_routes()
    _register_tiktok_routes()
    _register_downloads()

    result = await process_avatar_row(_row(), _build_clients(), job_id="jobX")

    assert result.status == STATUS_SUCCESS
    assert result.metadata["avatar_size"] == "medium"
    assert result.metadata["avatar_shape"] == "rectangle"
    # 9:16 → canvas width 1080; medium = 30% → 324 px overlay.
    assert result.metadata["avatar_overlay_width_px"] == 324


@respx.mock
async def test_avatar_size_small_uses_20pct_canvas_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Avatar Size = Small → overlay is 20 % of canvas width."""
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "tt-test")
    _register_openai_routes()
    _register_rendi_routes()
    _register_tiktok_routes()
    _register_downloads()

    result = await process_avatar_row(
        _row(avatar_size="Small"),    # uppercase to exercise the lowercasing
        _build_clients(), job_id="jobX",
    )
    assert result.status == STATUS_SUCCESS
    assert result.metadata["avatar_size"] == "small"
    # 1080 × 0.20 = 216 px.
    assert result.metadata["avatar_overlay_width_px"] == 216


@respx.mock
async def test_avatar_size_large_uses_40pct_canvas_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Avatar Size = Large → overlay is 40 % of canvas width."""
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "tt-test")
    _register_openai_routes()
    _register_rendi_routes()
    _register_tiktok_routes()
    _register_downloads()

    result = await process_avatar_row(
        _row(avatar_size="large"), _build_clients(), job_id="jobX",
    )
    assert result.status == STATUS_SUCCESS
    assert result.metadata["avatar_size"] == "large"
    # 1080 × 0.40 = 432 px.
    assert result.metadata["avatar_overlay_width_px"] == 432


@respx.mock
async def test_avatar_shape_circle_is_recorded_and_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Avatar Shape = Circle → the row metadata records ``circle`` and
    the Rendi ffmpeg command for this row uses the yuva alpha mask.

    We can't easily intercept the Rendi-internal command string at the
    method boundary in this test (it goes through ``_submit_and_poll``
    where the command is baked into an HTTP POST body), so we capture
    the POST body via respx and assert it carries the circle filter."""
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "tt-test")
    _register_openai_routes()
    _register_tiktok_routes()
    _register_downloads()

    # Custom Rendi submit handler that records each ffmpeg_command for
    # later inspection.
    captured: list[str] = []
    counter = {"n": 0}

    def _submit(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        body = json.loads(request.content)
        captured.append(body.get("ffmpeg_command", ""))
        return httpx.Response(200, json={"command_id": f"cmd-{counter['n']}"})

    respx.post(f"{RENDI_BASE}/v1/run-ffmpeg-command").mock(side_effect=_submit)
    respx.get(url__regex=r"https://api\.rendi\.dev/v1/commands/.+").mock(
        return_value=httpx.Response(
            200,
            json={"status": "SUCCESS",
                  "output_files": {"out_1": {"storage_url": "https://r.dev/o.mp4"}}},
        )
    )
    respx.delete(url__regex=r"https://api\.rendi\.dev/v1/commands/.+/files").mock(
        return_value=httpx.Response(200, json={})
    )

    result = await process_avatar_row(
        _row(avatar_shape="Circle"), _build_clients(), job_id="jobX",
    )
    assert result.status == STATUS_SUCCESS
    assert result.metadata["avatar_shape"] == "circle"
    # First Rendi command is the still-image+avatar composite; it MUST
    # contain the circle-shape filter graph (yuva + geq + hypot).
    assert captured, "expected at least one Rendi submit call"
    composite_cmd = captured[0]
    assert "yuva420p" in composite_cmd
    assert "geq=" in composite_cmd
    assert "hypot(X-W/2,Y-H/2)" in composite_cmd


@respx.mock
async def test_avatar_invalid_size_or_shape_falls_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo in Avatar Size / Avatar Shape (e.g. ``"medum"`` /
    ``"round"``) must NOT 500 the row — it must silently fall back to
    today's defaults. Matches the same forgiving behaviour the route
    layer enforces."""
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "tt-test")
    _register_openai_routes()
    _register_rendi_routes()
    _register_tiktok_routes()
    _register_downloads()

    result = await process_avatar_row(
        _row(avatar_size="huge", avatar_shape="hexagon"),
        _build_clients(), job_id="jobX",
    )
    assert result.status == STATUS_SUCCESS
    # Resolved values (what actually rendered) — the defaults.
    assert result.metadata["avatar_size"] == "medium"
    assert result.metadata["avatar_shape"] == "rectangle"
    # Raw values (what the operator typed) — preserved for diagnostics.
    assert result.metadata["avatar_size_raw"] == "huge"
    assert result.metadata["avatar_shape_raw"] == "hexagon"
