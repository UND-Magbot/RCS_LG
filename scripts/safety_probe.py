# -*- coding: utf-8 -*-
"""안전거리(Yellow/Red) 실측 프로브 — 로봇을 **움직이지 않고** 판정만 재현한다.

무엇을 하는가
  `safety_zone` 이 주행 중에 하는 계산을 **그대로** 돌리되, 마지막의 속도 지령
  (`POST /robot-params`)만 보내지 않는다. 로봇은 제자리에 서 있고, 사람이 장애물을
  옮기면서 "몇 m 에서 YELLOW/RED 가 뜨는가"를 줄자와 대조한다.

★ 로직을 복사하지 않는다 — `app.services.safety_zone` 에서 import 한다.
  복사하면 본 코드가 바뀔 때 조용히 어긋나서 "같은 로직"이라는 전제가 깨진다.

★ 로봇에 아무 명령도 보내지 않는다. WS 구독뿐이다.
★ 백엔드·DB 가 필요 없다. 다른 로봇이 주행 중이어도 영향이 없다.

정지 상태에서도 되는 이유 (2026-09-15 실측)
  `/scan_matched_points2` 는 로봇이 서 있어도 점이 나온다(평균 1134개). 예전에
  "정지 중엔 빈 배열"로 보였던 것은 정지 때문이 아니라 **위치추정이 inactive**
  였기 때문이다. `/slam/state` 가 positioning 이면 세워둔 채로 측정할 수 있다.

맵 벽 필터는 쓰지 않는다
  `map_image.is_wall_at()` 은 DB 가 있어야 한다. 서버 비의존이 이 도구의 요점이라
  벽 대조를 빼는 대신 **가까운 점 상위 N개를 전부 보여준다**. 벽인지 장애물인지는
  사람이 판단한다. 그래서 장애물은 벽에서 1 m 이상 떼어놓고 측정할 것.

전/후/좌/우 참고값
  우리 안전존은 **전방 밴드만** 판정한다. 좌·우·후는 로봇 자체 회피 담당이다.
  그 사실을 눈으로 확인하라고 4방향 최근접 거리를 같이 찍는다 — 옆에 물체를 둬도
  zone 이 안 바뀌는 게 **정상**이고, 이 출력이 그 근거가 된다.

쓰는 법
  # 1) 라이다 커버리지(FOV·최대거리) — 처음 한 번
  BackEnd\\venv\\Scripts\\python.exe scripts/safety_probe.py --ip 192.168.30.130 --lidar-spec --sec 30

  # 2) 조건별 측정 — 한 번 실행 = 한 조건
  ... --label 공차_정면3m --truth 3.0 --sec 15
  ... --label 적재_우0.4m --truth 2.0 --sec 15

  # 3) 물체를 옮겨가며 계속 보기 (Ctrl+C 로 종료)
  ... --sec 0

  # 4) 랙 규격만 바꿔서 계산해보기 (실물 교체 없이)
  ... --label LG2계산 --force-front 0.353 --force-half 0.400
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from collections import Counter

# stdout 인코딩 — Windows cp949 에서 한글·기호가 깨진다
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "BackEnd"))

try:
    from websocket import create_connection
except ImportError:
    print("websocket-client 가 없습니다. BackEnd\\venv\\Scripts\\python.exe 로 실행하세요.")
    raise SystemExit(1)

# 판정 로직은 전부 여기서 가져온다 (복사 금지)
from app.services.safety_zone import (  # noqa: E402
    CLEAR, RED, YELLOW,
    HYSTERESIS_M, SELF_CLEARANCE_M,
    candidates_in_band, decide_zone, self_front_extent, self_half_width,
    slow_speed_for,
)

LOG_DIR = os.path.join(_ROOT, "_logs")


def _fmt(v, nd=3, dash="—"):
    return dash if v is None else f"{v:.{nd}f}"


def _xy(q):
    """점 하나에서 (x, y). safety_zone 의 파싱과 같은 형태를 받는다."""
    if isinstance(q, dict):
        return q.get("x"), q.get("y")
    if isinstance(q, (list, tuple)) and len(q) >= 2:
        return q[0], q[1]
    return None, None


def around(points, px, py, ori):
    """전/후/좌/우 4방향 최근접 거리(로봇 중심 기준, 45도 사분면).

    안전존 판정과 무관한 **참고값**이다.
    """
    c, s = math.cos(ori), math.sin(ori)
    best = {"전": None, "후": None, "좌": None, "우": None}
    for q in points:
        qx, qy = _xy(q)
        if qx is None or qy is None:
            continue
        dx, dy = qx - px, qy - py
        fwd = dx * c + dy * s
        lat = -dx * s + dy * c
        d = math.hypot(fwd, lat)
        if d < 0.05:
            continue
        if fwd >= abs(lat):
            key = "전"
        elif -fwd >= abs(lat):
            key = "후"
        elif lat > 0:
            key = "좌"
        else:
            key = "우"
        if best[key] is None or d < best[key]:
            best[key] = d
    return best


def lidar_stats(points, px, py, ori):
    """점군의 반경·각도. 차체가 가린 실제 FOV 를 여기서 역산한다."""
    c, s = math.cos(ori), math.sin(ori)
    rads, degs = [], []
    for q in points:
        qx, qy = _xy(q)
        if qx is None or qy is None:
            continue
        dx, dy = qx - px, qy - py
        fwd = dx * c + dy * s
        lat = -dx * s + dy * c
        r = math.hypot(fwd, lat)
        if r < 0.05:
            continue
        rads.append(r)
        degs.append(math.degrees(math.atan2(lat, fwd)))   # 전방 0, 좌 +, 우 -
    return rads, degs


def main():
    ap = argparse.ArgumentParser(description="안전거리 실측 프로브 (읽기 전용)")
    ap.add_argument("--ip", required=True, help="로봇 IP")
    ap.add_argument("--sec", type=float, default=15.0, help="관찰 시간(초), 0=무한")
    ap.add_argument("--label", default="", help="이 측정의 이름")
    ap.add_argument("--truth", type=float, default=None, help="줄자 실측 거리(m)")
    ap.add_argument("--yellow", type=float, default=3.0, help="YELLOW 임계(앞끝 기준)")
    ap.add_argument("--red", type=float, default=1.0, help="RED 임계(앞끝 기준)")
    ap.add_argument("--band", type=float, default=0.2, help="밴드 반폭 하한(서버 설정값)")
    ap.add_argument("--near", type=float, default=0.45, help="근접 무시 하한")
    ap.add_argument("--top", type=int, default=3, help="표시할 상위 점 개수")
    ap.add_argument("--force-front", type=float, default=None, help="앞끝을 이 값으로 강제")
    ap.add_argument("--force-half", type=float, default=None, help="반폭을 이 값으로 강제")
    ap.add_argument("--lidar-spec", action="store_true", help="라이다 커버리지 측정 모드")
    ap.add_argument("--quiet", action="store_true", help="매 스캔 줄 출력 생략")
    a = ap.parse_args()

    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    tag = (a.label or ("lidar" if a.lidar_spec else "probe")).replace(" ", "_")
    out_path = os.path.join(LOG_DIR, f"safety_probe_{tag}_{stamp}.jsonl")

    print(f"[safety_probe] {a.ip}   label={a.label or '(없음)'}")
    print("★ 읽기 전용 — 로봇에 어떤 명령도 보내지 않습니다.\n")

    try:
        ws = create_connection(f"ws://{a.ip}:8090/ws/v2/topics", timeout=10)
    except Exception as e:
        print(f"WS 접속 실패: {e}")
        raise SystemExit(1)
    for t in ("/tracked_pose", "/scan_matched_points2", "/robot_model", "/slam/state"):
        ws.send(json.dumps({"enable_topic": t}))
        time.sleep(0.03)
    ws.settimeout(3.0)

    # 로봇 단독 기본값 — /robot_model 이 오면 갱신된다 (safety_zone 과 동일)
    front, half = 0.379, 0.23
    got_model = False
    pose = None
    zone = CLEAR
    slow_now = 0.0
    slam_state = None
    warned_slam = False

    rows = []
    edges = []
    zone_count = Counter()
    npts = []
    all_r, all_deg = [], []
    header_done = False

    t0 = time.time()
    fout = open(out_path, "w", encoding="utf-8")
    try:
        while a.sec <= 0 or (time.time() - t0) < a.sec:
            try:
                msg = json.loads(ws.recv())
            except Exception:
                continue
            topic = msg.get("topic")

            if topic == "/slam/state":
                slam_state = msg.get("state")
                if slam_state != "positioning" and not warned_slam:
                    print(f"⚠ slam state={slam_state} (positioning 아님) — "
                          f"점군이 안 나올 수 있습니다. 위치추정을 먼저 잡으세요.")
                    warned_slam = True
                continue

            if topic == "/tracked_pose":
                pose = (msg["pos"][0], msg["pos"][1], msg["ori"])
                continue

            if topic == "/robot_model":
                fp = msg.get("footprint") or []
                if fp:
                    front, half = self_front_extent(fp), self_half_width(fp)
                    got_model = True
                continue

            if topic != "/scan_matched_points2":
                continue
            if pose is None:
                continue

            pts = msg.get("points") or msg.get("data") or []
            npts.append(len(pts))

            # 강제 치수 — 실물 교체 없이 다른 랙 규격을 계산해보는 용도
            use_front = a.force_front if a.force_front is not None else front
            use_half = a.force_half if a.force_half is not None else half

            near = max(a.near, use_front + SELF_CLEARANCE_M)
            band_half = max(a.band, use_half)
            far = a.yellow + use_front + HYSTERESIS_M

            if not header_done:
                laden = "적재" if use_half > 0.30 else "공차"
                if a.force_front is not None or a.force_half is not None:
                    src = "강제값"
                elif got_model:
                    src = "/robot_model 실측"
                else:
                    src = "기본값(모델 미수신)"
                print(f"로봇 크기({src})  앞끝 {use_front:.3f} m / 반폭 {use_half:.3f} m  → {laden}")
                print(f"판정 밴드        전방 {near:.3f} ~ {far:.3f} m,  좌우 ±{band_half:.3f} m")
                print(f"임계값           YELLOW {a.yellow} / RED {a.red} / near_min {a.near}")
                print("⚠ 맵 벽 필터 미사용 — 벽도 장애물로 잡힙니다. 상위 점으로 판단하세요.")
                if a.label:
                    label_laden = ("적재" in a.label) or ("랙" in a.label) or ("대차" in a.label)
                    if label_laden != (laden == "적재"):
                        print(f"⚠ label('{a.label}')과 footprint 판정({laden})이 어긋납니다. "
                              f"/robot_model 은 0.08Hz 라 잭업 직후엔 옛 값일 수 있습니다.")
                print()
                if not a.quiet and not a.lidar_spec:
                    print("시각       zone    앞끝    중심    좌우  점(맵좌표)       "
                          "| 전/후/좌/우(중심)      상위")
                header_done = True

            if a.lidar_spec:
                r, d = lidar_stats(pts, pose[0], pose[1], pose[2])
                all_r.extend(r)
                all_deg.extend(d)
                fout.write(json.dumps({"t": time.time(), "n": len(pts),
                                       "rmax": max(r) if r else None},
                                      ensure_ascii=False) + "\n")
                continue

            cands = candidates_in_band(pts, pose[0], pose[1], pose[2],
                                       band_half, near, far)
            hit = cands[0] if cands else None
            raw = None if hit is None else hit[0]
            edge = None if raw is None else max(0.0, raw - use_front)

            prev = zone
            zone = decide_zone(zone, edge, "", a.red, a.yellow)
            if zone == YELLOW:
                slow_now = slow_speed_for(edge, slow_now, 0.3)
            elif zone == CLEAR:
                slow_now = 0.0

            ar = around(pts, pose[0], pose[1], pose[2])
            zone_count[zone] += 1
            if edge is not None:
                edges.append(edge)

            rec = {"t": time.time(), "zone": zone, "edge": edge, "raw": raw,
                   "lat": None if hit is None else hit[1],
                   "pt": None if hit is None else [hit[2], hit[3]],
                   "front": use_front, "half": use_half, "band_half": band_half,
                   "slow": slow_now if zone == YELLOW else (0.0 if zone == RED else None),
                   "around": ar, "n_pts": len(pts),
                   "top": [round(c[0] - use_front, 3) for c in cands[:a.top]]}
            rows.append(rec)
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")

            if not a.quiet:
                mark = "★" if (zone == RED and prev != RED) else " "
                ptxt = "—" if hit is None else f"({hit[2]:.2f},{hit[3]:.2f})"
                tops = " / ".join(_fmt(x, 2) for x in rec["top"]) or "—"
                lat_s = _fmt(None if hit is None else hit[1], 2, "  — ")
                print(f"{time.strftime('%H:%M:%S')}{mark}{zone:7s} "
                      f"{_fmt(edge, 3, '  —  ')} {_fmt(raw, 3, '  —  ')} "
                      f"{lat_s:>6s} {ptxt:16s}| "
                      f"{_fmt(ar['전'], 2)} {_fmt(ar['후'], 2)} "
                      f"{_fmt(ar['좌'], 2)} {_fmt(ar['우'], 2)}   {tops}")
    except KeyboardInterrupt:
        print("\n(중단)")
    finally:
        fout.close()
        try:
            ws.close()
        except Exception:
            pass

    el = time.time() - t0
    print(f"\n=== 요약  label={a.label or '(없음)'}   {el:.1f}초 ===")
    if slam_state and slam_state != "positioning":
        print(f"⚠ slam state={slam_state} — 측정이 유효하지 않을 수 있습니다.")

    if a.lidar_spec:
        if not all_r:
            print("점군을 받지 못했습니다. /slam/state 가 positioning 인지 확인하세요.")
            print(f"저장  {out_path}")
            return
        print(f"점군       {len(npts)}스캔, 평균 {sum(npts)/len(npts):.0f}개/스캔")
        print(f"최대 반경  {max(all_r):.2f} m   (카탈로그 LDS-50C-E: 40 m @90%, 15 m @10%)")
        print(f"최소 반경  {min(all_r):.2f} m   (카탈로그 사각 0.1 m)")
        print("\n각도 분포 (전방 0°, 좌 +, 우 −) — 차체가 가린 실제 FOV:")
        bins = {}
        for d, r in zip(all_deg, all_r):
            b = int(math.floor(d / 15.0)) * 15
            e = bins.setdefault(b, [0, 0.0])
            e[0] += 1
            e[1] = max(e[1], r)
        total = len(all_deg)
        empty = []
        for b in range(-180, 180, 15):
            n, rmax = bins.get(b, [0, 0.0])
            if n == 0:
                empty.append(f"{b}~{b+15}")
                continue
            bar = "█" * max(1, int(40 * n / total))
            print(f"  {b:+4d}°~{b+15:+4d}°  {bar:<40s} {n:6d}점  최대 {rmax:5.2f} m")
        print("\n점이 없는 구간: " +
              ("없음 (360° 전방향 수신)" if not empty else ", ".join(empty)))
        print(f"저장  {out_path}")
        return

    if not rows:
        print("판정 데이터가 없습니다. 점군이 안 왔거나 포즈를 못 받았습니다.")
        print(f"저장  {out_path}")
        return

    print(f"스캔       {len(rows)}건   점군 평균 {sum(npts)/len(npts):.0f}개")
    if edges:
        if len(edges) > 1:
            print(f"앞끝거리   최소 {min(edges):.3f}  최대 {max(edges):.3f}  "
                  f"중앙 {statistics.median(edges):.3f}  편차 {statistics.pstdev(edges):.3f}")
        else:
            print(f"앞끝거리   {edges[0]:.3f}")
        if a.truth is not None:
            err = statistics.median(edges) - a.truth
            print(f"줄자 {a.truth:.2f} m → 오차 {err:+.3f} m ({err/a.truth*100:+.1f}%)")
    else:
        print("앞끝거리   밴드 안에 잡힌 점이 없습니다 (앞이 비었음)")
    print("존 분포    " + " / ".join(f"{k.upper()} {zone_count.get(k, 0)}"
                                     for k in (CLEAR, YELLOW, RED)))
    la = [r["around"] for r in rows]
    print("전방향 최근접(중심 기준) — 좌·우·후는 안전존 판정 대상이 아님:")
    for k in ("전", "후", "좌", "우"):
        vals = [x[k] for x in la if x.get(k) is not None]
        if vals:
            print(f"   {k}  최소 {min(vals):.2f} m   중앙 {statistics.median(vals):.2f} m")
    print(f"저장       {out_path}")


if __name__ == "__main__":
    main()
