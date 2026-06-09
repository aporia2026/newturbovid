# Avatar overlay: per-row size + shape

Date: 2026-06-09
Status: Approved (decisions confirmed in chat)

## Goal

Let the operator pick the avatar overlay's **size** (Small / Medium /
Large) and **shape** (Rectangle / Circle) per row, from new sheet
columns. Defaults match today's behaviour so existing sheets stay
identical without any migration.

## What we deliberately are NOT doing

**No-background option** — TikTok's Symphony Digital Avatar API does
not expose a transparent / chroma-key path. Empirical probe
2026-06-09: 10 payload variants (``transparent``, ``background_type``,
``transparent_background``, ``background_color``, ``output_format``,
``alpha``, ``bg_type``, ``nobg``, etc.) all rendered to **identical
videos** — same ``pix_fmt=yuv420p``, same codec, same first-frame
average colour ``(131,128,124)``. TikTok's gateway silently accepts
every unknown field and ignores it.

The remaining route — AI matting (RVM / MODNet / BackgroundMattingV2)
as a separate step — costs ~$0.05–0.10 per video and adds 10–30 s
latency. Quality on an overlay that's ~30 % of canvas width is
mediocre (edge hair / collars degrade visibly at small sizes). Not
worth the spend for a tiny overlay. Revisit if TikTok ever ships a
real transparency option.

## Decisions

1. **Size column** — discrete dropdown (Small / Medium / Large). Empty cell defaults to Medium.
   * Small  → 20 % canvas width
   * Medium → 30 % canvas width (today's hardcoded value)
   * Large  → 40 % canvas width
2. **Shape column** — discrete dropdown (Rectangle / Circle). Empty cell defaults to Rectangle.
   * Rectangle → no mask (today's behaviour)
   * Circle → ffmpeg ``geq`` alpha mask producing a hard-edged circular crop
3. **Auto-detect** at row read time via **header-name lookup** in Apps Script. If the column is missing from row 1 of the sheet, the row reader returns ``""`` and the backend falls back to the default. Operator opts in by adding the column heading — no migration script needed.

## Pipeline impact

Only the Rendi composite command + its caller change. Everything before
that (article fetch, script_gen, kie image, TikTok avatar) is untouched.

## Implementation

### `src/bulkvid/adapters/rendi.py`

* Extend ``_STILL_IMAGE_AVATAR_OVERLAY_TEMPLATE`` to take a shape variant:
  - rectangle (today): ``[1:v]scale=__OVERLAY_W__:-1[av]``
  - circle: crop to square, convert to yuva, ``geq`` alpha mask = `if(hypot(X-W/2,Y-H/2) < W/2, 255, 0)`, scale to overlay width.
* Or cleaner: keep one template, branch in the builder by ``shape``.
* Builder ``render_still_image_avatar_overlay_command`` gains
  ``shape: Literal["rectangle","circle"] = "rectangle"``.
* Client method ``still_image_with_avatar_overlay`` forwards
  ``shape`` to the builder.

### `src/bulkvid/models/row.py` — `AvatarRow`

Add two new fields, both defaulting to ``""``:
```python
avatar_size: str = ""    # "" | "small" | "medium" | "large"
avatar_shape: str = ""   # "" | "rectangle" | "circle"
```

### `src/bulkvid/routes/jobs.py` — `AvatarRowIn` + `_build_avatar_row`

Mirror the model. Coerce input to lowercase and validate against the
small enum; **invalid → fall back to default**, don't 400 the whole
job (matches Apps Script's defensive dropdown UX).

### `src/bulkvid/orchestrator/row_processor_avatar.py`

* Replace the hardcoded ``AVATAR_OVERLAY_WIDTH_FRAC = 0.30`` use with
  a per-row resolution:
  ```python
  _SIZE_TO_FRAC = {"small": 0.20, "medium": 0.30, "large": 0.40}
  width_frac = _SIZE_TO_FRAC.get(row.avatar_size.lower(), 0.30)
  ```
* Resolve shape:
  ```python
  shape = row.avatar_shape.lower()
  if shape not in ("rectangle", "circle"):
      shape = "rectangle"
  ```
* Pass both into ``still_image_with_avatar_overlay``.
* Record both in ``metadata`` so a future "what did this row use?"
  question is one log read away.

### `apps_script/Code.gs`

* New `_findHeaderCol(sheet, headerName)` helper — case-insensitive,
  trim-aware row-1 lookup. Returns the 1-based column index or 0.
* `_readAvatarRow` calls the helper for ``Avatar Size`` and
  ``Avatar Shape``. If the helper returns 0, the wire field stays
  ``""`` and the backend defaults take over.
* No change to ``AVATAR_COLS`` (still position-based for the existing
  columns).

### Tests (rule 18)

* `test_rendi.py::test_still_image_avatar_overlay_command_circle_shape`
  — asserts the circle path emits ``crop`` + ``geq`` + ``yuva`` and the
  rectangle path doesn't.
* `test_row_processor_avatar.py::test_avatar_size_small_uses_20pct_width`
  — fakes a row with ``avatar_size="small"`` and asserts the resolved
  ``overlay_width_px`` matches 0.20 × canvas width.
* `test_row_processor_avatar.py::test_avatar_shape_circle_passes_through`
  — asserts the row processor calls Rendi with ``shape="circle"``.
* Full unit suite green.

## Security (rule 13)

No new surface. The two new fields are short strings validated against
a fixed enum at the route boundary; no SQL, no shell, no provider
mutation. Defaults are baked in code so a malformed input never lands
in the ffmpeg filter graph.

## Observability (rule 14)

Add to the existing ``row_start`` log:
``avatar_size`` and ``avatar_shape`` (the resolved values, not the raw
input — so operator-visible logs match what the renderer actually did).
Same line, no new namespace.

## Settings audit (rule 15)

No new admin-editable settings. The 20/30/40 size table and the shape
enum are baked in code; if you want operator-side tuning later, those
become candidates for the settings store. Flagging deliberately —
hardcoding now keeps the surface tight.

## Files touched

- `src/bulkvid/adapters/rendi.py`
- `src/bulkvid/models/row.py`
- `src/bulkvid/routes/jobs.py`
- `src/bulkvid/orchestrator/row_processor_avatar.py`
- `apps_script/Code.gs`
- `tests/unit/test_rendi.py`
- `tests/unit/test_row_processor_avatar.py`
