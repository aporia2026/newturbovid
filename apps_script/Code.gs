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
const TAB_CARTOON = 'cartoon';
const TAB_SIMPLE_X4 = 'simple_x4';
const TAB_TEXT_ON_IMG = 'text_on_img';
const TAB_AVATAR = 'avatar';

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

// Submit-POST retry policy. PythonAnywhere occasionally returns HTTP 500 when
// its uWSGI dispatcher cannot find a free worker quickly (cold-start, recycle,
// concurrent polls saturating the small pool). The Apps Script retries the
// submit with the same idempotency key — server returns the original job_id,
// no duplicate. 6 attempts × backoff = ~31s total, comfortably wider than a
// PA worker recycle window. See _plans/2026-06-04-submit-500-defensive-fix.md.
const SUBMIT_MAX_ATTEMPTS = 6;
const SUBMIT_BACKOFF_MS = [1000, 2000, 4000, 8000, 16000];

// Optional pre-warm: fire GET /health a moment before the submit so a cold PA
// worker has a chance to lazy-init. Submit fires regardless of pre-warm
// outcome — pre-warm is purely a nudge.
const PREWARM_ENABLED = true;
const PREWARM_COOLDOWN_MS = 60 * 1000;

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
    .addSeparator()
    .addItem('Migrate simple x4 columns…', 'migrateSimpleX4Columns')
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
    'e.g. https://yourname.pythonanywhere.com',
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
  // "simple" -> ONE video from the existing Manual Image, NO image generation.
  if (name.indexOf('simple') !== -1) return TAB_SIMPLE;
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

function _yes(value, def) {
  const v = String(value || '').toLowerCase();
  if (!v) return def;
  return v === 'yes' || v === 'y' || v === 'true' || v === '1';
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
    aspect_ratio: _cell(values, cols.aspectRatio) || '9:16',
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
    aspect_ratio: _cell(values, cols.aspectRatio) || '9:16',
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
    aspect_ratio: _cell(values, cols.aspectRatio) || '9:16',
    script_pattern: _cell(values, cols.scriptPattern),
    cta_enabled: _yes(_cell(values, cols.ctaEnabled), false),
    cta_text: _cell(values, cols.ctaText).slice(0, 80),    // bound at 80 chars
    open_comments: _cell(values, cols.openComments),
  };
}


function _readAvatarRow(sheet, rowNum) {
  // video with avatar: A-D from Image-VO, plus Avatar ID at E. The Avatar
  // ID is the TikTok Symphony avatar to narrate the video — operator picks
  // from /admin/avatars and pastes the ID here. CTA columns mirror cartoon.
  const cols = AVATAR_COLS;
  const values = sheet.getRange(rowNum, 1, 1, cols.lastInputCol).getValues()[0];
  return {
    row_num: rowNum,
    country: _cell(values, cols.country),
    vertical: _cell(values, cols.vertical),
    article_url: _cell(values, cols.article),
    manual_image_url: _cell(values, cols.manualImage),
    avatar_id: _cell(values, cols.avatarId).slice(0, 64),
    voice_over: _yes(_cell(values, cols.voiceOver), true),
    zapcap: _yes(_cell(values, cols.zapcap), false),
    aspect_ratio: _cell(values, cols.aspectRatio) || '9:16',
    script_pattern: _cell(values, cols.scriptPattern),
    cta_enabled: _yes(_cell(values, cols.ctaEnabled), false),
    cta_text: _cell(values, cols.ctaText).slice(0, 80),
    open_comments: _cell(values, cols.openComments),
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
    aspect_ratio: _cell(values, cols.aspectRatio) || '9:16',
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
    aspect_ratio: _cell(values, cols.aspectRatio) || '9:16',
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
  // Read the ready-video-1 column for all data rows.
  const values = sheet
    .getRange(firstData, readyVideoCol1, lastRow - firstData + 1, 1)
    .getValues();
  const rowNums = [];
  for (var i = 0; i < values.length; i++) {
    if (!String(values[i][0] || '').trim()) {
      rowNums.push(i + firstData);    // sheet row
    }
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
    : tabType === TAB_CARTOON ? CARTOON_COLS
    : tabType === TAB_TEXT_ON_IMG ? TEXT_ON_IMG_COLS
    : tabType === TAB_AVATAR ? AVATAR_COLS
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
    : tabType === TAB_SIMPLE_X4 ? _readSimpleX4Row
    : tabType === TAB_TEXT_ON_IMG ? _readTextOnImgRow
    : tabType === TAB_AVATAR ? _readAvatarRow
    : _readImageVORow;
  const validate = tabType === TAB_FOUR_IMAGES ? _validateFourImages
    : tabType === TAB_CARTOON ? _validateCartoon
    : tabType === TAB_SIMPLE_X4 ? _validateSimpleX4
    : tabType === TAB_TEXT_ON_IMG ? _validateTextOnImg
    : tabType === TAB_AVATAR ? _validateAvatar
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
      : tabType === TAB_CARTOON ? CARTOON_COLS
      : tabType === TAB_TEXT_ON_IMG ? TEXT_ON_IMG_COLS
      : tabType === TAB_AVATAR ? AVATAR_COLS
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
  else if (tabType === TAB_CARTOON) payload.rows_cartoon = rows;
  else if (tabType === TAB_SIMPLE_X4) payload.rows_simple_x4 = rows;
  else if (tabType === TAB_TEXT_ON_IMG) payload.rows_text_on_img = rows;
  else if (tabType === TAB_AVATAR) payload.rows_avatar = rows;
  else payload.rows_image_vo = rows;

  const body = _submitJobWithRetry_(payload);
  if (body === null) {
    // User cancelled the "Backend is busy" dialog. The pending idempotency
    // key remains stashed in DocumentProperties so the next click resumes
    // the same submit (no duplicate).
    return;
  }

  PropertiesService.getDocumentProperties().setProperty('LAST_JOB_ID', body.job_id);

  var message = 'Job submitted: ' + body.job_id + '\n' +
                'Rows queued: ' + body.row_count;
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
 *  (SUBMIT_MAX_ATTEMPTS × SUBMIT_BACKOFF_MS) because PA's frontend can take
 *  longer than 1.8s to recover a recycling uWSGI worker. */
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
        throw new Error('HTTP ' + code + ': ' + text.substring(0, 200));
      }
      lastErr = 'HTTP ' + code + ': ' + text.substring(0, 200);
      if (onAttempt) onAttempt(attempt, 'retry', code);
    } catch (e) {
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


/** Light "pre-warm" hint — fires a GET /health so PA's uWSGI has a chance to
 *  wake up a cold worker before we send the real submit POST. Submit fires
 *  regardless of whether this succeeds. Cooldown'd via DocumentProperties so
 *  rapid clicks don't pre-warm every time. */
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
      // Friendly retry/cancel dialog. The SAME key is reused on Retry, so a
      // submit that actually succeeded but whose response PA dropped is
      // idempotent — no duplicate job. Cancel leaves the key in place so the
      // user's next manual click resumes.
      const ui = SpreadsheetApp.getUi();
      const ans = ui.alert(
        'Backend is busy',
        'PythonAnywhere is temporarily overloaded and the submit could not get '
        + 'through after ' + SUBMIT_MAX_ATTEMPTS + ' attempts.\n\n'
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
    || payload.rows_simple || payload.rows_cartoon || []).length;
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


/** Called from Sidebar.html: kill ONE specific job. Other jobs keep running —
 *  killing one never cancels another. */
function killJob(jobId) {
  if (!jobId) return { ok: false, error: 'no job id' };
  try {
    _fetchJson('/jobs/' + encodeURIComponent(jobId) + '/kill', { method: 'post' });
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}


/** Called from Sidebar.html: clear the queue — kill ALL of your active jobs.
 *  Rows already in progress finish; everything still waiting is cancelled. */
function killAllJobs() {
  try {
    const r = _fetchJson('/jobs/kill-all', { method: 'post' });
    return { ok: true, killed: (r && r.killed) || 0 };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}
