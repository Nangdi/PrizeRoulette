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
    page.wait_for_timeout(1500)
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
