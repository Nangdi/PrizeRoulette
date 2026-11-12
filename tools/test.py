"""
PrizeRoulette 자동 시험 (Playwright + 설치된 Chrome)

  python tools/test.py            # 로직 + 화면 시험
  python tools/test.py --shots    # 화면 캡처를 tools/shots/ 에 저장

실제 apps-script/Code.gs를 index.html?mock=1 (tools/mock-gas.js) 위에서 돌린다.
배포한 서버의 동시 요청 시험은 tools/loadtest.py 를 쓴다.
"""
import functools
import http.server
import os
import sys
import threading
from collections import Counter

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(ROOT, "tools", "shots")
SAVE_SHOTS = "--shots" in sys.argv

failures = []


def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve():
    handler = functools.partial(QuietHandler, directory=ROOT)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def logic_tests(browser, base):
    print("[로직] 수량 · 중복 · 수령")
    page = browser.new_page()
    page.goto(base + "/index.html?mock=1&reset=1")
    page.wait_for_function("typeof window.mockCall === 'function'")

    res = page.evaluate(
        """() => {
        mockReset(); mockInit([10, 40, 150, 500]);
        const out = { ranks: [], first: null, again: null, sold: null, nos: [] };
        for (let i = 0; i < 700; i++) {
          const r = mockCall({ action: 'spin', deviceKey: 'dev-' + String(i).padStart(6, '0') });
          out.ranks.push(r.rank); out.nos.push(r.no);
        }
        out.again = mockCall({ action: 'spin', deviceKey: 'dev-000000' });
        out.status = mockCall({ action: 'status', deviceKey: 'dev-000000' });
        out.sold = mockCall({ action: 'spin', deviceKey: 'dev-new-device' });
        out.soldStatus = mockCall({ action: 'status', deviceKey: 'dev-new-device' });
        out.stockRow = mockData()['재고'].find(r => r[0] === todayStr());
        out.logRows = mockData()['기록'].length - 1;
        return out;
      }"""
    )
    c = Counter(res["ranks"])
    check(c == Counter({1: 10, 2: 40, 3: 150, 4: 500}), f"700회 추첨 등수별 수량 = {dict(sorted(c.items()))}")
    check(res["stockRow"][1:] == [0, 0, 0, 0], f"재고 탭 남은 수량 = {res['stockRow'][1:]}")
    check(res["logRows"] == 700, f"기록 탭 줄 수 = {res['logRows']}")
    check(res["nos"] == list(range(1, 701)), "참여번호 1~700 연속")
    check(res["again"]["again"] is True and res["again"]["rank"] == res["ranks"][0] and res["again"]["no"] == 1,
          "같은 기기가 다시 돌리면 새로 뽑지 않고 기존 결과(again)")
    check(res["status"]["state"] == "result" and res["status"]["no"] == 1, "참여한 기기 status → 기존 결과")
    check(res["sold"] == {"ok": True, "state": "closed", "reason": "sold_out"}, "재고 소진 후 새 기기 spin → 마감")
    check(res["soldStatus"]["reason"] == "sold_out", "재고 소진 후 새 기기 status → 마감")

    # 처음 몇 명 동안 1등이 몰리지 않는지(확률이 남은 수량 비율인지) 대략 확인
    first_100 = Counter(res["ranks"][:100])
    print(f"       참고: 처음 100명 등수 분포 {dict(sorted(first_100.items()))}")

    res = page.evaluate(
        """() => {
        mockReset(); mockInit([1, 0, 0, 0]);
        const a = mockCall({ action: 'spin', deviceKey: 'dev-claim-a' });
        return {
          a,
          noRecord: mockCall({ action: 'claim', deviceKey: 'dev-claim-zz', pin: '0000' }),
          wrong: mockCall({ action: 'claim', deviceKey: 'dev-claim-a', pin: '1234' }),
          right: mockCall({ action: 'claim', deviceKey: 'dev-claim-a', pin: '0000' }),
          twice: mockCall({ action: 'claim', deviceKey: 'dev-claim-a', pin: '0000' }),
          bad: mockCall({ action: 'spin', deviceKey: 'x' }),
          notDay: (() => { mockReset(); setup(); return mockCall({ action: 'status', deviceKey: 'dev-claim-a' }); })(),
        };
      }"""
    )
    check(res["a"]["rank"] == 1 and res["a"]["prize"] == "1등 경품", "재고 1등 1개 → 1등, 경품명은 설정 탭 값")
    check(res["noRecord"]["error"] == "no_record", "참여 기록 없는 기기 수령 → 거절")
    check(res["wrong"]["error"] == "wrong_pin", "틀린 PIN 수령 → 거절")
    check(bool(res["right"]["claimedAt"]), f"맞는 PIN 수령 → 수령시각 {res['right']['claimedAt']}")
    check(res["twice"]["claimedAt"] == res["right"]["claimedAt"], "두 번째 수령 → 처음 수령시각 유지(중복 기록 없음)")
    check(res["bad"]["error"] == "bad_request", "잘못된 기기키 → 거절")
    check(res["notDay"]["reason"] == "not_event_day", "재고 탭에 오늘 날짜 없음 → 운영일 아님")

    print("[로직] 빠른 저장소 구조 (예전 기록 이어받기 · 재고 적용 · 오늘 초기화)")
    res = page.evaluate(
        """() => {
        mockReset(); mockInit([1, 1, 1, 5]);
        const today = todayStr();
        // 예전 버전이 남긴 오늘 기록 2줄 + 다른 날 기록 1줄 (속성 없음)
        mockData()['기록'].push([1, today, '10:00:00', 'dev-old-0001', 4, ''], [2, today, '10:01:00', 'dev-old-0002', 3, '10:05:00'],
                               [1, '2026-01-01', '09:00:00', 'dev-other-day', 1, '']);
        const out = {};
        out.oldStatus = mockCall({ action: 'status', deviceKey: 'dev-old-0002' });
        out.oldSpin = mockCall({ action: 'spin', deviceKey: 'dev-old-0001' });
        out.newSpin = mockCall({ action: 'spin', deviceKey: 'dev-new-0001' });
        out.claim = mockCall({ action: 'claim', deviceKey: 'dev-new-0001', pin: '0000' });
        out.claimRow = mockData()['기록'].find(r => r[3] === 'dev-new-0001').slice();
        out.claimAgain = mockCall({ action: 'status', deviceKey: 'dev-new-0001' });
        // 운영자가 재고 탭을 손으로 고친 뒤 메뉴로 적용
        const row = mockData()['재고'].find(r => r[0] === today);
        row[1] = 0; row[2] = 0; row[3] = 0; row[4] = 3;
        out.applied = applyStockFromSheet(today);
        out.afterApply = [];
        for (let i = 0; i < 4; i++) out.afterApply.push(mockCall({ action: 'spin', deviceKey: 'dev-apply-' + String(i).padStart(4, '0') }));
        out.stockShown = mockData()['재고'].find(r => r[0] === today).slice(1);
        // 오늘 시험 데이터 초기화
        row[1] = 1; row[2] = 0; row[3] = 0; row[4] = 0;
        out.removed = resetToday(today);
        out.logAfterReset = mockData()['기록'].slice(1).map(r => r[1]);
        out.propsLeft = Object.keys(mockProps()).filter(k => k.includes(today)).length;
        out.afterReset = mockCall({ action: 'spin', deviceKey: 'dev-old-0001' });
        return out;
      }"""
    )
    check(res["oldStatus"]["state"] == "result" and res["oldStatus"]["claimedAt"] == "10:05:00", "속성 없는 예전 기록 → 기록 탭에서 결과 찾음")
    check(res["oldSpin"]["again"] is True and res["oldSpin"]["no"] == 1, "예전 기록 있는 기기 spin → 새로 안 뽑음")
    check(res["newSpin"]["no"] == 3 and res["newSpin"]["again"] is False, f"새 기기 참여번호는 예전 기록 다음 번호 ({res['newSpin']['no']})")
    check(bool(res["claim"]["claimedAt"]) and res["claimRow"][5] == res["claim"]["claimedAt"], "수령 → 속성과 기록 탭 수령시각 모두 기록")
    check(res["claimAgain"]["claimedAt"] == res["claim"]["claimedAt"], "수령 후 다시 열어도 수령시각 유지")
    check(res["applied"] == [0, 0, 0, 3], "메뉴 '재고 적용' → 재고 탭 숫자가 남은 수량이 됨")
    ranks = [r.get("rank") for r in res["afterApply"]]
    check(ranks[:3] == [4, 4, 4] and res["afterApply"][3].get("reason") == "sold_out", f"적용 후 4등 3개만 나오고 마감 {ranks}")
    check(res["stockShown"] == [0, 0, 0, 0], f"재고 탭 표시도 남은 수량으로 갱신 {res['stockShown']}")
    check(res["removed"] == 6 and res["logAfterReset"] == ["2026-01-01"] and res["propsLeft"] == 0,
          f"메뉴 '오늘 초기화' → 오늘 기록 {res['removed']}줄·속성 삭제, 다른 날 기록은 유지")
    check(res["afterReset"]["no"] == 1 and res["afterReset"]["rank"] == 1 and not res["afterReset"]["again"],
          "초기화 후 같은 기기도 새로 참여, 참여번호 1부터, 재고 탭 숫자로 시작")

    print("[로직] 기록 탭 (3일 × 700건 · 번호 자리 · 복구)")
    res = page.evaluate(
        """() => {
        mockReset(); mockInit([10, 40, 150, 500]);
        const realToday = window.todayStr;
        const days = ['2026-11-24', '2026-11-28', '2026-11-29'];
        const out = { days: [] };
        days.forEach((d, di) => {
          window.todayStr = () => d;
          for (let i = 0; i < 700; i++) mockCall({ action: 'spin', deviceKey: 'dev-' + di + '-' + String(i).padStart(6, '0') });
          out.days.push(statsOf(d));
        });
        // 둘째 날 기록 몇 줄을 지워(쓰기 실패 흉내) 점검 → 복구
        window.todayStr = () => '2026-11-28';
        const log = mockData()['기록'];
        let wiped = 0;
        for (let i = 1; i < log.length && wiped < 3; i++) if (log[i][1] === '2026-11-28' && log[i][0] % 100 === 0) { log[i] = ['', '', '', '', '', '']; wiped++; }
        out.beforeFix = statsOf('2026-11-28');
        out.rebuilt = rebuildLog('2026-11-28');
        out.afterFix = statsOf('2026-11-28');
        out.otherDays = [statsOf('2026-11-24').sheetRows, statsOf('2026-11-29').sheetRows];
        out.logLen = mockData()['기록'].length - 1;
        window.todayStr = realToday;
        return out;
      }"""
    )
    for d in res["days"]:
        check(d["records"] == 700 and d["byRank"] == [10, 40, 150, 500] and d["sheetRows"] == 700
              and d["missingInSheet"] == 0 and d["sheetOutOfOrder"] == 0 and d["sheetDup"] == 0
              and d["stockTab"] == [0, 0, 0, 0],
              f"{d['date']}: 서버 {d['records']}건 {d['byRank']}, 기록 탭 {d['sheetRows']}줄, 빠짐 {d['missingInSheet']}, 순서 어긋남 {d['sheetOutOfOrder']}, 재고 탭 {d['stockTab']}")
    check(res["logLen"] == 2100, f"기록 탭 2100줄 (기본 1000줄 넘어도 줄 늘려서 씀) → {res['logLen']}")
    check(res["beforeFix"]["missingInSheet"] == 3, f"기록 줄 3개 지우면 점검에서 빠진 번호로 잡힘 {res['beforeFix']['missingNos']}")
    check(res["rebuilt"] == 700 and res["afterFix"]["missingInSheet"] == 0 and res["afterFix"]["sheetOutOfOrder"] == 0,
          "메뉴 '기록 탭 다시 맞추기' → 700줄 복구, 번호순")
    check(res["otherDays"] == [700, 700], f"복구해도 다른 날 기록은 그대로 {res['otherDays']}")

    print("[로직] 관리자 모드")
    res = page.evaluate(
        """() => {
        mockReset(); mockInit([1, 0, 0, 0]);
        const stockRows = mockData()['재고'].length;
        const A = { deviceKey: 'dev-admin-1', admin: 'TEST-ADMIN' };
        const spins = [];
        for (let i = 0; i < 5; i++) spins.push(mockCall({ action: 'spin', ...A }));
        const out = {
          spins,
          status: mockCall({ action: 'status', ...A }),
          stockRow: mockData()['재고'].find(r => r[0] === todayStr()).slice(),
          logRows: mockData()['기록'].length - 1,
          wrongPin: mockCall({ action: 'claim', ...A, pin: '9' }),
          claim: mockCall({ action: 'claim', ...A, pin: '0000' }),
          bad: mockCall({ action: 'status', deviceKey: 'dev-admin-2', admin: 'WRONG' }),
          badSpin: mockCall({ action: 'spin', deviceKey: 'dev-admin-2', admin: 'WRONG' }),
        };
        // 관리자 코드 줄이 없는 예전 설정 탭에 setup()을 다시 돌리면 줄이 덧붙는지
        const s = mockData()['설정'];
        s.splice(s.findIndex(r => r[0] === '관리자 코드'), 1);
        setup();
        out.reAdded = mockData()['설정'].filter(r => r[0] === '관리자 코드').length;
        out.stockAfterSetup = mockData()['재고'].length === stockRows;
        return out;
      }"""
    )
    check(all(s["state"] == "result" and s["admin"] and s["rank"] == 1 for s in res["spins"]), "관리자는 같은 기기로 5번 연속 새로 돌림")
    check(res["status"]["state"] == "ready" and res["status"]["admin"], "관리자 status → 항상 돌릴 수 있음")
    check(res["stockRow"][1:] == [1, 0, 0, 0] and res["logRows"] == 0, f"관리자 추첨은 재고·기록 안 바뀜 (재고 {res['stockRow'][1:]}, 기록 {res['logRows']}줄)")
    check(res["wrongPin"]["error"] == "wrong_pin" and res["claim"]["state"] == "claimed", "관리자 수령 확인도 PIN 검사")
    check(res["bad"].get("adminInvalid") is True and not res["bad"].get("admin"), "틀린 관리자 코드 → 일반 참여 + 코드 오류 표시")
    check(res["badSpin"]["state"] == "result" and not res["badSpin"].get("admin") and res["badSpin"]["no"] == 1,
          "틀린 관리자 코드로 돌리면 일반 참여로 기록")
    check(res["reAdded"] == 1 and res["stockAfterSetup"] is True, "setup() 재실행 → 빠진 '관리자 코드' 줄만 추가, 다른 탭은 그대로")
    page.close()


def top_rank(page):
    return page.evaluate(
        "(() => { const s = 360 / SEGMENTS.length;"
        " const i = Math.round((((360 - angle % 360) % 360)) / s) % SEGMENTS.length;"
        " return SEGMENTS[i]; })()"
    )


def shot(page, name):
    if SAVE_SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.wait_for_timeout(500)  # 화면 등장 애니메이션이 끝난 뒤 캡처
        page.screenshot(path=os.path.join(SHOTS, name + ".png"), full_page=True)


def ui_tests(pw, browser, base):
    print("[화면] 응답이 늦을 때")
    slow = browser.new_page(viewport={"width": 390, "height": 844})
    slow.add_init_script("window.__extraDelay = 8000")
    slow.goto(base + "/index.html?mock=1&reset=1")
    slow.wait_for_selector("#v-wheel:not(.hidden)", timeout=20000)
    slow.click("#btn-spin")
    slow.wait_for_timeout(6800)
    check("기다리는 중" in slow.text_content("#wheel-note"), "응답 6초 넘으면 '기다리는 중' 안내")
    slow.wait_for_selector("#v-result:not(.hidden)", timeout=20000)
    check("1인 1회" in slow.text_content("#wheel-note"), "응답 오면 안내 원래대로")
    slow.close()

    print("[화면] 룰렛 · 결과 · 다시 보기 · 수령 · 마감")
    phone = pw.devices["iPhone 13"]
    ctx = browser.new_context(**{k: v for k, v in phone.items() if k != "default_browser_type"})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(base + "/index.html?mock=1&reset=1")
    page.wait_for_selector("#v-wheel:not(.hidden)")
    check(True, "첫 접속 → 룰렛 화면")
    shot(page, "1_wheel")

    page.click("#btn-spin")
    check(page.is_disabled("#btn-spin"), "돌리는 중 버튼 잠김")
    # 돌아가는 동안 룰렛 중심이 틀 중심에서 벗어나지 않는지(회전 기준점 확인)
    offsets = []
    for _ in range(12):
        page.wait_for_timeout(150)
        offsets.append(page.evaluate(
            """() => { const a = document.getElementById('wheel').getBoundingClientRect();
                       const b = document.querySelector('.wheel-box').getBoundingClientRect();
                       return Math.hypot((a.x + a.width / 2) - (b.x + b.width / 2), (a.y + a.height / 2) - (b.y + b.height / 2)); }"""))
    check(max(offsets) < 3, f"회전 중 룰렛 중심 고정 (최대 어긋남 {max(offsets):.1f}px)")
    over = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    check(over <= 0, f"회전 중 가로 스크롤 없음 (넘침 {over}px)")
    shot(page, "2_spinning")
    page.wait_for_selector("#v-result:not(.hidden)", timeout=15000)
    rank = int(page.text_content("#r-rank").replace("등", ""))
    check(top_rank(page) == rank, f"룰렛이 결과 등수 칸에 멈춤 (결과 {rank}등)")
    check(page.is_hidden("#r-again"), "방금 돌린 결과 → '다시 보기' 표시 없음")
    c1 = page.text_content("#r-clock")
    page.wait_for_timeout(1100)
    check(page.text_content("#r-clock") != c1, "현재 시각이 초 단위로 바뀜")
    shot(page, "3_result")

    page.goto(base + "/index.html?mock=1")
    page.wait_for_selector("#v-result:not(.hidden)")
    check(page.is_visible("#r-again") and int(page.text_content("#r-rank")[0]) == rank,
          "새로고침/재스캔 → 같은 결과 + '다시 보기' 표시")

    page.click("#btn-claim-open")
    page.fill("#pin", "9999")
    page.click("#btn-claim")
    page.wait_for_function("document.getElementById('claim-err').textContent.length > 0")
    check("PIN" in page.text_content("#claim-err"), "틀린 PIN → 오류 문구")
    shot(page, "4_wrong_pin")
    page.fill("#pin", "0000")
    page.click("#btn-claim")
    page.wait_for_selector("#r-stamp:not(.hidden)")
    check(page.is_hidden("#claim") and page.is_visible("#r-again"), "맞는 PIN → 수령 완료 도장, 수령 버튼 사라짐")
    shot(page, "5_claimed")

    page.goto(base + "/index.html?mock=1")
    page.wait_for_selector("#v-result:not(.hidden)")
    check(page.is_visible("#r-stamp"), "다시 열어도 수령 완료 도장 유지")
    ctx.close()

    # 등수마다 멈추는 칸 확인 (재고를 그 등수 1개만 두고 돌림)
    for r in [1, 2, 3, 4]:
        ctx = browser.new_context(**{k: v for k, v in phone.items() if k != "default_browser_type"})
        p = ctx.new_page()
        stock = ",".join("1" if i == r else "0" for i in range(1, 5))
        p.goto(base + f"/index.html?mock=1&reset=1&stock={stock}")
        p.wait_for_selector("#v-wheel:not(.hidden)")
        p.click("#btn-spin")
        p.wait_for_selector("#v-result:not(.hidden)", timeout=15000)
        check(p.text_content("#r-rank") == f"{r}등" and top_rank(p) == r, f"{r}등 결과 → {r}등 칸에 멈춤")
        if r == 1:
            shot(p, "6_rank1")
        ctx.close()

    ctx = browser.new_context(**{k: v for k, v in phone.items() if k != "default_browser_type"})
    p = ctx.new_page()
    p.goto(base + "/index.html?mock=1&reset=1&stock=0,0,0,0")
    p.wait_for_selector("#v-message:not(.hidden)")
    check("마감" in p.text_content("#m-title"), "재고 0 → 마감 화면")
    shot(p, "7_sold_out")
    ctx.close()

    # 실서버 모드에서 서버 요청이 실패하면 오류 화면 + 다시 시도
    ctx = browser.new_context(**{k: v for k, v in phone.items() if k != "default_browser_type"})
    ctx.route("https://script.google.com/**", lambda route: route.abort())
    p = ctx.new_page()
    p.goto(base + "/index.html")
    p.wait_for_selector("#v-message:not(.hidden)")
    check(p.is_visible("#btn-retry"), "서버 연결 실패 → 오류 화면 + 다시 시도 버튼")
    ctx.close()

    # 관리자 모드 화면
    ctx = browser.new_context(**{k: v for k, v in phone.items() if k != "default_browser_type"})
    p = ctx.new_page()
    p.on("pageerror", lambda e: errors.append(str(e)))
    p.goto(base + "/index.html?mock=1&reset=1&admin=TEST-ADMIN")
    p.wait_for_selector("#v-wheel:not(.hidden)")
    check(p.is_visible("#admin-badge") and "관리자 모드" in p.text_content("#admin-badge"), "?admin=코드 → 관리자 모드 표시")
    check("admin" not in p.url, f"주소창에서 관리자 코드 지움 ({p.url.split('/')[-1]})")
    ranks = []
    for i in range(3):
        p.click("#btn-spin")
        p.wait_for_selector("#v-result:not(.hidden)", timeout=15000)
        ranks.append(p.text_content("#r-rank"))
        if i == 0:
            check("TEST" in p.text_content("#r-no") and p.is_visible("#btn-admin-again"), "관리자 결과 → 참여번호 TEST, 다시 돌리기 버튼")
            p.click("#btn-claim-open"); p.fill("#pin", "0000"); p.click("#btn-claim")
            p.wait_for_selector("#r-stamp:not(.hidden)")
            check(True, "관리자 결과도 PIN 수령 확인 → 도장")
            shot(p, "8_admin_result")
        p.click("#btn-admin-again")
        p.wait_for_selector("#v-wheel:not(.hidden)")
    check(len(ranks) == 3, f"관리자 3회 연속 돌리기 {ranks}")
    p.goto(base + "/index.html?mock=1")
    p.wait_for_selector("#v-wheel:not(.hidden)")
    check(p.is_visible("#admin-badge"), "다시 열어도 관리자 모드 유지(코드 저장됨)")
    p.goto(base + "/index.html?mock=1&admin=off")
    p.wait_for_selector("#v-wheel:not(.hidden), #v-result:not(.hidden)")
    check(p.is_hidden("#admin-badge"), "?admin=off → 관리자 모드 해제")
    p.goto(base + "/index.html?mock=1&admin=WRONG")
    p.wait_for_selector("#v-wheel:not(.hidden), #v-result:not(.hidden)")
    check("맞지 않습니다" in p.text_content("#admin-badge"), "틀린 관리자 코드 → 오류 표시")
    ctx.close()

    check(not errors, f"페이지 스크립트 오류 없음 {errors if errors else ''}")


def main():
    httpd, base = serve()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        logic_tests(browser, base)
        ui_tests(pw, browser, base)
        browser.close()
    httpd.shutdown()
    print()
    print("모두 통과" if not failures else f"실패 {len(failures)}건")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
