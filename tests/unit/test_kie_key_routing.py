"""Tests for per-spreadsheet kie.ai key routing.

A sheet listed in ``KIE_KEY_MAP`` bills its own kie key(s); every other sheet
uses the shared ``KIE_AI_KEYS`` pool. Lets a duplicated sheet ("Bulk Videos 2")
run on a different key without a second backend. Plan
``_plans/2026-07-20-per-sheet-kie-key-routing.md``.

Covers:
  - Settings.kie_key_map parsing (single, multi-key, mixed separators, junk)
  - KieClientRouter.for_sheet (mapped, unmapped, blank id)
  - build_router_from_settings (default identity, per-sheet independence, raises)
  - JobQueue.claim_next_row surfaces the parent job's sheet_id
  - BatchRunner routes a mapped sheet's row to the mapped client end-to-end
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import bulkvid.orchestrator.runner as runner_mod
from bulkvid.adapters.article_fetch import ArticleFetcher
from bulkvid.adapters.gemini_tts import GeminiTTSClient
from bulkvid.adapters.kie import (
    KieClient,
    KieClientRouter,
    KiePool,
    build_router_from_settings,
)
from bulkvid.adapters.openai_client import OpenAIClient
from bulkvid.adapters.rendi import RendiClient
from bulkvid.adapters.storage import S3Uploader, StorageClient
from bulkvid.config import Settings
from bulkvid.models.row import STATUS_SUCCESS, ImageVORow, RowResult
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.queue import JOB_COMPLETED, TAB_IMAGE_VO, JobQueue
from bulkvid.orchestrator.runner import BatchRunner

# 24-char test keys → last-4 suffixes are distinct and readable in logs.
KEY_DEFAULT = "kie_test_key_DEFAULT00000"
KEY_SHEET_2 = "kie_test_key_SHEET20000AA"
KEY_SHEET_2B = "kie_test_key_SHEET20000BB"
KEY_SHEET_3 = "kie_test_key_SHEET30000CC"


# ── Settings.kie_key_map parsing ─────────────────────────────────────────────


def test_kie_key_map_empty_is_empty_dict() -> None:
    assert Settings(KIE_KEY_MAP="").kie_key_map == {}


def test_kie_key_map_single_entry() -> None:
    s = Settings(KIE_KEY_MAP=f"sheet-2 = {KEY_SHEET_2}")
    assert s.kie_key_map == {"sheet-2": [KEY_SHEET_2]}


def test_kie_key_map_multi_key_per_sheet() -> None:
    s = Settings(KIE_KEY_MAP=f"sheet-2={KEY_SHEET_2}|{KEY_SHEET_2B}")
    assert s.kie_key_map == {"sheet-2": [KEY_SHEET_2, KEY_SHEET_2B]}


def test_kie_key_map_mixed_newline_and_comma_separators() -> None:
    raw = f"sheet-2 = {KEY_SHEET_2}\nsheet-3 = {KEY_SHEET_3}, sheet-4 = {KEY_DEFAULT}"
    assert Settings(KIE_KEY_MAP=raw).kie_key_map == {
        "sheet-2": [KEY_SHEET_2],
        "sheet-3": [KEY_SHEET_3],
        "sheet-4": [KEY_DEFAULT],
    }


def test_kie_key_map_skips_blank_and_malformed_entries() -> None:
    # Blank lines, an entry with no '=', an empty sheet id, and an entry with no
    # key all drop out — a garbled line degrades to the default pool for that
    # sheet rather than poisoning the whole map.
    raw = "\n".join(
        [
            "",
            "no-equals-here",
            f" = {KEY_SHEET_2}",        # empty sheet id
            "sheet-3 =",                 # no key
            f"sheet-2 = {KEY_SHEET_2}",  # the one good entry
            "   ",
        ]
    )
    assert Settings(KIE_KEY_MAP=raw).kie_key_map == {"sheet-2": [KEY_SHEET_2]}


def test_kie_key_map_splits_on_first_equals_only() -> None:
    # A stray '=' in the value side must not corrupt the sheet id.
    s = Settings(KIE_KEY_MAP="sheet-2 = weird=key=value")
    assert s.kie_key_map == {"sheet-2": ["weird=key=value"]}


def test_kie_key_map_last_duplicate_sheet_wins() -> None:
    raw = f"sheet-2 = {KEY_SHEET_2}\nsheet-2 = {KEY_SHEET_3}"
    assert Settings(KIE_KEY_MAP=raw).kie_key_map == {"sheet-2": [KEY_SHEET_3]}


# ── KieClientRouter.for_sheet ────────────────────────────────────────────────


def _client(key: str) -> KieClient:
    return KieClient(pool=KiePool(keys=[key]))


def test_router_returns_mapped_client_for_mapped_sheet() -> None:
    default = _client(KEY_DEFAULT)
    mapped = _client(KEY_SHEET_2)
    router = KieClientRouter(default, {"sheet-2": mapped})
    assert router.for_sheet("sheet-2") is mapped


def test_router_falls_back_to_default_for_unmapped_sheet() -> None:
    default = _client(KEY_DEFAULT)
    router = KieClientRouter(default, {"sheet-2": _client(KEY_SHEET_2)})
    assert router.for_sheet("sheet-999") is default


def test_router_blank_or_none_sheet_id_returns_default() -> None:
    default = _client(KEY_DEFAULT)
    router = KieClientRouter(default, {"sheet-2": _client(KEY_SHEET_2)})
    assert router.for_sheet("") is default
    assert router.for_sheet(None) is default


# ── build_router_from_settings ───────────────────────────────────────────────


def test_build_router_default_is_the_kie_ai_keys_pool() -> None:
    s = Settings(KIE_AI_KEYS=KEY_DEFAULT, KIE_KEY_MAP="")
    router = build_router_from_settings(s)
    # No map → every sheet resolves to the single default client.
    assert router.mapped_sheet_ids == []
    assert router.for_sheet("anything") is router.default


def test_build_router_mapped_sheet_gets_independent_client() -> None:
    s = Settings(
        KIE_AI_KEYS=KEY_DEFAULT,
        KIE_KEY_MAP=f"sheet-2 = {KEY_SHEET_2}",
    )
    router = build_router_from_settings(s)
    assert router.mapped_sheet_ids == ["sheet-2"]
    mapped = router.for_sheet("sheet-2")
    assert mapped is not router.default            # separate client...
    assert router.for_sheet("other") is router.default   # ...only for that sheet


def test_build_router_raises_when_no_default_keys() -> None:
    # The default pool is the mandatory fallback for every unmapped sheet, so an
    # empty KIE_AI_KEYS is a hard error even when a map is present.
    s = Settings(KIE_AI_KEYS="", KIE_KEY_MAP=f"sheet-2 = {KEY_SHEET_2}")
    with pytest.raises(ValueError):
        build_router_from_settings(s)


# ── JobQueue.claim_next_row surfaces sheet_id ────────────────────────────────


async def test_claim_next_row_carries_sheet_id(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.db")
    try:
        await queue.enqueue(
            user_email="u@aporia.com",
            sheet_id="bulk-videos-2",
            worksheet="w",
            tab_type=TAB_IMAGE_VO,
            rows=[_img_row(2)],
        )
        claimed = await queue.claim_next_row()
        assert claimed is not None
        assert claimed.sheet_id == "bulk-videos-2"
    finally:
        queue.close()


# ── BatchRunner routes per sheet end-to-end ──────────────────────────────────


def _img_row(n: int) -> ImageVORow:
    return ImageVORow(
        row_num=n,
        country="US",
        vertical="tech",
        article_url="https://example.com/a",
        manual_image_url="https://example.com/s.png",
        voice_over=True,
        zapcap=False,
        aspect_ratio="9:16",
        script_pattern="How To",
        open_comments="",
    )


def _bundle(kie: KieClient, router: KieClientRouter) -> PipelineClients:
    """Minimal clients bundle — every processor is monkeypatched, so only the
    kie client + router matter; the rest just need to be constructible."""
    storage = StorageClient(
        primary=S3Uploader(
            bucket="b", access_key_id="x", secret_access_key="y", client=object(),
        )
    )
    return PipelineClients(
        openai=OpenAIClient(api_key="sk"),
        kie=kie,
        tts=GeminiTTSClient(project="amit-tts", client=object()),
        rendi=RendiClient(api_key="r"),
        storage=storage,
        article=ArticleFetcher(scrapingbee_api_key="t"),
        zapcap=None,
        kie_router=router,
    )


async def test_runner_dispatches_mapped_client_per_sheet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = _client(KEY_DEFAULT)
    mapped = _client(KEY_SHEET_2)
    router = KieClientRouter(default, {"sheet-mapped": mapped})

    # Capture which kie client instance each row's processor actually received.
    seen: dict[int, KieClient] = {}

    async def _capture(row, clients, *, job_id=None):
        seen[row.row_num] = clients.kie
        return RowResult(row_num=row.row_num, status=STATUS_SUCCESS, cost_usd=0.0)

    monkeypatch.setattr(runner_mod, "process_image_vo_row", _capture)

    queue = JobQueue(tmp_path / "jobs.db")
    try:
        mapped_job = await queue.enqueue(
            user_email="u@aporia.com", sheet_id="sheet-mapped", worksheet="w",
            tab_type=TAB_IMAGE_VO, rows=[_img_row(2)],
        )
        other_job = await queue.enqueue(
            user_email="u@aporia.com", sheet_id="sheet-other", worksheet="w",
            tab_type=TAB_IMAGE_VO, rows=[_img_row(3)],
        )

        runner = BatchRunner(
            queue, _bundle(default, router),
            max_concurrent=2, poll_idle_seconds=0.02,
        )

        async def _shutdown_when_done() -> None:
            while True:
                await asyncio.sleep(0.02)
                a = await queue.get_job(mapped_job)
                b = await queue.get_job(other_job)
                if (
                    a is not None and a.status == JOB_COMPLETED
                    and b is not None and b.status == JOB_COMPLETED
                ):
                    runner.request_shutdown()
                    return

        await asyncio.wait_for(
            asyncio.gather(runner.run(), _shutdown_when_done()),
            timeout=5.0,
        )
    finally:
        queue.close()

    # The mapped sheet's row saw the mapped client; the other sheet saw default.
    assert seen[2] is mapped
    assert seen[3] is default
