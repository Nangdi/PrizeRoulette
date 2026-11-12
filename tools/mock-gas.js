/*
 * 브라우저용 가짜 Apps Script 환경 (테스트 전용)
 *
 * index.html?mock=1 로 열면 이 파일과 apps-script/Code.gs를 불러와서
 * 실제 서버 코드를 그대로 브라우저 안에서 돌린다. 시트 내용은 localStorage에 저장한다.
 * 오늘 날짜 재고 줄을 자동으로 넣어 두므로 행사일이 아니어도 시험할 수 있다.
 *
 * tools/test.py(Playwright)가 수량·중복·수령 시험을 할 때도 이 파일을 쓴다.
 */
(function (global) {
  var STORE_KEY = 'prizeRoulette.mockSheets';

  function load() {
    try { return JSON.parse(localStorage.getItem(STORE_KEY)) || null; } catch (e) { return null; }
  }
  function save() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(data)); } catch (e) {}
  }
  var data = load() || {}; // { 시트이름: [[...], ...] }

  var maxRows = {};
  function Range(name, r, c, nr, nc) {
    this.set = function (vals) {
      if (r + nr - 1 > (maxRows[name] || 1000)) throw new Error('The coordinates of the range are outside the dimensions of the sheet.');
      var rows = data[name];
      for (var i = 0; i < nr; i++) {
        while (rows.length < r + i) rows.push([]);
        for (var j = 0; j < nc; j++) rows[r - 1 + i][c - 1 + j] = vals[i][j];
      }
      save();
      return this;
    };
    this.setValue = function (v) { return this.set([[v]]); };
    this.setValues = function (v) { return this.set(v); };
    this.clearContent = function () {
      var blank = [];
      for (var i = 0; i < nr; i++) { blank.push([]); for (var j = 0; j < nc; j++) blank[i].push(''); }
      this.set(blank);
      // 실제 시트처럼 뒤쪽 빈 줄은 getDataRange에 잡히지 않게 정리
      var rows = data[name];
      while (rows.length && rows[rows.length - 1].every(function (x) { return x === '' || x === undefined; })) rows.pop();
      save();
      return this;
    };
    this.setNumberFormat = function () { return this; };
  }

  function Sheet(name) {
    this.getDataRange = function () {
      return { getValues: function () { return JSON.parse(JSON.stringify(data[name])); } };
    };
    this.getRange = function (r, c, nr, nc) { return new Range(name, r, c, nr || 1, nc || 1); };
    this.appendRow = function (row) { data[name].push(row.slice()); save(); };
    // 실제 시트처럼 줄 수 한도를 둔다(새 탭 1000줄). 한도를 넘는 줄에 쓰면 오류.
    if (!maxRows[name]) maxRows[name] = 1000;
    this.getMaxRows = function () { return maxRows[name]; };
    this.insertRowsAfter = function (after, n) { maxRows[name] += n; };
    this.setFrozenRows = function () {};
  }

  var ss = {
    getSheetByName: function (n) { return data[n] ? new Sheet(n) : null; },
    insertSheet: function (n) { data[n] = []; save(); return new Sheet(n); },
    setSpreadsheetTimeZone: function () {}
  };

  global.SpreadsheetApp = { getActiveSpreadsheet: function () { return ss; }, flush: function () {} };
  var dummyLock = { waitLock: function () {}, tryLock: function () { return true; }, releaseLock: function () {} };
  global.LockService = {
    getScriptLock: function () { return dummyLock; },
    getDocumentLock: function () { return dummyLock; }
  };

  // 스크립트 속성 (localStorage에 저장)
  var PROP_KEY = 'prizeRoulette.mockProps';
  var props = {};
  try { props = JSON.parse(localStorage.getItem(PROP_KEY)) || {}; } catch (e) {}
  function saveProps() { try { localStorage.setItem(PROP_KEY, JSON.stringify(props)); } catch (e) {} }
  var scriptProps = {
    getProperty: function (k) { return Object.prototype.hasOwnProperty.call(props, k) ? props[k] : null; },
    setProperty: function (k, v) { props[k] = String(v); saveProps(); return this; },
    setProperties: function (o, deleteAllOthers) {
      if (deleteAllOthers) props = {};
      for (var k in o) props[k] = String(o[k]);
      saveProps(); return this;
    },
    deleteProperty: function (k) { delete props[k]; saveProps(); return this; },
    getKeys: function () { return Object.keys(props); },
    getProperties: function () { return Object.assign({}, props); }
  };
  global.PropertiesService = { getScriptProperties: function () { return scriptProps; } };
  // 캐시는 시험에서 설정 변경이 바로 보이게 항상 비어 있는 것으로 둔다
  global.CacheService = { getScriptCache: function () { return { get: function () { return null; }, put: function () {} }; } };
  global.ContentService = {
    MimeType: { JSON: 'json' },
    createTextOutput: function (t) { return { text: t, setMimeType: function () { return this; } }; }
  };
  global.Utilities = {
    formatDate: function (d, tz, fmt) {
      var p = {};
      new Intl.DateTimeFormat('en-CA', {
        timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
      }).formatToParts(d).forEach(function (x) { p[x.type] = x.value; });
      return fmt.replace('yyyy', p.year).replace('MM', p.month).replace('dd', p.day)
        .replace('HH', p.hour).replace('mm', p.minute).replace('ss', p.second);
    },
    sleep: function () {}
  };

  // Code.gs를 불러온 뒤 호출: 탭 만들기 + 오늘 날짜 재고 줄 추가
  global.mockInit = function (stockToday) {
    global.setup();
    var today = global.todayStr();
    var rows = data['재고'];
    var exists = rows.some(function (r) { return r[0] === today; });
    if (!exists) { rows.push([today].concat(stockToday || [10, 40, 150, 500])); save(); }
    // 시험용 관리자 코드 (실제 시트에서는 설정 탭에 직접 정함)
    data['설정'].forEach(function (r) { if (r[0] === '관리자 코드' && !r[1]) r[1] = 'TEST-ADMIN'; });
    save();
  };
  global.mockReset = function () { data = {}; save(); props = {}; saveProps(); maxRows = {}; };
  global.mockData = function () { return data; };
  global.mockProps = function () { return props; };
  global.mockCall = function (req) {
    return JSON.parse(global.doPost({ postData: { contents: JSON.stringify(req) } }).text);
  };
})(window);
