"""Tests for the TikTok Symphony avatar adapter. HTTP is mocked via respx."""

from __future__ import annotations

import httpx
import pytest
import respx

from bulkvid.adapters.tiktok_avatar import (
    TikTokAvatarClient,
    TikTokAvatarError,
)


_CREATE = "https://example.test/symphony/create/"
_GET = "https://example.test/symphony/get/"
_LIST = "https://example.test/symphony/list/"


def _client(token: str = "tok-test") -> TikTokAvatarClient:
    return TikTokAvatarClient(
        access_token=token,
        create_url=_CREATE,
        get_url=_GET,
        list_url=_LIST,
        poll_interval_seconds=0.0,    # tests don't actually wait
        poll_max_attempts=5,
    )


# ── Init guard ──────────────────────────────────────────────────────────────


def test_missing_token_raises(monkeypatch) -> None:
    monkeypatch.delenv("TIKTOK_ACCESS_TOKEN", raising=False)
    with pytest.raises(TikTokAvatarError, match="TIKTOK_ACCESS_TOKEN"):
        TikTokAvatarClient()


# ── create_task ─────────────────────────────────────────────────────────────


@respx.mock
async def test_create_task_returns_task_id() -> None:
    respx.post(_CREATE).mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"list": [{"task_id": "task-abc"}]},
            },
        )
    )
    c = _client()
    task_id = await c.create_task(
        avatar_id="av-1", script="Hello world", video_name="vid",
    )
    assert task_id == "task-abc"


@respx.mock
async def test_create_task_raises_on_api_error_code() -> None:
    respx.post(_CREATE).mock(
        return_value=httpx.Response(
            200, json={"code": 40000, "message": "bad avatar_id"}
        )
    )
    c = _client()
    with pytest.raises(TikTokAvatarError, match="bad avatar_id"):
        await c.create_task(avatar_id="bogus", script="x", video_name="y")


@respx.mock
async def test_create_task_raises_when_task_id_missing() -> None:
    respx.post(_CREATE).mock(
        return_value=httpx.Response(
            200, json={"code": 0, "data": {"list": [{}]}}
        )
    )
    c = _client()
    with pytest.raises(TikTokAvatarError, match="missing task_id"):
        await c.create_task(avatar_id="av-1", script="x", video_name="y")


# ── wait_for_result ─────────────────────────────────────────────────────────


@respx.mock
async def test_wait_for_result_success_returns_preview_url() -> None:
    respx.get(_GET).mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "list": [
                        {
                            "status": "SUCCESS",
                            "preview_url": "https://tt.test/vid.mp4",
                            "duration": 7.4,
                        }
                    ]
                },
            },
        )
    )
    c = _client()
    result = await c.wait_for_result("task-abc")
    assert result.preview_url == "https://tt.test/vid.mp4"
    assert result.duration_seconds == pytest.approx(7.4)


@respx.mock
async def test_wait_for_result_polls_until_success() -> None:
    """Returns PROCESSING first, SUCCESS second — covers the polling loop."""
    route = respx.get(_GET).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"code": 0, "data": {"list": [{"status": "PROCESSING"}]}},
            ),
            httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "list": [
                            {
                                "status": "SUCCESS",
                                "preview_url": "https://tt.test/vid.mp4",
                            }
                        ]
                    },
                },
            ),
        ]
    )
    c = _client()
    result = await c.wait_for_result("task-abc")
    assert result.preview_url == "https://tt.test/vid.mp4"
    assert route.call_count == 2


@respx.mock
async def test_wait_for_result_fail_status_raises() -> None:
    respx.get(_GET).mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "list": [
                        {
                            "status": "FAIL",
                            "failure_reason": "script too long",
                        }
                    ]
                },
            },
        )
    )
    c = _client()
    with pytest.raises(TikTokAvatarError, match="script too long"):
        await c.wait_for_result("task-abc")


@respx.mock
async def test_wait_for_result_times_out() -> None:
    """Every poll returns PROCESSING — we should give up cleanly."""
    respx.get(_GET).mock(
        return_value=httpx.Response(
            200, json={"code": 0, "data": {"list": [{"status": "PROCESSING"}]}}
        )
    )
    c = _client()
    with pytest.raises(TikTokAvatarError, match="timed out"):
        await c.wait_for_result("task-abc")


# ── list_avatars ────────────────────────────────────────────────────────────


@respx.mock
async def test_list_avatars_parses_entries() -> None:
    respx.get(_LIST).mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "list": [
                        {
                            "avatar_id": "av-1",
                            "name": "Anna",
                            "gender": "FEMALE",
                            "preview_url": "https://tt.test/anna.png",
                        },
                        {
                            "avatar_id": "av-2",
                            "name": "Ben",
                            "gender": "MALE",
                            "preview_url": "https://tt.test/ben.png",
                        },
                    ]
                },
            },
        )
    )
    c = _client()
    entries = await c.list_avatars()
    assert len(entries) == 2
    assert entries[0].avatar_id == "av-1"
    assert entries[0].name == "Anna"
    assert entries[0].gender == "female"    # lowercased
    assert entries[0].preview_url == "https://tt.test/anna.png"
    assert entries[1].avatar_id == "av-2"


@respx.mock
async def test_advertiser_id_threads_through_to_query_string() -> None:
    """Regression for the 403 chat 2026-06-09 hit: when
    ``advertiser_id`` is configured, every call must include it as
    ``?advertiser_id=...`` — that's what unblocks the Business API
    endpoints that 403 without it."""
    create_route = respx.post(_CREATE).mock(
        return_value=httpx.Response(
            200, json={"code": 0, "data": {"list": [{"task_id": "t"}]}},
        )
    )
    list_route = respx.get(_LIST).mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"list": []}})
    )
    c = TikTokAvatarClient(
        access_token="tok-test",
        advertiser_id="advtr-9999",
        create_url=_CREATE,
        get_url=_GET,
        list_url=_LIST,
        poll_interval_seconds=0.0,
        poll_max_attempts=1,
    )

    await c.create_task(avatar_id="av", script="x", video_name="v")
    await c.list_avatars()

    # Both calls must have shipped the advertiser_id in the query string.
    for route in (create_route, list_route):
        assert route.called, f"{route} was not called"
        sent_url = str(route.calls[0].request.url)
        assert "advertiser_id=advtr-9999" in sent_url, (
            f"expected advertiser_id in query string, got: {sent_url}"
        )


@respx.mock
async def test_403_surfaces_response_body_in_error() -> None:
    """A 403 should report what TikTok actually said (e.g. 'missing
    advertiser_id'), not just the URL. Default httpx.raise_for_status
    only includes the URL — useless for diagnosis."""
    respx.get(_LIST).mock(
        return_value=httpx.Response(
            403,
            json={"code": 40002, "message": "advertiser_id is required"},
        )
    )
    c = _client()
    with pytest.raises(
        TikTokAvatarError, match="advertiser_id is required"
    ):
        await c.list_avatars()


@respx.mock
async def test_list_avatars_skips_entries_without_id() -> None:
    respx.get(_LIST).mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "list": [
                        {"avatar_id": "av-1", "name": "A"},
                        {"name": "no id"},     # skipped
                        {"avatar_id": "", "name": "blank id"},  # skipped
                    ]
                },
            },
        )
    )
    c = _client()
    entries = await c.list_avatars()
    assert [e.avatar_id for e in entries] == ["av-1"]
