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


// ─── Menu ────────────────────────────────────────────────────────────────────

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Aporia Bulk Video')
    .addItem('Generate selected rows', 'generateSelected')
    .addItem('Generate all unprocessed', 'generateAllUnprocessed')
    .addSeparator()
    .addItem('Show job status sidebar', 'showJobsSidebar')
    .addSeparator()
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
  // "simple x4" -> the 4-video GENERATION flow (Manual Image as inspiration ->
  // generate 4 new images -> 4 videos). Must be checked BEFORE plain "simple",
  // since the name also contains "simple".
  if (name.indexOf('x4') !== -1) return TAB_IMAGE_VO;
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
  // Cartoon shares the Image-VO layout but ignores Manual Image (scenes are
  // generated from scratch), so the payload omits manual_image_url.
  const cols = IMAGE_VO_COLS;
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


// ─── Selection helpers ──────────────────────────────────────────────────────

function _selectedDataRowNumbers(sheet) {
  const ranges = sheet.getActiveRangeList().getRanges();
  const rowNums = {};
  ranges.forEach(function (range) {
    const start = range.getRow();
    const end = start + range.getNumRows() - 1;
    for (var r = Math.max(start, 2); r <= end; r++) {
      rowNums[r] = true;
    }
  });
  return Object.keys(rowNums).map(Number).sort(function (a, b) { return a - b; });
}


function _unprocessedRowNumbers(sheet, readyVideoCol1) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return [];
  // Read the ready-video-1 column for all data rows.
  const values = sheet.getRange(2, readyVideoCol1, lastRow - 1, 1).getValues();
  const rowNums = [];
  for (var i = 0; i < values.length; i++) {
    if (!String(values[i][0] || '').trim()) {
      rowNums.push(i + 2);    // sheet row
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
  const rowNums = _selectedDataRowNumbers(sheet);
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
  const cols = tabType === TAB_FOUR_IMAGES ? FOUR_IMAGES_COLS : IMAGE_VO_COLS;
  const rowNums = _unprocessedRowNumbers(sheet, cols.readyVideo1);
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
  // Simple + Image-VO share the same input columns/readers; 4Images and cartoon differ.
  const readRow = tabType === TAB_FOUR_IMAGES ? _readFourImagesRow
    : tabType === TAB_CARTOON ? _readCartoonRow
    : _readImageVORow;
  const validate = tabType === TAB_FOUR_IMAGES ? _validateFourImages
    : tabType === TAB_CARTOON ? _validateCartoon
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
    const videoCol = (tabType === TAB_FOUR_IMAGES ? FOUR_IMAGES_COLS : IMAGE_VO_COLS).readyVideo1;
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
  else payload.rows_image_vo = rows;

  var body;
  try {
    body = _fetchJson('/jobs', {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify(payload),
    });
  } catch (e) {
    SpreadsheetApp.getUi().alert('Submit failed:\n' + String((e && e.message) || e));
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


// ─── Sidebar ────────────────────────────────────────────────────────────────

function showJobsSidebar() {
  const html = HtmlService.createHtmlOutputFromFile('Sidebar')
    .setTitle('Aporia Bulk Video');
  SpreadsheetApp.getUi().showSidebar(html);
}


/** Authenticated fetch with retry. Retries up to 3 times on 5xx / network
 *  errors with a short backoff, so a transient backend blip self-heals instead
 *  of surfacing in the sidebar as an error. 4xx are real (auth / not-found) and
 *  are NOT retried. Returns parsed JSON (or null for an empty body); throws with
 *  a readable message after exhausting retries. */
function _fetchJson(path, options) {
  const backendUrl = _getBackendUrl();
  const opts = options || {};
  opts.headers = opts.headers || {};
  opts.headers['Authorization'] = 'Bearer ' + ScriptApp.getIdentityToken();
  opts.muteHttpExceptions = true;

  var lastErr = '';
  for (var attempt = 1; attempt <= 3; attempt++) {
    try {
      const resp = UrlFetchApp.fetch(backendUrl + path, opts);
      const code = resp.getResponseCode();
      const text = resp.getContentText();
      if (code >= 200 && code < 300) {
        return text ? JSON.parse(text) : null;
      }
      if (code >= 400 && code < 500) {
        throw new Error('HTTP ' + code + ': ' + text.substring(0, 200));
      }
      lastErr = 'HTTP ' + code + ': ' + text.substring(0, 200);
    } catch (e) {
      lastErr = String((e && e.message) || e);
    }
    if (attempt < 3) Utilities.sleep(600 * attempt);    // 0.6s, then 1.2s
  }
  throw new Error(lastErr || 'request failed');
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
