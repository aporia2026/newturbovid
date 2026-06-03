"""Tests for the Rendi.dev adapter.

All network calls mocked via respx — no real Rendi requests.

Covers:
  - dimensions_for_ratio: standard ratios, Sheets time-cast "09:16", WxH, fallback
  - render_*_command: W/H substitution, Rendi placeholders preserved literal
  - RendiClient.submit: success, 401, non-200, missing command_id
  - RendiClient.poll: SUCCESS, FAILED (with stderr surfaced), timeout, non-200 retry
  - High-level helpers: resize_image, stills_to_video, mix_music (return RendiOutput)
  - Cleanup: delete_command_files (200/404/error), cleanup_commands best-effort
  - Auth header is X-API-KEY (not Authorization: Bearer)
"""

from __future__ import annotations

import httpx
import pytest
import respx

from bulkvid.adapters.rendi import (
    COST_RENDI_COMMAND_USD,
    DEFAULT_DIMENSIONS_BY_RATIO,
    RendiAuthError,
    RendiClient,
    RendiCommandFailedError,
    RendiError,
    RendiTimeoutError,
    dimensions_for_ratio,
    normalize_aspect_ratio,
    render_fit_silent_command,
    render_fit_video_command,
    render_music_mix_command,
    render_resize_command,
    render_stills_to_video_command,
)

API_KEY = "rendi_test_key_xyz"
BASE = "https://api.rendi.dev"


# ── dimensions_for_ratio ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("input_str", "expected"),
    [
        ("9:16", (1080, 1920)),
        ("09:16", (1080, 1920)),        # Sheets time-cast
        ("1:1", (1080, 1080)),
        ("16:9", (1920, 1080)),
        ("4:5", (1080, 1350)),
        ("AUTO", (1080, 1920)),          # 9:16 fallback
        ("auto", (1080, 1920)),
        ("", (1080, 1920)),               # empty -> default
        ("garbage", (1080, 1920)),        # unrecognised -> default
        ("1080x1920", (1080, 1920)),      # explicit pixel format
        ("1920x1080", (1920, 1080)),
        ("0x0", (1080, 1920)),            # invalid pixel format -> default
    ],
)
def test_dimensions_for_ratio(input_str: str, expected: tuple[int, int]) -> None:
    assert dimensions_for_ratio(input_str) == expected


def test_default_dimensions_table_has_all_supported_ratios() -> None:
    # Sanity check: ratios mentioned in plan are present in the lookup.
    for ratio in ["9:16", "1:1", "16:9", "4:5", "5:4", "3:4", "4:3"]:
        assert ratio in DEFAULT_DIMENSIONS_BY_RATIO


# ── Template rendering ──────────────────────────────────────────────────────


def test_resize_command_substitutes_width_and_height() -> None:
    cmd = render_resize_command(1080, 1920)
    assert "1080" in cmd
    assert "1920" in cmd
    assert "__W__" not in cmd
    assert "__H__" not in cmd


def test_resize_command_preserves_rendi_placeholders() -> None:
    cmd = render_resize_command(720, 1280)
    # {{in_1}} and {{out_1}} are Rendi's substitution markers — must remain literal.
    assert "{{in_1}}" in cmd
    assert "{{out_1}}" in cmd


def test_resize_command_preserves_ffmpeg_overlay_vars() -> None:
    # `(W-w)/2` and `(H-h)/2` are ffmpeg variables — uppercase W,H must survive.
    cmd = render_resize_command(1080, 1920)
    assert "(W-w)/2" in cmd
    assert "(H-h)/2" in cmd


def test_stills_to_video_template_unchanged() -> None:
    cmd = render_stills_to_video_command()
    assert "{{in_1}}" in cmd
    assert "{{in_2}}" in cmd
    assert "{{out_1}}" in cmd
    assert "-loop 1" in cmd
    assert "libx264" in cmd
    assert "-shortest" in cmd


def test_fit_video_command_does_blurred_fit_and_audio() -> None:
    cmd = render_fit_video_command(1080, 1920)
    # Whole image visible (fit, NOT crop): a decrease-scaled foreground over a
    # blurred increase-scaled background.
    assert "force_original_aspect_ratio=decrease" in cmd
    assert "boxblur" in cmd
    assert "overlay=(W-w)/2:(H-h)/2" in cmd
    # Voiceover muxed + sped up, two inputs, capped at 15s.
    assert "{{in_1}}" in cmd and "{{in_2}}" in cmd and "{{out_1}}" in cmd
    assert "atempo=" in cmd
    assert "-shortest" in cmd
    assert "1080" in cmd and "1920" in cmd


def test_fit_silent_command_has_no_audio() -> None:
    cmd = render_fit_silent_command(1080, 1350, seconds=8)
    assert "force_original_aspect_ratio=decrease" in cmd
    assert "boxblur" in cmd
    assert "-an" in cmd                 # silent
    assert "atempo" not in cmd          # nothing to speed up
    assert "{{in_2}}" not in cmd        # single input (image only)
    assert "-t 8" in cmd
    assert "1080" in cmd and "1350" in cmd


def test_music_mix_template_unchanged() -> None:
    cmd = render_music_mix_command()
    assert "{{in_1}}" in cmd
    assert "{{in_2}}" in cmd
    assert "{{out_1}}" in cmd
    assert "volume=0.3" in cmd                 # background music gain
    assert "amix=inputs=2:duration=shortest" in cmd
    assert "[mixed]" in cmd


# ── RendiClient.submit ──────────────────────────────────────────────────────


@respx.mock
async def test_submit_success_returns_command_id() -> None:
    respx.post(f"{BASE}/v1/run-ffmpeg-command").mock(
        return_value=httpx.Response(200, json={"command_id": "cmd-123"})
    )
    async with RendiClient(api_key=API_KEY, base_url=BASE) as client:
        cmd_id = await client.submit(
            ffmpeg_command="-i {{in_1}} {{out_1}}",
            input_files={"in_1": "https://src/x"},
            output_files={"out_1": "out.mp4"},
        )
    assert cmd_id == "cmd-123"


@respx.mock
async def test_submit_uses_x_api_key_header() -> None:
    captured: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("x-api-key", ""))
        return httpx.Response(200, json={"command_id": "cmd-1"})

    respx.post(f"{BASE}/v1/run-ffmpeg-command").mock(side_effect=_handler)
    async with RendiClient(api_key=API_KEY, base_url=BASE) as client:
        await client.submit(
            ffmpeg_command="-i {{in_1}} {{out_1}}",
            input_files={"in_1": "u"},
            output_files={"out_1": "o.mp4"},
        )
    assert captured == [API_KEY]


@respx.mock
async def test_submit_401_raises_auth_error() -> None:
    respx.post(f"{BASE}/v1/run-ffmpeg-command").mock(
        return_value=httpx.Response(401, text="unauthorized")
    )
    async with RendiClient(api_key=API_KEY, base_url=BASE) as client:
        with pytest.raises(RendiAuthError):
            await client.submit(
                ffmpeg_command="x", input_files={}, output_files={"out_1": "o"}
            )


@respx.mock
async def test_submit_non_200_raises_rendi_error() -> None:
    respx.post(f"{BASE}/v1/run-ffmpeg-command").mock(
        return_value=httpx.Response(500, text="server error")
    )
    async with RendiClient(api_key=API_KEY, base_url=BASE) as client:
        with pytest.raises(RendiError):
            await client.submit(
                ffmpeg_command="x", input_files={}, output_files={"out_1": "o"}
            )


@respx.mock
async def test_submit_missing_command_id_raises() -> None:
    respx.post(f"{BASE}/v1/run-ffmpeg-command").mock(
        return_value=httpx.Response(200, json={})
    )
    async with RendiClient(api_key=API_KEY, base_url=BASE) as client:
        with pytest.raises(RendiError):
            await client.submit(
                ffmpeg_command="x", input_files={}, output_files={"out_1": "o"}
            )


def test_client_rejects_empty_api_key() -> None:
    with pytest.raises(ValueError):
        RendiClient(api_key="")


# ── RendiClient.poll ────────────────────────────────────────────────────────


@respx.mock
async def test_poll_success_returns_storage_url() -> None:
    respx.get(f"{BASE}/v1/commands/cmd-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "SUCCESS",
                "output_files": {"out_1": {"storage_url": "https://r.dev/out.mp4"}},
            },
        )
    )
    async with RendiClient(api_key=API_KEY, base_url=BASE) as client:
        url = await client.poll("cmd-1", max_attempts=2, delay_seconds=0.0)
    assert url == "https://r.dev/out.mp4"


@respx.mock
async def test_poll_failed_surfaces_ffmpeg_stderr() -> None:
    respx.get(f"{BASE}/v1/commands/cmd-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "FAILED",
                "error": {"message": "decode error", "stderr": "Invalid data found"},
            },
        )
    )
    async with RendiClient(api_key=API_KEY, base_url=BASE) as client:
        with pytest.raises(RendiCommandFailedError) as exc:
            await client.poll("cmd-1", max_attempts=2, delay_seconds=0.0)
    # Operator-debuggable: both the error message AND the ffmpeg stderr in the exception text.
    assert "decode error" in str(exc.value)
    assert "Invalid data found" in str(exc.value)


@respx.mock
async def test_poll_timeout_raises() -> None:
    respx.get(f"{BASE}/v1/commands/cmd-1").mock(
        return_value=httpx.Response(200, json={"status": "RUNNING"})
    )
    async with RendiClient(api_key=API_KEY, base_url=BASE) as client:
        with pytest.raises(RendiTimeoutError):
            await client.poll("cmd-1", max_attempts=3, delay_seconds=0.0)


@respx.mock
async def test_poll_success_missing_storage_url_raises() -> None:
    respx.get(f"{BASE}/v1/commands/cmd-1").mock(
        return_value=httpx.Response(
            200,
            json={"status": "SUCCESS", "output_files": {"out_1": {}}},
        )
    )
    async with RendiClient(api_key=API_KEY, base_url=BASE) as client:
        with pytest.raises(RendiError):
            await client.poll("cmd-1", max_attempts=2, delay_seconds=0.0)


# ── High-level helpers ──────────────────────────────────────────────────────


@respx.mock
async def test_resize_image_returns_url_and_cost() -> None:
    captured_payload: list[dict] = []

    def _submit_handler(request: httpx.Request) -> httpx.Response:
        import json

        captured_payload.append(json.loads(request.content))
        return httpx.Response(200, json={"command_id": "cmd-resize"})

    respx.post(f"{BASE}/v1/run-ffmpeg-command").mock(side_effect=_submit_handler)
    respx.get(f"{BASE}/v1/commands/cmd-resize").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "SUCCESS",
                "output_files": {"out_1": {"storage_url": "https://r.dev/resized.png"}},
            },
        )
    )

    async with RendiClient(api_key=API_KEY, base_url=BASE) as client:
        out = await client.resize_image(
            source_url="https://src/in.png",
            aspect_ratio="9:16",
            max_attempts=2,
            delay_seconds=0.0,
        )

    assert out.url == "https://r.dev/resized.png"
    assert out.cost_usd == COST_RENDI_COMMAND_USD
    assert out.command_id == "cmd-resize"
    # The submitted command must carry the resolved 1080x1920 dimensions.
    cmd_str = captured_payload[0]["ffmpeg_command"]
    assert "1080" in cmd_str
    assert "1920" in cmd_str
    # And the Rendi placeholders must still be literal.
    assert "{{in_1}}" in cmd_str
    assert "{{out_1}}" in cmd_str


@respx.mock
async def test_stills_to_video_returns_url_and_cost() -> None:
    respx.post(f"{BASE}/v1/run-ffmpeg-command").mock(
        return_value=httpx.Response(200, json={"command_id": "cmd-stitch"})
    )
    respx.get(f"{BASE}/v1/commands/cmd-stitch").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "SUCCESS",
                "output_files": {"out_1": {"storage_url": "https://r.dev/v.mp4"}},
            },
        )
    )
    async with RendiClient(api_key=API_KEY, base_url=BASE) as client:
        out = await client.stills_to_video(
            image_url="https://src/i.png",
            audio_url="https://src/a.mp3",
            max_attempts=2,
            delay_seconds=0.0,
        )
    assert out.url == "https://r.dev/v.mp4"
    assert out.cost_usd == COST_RENDI_COMMAND_USD
    assert out.command_id == "cmd-stitch"


@respx.mock
async def test_image_to_video_fit_with_audio() -> None:
    submitted: dict = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        import json as _json
        submitted.update(_json.loads(request.content))
        return httpx.Response(200, json={"command_id": "cmd-fit"})

    respx.post(f"{BASE}/v1/run-ffmpeg-command").mock(side_effect=_capture)
    respx.get(f"{BASE}/v1/commands/cmd-fit").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "SUCCESS",
                "output_files": {"out_1": {"storage_url": "https://r.dev/fit.mp4"}},
            },
        )
    )
    async with RendiClient(api_key=API_KEY, base_url=BASE) as client:
        out = await client.image_to_video_fit(
            image_url="https://src/ad.png",
            audio_url="https://src/vo.wav",
            aspect_ratio="9:16",
            max_attempts=2,
            delay_seconds=0.0,
        )
    assert out.url == "https://r.dev/fit.mp4"
    assert out.command_id == "cmd-fit"
    # One command, two inputs (image + audio) — not two separate Rendi calls.
    assert set(submitted["input_files"]) == {"in_1", "in_2"}
    assert "boxblur" in submitted["ffmpeg_command"]


@respx.mock
async def test_image_to_video_fit_silent_when_no_audio() -> None:
    submitted: dict = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        import json as _json
        submitted.update(_json.loads(request.content))
        return httpx.Response(200, json={"command_id": "cmd-fit-silent"})

    respx.post(f"{BASE}/v1/run-ffmpeg-command").mock(side_effect=_capture)
    respx.get(f"{BASE}/v1/commands/cmd-fit-silent").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "SUCCESS",
                "output_files": {"out_1": {"storage_url": "https://r.dev/silent.mp4"}},
            },
        )
    )
    async with RendiClient(api_key=API_KEY, base_url=BASE) as client:
        out = await client.image_to_video_fit(
            image_url="https://src/ad.png",
            audio_url=None,
            aspect_ratio="1:1",
            max_attempts=2,
            delay_seconds=0.0,
        )
    assert out.url == "https://r.dev/silent.mp4"
    assert set(submitted["input_files"]) == {"in_1"}      # image only
    assert "-an" in submitted["ffmpeg_command"]


@respx.mock
async def test_mix_music_returns_url_and_cost() -> None:
    respx.post(f"{BASE}/v1/run-ffmpeg-command").mock(
        return_value=httpx.Response(200, json={"command_id": "cmd-mix"})
    )
    respx.get(f"{BASE}/v1/commands/cmd-mix").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "SUCCESS",
                "output_files": {"out_1": {"storage_url": "https://r.dev/mixed.mp4"}},
            },
        )
    )
    async with RendiClient(api_key=API_KEY, base_url=BASE) as client:
        out = await client.mix_music(
            video_url="https://src/v.mp4",
            music_url="https://src/m.mp3",
            max_attempts=2,
            delay_seconds=0.0,
        )
    assert out.url == "https://r.dev/mixed.mp4"
    assert out.cost_usd == COST_RENDI_COMMAND_USD
    assert out.command_id == "cmd-mix"


# ── normalize_aspect_ratio (kie image-model aspect strings) ──────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("9:16", "9:16"),
        ("09:16", "9:16"),          # Sheets time-cast
        ("16:9", "16:9"),
        ("1:1", "1:1"),
        ("4:5", "4:5"),
        ("1080x1920", "9:16"),      # WxH reduced via GCD
        ("1080x1080", "1:1"),
        ("", "9:16"),               # empty -> default
        ("auto", "9:16"),
        ("garbage", "9:16"),
        ("7:13", "9:16"),           # valid format but not an allowed ratio -> default
    ],
)
def test_normalize_aspect_ratio(raw: str, expected: str) -> None:
    assert normalize_aspect_ratio(raw) == expected


def test_normalize_aspect_ratio_custom_default() -> None:
    assert normalize_aspect_ratio("nonsense", default="1:1") == "1:1"


# ── Auto-retry (_submit_and_poll) ────────────────────────────────────────────


@respx.mock
async def test_submit_and_poll_retries_on_timeout_then_succeeds() -> None:
    counter = {"n": 0}

    def _submit(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(200, json={"command_id": f"cmd-{counter['n']}"})

    def _poll(request: httpx.Request) -> httpx.Response:
        cid = str(request.url).rsplit("/", 1)[-1]
        if cid == "cmd-1":
            return httpx.Response(200, json={"status": "QUEUED"})    # never completes
        return httpx.Response(
            200,
            json={"status": "SUCCESS", "output_files": {"out_1": {"storage_url": "https://r.dev/ok.mp4"}}},
        )

    respx.post(f"{BASE}/v1/run-ffmpeg-command").mock(side_effect=_submit)
    respx.get(url__regex=rf"{BASE}/v1/commands/.+").mock(side_effect=_poll)

    async with RendiClient(api_key=API_KEY, base_url=BASE) as client:
        url, cid = await client._submit_and_poll(
            "cmd", {"in_1": "x"}, {"out_1": "o.mp4"},
            max_attempts=2, delay_seconds=0.0, retry_backoff_seconds=0.0,
        )
    assert url == "https://r.dev/ok.mp4"
    assert cid == "cmd-2"      # first command timed out, retry produced cmd-2
    assert counter["n"] == 2   # exactly one retry


@respx.mock
async def test_submit_and_poll_does_not_retry_command_failed() -> None:
    counter = {"n": 0}

    def _submit(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(200, json={"command_id": "cmd-x"})

    respx.post(f"{BASE}/v1/run-ffmpeg-command").mock(side_effect=_submit)
    respx.get(url__regex=rf"{BASE}/v1/commands/.+").mock(
        return_value=httpx.Response(
            200, json={"status": "FAILED", "error": {"message": "bad input"}}
        )
    )
    async with RendiClient(api_key=API_KEY, base_url=BASE) as client:
        with pytest.raises(RendiCommandFailedError):
            await client._submit_and_poll(
                "cmd", {"in_1": "x"}, {"out_1": "o.mp4"},
                max_attempts=2, delay_seconds=0.0, retry_backoff_seconds=0.0,
            )
    assert counter["n"] == 1   # genuine ffmpeg failure is NOT retried


# ── Cleanup: delete_command_files / cleanup_commands ─────────────────────────


@respx.mock
async def test_delete_command_files_calls_delete_endpoint() -> None:
    route = respx.delete(f"{BASE}/v1/commands/cmd-9/files").mock(
        return_value=httpx.Response(200, json={})
    )
    async with RendiClient(api_key=API_KEY, base_url=BASE) as client:
        await client.delete_command_files("cmd-9")
    assert route.called


@respx.mock
async def test_delete_command_files_treats_404_as_gone() -> None:
    respx.delete(f"{BASE}/v1/commands/missing/files").mock(
        return_value=httpx.Response(404, json={"detail": "not found"})
    )
    async with RendiClient(api_key=API_KEY, base_url=BASE) as client:
        await client.delete_command_files("missing")  # must not raise


@respx.mock
async def test_delete_command_files_raises_on_500() -> None:
    respx.delete(f"{BASE}/v1/commands/boom/files").mock(
        return_value=httpx.Response(500, text="server error")
    )
    async with RendiClient(api_key=API_KEY, base_url=BASE) as client:
        with pytest.raises(RendiError):
            await client.delete_command_files("boom")


@respx.mock
async def test_cleanup_commands_is_best_effort() -> None:
    # cmd-ok deletes fine; cmd-bad 500s. cleanup_commands must attempt both and
    # swallow the failure rather than propagate it.
    ok = respx.delete(f"{BASE}/v1/commands/cmd-ok/files").mock(
        return_value=httpx.Response(200, json={})
    )
    bad = respx.delete(f"{BASE}/v1/commands/cmd-bad/files").mock(
        return_value=httpx.Response(500, text="nope")
    )
    async with RendiClient(api_key=API_KEY, base_url=BASE) as client:
        await client.cleanup_commands(["cmd-ok", "cmd-bad"])  # must not raise
    assert ok.called
    assert bad.called


# ── Cost constant sanity ────────────────────────────────────────────────────


def test_cost_constant_positive() -> None:
    assert COST_RENDI_COMMAND_USD > 0
