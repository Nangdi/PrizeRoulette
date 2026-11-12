"""
배포한 Apps Script 서버에 동시 요청을 보내 수량이 넘치지 않는지 확인한다.

  python tools/loadtest.py <웹앱 /exec 주소> [총 요청 수=30] [동시 인원=30]

서로 다른 가짜 기기(기기 키)가 한 번씩 일반 참여한다. 마감(sold_out)이 나오면 거기서 멈춘다.
총 요청 수를 재고보다 크게 주면 "재고를 다 쓸 때까지" 돌려 등수별 개수가 재고와 같은지 볼 수 있다.

주의: 실제 시트의 오늘 재고를 쓰고 기록 탭에 줄이 남는다.
행사 전에 재고 탭 오늘 날짜 시험 줄로 돌린 뒤, 시험 줄과 기록 탭의 시험 기록을 지운다.
"""
import json
import sys
import threading
import time
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor


def call(url, body):
    data = json.dumps(body).encode()
    res, busy = None, 0
    for attempt in range(6):
        t = time.time()
        try:
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "text/plain"})
            with urllib.request.urlopen(req, timeout=90) as r:
                res = json.loads(r.read())
        except Exception as e:  # 네트워크·서버 오류도 결과로 남긴다
            res = {"ok": False, "error": "exception", "message": str(e)}
        if res.get("error") not in ("busy", "exception"):
            return res, time.time() - t, busy
        busy += 1
        time.sleep(1.5 + attempt)
    return res, time.time() - t, busy


def main():
    url = sys.argv[1]
    total = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 30
    keys = [f"load-{uuid.uuid4()}" for _ in range(total)]

    stop = threading.Event()
    done = []
    lock = threading.Lock()
    started = time.time()

    def one(k):
        if stop.is_set():
            return None
        r = call(url, {"action": "spin", "deviceKey": k})
        if r[0].get("reason") == "sold_out":
            stop.set()
        with lock:
            done.append(r)
            if len(done) % 50 == 0:
                print(f"  {len(done)}건 처리 ({time.time() - started:.0f}s)", flush=True)
        return (k, r)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = [x for x in ex.map(one, keys) if x]

    elapsed = time.time() - started
    res_only = [r for _, (r, _, _) in results]
    ok = [r for r in res_only if r.get("state") == "result"]
    ranks = Counter(r["rank"] for r in ok)
    states = Counter(r.get("reason") or r.get("state") or r.get("error") for r in res_only)
    nos = sorted(r["no"] for r in ok)
    times = sorted(t for _, (_, t, _) in results)
    retries = sum(b for _, (_, _, b) in results)
    errors = [r for r in res_only if not r.get("ok")]

    print()
    print(f"총 {len(results)}건 / {elapsed:.0f}초")
    print("응답 상태:", dict(states))
    print("당첨 등수별:", dict(sorted(ranks.items())), "합계", len(ok))
    print("참여번호 중복:", len(nos) - len(set(nos)), "건 /", f"범위 {nos[0]}~{nos[-1]}" if nos else "")
    print(f"응답 시간: 중간 {times[len(times)//2]:.1f}s / 90% {times[int(len(times)*0.9)]:.1f}s / 최대 {times[-1]:.1f}s")
    print("busy·오류 재시도:", retries, "회 / 최종 오류:", len(errors), "건", errors[:3])

    # 같은 기기 재요청은 같은 결과여야 함
    first_k, (first_r, _, _) = next((k, r) for k, r in results if r[0].get("state") == "result")
    again, _, _ = call(url, {"action": "spin", "deviceKey": first_k})
    print("같은 기기 재요청 → again:", again.get("again"), "/ 같은 참여번호·등수:",
          again.get("no") == first_r.get("no") and again.get("rank") == first_r.get("rank"))
    after, _, _ = call(url, {"action": "spin", "deviceKey": f"load-{uuid.uuid4()}"})
    print("끝난 뒤 새 기기:", after.get("state"), after.get("reason", ""))


if __name__ == "__main__":
    main()
