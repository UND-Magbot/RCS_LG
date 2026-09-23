#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""로봇이 자동 녹화한 주행 로그(bag)를 받아온다 — **읽기 전용**.

    python scripts/fetch_bags.py --ip 10.14.182.126
    python scripts/fetch_bags.py --ip 10.14.182.126 --date 2026-09-22 --from 15:50 --to 16:30

무엇인가
  AutoXing 로봇은 주행 로그를 **10분 단위 rosbag 으로 계속 녹화**한다.
  화면의 파란 버튼(→고객센터)을 누르지 않아도 이미 저장돼 있다.
  용량이 차면 오래된 것부터 지워지므로, 필요한 시간대를 빨리 받아둬야 한다.

  2026-09-22 확인 — 로봇 한 대에 453개 3.0GB, 8일치 보관 중이었다.

왜 필요한가
  비이상적 정지(급감속)의 원인을 우리 로그로는 더 좁히지 못했다.
  로봇이 스스로 속도를 낮추는데 그 **근거를 토픽으로 주지 않기 때문**이다.
  bag 에는 로봇 내부 상태가 들어 있어 제조사가 원인을 찾을 수 있다.

★ 로봇에 아무 명령도 보내지 않는다. 목록 조회와 파일 다운로드뿐이다.

받은 파일은 _logs/bags/ 에 저장된다. 이어받기를 지원하므로 중간에 끊겨도
다시 실행하면 이어서 받는다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUTDIR = os.path.join(ROOT, "_logs", "bags")

TS = re.compile(r"_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})\.bag$")


def fetch_list(ip: str, timeout: float = 30.0) -> list[dict]:
    url = "http://%s:8090/bags/" % ip
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def parse(fn: str):
    """파일명 → (날짜, 시각 분 단위). 못 읽으면 None."""
    m = TS.search(fn)
    if not m:
        return None
    d, hh, mm, _ = m.groups()
    return d, int(hh) * 60 + int(mm)


def hhmm(v: int) -> str:
    return "%02d:%02d" % (v // 60, v % 60)


def to_min(s: str) -> int:
    s = s.strip().replace("：", ":")
    if ":" not in s:
        raise ValueError("HH:MM 형식으로 주세요 (예: 15:50)")
    h, m = s.split(":")[:2]
    return int(h) * 60 + int(m)


def download(url: str, dest: str, size: int) -> bool:
    """이어받기 지원 다운로드. 이미 다 받았으면 건너뛴다."""
    have = os.path.getsize(dest) if os.path.exists(dest) else 0
    if size and have >= size:
        print("      이미 있음 (%.1f MB)" % (have / 1048576))
        return True
    req = urllib.request.Request(url)
    if have:
        req.add_header("Range", "bytes=%d-" % have)
        print("      이어받기 %.1f MB 부터" % (have / 1048576))
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as r, \
                open(dest, "ab" if have else "wb") as f:
            got = have
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if size:
                    pct = got / size * 100
                    sys.stdout.write("\r      %5.1f%%  %6.1f MB  %4.1f MB/s   "
                                     % (pct, got / 1048576,
                                        (got - have) / 1048576 / max(time.time() - t0, .01)))
                    sys.stdout.flush()
    except urllib.error.HTTPError as e:
        print("\n      [실패] HTTP %s" % e.code)
        return False
    except Exception as e:
        print("\n      [실패] %s" % str(e)[:80])
        return False
    print("\r      완료  %.1f MB                        " % (os.path.getsize(dest) / 1048576))
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", required=True, help="로봇 IP (현장 10.14.182.126)")
    ap.add_argument("--date", default="", help="YYYY-MM-DD. 생략하면 목록만 보여준다")
    ap.add_argument("--from", dest="frm", default="", help="시작 시각 HH:MM")
    ap.add_argument("--to", dest="to", default="", help="끝 시각 HH:MM")
    ap.add_argument("--out", default=OUTDIR)
    a = ap.parse_args()

    print("로봇 %s 에서 bag 목록을 받습니다…" % a.ip)
    try:
        items = fetch_list(a.ip)
    except Exception as e:
        print("실패: %s" % e)
        print("로봇과 같은 망에 있는지 확인하세요.")
        return 1

    rows = []
    for it in items:
        p = parse(it.get("filename", ""))
        if p:
            rows.append((p[0], p[1], it))
    rows.sort()
    if not rows:
        print("bag 이 없습니다.")
        return 1

    total = sum(it.get("size_bytes", 0) for _, _, it in rows)
    print("  %d개 · %.1f GB · %s ~ %s"
          % (len(rows), total / 1073741824, rows[0][0], rows[-1][0]))

    # 날짜별 요약
    days: dict[str, list] = {}
    for d, m, it in rows:
        days.setdefault(d, []).append((m, it))
    print()
    print("  날짜        개수   시간대")
    for d in sorted(days):
        ms = [m for m, _ in days[d]]
        print("  %s  %3d개   %s ~ %s" % (d, len(ms), hhmm(min(ms)), hhmm(max(ms))))

    if not a.date:
        print()
        print("받으려면 --date 와 --from/--to 를 주세요. 예:")
        print("  python scripts/fetch_bags.py --ip %s --date %s --from 15:50 --to 16:30"
              % (a.ip, sorted(days)[-1]))
        return 0

    if a.date not in days:
        print("\n%s 에 해당하는 bag 이 없습니다." % a.date)
        return 1

    lo = to_min(a.frm) if a.frm else 0
    hi = to_min(a.to) if a.to else 24 * 60
    # bag 은 '시작 시각' 기준 10분치라, 구간에 걸치는 것까지 포함한다
    pick = [(m, it) for m, it in days[a.date] if m + 10 > lo and m < hi]
    if not pick:
        print("\n그 시간대에 bag 이 없습니다.")
        return 1

    need = sum(it.get("size_bytes", 0) for _, it in pick)
    print()
    print("=" * 60)
    print("  받을 것 %d개 · %.0f MB" % (len(pick), need / 1048576))
    for m, it in pick:
        print("    %s  %8s  %s" % (hhmm(m), it.get("size"), it.get("filename")))
    print("=" * 60)

    os.makedirs(a.out, exist_ok=True)
    ok = 0
    for i, (m, it) in enumerate(pick, 1):
        fn = it["filename"]
        print("\n  [%d/%d] %s" % (i, len(pick), fn))
        if download(it["download_url"], os.path.join(a.out, fn), it.get("size_bytes", 0)):
            ok += 1

    print()
    print("=" * 60)
    print("  %d/%d 개 받았습니다" % (ok, len(pick)))
    print("  저장 위치: %s" % a.out)
    print()
    print("  이 파일을 제조사(AutoXing)에 전달하면 로봇 내부 상태를 분석할 수 있습니다.")
    print("=" * 60)
    return 0 if ok == len(pick) else 1


if __name__ == "__main__":
    sys.exit(main())
