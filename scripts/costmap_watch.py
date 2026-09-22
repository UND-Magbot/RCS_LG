"""로봇이 '장애물' 로 보고 있는 것을 **실시간 한 줄** 로 찍는다 — 읽기 전용.

    python scripts/costmap_watch.py <로봇IP> [--half 0.475] [--save]

멈칫할 때 이 출력만 보면 갈린다.

    정면에 값이 뜨는데 멈춤   → 로봇이 뭔가를 보고 있다 (그 지점을 눈으로 확인)
    정면이 '없음' 인데 멈춤   → 장애물 문제가 아니다 (구동계·위치추정·경로추종)

`--half` 는 로봇 반폭(m). 랙을 실었으면 0.475, 빈 몸이면 0.23.
이 폭 안에 들어온 것만 '정면' 으로 센다 — 서버 안전존과 같은 기준이라 비교가 된다.
좌/우 는 폭과 무관하게 옆으로 가장 가까운 것이라, 로봇 자체 회피를 볼 때 쓴다.

costmap 은 1 Hz 다. 짧은 멈칫은 프레임 사이에 묻힐 수 있으니 --save 로 남겨 두면
나중에 PNG 를 열어 그 순간을 볼 수 있다.
"""
import argparse
import base64
import io
import json
import math
import os
import time

import numpy as np
import websocket
from PIL import Image

TOPICS = ["/maps/1cm/1hz", "/tracked_pose", "/planning_state"]
FWD_MAX = 4.0          # 정면 판정에서 이보다 먼 것은 안 본다 (m)
# 좌/우 판정은 **로봇 몸통 옆** 만 본다. 이 범위를 넓게 잡으면 정면 장애물이
# 좌우 최소값으로 섞여 들어와 값이 틀린다(2026-09-09 합성 costmap 검증에서 확인).
SIDE_FWD = 0.5


def masks(png: bytes):
    """costmap PNG → (실제 장애물, 팽창 구역) 불리언 배열. 위가 0행."""
    a = np.asarray(Image.open(io.BytesIO(png)).convert("RGB")).astype(np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    redness = r - np.maximum(g, b)
    obstacle = (redness > 60) & (g < 90)          # 진한 빨강
    inflated = (redness > 25) & ~obstacle         # 연한 빨강
    return obstacle, inflated


def to_robot_frame(mask, res, origin, pose):
    """마스크의 참인 픽셀들을 로봇 기준 (전방, 좌우) 로 바꾼다."""
    rows, cols = np.nonzero(mask)
    if rows.size == 0:
        return np.empty(0), np.empty(0)
    h = mask.shape[0]
    wx = origin[0] + cols * res
    wy = origin[1] + (h - 1 - rows) * res         # origin 은 좌하단 픽셀
    dx, dy = wx - pose[0], wy - pose[1]
    c, s = math.cos(pose[2]), math.sin(pose[2])
    return dx * c + dy * s, -dx * s + dy * c      # (fwd, lat)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ip")
    ap.add_argument("--half", type=float, default=0.475, help="로봇 반폭(m)")
    ap.add_argument("--save", action="store_true", help="costmap PNG 를 남긴다")
    a = ap.parse_args()

    outdir = os.path.join("_logs", "costmap_" + time.strftime("%Y%m%d_%H%M%S"))
    if a.save:
        os.makedirs(outdir, exist_ok=True)
        print(f"PNG 저장: {outdir}")

    ws = websocket.create_connection(f"ws://{a.ip}:8090/ws/v2/topics", timeout=10)
    for t in TOPICS:
        ws.send(json.dumps({"enable_topic": t}))
    ws.settimeout(3.0)
    print(f"로봇 {a.ip} · 반폭 {a.half} m · Ctrl+C 로 종료\n")
    print(f"{'경과':>8}  {'상태':<10} {'정면':>8} {'좌':>7} {'우':>7}   비고")

    t0, pose, mstate, n = time.time(), None, "?", 0
    try:
        while True:
            try:
                m = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            topic = m.get("topic", "")

            if topic == "/tracked_pose":
                p = m.get("pos") or [m.get("x"), m.get("y")]
                o = m.get("ori", m.get("yaw", 0))
                if p and p[0] is not None:
                    pose = (float(p[0]), float(p[1]), float(o))
            elif topic == "/planning_state":
                mstate = str(m.get("move_state", "?"))
            elif topic.startswith("/maps/") and pose:
                n += 1
                png = base64.b64decode(m["data"])
                if a.save:
                    with open(os.path.join(outdir, f"{n:04d}_{time.time()-t0:07.2f}s.png"), "wb") as f:
                        f.write(png)
                obs, inf = masks(png)
                res, origin = float(m["resolution"]), m["origin"]

                fwd, lat = to_robot_frame(obs, res, origin, pose)
                band = (np.abs(lat) <= a.half) & (fwd > 0) & (fwd < FWD_MAX)
                front = fwd[band].min() if band.any() else None

                near = np.abs(fwd) < SIDE_FWD
                left = lat[near & (lat > 0)]
                right = -lat[near & (lat < 0)]
                L = left.min() if left.size else None
                R = right.min() if right.size else None

                note = ""
                if front is not None and front < 1.5:
                    note = "★ 정면 근접"
                elif front is None:
                    note = "정면 비었음"
                if (L is not None and L < a.half + 0.05) or \
                   (R is not None and R < a.half + 0.05):
                    note += "  ★ 옆이 몸에 닿음"
                f = lambda v: f"{v:6.2f}m" if v is not None else "  없음 "
                print(f"{time.time()-t0:7.1f}s  {mstate:<10} {f(front):>8} "
                      f"{f(L):>7} {f(R):>7}   {note}")
    except KeyboardInterrupt:
        print("\n종료")
    finally:
        try:
            ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
