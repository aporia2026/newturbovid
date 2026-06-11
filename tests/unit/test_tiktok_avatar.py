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


@respx.mock
async def test_wait_for_result_retries_on_rate_limit_code_then_succeeds() -> None:
    """code=40100 ("Too many requests") is per-account rate-limit and
    transient. The poll loop should log + continue rather than kill the
    row — the next iteration polls again after poll_interval. Reported
    2026-06-11: row 3 failed on attempt 12 with this exact code, when
    the next attempt would have succeeded.
    Plan: ``_plans/2026-06-11-tiktok-avatar-rate-limit-retry.md``.
    """
    route = respx.get(_GET).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"code": 40100, "message": "Too many requests. Please retry in some time."},
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
async def test_wait_for_result_retries_on_internal_timeout_51010_then_succeeds() -> None:
    """code=51010 ("internal service timed out") is also transient — the
    list endpoint already treats it as such; the poll endpoint follows
    the same pattern for symmetry."""
    route = respx.get(_GET).mock(
        side_effect=[
            httpx.Response(200, json={"code": 51010, "message": "internal timeout"}),
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
async def test_wait_for_result_terminal_code_still_raises() -> None:
    """Non-transient codes (auth failures, bad request, etc.) MUST still
    kill the row immediately — otherwise the operator never hears about
    a configuration error and the row times out 10 minutes later with a
    misleading message."""
    respx.get(_GET).mock(
        return_value=httpx.Response(
            200, json={"code": 40000, "message": "bad request"}
        )
    )
    c = _client()
    with pytest.raises(TikTokAvatarError, match="code=40000"):
        await c.wait_for_result("task-abc")


# ── list_avatars ────────────────────────────────────────────────────────────


@respx.mock
async def test_list_avatars_parses_actual_tiktok_response_shape() -> None:
    """Matches the shape returned by ``/creative/digital_avatar/get/``
    (operator's confirmed-working Avatar.py): ``avatar_name`` not
    ``name``, ``avatar_thumbnail`` not ``preview_url``, gender lives in
    ``tag_groups``."""
    respx.get(_LIST).mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "list": [
                        {
                            "avatar_id": "av-1",
                            "avatar_name": "Anna",
                            "avatar_thumbnail": "https://tt.test/anna.png",
                            "avatar_preview_url": "https://tt.test/anna.mp4",
                            "tag_groups": [
                                {"tag_type": "gender", "tags": ["female"]},
                                {"tag_type": "age", "tags": ["young"]},
                            ],
                        },
                        {
                            "avatar_id": "av-2",
                            "avatar_name": "Ben",
                            "avatar_thumbnail": "https://tt.test/ben.png",
                            "tag_groups": [
                                {"tag_type": "gender", "tags": ["MALE"]},
                            ],
                        },
                    ],
                    "page_info": {
                        "page": 1, "page_size": 100,
                        "total_number": 2, "total_page": 1,
                    },
                },
            },
        )
    )
    c = _client()
    entries = await c.list_avatars()
    assert len(entries) == 2
    assert entries[0].avatar_id == "av-1"
    assert entries[0].name == "Anna"
    assert entries[0].gender == "female"
    # thumbnail wins over preview_url (preview_url is a video URL,
    # thumbnail is the still — better for <img> tags).
    assert entries[0].preview_url == "https://tt.test/anna.png"
    assert entries[1].avatar_id == "av-2"
    assert entries[1].gender == "male"      # lowercased


@respx.mock
async def test_list_avatars_paginates_until_short_page() -> None:
    """When TikTok returns a full 100-item page, keep paginating; when
    the next page comes back short, stop. Mirrors Avatar.py's
    stop-on-short-page logic."""
    page1 = [
        {"avatar_id": f"av-{i}", "avatar_name": f"A{i}", "tag_groups": []}
        for i in range(100)
    ]
    page2 = [
        {"avatar_id": f"av-{100 + i}", "avatar_name": f"A{100 + i}", "tag_groups": []}
        for i in range(5)    # short page → stop
    ]
    route = respx.get(_LIST).mock(
        side_effect=[
            httpx.Response(200, json={"code": 0, "data": {"list": page1}}),
            httpx.Response(200, json={"code": 0, "data": {"list": page2}}),
        ]
    )
    c = _client()
    entries = await c.list_avatars()
    assert len(entries) == 105
    assert route.call_count == 2


@respx.mock
async def test_list_avatars_dedupes_repeating_ids_across_pages() -> None:
    """Defensive: TikTok very occasionally repeats an entry across
    page boundaries. We track ``seen_ids`` so the catalog stays clean."""
    page1 = [
        {"avatar_id": f"av-{i}", "avatar_name": "x", "tag_groups": []}
        for i in range(100)
    ]
    page2 = [
        {"avatar_id": "av-0", "avatar_name": "dup", "tag_groups": []},  # dup → skipped
        {"avatar_id": "av-100", "avatar_name": "new", "tag_groups": []},
    ]
    respx.get(_LIST).mock(
        side_effect=[
            httpx.Response(200, json={"code": 0, "data": {"list": page1}}),
            httpx.Response(200, json={"code": 0, "data": {"list": page2}}),
        ]
    )
    c = _client()
    entries = await c.list_avatars()
    assert len(entries) == 101    # 100 + 1 new, dup dropped
    assert {e.avatar_id for e in entries} == {f"av-{i}" for i in range(101)}


@respx.mock
async def test_list_avatars_retries_transient_code_51010() -> None:
    """TikTok's ``code=51010`` ("internal service timed out") gets
    retried per Avatar.py's pattern rather than failing the whole list."""
    route = respx.get(_LIST).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"code": 51010, "message": "internal service timed out"},
            ),
            httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "list": [{"avatar_id": "av-1", "tag_groups": []}]
                    },
                },
            ),
        ]
    )
    c = _client()
    # Patch sleep so the test doesn't actually wait the backoff.
    import bulkvid.adapters.tiktok_avatar as mod
    real_sleep = mod.asyncio.sleep
    mod.asyncio.sleep = lambda _s: real_sleep(0)
    try:
        entries = await c.list_avatars()
    finally:
        mod.asyncio.sleep = real_sleep
    assert [e.avatar_id for e in entries] == ["av-1"]
    assert route.call_count == 2


@respx.mock
async def test_advertiser_id_threads_through_to_create_but_not_list() -> None:
    """When ``advertiser_id`` is configured, the create endpoint
    includes it (Business API typically requires it). The list endpoint
    must NOT include it — operator's Avatar.py confirms the
    ``/creative/digital_avatar/get/`` path works without advertiser_id
    and TikTok would 403 with one on some accounts."""
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

    assert create_route.called
    create_url = str(create_route.calls[0].request.url)
    assert "advertiser_id=advtr-9999" in create_url, (
        f"create call missing advertiser_id: {create_url}"
    )

    assert list_route.called
    list_url = str(list_route.calls[0].request.url)
    assert "advertiser_id" not in list_url, (
        f"list call must NOT include advertiser_id: {list_url}"
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
