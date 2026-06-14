# Blank "Change Size" → use the manual image's native dimensions

Date: 2026-06-14
Owner: Yoav
Source: chat 2026-06-14 — Yoav highlighted rows on the image_vo tab with blank Change Size cells and asked that they render at the manual image's native size instead of silently defaulting to 9:16.

## Goal

When the operator leaves the "Change Size" cell blank on a manual-image tab, the output should match the manual image's native pixel dimensions (and aspect ratio for kie/seedance scene generation). Today, blank is silently coerced to `"9:16"` in two places (Apps Script + sheets adapter), so a 1080×1350 source ad gets rendered at 1080×1920 with blurred bars — exactly what the operator was trying to avoid by uploading a pre-cropped image.

## Scope

| Tab | Behavior on blank size | Notes |
|---|---|---|
| `image_vo` | Probe manual image → use native | Kie scene gen uses closest valid ratio; Rendi final assembly uses exact pixels. |
| `simple` | Probe manual image → use native | Rendi-only path; exact pixels straight through. |
| `simple_x4` | Probe manual image → use native | Same as image_vo. |
| `four_images` | Probe `image_urls[0]` → use native | First image carries the canonical dimensions. |
| `text_on_img` | Probe manual image → use native | Pure Pillow path — exact pixels into `overlay_text_on_image_bytes`. |
| `avatar` | Probe manual image → use native | Kie + Rendi same as image_vo. Skipped when `manual_image_url` is blank (avatar tab allows text-to-image fallback). |
| `cartoon` | Unchanged (default 9:16) | No manual image — nothing to probe. |

## Interpretation

Two possible reads of "as is with its given size":
- (a) **Exact pixel dimensions** — output rendered at 1234×567 if the source is 1234×567.
- (b) **Closest valid ratio** — output snapped to one of the 10 known ratios.

**Picked (a)** (confirmed with Yoav, 2026-06-14). Rendi's `dimensions_for_ratio()` already accepts `"WxH"` pixel format and passes it through unchanged — no Rendi changes needed. Kie + Seedance can't accept arbitrary `WxH`, so for THOSE calls we snap to the closest valid ratio via `normalize_aspect_ratio()`. The final Rendi assembly still uses exact pixels.

## Design

### D.1 Apps Script (`apps_script/Code.gs`)

Drop the `|| '9:16'` fallback in every row reader so blank cells flow through as `""`:

```js
aspect_ratio: _cell(values, cols.aspectRatio),
```

Affects `_readImageVORow`, `_readFourImagesRow`, `_readSimpleX4Row`, `_readCartoonRow`, `_readTextOnImgRow`, `_readAvatarRow`. (Cartoon's row will still get `"9:16"` on the backend — the resolver below preserves that default for tabs without a manual image.)

### D.2 Sheets adapter (`src/bulkvid/adapters/sheets.py`)

Drop `default="9:16"` from each `_cell(raw, cols.aspect_ratio, ...)` call on the six manual-image tab readers. The empty string flows to the row model.

### D.3 New helper — `src/bulkvid/util/image_probe.py`

```python
async def probe_native_dimensions(url: str) -> tuple[int, int] | None:
    """Download ``url`` via ``download_image`` and return ``(width, height)``.

    Returns ``None`` on any failure (network, decode, ImageDownloadError)
    so callers can fall back to the default ratio without crashing the row.
    """
```

PIL's `Image.open(io.BytesIO(...))` reads the header for dimensions without decoding pixels — cheap and decompression-bomb resistant.

### D.4 New helper — `src/bulkvid/orchestrator/aspect_resolve.py`

```python
async def resolve_aspect_ratio(
    raw: str,
    *,
    manual_image_url: str | None,
    row_num: int,
    fallback: str = "9:16",
) -> str:
    """If ``raw`` is set, return it verbatim.
    If blank and ``manual_image_url`` is given, probe and return 'WxH'.
    Else return ``fallback``. Never raises."""
```

### D.5 Each row processor

At entry, before `metadata` and `row_start` log are built, call the resolver and **mutate** `row.aspect_ratio`. The row dataclasses are not frozen.

```python
row.aspect_ratio = await resolve_aspect_ratio(
    row.aspect_ratio,
    manual_image_url=row.manual_image_url,
    row_num=row.row_num,
)
```

For `four_images`, pass `image_urls[0]` (the first manual image carries the canonical dimensions). For `avatar`, skip the probe when `manual_image_url` is blank — the avatar tab supports text-to-image fallback, which has no source to read from. For `cartoon`, no change at all.

### D.6 `normalize_aspect_ratio` (`src/bulkvid/adapters/rendi.py`)

Today: a WxH whose GCD-reduced ratio isn't in `VALID_RATIO_STRINGS` falls back to `"9:16"`. That's the worst-of-both: kie+seedance receive a wrong-shape ratio and the operator's "use my dimensions" intent is silently dropped.

New: snap to the CLOSEST valid ratio by `|actual_aspect - candidate_aspect|`. A 1234×567 source (aspect 2.18) snaps to `"21:9"` (aspect 2.33), not `"9:16"`.

`dimensions_for_ratio()` needs no change — it already passes WxH through.

## Security (rule 13)

- One extra HTTP fetch per row to the operator-provided URL. The same URL is fetched later in every pipeline (Rendi via URL, text_on_img directly), so no new external attack surface.
- `Image.open()` reads only the header for `.size`. Decompression-bomb risk is mitigated by PIL's default `MAX_IMAGE_PIXELS` and by NOT decoding pixels.
- `ImageDownloadError` already redacts the URL's query string from logs (FB `?d=...` tokens). The probe helper inherits that.
- Bad URL → probe returns `None` → fallback to 9:16 → row continues. Probe never blocks a row.

## Observability (rule 14)

In the probe helper:
- `[aspect probe] from_native row=N url=host w=W h=H` on success
- `[aspect probe] failed row=N url=host err=…` on probe failure
- `[aspect probe] skipped row=N reason=user_set|no_manual_image`

In the rendi snap:
- `[aspect snap] WxH → ratio=… delta=…` when a WxH falls outside the valid set and gets snapped

## Settings (rule 15)

None new. "Blank = use the image" is a behavior, not a knob; surfacing it as a toggle would just create a way for an operator to disable the very thing they want. The existing per-row Change Size cell IS the operator's escape hatch (type `9:16` to override).

## Cost (rule 8)

Zero new paid services. One extra HTTP GET per row probed; ~3 MB avg per ad image; ~600 MB extra egress on a 200-row batch. Existing pipelines already pull these images, so the marginal hit is one round-trip per row, not one download. Acceptable.

## Tests (rule 18)

- `tests/unit/test_image_probe.py` (new) — round-trip a tiny in-memory PNG via mocked `download_image`; assert `(w, h)` is returned. Failure path: mock raises → returns `None`.
- `tests/unit/test_aspect_resolve.py` (new) — three branches: non-empty input, blank + URL (probe called), blank + no URL (fallback used).
- `tests/unit/test_rendi.py` (extend) — `normalize_aspect_ratio("1234x567")` snaps to the closest valid ratio (`"21:9"`), not `"9:16"`. Same with several known off-ratio inputs.
- `tests/unit/test_row_processor_text_on_img.py` (extend) — blank aspect + manual image → composed image at native dims.
- Run the full pytest suite after the changes — the bar is green, not just "the new tests pass."

## Open questions

None.
