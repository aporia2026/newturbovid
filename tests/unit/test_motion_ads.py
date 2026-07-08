"""Unit tests for the Motion_Ads tab.

Motion_Ads turns an article into a silent 12s motion-ad video plus two text
outputs (Headline -> col D, Description -> col E). Covers:

  - Payload round-trip for ``MotionAdsRow`` + runner tab routing.
  - Image-prompt composition: realistic style + no-brands always; no-people on
    Apple=Yes; sensitive-apparel safety block on match.
  - Copy generation: char caps enforced (60 / 80) and graceful JSON fallback.
  - The NEW text write-back: Headline/Description land in D/E (positional and
    header-resolved), copy lands even when the video failed, and NO other tab is
    ever touched by a stray copy field.
  - Processor happy path (generated + manual image) via monkeypatched helpers.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import bulkvid.orchestrator.row_processor_motion_ads as rpma
from bulkvid.adapters.sheets import MOTION_ADS_COLS, SheetsClient
from bulkvid.adapters.storage import UploadResult
from bulkvid.models.row import STATUS_SUCCESS, MotionAdsRow, RowResult
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.queue import (
    TAB_IMAGE_VO,
    TAB_MOTION_ADS,
    _row_to_payload,
    payload_to_row,
)
from bulkvid.orchestrator.row_processor_motion_ads import (
    MA_VIDEO_DURATION_SECONDS,
    NO_PEOPLE,
    _compose_image_prompt,
    process_motion_ads_row,
)
from bulkvid.orchestrator.runner import _TAB_MOTION_ADS, _tab_for_row
from bulkvid.orchestrator.sheet_writer import PendingWrite
from bulkvid.pipeline.cartoon_prompt import NO_BRANDING, REALISTIC_STYLE
from bulkvid.pipeline.language import LanguageResult
from bulkvid.pipeline.motion_ads_copy import (
    DESCRIPTION_MAX_CHARS,
    HEADLINE_MAX_CHARS,
    MotionAdsCopy,
    generate_motion_ads_copy,
)
from bulkvid.pipeline.safety import SAFE, SafetyContext


def _row(**overrides) -> MotionAdsRow:
    base = dict(
        row_num=2,
        country="DE",
        vertical="Shipping Container Homes",
        article_url="https://example.com/a",
        manual_image_url="",
        apple=False,
        aspect_ratio="16:9",
        open_comments="",
    )
    base.update(overrides)
    return MotionAdsRow(**base)


# ── Payload round-trip + routing ────────────────────────────────────────────


def test_payload_round_trip_motion_ads() -> None:
    row = _row(apple=True, manual_image_url="https://img/x.png", open_comments="hi")
    payload = _row_to_payload(row, TAB_MOTION_ADS)
    assert '"__tab__": "motion_ads"' in payload
    # Deserialize exactly as the worker does: the claimed row's JSON payload is
    # ``json.loads``-ed to a dict, then handed to ``payload_to_row``. Testing the
    # live function (not a look-alike helper) is the whole point — an earlier
    # version round-tripped through a dead duplicate that HAD the motion_ads
    # branch while the real ``payload_to_row`` did not, so every prod row crashed
    # with ``ImageVORow ... unexpected keyword argument 'apple'`` (2026-07-08).
    restored = payload_to_row(json.loads(payload))
    assert isinstance(restored, MotionAdsRow)
    assert restored == row


def test_runner_routes_motion_ads_row() -> None:
    assert _tab_for_row(_row()) == _TAB_MOTION_ADS
    assert _TAB_MOTION_ADS == "motion_ads"


# ── Image-prompt composition ────────────────────────────────────────────────


def test_compose_image_prompt_realistic_and_no_brands_always() -> None:
    prompt = _compose_image_prompt(
        "a cozy container home at dusk", apple=False, safety=SAFE, safety_block="",
    )
    assert REALISTIC_STYLE in prompt
    assert NO_BRANDING in prompt
    assert "a cozy container home at dusk" in prompt
    assert NO_PEOPLE not in prompt         # apple off → people allowed


def test_compose_image_prompt_apple_adds_no_people() -> None:
    prompt = _compose_image_prompt(
        "a cozy container home", apple=True, safety=SAFE, safety_block="",
    )
    assert NO_PEOPLE in prompt


def test_compose_image_prompt_appends_safety_block_on_match() -> None:
    safety = SafetyContext(matched=True, matched_keyword="bra")
    block = "PRODUCT ONLY — no humans, no mannequins."
    prompt = _compose_image_prompt(
        "a product on a table", apple=False, safety=safety, safety_block=block,
    )
    assert block in prompt


# ── Copy generation ─────────────────────────────────────────────────────────


class _FakeOpenAI:
    """Minimal OpenAI stand-in: ``chat`` returns a canned text + cost."""

    def __init__(self, text: str, cost: float = 0.0012) -> None:
        self._text = text
        self._cost = cost
        self.calls = 0

    async def chat(self, **_kwargs) -> SimpleNamespace:
        self.calls += 1
        return SimpleNamespace(text=self._text, cost_usd=self._cost)


async def test_copy_enforces_char_caps() -> None:
    long_headline = "word " * 40         # ~200 chars
    long_description = "detail " * 40     # ~280 chars
    import json

    client = _FakeOpenAI(
        json.dumps(
            {
                "headline": long_headline,
                "description": long_description,
                "image_scene": "a container home in a green field, no text",
            }
        )
    )
    copy = await generate_motion_ads_copy(
        client, article_body="body", language="de", country="DE",
        vertical="Homes", open_comments="", apple=False,
    )
    assert len(copy.headline) <= HEADLINE_MAX_CHARS
    assert len(copy.description) <= DESCRIPTION_MAX_CHARS
    assert copy.image_scene.startswith("a container home")
    assert copy.cost_usd == 0.0012


async def test_copy_falls_back_on_bad_json() -> None:
    client = _FakeOpenAI("this is not json at all")
    copy = await generate_motion_ads_copy(
        client, article_body="body", language="en", country="US",
        vertical="Solar Panels", open_comments="", apple=False,
    )
    # Fallback still yields usable, in-cap copy derived from the vertical.
    assert 0 < len(copy.headline) <= HEADLINE_MAX_CHARS
    assert 0 < len(copy.description) <= DESCRIPTION_MAX_CHARS
    assert "Solar Panels" in copy.image_scene


# ── Text write-back (the novel plumbing) ────────────────────────────────────


def _make_fake_client() -> tuple[MagicMock, dict[tuple[str, str], MagicMock]]:
    worksheets: dict[tuple[str, str], MagicMock] = {}

    def _open_by_key(sheet_id: str) -> MagicMock:
        spreadsheet = MagicMock()

        def _worksheet(name: str) -> MagicMock:
            key = (sheet_id, name)
            if key not in worksheets:
                ws = MagicMock()
                ws.batch_update = MagicMock(return_value=None)
                worksheets[key] = ws
            return worksheets[key]

        spreadsheet.worksheet = MagicMock(side_effect=_worksheet)
        return spreadsheet

    client = MagicMock()
    client.open_by_key = MagicMock(side_effect=_open_by_key)
    return client, worksheets


def _write(**overrides) -> PendingWrite:
    base = dict(
        job_id="job-1", sheet_id="s", worksheet="Motion_Ads",
        tab_type=TAB_MOTION_ADS, row_num=2, video_urls=["https://v/1.mp4"],
        status=STATUS_SUCCESS, error=None, headline="Great homes", description="Buy now.",
    )
    base.update(overrides)
    return PendingWrite(**base)


async def test_motion_ads_writes_video_and_copy_positional() -> None:
    """No header row → video → J, Headline → D, Description → E (positional)."""
    client, worksheets = _make_fake_client()
    sc = SheetsClient(client=client)
    n = await sc.batch_write_video_urls([_write()])
    ws = worksheets[("s", "Motion_Ads")]
    updates = ws.batch_update.call_args.args[0]
    cells = {u["range"]: u["values"][0][0] for u in updates}
    assert cells == {"J2": "https://v/1.mp4", "D2": "Great homes", "E2": "Buy now."}
    assert n == 3


async def test_motion_ads_writes_copy_by_header_when_shifted() -> None:
    """Headline/Description resolved by header even if the operator moved them."""
    client, worksheets = _make_fake_client()
    sc = SheetsClient(client=client)
    ws = client.open_by_key("s").worksheet("Motion_Ads")
    # Operator layout: copy columns pushed to the right, Ready Video too.
    ws.row_values = MagicMock(return_value=[
        "Country", "Vertical", "Article", "Manual Image", "Apple", "Change Size",
        "Open Comments", "Headline", "Description", "Ready Video",
    ])
    await sc.batch_write_video_urls([_write()])
    updates = ws.batch_update.call_args.args[0]
    cells = {u["range"]: u["values"][0][0] for u in updates}
    # Headline at col H (8th), Description at I (9th), Ready Video at J (10th).
    assert cells == {"H2": "Great homes", "I2": "Buy now.", "J2": "https://v/1.mp4"}


async def test_motion_ads_writes_copy_even_without_video() -> None:
    """A row whose video failed still lands its Headline / Description."""
    client, worksheets = _make_fake_client()
    sc = SheetsClient(client=client)
    n = await sc.batch_write_video_urls([_write(video_urls=[])])
    ws = worksheets[("s", "Motion_Ads")]
    updates = ws.batch_update.call_args.args[0]
    cells = {u["range"]: u["values"][0][0] for u in updates}
    assert cells == {"D2": "Great homes", "E2": "Buy now."}
    assert n == 2


async def test_other_tab_never_receives_copy_columns() -> None:
    """A stray headline on a non-motion_ads write is ignored (no positional
    guess), so an unrelated tab can never have a cell clobbered."""
    client, worksheets = _make_fake_client()
    sc = SheetsClient(client=client)
    await sc.batch_write_video_urls([
        _write(
            worksheet="Image-VO", tab_type=TAB_IMAGE_VO,
            video_urls=["https://v/1.mp4"], headline="stray", description="stray",
        )
    ])
    ws = worksheets[("s", "Image-VO")]
    updates = ws.batch_update.call_args.args[0]
    cells = {u["range"]: u["values"][0][0] for u in updates}
    # Only the video URL lands; no D/E writes for a tab with no copy columns.
    assert cells == {"J2": "https://v/1.mp4"}


# ── Processor happy path ────────────────────────────────────────────────────


class _FakeArticle:
    async def fetch(self, url: str) -> SimpleNamespace:
        return SimpleNamespace(
            content="A story about container homes.", source="scrapingbee",
            char_count=30, cost_usd=0.008, url=url,
        )


class _FakeStorage:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def upload_bytes(
        self, data: bytes, key: str, content_type: str = "application/octet-stream"
    ) -> UploadResult:
        self.keys.append(key)
        return UploadResult(
            url=f"https://storage.test/{key}", backend="gcs",
            bytes_written=len(data), cost_usd=0.0001,
        )


def _clients(storage: _FakeStorage) -> PipelineClients:
    dummy = MagicMock()
    return PipelineClients(
        openai=dummy, kie=dummy, tts=dummy, rendi=dummy,
        storage=storage, article=_FakeArticle(), settings_store=None,
    )


def _patch_common(monkeypatch: pytest.MonkeyPatch, *, scene: str = "a container home") -> dict:
    captured: dict = {}

    async def _fake_detect(_client, _body, **_kw) -> LanguageResult:
        return LanguageResult(language="de", confidence=1.0, cost_usd=0.0, cached=False)

    async def _fake_safety(_store, _vertical, _row_num=0) -> SafetyContext:
        return SAFE

    async def _fake_copy(_client, **kwargs) -> MotionAdsCopy:
        captured["copy_kwargs"] = kwargs
        return MotionAdsCopy(
            headline="Container homes", description="A smarter way to live.",
            image_scene=scene, cost_usd=0.001,
        )

    async def _fake_t2i(_kie, prompt, aspect, resolution="1K"):
        captured["image_prompt"] = prompt
        captured["image_aspect"] = aspect
        return "https://kie/img.png", 0.06

    async def _fake_seedance(_kie, image_url, motion, aspect, duration=4, resolution="720p"):
        captured["seedance"] = {"image_url": image_url, "duration": duration, "aspect": aspect}
        return "https://kie/clip.mp4", 0.21

    async def _fake_download(_url, timeout=60.0) -> bytes:
        return b"binarydata"

    monkeypatch.setattr(rpma, "detect_language", _fake_detect)
    monkeypatch.setattr(rpma, "resolve_safety", _fake_safety)
    monkeypatch.setattr(rpma, "generate_motion_ads_copy", _fake_copy)
    monkeypatch.setattr(rpma, "nano_banana_2_text_to_image", _fake_t2i)
    monkeypatch.setattr(rpma, "seedance_image_to_video", _fake_seedance)
    monkeypatch.setattr(rpma, "download_image", _fake_download)
    return captured


async def test_process_generated_image_apple_sets_no_people(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_common(monkeypatch)
    storage = _FakeStorage()
    result = await process_motion_ads_row(
        _row(apple=True), _clients(storage), job_id="job-x"
    )
    assert isinstance(result, RowResult)
    assert result.status == STATUS_SUCCESS
    assert result.video_urls and result.video_urls[0].startswith("https://storage.test/")
    assert result.headline == "Container homes"
    assert result.description == "A smarter way to live."
    # Apple → the no-people clause reached the actual image prompt.
    assert NO_PEOPLE in captured["image_prompt"]
    assert REALISTIC_STYLE in captured["image_prompt"]
    # Always a 12s clip.
    assert captured["seedance"]["duration"] == MA_VIDEO_DURATION_SECONDS == 12
    # Apple flag was threaded into the copy call too.
    assert captured["copy_kwargs"]["apple"] is True


async def test_process_manual_image_skips_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_common(monkeypatch)

    async def _boom(*_a, **_k):
        raise AssertionError("nano_banana must NOT be called on the manual path")

    monkeypatch.setattr(rpma, "nano_banana_2_text_to_image", _boom)
    storage = _FakeStorage()
    result = await process_motion_ads_row(
        _row(manual_image_url="https://user/pic.png"), _clients(storage), job_id="job-y"
    )
    assert result.status == STATUS_SUCCESS
    # The pasted image was re-uploaded and then animated.
    assert any("motion_ads_images" in k for k in storage.keys)
    assert captured["seedance"]["image_url"].startswith("https://storage.test/")
    # Copy is still generated on the manual path.
    assert result.headline == "Container homes"
