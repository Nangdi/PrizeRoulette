/**
 * 경품 룰렛 서버 (Google Apps Script 웹 앱)
 *
 * 구글 시트에 붙여서 쓴다. 처음 한 번 setup()을 실행하면 시트 탭 3개가 만들어진다.
 *   - 설정: 수령 PIN, 등수별 경품명
 *   - 재고: 날짜별 등수 남은 수량 (추첨할 때마다 줄어듦)
 *   - 기록: 참여 1건당 1줄 (참여번호, 날짜, 시각, 기기키, 등수, 수령시각)
 *
 * 요청은 POST(text/plain, JSON 본문) 한 가지로 받는다.
 *   { action: 'status', deviceKey }        → 오늘 이 기기의 참여 결과(있으면) 또는 참여 가능 여부
 *   { action: 'spin',   deviceKey }        → 추첨. 이미 참여한 기기면 새로 뽑지 않고 기존 결과를 돌려줌
 *   { action: 'claim',  deviceKey, pin }   → 스태프 PIN 확인 후 수령 처리
 *
 * 추첨과 수령은 ScriptLock 안에서 한 번에 하나씩 처리한다.
 * (동시에 여러 명이 돌려도 같은 재고를 두 번 쓰지 않게 하기 위해서)
 */

var TZ = 'Asia/Seoul';
var SHEET_SETTINGS = '설정';
var SHEET_STOCK = '재고';
var SHEET_LOG = '기록';
var RANKS = [1, 2, 3, 4];
var LOCK_WAIT_MS = 20000;

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

  // 관리자 모드: 설정 탭의 "관리자 코드"와 같은 코드를 보낸 브라우저.
  // 횟수 제한 없이 돌릴 수 있고, 재고·기록 탭은 바꾸지 않는다(행사 중 디버그해도 수량에 영향 없음).
  if (req.admin) {
    var adminCode = readSettings().adminCode;
    if (adminCode && String(req.admin) === adminCode) return handleAdmin(req);
    if (req.action === 'status') {
      var res = statusOf(deviceKey);
      res.adminInvalid = true; // 화면에 "관리자 코드가 맞지 않습니다" 표시
      return res;
    }
  }

  if (req.action === 'status') return statusOf(deviceKey);
  if (req.action === 'spin') return withLock(function () { return spin(deviceKey); });
  if (req.action === 'claim') return withLock(function () { return claim(deviceKey, String(req.pin || '')); });
  return { ok: false, error: 'bad_request' };
}

function withLock(fn) {
  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(LOCK_WAIT_MS);
  } catch (err) {
    return { ok: false, error: 'busy' }; // 화면이 잠시 후 다시 요청한다(같은 기기키라 중복 추첨 없음)
  }
  try {
    var res = fn();
    SpreadsheetApp.flush();
    return res;
  } finally {
    lock.releaseLock();
  }
}

// ───────────────────────── 동작 ─────────────────────────

function statusOf(deviceKey) {
  var today = todayStr();
  var found = findRecord(deviceKey, today);
  if (found) return resultOf(found.row, true);

  var stock = readStock(today);
  if (!stock) return { ok: true, state: 'closed', reason: 'not_event_day' };
  if (sum(stock.remain) <= 0) return { ok: true, state: 'closed', reason: 'sold_out' };
  return { ok: true, state: 'ready' };
}

function spin(deviceKey) {
  var today = todayStr();

  // 같은 기기가 다시 돌리면(새로고침, 재스캔, 재요청) 새로 뽑지 않는다.
  var found = findRecord(deviceKey, today);
  if (found) return resultOf(found.row, true);

  var stock = readStock(today);
  if (!stock) return { ok: true, state: 'closed', reason: 'not_event_day' };

  var rank = drawRank(stock.remain, Math.random());
  if (!rank) return { ok: true, state: 'closed', reason: 'sold_out' };

  // 재고 1 줄이기
  stock.remain[rank - 1] -= 1;
  sheet(SHEET_STOCK).getRange(stock.rowIndex + 1, rank + 1).setValue(stock.remain[rank - 1]);

  // 기록 남기기 (참여번호는 날짜별 일련번호)
  var no = countRecords(today) + 1;
  var row = [no, today, nowTimeStr(), deviceKey, rank, ''];
  sheet(SHEET_LOG).appendRow(row);
  return resultOf(row, false);
}

function claim(deviceKey, pin) {
  var settings = readSettings();
  if (!settings.pin || pin !== settings.pin) return { ok: false, error: 'wrong_pin' };

  var today = todayStr();
  var found = findRecord(deviceKey, today);
  if (!found) return { ok: false, error: 'no_record' };

  if (!found.row[COL.claimedAt]) {
    found.row[COL.claimedAt] = nowTimeStr();
    sheet(SHEET_LOG).getRange(found.rowIndex + 1, COL.claimedAt + 1).setValue(found.row[COL.claimedAt]);
  }
  return resultOf(found.row, true);
}

/**
 * 관리자 요청. 오늘 재고 비율로 뽑되(없거나 0이면 기본 10/40/150/500) 재고·기록은 바꾸지 않는다.
 */
function handleAdmin(req) {
  var settings = readSettings();
  if (req.action === 'status') return { ok: true, state: 'ready', admin: true };
  if (req.action === 'claim') {
    if (!settings.pin || String(req.pin || '') !== settings.pin) return { ok: false, error: 'wrong_pin' };
    return { ok: true, state: 'claimed', admin: true, claimedAt: nowTimeStr() };
  }
  if (req.action === 'spin') {
    var stock = readStock(todayStr());
    var remain = stock && sum(stock.remain) > 0 ? stock.remain : [10, 40, 150, 500];
    var rank = drawRank(remain, Math.random());
    return {
      ok: true, state: 'result', again: false, admin: true,
      no: 0, date: todayStr(), time: nowTimeStr(),
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

function resultOf(row, again) {
  var settings = readSettings();
  var rank = Number(row[COL.rank]);
  return {
    ok: true,
    state: 'result',
    again: again,
    no: Number(row[COL.no]),
    date: cellStr(row[COL.date], 'yyyy-MM-dd'),
    time: cellStr(row[COL.time], 'HH:mm:ss'),
    rank: rank,
    prize: settings.prizes[rank - 1] || '',
    claimedAt: cellStr(row[COL.claimedAt], 'HH:mm:ss')
  };
}

// ───────────────────────── 시트 읽기 ─────────────────────────

function sheet(name) {
  var sh = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(name);
  if (!sh) throw new Error('시트 탭이 없습니다: ' + name + ' (setup()을 먼저 실행하세요)');
  return sh;
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

function findRecord(deviceKey, today) {
  var values = sheet(SHEET_LOG).getDataRange().getValues();
  for (var i = values.length - 1; i >= 1; i--) {
    if (values[i][COL.device] === deviceKey && cellStr(values[i][COL.date], 'yyyy-MM-dd') === today) {
      return { rowIndex: i, row: values[i] };
    }
  }
  return null;
}

function countRecords(today) {
  var values = sheet(SHEET_LOG).getDataRange().getValues();
  var n = 0;
  for (var i = 1; i < values.length; i++) if (cellStr(values[i][COL.date], 'yyyy-MM-dd') === today) n++;
  return n;
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
