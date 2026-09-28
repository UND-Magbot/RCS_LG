# -*- coding: utf-8 -*-
"""footprint 여유 마진 실측 — "장애물 감지거리가 타이트한가" 를 수치로 본다.

무엇을 재는가
  로봇은 랙을 들면 자기 풋프린트를 랙 크기까지 키운다(`supportsDynamicFootprints`).
  그 **풋프린트 경계와 실제 랙(라이다에 찍히는 다리) 사이의 여유**가 마진이다.

      마진이 크다  → 랙이 좀 틀어져도 자기 몸 안. 안전하다.
      마진이 작다  → 조금만 틀어지거나 라이다가 튀면 **자기 랙이 장애물이 된다.**

  계산상 예상 (S300 rack.specs 기준)
      풋프린트 경계  0.475 m   ( width 0.765 + margin 0.0925*2 = 0.95 의 반 )
      랙 다리 위치   0.3825 m  ( width 0.765 의 반 )
      ─────────────────────────
      여유           0.0925 m  ← 이 값이 실제로 얼마인지 재는 것이 이 스크립트다

★ 먼저 확인되는 것 — **라이다가 자기 랙을 보기는 하는가.**
  내부 점이 0개면 라이다 평면에 랙 다리가 안 걸린다는 뜻이고, 그러면
  "자기 랙을 장애물로 본다" 는 가설 자체가 성립하지 않는다. 그것부터 알려준다.

좌표계 (safety_zone.py 와 동일)
  라이다 점은 **맵 좌표**로 온다. `/tracked_pose` 로 로봇 좌표계로 되돌린다.
  로봇 좌표계는 X = 우 / Y = 전. `/robot_model.footprint` 도 같은 좌표계다.

쓰는 법
  # 제자리에서 20초 관찰 (권장 — 랙을 들고 세워둔 채로)
  BackEnd\\venv\\Scripts\\python.exe scripts/footprint_margin.py --ip 192.168.30.110 --sec 20

  # 주행하는 내내 관찰 (Ctrl+C 로 종료)
  ... --sec 0 --label 배송1회차

  # 랙을 옮겨가며 비교
  ... --sec 15 --label 좌측8cm

⚠ 로봇을 움직이지 않는다. 관찰만 한다.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

try:
    from websocket import create_connection
except ImportError:
    print("[중단] websocket-client 가 없다.")
    print("       BackEnd\\venv\\Scripts\\python.exe 로 실행할 것.")
    sys.exit(1)


def _force_utf8_console() -> None:
    """윈도우 콘솔에서 한글이 깨지지 않게 한다."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# 풋프린트 경계 바깥 이만큼까지를 "몸 근처" 로 보고 따로 센다.
# 자기 랙이 경계를 살짝 넘은 경우를 잡아내기 위한 것이다.
OUTSIDE_NEAR_M = 0.30

# 이 값보다 마진이 작으면 타이트하다고 본다.
# 근거 — 위치추정 오차와 라이다 산포가 각각 2~3 cm 수준이라 5 cm 아래면
# 정상 주행에서도 경계를 넘나들 수 있다.
TIGHT_WARN_M = 0.05

# 몸 주변 이 범위 밖의 점은 아예 보지 않는다 (통로 벽까지 다 계산하면 느리다)
NEAR_BOX_M = 2.0


def point_in_poly(x: float, y: float, poly) -> bool:
    """다각형 내부 판정 (ray casting)."""
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < xin:
                inside = not inside
    return inside


def dist_to_seg(px: float, py: float, ax: float, ay: float,
                bx: float, by: float) -> float:
    """점에서 선분까지의 최단거리."""
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 <= 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def dist_to_poly(x: float, y: float, poly) -> float:
    """점에서 다각형 **경계선**까지의 최단거리 (안/밖 무관, 항상 양수)."""
    n = len(poly)
    return min(dist_to_seg(x, y, poly[i][0], poly[i][1],
                           poly[(i + 1) % n][0], poly[(i + 1) % n][1])
               for i in range(n))


def side_of(lat: float, fwd: float) -> str:
    """점이 로봇의 어느 쪽인지 — 전/후/좌/우."""
    if abs(fwd) >= abs(lat):
        return "전" if fwd > 0 else "후"
    return "우" if lat > 0 else "좌"


def main() -> int:
    _force_utf8_console()
    ap = argparse.ArgumentParser(description="footprint 여유 마진 실측")
    ap.add_argument("--ip", required=True)
    ap.add_argument("--sec", type=float, default=20.0,
                    help="관찰 시간(초). 0 이면 Ctrl+C 까지 계속")
    ap.add_argument("--label", default="", help="기록 파일 이름에 붙일 꼬리표")
    ap.add_argument("--outdir", default="_logs")
    ap.add_argument("--quiet", action="store_true",
                    help="스캔별 줄을 안 찍고 요약만 낸다")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    tag = ("_" + args.label) if args.label else ""
    path = os.path.join(args.outdir, "fpmargin{}_{}.jsonl".format(tag, stamp))
    out = open(path, "w", encoding="utf-8")

    print("=" * 78)
    print(" footprint 여유 마진 실측   {}".format(args.ip))
    print("=" * 78)
    print(" 기록 : {}".format(path))
    print(" 관찰 : {}".format("무제한 (Ctrl+C 로 종료)" if args.sec <= 0
                              else "{:.0f}초".format(args.sec)))
    print()

    try:
        ws = create_connection("ws://{}:8090/ws/v2/topics".format(args.ip), timeout=5)
    except Exception as e:
        print("[중단] 로봇에 붙지 못했다: {}".format(e))
        return 1
    for t in ("/tracked_pose", "/scan_matched_points2", "/robot_model"):
        ws.send(json.dumps({"enable_topic": t}))
    ws.settimeout(5)

    pose = None
    poly = None
    fp_front = fp_half = None
    laden = None

    n_scan = 0
    n_noinside = 0
    best = None            # 가장 빡빡했던 순간 (마진, lat, fwd, 방향, 시각)
    per_side = {}          # 방향별 최소 마진
    outside_min = None     # 몸 밖 최근접 점
    t0 = time.time()

    try:
        while True:
            if args.sec > 0 and time.time() - t0 >= args.sec:
                break
            try:
                msg = json.loads(ws.recv())
            except Exception:
                continue
            topic = msg.get("topic")

            if topic == "/tracked_pose" and msg.get("pos"):
                pose = (float(msg["pos"][0]), float(msg["pos"][1]),
                        float(msg.get("ori", 0.0)))
                continue

            if topic == "/robot_model":
                fp = msg.get("footprint") or []
                if fp:
                    new = [(float(p[0]), float(p[1])) for p in fp]
                    front = max(p[1] for p in new)
                    half = max(abs(p[0]) for p in new)
                    was = laden
                    laden = half > 0.35           # 0.23(공차) vs 0.475(적재)
                    if poly is None or abs(front - (fp_front or 0)) > 0.02 \
                            or abs(half - (fp_half or 0)) > 0.02:
                        poly, fp_front, fp_half = new, front, half
                        print("[footprint] {} — 앞끝 {:.3f} / 뒤끝 {:.3f} / 반폭 {:.3f}"
                              "  (폭 {:.2f})".format(
                                  "랙 적재" if laden else "공차",
                                  front, abs(min(p[1] for p in new)), half, half * 2))
                        if was is not None and was != laden:
                            print("           ↑ 적재 상태가 바뀌었다 — 통계를 이어서 낸다")
                continue

            if topic != "/scan_matched_points2":
                continue
            if pose is None or poly is None:
                continue

            pts = msg.get("points") or msg.get("data") or []
            c, s = math.cos(pose[2]), math.sin(pose[2])
            inside = []       # (경계까지 여유, lat, fwd)
            outside = []      # (경계까지 거리, lat, fwd)
            for q in pts:
                if isinstance(q, dict):
                    qx, qy = q.get("x"), q.get("y")
                elif isinstance(q, (list, tuple)) and len(q) >= 2:
                    qx, qy = q[0], q[1]
                else:
                    continue
                if qx is None or qy is None:
                    continue
                dx, dy = float(qx) - pose[0], float(qy) - pose[1]
                fwd = dx * c + dy * s          # 로봇 Y (전방)
                lat = -dx * s + dy * c         # 로봇 X (우측 +)
                if abs(fwd) > NEAR_BOX_M or abs(lat) > NEAR_BOX_M:
                    continue                   # 몸 주변만 본다
                d = dist_to_poly(lat, fwd, poly)
                if point_in_poly(lat, fwd, poly):
                    inside.append((d, lat, fwd))
                elif d <= OUTSIDE_NEAR_M:
                    outside.append((d, lat, fwd))
                else:
                    if outside_min is None or d < outside_min[0]:
                        outside_min = (d, lat, fwd)

            n_scan += 1
            rec = {"t": round(time.time() - t0, 2), "laden": laden,
                   "n_in": len(inside), "n_out_near": len(outside)}

            if not inside:
                n_noinside += 1
                rec["margin"] = None
            else:
                m, lat, fwd = min(inside, key=lambda x: x[0])
                sd = side_of(lat, fwd)
                rec.update({"margin": round(m, 4), "lat": round(lat, 3),
                            "fwd": round(fwd, 3), "side": sd})
                if best is None or m < best[0]:
                    best = (m, lat, fwd, sd, rec["t"])
                if sd not in per_side or m < per_side[sd][0]:
                    per_side[sd] = (m, lat, fwd)
                if not args.quiet:
                    warn = "  ← 타이트!" if m < TIGHT_WARN_M else ""
                    print("  [{:>4}] 내부 {:>3}개 | 최소 여유 {:>6.1f} cm ({}쪽, "
                          "x{:+.3f} y{:+.3f}){}".format(
                              n_scan, len(inside), m * 100, sd, lat, fwd, warn))
            if outside and not args.quiet:
                d, lat, fwd = min(outside, key=lambda x: x[0])
                print("          몸 밖 {:>2}개 — 가장 가까운 것 {:.1f} cm "
                      "({}쪽)".format(len(outside), d * 100, side_of(lat, fwd)))
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
    except KeyboardInterrupt:
        print("\n(중단됨)")
    finally:
        try:
            ws.close()
        except Exception:
            pass
        out.close()

    # ── 요약 ──
    print()
    print("#" * 78)
    print("# 결과")
    print("#" * 78)
    print("  스캔 {}회 / 관찰 {:.1f}초".format(n_scan, time.time() - t0))
    if fp_half is not None:
        print("  풋프린트 : {} — 앞끝 {:.3f} m / 반폭 {:.3f} m".format(
            "랙 적재" if laden else "공차", fp_front, fp_half))
    print()

    if n_scan == 0:
        print("  [판정 불가] 스캔을 한 건도 못 받았다. 로봇 연결을 확인할 것.")
        return 1

    if best is None:
        print("  ★ 판정 — 풋프린트 **안에 찍힌 라이다 점이 한 번도 없었다.**")
        print("     라이다 평면에 자기 몸(랙 다리)이 아예 안 걸린다는 뜻이다.")
        print("     → '로봇이 자기 랙을 장애물로 본다' 는 가설은 **성립하지 않는다.**")
        print("        멈칫거림의 원인은 다른 데 있다.")
        print()
        print("     ※ 랙을 들고 있었는지 확인할 것 — 공차 상태로 쟀으면 당연한 결과다.")
        return 0

    print("  ── 가장 빡빡했던 순간 ──")
    m, lat, fwd, sd, tt = best
    print("     여유 {:.1f} cm   ({}쪽, 로봇좌표 x{:+.3f} y{:+.3f}, {:.1f}초)".format(
        m * 100, sd, lat, fwd, tt))
    print()
    print("  ── 방향별 최소 여유 ──")
    for sd2 in ("전", "후", "좌", "우"):
        if sd2 in per_side:
            v = per_side[sd2]
            print("     {}쪽 : {:>6.1f} cm   (x{:+.3f} y{:+.3f})".format(
                sd2, v[0] * 100, v[1], v[2]))
        else:
            print("     {}쪽 : (점 없음)".format(sd2))
    if outside_min:
        print()
        print("  ── 몸 밖 최근접 (장애물 후보) ──")
        print("     {:.2f} m  ({}쪽)".format(
            outside_min[0], side_of(outside_min[1], outside_min[2])))
    if n_noinside:
        print()
        print("  ※ 내부 점이 없던 스캔 {}회 ({:.0f}%) — 라이다가 자기 몸을 "
              "놓친 순간이다.".format(n_noinside, 100.0 * n_noinside / n_scan))

    print()
    print("  ── 판정 ──")
    if m < TIGHT_WARN_M:
        print("     ★ 타이트하다. 최소 여유 {:.1f} cm 는 위치추정·라이다 "
              "산포(2~3 cm)로도".format(m * 100))
        print("        넘나들 수 있다. 자기 랙이 장애물로 잡힐 여지가 있다.")
        print("        → rack.specs margin 을 키우거나 "
              "robot.footprint_expansion 을 올릴 것.")
    elif m < 0.09:
        print("     여유 {:.1f} cm — 계산상 예상(9.25 cm)보다 좁다. 랙이 조금 "
              "틀어져".format(m * 100))
        print("        있거나 캐스터가 다리 밖으로 나와 있을 수 있다.")
    else:
        print("     여유 {:.1f} cm — 넉넉하다. 자기 랙이 장애물로 잡힐 "
              "가능성은 낮다.".format(m * 100))
        print("        → 멈칫거림의 원인은 **풋프린트가 아니다.** 다른 쪽을 봐야 한다.")
    print()
    print("  기록: {}".format(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
