# -*- coding: utf-8 -*-
"""depth 화면의 '색' 을 '거리' 로 바꾸는 대응표를 만든다.

왜 필요한가
  이 로봇의 depth 는 WS 로 **거리값이 아니라 컬러맵 PNG** 로 온다(실측 2026-09-15).
  원본 16비트 배열을 받을 경로가 없다 — `/depth_camera/*/points` 는 로봇이
  활성화를 거부하고, REST 에도 조회가 없다.
  게다가 색과 거리의 방향조차 확실치 않다. 하방은 '먼 바닥 = 연두', 전방은
  '먼 벽 = 파랑 / 가까운 랙 = 주황' 으로 **서로 반대로 보인다.** 값이 거리인지
  그 역수(disparity)인지 모른다.
  그래서 **알려진 거리에 판을 놓고 색을 기록해** 대응표를 직접 만든다.

어떻게 쓰나
  평평한 판을 로봇 정면에 놓고, 정해진 시간마다 뒤로 물린다.
      BackEnd\\venv\\Scripts\\python.exe scripts/depth_calib.py --ip 192.168.30.140 ^
          --step 20 --dists 0.3,0.5,1.0,1.5,2.0,2.5,3.0,4.0
  --step 초마다 다음 거리로 넘어간 것으로 보고 구간을 나눈다.
  넘어갈 때마다 화면에 "이제 N m 로" 라고 찍어주니 그때 옮기면 된다.

무엇이 나오나
  · 거리별 화면 중앙의 대표 색(RGB)과 그 색의 '컬러맵 위치'
  · 무효(검정) 픽셀 비율 -> 너무 가깝거나 멀어서 못 재는 구간
  · 레터박스를 뺀 **실제 데이터 영역** 크기 -> 화각 계산의 기준
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

from PIL import Image                      # noqa: E402
from websocket import WebSocketApp         # noqa: E402

LOG_DIR = os.path.join(_ROOT, "_logs")

CAMS = {
    "/depth_camera/forward/image":  "Depth 전방",
    "/depth_camera/downward/image": "Depth 하방",
}
DEAD = 12          # 이 밑이면 '검정(무효)' 으로 본다
CENTER = 0.34      # 중앙 몇 %를 대표 영역으로 삼을지

rows = []
lock = threading.Lock()
T0 = 0.0


def content_box(im):
    """레터박스(위아래 검은 띠)를 뺀 실제 데이터 영역을 찾는다."""
    w, h = im.size
    px = im.convert("RGB").load()
    top, bot = 0, h - 1
    def row_dead(y):
        n = sum(1 for x in range(w) if sum(px[x, y]) / 3 < DEAD)
        return n > w * 0.9
    while top < h - 1 and row_dead(top):
        top += 1
    while bot > top and row_dead(bot):
        bot -= 1
    return 0, top, w, bot + 1


def sample(im):
    """중앙 영역의 대표 색과 무효 비율."""
    x0, y0, x1, y1 = content_box(im)
    cw, ch = x1 - x0, y1 - y0
    if cw <= 0 or ch <= 0:
        return None
    mx0 = x0 + int(cw * (0.5 - CENTER / 2))
    mx1 = x0 + int(cw * (0.5 + CENTER / 2))
    my0 = y0 + int(ch * (0.5 - CENTER / 2))
    my1 = y0 + int(ch * (0.5 + CENTER / 2))
    px = im.convert("RGB").load()
    rs = gs = bs = 0
    n = dead = 0
    for y in range(my0, max(my0 + 1, my1)):
        for x in range(mx0, max(mx0 + 1, mx1)):
            r, g, b = px[x, y]
            n += 1
            if (r + g + b) / 3 < DEAD:
                dead += 1
                continue
            rs += r; gs += g; bs += b
    live = max(1, n - dead)
    return {
        "box": [x0, y0, x1, y1], "content": [cw, ch],
        "rgb": [rs // live, gs // live, bs // live],
        "dead_pct": round(100.0 * dead / max(1, n), 1),
        "n": n,
    }


def hue_pos(rgb):
    """무지개 컬러맵에서의 대략적 위치 0(파랑)~1(빨강). 색을 한 숫자로 비교하려는 것."""
    r, g, b = rgb
    mx, mn = max(rgb), min(rgb)
    if mx == mn:
        return 0.5
    d = float(mx - mn)
    if mx == r:
        h = ((g - b) / d) % 6
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    h *= 60.0                       # 0~360
    # 파랑(240) -> 빨강(0) 방향을 0~1 로 편다
    return max(0.0, min(1.0, (240.0 - h) / 240.0)) if h <= 240 else 0.0


def on_msg(ws, msg):
    if isinstance(msg, (bytes, bytearray)):
        return
    try:
        m = json.loads(msg)
    except Exception:
        return
    tp = m.get("topic")
    if tp not in CAMS or not m.get("data"):
        return
    try:
        im = Image.open(io.BytesIO(base64.b64decode(m["data"])))
        im.load()
    except Exception:
        return
    s = sample(im)
    if not s:
        return
    s.update({"t": time.time(), "topic": tp, "size": list(im.size)})
    with lock:
        rows.append(s)


def on_open(ws):
    for t in CAMS:
        ws.send(json.dumps({"enable_topic": t}))
        time.sleep(0.05)
    print("구독 완료\n", flush=True)


def main():
    global T0
    ap = argparse.ArgumentParser(description="depth 색-거리 대응표 만들기")
    ap.add_argument("--ip", required=True)
    ap.add_argument("--step", type=float, default=20.0, help="한 거리에 머무는 초")
    ap.add_argument("--dists", default="0.3,0.5,1.0,1.5,2.0,2.5,3.0,4.0",
                    help="판을 놓을 거리들 (m, 쉼표로)")
    ap.add_argument("--lead", type=float, default=8.0,
                    help="첫 거리에 놓을 준비 시간 (초)")
    a = ap.parse_args()

    dists = [float(x) for x in a.dists.split(",") if x.strip()]
    T0 = time.time()
    os.makedirs(LOG_DIR, exist_ok=True)
    out = os.path.join(LOG_DIR, "depth_calib_%s.jsonl"
                       % time.strftime("%Y%m%d_%H%M%S"))

    print("[depth_calib] %s" % a.ip)
    print("판을 로봇 정면에 놓고, 안내가 나올 때마다 그 거리로 옮기세요.")
    print("거리 %d개 x %.0f초 = 약 %.0f초\n"
          % (len(dists), a.step, a.lead + len(dists) * a.step))

    ws = WebSocketApp("ws://%s:8090/ws/v2/topics" % a.ip,
                      on_message=on_msg, on_open=on_open)
    threading.Thread(target=ws.run_forever, daemon=True).start()

    print(">>> 먼저 %.1f m 에 놓아주세요 (%.0f초)" % (dists[0], a.lead), flush=True)
    time.sleep(a.lead)

    marks = []
    for d in dists:
        s = time.time()
        print(">>> 지금부터 %.1f m   (%.0f초)" % (d, a.step), flush=True)
        time.sleep(a.step)
        marks.append((d, s, time.time()))
        if d != dists[-1]:
            print("    ... 다음 거리로 옮기세요", flush=True)

    ws.close()
    time.sleep(0.5)

    with lock:
        allrows = list(rows)
    with io.open(out, "w", encoding="utf-8") as f:
        for r in allrows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print("\n" + "=" * 78)
    for tp, nm in CAMS.items():
        rs = [r for r in allrows if r["topic"] == tp]
        print("\n■ %s   표본 %d개" % (nm, len(rs)))
        if not rs:
            print("   수신 없음 — 이 카메라는 데이터가 안 왔습니다")
            continue
        c = rs[0]
        print("   화면 %dx%d 중 실제 데이터 영역 %dx%d  (위아래 레터박스 제외)"
              % (c["size"][0], c["size"][1], c["content"][0], c["content"][1]))
        print()
        print("   거리     표본   대표색 RGB         컬러맵위치   무효%   판정")
        print("   " + "-" * 68)
        prev_pos = None
        for d, s, e in marks:
            seg = [r for r in rs if s + 2.0 <= r["t"] <= e]   # 옮기는 2초는 버린다
            if not seg:
                print("   %4.1f m   (표본 없음)" % d)
                continue
            rr = sum(x["rgb"][0] for x in seg) // len(seg)
            gg = sum(x["rgb"][1] for x in seg) // len(seg)
            bb = sum(x["rgb"][2] for x in seg) // len(seg)
            dead = sum(x["dead_pct"] for x in seg) / len(seg)
            pos = hue_pos((rr, gg, bb))
            note = ""
            if dead > 60:
                note = "거의 무효 — 감지 범위 밖"
            elif prev_pos is not None and abs(pos - prev_pos) < 0.02:
                note = "색이 안 변함 — 포화(한계)일 수 있음"
            prev_pos = pos
            print("   %4.1f m   %4d   (%3d,%3d,%3d)   %8.3f   %5.1f   %s"
                  % (d, len(seg), rr, gg, bb, pos, dead, note))
        print()
        print("   ※ 컬러맵위치가 거리에 따라 **단조롭게** 변하면 그 구간은 쓸 수 있다.")
        print("     변화가 멈추거나 무효%% 가 치솟는 지점이 감지 한계다.")
    print("\n원본  %s" % out)


if __name__ == "__main__":
    main()
