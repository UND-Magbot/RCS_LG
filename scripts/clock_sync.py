#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""로봇 시계와 서버(이 PC) 시계의 **차이를 재서 기록**한다 — 읽기 전용.

    python scripts/clock_sync.py --ip 10.14.182.126
    python scripts/clock_sync.py --ip 10.14.182.126 --precise     # 초 경계까지 추적
    python scripts/clock_sync.py --ip 10.14.182.126 --watch 300   # 5분마다 계속 기록

왜 필요한가
  비이상적 정지(급감속)의 원인을 찾을 때 **서버 로그와 로봇 bag 로그를 나란히**
  놓아야 한다. 그런데 두 시계가 같다는 보장이 없다.

  로봇은 시각을 NTP(chrony)로 받아온다. 설정된 시계 서버는 전부 인터넷 주소다.
      pool ntp.ubuntu.com / 0~2.ubuntu.pool.ntp.org
      server ntp1.autoxing.com / ntp2.autoxing.com

  ★ 2026-09-28 현장 확인 — **로봇은 실제로 동기가 된다.**
      "there is no need to make step: system time is 0.000035180 seconds fast"
    M2M 망에서도 NTP 에 닿는 것이다. "현장은 인터넷이 없으니 로봇 시계가
    흘러간다" 는 처음 전제는 **틀렸다.**

    그래서 이 도구의 쓸모는 바뀐다 — 로봇이 아니라 **서버PC 시계가 틀어졌는지**
    보는 수단이다. 두 로그를 겹치려면 어느 쪽이 틀어졌든 차이를 알아야 한다.

  게다가 로봇은 스스로 따라잡지 못한다.
      maxslewrate 277.778 ppm   1시간에 1초씩만 고친다
      makestep 600 0            '한 번에 확 맞추기' 가 꺼져 있다
  1분 어긋나면 고치는 데 60시간이 걸린다.

  그래서 **시계를 맞추는 대신 차이를 기록**한다. 로그를 볼 때 그만큼 옮겨서
  겹치면 된다. 로봇 설정을 건드리지 않으므로 실패할 여지가 없다.

부호 규약 — 헷갈리기 쉬우니 기록에 문장으로도 남긴다
    offset_sec = 로봇 시각 - 서버 시각      (로봇이 빠르면 +)
    서버 시각 = 로봇 시각 - offset_sec
    로봇 시각 = 서버 시각 + offset_sec

정밀도
  로봇 시각은 HTTP `Date` 헤더로 읽는다 — **초 단위 해상도**라 그대로 쓰면
  ±0.5초 불확실성이 남는다. `--precise` 를 주면 초가 바뀌는 순간을 짧은 간격으로
  포착해 ±50ms 수준까지 좁힌다(요청이 수십 번 나가지만 전부 조회다).
  bag 은 10분 단위 파일이라 구간을 찾는 데는 ±0.5초로도 충분하다.

★ 로봇에 아무 명령도 보내지 않는다. `GET` 만 쓴다.
  `POST /services/step_time`(시각 강제 보정)은 **부르지 않는다** — 주행 중에
  시각이 점프하면 무슨 일이 생기는지 확인된 바가 없다.
"""
from __future__ import annotations

import argparse
import email.utils
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

ROBOT_PORT = 8090
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LOG = os.path.join(_ROOT, "_logs", "clock_offset.jsonl")

# Date 헤더는 초 해상도다. 이 값이 그대로 불확실성이 된다.
COARSE_UNCERTAINTY_SEC = 0.5
# --precise 에서 초 경계를 찾을 때 쓰는 폴링 간격과 상한.
PRECISE_POLL_SEC = 0.05
PRECISE_MAX_SEC = 2.5


def _robot_date(ip: str, timeout: float = 6.0) -> "tuple[float, float, float] | None":
    """로봇의 `Date` 헤더를 읽는다.

    반환: (로봇 epoch초, 요청 중간 시점의 PC epoch초, 왕복 ms). 실패면 None.

    ※ 표준 라이브러리만 쓴다(2026-09-28). 현장 서버PC 의 시스템 파이썬에는
      requests 가 없어서 `ModuleNotFoundError` 로 막혔다. 진단 도구가 환경
      때문에 못 돌면 쓸모가 없다.
    """
    # GET 만 쓴다. HEAD 를 먼저 시도하다 405 를 받으면 폴백 처리가 번거로워지고
    # (실측 — 백엔드는 HEAD 를 405 로 거부한다), 이 응답은 작은 JSON 이라
    # GET 비용이 낮다. 현장 도구는 단순한 편이 낫다.
    url = f"http://{ip}:{ROBOT_PORT}/device/info/brief"
    try:
        t0 = time.time()
        with urllib.request.urlopen(url, timeout=timeout) as r:
            hdr = r.headers
            r.read(64)                  # 헤더만 필요하지만 연결을 깔끔히 닫는다
        t1 = time.time()
        d = hdr.get("Date")
        if not d:
            return None
        rt = email.utils.parsedate_to_datetime(d).timestamp()
        return rt, (t0 + t1) / 2.0, (t1 - t0) * 1000.0
    except Exception:
        return None


def _step_time(ip: str, timeout: float = 6.0) -> dict | None:
    """로봇이 스스로 보고하는 시각 오차 — `GET /services/step_time`.

    NTP 에 닿을 때만 의미가 있다. 현장(인터넷 없음)에서는 실패하거나
    "no NTP" 류의 메시지가 온다. 그것도 정보이므로 그대로 남긴다.
    """
    try:
        with urllib.request.urlopen(
                f"http://{ip}:{ROBOT_PORT}/services/step_time", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "body": e.read()[:200].decode("utf-8", "replace")}
    except Exception as e:
        return {"error": str(e)[:200]}


def _precise_offset(ip: str) -> "tuple[float, float] | None":
    """초가 바뀌는 순간을 포착해 오프셋을 좁힌다.

    `Date` 헤더가 N초에서 N+1초로 넘어가는 시점을 잡으면, 그 순간 로봇 시계는
    정확히 N+1.000 초다. 그때의 PC 시각과 비교하면 초 해상도 한계를 넘는다.

    반환: (offset_sec, 불확실성_sec). 상한 안에 경계를 못 잡으면 None.
    """
    first = _robot_date(ip)
    if first is None:
        return None
    prev_sec = int(first[0])
    deadline = time.time() + PRECISE_MAX_SEC
    while time.time() < deadline:
        time.sleep(PRECISE_POLL_SEC)
        s = _robot_date(ip)
        if s is None:
            return None
        rt, pc_mid, rtt = s
        if int(rt) != prev_sec:
            # 방금 경계를 넘었다 — 로봇 시각은 int(rt).000 초.
            # 관측 지연은 최대 (폴링 간격 + 왕복/2) 이므로 그만큼이 불확실성이다.
            unc = PRECISE_POLL_SEC + rtt / 2000.0
            return float(int(rt)) - pc_mid, unc
        prev_sec = int(rt)
    return None


def measure(ip: str, samples: int = 5, precise: bool = False) -> dict:
    """로봇-서버 시계 차이를 재서 dict 로 돌려준다. **조회만 한다.**"""
    best = None          # 왕복이 가장 짧은 샘플이 가장 정확하다 (NTP 와 같은 논리)
    for _ in range(max(1, samples)):
        s = _robot_date(ip)
        if s is None:
            continue
        if best is None or s[2] < best[2]:
            best = s
        time.sleep(0.05)

    rec: dict = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "robot_ip": ip,
    }
    if best is None:
        rec["error"] = "로봇 Date 헤더를 읽지 못했다 (통신 실패)"
        rec["step_time"] = _step_time(ip)
        return rec

    rt, pc_mid, rtt = best
    offset = rt - pc_mid
    unc = COARSE_UNCERTAINTY_SEC + rtt / 2000.0
    method = "Date 헤더 (초 해상도)"

    if precise:
        p = _precise_offset(ip)
        if p is not None:
            offset, unc = p
            method = "초 경계 추적"

    rec.update({
        "offset_sec": round(offset, 3),
        "uncertainty_sec": round(unc, 3),
        "rtt_ms": round(rtt, 1),
        "method": method,
        "robot_time": datetime.fromtimestamp(rt).strftime("%Y-%m-%d %H:%M:%S"),
        "server_time": datetime.fromtimestamp(pc_mid).strftime("%Y-%m-%d %H:%M:%S"),
        # ★ 부호를 헷갈리지 않도록 사람이 읽는 문장으로도 남긴다
        "how_to_convert": (f"서버 시각 = 로봇 시각 - ({offset:+.3f})초  /  "
                           f"로봇 시각 = 서버 시각 + ({offset:+.3f})초"),
        "step_time": _step_time(ip),
    })
    return rec


def append_log(rec: dict, path: str = DEFAULT_LOG) -> str:
    """기록을 jsonl 로 한 줄 덧붙인다. 실패해도 예외를 올리지 않는다."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return path
    except Exception as e:
        print(f"  [경고] 기록 실패: {e}", file=sys.stderr)
        return ""


def describe(rec: dict) -> str:
    """사람이 읽을 한 덩어리로 만든다 (다른 스크립트의 헤더에도 쓴다)."""
    if rec.get("error"):
        return f"시계 차이: 측정 실패 — {rec['error']}"
    off = rec["offset_sec"]
    unc = rec["uncertainty_sec"]
    who = "로봇이 빠름" if off > 0 else ("로봇이 느림" if off < 0 else "일치")
    lines = [
        f"시계 차이 {off:+.3f} 초 (±{unc:.3f}, {who})   [{rec['method']}]",
        f"  로봇 {rec['robot_time']}  /  서버 {rec['server_time']}   왕복 {rec['rtt_ms']} ms",
        f"  {rec['how_to_convert']}",
    ]
    st = rec.get("step_time") or {}
    if isinstance(st, dict):
        if st.get("message"):
            lines.append(f"  로봇 자체 보고: {st['message']}")
        elif st.get("error"):
            lines.append(f"  로봇 자체 보고: 확인 불가 ({st['error']})  "
                         f"← 현장에서는 NTP 에 못 닿아 정상적으로 이렇게 나온다")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="로봇-서버 시계 차이 측정 (읽기 전용)")
    ap.add_argument("--ip", required=True, help="로봇 IP")
    ap.add_argument("--precise", action="store_true",
                    help="초 경계를 추적해 ±50ms 수준까지 좁힌다 (요청이 수십 번 나간다)")
    ap.add_argument("--watch", type=float, default=0, metavar="초",
                    help="이 간격으로 계속 측정한다 (0=한 번만). 드리프트를 보려면 쓴다")
    ap.add_argument("--out", default=DEFAULT_LOG, help=f"기록 파일 (기본 {DEFAULT_LOG})")
    a = ap.parse_args()

    print(f"로봇 {a.ip} — 시계 차이 측정 (조회만 한다)")
    print(f"기록: {a.out}\n")
    n = 0
    try:
        while True:
            rec = measure(a.ip, precise=a.precise)
            n += 1
            print(f"[{rec['at']}] {describe(rec)}\n")
            append_log(rec, a.out)
            if a.watch <= 0:
                break
            time.sleep(a.watch)
    except KeyboardInterrupt:
        print(f"\n중단 — {n}건 기록했다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
