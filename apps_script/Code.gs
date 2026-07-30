/**
 * Aporia Bulk Video — Sheet integration.
 *
 * Bound Apps Script for the bulk team's spreadsheet.
 * Pairs with the FastAPI backend at BACKEND_URL.
 *
 * Plan: _plans/2026-06-02-aporia-bulk-video-tool.md §5 (Apps Script + sheet),
 *       §7 (Authentication: Google OAuth ID token + email allowlist),
 *       §15 Appendix A (column maps), Phase 6.
 */

// ─── Config ──────────────────────────────────────────────────────────────────

const TAB_IMAGE_VO = 'image_vo';
const TAB_FOUR_IMAGES = 'four_images_vo2';
const TAB_SIMPLE = 'simple';
const TAB_SIMPLE_MOTION = 'simple_motion';
const TAB_FAST_FURIOUS = 'fast_furious';
const TAB_CARTOON = 'cartoon';
const TAB_YT_CARTOON = 'yt_cartoon';
const TAB_SIMPLE_X4 = 'simple_x4';
const TAB_TEXT_ON_IMG = 'text_on_img';
const TAB_AVATAR = 'avatar';
const TAB_MOTION_ADS = 'motion_ads';
const TAB_HOOK_CARD = 'hook_card';

// Card-template preview asset URLs. The PNGs live in the HF Space repo
// (LFS-tracked) and are served directly by HuggingFace's resolver, which
// returns a 302 → signed CDN URL with Content-Type: image/png. Sheets
// =IMAGE() follows the redirect cleanly, so no separate hosting needed.
//
// The "_labeled" variants have a "DEFAULT" / "TEMPLATE 1" / "TEMPLATE 2" /
// "TEMPLATE 3" caption baked into the top of each PNG so the in-sheet
// preview row is self-identifying — no extra label cells needed.
// Regenerate with `python tools/render_labeled_template_previews.py` after
// editing the source PNGs at
// `apps_script/template_previews/template_{default,1,2,3}.png`.
// Plan _plans/2026-06-08-simple-x4-template-cards.md §D.7;
// Template 3 added per _plans/2026-06-08-simple-x4-template-3.md;
// DEFAULT preview added 2026-06-09 so the operator can see what ships
// when the Template column is left blank.
const CARD_TEMPLATE_PREVIEW_URLS = {
  'default': 'https://huggingface.co/spaces/yoavaporia/aporia-bulkvid/resolve/main/apps_script/template_previews/template_default_labeled.png',
  '1': 'https://huggingface.co/spaces/yoavaporia/aporia-bulkvid/resolve/main/apps_script/template_previews/template_1_labeled.png',
  '2': 'https://huggingface.co/spaces/yoavaporia/aporia-bulkvid/resolve/main/apps_script/template_previews/template_2_labeled.png',
  '3': 'https://huggingface.co/spaces/yoavaporia/aporia-bulkvid/resolve/main/apps_script/template_previews/template_3_labeled.png',
};

// Submit-POST retry policy. The backend occasionally returns HTTP 5xx while a
// container is cold-starting or restarting (HF Spaces sleep after inactivity).
// The Apps Script retries the submit with the same idempotency key — server
// returns the original job_id, no duplicate. 6 attempts × backoff = ~31s
// total, comfortably wider than a cold-start window.
// See _plans/2026-06-04-submit-500-defensive-fix.md.
const SUBMIT_MAX_ATTEMPTS = 6;
const SUBMIT_BACKOFF_MS = [1000, 2000, 4000, 8000, 16000];

// Optional pre-warm: fire GET /health a moment before the submit so a cold
// backend container has a chance to lazy-init. Submit fires regardless of
// pre-warm outcome — pre-warm is purely a nudge.
const PREWARM_ENABLED = true;
const PREWARM_COOLDOWN_MS = 60 * 1000;

// Aspect ratios offered in the Change Size dropdown — must stay in sync with
// VALID_RATIO_STRINGS in src/bulkvid/adapters/rendi.py. Ordered by how often
// the bulk team uses them; 4:3 added per chat 2026-06-10. The validation is
// non-strict because the backend ALSO accepts free-typed WxH pixel values
// (e.g. 1080x1350).
const SIZE_DROPDOWN_OPTIONS = [
  '9:16', '4:5', '1:1', '16:9', '4:3', '3:4', '5:4', '2:3', '3:2', '21:9',
];

// The animated tabs render through the Seedance video model, which accepts a
// STRICTER set of sizes than the image tabs (no 2:3 / 3:2 / 4:5 / 5:4). Sending
// an unsupported ratio used to fail the whole video ("no Seedance clips
// produced"); the backend now snaps any other ratio to the nearest supported
// one, and these tabs get a curated Change Size dropdown + a header note so the
// operator sees the active model and its sizes at pick time. Keep
// SEEDANCE_SIZE_OPTIONS in sync with SEEDANCE_ALLOWED_ASPECT_RATIOS in
// src/bulkvid/adapters/kie.py.
const SEEDANCE_VIDEO_TABS = [TAB_SIMPLE_MOTION, TAB_FAST_FURIOUS, TAB_CARTOON, TAB_YT_CARTOON, TAB_MOTION_ADS];
const ACTIVE_VIDEO_MODEL_LABEL = 'Seedance 1.5 Pro';
const SEEDANCE_SIZE_OPTIONS = ['9:16', '3:4', '1:1', '4:3', '16:9', '21:9'];

// yt-cartoon dropdown options (2026-06-17). Tone toggles the narration style;
// Cap/CTA Position nudge the caption + CTA pill height relative to default;
// Vid Length caps the video at 10/15/20s. Backend coerces these defensively,
// so the labels just need to be recognisable (it lowercases + matches digits).
const YT_CARTOON_TONE_OPTIONS = ['Engaging', 'Calm'];
const YT_CARTOON_POSITION_OPTIONS = [
  'Much Higher', 'Higher', 'Default', 'Lower', 'Much Lower',
];
const YT_CARTOON_VID_LENGTH_OPTIONS = ['up to 10s', 'up to 15s', 'up to 20s'];

/** Column maps — MUST match src/bulkvid/adapters/sheets.py (1-indexed for Sheets). */
const IMAGE_VO_COLS = {
  country: 1, vertical: 2, article: 3, manualImage: 4,
  voiceOver: 5, zapcap: 6, aspectRatio: 7, scriptPattern: 8,
  openComments: 9,
  readyVideo1: 10, readyVideo2: 11, readyVideo3: 12, readyVideo4: 13,
  lastInputCol: 9,    // for selection reading
};

const FOUR_IMAGES_COLS = {
  country: 1, vertical: 2, article: 3, howMany: 4,
  voiceOver: 5, image1: 6, image2: 7, image3: 8, image4: 9,
  zapcap: 10, aspectRatio: 11, scriptPattern: 12, openComments: 13,
  readyVideo1: 14, readyVideo2: 15, readyVideo3: 16, readyVideo4: 17,
  lastInputCol: 13,
};

// Simple x4 (post-migration). Same A-H as IMAGE_VO_COLS, plus 8 new columns
// for per-video Template + CTA pairs, then Open Comments and Ready Video 1-4
// shifted right by 8. Data starts at sheet ROW 3 (row 1 = preview header,
// row 2 = column names). Plan _plans/2026-06-08-simple-x4-template-cards.md §D.1.
const SIMPLE_X4_COLS = {
  country: 1, vertical: 2, article: 3, manualImage: 4,
  voiceOver: 5, zapcap: 6, aspectRatio: 7, scriptPattern: 8,
  template1: 9, cta1: 10,
  template2: 11, cta2: 12,
  template3: 13, cta3: 14,
  template4: 15, cta4: 16,
  openComments: 17,
  readyVideo1: 18, readyVideo2: 19, readyVideo3: 20, readyVideo4: 21,
  lastInputCol: 17,
};

// Cartoon tab (post-2026-06-08 CTA column insertion). Inherits A-H from the
// Image-VO layout, inserts CTA (Yes/No dropdown) at I and CTA Text at J,
// then shifts Open Comments + Ready Video 1/2 right by 2. Cartoon only
// produces 2 videos per row (not 4), so readyVideo columns end at M.
const CARTOON_COLS = {
  country: 1, vertical: 2, article: 3, manualImage: 4,
  voiceOver: 5, zapcap: 6, aspectRatio: 7, scriptPattern: 8,
  ctaEnabled: 9, ctaText: 10,
  openComments: 11,
  readyVideo1: 12, readyVideo2: 13,
  lastInputCol: 11,
};

// simple-motion tab (2026-06-22): animate super-realistic images. Image-VO A-C,
// then TWO Manual Image columns (D = shot 1, E = shot 2), then Voice Over /
// ZapCap / Change Size / Script Pattern, then CTA + CTA Text (mirrors cartoon),
// then Open Comments + Ready Video. Read by HEADER NAME first (like avatar /
// yt-cartoon) so inserting/moving columns can't corrupt the read; these
// positional values are the fallback. ONE video per row → Ready Video 1 (M).
const SIMPLE_MOTION_COLS = {
  country: 1, vertical: 2, article: 3,
  manualImage1: 4, manualImage2: 5,
  voiceOver: 6, zapcap: 7, aspectRatio: 8, scriptPattern: 9,
  ctaEnabled: 10, ctaText: 11,
  openComments: 12,
  readyVideo1: 13, readyVideo2: 14,
  lastInputCol: 12,
};

// fast-and-furious tab (2026-07-30): like simple-motion (two Manual Image columns
// D/E, shared across videos) PLUS a Number of Videos column (F) and FOUR Change
// Size columns (I-L, one aspect per output video), feeding Ready Video 1-4 (Q-T).
// Each row produces up to 4 Gen-Z variations. Read by HEADER NAME first (like
// simple-motion) — but the four Change Size columns share a header, so they are
// read POSITIONALLY. These positional values are the fallback. Plan
// _plans/2026-07-30-fast-and-furious-tab.md.
const FAST_FURIOUS_COLS = {
  country: 1, vertical: 2, article: 3,
  manualImage1: 4, manualImage2: 5,
  numVideos: 6, voiceOver: 7, zapcap: 8,
  changeSize1: 9, changeSize2: 10, changeSize3: 11, changeSize4: 12,
  scriptPattern: 13, ctaEnabled: 14, ctaText: 15,
  openComments: 16,
  readyVideo1: 17, readyVideo2: 18, readyVideo3: 19, readyVideo4: 20,
  lastInputCol: 16,
};

// yt-cartoon tab (2026-06-17): the cartoon layout PLUS four new columns
// inserted after ZapCap (F) — Tone (G), Cap Position (H), CTA Position (I),
// Vid Length (J) — which shift Change Size..Ready Video right by 4. Read by
// HEADER NAME first (like the avatar tab) so inserting/moving columns can't
// silently corrupt the input; these positional values are the fallback for a
// fresh sheet. Manual Image (D) is present but ignored (scenes are generated).
const YT_CARTOON_COLS = {
  country: 1, vertical: 2, article: 3, manualImage: 4,
  voiceOver: 5, zapcap: 6,
  tone: 7, capPosition: 8, ctaPosition: 9, vidLength: 10,
  aspectRatio: 11, scriptPattern: 12, ctaEnabled: 13, ctaText: 14,
  openComments: 15,
  readyVideo1: 16, readyVideo2: 17,
  lastInputCol: 15,
};

// paste text on img (2026-06-09): A-D from Image-VO, then a Text column at
// E (the overlay text), then the standard F-J input columns shifted right
// by 1. One video per row, so Ready Video lands at K.
const TEXT_ON_IMG_COLS = {
  country: 1, vertical: 2, article: 3, manualImage: 4,
  text: 5,
  voiceOver: 6, zapcap: 7, aspectRatio: 8, scriptPattern: 9,
  openComments: 10,
  readyVideo1: 11,
  lastInputCol: 10,
};

// video with avatar (2026-06-09): A-D from Image-VO, plus Avatar ID at E,
// shifted F-I, then CTA Yes/No + CTA Text mirroring cartoon, then Open
// Comments + Ready Video. One video per row, so Ready Video lands at M.
const AVATAR_COLS = {
  country: 1, vertical: 2, article: 3, manualImage: 4,
  avatarId: 5,
  voiceOver: 6, zapcap: 7, aspectRatio: 8, scriptPattern: 9,
  ctaEnabled: 10, ctaText: 11,
  openComments: 12,
  readyVideo1: 13,
  lastInputCol: 12,
};

// Motion_Ads tab (2026-07-08): a silent motion-ad video + ad copy. Country /
// Vertical / Article, then TWO text OUTPUT columns the backend writes — Headline
// (D) and Description (E) — then Manual Image (F), Apple Yes/No (G), Change Size
// (H), Open Comments (I), Ready Video (J). Read by HEADER NAME first (like avatar
// / simple-motion) so inserting/moving columns can't corrupt the read; these
// positional values are the fallback. ONE video per row. Manual Image is
// optional (blank → generate; filled → animate as-is). Apple = Yes → the
// generated image has no people.
const MOTION_ADS_COLS = {
  country: 1, vertical: 2, article: 3,
  headline: 4, description: 5,
  manualImage: 6, apple: 7, aspectRatio: 8,
  openComments: 9,
  readyVideo1: 10,
  lastInputCol: 9,
};

// Hook_Card layout: Country / Vertical / Article / Num of Images / Text /
// Voiceover / Music, then FIVE Manual Media columns (H-L), then Change Size /
// Open Comments, then ONE Ready Video (O). Read BY HEADER NAME (mirrors
// motion_ads) so inserting/moving columns can't corrupt the read; these
// positional values are the fallback.
const HOOK_CARD_COLS = {
  country: 1, vertical: 2, article: 3,
  numImages: 4, text: 5, voiceover: 6, music: 7,
  manualMedia1: 8, manualMedia2: 9, manualMedia3: 10,
  manualMedia4: 11, manualMedia5: 12,
  aspectRatio: 13, openComments: 14,
  readyVideo1: 15,
  lastInputCol: 14,
};

// Pickable names for the Music column (col G) dropdown. MUST match the track
// file names produced by tools/generate_hook_card_music.py (a blank cell = a
// random track; "None" = silent). Applied via applyHookCardMusicDropdown().
const HOOK_CARD_MUSIC_NAMES = [
  'Uplifting', 'Cinematic', 'Piano', 'Lofi', 'Acoustic',
  'Energetic', 'Ambient', 'Electronic', 'Indie',
];

// Base URL for the music-preview players. The tracks live in the HF Space repo
// (LFS) and are served by HuggingFace's resolver (302 → CDN), exactly like the
// card-template previews above. Only resolves once the tracks are deployed
// (git push hf main). File names: <name-lowercased>_1.mp3 / _2.mp3.
const HOOK_CARD_MUSIC_BASE_URL =
  'https://huggingface.co/spaces/yoavaporia/aporia-bulkvid/resolve/main/src/bulkvid/assets/music/';

// Row indices for the post-migration simple_x4 layout.
const SIMPLE_X4_PREVIEW_ROW = 1;    // template preview images (frozen)
const SIMPLE_X4_HEADER_ROW = 2;     // column names (frozen)
const SIMPLE_X4_FIRST_DATA_ROW = 3; // data starts here


// ─── Menu ────────────────────────────────────────────────────────────────────

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Aporia Bulk Video')
    .addItem('Generate selected rows', 'generateSelected')
    .addItem('Generate all unprocessed', 'generateAllUnprocessed')
    .addSeparator()
    .addItem('Show job status sidebar', 'showJobsSidebar')
    .addItem('Active models & sizes…', 'showActiveModels')
    .addItem('Pick avatar for current row…', 'pickAvatarForCurrentRow')
    .addSeparator()
    .addItem('Migrate simple x4 columns…', 'migrateSimpleX4Columns')
    .addItem('Update size dropdowns on all tabs', 'applySizeDropdowns')
    .addItem('Apply yt-cartoon dropdowns', 'applyYtCartoonDropdowns')
    .addItem('Apply Motion_Ads dropdowns', 'applyMotionAdsDropdowns')
    .addItem('Apply Hook_Card dropdowns', 'applyHookCardMusicDropdown')
    .addItem('Preview Hook_Card music…', 'showHookCardMusicPreview')
    .addItem('Add "use this script" tips', 'applyOpenCommentsTips')
    .addItem('Configure backend URL', 'configureBackendUrl')
    .addToUi();
}


// ─── Settings ────────────────────────────────────────────────────────────────

function _getBackendUrl() {
  const url = PropertiesService.getScriptProperties().getProperty('BACKEND_URL');
  if (!url) {
    throw new Error(
      'BACKEND_URL not configured. Use "Aporia Bulk Video → Configure backend URL".'
    );
  }
  return url.replace(/\/+$/, '');    // strip trailing slash
}


function configureBackendUrl() {
  const ui = SpreadsheetApp.getUi();
  const current = PropertiesService.getScriptProperties()
                    .getProperty('BACKEND_URL') || '(none)';
  const response = ui.prompt(
    'Backend URL',
    'Enter the backend URL (current: ' + current + '):\n' +
    'e.g. https://<owner>-aporia-bulkvid.hf.space',
    ui.ButtonSet.OK_CANCEL
  );
  if (response.getSelectedButton() !== ui.Button.OK) return;
  const newUrl = response.getResponseText().trim();
  if (!newUrl) return;
  PropertiesService.getScriptProperties().setProperty('BACKEND_URL', newUrl);
  ui.alert('Backend URL set to: ' + newUrl);
}


// ─── Tab detection ───────────────────────────────────────────────────────────

function _detectTabType(sheet) {
  // Detect by sheet NAME first (these tabs share the Image-VO columns).
  const name = String(sheet.getName() || '').toLowerCase().trim();
  // "video with avatar" / "avatar" -> kie+seedance scenes + TikTok avatar
  // narration overlaid at bottom-left (2026-06-09). Checked BEFORE
  // generic name matches.
  if (name.indexOf('avatar') !== -1) return TAB_AVATAR;
  // "Motion_Ads" -> silent motion-ad video (no VO/CTA/sound) + Headline /
  // Description ad copy (2026-07-08). The name has no "simple"/"cartoon", so
  // order vs those matches is moot; kept up top with the other name detections.
  if (name.indexOf('motion_ads') !== -1 || name.indexOf('motion ads') !== -1
      || name.indexOf('motion-ads') !== -1) {
    return TAB_MOTION_ADS;
  }
  // "Hook_Card" -> 9:16 hook-box slideshow + background music, no voiceover
  // (2026-07-13). The name has no "simple"/"cartoon", so order vs those is
  // moot; kept up top with the other name detections.
  if (name.indexOf('hook_card') !== -1 || name.indexOf('hook card') !== -1
      || name.indexOf('hook-card') !== -1) {
    return TAB_HOOK_CARD;
  }
  // "paste text on img" -> manual image + center-overlay text (2026-06-09).
  // Checked BEFORE "simple" because the name doesn't contain "simple" — but
  // ordered up top alongside the other name-based detections for clarity.
  if (name.indexOf('text on img') !== -1 || name.indexOf('paste text') !== -1) {
    return TAB_TEXT_ON_IMG;
  }
  // "simple x4" -> needs disambiguation: post-migration it has 2 header rows
  // and the new Template/CTA columns; pre-migration it's the legacy image_vo
  // shape. Must be checked BEFORE plain "simple" since the name contains it.
  if (name.indexOf('x4') !== -1) {
    return _isSimpleX4Migrated(sheet) ? TAB_SIMPLE_X4 : TAB_IMAGE_VO;
  }
  // "fast-and-furious" -> simple-motion variant with TikTok/Gen-Z narration and
  // no silent tail (2026-07-30). The name contains none of the other tab
  // keywords, so ordering vs them is moot; kept up top with the name detections.
  if (name.indexOf('fast-and-furious') !== -1 || name.indexOf('fast and furious') !== -1
      || name.indexOf('fast_and_furious') !== -1 || name.indexOf('fast-furious') !== -1) {
    return TAB_FAST_FURIOUS;
  }
  // "simple-motion" -> animate super-realistic images (manual D/E or generated).
  // MUST be checked BEFORE the generic "simple" match below, since the name
  // "simple-motion" contains "simple".
  if (name.indexOf('simple-motion') !== -1 || name.indexOf('simple motion') !== -1) {
    return TAB_SIMPLE_MOTION;
  }
  // "simple" -> ONE video from the existing Manual Image, NO image generation.
  if (name.indexOf('simple') !== -1) return TAB_SIMPLE;
  // "yt-cartoon" -> engaging, variable-length cartoon (Tone / Cap Position /
  // CTA Position / Vid Length knobs). MUST be checked BEFORE the generic
  // "cartoon" match below, since the name "yt-cartoon" contains "cartoon".
  if (name.indexOf('yt-cartoon') !== -1 || name.indexOf('yt cartoon') !== -1) {
    return TAB_YT_CARTOON;
  }
  // "cartoon" -> generate animated multi-shot videos from text (no seed image).
  // Checked by NAME because the tab shares the Image-VO "Manual Image" header.
  if (name.indexOf('cartoon') !== -1) return TAB_CARTOON;

  const lastCol = sheet.getLastColumn();
  if (lastCol === 0) return null;
  const headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0]
    .map(function (h) { return String(h || '').toLowerCase().trim(); });

  if (headers.indexOf('how many') !== -1) return TAB_FOUR_IMAGES;
  if (headers.indexOf('manual image') !== -1) return TAB_IMAGE_VO;
  return null;
}


/** Probe a tab to decide whether it has been migrated to the new simple_x4
 *  layout. Migrated = "Template 1" header sits at row 2 col I (the position
 *  the migration menu writes). Returns true even if other things look weird,
 *  because the header is the authoritative signal — if the operator added
 *  "Template 1" by hand, the backend can read the tab. */
function _isSimpleX4Migrated(sheet) {
  if (sheet.getLastRow() < SIMPLE_X4_HEADER_ROW) return false;
  if (sheet.getLastColumn() < SIMPLE_X4_COLS.template1) return false;
  const cell = sheet
    .getRange(SIMPLE_X4_HEADER_ROW, SIMPLE_X4_COLS.template1)
    .getValue();
  return String(cell || '').toLowerCase().trim().indexOf('template 1') === 0;
}


// ─── Row readers ─────────────────────────────────────────────────────────────

function _cell(values, idx1based) {
  const v = values[idx1based - 1];
  return v == null ? '' : String(v).trim();
}

/** Read a "Change Size" cell into a ratio string the backend can parse.
 *  Google Sheets casts a typed ratio like "9:16" to a TIME value (09:16), so
 *  getValues() hands back a Date and String(date) is a full date string
 *  ("Sat Dec 30 1899 09:16:00 GMT…") the backend can't parse — it then falls
 *  back to the tab default, so a picked 9:16 silently became the default
 *  (16:9 on Motion_Ads). Recover the ratio from the Date's hours:minutes,
 *  which round-trips every dropdown option exactly (9:16, 16:9, 4:5, 21:9…).
 *  A plain string (blank, or a free-typed "1080x1350") passes straight
 *  through unchanged. */
function _aspectCell(values, idx1based) {
  const v = values[idx1based - 1];
  if (v instanceof Date) {
    return v.getHours() + ':' + v.getMinutes();
  }
  return v == null ? '' : String(v).trim();
}

function _yes(value, def) {
  const v = String(value || '').toLowerCase();
  if (!v) return def;
  return v === 'yes' || v === 'y' || v === 'true' || v === '1';
}


/** Find a column by its row-1 header name (case-insensitive, trim-aware).
 *  Returns the 1-based column index, or 0 when the header isn't present.
 *  Lightweight helper kept for callers that only need ONE column. The
 *  row readers below build a full header→col map once per read instead.
 *  Plan _plans/2026-06-09-avatar-overlay-size-shape.md. */
function _findHeaderCol(sheet, headerName) {
  const lastCol = sheet.getLastColumn();
  if (lastCol === 0) return 0;
  const headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
  const target = String(headerName).toLowerCase().trim();
  for (var i = 0; i < headers.length; i++) {
    if (String(headers[i] || '').toLowerCase().trim() === target) {
      return i + 1;
    }
  }
  return 0;
}


/** Build a {normalized-header → 1-based column index} map from row 1.
 *  First-occurrence wins (so a duplicate header is harmless). Used by
 *  ``_readAvatarRow`` so the operator can insert columns ANYWHERE without
 *  breaking the input read.
 *
 *  Chat 2026-06-09: an operator added "Avatar Size" + "Avatar Shape"
 *  between "Avatar ID" and "Voice Over"; every subsequent column
 *  shifted right by 2 and the positional reads grabbed the wrong cells
 *  (voice_over read "Small", aspect_ratio read "Yes", etc.). Reading
 *  by header name makes the input layout robust to column reordering. */
function _buildHeaderColMap(sheet) {
  const lastCol = sheet.getLastColumn();
  if (lastCol === 0) return {};
  const headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
  const map = {};
  for (var i = 0; i < headers.length; i++) {
    const name = String(headers[i] || '').toLowerCase().trim();
    if (name && !(name in map)) {    // first occurrence wins
      map[name] = i + 1;
    }
  }
  return map;
}


/** Look up the column for the first matching header name in ``candidates``.
 *  Returns the 1-based column index, or ``fallback`` (also 1-based) when
 *  none of the candidates is present.
 *
 *  ``candidates`` is an array because some columns have known synonyms —
 *  e.g. some sheets label the avatar id column "Avatar ID (NEW)" while
 *  others use just "Avatar ID". We accept either rather than forcing a
 *  one-time rename. */
function _colForHeaders(headerMap, candidates, fallback) {
  for (var i = 0; i < candidates.length; i++) {
    const name = String(candidates[i] || '').toLowerCase().trim();
    if (name && (name in headerMap)) return headerMap[name];
  }
  return fallback || 0;
}


function _readImageVORow(sheet, rowNum) {
  const cols = IMAGE_VO_COLS;
  const values = sheet.getRange(rowNum, 1, 1, cols.lastInputCol).getValues()[0];
  return {
    row_num: rowNum,
    country: _cell(values, cols.country),
    vertical: _cell(values, cols.vertical),
    article_url: _cell(values, cols.article),
    manual_image_url: _cell(values, cols.manualImage),
    voice_over: _yes(_cell(values, cols.voiceOver), true),
    zapcap: _yes(_cell(values, cols.zapcap), false),
    // Blank "Change Size" flows through as "" so the backend probes the
    // manual image's native pixel dimensions instead of silently
    // defaulting to 9:16. Plan
    // _plans/2026-06-14-blank-size-uses-native-image.md.
    aspect_ratio: _aspectCell(values, cols.aspectRatio),
    script_pattern: _cell(values, cols.scriptPattern),
    open_comments: _cell(values, cols.openComments),
  };
}


function _readFourImagesRow(sheet, rowNum) {
  const cols = FOUR_IMAGES_COLS;
  const values = sheet.getRange(rowNum, 1, 1, cols.lastInputCol).getValues()[0];
  const howMany = parseInt(_cell(values, cols.howMany), 10) || 0;
  const imageUrls = [
    _cell(values, cols.image1),
    _cell(values, cols.image2),
    _cell(values, cols.image3),
    _cell(values, cols.image4),
  ].slice(0, howMany).filter(function (u) { return !!u; });

  return {
    row_num: rowNum,
    country: _cell(values, cols.country),
    vertical: _cell(values, cols.vertical),
    article_url: _cell(values, cols.article),
    how_many: howMany,
    voice_over: _yes(_cell(values, cols.voiceOver), true),
    image_urls: imageUrls,
    zapcap: _yes(_cell(values, cols.zapcap), false),
    // Blank → backend probes image_urls[0] for native dims. See _readImageVORow.
    aspect_ratio: _aspectCell(values, cols.aspectRatio),
    script_pattern: _cell(values, cols.scriptPattern),
    open_comments: _cell(values, cols.openComments),
  };
}


function _readCartoonRow(sheet, rowNum) {
  // Cartoon uses CARTOON_COLS — post-2026-06-08 layout has CTA (Yes/No) at I
  // and CTA Text at J, with Open Comments + Ready Video columns shifted right
  // by 2. Manual Image (D) is present in the sheet but ignored (cartoon
  // scenes are generated from scratch, no seed image).
  const cols = CARTOON_COLS;
  const values = sheet.getRange(rowNum, 1, 1, cols.lastInputCol).getValues()[0];
  return {
    row_num: rowNum,
    country: _cell(values, cols.country),
    vertical: _cell(values, cols.vertical),
    article_url: _cell(values, cols.article),
    voice_over: _yes(_cell(values, cols.voiceOver), true),
    zapcap: _yes(_cell(values, cols.zapcap), false),
    aspect_ratio: _aspectCell(values, cols.aspectRatio) || '9:16',
    script_pattern: _cell(values, cols.scriptPattern),
    cta_enabled: _yes(_cell(values, cols.ctaEnabled), false),
    cta_text: _cell(values, cols.ctaText).slice(0, 80),    // bound at 80 chars
    open_comments: _cell(values, cols.openComments),
  };
}


function _readSimpleMotionRow(sheet, rowNum) {
  // simple-motion: cartoon-style inputs PLUS two Manual Image columns (D = shot
  // 1, E = shot 2). Every column is resolved by HEADER NAME first (mirrors
  // _readAvatarRow / _readYtCartoonRow) so the operator can insert/move columns
  // without breaking the read; the SIMPLE_MOTION_COLS positional index is the
  // fallback. A blank image cell → the backend generates a realistic image; a
  // filled cell → the backend animates it as-is.
  const cols = SIMPLE_MOTION_COLS;
  const headerMap = _buildHeaderColMap(sheet);
  const lastCol = Math.max(cols.lastInputCol, sheet.getLastColumn());
  const values = sheet.getRange(rowNum, 1, 1, lastCol).getValues()[0];

  const cCountry      = _colForHeaders(headerMap, ['Country'], cols.country);
  const cVertical     = _colForHeaders(headerMap, ['Vertical'], cols.vertical);
  const cArticle      = _colForHeaders(headerMap, ['Article'], cols.article);
  const cManualImage1 = _colForHeaders(headerMap, ['Manual Image 1', 'Manual Image'], cols.manualImage1);
  const cManualImage2 = _colForHeaders(headerMap, ['Manual Image 2'], cols.manualImage2);
  const cVoiceOver    = _colForHeaders(headerMap, ['Voice Over', 'VoiceOver'], cols.voiceOver);
  const cZapcap       = _colForHeaders(headerMap, ['ZapCap'], cols.zapcap);
  const cAspectRatio  = _colForHeaders(headerMap, ['Change Size', 'Aspect Ratio'], cols.aspectRatio);
  const cScriptPat    = _colForHeaders(headerMap, ['Script Pattern'], cols.scriptPattern);
  const cCtaEnabled   = _colForHeaders(headerMap, ['CTA'], cols.ctaEnabled);
  const cCtaText      = _colForHeaders(headerMap, ['CTA Text'], cols.ctaText);
  const cOpenComments = _colForHeaders(headerMap, ['Open Comments', 'Open Comment'], cols.openComments);

  return {
    row_num: rowNum,
    country: _cell(values, cCountry),
    vertical: _cell(values, cVertical),
    article_url: _cell(values, cArticle),
    manual_image_1: _cell(values, cManualImage1),
    manual_image_2: _cell(values, cManualImage2),
    voice_over: _yes(_cell(values, cVoiceOver), true),
    zapcap: _yes(_cell(values, cZapcap), false),
    aspect_ratio: _aspectCell(values, cAspectRatio) || '9:16',
    script_pattern: _cell(values, cScriptPat),
    cta_enabled: _yes(_cell(values, cCtaEnabled), false),
    cta_text: _cell(values, cCtaText).slice(0, 80),
    open_comments: _cell(values, cOpenComments),
  };
}


function _readFastFuriousRow(sheet, rowNum) {
  // fast-and-furious: simple-motion inputs PLUS Number of Videos (F) and FOUR
  // Change Size columns (one aspect per output video), feeding Ready Video 1-4.
  // Header-name first for everything EXCEPT the four Change Size cells: they share
  // the "Change Size" header, so they're read POSITIONALLY (FAST_FURIOUS_COLS).
  // Both Manual Image cells are SHARED by all videos (blank → generated; filled →
  // animated as-is). Plan _plans/2026-07-30-fast-and-furious-tab.md.
  const cols = FAST_FURIOUS_COLS;
  const headerMap = _buildHeaderColMap(sheet);
  const lastCol = Math.max(cols.lastInputCol, sheet.getLastColumn());
  const values = sheet.getRange(rowNum, 1, 1, lastCol).getValues()[0];

  const cCountry      = _colForHeaders(headerMap, ['Country'], cols.country);
  const cVertical     = _colForHeaders(headerMap, ['Vertical'], cols.vertical);
  const cArticle      = _colForHeaders(headerMap, ['Article'], cols.article);
  const cManualImage1 = _colForHeaders(headerMap, ['Manual Image 1', 'Manual Image'], cols.manualImage1);
  const cManualImage2 = _colForHeaders(headerMap, ['Manual Image 2'], cols.manualImage2);
  const cNumVideos    = _colForHeaders(headerMap, ['Number of Videos', 'Number of Video', 'Num Videos'], cols.numVideos);
  const cVoiceOver    = _colForHeaders(headerMap, ['Voice Over', 'VoiceOver'], cols.voiceOver);
  const cZapcap       = _colForHeaders(headerMap, ['ZapCap'], cols.zapcap);
  const cScriptPat    = _colForHeaders(headerMap, ['Script Pattern'], cols.scriptPattern);
  const cCtaEnabled   = _colForHeaders(headerMap, ['CTA'], cols.ctaEnabled);
  const cCtaText      = _colForHeaders(headerMap, ['CTA Text'], cols.ctaText);
  const cOpenComments = _colForHeaders(headerMap, ['Open Comments', 'Open Comment'], cols.openComments);

  const numRaw = parseInt(_cell(values, cNumVideos), 10);
  const numVideos = isNaN(numRaw) ? 1 : Math.max(1, Math.min(4, numRaw));

  // The four Change Size columns share the "Change Size" header, so read them
  // positionally. Blank cells stay '' → the backend defaults that video to 9:16.
  const aspectRatios = [
    _aspectCell(values, cols.changeSize1),
    _aspectCell(values, cols.changeSize2),
    _aspectCell(values, cols.changeSize3),
    _aspectCell(values, cols.changeSize4),
  ];

  return {
    row_num: rowNum,
    country: _cell(values, cCountry),
    vertical: _cell(values, cVertical),
    article_url: _cell(values, cArticle),
    manual_image_1: _cell(values, cManualImage1),
    manual_image_2: _cell(values, cManualImage2),
    num_videos: numVideos,
    voice_over: _yes(_cell(values, cVoiceOver), true),
    zapcap: _yes(_cell(values, cZapcap), false),
    aspect_ratios: aspectRatios,
    script_pattern: _cell(values, cScriptPat),
    cta_enabled: _yes(_cell(values, cCtaEnabled), false),
    cta_text: _cell(values, cCtaText).slice(0, 80),
    open_comments: _cell(values, cOpenComments),
  };
}


function _readMotionAdsRow(sheet, rowNum) {
  // Motion_Ads: Country / Vertical / Article, then TWO OUTPUT columns the backend
  // fills (Headline D, Description E) which are NOT read here, then Manual Image /
  // Apple / Change Size / Open Comments. Resolved by HEADER NAME first (mirrors
  // _readSimpleMotionRow / _readAvatarRow) so inserting/moving columns can't
  // corrupt the read; MOTION_ADS_COLS is the positional fallback. Only the
  // article is required — a blank Manual Image means "generate the image".
  const cols = MOTION_ADS_COLS;
  const headerMap = _buildHeaderColMap(sheet);
  const lastCol = Math.max(cols.lastInputCol, sheet.getLastColumn());
  const values = sheet.getRange(rowNum, 1, 1, lastCol).getValues()[0];

  const cCountry      = _colForHeaders(headerMap, ['Country'], cols.country);
  const cVertical     = _colForHeaders(headerMap, ['Vertical'], cols.vertical);
  const cArticle      = _colForHeaders(headerMap, ['Article'], cols.article);
  const cManualImage  = _colForHeaders(headerMap, ['Manual Image'], cols.manualImage);
  const cApple        = _colForHeaders(headerMap, ['Apple'], cols.apple);
  const cAspectRatio  = _colForHeaders(headerMap, ['Change Size', 'Aspect Ratio'], cols.aspectRatio);
  const cOpenComments = _colForHeaders(headerMap, ['Open Comments', 'Open Comment'], cols.openComments);

  return {
    row_num: rowNum,
    country: _cell(values, cCountry),
    vertical: _cell(values, cVertical),
    article_url: _cell(values, cArticle),
    manual_image_url: _cell(values, cManualImage),
    apple: _yes(_cell(values, cApple), false),
    aspect_ratio: _aspectCell(values, cAspectRatio) || '16:9',
    open_comments: _cell(values, cOpenComments),
  };
}


function _readHookCardRow(sheet, rowNum) {
  // Hook_Card: Country / Vertical / Article / Num of Images / Text / Voiceover /
  // Music, then FIVE Manual Media columns, then Change Size / Open Comments.
  // Resolved by HEADER NAME first (mirrors _readMotionAdsRow) so inserting/
  // moving columns can't corrupt the read; HOOK_CARD_COLS is the positional
  // fallback. Each Manual Media cell is a URL (image/video) or the keyword
  // "AI" / "AI Video"; all blank -> the backend generates Num of Images images.
  // Only the article is required (see validate).
  const cols = HOOK_CARD_COLS;
  const headerMap = _buildHeaderColMap(sheet);
  const lastCol = Math.max(cols.lastInputCol, sheet.getLastColumn());
  const values = sheet.getRange(rowNum, 1, 1, lastCol).getValues()[0];

  const cCountry      = _colForHeaders(headerMap, ['Country'], cols.country);
  const cVertical     = _colForHeaders(headerMap, ['Vertical'], cols.vertical);
  const cArticle      = _colForHeaders(headerMap, ['Article'], cols.article);
  const cNumImages    = _colForHeaders(headerMap, ['Num of Images', 'Number of Images', 'Num Images'], cols.numImages);
  const cText         = _colForHeaders(headerMap, ['Text'], cols.text);
  const cVoiceover    = _colForHeaders(headerMap, ['Voiceover', 'Voice Over'], cols.voiceover);
  const cMusic        = _colForHeaders(headerMap, ['Music'], cols.music);
  const cAspectRatio  = _colForHeaders(headerMap, ['Change Size', 'Aspect Ratio'], cols.aspectRatio);
  const cOpenComments = _colForHeaders(headerMap, ['Open Comments', 'Open Comment'], cols.openComments);

  const manualMedia = [
    _colForHeaders(headerMap, ['Manual Media 1', 'Manual Image 1'], cols.manualMedia1),
    _colForHeaders(headerMap, ['Manual Media 2', 'Manual Image 2'], cols.manualMedia2),
    _colForHeaders(headerMap, ['Manual Media 3', 'Manual Image 3'], cols.manualMedia3),
    _colForHeaders(headerMap, ['Manual Media 4', 'Manual Image 4'], cols.manualMedia4),
    _colForHeaders(headerMap, ['Manual Media 5', 'Manual Image 5'], cols.manualMedia5),
  ].map(function (c) { return _cell(values, c); })
   .filter(function (u) { return !!u; });

  const numImages = Math.max(1, Math.min(5, parseInt(_cell(values, cNumImages), 10) || 1));

  return {
    row_num: rowNum,
    country: _cell(values, cCountry),
    vertical: _cell(values, cVertical),
    article_url: _cell(values, cArticle),
    num_images: numImages,
    text: _cell(values, cText),
    voice_over: _yes(_cell(values, cVoiceover), false),
    music: _cell(values, cMusic),
    manual_media: manualMedia,
    aspect_ratio: _aspectCell(values, cAspectRatio) || '9:16',
    open_comments: _cell(values, cOpenComments),
  };
}


function _readYtCartoonRow(sheet, rowNum) {
  // yt-cartoon: cartoon inputs PLUS four new knobs (Tone, Cap Position, CTA
  // Position, Vid Length). Every column is resolved by HEADER NAME first
  // (mirrors _readAvatarRow) so the operator can insert/move columns without
  // breaking the read — important because this tab is brand new and still
  // being laid out. The YT_CARTOON_COLS positional index is the fallback for a
  // fresh sheet whose headers don't match. Manual Image (D) is ignored.
  const cols = YT_CARTOON_COLS;
  const headerMap = _buildHeaderColMap(sheet);
  const lastCol = Math.max(cols.lastInputCol, sheet.getLastColumn());
  const values = sheet.getRange(rowNum, 1, 1, lastCol).getValues()[0];

  const cCountry      = _colForHeaders(headerMap, ['Country'], cols.country);
  const cVertical     = _colForHeaders(headerMap, ['Vertical'], cols.vertical);
  const cArticle      = _colForHeaders(headerMap, ['Article'], cols.article);
  const cVoiceOver    = _colForHeaders(headerMap, ['Voice Over', 'VoiceOver'], cols.voiceOver);
  const cZapcap       = _colForHeaders(headerMap, ['ZapCap'], cols.zapcap);
  const cTone         = _colForHeaders(headerMap, ['Tone'], cols.tone);
  const cCapPosition  = _colForHeaders(headerMap, ['Cap Position', 'Caption Position'], cols.capPosition);
  const cCtaPosition  = _colForHeaders(headerMap, ['CTA Position'], cols.ctaPosition);
  const cVidLength    = _colForHeaders(headerMap, ['Vid Length', 'Video Length'], cols.vidLength);
  const cAspectRatio  = _colForHeaders(headerMap, ['Change Size', 'Aspect Ratio'], cols.aspectRatio);
  const cScriptPat    = _colForHeaders(headerMap, ['Script Pattern'], cols.scriptPattern);
  const cCtaEnabled   = _colForHeaders(headerMap, ['CTA'], cols.ctaEnabled);
  const cCtaText      = _colForHeaders(headerMap, ['CTA Text'], cols.ctaText);
  const cOpenComments = _colForHeaders(headerMap, ['Open Comments'], cols.openComments);

  return {
    row_num: rowNum,
    country: _cell(values, cCountry),
    vertical: _cell(values, cVertical),
    article_url: _cell(values, cArticle),
    voice_over: _yes(_cell(values, cVoiceOver), true),
    zapcap: _yes(_cell(values, cZapcap), false),
    aspect_ratio: _aspectCell(values, cAspectRatio) || '9:16',
    script_pattern: _cell(values, cScriptPat),
    cta_enabled: _yes(_cell(values, cCtaEnabled), false),
    cta_text: _cell(values, cCtaText).slice(0, 80),
    open_comments: _cell(values, cOpenComments),
    // New knobs — sent as raw labels; the backend normalises them.
    tone: _cell(values, cTone).slice(0, 40),
    cap_position: _cell(values, cCapPosition).slice(0, 40),
    cta_position: _cell(values, cCtaPosition).slice(0, 40),
    vid_length: _cell(values, cVidLength).slice(0, 40),
  };
}


function _readAvatarRow(sheet, rowNum) {
  // video with avatar: every input column is resolved by HEADER NAME
  // first, with the AVATAR_COLS positional value as fallback for sheets
  // whose row 1 headers don't match the canonical names. Header-first
  // lookup means inserting / moving / renaming a column doesn't break
  // the read — the bug from chat 2026-06-09 (operator added Avatar Size
  // / Avatar Shape between Avatar ID and Voice Over, shifting everything
  // right by 2; voice_over read "Small", cta_enabled read "09:16", etc.)
  // is no longer possible.
  const cols = AVATAR_COLS;
  const headerMap = _buildHeaderColMap(sheet);
  const lastCol = Math.max(cols.lastInputCol, sheet.getLastColumn());
  const values = sheet.getRange(rowNum, 1, 1, lastCol).getValues()[0];

  // Resolve every input column by header. The second arg to
  // _colForHeaders is the positional fallback (the canonical AVATAR_COLS
  // index) so a brand-new sheet with no headers still reads correctly.
  const cCountry      = _colForHeaders(headerMap, ['Country'], cols.country);
  const cVertical     = _colForHeaders(headerMap, ['Vertical'], cols.vertical);
  const cArticle      = _colForHeaders(headerMap, ['Article'], cols.article);
  const cManualImage  = _colForHeaders(headerMap, ['Manual Image'], cols.manualImage);
  // "Avatar ID" and "Avatar ID (NEW)" both refer to the same column.
  // Accepting both prevents a one-time sheet rename from breaking
  // submits across sheets that label it differently.
  const cAvatarId     = _colForHeaders(
    headerMap, ['Avatar ID', 'Avatar ID (NEW)'], cols.avatarId
  );
  const cVoiceOver    = _colForHeaders(headerMap, ['Voice Over', 'VoiceOver'], cols.voiceOver);
  const cZapcap       = _colForHeaders(headerMap, ['ZapCap'], cols.zapcap);
  const cAspectRatio  = _colForHeaders(headerMap, ['Change Size', 'Aspect Ratio'], cols.aspectRatio);
  const cScriptPat    = _colForHeaders(headerMap, ['Script Pattern'], cols.scriptPattern);
  const cCtaEnabled   = _colForHeaders(headerMap, ['CTA'], cols.ctaEnabled);
  const cCtaText      = _colForHeaders(headerMap, ['CTA Text'], cols.ctaText);
  const cOpenComments = _colForHeaders(headerMap, ['Open Comments'], cols.openComments);
  // Optional knobs — no positional fallback (these columns may not
  // exist at all; 0 → empty string → backend uses today's defaults).
  const cAvatarSize   = _colForHeaders(headerMap, ['Avatar Size'], 0);
  const cAvatarShape  = _colForHeaders(headerMap, ['Avatar Shape'], 0);

  return {
    row_num: rowNum,
    country: _cell(values, cCountry),
    vertical: _cell(values, cVertical),
    article_url: _cell(values, cArticle),
    manual_image_url: _cell(values, cManualImage),
    avatar_id: _cell(values, cAvatarId).slice(0, 64),
    voice_over: _yes(_cell(values, cVoiceOver), true),
    zapcap: _yes(_cell(values, cZapcap), false),
    // Blank → backend probes manual_image_url (or kie text-to-image fallback
    // → 9:16). See _readImageVORow.
    aspect_ratio: _aspectCell(values, cAspectRatio),
    script_pattern: _cell(values, cScriptPat),
    cta_enabled: _yes(_cell(values, cCtaEnabled), false),
    cta_text: _cell(values, cCtaText).slice(0, 80),
    open_comments: _cell(values, cOpenComments),
    avatar_size: cAvatarSize ? _cell(values, cAvatarSize).toLowerCase() : '',
    avatar_shape: cAvatarShape ? _cell(values, cAvatarShape).toLowerCase() : '',
  };
}


function _readTextOnImgRow(sheet, rowNum) {
  // paste text on img: Image-VO columns A-D, plus Text (E), then F-J shifted
  // right by 1. The Text column is the overlay text — bounded at 240 chars
  // (matches the server-side coercion in _build_text_on_img_row).
  const cols = TEXT_ON_IMG_COLS;
  const values = sheet.getRange(rowNum, 1, 1, cols.lastInputCol).getValues()[0];
  return {
    row_num: rowNum,
    country: _cell(values, cols.country),
    vertical: _cell(values, cols.vertical),
    article_url: _cell(values, cols.article),
    manual_image_url: _cell(values, cols.manualImage),
    text: _cell(values, cols.text).slice(0, 240),
    voice_over: _yes(_cell(values, cols.voiceOver), true),
    zapcap: _yes(_cell(values, cols.zapcap), false),
    // Blank → backend probes manual_image_url for native dims. See _readImageVORow.
    aspect_ratio: _aspectCell(values, cols.aspectRatio),
    script_pattern: _cell(values, cols.scriptPattern),
    open_comments: _cell(values, cols.openComments),
  };
}


function _readSimpleX4Row(sheet, rowNum) {
  // Post-migration simple_x4 layout: same A-H as image_vo + 4 (template, cta)
  // pairs + open_comments at col Q. Backend rejects Template* values that
  // aren't "" / "1" / "2" / "3", but we ALSO validate here for fast UX feedback
  // (Apps Script dialog) rather than waiting for a 400 from the server.
  const cols = SIMPLE_X4_COLS;
  const values = sheet.getRange(rowNum, 1, 1, cols.lastInputCol).getValues()[0];
  const cards = [];
  const tplIdxs = [cols.template1, cols.template2, cols.template3, cols.template4];
  const ctaIdxs = [cols.cta1, cols.cta2, cols.cta3, cols.cta4];
  for (var i = 0; i < 4; i++) {
    cards.push({
      template_id: _cell(values, tplIdxs[i]),
      cta: _cell(values, ctaIdxs[i]),
    });
  }
  return {
    row_num: rowNum,
    country: _cell(values, cols.country),
    vertical: _cell(values, cols.vertical),
    article_url: _cell(values, cols.article),
    manual_image_url: _cell(values, cols.manualImage),
    voice_over: _yes(_cell(values, cols.voiceOver), true),
    zapcap: _yes(_cell(values, cols.zapcap), false),
    // Blank → backend probes manual_image_url for native dims. See _readImageVORow.
    aspect_ratio: _aspectCell(values, cols.aspectRatio),
    script_pattern: _cell(values, cols.scriptPattern),
    cards: cards,
    open_comments: _cell(values, cols.openComments),
  };
}


function _validateImageVO(r) {
  if (!r.article_url) return 'article URL missing';
  if (!r.manual_image_url) return 'manual image URL missing';
  return null;
}


function _validateCartoon(r) {
  if (!r.article_url) return 'article URL missing';
  return null;
}


function _validateSimpleMotion(r) {
  // Only the article is required (it drives the voiceover + any generated
  // scenes). Both Manual Image columns are optional — a blank cell is
  // auto-generated, a filled cell is animated as-is.
  if (!r.article_url) return 'article URL missing';
  return null;
}


function _validateFastFurious(r) {
  // Only the article is required (it drives the voiceovers + any generated
  // scenes). Manual Image cells are optional (shared across videos); Number of
  // Videos + Change Size default sensibly.
  if (!r.article_url) return 'article URL missing';
  return null;
}


function _validateMotionAds(r) {
  // Only the article is required. Manual Image is optional (blank → the backend
  // generates a realistic image; filled → it animates that image as-is). Apple /
  // Change Size / Open Comments are all optional with sensible defaults.
  if (!r.article_url) return 'article URL missing';
  return null;
}


function _validateHookCard(r) {
  // The article is required whenever the backend must GENERATE something: a
  // blank Text needs it for the hook, no Manual Media needs it for the scenes,
  // and Voiceover needs it for the script. A row that supplies the Text, >=1
  // Manual Media cell, and no Voiceover needs no article.
  if (!r.article_url) {
    if (r.voice_over) return 'article URL missing (needed for the voiceover script)';
    if (!r.text) return 'article URL missing (needed to write the hook)';
    if (!r.manual_media.length) return 'article URL missing (needed to generate media)';
  }
  return null;
}


function _validateYtCartoon(r) {
  // Same as cartoon — only the article is required. The four knob columns are
  // optional (blank = defaults) and coerced server-side.
  if (!r.article_url) return 'article URL missing';
  return null;
}


function _validateFourImages(r) {
  if (!r.article_url) return 'article URL missing';
  if (r.how_many < 1 || r.how_many > 4) return 'how_many must be 1..4';
  if (r.image_urls.length !== r.how_many) return 'need ' + r.how_many + ' image URLs';
  return null;
}


function _validateAvatar(r) {
  if (!r.article_url) return 'article URL missing';
  if (!r.avatar_id) return 'avatar ID missing — pick one from /admin/avatars';
  return null;
}


function _validateTextOnImg(r) {
  // Article URL is no longer needed — the tab produces a still image, not a
  // video, so there's no script/VO pipeline that would consume the article.
  // Only the manual image is required. (2026-06-09: video pipeline stripped.)
  if (!r.manual_image_url) return 'manual image URL missing';
  // Empty Text is allowed (the renderer ships the image as-is). Length is
  // bounded by _readTextOnImgRow's .slice(0, 240) above — no upper-bound
  // check needed here.
  return null;
}


function _validateSimpleX4(r) {
  if (!r.article_url) return 'article URL missing';
  if (!r.manual_image_url) return 'manual image URL missing';
  // Card values: backend coerces invalid template ids to "" silently, but it's
  // friendlier to flag obvious typos at submit time so the operator doesn't
  // ship 4 videos and wonder why no overlay appeared.
  for (var i = 0; i < r.cards.length; i++) {
    var t = r.cards[i].template_id;
    if (t && t !== '1' && t !== '2' && t !== '3') {
      return 'Template ' + (i + 1) + ' must be empty, 1, 2, or 3 (got "' + t + '")';
    }
  }
  return null;
}


// ─── Selection helpers ──────────────────────────────────────────────────────

function _firstDataRowForTab(tabType) {
  // simple_x4 has TWO header rows (preview + column names); everyone else has one.
  return tabType === TAB_SIMPLE_X4 ? SIMPLE_X4_FIRST_DATA_ROW : 2;
}


function _selectedDataRowNumbers(sheet, tabType) {
  const firstData = _firstDataRowForTab(tabType);
  const ranges = sheet.getActiveRangeList().getRanges();
  const rowNums = {};
  ranges.forEach(function (range) {
    const start = range.getRow();
    const end = start + range.getNumRows() - 1;
    for (var r = Math.max(start, firstData); r <= end; r++) {
      rowNums[r] = true;
    }
  });
  return Object.keys(rowNums).map(Number).sort(function (a, b) { return a - b; });
}


function _unprocessedRowNumbers(sheet, readyVideoCol1, tabType) {
  const firstData = _firstDataRowForTab(tabType);
  const lastRow = sheet.getLastRow();
  if (lastRow < firstData) return [];
  const numRows = lastRow - firstData + 1;
  // Ready-video-1 column plus input columns A-D (Country / Vertical /
  // Article / Manual Image or How Many on every layout), batch-read once.
  // A row only counts as unprocessed WORK when A-D hold something real —
  // sheets are pre-formatted hundreds of rows deep with default dropdowns
  // (Voice Over "No", Change Size "4:3"), and counting those produced a
  // scary "Submit 500 unprocessed rows?" over ~zero actual rows
  // (chat 2026-06-10).
  const ready = sheet.getRange(firstData, readyVideoCol1, numRows, 1).getValues();
  const inputs = sheet.getRange(firstData, 1, numRows, 4).getValues();
  const rowNums = [];
  for (var i = 0; i < ready.length; i++) {
    if (String(ready[i][0] || '').trim()) continue;    // already has output
    const hasInput = inputs[i].some(function (v) {
      return String(v == null ? '' : v).trim() !== '';
    });
    if (hasInput) rowNums.push(i + firstData);    // sheet row
  }
  return rowNums;
}


// ─── Menu actions ───────────────────────────────────────────────────────────

function generateSelected() {
  const sheet = SpreadsheetApp.getActiveSheet();
  const tabType = _detectTabType(sheet);
  if (!tabType) {
    SpreadsheetApp.getUi().alert(
      'Could not detect tab type. Row 1 must contain "Manual Image" or "How Many".'
    );
    return;
  }
  const rowNums = _selectedDataRowNumbers(sheet, tabType);
  if (rowNums.length === 0) {
    SpreadsheetApp.getUi().alert('No data rows selected. Click on a data row first.');
    return;
  }
  _submitJobForRowNums(sheet, tabType, rowNums, true);    // confirm before overwriting
}


function generateAllUnprocessed() {
  const sheet = SpreadsheetApp.getActiveSheet();
  const tabType = _detectTabType(sheet);
  if (!tabType) {
    SpreadsheetApp.getUi().alert(
      'Could not detect tab type. Row 1 must contain "Manual Image" or "How Many".'
    );
    return;
  }
  const cols = (
    tabType === TAB_FOUR_IMAGES ? FOUR_IMAGES_COLS
    : tabType === TAB_SIMPLE_X4 ? SIMPLE_X4_COLS
    : tabType === TAB_SIMPLE_MOTION ? SIMPLE_MOTION_COLS
    : tabType === TAB_FAST_FURIOUS ? FAST_FURIOUS_COLS
    : tabType === TAB_CARTOON ? CARTOON_COLS
    : tabType === TAB_YT_CARTOON ? YT_CARTOON_COLS
    : tabType === TAB_TEXT_ON_IMG ? TEXT_ON_IMG_COLS
    : tabType === TAB_AVATAR ? AVATAR_COLS
    : tabType === TAB_MOTION_ADS ? MOTION_ADS_COLS
    : tabType === TAB_HOOK_CARD ? HOOK_CARD_COLS
    : IMAGE_VO_COLS
  );
  const rowNums = _unprocessedRowNumbers(sheet, cols.readyVideo1, tabType);
  if (rowNums.length === 0) {
    SpreadsheetApp.getUi().alert('No unprocessed rows found.');
    return;
  }
  const ui = SpreadsheetApp.getUi();
  const ok = ui.alert(
    'Submit ' + rowNums.length + ' unprocessed rows?',
    ui.ButtonSet.OK_CANCEL
  );
  if (ok !== ui.Button.OK) return;
  // Already filtered to rows WITHOUT a video, so no overwrite confirm needed.
  _submitJobForRowNums(sheet, tabType, rowNums, false);
}


function _submitJobForRowNums(sheet, tabType, rowNums, checkExisting) {
  // Simple + Image-VO share the same input columns/readers; the other tabs
  // each have their own reader (different column maps).
  const readRow = tabType === TAB_FOUR_IMAGES ? _readFourImagesRow
    : tabType === TAB_CARTOON ? _readCartoonRow
    : tabType === TAB_SIMPLE_MOTION ? _readSimpleMotionRow
    : tabType === TAB_FAST_FURIOUS ? _readFastFuriousRow
    : tabType === TAB_YT_CARTOON ? _readYtCartoonRow
    : tabType === TAB_SIMPLE_X4 ? _readSimpleX4Row
    : tabType === TAB_TEXT_ON_IMG ? _readTextOnImgRow
    : tabType === TAB_AVATAR ? _readAvatarRow
    : tabType === TAB_MOTION_ADS ? _readMotionAdsRow
    : tabType === TAB_HOOK_CARD ? _readHookCardRow
    : _readImageVORow;
  const validate = tabType === TAB_FOUR_IMAGES ? _validateFourImages
    : tabType === TAB_CARTOON ? _validateCartoon
    : tabType === TAB_SIMPLE_MOTION ? _validateSimpleMotion
    : tabType === TAB_FAST_FURIOUS ? _validateFastFurious
    : tabType === TAB_YT_CARTOON ? _validateYtCartoon
    : tabType === TAB_SIMPLE_X4 ? _validateSimpleX4
    : tabType === TAB_TEXT_ON_IMG ? _validateTextOnImg
    : tabType === TAB_AVATAR ? _validateAvatar
    : tabType === TAB_MOTION_ADS ? _validateMotionAds
    : tabType === TAB_HOOK_CARD ? _validateHookCard
    : _validateImageVO;

  let rows = [];
  const skipped = [];
  rowNums.forEach(function (rn) {
    const row = readRow(sheet, rn);
    const err = validate(row);
    if (err) {
      skipped.push('Row ' + rn + ': ' + err);
    } else {
      rows.push(row);
    }
  });

  if (rows.length === 0) {
    SpreadsheetApp.getUi().alert(
      'No valid rows. Issues:\n' + skipped.join('\n')
    );
    return;
  }

  // Guard against accidentally regenerating rows that already have a video.
  if (checkExisting) {
    const videoCol = (
      tabType === TAB_FOUR_IMAGES ? FOUR_IMAGES_COLS
      : tabType === TAB_SIMPLE_X4 ? SIMPLE_X4_COLS
      : tabType === TAB_SIMPLE_MOTION ? SIMPLE_MOTION_COLS
      : tabType === TAB_FAST_FURIOUS ? FAST_FURIOUS_COLS
      : tabType === TAB_CARTOON ? CARTOON_COLS
      : tabType === TAB_YT_CARTOON ? YT_CARTOON_COLS
      : tabType === TAB_TEXT_ON_IMG ? TEXT_ON_IMG_COLS
      : tabType === TAB_AVATAR ? AVATAR_COLS
      : tabType === TAB_MOTION_ADS ? MOTION_ADS_COLS
      : tabType === TAB_HOOK_CARD ? HOOK_CARD_COLS
      : IMAGE_VO_COLS
    ).readyVideo1;
    const withVideo = rows.filter(function (r) {
      return String(sheet.getRange(r.row_num, videoCol).getValue() || '').trim() !== '';
    }).map(function (r) { return r.row_num; });
    if (withVideo.length > 0) {
      const ans = SpreadsheetApp.getUi().alert(
        'Some rows already have a video',
        withVideo.length + ' of the selected rows already have a video (rows ' +
        withVideo.join(', ') + ').\n\nRegenerate and OVERWRITE them?\n' +
        'Yes = regenerate all · No = skip those, do the rest.',
        SpreadsheetApp.getUi().ButtonSet.YES_NO
      );
      if (ans !== SpreadsheetApp.getUi().Button.YES) {
        rows = rows.filter(function (r) { return withVideo.indexOf(r.row_num) === -1; });
        if (rows.length === 0) {
          SpreadsheetApp.getUi().alert('Nothing to do — all selected rows already have a video.');
          return;
        }
      }
    }
  }

  const idToken = ScriptApp.getIdentityToken();
  if (!idToken) {
    SpreadsheetApp.getUi().alert(
      'Could not get Google OAuth ID token. Re-authorize the script and try again.'
    );
    return;
  }

  const payload = {
    sheet_id: SpreadsheetApp.getActive().getId(),
    worksheet: sheet.getName(),
    tab_type: tabType,
  };
  if (tabType === TAB_FOUR_IMAGES) payload.rows_four_images = rows;
  else if (tabType === TAB_SIMPLE) payload.rows_simple = rows;
  else if (tabType === TAB_SIMPLE_MOTION) payload.rows_simple_motion = rows;
  else if (tabType === TAB_FAST_FURIOUS) payload.rows_fast_furious = rows;
  else if (tabType === TAB_CARTOON) payload.rows_cartoon = rows;
  else if (tabType === TAB_YT_CARTOON) payload.rows_yt_cartoon = rows;
  else if (tabType === TAB_SIMPLE_X4) payload.rows_simple_x4 = rows;
  else if (tabType === TAB_TEXT_ON_IMG) payload.rows_text_on_img = rows;
  else if (tabType === TAB_AVATAR) payload.rows_avatar = rows;
  else if (tabType === TAB_MOTION_ADS) payload.rows_motion_ads = rows;
  else if (tabType === TAB_HOOK_CARD) payload.rows_hook_card = rows;
  else payload.rows_image_vo = rows;

  const body = _submitJobWithRetry_(payload);
  if (body === null) {
    // User cancelled the "Backend is busy" dialog. The pending idempotency
    // key remains stashed in DocumentProperties so the next click resumes
    // the same submit (no duplicate).
    return;
  }

  PropertiesService.getDocumentProperties().setProperty('LAST_JOB_ID', body.job_id);

  // Dropped count = rows the server suppressed as duplicates of an in-flight
  // job. ``body.dropped_count`` is 0 on older backends — the ``|| 0`` keeps
  // the alert clean either way.
  var droppedCount = body.dropped_count || 0;
  var keptCount = body.row_count || 0;
  var message;
  if (keptCount === 0 && droppedCount > 0) {
    // Whole batch was suppressed. Surface this loudly — previously this
    // looked identical to "job completed with no output" in the sidebar.
    message =
      'No rows were queued.\n\n' +
      'The server skipped all ' + droppedCount + ' rows because they are ' +
      'already running in another active job for this sheet.\n\n' +
      'Wait for the existing job to finish (or use "Stop all jobs" from the ' +
      'sidebar) and resubmit.';
  } else {
    message = 'Job submitted: ' + body.job_id + '\n' +
              'Rows queued: ' + keptCount;
    if (droppedCount > 0) {
      message += '\nSkipped (already in another active job): ' + droppedCount;
    }
  }
  if (skipped.length > 0) {
    message += '\n\nSkipped (incomplete):\n' + skipped.join('\n');
  }
  SpreadsheetApp.getUi().alert(message);
  showJobsSidebar();
}


// ─── Simple x4 migration ────────────────────────────────────────────────────
//
// One-shot setup for the simple x4 tab. Idempotent — safe to run multiple
// times. Each step is gated by a "does this already look done?" probe, so
// re-running the menu item never corrupts a partially-migrated sheet.
//
// What it ensures:
//   1. A frozen row 1 holding the "Template Preview" label + two =IMAGE()
//      formulas pointing at the Template 1 and Template 2 preview PNGs.
//   2. A frozen row 2 holding the column-name headers (Country, …,
//      Template 1, CTA 1, …, Open Comments, Ready Video 1-4).
//   3. 8 new columns inserted between H (Script Pattern) and the old col I
//      (Open Comments) — but only if they aren't already there.
//   4. Data Validation (dropdown blank / 1 / 2) on the Template* columns.
//
// Plan _plans/2026-06-08-simple-x4-template-cards.md §Migration.

function migrateSimpleX4Columns() {
  const ui = SpreadsheetApp.getUi();
  const sheet = SpreadsheetApp.getActiveSheet();
  const name = String(sheet.getName() || '').toLowerCase().trim();

  if (name.indexOf('x4') === -1) {
    ui.alert(
      'Wrong tab',
      'Switch to the "simple x4" tab first — the active tab is "' +
        sheet.getName() + '".',
      ui.ButtonSet.OK
    );
    return;
  }

  const ans = ui.alert(
    'Migrate simple x4 columns?',
    'This will:\n' +
    '  1. Insert a frozen row 1 with the template-preview images\n' +
    '  2. Insert 8 new columns (Template 1, CTA 1, …, CTA 4) between\n' +
    '     "Script Pattern" and the existing "Open Comments"\n' +
    '  3. Freeze rows 1 + 2 so they stay visible while you scroll\n' +
    '  4. Add a dropdown (blank / 1 / 2) to each Template column\n\n' +
    'Re-running this is SAFE — already-migrated tabs are left alone.\n\n' +
    'Continue?',
    ui.ButtonSet.OK_CANCEL
  );
  if (ans !== ui.Button.OK) return;

  const steps = [];

  // ─── Step 1: ensure row 1 (preview header) exists ───
  // Probe: cell A1 reads "Template Preview"? If not, treat the tab as needing
  // row 1 + 2 inserted (i.e. the headers currently sit at row 1, not row 2).
  const a1 = String(sheet.getRange(1, 1).getValue() || '').toLowerCase().trim();
  const a2 = String(sheet.getRange(2, 1).getValue() || '').toLowerCase().trim();
  const needRowInsert = a1 !== 'template preview';
  if (needRowInsert) {
    sheet.insertRowBefore(1);
    sheet.getRange(1, 1).setValue('Template Preview').setFontWeight('bold');
    steps.push('inserted row 1 (preview header)');
  } else if (a2 === 'country') {
    steps.push('row 1 already present (skipped)');
  } else {
    // Defensive: row 1 says "Template Preview" but row 2 doesn't look like
    // column headers. Bail rather than push garbage around.
    ui.alert(
      'Unexpected layout',
      'Row 1 reads "Template Preview" but row 2 does NOT look like the column ' +
      'header row (got "' + a2 + '"). Aborting to avoid corrupting data — ' +
      'please inspect the sheet manually.',
      ui.ButtonSet.OK
    );
    return;
  }

  // ─── Step 2: ensure the 8 new columns exist between H and old I ───
  // Probe: row 2 col I reads "Template 1"? If yes, already inserted.
  const headerI = String(
    sheet.getRange(SIMPLE_X4_HEADER_ROW, SIMPLE_X4_COLS.template1).getValue() || ''
  ).toLowerCase().trim();
  const needColInsert = headerI.indexOf('template 1') !== 0;
  if (needColInsert) {
    // Insert 8 columns AFTER col 8 (H = Script Pattern). The existing
    // Open Comments + Ready Video columns shift right by 8 automatically.
    sheet.insertColumnsAfter(8, 8);
    steps.push('inserted 8 new columns (I-P)');
  } else {
    steps.push('8 columns already inserted (skipped)');
  }

  // ─── Step 3: write the row 2 column headers idempotently ───
  // Always overwrite — operator might have typo'd "Tempate 1" by hand.
  const headers = [
    'Country', 'Vertical', 'Article', 'Manual Image',
    'Voice Over', 'ZapCap', 'Change Size', 'Script Pattern',
    'Template 1', 'CTA 1', 'Template 2', 'CTA 2',
    'Template 3', 'CTA 3', 'Template 4', 'CTA 4',
    'Open Comments',
    'Ready Video 1', 'Ready Video 2', 'Ready Video 3', 'Ready Video 4',
  ];
  sheet.getRange(SIMPLE_X4_HEADER_ROW, 1, 1, headers.length)
    .setValues([headers])
    .setFontWeight('bold');
  steps.push('wrote row 2 column headers');

  // ─── Step 4: row 1 preview =IMAGE() formulas ───
  // Each preview PNG has "DEFAULT" / "TEMPLATE 1" / "TEMPLATE 2" /
  // "TEMPLATE 3" baked into the top of the image (see
  // tools/render_labeled_template_previews.py), so a single cell per
  // template is enough — the label IS the image. Placed at B1/C1/D1/E1 so
  // they sit at the leftmost area of the sheet and stay visible regardless
  // of horizontal scroll position. The DEFAULT cell (B1) shows what ships
  // when the operator leaves the Template column blank — a bare kie photo
  // with no overlay.
  //
  // We always rewrite these cells so re-running the migration cleans up any
  // earlier layout drift (e.g. stray "1"/"2"/"3" text typed by the operator).
  sheet.getRange(SIMPLE_X4_PREVIEW_ROW, 1)
    .setValue('Template Preview')
    .setFontWeight('bold');
  sheet.getRange(SIMPLE_X4_PREVIEW_ROW, 2)
    .setFormula('=IMAGE("' + CARD_TEMPLATE_PREVIEW_URLS['default'] + '")');
  sheet.getRange(SIMPLE_X4_PREVIEW_ROW, 3)
    .setFormula('=IMAGE("' + CARD_TEMPLATE_PREVIEW_URLS['1'] + '")');
  sheet.getRange(SIMPLE_X4_PREVIEW_ROW, 4)
    .setFormula('=IMAGE("' + CARD_TEMPLATE_PREVIEW_URLS['2'] + '")');
  sheet.getRange(SIMPLE_X4_PREVIEW_ROW, 5)
    .setFormula('=IMAGE("' + CARD_TEMPLATE_PREVIEW_URLS['3'] + '")');

  // Bump row 1 height so the previews render at a useful size (the labeled
  // PNGs are 1080×1240 → ~14% aspect overhead from the label band).
  sheet.setRowHeight(SIMPLE_X4_PREVIEW_ROW, 200);
  steps.push('wrote row-1 preview =IMAGE() formulas');

  // ─── Step 5: freeze rows 1 + 2 ───
  if (sheet.getFrozenRows() < 2) {
    sheet.setFrozenRows(2);
    steps.push('froze rows 1 + 2');
  } else {
    steps.push('rows already frozen (skipped)');
  }

  // ─── Step 6: data validation dropdowns on Template* columns ───
  const validation = SpreadsheetApp.newDataValidation()
    .requireValueInList(['1', '2', '3'], true)
    .setAllowInvalid(false)
    .setHelpText('Empty for no card, or 1 / 2 / 3 to pick a template.')
    .build();
  const maxRow = Math.max(sheet.getMaxRows(), 1000);
  const tplCols = [
    SIMPLE_X4_COLS.template1,
    SIMPLE_X4_COLS.template2,
    SIMPLE_X4_COLS.template3,
    SIMPLE_X4_COLS.template4,
  ];
  tplCols.forEach(function (col) {
    sheet
      .getRange(SIMPLE_X4_FIRST_DATA_ROW, col, maxRow - SIMPLE_X4_FIRST_DATA_ROW + 1, 1)
      .setDataValidation(validation);
  });
  steps.push('added dropdown validation to Template 1-4 columns');

  ui.alert(
    'Migration done',
    'Steps applied to "' + sheet.getName() + '":\n\n• ' + steps.join('\n• ') +
    '\n\nThe simple x4 tab is now ready. Submit a row to test.',
    ui.ButtonSet.OK
  );
}


// ─── Size dropdowns ─────────────────────────────────────────────────────────

/** One-shot: (re)apply the Change Size dropdown on EVERY tab that has a
 *  "Change Size" (or "Aspect Ratio") column. Idempotent — re-running just
 *  rewrites the same validation, so it doubles as the upgrade path whenever
 *  the option lists grow (4:3 added per chat 2026-06-10).
 *
 *  Tab-aware: the animated tabs (SEEDANCE_VIDEO_TABS) render through Seedance,
 *  which supports a stricter size set, so they get SEEDANCE_SIZE_OPTIONS plus a
 *  header note naming the active model; every other tab keeps the full
 *  SIZE_DROPDOWN_OPTIONS. Non-strict on both: the backend accepts typed WxH
 *  pixels and snaps any unsupported ratio, so a strict rule would wipe a valid
 *  free-typed cell. */
function applySizeDropdowns() {
  const ui = SpreadsheetApp.getUi();

  const imageValidation = SpreadsheetApp.newDataValidation()
    .requireValueInList(SIZE_DROPDOWN_OPTIONS, true)
    .setAllowInvalid(true)
    .setHelpText('Pick a ratio, or type WxH pixels (e.g. 1080x1350).')
    .build();
  const seedanceValidation = SpreadsheetApp.newDataValidation()
    .requireValueInList(SEEDANCE_SIZE_OPTIONS, true)
    .setAllowInvalid(true)
    .setHelpText(
      ACTIVE_VIDEO_MODEL_LABEL + ' supports ' + SEEDANCE_SIZE_OPTIONS.join(', ')
      + '. Any other ratio auto-snaps to the closest one.'
    )
    .build();
  const seedanceNote =
    '🎬 Video model: ' + ACTIVE_VIDEO_MODEL_LABEL + '\n' +
    'Supported sizes: ' + SEEDANCE_SIZE_OPTIONS.join(' · ') + '\n\n' +
    'Pick one of these, or type WxH pixels (e.g. 1080x1920). Any other ratio\n' +
    '(2:3, 3:2, 4:5…) auto-snaps to the closest supported size, so the video\n' +
    'still renders.';

  const updatedVideo = [];
  const updatedImage = [];
  SpreadsheetApp.getActive().getSheets().forEach(function (sheet) {
    // Headers sit on row 1 everywhere except migrated simple_x4 tabs,
    // where row 1 is the template-preview band and row 2 holds them.
    var headerRow = 0;
    var col = 0;
    [1, 2].some(function (probe) {
      if (sheet.getLastRow() < probe || sheet.getLastColumn() === 0) return false;
      const headers = sheet.getRange(probe, 1, 1, sheet.getLastColumn()).getValues()[0];
      for (var i = 0; i < headers.length; i++) {
        const h = String(headers[i] || '').toLowerCase().trim();
        if (h === 'change size' || h === 'aspect ratio') {
          headerRow = probe;
          col = i + 1;
          return true;
        }
      }
      return false;
    });
    if (!col) return;    // tab has no size column — skip

    const firstData = headerRow + 1;
    const maxRow = sheet.getMaxRows();
    if (maxRow < firstData) return;

    const isSeedanceTab = SEEDANCE_VIDEO_TABS.indexOf(_detectTabType(sheet)) !== -1;
    sheet.getRange(firstData, col, maxRow - firstData + 1, 1)
      .setDataValidation(isSeedanceTab ? seedanceValidation : imageValidation);
    if (isSeedanceTab) {
      // Name the active model right where the size is picked. Set only on video
      // tabs — never clobber an operator's own note on the image tabs.
      sheet.getRange(headerRow, col).setNote(seedanceNote);
      updatedVideo.push(sheet.getName());
    } else {
      updatedImage.push(sheet.getName());
    }
  });

  const parts = [];
  if (updatedVideo.length) {
    parts.push(
      ACTIVE_VIDEO_MODEL_LABEL + ' sizes (' + SEEDANCE_SIZE_OPTIONS.join(', ')
      + ') + model note applied to:\n• ' + updatedVideo.join('\n• ')
    );
  }
  if (updatedImage.length) {
    parts.push(
      'Image-tab sizes (' + SIZE_DROPDOWN_OPTIONS.join(', ') + ') applied to:\n• '
      + updatedImage.join('\n• ')
    );
  }
  ui.alert(
    'Size dropdowns updated',
    parts.length ? parts.join('\n\n') : 'No tabs with a "Change Size" column found.',
    ui.ButtonSet.OK
  );
}


/** Modal: a clean, at-a-glance reference of which model each tab runs and the
 *  sizes it supports. Reads the same option constants the dropdowns use, so it
 *  can never drift from what the sheet actually enforces. Pure info — no writes. */
function showActiveModels() {
  function chips(sizes) {
    return sizes.map(function (s) {
      return '<span class="chip">' + s + '</span>';
    }).join('');
  }

  const html =
    '<!DOCTYPE html><html><head><base target="_top"><meta charset="utf-8"><style>' +
    '*{box-sizing:border-box}' +
    'body{margin:0;font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;' +
    'color:#1b2733;background:#fff;font-size:13px;line-height:1.5}' +
    '.wrap{padding:18px 20px 20px}' +
    'h1{margin:0 0 2px;font-size:16px;font-weight:600}' +
    '.sub{margin:0 0 16px;color:#6b7684;font-size:12px}' +
    '.card{border:1px solid #e3e8ee;border-radius:10px;padding:13px 15px;margin-bottom:11px}' +
    '.card.video{border-left:3px solid #2f6bff}' +
    '.card.image{border-left:3px solid #12b886}' +
    '.top{display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin-bottom:9px}' +
    '.tabs{font-weight:600}' +
    '.model{color:#6b7684;font-size:12px;white-space:nowrap}' +
    '.model b{color:#1b2733}' +
    '.chips{display:flex;flex-wrap:wrap;gap:6px}' +
    '.chip{display:inline-block;padding:2px 8px;border-radius:6px;background:#f1f4f8;' +
    'font-variant-numeric:tabular-nums;font-size:12px}' +
    '.foot{margin:14px 0 0;color:#6b7684;font-size:12px}' +
    '.foot b{color:#1b2733}' +
    '</style></head><body><div class="wrap">' +
    '<h1>Active models &amp; sizes</h1>' +
    '<p class="sub">Which model each tab runs, and the sizes it accepts.</p>' +
    '<div class="card video"><div class="top">' +
    '<span class="tabs">cartoon · yt-cartoon · simple-motion · fast-and-furious · Motion_Ads</span>' +
    '<span class="model">animated video · <b>' + ACTIVE_VIDEO_MODEL_LABEL + '</b></span>' +
    '</div><div class="chips">' + chips(SEEDANCE_SIZE_OPTIONS) + '</div></div>' +
    '<div class="card image"><div class="top">' +
    '<span class="tabs">image tabs</span>' +
    '<span class="model">still image · <b>wider size set</b></span>' +
    '</div><div class="chips">' + chips(SIZE_DROPDOWN_OPTIONS) + '<span class="chip">WxH px</span></div></div>' +
    '<p class="foot">On the video tabs, any other ratio (2:3, 3:2, 4:5…) <b>auto-snaps</b> ' +
    'to the closest supported size, so the video still renders.</p>' +
    '</div></body></html>';

  const out = HtmlService.createHtmlOutput(html).setWidth(430).setHeight(350);
  SpreadsheetApp.getUi().showModalDialog(out, 'Active models & sizes');
}


// ─── yt-cartoon dropdowns ───────────────────────────────────────────────────

/** One-shot: (re)apply the Tone / Cap Position / CTA Position / Vid Length
 *  dropdowns on every yt-cartoon tab. Resolves each column BY HEADER NAME, so
 *  it follows the column wherever the operator put it. Idempotent and
 *  non-strict (blank + free-typed values are allowed — the backend coerces
 *  them), matching applySizeDropdowns. The Change Size column on this tab is
 *  handled by applySizeDropdowns like every other tab. */
function applyYtCartoonDropdowns() {
  const ui = SpreadsheetApp.getUi();
  const specs = [
    { headers: ['Tone'], options: YT_CARTOON_TONE_OPTIONS,
      help: 'Engaging (lively, clickable) or Calm (current style). Blank = Engaging.' },
    { headers: ['Cap Position', 'Caption Position'], options: YT_CARTOON_POSITION_OPTIONS,
      help: 'Nudge the caption height vs default. Blank = Default.' },
    { headers: ['CTA Position'], options: YT_CARTOON_POSITION_OPTIONS,
      help: 'Nudge the CTA button height vs default. Blank = Default.' },
    { headers: ['Vid Length', 'Video Length'], options: YT_CARTOON_VID_LENGTH_OPTIONS,
      help: 'Cap the video length. Blank = up to 10s. 10s makes 2 videos; 15s/20s make 1.' },
  ];

  const updated = [];
  SpreadsheetApp.getActive().getSheets().forEach(function (sheet) {
    if (_detectTabType(sheet) !== TAB_YT_CARTOON) return;
    const headerMap = _buildHeaderColMap(sheet);
    const firstData = 2;
    const maxRow = sheet.getMaxRows();
    if (maxRow < firstData) return;
    var applied = 0;
    specs.forEach(function (spec) {
      const col = _colForHeaders(headerMap, spec.headers, 0);
      if (!col) return;    // column not present under any known name — skip
      const validation = SpreadsheetApp.newDataValidation()
        .requireValueInList(spec.options, true)
        .setAllowInvalid(true)    // blank + free-typed values are fine
        .setHelpText(spec.help)
        .build();
      sheet.getRange(firstData, col, maxRow - firstData + 1, 1)
        .setDataValidation(validation);
      applied++;
    });
    if (applied) updated.push(sheet.getName() + ' (' + applied + ' dropdowns)');
  });

  ui.alert(
    'yt-cartoon dropdowns updated',
    updated.length
      ? 'Applied to:\n\n• ' + updated.join('\n• ')
        + '\n\nAlso run "Update size dropdowns on all tabs" for the Change Size column.'
      : 'No yt-cartoon tab found. Name a tab "yt-cartoon" and re-run.',
    ui.ButtonSet.OK
  );
}


// ─── Hook_Card music dropdown ───────────────────────────────────────────────

/** The pickable Music values: each style name x its 2 variations, e.g.
 *  "Uplifting 2". Kept in sync with the generated tracks (<name>_1.mp3 /
 *  _2.mp3). A bare style name (random variation) and a blank cell (random
 *  track) also work — both handled server-side. */
function _hookCardMusicChoices() {
  const out = ['None'];    // "None" = silent video
  HOOK_CARD_MUSIC_NAMES.forEach(function (name) {
    out.push(name + ' 1');
    out.push(name + ' 2');
  });
  return out;
}


/** One-shot: (re)apply the Music, Voiceover and Manual Media dropdowns on every
 *  Hook_Card tab. Every column is resolved BY HEADER NAME (positional fallback)
 *  so it follows the header wherever it sits. All are non-strict (allowInvalid)
 *  so a pasted URL or blank is accepted. Keep HOOK_CARD_MUSIC_NAMES in sync with
 *  tools/generate_hook_card_music.py. */
function applyHookCardMusicDropdown() {
  const ui = SpreadsheetApp.getUi();
  const updated = [];
  SpreadsheetApp.getActive().getSheets().forEach(function (sheet) {
    if (_detectTabType(sheet) !== TAB_HOOK_CARD) return;
    const headerMap = _buildHeaderColMap(sheet);
    const firstData = 2;
    const maxRow = sheet.getMaxRows();
    if (maxRow < firstData) return;
    const nRows = maxRow - firstData + 1;

    function applyList(headers, positional, options, help) {
      const col = _colForHeaders(headerMap, headers, positional);
      if (!col) return;
      const v = SpreadsheetApp.newDataValidation()
        .requireValueInList(options, true)
        .setAllowInvalid(true)
        .setHelpText(help)
        .build();
      sheet.getRange(firstData, col, nRows, 1).setDataValidation(v);
    }

    applyList(['Music'], HOOK_CARD_COLS.music, _hookCardMusicChoices(),
      'Pick a track (e.g. "Uplifting 2"), "None" for silent, a style name for a '
      + 'random variation, or blank for a random track.');
    applyList(['Voiceover', 'Voice Over'], HOOK_CARD_COLS.voiceover, ['Yes', 'No'],
      'Yes narrates the article (the length follows the narration and music is '
      + 'ducked under it). Blank = No.');
    const mediaCols = ['manualMedia1', 'manualMedia2', 'manualMedia3',
                       'manualMedia4', 'manualMedia5'];
    for (var i = 0; i < 5; i++) {
      applyList(['Manual Media ' + (i + 1), 'Manual Image ' + (i + 1)],
        HOOK_CARD_COLS[mediaCols[i]], ['AI', 'AI Video'],
        'Paste an image or video URL, or pick "AI" / "AI Video" to generate this '
        + 'scene. All blank -> Num of Images AI images.');
    }
    updated.push(sheet.getName());
  });

  ui.alert(
    'Hook_Card dropdowns',
    updated.length
      ? 'Music, Voiceover and Manual Media dropdowns applied to:\n\n• ' + updated.join('\n• ')
      : 'No Hook_Card tab found. Name a tab to contain "hook card" and re-run.',
    ui.ButtonSet.OK
  );
}


/** Open an audition panel: every pickable Music name with inline players for
 *  its two variations, so the operator chooses BY EAR and then types/picks the
 *  name in the Music column (blank = random). Audio streams from the deployed
 *  HF Space, so a track only plays once it has been pushed (git push hf main);
 *  before that the players show but won't load. Names come from
 *  HOOK_CARD_MUSIC_NAMES so this panel always matches the dropdown. */
function showHookCardMusicPreview() {
  const rowsHtml = HOOK_CARD_MUSIC_NAMES.map(function (name) {
    const slug = String(name).toLowerCase();
    return [1, 2].map(function (v) {
      const url = HOOK_CARD_MUSIC_BASE_URL + slug + '_' + v + '.mp3';
      // The label IS the exact value to type in the Music column.
      return '<div class="row"><div class="name">' + name + ' ' + v + '</div>' +
        '<audio controls preload="none" src="' + url + '"></audio></div>';
    }).join('');
  }).join('');

  const html = '<!DOCTYPE html><html><head><base target="_top"><style>' +
    'body{font-family:Arial,Helvetica,sans-serif;margin:0;padding:14px;color:#222}' +
    'h3{margin:0 0 4px}' +
    '.hint{color:#666;font-size:12px;margin:0 0 12px;line-height:1.4}' +
    '.row{display:flex;align-items:center;gap:12px;padding:8px 0;border-top:1px solid #eee}' +
    '.name{width:108px;font-weight:bold;flex:0 0 auto}' +
    'audio{height:32px}' +
    '</style></head><body>' +
    '<h3>Hook_Card music</h3>' +
    '<p class="hint">Each track is labelled with its exact name. Type that name ' +
    'in the Music column (e.g. "Uplifting 2") to lock it in — or a bare style ' +
    'name for a random variation, or leave the cell blank for a random track.</p>' +
    rowsHtml +
    '</body></html>';

  const out = HtmlService.createHtmlOutput(html).setWidth(520).setHeight(560);
  SpreadsheetApp.getUi().showModalDialog(out, 'Hook_Card music preview');
}


// ─── Motion_Ads dropdowns ───────────────────────────────────────────────────

/** One-shot: (re)apply the Apple (Yes/No) dropdown on every Motion_Ads tab.
 *  Resolves the Apple column BY HEADER NAME (positional fallback = col G) so it
 *  follows the header wherever it sits. Idempotent and non-strict (blank is
 *  allowed — the backend treats a blank Apple cell as No). The Change Size
 *  column on this tab is handled by applySizeDropdowns like every other tab.
 *  Plan _plans/2026-07-08-motion-ads-tab.md. */
function applyMotionAdsDropdowns() {
  const ui = SpreadsheetApp.getUi();
  const validation = SpreadsheetApp.newDataValidation()
    .requireValueInList(['Yes', 'No'], true)
    .setAllowInvalid(true)    // blank = No
    .setHelpText('Apple = Yes → the generated image has NO people. Blank = No.')
    .build();

  const updated = [];
  SpreadsheetApp.getActive().getSheets().forEach(function (sheet) {
    if (_detectTabType(sheet) !== TAB_MOTION_ADS) return;
    const headerMap = _buildHeaderColMap(sheet);
    const col = _colForHeaders(headerMap, ['Apple'], MOTION_ADS_COLS.apple);
    if (!col) return;
    const firstData = 2;
    const maxRow = sheet.getMaxRows();
    if (maxRow < firstData) return;
    sheet.getRange(firstData, col, maxRow - firstData + 1, 1)
      .setDataValidation(validation);
    updated.push(sheet.getName());
  });

  ui.alert(
    'Motion_Ads dropdowns updated',
    updated.length
      ? 'Apple (Yes/No) dropdown applied to:\n\n• ' + updated.join('\n• ')
        + '\n\nAlso run "Update size dropdowns on all tabs" for the Change Size column.'
      : 'No Motion_Ads tab found. Name a tab "Motion_Ads" and re-run.',
    ui.ButtonSet.OK
  );
}


// ─── Open Comments verbatim-script tip ──────────────────────────────────────

/** One-shot: add a header NOTE to the "Open Comments" column on every video tab
 *  explaining the verbatim-script marker. The backend speaks an Open Comments
 *  cell verbatim when it starts with "use this script:" (parsed server-side — no
 *  sheet change is needed for the feature to work); this just makes the
 *  convention DISCOVERABLE so a new operator doesn't have to be told. Skips the
 *  text-on-img tab (it produces an image, no voiceover). Resolves the column BY
 *  HEADER NAME and probes rows 1-2 like applySizeDropdowns, so it follows the
 *  header wherever it sits. Idempotent — re-running rewrites the same note.
 *  Plan _plans/2026-06-29-pinned-script-open-comments-all-tabs.md. */
function applyOpenCommentsTips() {
  const ui = SpreadsheetApp.getUi();
  const note =
    'Tip: to make the voiceover say your EXACT words, start this cell with:\n' +
    '    use this script: <your script>\n\n' +
    'It is then spoken verbatim, in any language, with no AI rewrite. Leave the\n' +
    'marker off and the system writes the script from the article instead.\n' +
    'Works on every video tab.';

  const updated = [];
  SpreadsheetApp.getActive().getSheets().forEach(function (sheet) {
    var tabType = _detectTabType(sheet);
    // No voiceover on these tabs, so the "use this script:" marker does nothing.
    if (tabType === TAB_TEXT_ON_IMG || tabType === TAB_MOTION_ADS
        || tabType === TAB_HOOK_CARD) return;
    // Headers sit on row 1 everywhere except migrated simple_x4 tabs, where
    // row 1 is the template-preview band and row 2 holds them (mirrors
    // applySizeDropdowns).
    var headerRow = 0;
    var col = 0;
    [1, 2].some(function (probe) {
      if (sheet.getLastRow() < probe || sheet.getLastColumn() === 0) return false;
      const headers = sheet.getRange(probe, 1, 1, sheet.getLastColumn()).getValues()[0];
      for (var i = 0; i < headers.length; i++) {
        const h = String(headers[i] || '').toLowerCase().trim();
        if (h === 'open comments' || h === 'open comment') {
          headerRow = probe;
          col = i + 1;
          return true;
        }
      }
      return false;
    });
    if (!col) return;    // tab has no Open Comments column — skip
    sheet.getRange(headerRow, col).setNote(note);
    updated.push(sheet.getName());
  });

  ui.alert(
    'Open Comments tips added',
    updated.length
      ? 'The "use this script:" tip note was added to:\n\n• ' + updated.join('\n• ')
      : 'No tab with an "Open Comments" column found.',
    ui.ButtonSet.OK
  );
}


// ─── Sidebar ────────────────────────────────────────────────────────────────

function showJobsSidebar() {
  const html = HtmlService.createHtmlOutputFromFile('Sidebar')
    .setTitle('Aporia Bulk Video');
  SpreadsheetApp.getUi().showSidebar(html);
}


/** Authenticated fetch with retry. Retries on 5xx / network errors with a
 *  short backoff so a transient backend blip self-heals; 4xx are real
 *  (auth / not-found) and are NOT retried. Returns parsed JSON (or null for an
 *  empty body); throws with a readable message after exhausting retries.
 *
 *  ``retryOpts`` (optional) overrides the default 3-attempt × [0.6s, 1.2s]
 *  policy. Used by the submit POST to widen the window to ~31s
 *  (SUBMIT_MAX_ATTEMPTS × SUBMIT_BACKOFF_MS) because a sleeping HF Space can
 *  take longer than 1.8s to cold-start.
 *
 *  Errors from 4xx responses carry ``permanent: true`` so callers can tell
 *  "fix the cause" apart from "try again later". */
function _fetchJson(path, options, retryOpts) {
  const backendUrl = _getBackendUrl();
  const opts = options || {};
  opts.headers = opts.headers || {};
  opts.headers['Authorization'] = 'Bearer ' + ScriptApp.getIdentityToken();
  opts.muteHttpExceptions = true;

  const maxAttempts = (retryOpts && retryOpts.maxAttempts) || 3;
  const backoffMs = (retryOpts && retryOpts.backoffMs) || [600, 1200];
  const onAttempt = retryOpts && retryOpts.onAttempt;

  var lastErr = '';
  for (var attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      const resp = UrlFetchApp.fetch(backendUrl + path, opts);
      const code = resp.getResponseCode();
      const text = resp.getContentText();
      if (code >= 200 && code < 300) {
        if (onAttempt) onAttempt(attempt, 'ok', code);
        return text ? JSON.parse(text) : null;
      }
      if (code >= 400 && code < 500) {
        // Real error (auth / validation / not-found) — retrying cannot help.
        // Tag it so the catch below rethrows instead of swallowing it into
        // the retry loop.
        if (onAttempt) onAttempt(attempt, 'fatal', code);
        const err = new Error('HTTP ' + code + ': ' + text.substring(0, 200));
        err.permanent = true;
        throw err;
      }
      lastErr = 'HTTP ' + code + ': ' + text.substring(0, 200);
      if (onAttempt) onAttempt(attempt, 'retry', code);
    } catch (e) {
      if (e && e.permanent) throw e;
      lastErr = String((e && e.message) || e);
      if (onAttempt) onAttempt(attempt, 'error', 0);
    }
    if (attempt < maxAttempts) {
      const sleep = backoffMs[attempt - 1] != null
        ? backoffMs[attempt - 1]
        : backoffMs[backoffMs.length - 1];    // cap at last entry
      Utilities.sleep(sleep);
    }
  }
  throw new Error(lastErr || 'request failed');
}


/** Light "pre-warm" hint — fires a GET /health so a sleeping backend
 *  container has a chance to wake up before we send the real submit POST.
 *  Submit fires regardless of whether this succeeds. Cooldown'd via
 *  DocumentProperties so rapid clicks don't pre-warm every time. */
function _prewarmBackend_() {
  if (!PREWARM_ENABLED) return;
  const props = PropertiesService.getDocumentProperties();
  const last = parseInt(props.getProperty('LAST_PREWARM_MS') || '0', 10);
  const now = Date.now();
  if (now - last < PREWARM_COOLDOWN_MS) {
    console.info('[bulkvid prewarm] skip cooldown', { sinceMs: now - last });
    return;
  }
  props.setProperty('LAST_PREWARM_MS', String(now));
  try {
    // /health is open and cheap; we don't need auth here. Catch BACKEND_URL
    // misconfig too — pre-warm failures are never actionable; the real
    // submit's retry policy will surface the user-facing error.
    const url = _getBackendUrl() + '/health';
    UrlFetchApp.fetch(url, { muteHttpExceptions: true });
    console.info('[bulkvid prewarm] hit', { url: url });
  } catch (e) {
    console.info('[bulkvid prewarm] error', { err: String((e && e.message) || e) });
  }
}


/** Submit a job batch with idempotency-safe retry. Stashes the idempotency
 *  key in DocumentProperties BEFORE sending so a mid-call Apps Script crash
 *  leaves the key recoverable; the server returns the SAME job_id for a
 *  repeated key, so a retry never creates a duplicate. On final failure,
 *  shows a clear Retry/Cancel dialog rather than the cryptic "HTTP 500"
 *  toast. */
function _submitJobWithRetry_(payload) {
  const props = PropertiesService.getDocumentProperties();
  // Reuse an in-flight key if one was stashed (previous submit attempt died
  // mid-call). Otherwise mint a fresh one.
  var key = props.getProperty('PENDING_SUBMIT_KEY') || '';
  if (!key) {
    key = 'sub-' + Date.now() + '-' + Utilities.getUuid().replace(/-/g, '').slice(0, 8);
    props.setProperty('PENDING_SUBMIT_KEY', key);
  }
  payload.idempotency_key = key;
  console.info('[bulkvid submit] start', { key: key, rows: _rowCountForPayload_(payload) });

  _prewarmBackend_();

  while (true) {
    try {
      const body = _fetchJson('/jobs', {
        method: 'post',
        contentType: 'application/json',
        payload: JSON.stringify(payload),
      }, {
        maxAttempts: SUBMIT_MAX_ATTEMPTS,
        backoffMs: SUBMIT_BACKOFF_MS,
        onAttempt: function (n, outcome, code) {
          console.info('[bulkvid submit] attempt', { n: n, outcome: outcome, http: code });
        },
      });
      // Success — clear the stashed key so the next click mints a fresh one.
      props.deleteProperty('PENDING_SUBMIT_KEY');
      console.info('[bulkvid submit] ok', { jobId: body && body.job_id });
      return body;
    } catch (e) {
      console.info('[bulkvid submit] final-fail', { err: String((e && e.message) || e) });
      const ui = SpreadsheetApp.getUi();
      if (e && e.permanent) {
        // 4xx — auth or validation. Retrying the same payload cannot succeed,
        // so surface the real reason instead of the busy dialog. The pending
        // key stays stashed; once the cause is fixed, the next click resumes
        // the same idempotent submit.
        ui.alert(
          'Submit rejected',
          String((e && e.message) || e) + '\n\n'
          + 'This is not a backend overload. If the message shows 401 or 403, '
          + 'your Google account is not authorized on the backend — ask the '
          + 'admin to add your email to the allowlist.',
          ui.ButtonSet.OK
        );
        return null;
      }
      // Friendly retry/cancel dialog. The SAME key is reused on Retry, so a
      // submit that actually succeeded but whose response the backend dropped
      // is idempotent — no duplicate job. Cancel leaves the key in place so
      // the user's next manual click resumes. The last error is included
      // verbatim so a screenshot of this dialog is diagnosable — "HTTP 500:
      // …" means the backend failed, an exception message means the request
      // never got through (chat 2026-06-10, the omer@ incident).
      const ans = ui.alert(
        'Backend is busy',
        'The backend is temporarily overloaded and the submit could not get '
        + 'through after ' + SUBMIT_MAX_ATTEMPTS + ' attempts.\n\n'
        + 'Last error: ' + String((e && e.message) || e).substring(0, 300) + '\n\n'
        + 'Click YES to retry, or NO to try again later from the menu.',
        ui.ButtonSet.YES_NO
      );
      if (ans === ui.Button.YES) continue;
      return null;    // user cancelled; key persists for next click
    }
  }
}


function _rowCountForPayload_(payload) {
  return (payload.rows_image_vo || payload.rows_four_images
    || payload.rows_simple || payload.rows_cartoon || payload.rows_yt_cartoon
    || payload.rows_simple_x4 || payload.rows_text_on_img
    || payload.rows_avatar || payload.rows_motion_ads
    || payload.rows_hook_card || []).length;
}


/** Called from Sidebar.html on EVERY poll cycle: jobs + per-row status for
 *  running jobs + log tails for open panes, all in ONE authenticated request.
 *
 *  Replaces what used to take three separate calls (listJobs + per-job
 *  getJobRows + per-pane getJobLog), which fanned out to ~5 backend hits per
 *  3-second cycle and saturated PA's small uWSGI worker pool. See
 *  _plans/2026-06-04-fix-sidebar-500s.md.
 *
 *  ``openLogIds`` — array of job IDs the sidebar currently has its log pane
 *  open on. Pass [] when no logs are open. */
function pollAll(openLogIds) {
  var qs = '/jobs/poll?limit=100';
  if (openLogIds && openLogIds.length) {
    // Cap client-side too (server caps at 50 and 400s above that).
    var capped = openLogIds.slice(0, 50).map(encodeURIComponent).join(',');
    qs += '&logs=' + capped;
  }
  return _fetchJson(qs, { method: 'get' });
}


/** Called from Sidebar.html: list this user's jobs (active + finished archive).
 *  Backed by GET /jobs. Kept alongside pollAll() for compatibility with any
 *  out-of-band caller (admin scripts, monitoring); the sidebar itself uses
 *  pollAll() now. */
function listJobs() {
  return _fetchJson('/jobs?limit=100', { method: 'get' });
}


/** Called from Sidebar.html: per-row status for one job. Kept for compat;
 *  pollAll() includes the same data for the running set. */
function getJobRows(jobId) {
  if (!jobId) return { job_id: '', rows: [] };
  return _fetchJson('/jobs/' + encodeURIComponent(jobId) + '/rows', { method: 'get' });
}


/** Called from Sidebar.html: tail of a job's log (optionally one row). Kept
 *  for compat; pollAll() includes log tails for the panes currently open. */
function getJobLog(jobId, rowNum) {
  if (!jobId) return { job_id: '', exists: false, lines: [] };
  var path = '/jobs/' + encodeURIComponent(jobId) + '/log?tail=200';
  if (rowNum) path += '&row=' + encodeURIComponent(rowNum);
  return _fetchJson(path, { method: 'get' });
}


// Kill calls bypass the default 3-attempt retry loop in ``_fetchJson``.
// A 504 from the kill route already means "the backend tried for 10 s
// and the libsql roundtrip is stalled" — retrying twice more (with the
// default 600 ms + 1200 ms backoffs) stretches the user-visible wait
// to ~75 s before the toast appears, which is indistinguishable from
// "the button is broken." Fail fast and let the operator click again
// or restart the backend. Plan
// _plans/2026-06-14-fast-fail-kill-and-poll-timeout.md §A.
const KILL_RETRY_OPTS = { maxAttempts: 1 };

/** Called from Sidebar.html: kill ONE specific job. Other jobs keep running —
 *  killing one never cancels another. Also aborts pending/in-flight rows so
 *  the sidebar reflects the kill immediately (plan
 *  _plans/2026-06-14-stuck-processing-rows.md §B); the response carries
 *  ``rows_aborted`` so the toast can say "Killed N rows". */
function killJob(jobId) {
  if (!jobId) return { ok: false, error: 'no job id' };
  try {
    const r = _fetchJson(
      '/jobs/' + encodeURIComponent(jobId) + '/kill',
      { method: 'post' },
      KILL_RETRY_OPTS,
    );
    return { ok: true, rows_aborted: (r && r.rows_aborted) || 0 };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}


/** Called from Sidebar.html: clear the queue — kill ALL of your active jobs.
 *  Pending and in-flight rows are aborted with a ``killed by user`` result so
 *  the sidebar reflects the kill immediately (plan
 *  _plans/2026-06-14-stuck-processing-rows.md §B). */
function killAllJobs() {
  try {
    const r = _fetchJson('/jobs/kill-all', { method: 'post' }, KILL_RETRY_OPTS);
    return {
      ok: true,
      killed: (r && r.killed) || 0,
      rows_aborted: (r && r.rows_aborted) || 0,
    };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}


// ─── Avatar picker (in-sheet modal) ──────────────────────────────────────────
//
// Opens a modal dialog showing every TikTok Symphony avatar (thumbnail +
// name + gender). Operator clicks one, dialog closes, the avatar_id is
// written to the row's Avatar ID cell + a friendly note. Backed by the
// bearer-authed GET /jobs/avatars endpoint, which fetches live from
// TikTok with cache fallback (same data the /admin/avatars page shows).

function pickAvatarForCurrentRow() {
  const ui = SpreadsheetApp.getUi();
  const sheet = SpreadsheetApp.getActiveSheet();
  const tabType = _detectTabType(sheet);
  if (tabType !== TAB_AVATAR) {
    ui.alert(
      'Avatar picker only works on the "video with avatar" tab. ' +
      'Active tab type: ' + (tabType || 'unknown')
    );
    return;
  }
  const cell = sheet.getActiveCell();
  const row = cell.getRow();
  if (row < 2) {
    ui.alert('Click on a data row (row 2 or below) first.');
    return;
  }
  // Stash the target row in DocumentProperties so the HTML callback
  // can find it after the dialog opens and the active cell drifts.
  PropertiesService.getDocumentProperties()
    .setProperty('AVATAR_PICK_TARGET_ROW', String(row));

  const html = HtmlService.createHtmlOutputFromFile('AvatarPicker')
    .setWidth(960)
    .setHeight(720);
  ui.showModalDialog(html, 'Pick an avatar for row ' + row);
}


/** Called from the AvatarPicker modal: fetch the catalog from the backend.
 *  Returns the same shape as the FastAPI route — see GET /jobs/avatars. */
function getAvatarCatalog() {
  try {
    return _fetchJson('/jobs/avatars', { method: 'get' });
  } catch (e) {
    return {
      avatars: [],
      source: 'empty',
      error: String((e && e.message) || e),
    };
  }
}


/** Called from the AvatarPicker modal when the operator clicks an avatar.
 *  Writes the avatar_id into the previously-stashed target row + adds a
 *  note showing the human-readable name + gender for context. */
function setAvatarIdForPickedRow(avatarId, displayName, gender) {
  const row = parseInt(
    PropertiesService.getDocumentProperties()
      .getProperty('AVATAR_PICK_TARGET_ROW') || '0',
    10
  );
  if (!row) return { ok: false, error: 'no target row stashed' };
  const sheet = SpreadsheetApp.getActiveSheet();
  const cell = sheet.getRange(row, AVATAR_COLS.avatarId);
  cell.setValue(avatarId);
  const noteParts = [];
  if (displayName) noteParts.push(displayName);
  if (gender) noteParts.push('(' + gender + ')');
  cell.setNote(noteParts.length ? noteParts.join(' ') : '');
  return { ok: true, row: row };
}
