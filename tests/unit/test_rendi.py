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

import asyncio

import httpx
import pytest
import respx
import structlog.testing

from bulkvid.adapters.rendi import (
    COST_RENDI_COMMAND_USD,
    DEFAULT_DIMENSIONS_BY_RATIO,
    RENDI_DEFAULT_MAX_CONCURRENT,
    RENDI_SEMAPHORE_WAIT_LOG_THRESHOLD_SECONDS,
    RendiAuthError,
    RendiClient,
    RendiCommandFailedError,
    RendiError,
    RendiTimeoutError,
    dimensions_for_ratio,
    normalize_aspect_ratio,
    render_cartoon_concat_command,
    render_fit_silent_command,
    render_fit_video_command,
    render_music_mix_command,
    render_resize_command,
    render_still_image_avatar_overlay_command,
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


def test_still_image_avatar_overlay_command_shape() -> None:
    """Regression for the simplified avatar pipeline (chat 2026-06-09).

    The command must:
      * loop the still image input so it runs for the avatar's full length
      * scale the background to the row's aspect (cover + center-crop)
      * scale the avatar overlay to the requested width preserving aspect
      * pin the overlay at the bottom-left at the requested margins
      * map the OVERLAY's audio (in_2) — not the background's, the image
        has no audio
      * end when the overlay ends (-shortest), so a long looped image
        doesn't pad the output past the avatar's narration
      * leave Rendi placeholders ``{{in_1}}`` / ``{{in_2}}`` / ``{{out_1}}``
        intact for the API substitution
    """
    cmd = render_still_image_avatar_overlay_command(
        width=1080, height=1920,
        overlay_width_px=324, margin_x=40, margin_y=40,
    )
    # Rendi placeholders preserved.
    assert "{{in_1}}" in cmd and "{{in_2}}" in cmd and "{{out_1}}" in cmd
    # Background image looped (so it lasts the avatar duration).
    assert "-loop 1" in cmd
    # Aspect-fit + cover-crop on the background.
    assert "scale=1080:1920:force_original_aspect_ratio=increase" in cmd
    assert "crop=1080:1920" in cmd
    # Avatar scaled to the configured pixel width.
    assert "scale=324:-1" in cmd
    # Pinned at bottom-left with the requested margin.
    assert "overlay=40:H-h-40" in cmd
    # Avatar audio drives the output (not background — bg is silent image).
    assert "-map 1:a" in cmd
    # End on the shorter of the two inputs (i.e. the avatar video).
    assert "-shortest" in cmd
    # Tuned for still image input so libx264 picks the right rate-control.
    assert "-tune stillimage" in cmd


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


# ── Cartoon concat command ──────────────────────────────────────────────────


def test_cartoon_concat_command_with_audio() -> None:
    # Legacy path (no total_video_seconds): -shortest, no fade, no -t.
    cmd = render_cartoon_concat_command(2, 3.5, 1080, 1920, audio=True)
    # Two video inputs + one audio input (in_3), one output.
    assert "-i {{in_1}}" in cmd
    assert "-i {{in_2}}" in cmd
    assert "-i {{in_3}}" in cmd        # voiceover is the last input
    assert "{{out_1}}" in cmd
    # Each clip trimmed to the per-clip duration, then concatenated.
    assert "trim=start=0:duration=3.500" in cmd
    assert "concat=n=2:v=1:a=0[outv]" in cmd
    # Audio sped up + muxed, shortest so video tracks the VO length.
    assert "atempo=" in cmd
    assert '-map "[outa]"' in cmd
    assert "-shortest" in cmd
    assert "afade" not in cmd          # no fade in legacy path
    assert "1080:1920" in cmd


def test_cartoon_concat_command_with_total_video_seconds_forces_duration() -> None:
    # When total_video_seconds is set the output is forced to that length via
    # -t, the audio gets a 0.3s afade-out, and -shortest is gone. This is the
    # path the cartoon row processor uses to guarantee a 6-8s clip.
    cmd = render_cartoon_concat_command(
        2, 4.0, 1080, 1920, audio=True, total_video_seconds=8.0,
    )
    assert "-t 8.000" in cmd
    assert "afade=t=out:st=7.700:d=0.300" in cmd
    assert "-shortest" not in cmd
    # Atempo still present so the VO is sped up before the fade.
    assert "atempo=" in cmd


def test_cartoon_concat_command_total_video_seconds_short_target_safe_fade() -> None:
    # Tiny target (smaller than the fade duration) must not produce a negative
    # fade start — clamp to 0.0.
    cmd = render_cartoon_concat_command(
        2, 0.1, 1080, 1920, audio=True, total_video_seconds=0.2,
    )
    assert "afade=t=out:st=0.000:d=0.300" in cmd
    assert "-t 0.200" in cmd


def test_cartoon_concat_command_total_video_seconds_ignored_when_silent() -> None:
    # No audio -> no -t and no fade; silent stitch falls back to -an.
    cmd = render_cartoon_concat_command(
        2, 3.0, 1080, 1920, audio=False, total_video_seconds=8.0,
    )
    assert "-an" in cmd
    assert "-t " not in cmd
    assert "afade" not in cmd


def test_cartoon_concat_command_per_clip_list_trims_each_clip_independently() -> None:
    # Long-VO path: first shot is the standard 4s, last shot extends to ~6.8s
    # (Seedance 8s clip trimmed). Each input must trim to its own duration.
    cmd = render_cartoon_concat_command(
        2, [4.0, 6.8], 1080, 1920, audio=True, total_video_seconds=10.8,
    )
    assert "trim=start=0:duration=4.000" in cmd
    assert "trim=start=0:duration=6.800" in cmd
    # Both clips still concat in order, audio still gets the speedup + fade.
    assert "concat=n=2:v=1:a=0[outv]" in cmd
    assert "atempo=" in cmd
    assert "-t 10.800" in cmd


def test_cartoon_concat_command_per_clip_list_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        render_cartoon_concat_command(2, [4.0, 4.0, 4.0])    # 3 durations, 2 clips


def test_cartoon_concat_command_silent() -> None:
    cmd = render_cartoon_concat_command(3, 2.0, 1080, 1920, audio=False)
    # Three video inputs, NO audio input, no audio map.
    assert "-i {{in_1}}" in cmd
    assert "-i {{in_3}}" in cmd
    assert "-i {{in_4}}" not in cmd
    assert "concat=n=3:v=1:a=0[outv]" in cmd
    assert "-an" in cmd
    assert "atempo" not in cmd
    assert '-map "[outa]"' not in cmd


def test_cartoon_concat_command_rejects_zero_clips() -> None:
    with pytest.raises(ValueError):
        render_cartoon_concat_command(0, 3.0)


# ── Cost constant sanity ────────────────────────────────────────────────────


def test_cost_constant_positive() -> None:
    assert COST_RENDI_COMMAND_USD > 0


# ── Full-body logging on FAILED + per-provider semaphore ────────────────────
#
# Coverage for ``_plans/2026-06-08-200-row-batch-failures.md`` §Phase 1.
# Adds three protections that landed in response to the 277-row simple-tab
# batch failure (46% failure rate, primarily Rendi platform overload).


@respx.mock
async def test_poll_failed_logs_full_response_body() -> None:
    # Reproduces the 2026-06-07 production shape: Rendi marks the command
    # FAILED with an empty error dict and no stderr. Pre-change code logged
    # ``error_message=""`` and dropped the body, leaving operators blind.
    # Post-change code MUST include the full body in the log record so the
    # next variant of this failure shape lands debuggable.
    failed_body = {"status": "FAILED", "error": {}, "command_id": "cmd-empty"}
    respx.get(f"{BASE}/v1/commands/cmd-empty").mock(
        return_value=httpx.Response(200, json=failed_body)
    )

    with structlog.testing.capture_logs() as logs:
        async with RendiClient(api_key=API_KEY, base_url=BASE) as client:
            with pytest.raises(RendiCommandFailedError):
                await client.poll("cmd-empty", max_attempts=2, delay_seconds=0.0)

    failed = [e for e in logs if e.get("event") == "rendi_poll_failed"]
    assert failed, "expected one rendi_poll_failed log event"
    assert failed[0].get("full_body") == failed_body, (
        "rendi_poll_failed must include the full Rendi response body so the "
        "next unknown FAILED shape is diagnosable (see plan §Phase 1 Part 1)"
    )


async def test_constructor_rejects_max_concurrent_zero() -> None:
    with pytest.raises(ValueError):
        RendiClient(api_key=API_KEY, max_concurrent=0)


async def test_semaphore_default_matches_module_constant() -> None:
    # Sanity: the factory pulls the default from the module constant. If the
    # constant moves, the public default moves with it.
    client = RendiClient(api_key=API_KEY)
    sem = client._get_sem()
    # ``Semaphore._value`` is the remaining capacity at idle == initial value.
    assert sem._value == RENDI_DEFAULT_MAX_CONCURRENT


@respx.mock
async def test_semaphore_caps_concurrent_submit_and_poll() -> None:
    # Probe: with max_concurrent=2, only 2 _submit_and_poll calls should be
    # mid-flight at any instant. We track peak concurrency observed inside
    # the submit handler (which fires only after the slot is acquired).
    in_flight = {"now": 0, "peak": 0}
    submit_block = asyncio.Event()    # holds submits open until released

    async def _submit_handler(request: httpx.Request) -> httpx.Response:
        in_flight["now"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        await submit_block.wait()    # park while holding the semaphore slot
        return httpx.Response(200, json={"command_id": f"cmd-{in_flight['now']}"})

    respx.post(f"{BASE}/v1/run-ffmpeg-command").mock(side_effect=_submit_handler)
    respx.get(url__regex=rf"{BASE}/v1/commands/.+").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "SUCCESS",
                "output_files": {"out_1": {"storage_url": "https://r.dev/o.mp4"}},
            },
        )
    )

    async with RendiClient(api_key=API_KEY, base_url=BASE, max_concurrent=2) as client:
        async def _one() -> str:
            try:
                url, _ = await client._submit_and_poll(
                    "cmd", {"in_1": "x"}, {"out_1": "o.mp4"},
                    max_attempts=2, delay_seconds=0.0, retry_backoff_seconds=0.0,
                )
                return url
            finally:
                in_flight["now"] -= 1

        tasks = [asyncio.create_task(_one()) for _ in range(5)]
        # Let the scheduler acquire as many slots as the cap allows.
        await asyncio.sleep(0.05)
        assert in_flight["peak"] == 2, (
            f"expected peak concurrency 2, saw {in_flight['peak']} "
            "(semaphore is NOT capping cross-row Rendi commands)"
        )
        submit_block.set()
        await asyncio.gather(*tasks)


@respx.mock
async def test_semaphore_wait_logged_when_threshold_exceeded() -> None:
    # When the cap bites for longer than the log threshold, we want a
    # rendi_semaphore_wait event. The second call must wait for the first.
    submit_block = asyncio.Event()

    async def _slow_submit(request: httpx.Request) -> httpx.Response:
        await submit_block.wait()
        return httpx.Response(200, json={"command_id": "cmd-1"})

    respx.post(f"{BASE}/v1/run-ffmpeg-command").mock(side_effect=_slow_submit)
    respx.get(url__regex=rf"{BASE}/v1/commands/.+").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "SUCCESS",
                "output_files": {"out_1": {"storage_url": "https://r.dev/o.mp4"}},
            },
        )
    )

    with structlog.testing.capture_logs() as logs:
        async with RendiClient(
            api_key=API_KEY, base_url=BASE, max_concurrent=1
        ) as client:
            async def _one() -> None:
                await client._submit_and_poll(
                    "cmd", {"in_1": "x"}, {"out_1": "o.mp4"},
                    max_attempts=2, delay_seconds=0.0, retry_backoff_seconds=0.0,
                )

            t1 = asyncio.create_task(_one())
            t2 = asyncio.create_task(_one())
            # Wait long enough that t2's queue time crosses the threshold.
            await asyncio.sleep(RENDI_SEMAPHORE_WAIT_LOG_THRESHOLD_SECONDS + 0.2)
            submit_block.set()
            await asyncio.gather(t1, t2)

    waits = [e for e in logs if e.get("event") == "rendi_semaphore_wait"]
    assert waits, (
        "expected at least one rendi_semaphore_wait event when the cap is biting"
    )
    # The recorded wait should at least equal the slept time minus jitter.
    assert waits[0]["queued_for_s"] >= RENDI_SEMAPHORE_WAIT_LOG_THRESHOLD_SECONDS
