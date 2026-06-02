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


function _validateImageVO(r) {
  if (!r.article_url) return 'article URL missing';
  if (!r.manual_image_url) return 'manual image URL missing';
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
  _submitJobForRowNums(sheet, tabType, rowNums);
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
  const cols = tabType === TAB_IMAGE_VO ? IMAGE_VO_COLS : FOUR_IMAGES_COLS;
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
  _submitJobForRowNums(sheet, tabType, rowNums);
}


function _submitJobForRowNums(sheet, tabType, rowNums) {
  const readRow = tabType === TAB_IMAGE_VO ? _readImageVORow : _readFourImagesRow;
  const validate = tabType === TAB_IMAGE_VO ? _validateImageVO : _validateFourImages;

  const rows = [];
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
  if (tabType === TAB_IMAGE_VO) payload.rows_image_vo = rows;
  else payload.rows_four_images = rows;

  const backendUrl = _getBackendUrl();
  const resp = UrlFetchApp.fetch(backendUrl + '/jobs', {
    method: 'post',
    contentType: 'application/json',
    headers: { 'Authorization': 'Bearer ' + idToken },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });

  const code = resp.getResponseCode();
  const text = resp.getContentText();

  if (code < 200 || code >= 300) {
    SpreadsheetApp.getUi().alert(
      'Submit failed (HTTP ' + code + '):\n' + text.substring(0, 500)
    );
    return;
  }

  const body = JSON.parse(text);
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


/** Called from Sidebar.html via google.script.run. */
function getLastJobStatus() {
  const jobId = PropertiesService.getDocumentProperties().getProperty('LAST_JOB_ID');
  if (!jobId) return null;
  return _getJobStatus(jobId);
}


function _getJobStatus(jobId) {
  const backendUrl = _getBackendUrl();
  const idToken = ScriptApp.getIdentityToken();
  const resp = UrlFetchApp.fetch(backendUrl + '/jobs/' + jobId, {
    headers: { 'Authorization': 'Bearer ' + idToken },
    muteHttpExceptions: true,
  });
  const code = resp.getResponseCode();
  if (code < 200 || code >= 300) {
    throw new Error('HTTP ' + code + ': ' + resp.getContentText().substring(0, 200));
  }
  return JSON.parse(resp.getContentText());
}


/** Called from Sidebar.html when the user clicks "Kill". */
function killLastJob() {
  const jobId = PropertiesService.getDocumentProperties().getProperty('LAST_JOB_ID');
  if (!jobId) return { ok: false, error: 'no active job' };
  const backendUrl = _getBackendUrl();
  const idToken = ScriptApp.getIdentityToken();
  const resp = UrlFetchApp.fetch(backendUrl + '/jobs/' + jobId + '/kill', {
    method: 'post',
    headers: { 'Authorization': 'Bearer ' + idToken },
    muteHttpExceptions: true,
  });
  const code = resp.getResponseCode();
  if (code < 200 || code >= 300) {
    return { ok: false, error: 'HTTP ' + code };
  }
  return { ok: true };
}
