# -*- coding: utf-8 -*-
"""안전거리 주행 시험 — 로봇을 직진시키고 YELLOW/RED 가 실제로 어디서 걸리는지 기록한다.

`safety_probe.py` 는 로봇을 세워둔 채 판정만 재현한다. 이 스크립트는 **실제로 주행**시켜
"정지 명령 후 몇 cm 더 가서 서는가"(제동거리)까지 잰다 — 프로브로는 못 재는 부분이다.

무엇을 하는가
  1) 현재 포즈를 읽어 **정면 `--dist` m 앞**을 목표로 잡는다 (장애물 너머로 잡아야
     로봇이 계속 가려 하고, 그래야 안전존이 세우는 걸 볼 수 있다)
  2) WS 로 포즈·점군·planning_state 를 기록하면서
  3) `POST /chassis/moves` 로 직진시키고
  4) 멈추면 **어디서 멈췄는지 / 장애물까지 실제 거리**를 요약한다

★ 안전장치 — 앞끝 기준 `--abort` m 안으로 들어오면 **즉시 이동을 취소**한다.
  백엔드 안전존이 RED(1.0 m)에서 세우는 게 정상이지만, 그게 실패했을 때를 대비한
  2차 방어다. 취소는 `PATCH /chassis/moves/current {"state":"cancelled"}` 로 한다.

★ 속도는 이 스크립트가 건드리지 않는다. 미리 `POST /robot-params` 로 맞춰둘 것.
  서행·정지 지령은 **백엔드 safety_zone 이** 건다 (이 스크립트는 관찰만).

쓰는 법
  BackEnd\\venv\\Scripts\\python.exe scripts/safety_drive_test.py --ip 192.168.30.130 \
      --dist 4.0 --label 1회차_공차_0.5ms --truth 3.5
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
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

import requests  # noqa: E402
from websocket import create_connection  # noqa: E402

from app.services.safety_zone import (  # noqa: E402
    HYSTERESIS_M, SELF_CLEARANCE_M,
    candidates_in_band, self_front_extent, self_half_width,
)

LOG_DIR = os.path.join(_ROOT, "_logs")

state = {
    "pose": None, "pose_t": 0.0,
    "front": 0.379, "half": 0.23,
    "edge": None, "raw": None, "lat": None, "n_pts": 0,
    "pt": None,                 # 최근접점의 맵좌표 — 조준에 쓴다
    "move_state": None, "remaining": None,
    "stop": False, "abort_fired": False,
    # 판정 파라미터. --aim-nearest 는 조준할 때만 넓게 썼다가 실제값으로 되돌린다.
    "band": 0.2, "yellow": 3.0,
}
rows = []
lock = threading.Lock()


def monitor(ip, near_min, abort_m, fout):
    """WS 구독 → 매 스캔 판정 기록. 위험하면 abort 플래그를 세운다."""
    try:
        ws = create_connection(f"ws://{ip}:8090/ws/v2/topics", timeout=10)
        for t in ("/tracked_pose", "/scan_matched_points2", "/robot_model",
                  "/planning_state"):
            ws.send(json.dumps({"enable_topic": t}))
            time.sleep(0.03)
        ws.settimeout(2.0)
    except Exception as e:
        print(f"WS 접속 실패: {e}")
        state["stop"] = True
        return

    while not state["stop"]:
        try:
            m = json.loads(ws.recv())
        except Exception:
            continue
        tp = m.get("topic")

        if tp == "/tracked_pose":
            with lock:
                state["pose"] = (m["pos"][0], m["pos"][1], m["ori"])
                state["pose_t"] = time.time()
            continue
        if tp == "/robot_model":
            fp = m.get("footprint") or []
            if fp:
                with lock:
                    state["front"] = self_front_extent(fp)
                    state["half"] = self_half_width(fp)
            continue
        if tp == "/planning_state":
            with lock:
                state["move_state"] = m.get("move_state")
                state["remaining"] = m.get("remaining_distance")
            continue
        if tp != "/scan_matched_points2":
            continue

        with lock:
            pose = state["pose"]
            front, half = state["front"], state["half"]
            band, yellow = state["band"], state["yellow"]
        if pose is None:
            continue

        # ★ 스캔이 얼마나 낡았는가 (2026-09-15 추가).
        #   `stamp` 는 로봇이 그 스캔을 만든 시각이다. `지금 - stamp` 가 커지면
        #   받는 쪽이 밀리고 있다는 뜻 — 밀린 값으로 판정하면 이미 지나간 상황을
        #   보고 서행·정지를 걸게 되고, 그게 '멈칫'으로 나타난다.
        #   프로브 lag 는 작은데 백엔드 [safety] 로그만 늦으면 백엔드 처리 문제다.
        stamp = m.get("stamp")
        lag = (time.time() - float(stamp)) if stamp else None

        pts = m.get("points") or m.get("data") or []
        near = max(near_min, front + SELF_CLEARANCE_M)
        band_half = max(band, half)
        far = yellow + front + HYSTERESIS_M
        cands = candidates_in_band(pts, pose[0], pose[1], pose[2],
                                   band_half, near, far)
        hit = cands[0] if cands else None
        raw = None if hit is None else hit[0]
        edge = None if raw is None else max(0.0, raw - front)

        with lock:
            state["edge"] = edge
            state["raw"] = raw
            state["lat"] = None if hit is None else hit[1]
            state["pt"] = None if hit is None else (hit[2], hit[3])
            state["n_pts"] = len(pts)
            ms, rem = state["move_state"], state["remaining"]

        rec = {"t": time.time(), "x": pose[0], "y": pose[1], "ori": pose[2],
               "edge": edge, "raw": raw, "front": front, "half": half,
               "lat": None if hit is None else hit[1],
               "pt": None if hit is None else [hit[2], hit[3]],
               "move_state": ms, "remaining": rem, "n_pts": len(pts),
               "stamp": stamp, "lag": lag, "band_half": band_half,
               "n_cand": len(cands)}      # 밴드 안 후보 수 — 랙 적재 시 늘어난다
        rows.append(rec)
        fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fout.flush()

        if edge is not None and edge < abort_m and not state["abort_fired"]:
            state["abort_fired"] = True
            print(f"\n*** 비상 취소 — 앞끝 {edge:.3f} m < {abort_m} m ***", flush=True)
            try:
                requests.patch(f"http://{ip}:8090/chassis/moves/current",
                               json={"state": "cancelled"}, timeout=5)
                print("    이동 취소 전송 완료", flush=True)
            except Exception as e:
                print(f"    취소 실패: {e}", flush=True)
    try:
        ws.close()
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="안전거리 주행 시험")
    ap.add_argument("--ip", required=True)
    ap.add_argument("--dist", type=float, default=4.0, help="정면 목표 거리(m)")
    ap.add_argument("--label", default="")
    ap.add_argument("--truth", type=float, default=None, help="장애물까지 줄자 실측(m)")
    ap.add_argument("--yellow", type=float, default=3.0)
    ap.add_argument("--red", type=float, default=1.0)
    ap.add_argument("--band", type=float, default=0.2)
    ap.add_argument("--near", type=float, default=0.45)
    ap.add_argument("--abort", type=float, default=0.35,
                    help="앞끝 이 거리 안으로 들어오면 이동 취소")
    ap.add_argument("--timeout", type=float, default=70.0)
    ap.add_argument("--dry-run", action="store_true", help="이동 명령 없이 관찰만")
    # 물체가 정면에 없을 때 — 넓게 훑어 찾은 뒤 그 방향으로 목표를 잡는다.
    # 로봇이 스스로 돌아서 물체를 정면에 놓으므로 물건·로봇을 손댈 필요가 없다.
    ap.add_argument("--aim-nearest", action="store_true",
                    help="가장 가까운 물체 방향으로 목표를 잡는다(자동 조준)")
    ap.add_argument("--scan-band", type=float, default=1.2, help="조준 탐색 밴드 반폭")
    ap.add_argument("--scan-range", type=float, default=6.0, help="조준 탐색 거리")
    # ── 실제 배차와 같은 조건으로 보낸다 ──────────────────────────
    # dispatch_service 는 경유지 구간에서 along_given_route + detour_tolerance=0 을 쓴다.
    # standard 로 보내면 로봇이 **장애물을 피해 돌아가서** 안전존이 개입할 틈이 없다
    # (2026-09-15 실측: 직선 이탈 0.729 m, edge 가 끝까지 None).
    ap.add_argument("--route", action="store_true",
                    help="along_given_route + detour_tolerance=0 (실제 경유지 주행과 동일)")
    ap.add_argument("--no-face", action="store_true",
                    help="출발 전 제자리 회전을 생략(기본은 실제 동작대로 수행)")
    # 속도는 **로봇 파라미터로만** 걸고 끝나면 되돌린다.
    #   서버 설정(robot_speed.json / DB)은 건드리지 않는다 — 그걸 바꾸면 실제 배차
    #   주행 속도까지 영구히 바뀐다(2026-09-15 실수).
    #   단, 안전존이 YELLOW/RED 를 **해제**할 때 `_base_speed()`(=speed_settings)로
    #   덮어쓰므로 이 값은 그때까지만 유효하다. 해제는 보통 시험이 끝난 뒤다.
    ap.add_argument("--speed", type=float, default=None,
                    help="주행 직전 max_forward_velocity 를 이 값으로. 끝나면 원복")
    ap.add_argument("--route-step", type=float, default=1.0,
                    help="route_coordinates 경로점 간격(m). 촘촘할수록 직선에 가깝다")
    a = ap.parse_args()

    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    tag = (a.label or "drive").replace(" ", "_")
    out_path = os.path.join(LOG_DIR, f"safety_drive_{tag}_{stamp}.jsonl")
    fout = open(out_path, "w", encoding="utf-8")

    print(f"[safety_drive_test] {a.ip}  label={a.label or '(없음)'}")
    # 조준 단계에서만 넓게 본다. 목표를 정한 뒤 실제 판정값으로 되돌린다.
    with lock:
        state["band"] = a.scan_band if a.aim_nearest else a.band
        state["yellow"] = a.scan_range if a.aim_nearest else a.yellow
    th = threading.Thread(target=monitor,
                          args=(a.ip, a.near, a.abort, fout),
                          daemon=True)
    th.start()

    # 포즈·크기 수신 대기
    t0 = time.time()
    while time.time() - t0 < 12:
        with lock:
            if state["pose"] is not None and state["edge"] is not None:
                break
        time.sleep(0.2)
    with lock:
        pose, front, half = state["pose"], state["front"], state["half"]
        edge0, raw0, lat0, pt0 = state["edge"], state["raw"], state["lat"], state["pt"]
    if pose is None:
        print("포즈를 못 받았습니다. 위치추정(slam) 확인 필요.")
        state["stop"] = True
        fout.close()
        raise SystemExit(1)

    x, y, o = pose
    aim = o
    if a.aim_nearest:
        if pt0 is None:
            print(f"조준 실패 — 탐색 범위(±{a.scan_band} m, {a.scan_range} m) 안에 "
                  f"물체가 없습니다.")
            state["stop"] = True
            fout.close()
            raise SystemExit(1)
        aim = math.atan2(pt0[1] - y, pt0[0] - x)
        d_obj = math.hypot(pt0[0] - x, pt0[1] - y)
        turn = math.degrees((aim - o + math.pi) % (2 * math.pi) - math.pi)
        print(f"자동 조준   물체 맵({pt0[0]:.2f}, {pt0[1]:.2f})  "
              f"중심거리 {d_obj:.3f} m  좌우 {lat0:+.2f} m")
        print(f"            로봇이 {turn:+.1f}° 회전한 뒤 직진합니다")
        # 조준이 끝났으니 실제 판정값으로 되돌린다
        with lock:
            state["band"] = a.band
            state["yellow"] = a.yellow
    tx = x + a.dist * math.cos(aim)
    ty = y + a.dist * math.sin(aim)
    print(f"현재 포즈   ({x:.3f}, {y:.3f})  ori={o:.3f}")
    print(f"로봇 크기   앞끝 {front:.3f} / 반폭 {half:.3f}  "
          f"({'적재' if half > 0.30 else '공차'})")
    print(f"출발 전 감지 앞끝 {edge0 if edge0 is None else round(edge0,3)} m "
          f"/ 중심 {raw0 if raw0 is None else round(raw0,3)} m")
    if a.truth is not None:
        print(f"줄자 실측   {a.truth} m (앞범퍼 기준)")
    print(f"목표        ({tx:.3f}, {ty:.3f})  = {'조준방향' if a.aim_nearest else '정면'} "
          f"{a.dist} m")
    print(f"판정        YELLOW {a.yellow} / RED {a.red} / 밴드 ±{max(a.band, half):.3f} m")
    print(f"비상취소    앞끝 {a.abort} m 안\n")

    if a.dry_run:
        print("--dry-run 이므로 이동 명령을 보내지 않습니다. 20초 관찰 후 종료.")
        time.sleep(20)
    else:
        # ⓪ 속도 — 로봇 파라미터만 바꾸고 원래값을 기억해둔다
        speed_before = None
        if a.speed is not None:
            try:
                rp = requests.get(f"http://{a.ip}:8090/robot-params", timeout=5).json()
                speed_before = rp.get("/wheel_control/max_forward_velocity")
                requests.post(f"http://{a.ip}:8090/robot-params",
                              json={"/wheel_control/max_forward_velocity": a.speed},
                              timeout=5)
                print(f"[0단계] 속도 {speed_before} → {a.speed} m/s "
                      f"(끝나면 {speed_before} 로 원복)")
            except Exception as e:
                print(f"    속도 설정 실패(무시하고 진행): {e}")

        # ① 실제 배차와 같이, 경로 진입 전 제자리 회전 (_face_route_start 재현).
        #    detour_tolerance=0 에서 자세가 15° 이상 틀어져 있으면 로봇이
        #    자세 교정 움직임조차 경로 이탈로 보고 그 자리에 선다(2026-09-14 실측).
        if a.route and not a.no_face:
            turn_deg = math.degrees(math.atan2(math.sin(aim - o), math.cos(aim - o)))
            if abs(turn_deg) >= 15.0:
                print(f"[1단계] 경로 진입 전 제자리 회전 {turn_deg:+.1f}° "
                      f"(FACE_ROUTE_MIN_DEG=15.0 초과)")
                try:
                    requests.post(f"http://{a.ip}:8090/chassis/moves",
                                  json={"creator": "safety_test", "type": "standard",
                                        "target_x": x, "target_y": y,
                                        "target_ori": aim}, timeout=10)
                except Exception as e:
                    print(f"    회전 명령 실패(무시하고 진행): {e}")
                for _ in range(80):          # 최대 40초
                    time.sleep(0.5)
                    with lock:
                        p, ms = state["pose"], state["move_state"]
                    if p is not None:
                        left = math.degrees(math.atan2(math.sin(aim - p[2]),
                                                       math.cos(aim - p[2])))
                        if abs(left) < 8.0:
                            print(f"    회전 완료 (남은 각도 {left:+.1f}°)")
                            break
                    if ms in ("succeeded", "failed", "cancelled"):
                        break
                else:
                    print("    회전 타임아웃 — 그대로 진행합니다")
                time.sleep(1.0)
            else:
                print(f"[1단계] 회전 불필요 ({turn_deg:+.1f}° < 15°)")

        # ② 본 이동
        body = {"creator": "safety_test", "type": "standard",
                "target_x": tx, "target_y": ty, "target_ori": aim}
        if a.route:
            # ★ 경로는 **출발점부터** 촘촘히 준다 (2026-09-15 실측으로 정정).
            #   처음엔 "중간점 + 목표점" 2점만 줬더니 로봇이 0.425 m 벗어나 장애물을
            #   피해 갔다. 조회해보니 `properties = {}` 로 route_coordinates 흔적이
            #   없었다 — 출발점이 빠져 첫 구간이 자유 주행이 된 것으로 보인다.
            #   실제 배차(`waypoint_route.plan`)도 경유지를 여러 개 준다.
            n = max(2, int(math.hypot(tx - x, ty - y) / a.route_step))
            pts_route = []
            for i in range(n + 1):
                f = i / n
                pts_route += [f"{x + (tx - x) * f:.4f}", f"{y + (ty - y) * f:.4f}"]
            body["type"] = "along_given_route"
            body["route_coordinates"] = ",".join(pts_route)
            body["detour_tolerance"] = 0
            print(f"[2단계] along_given_route  detour_tolerance=0  "
                  f"경로점 {n+1}개({a.route_step} m 간격)")
            print(f"        route_coordinates = {body['route_coordinates'][:90]}…")
        try:
            r = requests.post(f"http://{a.ip}:8090/chassis/moves",
                              json=body, timeout=10)
            r.raise_for_status()
            mid = r.json().get("id")
            print(f"이동 명령 전송 — move id={mid}  type={body['type']}\n")
        except Exception as e:
            print(f"이동 명령 실패: {e}")
            state["stop"] = True
            fout.close()
            raise SystemExit(1)

        print("시각       앞끝    중심   좌우   이동거리  move_state   남은거리")
        last_print = 0.0
        still_since = None
        t0 = time.time()
        while time.time() - t0 < a.timeout:
            time.sleep(0.25)
            with lock:
                p = state["pose"]
                e, rw, lt = state["edge"], state["raw"], state["lat"]
                ms, rem = state["move_state"], state["remaining"]
            if p is None:
                continue
            moved = math.hypot(p[0] - x, p[1] - y)
            now = time.time()
            if now - last_print >= 0.5:
                last_print = now
                print(f"{time.strftime('%H:%M:%S')}  "
                      f"{'—' if e is None else f'{e:6.3f}'} "
                      f"{'—' if rw is None else f'{rw:6.3f}'} "
                      f"{'—' if lt is None else f'{lt:+5.2f}'}  "
                      f"{moved:7.3f}  {str(ms):11s} "
                      f"{'—' if rem is None else f'{rem:.2f}'}")
            if ms in ("succeeded", "failed", "cancelled"):
                print(f"\n이동 종료 — move_state={ms}")
                break
            # 3초 이상 제자리면 멈춘 것으로 본다
            if len(rows) > 4:
                recent = rows[-8:]
                dd = math.hypot(recent[-1]["x"] - recent[0]["x"],
                                recent[-1]["y"] - recent[0]["y"])
                if dd < 0.02:
                    if still_since is None:
                        still_since = now
                    elif now - still_since > 3.0:
                        print("\n3초 이상 정지 — 안전존이 세운 것으로 보입니다.")
                        break
                else:
                    still_since = None

    time.sleep(1.0)
    state["stop"] = True
    time.sleep(1.5)
    fout.close()

    # 속도 원복 — 이 스크립트가 바꾼 흔적을 남기지 않는다
    if not a.dry_run and a.speed is not None and speed_before is not None:
        try:
            requests.post(f"http://{a.ip}:8090/robot-params",
                          json={"/wheel_control/max_forward_velocity": speed_before},
                          timeout=5)
            print(f"\n속도 원복 — {a.speed} → {speed_before} m/s")
        except Exception as e:
            print(f"\n⚠ 속도 원복 실패 — 수동 확인 필요 ({speed_before} m/s): {e}")

    # ── 요약 ──
    print(f"\n=== 요약  label={a.label or '(없음)'}  {len(rows)}샘플 ===")
    if not rows:
        print("데이터 없음")
        print(f"저장  {out_path}")
        return
    moved = math.hypot(rows[-1]["x"] - x, rows[-1]["y"] - y)
    final = rows[-1]
    print(f"이동 거리        {moved:.3f} m")
    fe = final["edge"]
    print(f"최종 앞끝 거리   {'—' if fe is None else format(fe, '.3f')} m")
    edges = [r["edge"] for r in rows if r["edge"] is not None]
    if edges:
        print(f"최근접(최소)     {min(edges):.3f} m  ← 가장 가까이 간 순간")
    # 존 전이 시점
    def first_below(th):
        for r in rows:
            if r["edge"] is not None and r["edge"] <= th:
                return r
        return None
    for name, th in (("YELLOW", a.yellow), ("RED", a.red)):
        r = first_below(th)
        if r:
            d = math.hypot(r["x"] - x, r["y"] - y)
            print(f"{name} 도달       앞끝 {r['edge']:.3f} m (출발 후 {d:.3f} m 지점, "
                  f"{time.strftime('%H:%M:%S', time.localtime(r['t']))})")
        else:
            print(f"{name} 도달       없음")
    if a.truth is not None and edges:
        print(f"줄자 {a.truth} m → 최종 정지 시 실측으로 확인해 주세요 "
              f"(라이다 기준 최근접 {min(edges):.3f} m)")
    # ── 스캔 지연 — '멈칫'의 원인 판별용 ──
    lags = [r["lag"] for r in rows if r.get("lag") is not None]
    if lags:
        print(f"\n스캔 지연(프로브)  평균 {statistics.mean(lags)*1000:.0f} ms  "
              f"최대 {max(lags)*1000:.0f} ms  마지막 {lags[-1]*1000:.0f} ms")
        if max(lags) > 1.0:
            print("  ⚠ 프로브에서도 1초 넘게 밀립니다 — 로봇 전송 지연일 수 있습니다")
        else:
            print("  → 프로브는 밀리지 않았습니다. 백엔드 [safety] 로그가 늦으면 "
                  "백엔드 처리 문제입니다")
    cands_n = [r["n_cand"] for r in rows if r.get("n_cand") is not None]
    if cands_n:
        print(f"밴드 안 후보점    평균 {statistics.mean(cands_n):.1f}개  "
              f"최대 {max(cands_n)}개  (밴드 반폭 {rows[-1].get('band_half')} m)")
    if state["abort_fired"]:
        print("⚠ 비상 취소가 발동했습니다 — 안전존이 제때 세우지 못했습니다.")
    print(f"저장             {out_path}")


if __name__ == "__main__":
    main()
