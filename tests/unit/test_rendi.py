"""Tests for the Rendi.dev adapter.

All network calls mocked via respx — no real Rendi requests.

Covers:
  - dimensions_for_ratio: standard ratios, Sheets time-cast "09:16", WxH, fallback
  - render_*_command: W/H substitution, Rendi placeholders preserved literal
  - RendiClient.submit: success, 401, non-200, missing command_id
  - RendiClient.poll: SUCCESS, FAILED (with stderr surfaced), timeout, non-200 retry
  - High-level helpers: resize_image, stills_to_video, mix_music
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
        url, cost = await client.resize_image(
            source_url="https://src/in.png",
            aspect_ratio="9:16",
            max_attempts=2,
            delay_seconds=0.0,
        )

    assert url == "https://r.dev/resized.png"
    assert cost == COST_RENDI_COMMAND_USD
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
        url, cost = await client.stills_to_video(
            image_url="https://src/i.png",
            audio_url="https://src/a.mp3",
            max_attempts=2,
            delay_seconds=0.0,
        )
    assert url == "https://r.dev/v.mp4"
    assert cost == COST_RENDI_COMMAND_USD


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
        url, cost = await client.mix_music(
            video_url="https://src/v.mp4",
            music_url="https://src/m.mp3",
            max_attempts=2,
            delay_seconds=0.0,
        )
    assert url == "https://r.dev/mixed.mp4"
    assert cost == COST_RENDI_COMMAND_USD


# ── Cost constant sanity ────────────────────────────────────────────────────


def test_cost_constant_positive() -> None:
    assert COST_RENDI_COMMAND_USD > 0
