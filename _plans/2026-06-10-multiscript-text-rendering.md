# Multi-script text rendering (Thai tofu fix)

Date: 2026-06-10
Status: approved in chat (user: "Look how Thai language appears! Must be
fixed for all tabs and all languages!", then confirmed the same tofu on
Hong Kong Chinese, Japanese and Hebrew screenshots)

## Problem

Screenshot from the text_on_img tab: a Thai headline rendered as a grid
of "no glyph" tofu boxes on the composed image. Root causes, confirmed
in code and on the dev box:

1. **Missing glyphs.** The generic font path (`_load_font` in
   `card_renderer.py`) only ever loads bundled Inter, which covers
   Latin (+Ext), Cyrillic, Greek and Vietnamese. It has NO Thai,
   Japanese, Hebrew or Arabic glyphs. The sheet has TH, JP and IL rows
   today. Template 3 has a per-script picker (Heebo/Cairo/Oswald) but
   it also lacks Thai and Japanese.
2. **No complex-text engine.** Pillow's bundled raqm needs system
   libfribidi at runtime; neither the dev box nor the Docker image has
   it (`features.check('raqm')` is False, Dockerfile installs no
   fribidi). Without it Hebrew renders reversed, Arabic unjoined, and
   Thai combining marks misplace even with the right font.
3. **Wrap assumes spaces.** `_wrap_text_to_width` splits on whitespace.
   Thai and Japanese write without spaces, so an entire sentence is one
   "word": the auto-fit either shrinks to the floor size or overflows.

Affected paths: `text_overlay.py` (text_on_img tab), `card_renderer.py`
templates 1/2/3 title + CTA pill (simple x4 tab), `cartoon_cta.py`
(CTA pill on cartoon/avatar videos). ZapCap captions are rendered by
ZapCap, not by us — out of scope.

## Decision

Per-script font routing, generalized from the existing Template 3
pattern (option B below).

* Bundle **Noto Sans Thai** (variable, wght+wdth), **Noto Sans JP**
  (variable, wght) and **Noto Sans HK** (variable, wght; Traditional
  Chinese with Hong Kong glyph conventions). All SIL OFL like the five
  fonts already bundled.
* New central detector `_non_latin_script_for(text)` in
  `card_renderer.py` returning
  `hebrew | arabic | cyrillic | thai | jp | zh | None`. Han is shared
  between Japanese and Chinese (Han unification): kana anywhere in the
  string means Japanese; Han with no kana means Chinese (HK font).
  Cyrillic/Greek stay on Inter for the generic path, which covers them.
* `_load_font(size, override, text="")` routes: Hebrew -> Heebo@700,
  Arabic -> Cairo@700, Thai -> Noto Thai@700, JP -> Noto JP@700,
  ZH -> Noto HK@700, else Inter (unchanged). All callers pass the text
  they are about to draw.
* Template 3 picker gains Thai + JP + ZH routes -> the Noto fonts
  pinned at 900 to match its Black-weight look. Side fix: Cairo
  declares [wght, slnt] and the old single-value weight pin raised
  inside set_variation_by_axes and silently left Arabic at Regular;
  the new full-axes pin (`_T3_FONT_AXES`) fixes that.
* `_wrap_text_to_width`: when a single word alone exceeds max_width,
  break it at character level so spaceless scripts wrap instead of
  overflowing.
* Dockerfile: apt-install `libfribidi0`. Pillow wheels since 8.2 bundle
  a libraqm that dlopens fribidi at runtime (verified against current
  Pillow docs 2026-06-10); with it present, draw.text shapes and
  reorders complex scripts automatically. Log raqm availability once at
  renderer import so the deploy can be verified from HF logs.

## Rejected alternatives

* **One pan-Unicode font.** No single TTF covers Latin+Thai+CJK+RTL;
  Noto merges top out before CJK. Dead end.
* **External rendering (browser/HarfBuzz service).** Correct but adds a
  service + latency + cost for what Pillow+raqm does in-process.
* **Pure-python bidi/shaping fallback (python-bidi + arabic-reshaper).**
  Two more deps to do at runtime what fribidi does better; only wins if
  we could not touch the image, but we own the Dockerfile.

## Round 2 (same day): full coverage + safety net

A Korean tofu screenshot arrived hours after round 1 shipped — the
"only scripts visible in the sheet" scoping was wrong for an ads
operation that adds markets without warning. Round 2 adds:

* **Noto Sans KR** (Korean; hangul anywhere wins over Han, so
  Korean-with-Hanja routes correctly), **Noto Sans SC** (Simplified
  Chinese) and **Noto Sans Devanagari** (Hindi).
* ``zh`` split into ``zh-hant`` / ``zh-hans`` via distinctive-character
  marker sets (们/們, 这/這, ...). Ambiguous all-shared strings default
  to Traditional — the sheet's Chinese market is Hong Kong.
* ``_warn_if_missing_glyphs``: every loaded font is checked against the
  text it is about to draw (once per unique font+text, pixel-compare vs
  the .notdef box). A coverage hole now logs a ``font_missing_glyphs``
  warning with the codepoints — silent tofu is structurally gone.

## Known limits (stated, not silent)

* Japanese-only Han with zero kana (rare in headlines) routes to the
  Chinese fonts: right characters, occasionally Chinese-style glyph
  variants. Acceptable; kana check covers real Japanese copy.
* Scripts beyond the bundled ten (e.g. Tamil, Burmese, Khmer) have no
  font yet — they trip the ``font_missing_glyphs`` warning instead of
  shipping silently. Adding one = bundle a Noto font + one range + one
  dict entry.
* Thai char-level wrap can break mid-word (proper Thai segmentation
  needs a dictionary, e.g. pythainlp). Acceptable for ad creatives;
  revisit only if the operator complains about awkward breaks.
* Local Windows dev renders RTL unshaped (no fribidi DLL); production
  output is what matters and the HF image gets fribidi.

## Tests

* Script detector unit tests (Thai, JP kana/kanji, Hebrew, Arabic,
  Latin, mixed, empty).
* `_load_font` routes to the expected font file per script
  (`font.path`).
* Tofu regression: mask of a Thai/JP/Hebrew char under the routed font
  differs from the .notdef mask (render U+0378, an unassigned
  codepoint, as the notdef reference).
* Wrap: spaceless Thai/JP strings produce multiple lines, each within
  max_width; Latin behavior unchanged.

## Security / cost

No new services, no new deps, no cost (fonts are SIL OFL). Fonts are
static assets in the repo; no user input reaches font selection beyond
codepoint-range checks.
