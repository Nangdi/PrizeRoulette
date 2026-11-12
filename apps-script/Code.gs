/**
 * 경품 룰렛 서버 (Google Apps Script 웹 앱)
 *
 * 구글 시트에 붙여서 쓴다. 처음 한 번 setup()을 실행하면 시트 탭 3개가 만들어진다.
 *   - 설정: 수령 PIN, 등수별 경품명, 관리자 코드
 *   - 재고: 날짜별 등수 남은 수량 (보기용. 실제 남은 수량은 스크립트 속성에 있음 ↓)
 *   - 기록: 참여 1건당 1줄 (참여번호, 날짜, 시각, 기기키, 등수, 수령시각)
 *
 * 요청은 POST(text/plain, JSON 본문) 한 가지로 받는다.
 *   { action: 'status', deviceKey }        → 오늘 이 기기의 참여 결과(있으면) 또는 참여 가능 여부
 *   { action: 'spin',   deviceKey }        → 추첨. 이미 참여한 기기면 새로 뽑지 않고 기존 결과를 돌려줌
 *   { action: 'claim',  deviceKey, pin }   → 스태프 PIN 확인 후 수령 처리
 *
 * ── 속도를 위한 구조 ──
 * 추첨은 ScriptLock으로 한 명씩 처리한다(같은 재고를 두 번 쓰지 않게).
 * 시트 읽기·쓰기는 한 번에 0.2~0.5초라 잠금 안에서 하면 줄이 길어지므로,
 * 잠금 안에서는 스크립트 속성(PropertiesService)만 쓴다.
 *   S|날짜        남은 수량 [1등,2등,3등,4등]  ← 실제 기준
 *   N|날짜        마지막 참여번호
 *   D|날짜|기기키  그 기기의 결과 {no,time,rank,c(수령시각)}
 * 기록 탭 줄 추가와 재고 탭 숫자 갱신은 잠금을 푼 뒤에 한다.
 *
 * 그날 첫 추첨 때 재고 탭 숫자와 기록 탭(그날 기록)을 읽어 속성을 만든다.
 * 그 뒤 재고 탭 숫자를 손으로 고치면 시트 메뉴 "룰렛 → 재고 탭 숫자를 남은 수량으로 적용"을 눌러야 반영된다.
 */

var TZ = 'Asia/Seoul';
var SHEET_SETTINGS = '설정';
var SHEET_STOCK = '재고';
var SHEET_LOG = '기록';
var RANKS = [1, 2, 3, 4];
var LOCK_WAIT_MS = 28000;
var SETTINGS_CACHE_SEC = 20; // 설정 탭을 고치면 최대 이 시간 뒤에 반영

// 기록 탭 열 순서
var COL = { no: 0, date: 1, time: 2, device: 3, rank: 4, claimedAt: 5 };

// ───────────────────────── 진입점 ─────────────────────────

function doPost(e) {
  var res;
  try {
    var req = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    res = handle(req);
  } catch (err) {
    res = { ok: false, error: 'server', message: String(err && err.message || err) };
  }
  return ContentService.createTextOutput(JSON.stringify(res))
    .setMimeType(ContentService.MimeType.JSON);
}

function doGet() {
  return ContentService.createTextOutput(JSON.stringify({ ok: true, message: 'PrizeRoulette server' }))
    .setMimeType(ContentService.MimeType.JSON);
}

function handle(req) {
  var deviceKey = String(req.deviceKey || '');
  if (!/^[A-Za-z0-9-]{8,64}$/.test(deviceKey)) return { ok: false, error: 'bad_request' };
  var settings = getSettings();

  // 관리자 모드: 설정 탭의 "관리자 코드"와 같은 코드를 보낸 브라우저.
  // 횟수 제한 없이 돌릴 수 있고, 재고·기록은 바꾸지 않는다(행사 중 디버그해도 수량에 영향 없음).
  if (req.admin) {
    if (settings.adminCode && String(req.admin) === settings.adminCode) return handleAdmin(req, settings);
    if (req.action === 'status') {
      var res = statusOf(deviceKey, settings);
      res.adminInvalid = true; // 화면에 "관리자 코드가 맞지 않습니다" 표시
      return res;
    }
  }

  if (req.action === 'status') return statusOf(deviceKey, settings);
  if (req.action === 'spin') return spin(deviceKey, settings);
  if (req.action === 'claim') return claim(deviceKey, String(req.pin || ''), settings);
  return { ok: false, error: 'bad_request' };
}

// ───────────────────────── 동작 ─────────────────────────

function statusOf(deviceKey, settings) {
  var today = todayStr();
  var rec = getRecord(today, deviceKey);
  if (rec) return resultOf(today, rec, true, settings);

  var remain = getRemain(today);
  if (!remain) {
    var stock = readStock(today); // 그날 첫 추첨 전이면 재고 탭을 본다(속성은 만들지 않음)
    remain = stock && stock.remain;
  }
  if (!remain) return { ok: true, state: 'closed', reason: 'not_event_day' };
  if (sum(remain) <= 0) return { ok: true, state: 'closed', reason: 'sold_out' };
  return { ok: true, state: 'ready' };
}

function spin(deviceKey, settings) {
  var today = todayStr();

  // 같은 기기가 다시 돌리면(새로고침, 재스캔, 재요청) 새로 뽑지 않는다. 잠금 밖에서 먼저 확인.
  var rec = getRecord(today, deviceKey);
  if (rec) return resultOf(today, rec, true, settings);

  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(LOCK_WAIT_MS);
  } catch (err) {
    return { ok: false, error: 'busy' }; // 화면이 잠시 후 다시 요청한다(같은 기기키라 중복 추첨 없음)
  }
  var out;
  try {
    out = drawLocked(today, deviceKey);
  } finally {
    lock.releaseLock();
  }
  if (out.closed) return { ok: true, state: 'closed', reason: out.closed };
  if (out.again) return resultOf(today, out.rec, true, settings);

  // ── 잠금 밖: 시트에 기록 (참여번호를 이미 받았으므로 겹치지 않음) ──
  try {
    sheet(SHEET_LOG).appendRow([out.rec.no, today, out.rec.time, deviceKey, out.rec.rank, '']);
  } catch (err) {
    console.error('기록 탭 쓰기 실패', out.rec, err); // 결과는 속성에 남아 있음
  }
  refreshStockDisplay(today);
  return resultOf(today, out.rec, false, settings);
}

/**
 * 잠금 안에서 실행. 스크립트 속성만 읽고 쓴다.
 * (그날 첫 추첨이면 initDay가 시트를 한 번 읽는다)
 */
function drawLocked(today, deviceKey) {
  var props = PropertiesService.getScriptProperties();
  var remain = getRemain(today) || initDay(today);
  if (!remain) return { closed: 'not_event_day' };

  // 같은 기기의 요청 두 개가 동시에 들어온 경우를 위해 잠금 안에서 한 번 더 본다
  var raw = props.getProperty(devKey(today, deviceKey));
  if (raw) return { again: true, rec: JSON.parse(raw) };

  var rank = drawRank(remain, Math.random());
  if (!rank) return { closed: 'sold_out' };

  remain[rank - 1] -= 1;
  var no = Number(props.getProperty('N|' + today) || 0) + 1;
  var rec = { no: no, time: nowTimeStr(), rank: rank, c: '' };
  var write = {};
  write['S|' + today] = JSON.stringify(remain);
  write['N|' + today] = String(no);
  write[devKey(today, deviceKey)] = JSON.stringify(rec);
  props.setProperties(write);
  return { rec: rec };
}

function claim(deviceKey, pin, settings) {
  if (!settings.pin || pin !== settings.pin) return { ok: false, error: 'wrong_pin' };

  var today = todayStr();
  var rec = getRecord(today, deviceKey);
  if (!rec) return { ok: false, error: 'no_record' };

  if (!rec.c) {
    // 수령은 한 기기에서만 하므로 잠금 없이 처리한다(같은 기기가 동시에 두 번 눌러도 같은 값).
    rec.c = nowTimeStr();
    if (getRemain(today)) PropertiesService.getScriptProperties().setProperty(devKey(today, deviceKey), JSON.stringify(rec));
    var log = readLog();
    for (var i = log.length - 1; i >= 1; i--) {
      if (log[i][COL.device] === deviceKey && cellStr(log[i][COL.date], 'yyyy-MM-dd') === today) {
        sheet(SHEET_LOG).getRange(i + 1, COL.claimedAt + 1).setValue(rec.c);
        break;
      }
    }
  }
  return resultOf(today, rec, true, settings);
}

/**
 * 관리자 요청. 오늘 남은 수량 비율로 뽑되(없거나 0이면 기본 10/40/150/500) 재고·기록은 바꾸지 않는다.
 */
function handleAdmin(req, settings) {
  if (req.action === 'status') return { ok: true, state: 'ready', admin: true };
  if (req.action === 'claim') {
    if (!settings.pin || String(req.pin || '') !== settings.pin) return { ok: false, error: 'wrong_pin' };
    return { ok: true, state: 'claimed', admin: true, claimedAt: nowTimeStr() };
  }
  if (req.action === 'spin') {
    var today = todayStr();
    var remain = getRemain(today);
    if (!remain) { var s = readStock(today); remain = s && s.remain; }
    if (!remain || sum(remain) <= 0) remain = [10, 40, 150, 500];
    var rank = drawRank(remain, Math.random());
    return {
      ok: true, state: 'result', again: false, admin: true,
      no: 0, date: today, time: nowTimeStr(),
      rank: rank, prize: settings.prizes[rank - 1] || '', claimedAt: ''
    };
  }
  return { ok: false, error: 'bad_request' };
}

/**
 * 남은 수량 비율로 등수를 뽑는다. (1등 확률 = 남은 1등 / 남은 전체)
 * 하루 동안 모든 재고를 다 쓰면 등수별 수량이 정확히 설정값과 같아진다.
 * r: 0 이상 1 미만 난수. 재고가 없으면 0.
 */
function drawRank(remain, r) {
  var total = sum(remain);
  if (total <= 0) return 0;
  var pick = Math.floor(r * total);
  for (var i = 0; i < remain.length; i++) {
    if (pick < remain[i]) return i + 1;
    pick -= remain[i];
  }
  return 0;
}

function resultOf(today, rec, again, settings) {
  return {
    ok: true,
    state: 'result',
    again: again,
    no: Number(rec.no),
    date: today,
    time: rec.time,
    rank: Number(rec.rank),
    prize: settings.prizes[rec.rank - 1] || '',
    claimedAt: rec.c || ''
  };
}

// ───────────────────────── 스크립트 속성 (실제 남은 수량·참여 기록) ─────────────────────────

function devKey(today, deviceKey) { return 'D|' + today + '|' + deviceKey; }

function getRemain(today) {
  var raw = PropertiesService.getScriptProperties().getProperty('S|' + today);
  return raw ? JSON.parse(raw) : null;
}

/**
 * 이 기기의 오늘 결과. 그날 속성이 아직 없으면(첫 추첨 전, 예전 버전 기록) 기록 탭에서 찾는다.
 */
function getRecord(today, deviceKey) {
  var props = PropertiesService.getScriptProperties();
  var raw = props.getProperty(devKey(today, deviceKey));
  if (raw) return JSON.parse(raw);
  if (props.getProperty('S|' + today)) return null; // 그날 속성이 있으면 속성이 기준
  var log = readLog();
  for (var i = log.length - 1; i >= 1; i--) {
    if (log[i][COL.device] === deviceKey && cellStr(log[i][COL.date], 'yyyy-MM-dd') === today) return rowToRec(log[i]);
  }
  return null;
}

/**
 * 그날 첫 추첨 때(잠금 안) 재고 탭 숫자와 기록 탭의 그날 기록으로 속성을 만든다.
 * 재고 탭에 그날 줄이 없으면 null(운영일 아님).
 */
function initDay(today) {
  var stock = readStock(today);
  if (!stock) return null;
  var write = {};
  var n = 0;
  var log = readLog();
  for (var i = 1; i < log.length; i++) {
    if (cellStr(log[i][COL.date], 'yyyy-MM-dd') !== today) continue;
    var rec = rowToRec(log[i]);
    write[devKey(today, String(log[i][COL.device]))] = JSON.stringify(rec);
    n = Math.max(n, rec.no);
  }
  write['S|' + today] = JSON.stringify(stock.remain);
  write['N|' + today] = String(n);
  PropertiesService.getScriptProperties().setProperties(write);
  return stock.remain;
}

function rowToRec(row) {
  return {
    no: Number(row[COL.no]) || 0,
    time: cellStr(row[COL.time], 'HH:mm:ss'),
    rank: Number(row[COL.rank]),
    c: cellStr(row[COL.claimedAt], 'HH:mm:ss')
  };
}

/**
 * 재고 탭의 그날 숫자를 속성의 남은 수량으로 맞춘다(보기용).
 * 여러 요청이 동시에 쓰면 순서가 뒤바뀌어 옛 숫자가 남을 수 있으므로 문서 잠금으로 한 명만 쓰고,
 * 쓰는 동안 참여번호가 바뀌었으면 다시 쓴다. 잠금을 못 잡은 요청은 건너뛴다(잡은 쪽이 최신값까지 씀).
 * 추첨 잠금(ScriptLock)과는 다른 잠금이라 추첨을 막지 않는다.
 */
function refreshStockDisplay(today) {
  var lock = LockService.getDocumentLock();
  if (!lock || !lock.tryLock(0)) return;
  try {
    var props = PropertiesService.getScriptProperties();
    var stock = readStock(today);
    if (!stock) return;
    for (var k = 0; k < 5; k++) {
      var n0 = props.getProperty('N|' + today);
      var remain = getRemain(today);
      if (!remain) return;
      sheet(SHEET_STOCK).getRange(stock.rowIndex + 1, 2, 1, RANKS.length).setValues([remain]);
      SpreadsheetApp.flush();
      if (props.getProperty('N|' + today) === n0) break;
    }
  } catch (err) {
    console.error('재고 탭 갱신 실패', err);
  } finally {
    lock.releaseLock();
  }
}

// ───────────────────────── 시트 읽기 ─────────────────────────

function sheet(name) {
  var sh = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(name);
  if (!sh) throw new Error('시트 탭이 없습니다: ' + name + ' (setup()을 먼저 실행하세요)');
  return sh;
}

// 설정 탭을 매 요청마다 읽지 않게 잠깐 기억해 둔다.
function getSettings() {
  var cache = CacheService.getScriptCache();
  var raw = cache.get('settings');
  if (raw) return JSON.parse(raw);
  var s = readSettings();
  cache.put('settings', JSON.stringify(s), SETTINGS_CACHE_SEC);
  return s;
}

function readSettings() {
  var values = sheet(SHEET_SETTINGS).getDataRange().getValues();
  var map = {};
  for (var i = 1; i < values.length; i++) map[String(values[i][0]).trim()] = String(values[i][1]).trim();
  return {
    pin: map['수령 PIN'] || '',
    adminCode: map['관리자 코드'] || '',
    prizes: RANKS.map(function (r) { return map[r + '등 경품'] || ''; })
  };
}

function readStock(today) {
  var values = sheet(SHEET_STOCK).getDataRange().getValues();
  for (var i = 1; i < values.length; i++) {
    if (cellStr(values[i][0], 'yyyy-MM-dd') === today) {
      return {
        rowIndex: i,
        remain: RANKS.map(function (r) { return Math.max(0, Number(values[i][r]) || 0); })
      };
    }
  }
  return null;
}

function readLog() {
  return sheet(SHEET_LOG).getDataRange().getValues();
}

// ───────────────────────── 유틸 ─────────────────────────

function todayStr() { return Utilities.formatDate(new Date(), TZ, 'yyyy-MM-dd'); }
function nowTimeStr() { return Utilities.formatDate(new Date(), TZ, 'HH:mm:ss'); }
function sum(a) { var s = 0; for (var i = 0; i < a.length; i++) s += a[i]; return s; }

// 시트가 날짜·시각 문자열을 Date로 바꿔 저장했어도 문자열로 맞춘다.
function cellStr(v, fmt) {
  if (v === null || v === undefined || v === '') return '';
  if (Object.prototype.toString.call(v) === '[object Date]') return Utilities.formatDate(v, TZ, fmt);
  return String(v).trim();
}

// ───────────────────────── 시트 메뉴 (운영자용) ─────────────────────────

function onOpen() {
  SpreadsheetApp.getUi().createMenu('룰렛')
    .addItem('재고 탭 숫자를 남은 수량으로 적용 (오늘)', 'menuApplyStock')
    .addItem('오늘 시험 데이터 초기화', 'menuResetToday')
    .addToUi();
}

function menuApplyStock() {
  var ui = SpreadsheetApp.getUi();
  var today = todayStr();
  var ok = ui.alert('재고 적용',
    '재고 탭 ' + today + ' 줄의 숫자를 지금 남은 수량으로 적용합니다.\n(손으로 고친 숫자를 반영할 때만 누르세요)', ui.ButtonSet.OK_CANCEL);
  if (ok !== ui.Button.OK) return;
  var r = applyStockFromSheet(today);
  ui.alert(r ? '적용했습니다: ' + r.join(' / ') : '재고 탭에 오늘 날짜 줄이 없습니다.');
}

function menuResetToday() {
  var ui = SpreadsheetApp.getUi();
  var today = todayStr();
  var ok = ui.alert('시험 데이터 초기화',
    today + ' 참여 기록(기록 탭의 오늘 줄)과 남은 수량을 지웁니다.\n재고 탭 오늘 줄 숫자를 먼저 원하는 수량으로 고친 뒤 누르세요.\n되돌릴 수 없습니다.', ui.ButtonSet.OK_CANCEL);
  if (ok !== ui.Button.OK) return;
  var n = resetToday(today);
  ui.alert('오늘 기록 ' + n + '줄을 지웠습니다. 다음 추첨 때 재고 탭 숫자로 다시 시작합니다.');
}

// 재고 탭의 그날 숫자를 실제 남은 수량(속성)으로 쓴다. 기록·참여번호는 그대로.
function applyStockFromSheet(today) {
  var lock = LockService.getScriptLock();
  lock.waitLock(LOCK_WAIT_MS);
  try {
    var stock = readStock(today);
    if (!stock) return null;
    if (getRemain(today)) PropertiesService.getScriptProperties().setProperty('S|' + today, JSON.stringify(stock.remain));
    else initDay(today);
    return stock.remain;
  } finally {
    lock.releaseLock();
  }
}

// 그날 속성과 기록 탭의 그날 줄을 지운다. 지운 기록 줄 수를 돌려준다.
function resetToday(today) {
  var lock = LockService.getScriptLock();
  lock.waitLock(LOCK_WAIT_MS);
  try {
    var props = PropertiesService.getScriptProperties();
    props.getKeys().forEach(function (k) {
      if (k === 'S|' + today || k === 'N|' + today || k.indexOf('D|' + today + '|') === 0) props.deleteProperty(k);
    });
    var sh = sheet(SHEET_LOG);
    var log = sh.getDataRange().getValues();
    var keep = [], removed = 0;
    for (var i = 1; i < log.length; i++) {
      if (cellStr(log[i][COL.date], 'yyyy-MM-dd') === today) removed++;
      else keep.push(log[i]);
    }
    if (log.length > 1) sh.getRange(2, 1, log.length - 1, log[0].length).clearContent();
    if (keep.length) sh.getRange(2, 1, keep.length, log[0].length).setValues(keep);
    SpreadsheetApp.flush();
    return removed;
  } finally {
    lock.releaseLock();
  }
}

// ───────────────────────── 초기 설정 (편집기에서 한 번 실행) ─────────────────────────

/**
 * 시트 탭 3개를 만들고 기본값을 넣는다. 이미 있는 탭은 건드리지 않는다.
 * 설정 탭은 이미 있어도 빠진 항목(새 버전에서 추가된 항목)만 아래에 덧붙인다.
 * 날짜·시각이 Date로 바뀌지 않게 모든 칸을 텍스트 서식으로 둔다.
 */
function setup() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  ss.setSpreadsheetTimeZone(TZ);

  var settingRows = [
    ['항목', '값'],
    ['수령 PIN', '0000'],
    ['1등 경품', '1등 경품'],
    ['2등 경품', '2등 경품'],
    ['3등 경품', '3등 경품'],
    ['4등 경품', '과학관 입장권 + 키링'],
    ['관리자 코드', ''] // 비워 두면 관리자 모드 사용 안 함
  ];
  if (!makeSheet(ss, SHEET_SETTINGS, settingRows)) {
    var sh = ss.getSheetByName(SHEET_SETTINGS);
    var have = sh.getDataRange().getValues().map(function (r) { return String(r[0]).trim(); });
    settingRows.forEach(function (r) { if (have.indexOf(r[0]) < 0) sh.appendRow(r); });
  }
  makeSheet(ss, SHEET_STOCK, [
    ['날짜', '1등', '2등', '3등', '4등'],
    ['2026-11-24', 10, 40, 150, 500],
    ['2026-11-28', 10, 40, 150, 500],
    ['2026-11-29', 10, 40, 150, 500]
  ]);
  makeSheet(ss, SHEET_LOG, [
    ['참여번호', '날짜', '시각', '기기키', '등수', '수령시각']
  ]);
}

// 새로 만들었으면 true, 이미 있으면 false
function makeSheet(ss, name, rows) {
  if (ss.getSheetByName(name)) return false;
  var sh = ss.insertSheet(name);
  sh.getRange(1, 1, 1000, rows[0].length).setNumberFormat('@');
  sh.getRange(1, 1, rows.length, rows[0].length).setValues(rows);
  sh.setFrozenRows(1);
  return true;
}
