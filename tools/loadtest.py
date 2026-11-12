"""
배포한 Apps Script 서버에 동시 요청을 보내 수량이 넘치지 않는지 확인한다.

  python tools/loadtest.py <웹앱 /exec 주소> [동시 인원=30]

주의: 실제 시트의 오늘 재고를 쓴다. 행사 전에 재고 탭에 오늘 날짜 시험 줄
(예: 오늘 | 1 | 2 | 3 | 24 = 30개)을 넣고 돌린 뒤, 시험 줄과 기록 탭의 시험 기록을 지운다.
"""
import json
import sys
import time
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor


def call(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "text/plain"})
    for attempt in range(5):
        t = time.time()
        with urllib.request.urlopen(req, timeout=60) as r:
            res = json.loads(r.read())
        if res.get("error") != "busy":
            return res, time.time() - t
        time.sleep(1.5)
    return res, time.time() - t


def main():
    url = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    keys = [f"load-{uuid.uuid4()}" for _ in range(n)]

    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(lambda k: call(url, {"action": "spin", "deviceKey": k}), keys))

    ranks = Counter(r["rank"] for r, _ in results if r.get("state") == "result")
    states = Counter(r.get("state") or r.get("error") for r, _ in results)
    nos = sorted(r["no"] for r, _ in results if r.get("state") == "result")
    times = sorted(t for _, t in results)
    print("응답 상태:", dict(states))
    print("등수별:", dict(sorted(ranks.items())))
    print("참여번호 중복:", len(nos) - len(set(nos)), "건")
    print(f"응답 시간: 최소 {times[0]:.1f}s / 중간 {times[len(times)//2]:.1f}s / 최대 {times[-1]:.1f}s")

    # 같은 기기 재요청은 같은 결과여야 함
    again, _ = call(url, {"action": "spin", "deviceKey": keys[0]})
    print("같은 기기 재요청 → again:", again.get("again"), "/ 같은 참여번호:", again.get("no") == results[0][0].get("no"))


if __name__ == "__main__":
    main()
