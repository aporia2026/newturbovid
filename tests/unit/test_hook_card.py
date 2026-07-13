"""Unit tests for the Hook_Card tab (v2: per-cell media, AI video, voiceover)."""

from __future__ import annotations

import io
import json
import random
from types import SimpleNamespace
from unittest.mock import MagicMock

from PIL import Image

import bulkvid.orchestrator.row_processor_hook_card as rphc
import bulkvid.pipeline.hook_card_music as hcm
from bulkvid.adapters.rendi import (
    RendiOutput,
    render_cartoon_concat_command,
    render_ken_burns_command,
    render_mix_vo_music_command,
    render_set_vo_command,
)
from bulkvid.adapters.sheets import SheetsClient
from bulkvid.adapters.storage import UploadResult
from bulkvid.models.row import (
    STATUS_SUCCESS,
    STATUS_VIDEO_ASSEMBLY_FAILED,
    HookCardRow,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.queue import TAB_HOOK_CARD, _row_to_payload, payload_to_row
from bulkvid.orchestrator.row_processor_hook_card import (
    _classify_media,
    _music_label,
    _video_name,
    process_hook_card_row,
)
from bulkvid.orchestrator.runner import _TAB_HOOK_CARD, _tab_for_row
from bulkvid.orchestrator.sheet_writer import PendingWrite
from bulkvid.pipeline.card_renderer import render_hook_overlay_bytes
from bulkvid.pipeline.cartoon_prompt import NO_BRANDING, REALISTIC_STYLE
from bulkvid.pipeline.hook_card_copy import HookCardCopy
from bulkvid.pipeline.language import LanguageResult
from bulkvid.pipeline.safety import SAFE


def _row(**overrides) -> HookCardRow:
    base = dict(
        row_num=2, country="SE", vertical="Shipping Container Homes",
        article_url="https://example.com/a", num_images=3, text="", voice_over=False,
        music="", manual_media=[], aspect_ratio="9:16", open_comments="",
    )
    base.update(overrides)
    return HookCardRow(**base)


# ── Payload round-trip + routing ────────────────────────────────────────────


def test_payload_round_trip_hook_card() -> None:
    row = _row(text="Buy homes", voice_over=True, music="None", manual_media=["AI", "https://i/1.png"])
    payload = _row_to_payload(row, TAB_HOOK_CARD)
    assert '"__tab__": "hook_card"' in payload
    restored = payload_to_row(json.loads(payload))
    assert isinstance(restored, HookCardRow)
    assert restored == row


def test_runner_routes_hook_card_row() -> None:
    assert _tab_for_row(_row()) == _TAB_HOOK_CARD == "hook_card"


# ── Media classification + naming ───────────────────────────────────────────


def test_classify_media() -> None:
    assert _classify_media("https://x/a.png") == ("image", "https://x/a.png")
    assert _classify_media("https://x/a.jpg?q=1") == ("image", "https://x/a.jpg?q=1")
    assert _classify_media("https://x/clip.mp4") == ("video", "https://x/clip.mp4")
    assert _classify_media("https://x/clip.MOV") == ("video", "https://x/clip.MOV")
    assert _classify_media("AI") == ("ai_image", None)
    assert _classify_media("ai image") == ("ai_image", None)
    assert _classify_media("AI Video") == ("ai_video", None)
    assert _classify_media("ai-video") == ("ai_video", None)
    assert _classify_media("something else") == ("ai_image", None)


def test_music_label() -> None:
    assert _music_label(None) == "NoMusic"


def test_video_name() -> None:
    name = _video_name("SE", "Shipping Container Homes", "sv", None, 5)
    assert name == "SE-ShippingContainerHomes-sv-NoMusic-5"


# ── Overlay renderer ────────────────────────────────────────────────────────


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def test_hook_overlay_valid_png() -> None:
    data = render_hook_overlay_bytes(text="Acheter des maisons", width=1080, height=1920)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    img = _open(data)
    assert img.size == (1080, 1920) and img.mode == "RGBA"
    assert img.getchannel("A").getextrema()[1] == 255


def test_hook_overlay_empty_is_transparent() -> None:
    img = _open(render_hook_overlay_bytes(text="", width=1080, height=1920))
    assert img.getchannel("A").getextrema() == (0, 0)


# ── Rendi command builders ──────────────────────────────────────────────────


def test_ken_burns_command() -> None:
    cmd = render_ken_burns_command(1080, 1920, 2.0, zoom_in=True)
    assert "zoompan" in cmd and "scale=3240:5760" in cmd and "-frames:v 60" in cmd and "-an" in cmd


def test_concat_fps_normalizes() -> None:
    cmd = render_cartoon_concat_command(2, 2.0, 1080, 1920, audio=False, fps=30)
    assert "fps=30" in cmd
    cmd_no_fps = render_cartoon_concat_command(2, 2.0, 1080, 1920, audio=False)
    assert "fps=" not in cmd_no_fps


def test_set_vo_command() -> None:
    cmd = render_set_vo_command(1.3)
    assert "atempo=1.300" in cmd and "-map 0:v" in cmd and '-map "[a]"' in cmd


def test_mix_vo_music_command() -> None:
    cmd = render_mix_vo_music_command(1.3, 0.3)
    assert "atempo=1.300" in cmd and "volume=0.300" in cmd and "amix=inputs=2" in cmd


# ── Music selection ─────────────────────────────────────────────────────────


def _pool(tmp_path, names: list[str]):
    d = tmp_path / "music"
    d.mkdir()
    for n in names:
        (d / n).write_bytes(b"x")
    return d


def test_select_track_none_is_silent(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(hcm, "MUSIC_DIR", _pool(tmp_path, ["uplifting_1.mp3", "piano_1.mp3"]))
    assert hcm.select_track("None") is None          # explicit silent
    assert hcm.select_track("Uplifting").name == "uplifting_1.mp3"


def test_select_track_exact_variation(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(hcm, "MUSIC_DIR", _pool(tmp_path, ["uplifting_1.mp3", "uplifting_2.mp3"]))
    assert hcm.select_track("Uplifting 2").name == "uplifting_2.mp3"


def test_select_track_blank_random(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(hcm, "MUSIC_DIR", _pool(tmp_path, ["a_1.mp3", "b_1.mp3"]))
    assert hcm.select_track("", rng=random.Random(3)).suffix == ".mp3"


# ── Processor ───────────────────────────────────────────────────────────────


class _FakeArticle:
    async def fetch(self, url: str):
        return SimpleNamespace(content="A story about container homes.", source="sb", char_count=30, cost_usd=0.008, url=url)


class _FakeStorage:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def upload_bytes(self, data, key, content_type="application/octet-stream"):
        self.keys.append(key)
        return UploadResult(url=f"https://storage.test/{key}", backend="gcs", bytes_written=len(data), cost_usd=0.0001)


class _FakeTTS:
    async def synthesize(self, *, text, language, voice, style_prompt, country):
        return SimpleNamespace(wav_bytes=b"wavdata", cost_usd=0.01, duration_seconds=6.5, voice="v1")


class _FakeRendi:
    def __init__(self, *, ken_burns_raises: bool = False) -> None:
        self.calls: list[tuple] = []
        self._raise = ken_burns_raises
        self._n = 0

    async def ken_burns_clip(self, image_url, output_filename="out.mp4", *, aspect_ratio="9:16", seconds, zoom_in=True, **_k):
        if self._raise:
            raise RuntimeError("rendi boom")
        self._n += 1
        self.calls.append(("ken_burns", image_url, round(seconds, 3)))
        return RendiOutput(url=f"https://rendi/{output_filename}", cost_usd=0.01, command_id=f"kb-{self._n}")

    async def concat_clips_with_audio(self, clip_urls, audio_url, per_clip_seconds, output_filename="out.mp4", *, aspect_ratio="9:16", fps=None, **_k):
        self.calls.append(("concat", tuple(clip_urls), fps))
        return RendiOutput(url="https://rendi/slideshow.mp4", cost_usd=0.01, command_id="concat")

    async def overlay_image_on_video(self, video_url, overlay_url, output_filename="out.mp4", **_k):
        self.calls.append(("overlay_silent", video_url))
        return RendiOutput(url=f"https://rendi/{output_filename}", cost_usd=0.01, command_id="ov")

    async def overlay_and_add_music(self, video_url, overlay_url, music_url, output_filename="out.mp4", **_k):
        self.calls.append(("overlay_music", music_url))
        return RendiOutput(url="https://rendi/final.mp4", cost_usd=0.01, command_id="om")

    async def set_vo_audio(self, video_url, vo_url, output_filename="out.mp4", **_k):
        self.calls.append(("set_vo", vo_url))
        return RendiOutput(url="https://rendi/final.mp4", cost_usd=0.01, command_id="vo")

    async def mix_vo_and_music(self, video_url, vo_url, music_url, output_filename="out.mp4", **_k):
        self.calls.append(("mix_vo_music", vo_url, music_url))
        return RendiOutput(url="https://rendi/final.mp4", cost_usd=0.01, command_id="mvm")

    async def cleanup_commands(self, ids) -> None:
        self.calls.append(("cleanup", tuple(ids)))

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


def _clients(storage, rendi, tts=None) -> PipelineClients:
    dummy = SimpleNamespace()
    return PipelineClients(
        openai=dummy, kie=dummy, tts=tts or dummy, rendi=rendi,
        storage=storage, article=_FakeArticle(), settings_store=None,
    )


def _patch(monkeypatch, *, scenes=None) -> dict:
    cap: dict = {"image_prompts": [], "seedance": []}

    async def _detect(_c, _t, **_k):
        return LanguageResult(language="sv", confidence=1.0, cost_usd=0.0, cached=False)

    async def _safety(_s, _v, _r=0):
        return SAFE

    async def _copy(_c, *, want_hook, want_scenes, **_k):
        cap["copy"] = {"want_hook": want_hook, "want_scenes": want_scenes}
        return HookCardCopy(hook="Buy container homes" if want_hook else "",
                            scenes=[f"scene {i}" for i in range(want_scenes)], cost_usd=0.001)

    async def _t2i(_k, prompt, aspect, resolution="2K"):
        cap["image_prompts"].append(prompt)
        return f"https://kie/img{len(cap['image_prompts'])}.png", 0.06

    async def _seedance(_k, image_url, prompt, aspect, duration=4, resolution="720p"):
        cap["seedance"].append({"image": image_url, "duration": duration})
        return f"https://kie/clip{len(cap['seedance'])}.mp4", 0.07

    async def _classify(_c, _oc):
        return SimpleNamespace(cost_usd=0.001, mode=SimpleNamespace(value="x"))

    async def _script(_c, **_k):
        return SimpleNamespace(script="narration", voice="v", style_direction="calm",
                               cost_usd=0.002, word_count=3)

    async def _download(_u, timeout=60.0):
        return b"x" * 20_000

    monkeypatch.setattr(rphc, "detect_language", _detect)
    monkeypatch.setattr(rphc, "reconcile_language", lambda lang, **_k: lang)
    monkeypatch.setattr(rphc, "resolve_safety", _safety)
    monkeypatch.setattr(rphc, "generate_hook_card_copy", _copy)
    monkeypatch.setattr(rphc, "nano_banana_2_text_to_image", _t2i)
    monkeypatch.setattr(rphc, "seedance_image_to_video", _seedance)
    monkeypatch.setattr(rphc, "classify_open_comments", _classify)
    monkeypatch.setattr(rphc, "generate_script", _script)
    monkeypatch.setattr(rphc, "download_image", _download)
    monkeypatch.setattr(rphc, "select_track", lambda _m=None: None)
    return cap


async def test_ai_images_default(monkeypatch) -> None:
    cap = _patch(monkeypatch)
    storage, rendi = _FakeStorage(), _FakeRendi()
    result = await process_hook_card_row(_row(num_images=3), _clients(storage, rendi), job_id="j")
    assert result.status == STATUS_SUCCESS
    assert len(cap["image_prompts"]) == 3
    assert rendi.kinds().count("ken_burns") == 3 and "concat" in rendi.kinds()
    assert "overlay_silent" in rendi.kinds()          # no music -> silent
    assert all(REALISTIC_STYLE in p and NO_BRANDING in p for p in cap["image_prompts"])
    # concat is fps-normalized so mixed sources join cleanly.
    assert any(c[0] == "concat" and c[2] == rphc.HC_FPS for c in rendi.calls)
    # Filename: Country-Vertical-lang-Music-Row.
    assert result.video_urls[0].endswith("SE-ShippingContainerHomes-sv-NoMusic-2.mp4")


async def test_manual_images_skip_ai(monkeypatch) -> None:
    cap = _patch(monkeypatch)
    storage, rendi = _FakeStorage(), _FakeRendi()
    row = _row(text="A hook", manual_media=["https://u/1.png", "https://u/2.png"])
    result = await process_hook_card_row(row, _clients(storage, rendi), job_id="j")
    assert result.status == STATUS_SUCCESS
    assert rendi.kinds().count("ken_burns") == 2
    assert cap["image_prompts"] == []


async def test_manual_video_is_raw_clip(monkeypatch) -> None:
    _patch(monkeypatch)
    storage, rendi = _FakeStorage(), _FakeRendi()
    row = _row(text="A hook", manual_media=["https://u/clip.mp4"])
    result = await process_hook_card_row(row, _clients(storage, rendi), job_id="j")
    assert result.status == STATUS_SUCCESS
    assert rendi.kinds().count("ken_burns") == 0        # video passed straight to concat
    concat = next(c for c in rendi.calls if c[0] == "concat")
    assert concat[1] == ("https://u/clip.mp4",)


async def test_ai_video_uses_seedance(monkeypatch) -> None:
    cap = _patch(monkeypatch)
    storage, rendi = _FakeStorage(), _FakeRendi()
    row = _row(manual_media=["AI Video", "AI"])
    result = await process_hook_card_row(row, _clients(storage, rendi), job_id="j")
    assert result.status == STATUS_SUCCESS
    assert len(cap["seedance"]) == 1                    # one AI Video cell
    assert rendi.kinds().count("ken_burns") == 1        # one AI image cell
    assert len(cap["image_prompts"]) == 2               # both AI cells make a still


async def test_none_music_is_silent(monkeypatch) -> None:
    _patch(monkeypatch)
    storage, rendi = _FakeStorage(), _FakeRendi()
    result = await process_hook_card_row(_row(music="None"), _clients(storage, rendi), job_id="j")
    assert result.status == STATUS_SUCCESS
    assert "overlay_silent" in rendi.kinds() and "overlay_music" not in rendi.kinds()
    assert result.video_urls[0].endswith("-NoMusic-2.mp4")


async def test_music_track_in_name(monkeypatch, tmp_path) -> None:
    _patch(monkeypatch)
    track = tmp_path / "uplifting_2.mp3"
    track.write_bytes(b"ID3")
    monkeypatch.setattr(rphc, "select_track", lambda _m=None: track)
    storage, rendi = _FakeStorage(), _FakeRendi()
    result = await process_hook_card_row(_row(music="Uplifting 2"), _clients(storage, rendi), job_id="j")
    assert result.status == STATUS_SUCCESS
    assert "overlay_music" in rendi.kinds()
    assert result.video_urls[0].endswith("SE-ShippingContainerHomes-sv-Uplifting2-2.mp4")


async def test_voiceover_ducks_music(monkeypatch, tmp_path) -> None:
    _patch(monkeypatch)
    track = tmp_path / "lofi_1.mp3"
    track.write_bytes(b"ID3")
    monkeypatch.setattr(rphc, "select_track", lambda _m=None: track)
    storage, rendi = _FakeStorage(), _FakeRendi()
    result = await process_hook_card_row(
        _row(voice_over=True, music="Lofi 1"), _clients(storage, rendi, _FakeTTS()), job_id="j"
    )
    assert result.status == STATUS_SUCCESS
    assert "mix_vo_music" in rendi.kinds()              # VO over ducked music
    assert "overlay_silent" in rendi.kinds()            # hook burned in first
    assert any("bulkvid/vo/" in k for k in storage.keys)


async def test_voiceover_only_no_music(monkeypatch) -> None:
    _patch(monkeypatch)   # select_track -> None
    storage, rendi = _FakeStorage(), _FakeRendi()
    result = await process_hook_card_row(
        _row(voice_over=True, music="None"), _clients(storage, rendi, _FakeTTS()), job_id="j"
    )
    assert result.status == STATUS_SUCCESS
    assert "set_vo" in rendi.kinds() and "mix_vo_music" not in rendi.kinds()


async def test_fail_soft_on_rendi_error(monkeypatch) -> None:
    _patch(monkeypatch)
    storage, rendi = _FakeStorage(), _FakeRendi(ken_burns_raises=True)
    result = await process_hook_card_row(_row(), _clients(storage, rendi), job_id="j")
    assert result.status == STATUS_VIDEO_ASSEMBLY_FAILED
    assert result.video_urls == [] and result.error


# ── Ready Video write-back (now col O) ───────────────────────────────────────


def _make_fake_sheets_client():
    worksheets: dict = {}

    def _open_by_key(sheet_id):
        spreadsheet = MagicMock()

        def _worksheet(name):
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


async def test_hook_card_writes_to_ready_video_col_o() -> None:
    client, worksheets = _make_fake_sheets_client()
    ws = client.open_by_key("s").worksheet("Hook_Card")
    ws.row_values = MagicMock(return_value=[
        "Country", "Vertical", "Article", "Num of Images", "Text", "Voiceover",
        "Music", "Manual Media 1", "Manual Media 2", "Manual Media 3",
        "Manual Media 4", "Manual Media 5", "Change Size", "Open Comments",
        "Ready Video",
    ])
    sc = SheetsClient(client=client)
    n = await sc.batch_write_video_urls([
        PendingWrite(job_id="j", sheet_id="s", worksheet="Hook_Card",
                     tab_type=TAB_HOOK_CARD, row_num=2,
                     video_urls=["https://v/SE-X-sv-Piano1-2.mp4"],
                     status=STATUS_SUCCESS, error=None)
    ])
    ws = worksheets[("s", "Hook_Card")]
    cells = {u["range"]: u["values"][0][0] for u in ws.batch_update.call_args.args[0]}
    assert cells == {"O2": "https://v/SE-X-sv-Piano1-2.mp4"}
    assert n == 1
