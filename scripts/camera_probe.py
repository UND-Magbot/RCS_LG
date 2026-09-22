# -*- coding: utf-8 -*-
"""카메라 3대의 시야를 손동작으로 찾는다 — 로봇은 세워둔 채로.

왜 이렇게 하나
  depth 이미지가 WS 로 올 때 **거리값이 아니라 컬러맵**으로 온다(실측 2026-09-15).
  원본 16비트 배열을 받을 경로가 없어서(`/depth_camera/*/points` 는 로봇이
  활성화를 거부한다) "몇 m 까지 보이나" 를 조회만으로는 못 낸다.
  그래서 **사람이 카메라 앞에서 손을 움직이고, 어느 화면이 변하는지** 로 역산한다.

무엇을 재나
  · 카메라 3대 각각의 프레임 간 변화량 (0 = 정지 화면, 크면 뭔가 움직였다)
  · 그 변화가 화면 **어느 구역**에서 났는지 (3x3 격자) -> 좌우·상하 방향
  · 같은 시각의 라이다 점 변화 -> "라이다에 안 걸리는 위치" 라는 전제가 맞는지 검증
  · planning_state -> 로봇이 장애물로 인식해 뭔가 했는지

로봇은 정지 상태로 두므로 위치추정이 나빠도 안전하다.

쓰는 법
  BackEnd\\venv\\Scripts\\python.exe scripts/camera_probe.py --ip 192.168.30.140 --sec 130
  기록 중에 카메라 하나씩 앞에서 30초 움직이고, 사이에 3초 이상 가만히 있으면
  끝나고 **활동 구간을 자동으로 잘라서** 구간별로 어느 카메라가 반응했는지 표로 낸다.
  어느 카메라 차례인지 미리 말하지 않아도 된다 — 데이터가 알려준다.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import threading
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "BackEnd"))

from PIL import Image, ImageChops, ImageStat  # noqa: E402
from websocket import WebSocketApp            # noqa: E402

LOG_DIR = os.path.join(_ROOT, "_logs")

CAMS = {
    "/rgb_cameras/front/compressed": "RGB 전방",
    "/depth_camera/forward/image":   "Depth 전방",
    "/depth_camera/downward/image":  "Depth 하방",
}
GRID = 3            # 화면을 3x3 으로 나눠 어디가 변했는지 본다
QUIET = 3.0         # 이 시간 이상 조용하면 구간을 자른다 (초)

# ★ 임계를 고정값으로 두면 안 된다 — 카메라마다 '가만히 있어도 생기는 변화' 가 다르다.
#   RGB 화면에는 `26/09/15 19:22:42 0.00m/s` 타임스탬프가 **오버레이로 찍혀** 있어서
#   아무도 안 움직여도 매 프레임 그 자리가 바뀐다(실측 기준선 5.13).
#   그래서 ① 글자가 있는 위·아래 띠를 잘라내고 ② 시작 몇 초를 기준선으로 재서
#   카메라별 임계를 따로 잡는다.
CROP = {
    "/rgb_cameras/front/compressed": (0.07, 0.93),   # 세로 위·아래 7% 잘라냄
}
TH_MIN = 1.0        # 아무리 조용해도 이 밑은 움직임으로 안 본다
TH_MULT = 2.0       # 기준선 최대값의 몇 배를 넘어야 움직임인가

T0 = 0.0
BASE_END = 0.0      # 기준선 구간 끝 시각
TH = {}             # 토픽 -> 카메라별 임계 (기록이 끝난 뒤 산출한다)
RUN_MAX = {}        # 토픽 -> 지금까지 본 최대 변화량 (실시간 출력용)
prev = {}           # 토픽 -> 직전 PIL 이미지
rows = []           # 모든 표본
lidar_prev = None
lock = threading.Lock()
plan_events = []


def crop_for(tp, im):
    """오버레이(타임스탬프 등)가 찍히는 띠를 잘라낸다."""
    c = CROP.get(tp)
    if not c:
        return im
    w, h = im.size
    return im.crop((0, int(h * c[0]), w, int(h * c[1])))


def cell_diff(a, b):
    """전체 변화량과 3x3 구역별 변화량."""
    d = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
    total = sum(ImageStat.Stat(d).mean) / 3.0
    w, h = d.size
    cells = []
    for r in range(GRID):
        for c in range(GRID):
            box = (c * w // GRID, r * h // GRID,
                   (c + 1) * w // GRID, (r + 1) * h // GRID)
            cells.append(sum(ImageStat.Stat(d.crop(box)).mean) / 3.0)
    return total, cells


def where(cells):
    """가장 크게 변한 구역을 '상-좌' 처럼."""
    i = max(range(len(cells)), key=lambda k: cells[k])
    r, c = divmod(i, GRID)
    return "%s-%s" % (["상", "중", "하"][r], ["좌", "중", "우"][c])


def on_msg(ws, msg):
    global lidar_prev
    if isinstance(msg, (bytes, bytearray)):
        return
    try:
        m = json.loads(msg)
    except Exception:
        return
    tp = m.get("topic")
    now = time.time()

    if tp in CAMS:
        try:
            im = Image.open(io.BytesIO(base64.b64decode(m["data"])))
            im.load()
            im = crop_for(tp, im)
        except Exception:
            return
        with lock:
            p = prev.get(tp)
            prev[tp] = im
        if p is None or p.size != im.size:
            return
        total, cells = cell_diff(p, im)
        with lock:
            rows.append({"t": now, "topic": tp, "diff": round(total, 3),
                         "cells": [round(c, 2) for c in cells]})
        # 실시간 출력은 참고용이다. 판정은 기록이 끝난 뒤 조용한 구간을 찾아서 한다.
        with lock:
            mx = RUN_MAX.get(tp, 0.0)
            if total > mx:
                RUN_MAX[tp] = total
        if mx > 0 and total > mx * 0.4:
            print("  %6.1fs  %-12s 변화 %6.2f   %s"
                  % (now - T0, CAMS[tp], total, where(cells)), flush=True)
        return

    if tp == "/scan_matched_points2":
        pts = m.get("points") or []
        n = len(pts)
        with lock:
            pv = lidar_prev
            lidar_prev = n
            rows.append({"t": now, "topic": "lidar", "n": n,
                         "dn": (n - pv) if pv is not None else 0})
        return

    if tp == "/planning_state":
        st = m.get("move_state")
        stuck = m.get("stuck_state")
        if st not in (None, "idle") or stuck not in (None, "none"):
            with lock:
                plan_events.append({"t": now, "move_state": st, "stuck": stuck})


USE_LIDAR = True    # --no-lidar 로 끈다


def on_open(ws):
    # ★ 라이다(/scan_matched_points2)는 건당 35 KB 로 이 로봇에서 가장 무겁다.
    #   .140 은 통신이 불안정해 "밀린 스캔 N건 버림(지연 11초)" 이 자주 뜬다.
    #   시야만 볼 때는 빼는 편이 카메라 프레임이 더 고르게 들어온다.
    topics = list(CAMS) + ["/planning_state"]
    if USE_LIDAR:
        topics.insert(len(CAMS), "/scan_matched_points2")
    for t in topics:
        ws.send(json.dumps({"enable_topic": t}))
        time.sleep(0.05)
    print("구독 완료 (%s) — 지금부터 기록합니다\n"
          % ("카메라+라이다" if USE_LIDAR else "카메라만"), flush=True)


def segments(cam_rows, th, quiet=QUIET):
    """움직임이 있는 구간을 잘라낸다."""
    hot = [r for r in cam_rows if r["diff"] > th]
    if not hot:
        return []
    segs, s, last = [], hot[0]["t"], hot[0]["t"]
    for r in hot[1:]:
        if r["t"] - last > quiet:
            segs.append((s, last))
            s = r["t"]
        last = r["t"]
    segs.append((s, last))
    return [(a, b) for a, b in segs if b - a >= 2.0]


def main():
    global T0, USE_LIDAR
    ap = argparse.ArgumentParser(description="카메라 시야 탐침 (로봇 정지 상태)")
    ap.add_argument("--ip", required=True)
    ap.add_argument("--sec", type=float, default=130.0)
    ap.add_argument("--baseline", type=float, default=12.0,
                    help="시작 후 이 시간은 '아무도 안 움직이는' 기준선 측정 (초)")
    ap.add_argument("--no-lidar", action="store_true",
                    help="라이다 구독을 뺀다 (통신이 불안정할 때 권장)")
    a = ap.parse_args()

    USE_LIDAR = not a.no_lidar

    T0 = time.time()
    os.makedirs(LOG_DIR, exist_ok=True)
    out = os.path.join(LOG_DIR, "camera_probe_%s.jsonl"
                       % time.strftime("%Y%m%d_%H%M%S"))

    print("[camera_probe] %s   %.0f초 기록" % (a.ip, a.sec))
    print("카메라 3대 + 라이다 + 주행상태를 동시에 봅니다.")
    print("처음 %.0f초는 **기준선** 입니다 — 아무것도 하지 말고 가만히 계세요."
          % a.baseline)
    print("그 뒤로 카메라 하나씩 앞에서 움직이시고, 사이에 %.0f초 이상 쉬세요.\n" % QUIET)

    ws = WebSocketApp("ws://%s:8090/ws/v2/topics" % a.ip,
                      on_message=on_msg, on_open=on_open)
    threading.Thread(target=ws.run_forever, daemon=True).start()

    # ★ 기준선을 '시작 직후 N초' 로 잡으면 안 된다 — 사람이 언제부터 움직일지
    #   맞출 수 없기 때문이다(2026-09-15 실측: 기준선 구간에 이미 움직여서
    #   임계가 158 까지 올라가 아무것도 안 잡혔다).
    #   그래서 **기록이 다 끝난 뒤** 변화량 분포의 아래쪽(조용한 구간)을 찾아
    #   그것을 노이즈로 삼는다. 시작 타이밍을 맞출 필요가 없어진다.
    print("─" * 60)
    print("기록 중입니다. 카메라 하나씩 앞에서 움직이시고 사이에 %.0f초 이상 쉬세요." % QUIET)
    print("(시작 타이밍을 맞출 필요 없습니다 — 조용한 구간은 끝나고 자동으로 찾습니다)")
    print("─" * 60)
    print()

    t_end = T0 + a.sec
    while time.time() < t_end:
        time.sleep(1.0)
        left = t_end - time.time()
        if left > 1 and int(left) % 15 == 0:
            print("   ... 남은 시간 %.0f초" % left, flush=True)
    ws.close()
    time.sleep(0.5)

    with lock:
        allrows = list(rows)
    with io.open(out, "w", encoding="utf-8") as f:
        for r in allrows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print("\n" + "=" * 78)
    print("=== 결과 ===\n")

    cam_rows = {tp: [r for r in allrows if r.get("topic") == tp] for tp in CAMS}
    lid = [r for r in allrows if r.get("topic") == "lidar"]

    # ── 조용한 구간을 찾아 노이즈 기준을 잡는다 ──
    #   사람이 계속 움직여도 표본의 아래쪽 20% 는 쉬는 순간일 가능성이 높다.
    print("노이즈 기준 (변화량 분포의 아래쪽 20% = 조용했던 순간)")
    for tp, nm in CAMS.items():
        v = sorted(r["diff"] for r in cam_rows[tp])
        if not v:
            TH[tp] = TH_MIN
            print("   %-12s 표본 없음" % nm)
            continue
        p20 = v[max(0, int(len(v) * 0.20))]
        med = v[len(v) // 2]
        TH[tp] = max(TH_MIN, p20 * 3.0)
        print("   %-12s 하위20%% %6.2f  중앙 %6.2f  최대 %6.2f  ->  임계 %6.2f"
              % (nm, p20, med, v[-1], TH[tp]))
    print()

    print("수신량 (기준선 구간 제외)")
    for tp, nm in CAMS.items():
        rs = cam_rows[tp]
        print("   %-12s %3d 프레임   변화 평균 %5.2f  최대 %6.2f   임계 %5.2f"
              % (nm, len(rs),
                 (sum(r["diff"] for r in rs) / len(rs)) if rs else 0,
                 max((r["diff"] for r in rs), default=0), TH.get(tp, 0)))
    if USE_LIDAR:
        print("   %-12s %3d 표본   점 수 변화 최대 %+d"
              % ("라이다", len(lid), max((r["dn"] for r in lid), key=abs, default=0)))
    print()

    # 구간은 '임계 대비 가장 크게 반응한 카메라' 로 자른 뒤, 모든 카메라를 그 구간에서 비교
    def ratio(tp):
        mx = max((r["diff"] for r in cam_rows[tp]), default=0.0)
        return mx / max(0.01, TH.get(tp, 1.0))
    base = max(CAMS, key=ratio)
    segs = segments(cam_rows[base], TH.get(base, TH_MIN))
    if not segs:
        print("움직임 구간을 못 찾았습니다.")
        print("카메라별 임계: " + ", ".join("%s %.2f" % (CAMS[t], TH.get(t, 0))
                                            for t in CAMS))
        print("카메라 앞에서 충분히 크게 움직이셨는지, 기록 중이었는지 확인하세요.")
        return

    print("움직임 구간 %d개 — 구간마다 어느 카메라가 반응했는지" % len(segs))
    print("(배수 = 그 카메라 임계의 몇 배. 1.0 이하면 반응 안 한 것)\n")
    print("  구간  시작~끝      RGB 전방         Depth 전방       Depth 하방     %s"
          % ("  라이다" if USE_LIDAR else ""))
    print("  " + "-" * 78)
    for i, (s, e) in enumerate(segs, 1):
        line = "  %2d   %4.0f~%4.0fs  " % (i, s - T0, e - T0)
        mult = {}
        for tp in CAMS:
            rs = [r for r in cam_rows[tp] if s - 1.0 <= r["t"] <= e + 1.0]
            mx = max((r["diff"] for r in rs), default=0.0)
            th = TH.get(tp, TH_MIN)
            mult[tp] = mx / max(0.01, th)
            zone = ""
            if rs:
                top = max(rs, key=lambda r: r["diff"])
                if top["diff"] > th:
                    zone = where(top["cells"])
            line += "%-17s" % ("x%-4.1f %6.2f %s" % (mult[tp], mx, zone))
        ls = [r for r in lid if s - 1.0 <= r["t"] <= e + 1.0]
        dn = max((abs(r["dn"]) for r in ls), default=0)
        if USE_LIDAR:
            line += "%+6d" % dn
        print(line)
        win = max(mult, key=lambda k: mult[k])
        others = sorted(v for k, v in mult.items() if k != win)
        second = others[-1] if others else 0.0
        if mult[win] <= 1.0:
            verdict = "어느 카메라도 반응 안 함 — 3대 모두 이 위치를 못 본다"
        elif mult[win] > max(1.5, second * 2):
            verdict = "**%s 만 반응**" % CAMS[win]
        else:
            verdict = "여러 대가 같이 반응 — 시야가 겹치거나 움직임이 컸음"
        tail = ("   / 라이다 점변화 %d" % dn) if USE_LIDAR else ""
        print("       -> %s   (1위 x%.1f / 2위 x%.1f%s)"
              % (verdict, mult[win], second, tail))
    print()
    if USE_LIDAR:
        print("※ 라이다 점 변화가 기준선 수준이면 '라이다에 안 걸리는 위치' 가 맞다.")
        print("  다만 이 시험은 로봇을 세워두고 하므로 라이다에 걸려도 판정에는 영향이 없다.")
        print("  라이다 구분이 필요한 것은 주행 시험(무엇이 로봇을 세웠나) 쪽이다.")
        print()

    if plan_events:
        print("주행상태 변화 %d건 (로봇이 장애물로 인식했을 가능성):" % len(plan_events))
        for e in plan_events[:10]:
            print("   %6.1fs  move_state=%s  stuck=%s"
                  % (e["t"] - T0, e["move_state"], e["stuck"]))
    else:
        print("주행상태 변화 없음 — 로봇은 정지 상태 그대로였습니다.")
    print()
    print("원본  %s" % out)


if __name__ == "__main__":
    main()
