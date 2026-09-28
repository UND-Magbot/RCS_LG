# -*- coding: utf-8 -*-
"""안전거리 반복 시험 — 같은 구간을 N회 자동 왕복하며 편차를 잡는다.

왜 반복이 필요한가 (2026-09-15)
  2회 시험에서 **판정 거리는 안정적인데 최종 정지 위치가 2배 차이**났다.
      5회차  RED 임계 0.995 m → 정지 0.699 m   (임계 통과 후 0.296 m 전진)
      6회차  RED 임계 0.974 m → 정지 0.385 m   (임계 통과 후 0.566 m 전진)
  원인은 제동 성능이 아니다. **RED 가 확정되기까지 걸리는 시간**이 회차마다 다르고,
  그 확인 구간에서 로봇이 0.10 m/s 로 계속 다가가기 때문이다.
  → 몇 회를 돌려 **분포**를 봐야 "최악 몇 cm" 를 낼 수 있다.

한 회차 흐름
  1) (2회차부터) 시작 위치로 복귀 — standard 이동, 원래 자세까지 복원
  2) 속도 설정 → along_given_route(detour_tolerance=0) 직선 주행
  3) 정지 감지 → 그 회차 지표 산출
  4) 다음 회차

안전
  · 앞끝 `--abort` m 안으로 들어오면 즉시 이동 취소 (회차마다 동작)
  · 복귀 실패·연속 이상 시 중단
  · 로봇 파라미터(속도)만 건드리고 **서버 설정은 손대지 않는다**

쓰는 법
  BackEnd\\venv\\Scripts\\python.exe scripts/safety_repeat_test.py \
      --ip 192.168.30.130 --runs 10 --speed 0.5
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

S = {
    "pose": None, "front": 0.379, "half": 0.23,
    "edge": None, "raw": None, "lat": None, "pt": None,
    "move_state": None, "remaining": None, "lag": None, "n_cand": 0,
    "stop": False, "abort": False, "recording": False,
    # ── /motion_metrics (10 Hz) — 로봇이 보고하는 실제 속도·가속도 ──
    #   포즈(1 Hz)를 미분해서 속도를 얻으면 감속 구간(0.15초)을 아예 못 본다.
    #   이 토픽은 가속도를 직접 준다. 가감속 부드러움 판정의 근거가 된다.
    "v": None, "acc": None, "w": None,
}
rows = []          # 현재 회차 기록
mot_rows = []      # 현재 회차의 /motion_metrics 기록 (10 Hz)
fmot = None        # 모션 전용 JSONL 파일 핸들
lock = threading.Lock()


def monitor(ip, band, near_min, yellow, abort_m, fout):
    try:
        ws = create_connection(f"ws://{ip}:8090/ws/v2/topics", timeout=10)
        for t in ("/tracked_pose", "/scan_matched_points2", "/robot_model",
                  "/planning_state", "/motion_metrics"):
            ws.send(json.dumps({"enable_topic": t}))
            time.sleep(0.03)
        ws.settimeout(2.0)
    except Exception as e:
        print(f"WS 접속 실패: {e}", flush=True)
        S["stop"] = True
        return

    while not S["stop"]:
        try:
            m = json.loads(ws.recv())
        except Exception:
            continue
        tp = m.get("topic")
        if tp == "/tracked_pose":
            with lock:
                S["pose"] = (m["pos"][0], m["pos"][1], m["ori"])
            continue
        if tp == "/robot_model":
            fp = m.get("footprint") or []
            if fp:
                with lock:
                    S["front"] = self_front_extent(fp)
                    S["half"] = self_half_width(fp)
            continue
        if tp == "/planning_state":
            with lock:
                S["move_state"] = m.get("move_state")
                S["remaining"] = m.get("remaining_distance")
            continue
        if tp == "/motion_metrics":
            # 10 Hz. 스캔 행과 따로 **전량** 기록한다 — 감속은 0.15초 만에 끝나서
            # 1.86 Hz 스캔 행에 얹으면 사라진다.
            v = m.get("linear_velocity")
            a = m.get("linear_acc")
            w = m.get("angular_velocity")
            with lock:
                S["v"], S["acc"], S["w"] = v, a, w
                rec_on = S["recording"]
            if rec_on and fmot is not None:
                r = {"t": time.time(), "v": v, "acc": a, "w": w}
                mot_rows.append(r)
                fmot.write(json.dumps(r) + "\n")
            continue
        if tp != "/scan_matched_points2":
            continue

        with lock:
            pose, front, half = S["pose"], S["front"], S["half"]
        if pose is None:
            continue

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
            S["edge"], S["raw"] = edge, raw
            S["lat"] = None if hit is None else hit[1]
            S["pt"] = None if hit is None else (hit[2], hit[3])
            S["lag"], S["n_cand"] = lag, len(cands)
            ms, rem = S["move_state"], S["remaining"]
            rec_on = S["recording"]
            v_now, a_now = S["v"], S["acc"]

        if rec_on:
            rec = {"t": time.time(), "x": pose[0], "y": pose[1], "ori": pose[2],
                   "edge": edge, "raw": raw, "front": front, "half": half,
                   "lat": None if hit is None else hit[1],
                   "pt": None if hit is None else [hit[2], hit[3]],
                   "move_state": ms, "remaining": rem, "n_pts": len(pts),
                   "lag": lag, "n_cand": len(cands),
                   "v": v_now, "acc": a_now}
            rows.append(rec)
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fout.flush()

        # 비상취소 — 회차마다 재무장된다
        if rec_on and edge is not None and edge < abort_m and not S["abort"]:
            S["abort"] = True
            print(f"    *** 비상취소 — 앞끝 {edge:.3f} m < {abort_m} ***", flush=True)
            try:
                requests.patch(f"http://{ip}:8090/chassis/moves/current",
                               json={"state": "cancelled"}, timeout=5)
            except Exception as e:
                print(f"    취소 실패: {e}", flush=True)
    try:
        ws.close()
    except Exception:
        pass


def set_speed(ip, v):
    try:
        requests.post(f"http://{ip}:8090/robot-params",
                      json={"/wheel_control/max_forward_velocity": float(v)},
                      timeout=5)
        return True
    except Exception:
        return False


def cancel(ip):
    try:
        requests.patch(f"http://{ip}:8090/chassis/moves/current",
                       json={"state": "cancelled"}, timeout=5)
    except Exception:
        pass


def wait_still(sec=3.0, timeout=60.0, tag=""):
    """`sec` 초 이상 제자리면 True. 이동 종료 상태여도 True."""
    t0 = time.time()
    still_since = None
    last = None
    while time.time() - t0 < timeout:
        time.sleep(0.3)
        with lock:
            p, ms = S["pose"], S["move_state"]
        if p is None:
            continue
        if ms in ("succeeded", "failed", "cancelled"):
            return True, ms
        if last is not None:
            if math.hypot(p[0] - last[0], p[1] - last[1]) < 0.01:
                if still_since is None:
                    still_since = time.time()
                elif time.time() - still_since > sec:
                    return True, "still"
            else:
                still_since = None
        last = p
    return False, "timeout"


def go_back(ip, sx, sy, sori, speed, timeout=70.0):
    """시작 위치·자세로 복귀. 안전존이 걸어둔 속도 0 을 먼저 풀어준다."""
    set_speed(ip, speed)          # RED 로 0 이 걸려 있으면 못 움직인다
    time.sleep(0.4)
    try:
        r = requests.post(f"http://{ip}:8090/chassis/moves",
                          json={"creator": "safety_repeat", "type": "standard",
                                "target_x": sx, "target_y": sy, "target_ori": sori},
                          timeout=10)
        r.raise_for_status()
    except Exception as e:
        return False, f"복귀 명령 실패: {e}"
    t0 = time.time()
    arrived = False
    d = 9.0
    while time.time() - t0 < timeout:
        time.sleep(0.5)
        set_speed(ip, speed)      # 안전존이 도중에 0 으로 덮어써도 계속 풀어준다
        with lock:
            p, ms = S["pose"], S["move_state"]
        if p is not None:
            d = math.hypot(p[0] - sx, p[1] - sy)
            if d < 0.15:
                arrived = True
                break
        if ms in ("succeeded",):
            arrived = True
            break
        if ms in ("failed", "cancelled"):
            return False, f"복귀 실패 (move_state={ms})"
    if not arrived:
        return False, "복귀 타임아웃"

    # ★ 위치만 맞고 자세가 틀어지는 일이 있다 (2026-09-15 실측: 목표 0.560 → 실제
    #   -0.270, 47° 차이). 그대로 출발하면 엉뚱한 방향으로 직진한다.
    #   제자리 회전으로 시작 자세까지 복원한다.
    for _ in range(2):
        with lock:
            p = S["pose"]
        if p is None:
            break
        diff = math.degrees(math.atan2(math.sin(sori - p[2]),
                                       math.cos(sori - p[2])))
        if abs(diff) < 8.0:
            return True, f"복귀 완료 (위치오차 {d:.3f} m, 자세오차 {diff:+.1f}°)"
        try:
            requests.post(f"http://{ip}:8090/chassis/moves",
                          json={"creator": "safety_repeat", "type": "standard",
                                "target_x": p[0], "target_y": p[1],
                                "target_ori": sori}, timeout=10)
        except Exception as e:
            return False, f"자세 정렬 명령 실패: {e}"
        t1 = time.time()
        while time.time() - t1 < 40:
            time.sleep(0.4)
            set_speed(ip, speed)
            with lock:
                p2, ms2 = S["pose"], S["move_state"]
            if p2 is not None:
                left = math.degrees(math.atan2(math.sin(sori - p2[2]),
                                               math.cos(sori - p2[2])))
                if abs(left) < 8.0:
                    break
            if ms2 in ("succeeded", "failed", "cancelled"):
                break
    with lock:
        p = S["pose"]
    left = 0.0 if p is None else math.degrees(
        math.atan2(math.sin(sori - p[2]), math.cos(sori - p[2])))
    if abs(left) >= 15.0:
        return False, f"자세 정렬 실패 ({left:+.1f}° 남음)"
    return True, f"복귀 완료 (위치오차 {d:.3f} m, 자세오차 {left:+.1f}°)"


def analyze(run_no, sx, sy, yellow_m, red_m):
    """이번 회차 지표. rows 는 이 회차 것만 들어 있다."""
    if not rows:
        return None
    e = [r for r in rows if r["edge"] is not None]
    if not e:
        return {"run": run_no, "error": "밴드 안에 점이 한 번도 안 잡힘"}
    moved = math.hypot(rows[-1]["x"] - sx, rows[-1]["y"] - sy)

    def first_below(th):
        for r in e:
            if r["edge"] <= th:
                return r
        return None

    y = first_below(yellow_m)
    rr = first_below(red_m)
    final = e[-1]["edge"]
    lags = [r["lag"] for r in rows if r.get("lag") is not None]
    nc = [r["n_cand"] for r in rows if r.get("n_cand") is not None]
    out = {
        "run": run_no,
        "start_edge": e[0]["edge"],
        "yellow_at": None if y is None else round(y["edge"], 3),
        "red_at": None if rr is None else round(rr["edge"], 3),
        "final": round(final, 3),
        "moved": round(moved, 3),
        "after_red": None if rr is None else round(rr["edge"] - final, 3),
        "lag_span": None if not lags else round((max(lags) - min(lags)) * 1000),
        "cand_avg": None if not nc else round(statistics.mean(nc), 1),
        "aborted": S["abort"],
        "samples": len(rows),
    }
    out.update(motion_summary(mot_rows))
    return out


def motion_summary(mrows) -> dict:
    """`/motion_metrics`(10 Hz) 로 가감속이 얼마나 거친지 잰다.

    ★ 이 지표가 '멈칫' 의 정량 척도다. 사람은 **가속도의 크기**를 느낀다.
      로봇 설정상 가속은 0.3 m/s², 감속은 2.0 m/s² 까지 허용돼 있어
      감속 쪽이 7배 거칠다. 그걸 숫자로 확인하려는 것이다.

      v_max        그 회차 최고 속도
      decel_peak   가장 강했던 감속(음수 가속도의 절댓값). 클수록 덜컹인다
      decel_p95    상위 5% 감속. 한 번 튄 값에 휘둘리지 않게 같이 본다
      accel_peak   가장 강했던 가속
      decel_over_1 감속이 1.0 m/s² 를 넘은 샘플 수 — 체감 덜컹 횟수의 대용
      n_mot        이 회차에 받은 10 Hz 샘플 수
    """
    vs = [r["v"] for r in mrows if r.get("v") is not None]
    ac = [r["acc"] for r in mrows if r.get("acc") is not None]
    if not ac:
        return {"n_mot": 0}
    dec = [-x for x in ac if x < 0]          # 감속만 (양수로 뒤집음)
    acc = [x for x in ac if x > 0]
    dec_sorted = sorted(dec)
    def p95(xs):
        return None if not xs else round(xs[min(len(xs) - 1, int(len(xs) * 0.95))], 2)
    return {
        "v_max": None if not vs else round(max(vs), 3),
        "decel_peak": None if not dec else round(max(dec), 2),
        "decel_p95": p95(dec_sorted),
        "accel_peak": None if not acc else round(max(acc), 2),
        "decel_over_1": sum(1 for x in dec if x > 1.0),
        "n_mot": len(mrows),
    }


def main():
    ap = argparse.ArgumentParser(description="안전거리 반복 시험")
    ap.add_argument("--ip", required=True)
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--dist", type=float, default=5.0)
    ap.add_argument("--speed", type=float, default=0.5)
    ap.add_argument("--yellow", type=float, default=3.0)
    ap.add_argument("--red", type=float, default=1.0)
    ap.add_argument("--band", type=float, default=0.2)
    ap.add_argument("--near", type=float, default=0.45)
    ap.add_argument("--abort", type=float, default=0.35)
    ap.add_argument("--route-step", type=float, default=1.0)
    ap.add_argument("--run-timeout", type=float, default=60.0)
    ap.add_argument("--settle", type=float, default=4.0, help="회차 사이 안정화 대기(초)")
    a = ap.parse_args()

    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(LOG_DIR, f"safety_repeat_{stamp}.jsonl")
    sum_path = os.path.join(LOG_DIR, f"safety_repeat_{stamp}_summary.json")
    mot_path = os.path.join(LOG_DIR, f"safety_repeat_{stamp}_motion.jsonl")
    fout = open(out_path, "w", encoding="utf-8")
    global fmot
    fmot = open(mot_path, "w", encoding="utf-8")

    print(f"[safety_repeat] {a.ip}  {a.runs}회 반복  속도 {a.speed} m/s")
    th = threading.Thread(target=monitor,
                          args=(a.ip, a.band, a.near, a.yellow, a.abort, fout),
                          daemon=True)
    th.start()

    # 시작 위치·속도 기록
    t0 = time.time()
    while time.time() - t0 < 15:
        with lock:
            if S["pose"] is not None and S["edge"] is not None:
                break
        time.sleep(0.2)
    with lock:
        pose, front, half, edge0 = S["pose"], S["front"], S["half"], S["edge"]
    if pose is None:
        print("포즈 수신 실패 — 위치추정 확인 필요")
        S["stop"] = True
        fout.close()
        fmot.close()
        raise SystemExit(1)
    sx, sy, sori = pose
    # ★ 속도 '원래값'을 여기서 읽으면 안 된다 (2026-09-15 실측).
    #   안전존이 서행·정지로 걸어둔 값(0.1 이나 0)을 원래값으로 잘못 기억한다.
    #   끝나고 그걸로 되돌리면 로봇이 0.1 m/s 에 묶인다. 원복하지 않고 그대로 둔다 —
    #   안전존이 CLEAR 로 풀릴 때 speed_settings 기준값으로 알아서 복구한다.
    speed_before = None

    print(f"시작 위치   ({sx:.3f}, {sy:.3f})  ori={sori:.3f}")
    print(f"로봇 크기   앞끝 {front:.3f} / 반폭 {half:.3f}  "
          f"({'적재' if half > 0.30 else '공차'})")
    print(f"출발 전 감지 앞끝 {edge0 if edge0 is None else round(edge0, 3)} m")
    print(f"주행 속도   {a.speed} m/s (끝나면 안전존이 자동 복구)")
    print(f"비상취소    앞끝 {a.abort} m\n")

    results = []
    try:
        for i in range(1, a.runs + 1):
            print(f"───── {i}/{a.runs} 회차 ─────", flush=True)
            if i > 1:
                ok, msg = go_back(a.ip, sx, sy, sori, a.speed)
                print(f"  복귀: {msg}", flush=True)
                if not ok:
                    print("  복귀 실패 — 중단합니다", flush=True)
                    break
                time.sleep(a.settle)

            with lock:
                p = S["pose"]
                S["abort"] = False
                rows.clear()
                mot_rows.clear()
            if p is None:
                print("  포즈 없음 — 중단", flush=True)
                break
            x, y, o = p
            tx = x + a.dist * math.cos(o)
            ty = y + a.dist * math.sin(o)
            n = max(2, int(a.dist / a.route_step))
            rc = []
            for k in range(n + 1):
                f = k / n
                rc += [f"{x + (tx - x) * f:.4f}", f"{y + (ty - y) * f:.4f}"]

            set_speed(a.ip, a.speed)
            time.sleep(0.3)
            with lock:
                S["recording"] = True
            try:
                r = requests.post(f"http://{a.ip}:8090/chassis/moves",
                                  json={"creator": "safety_repeat",
                                        "type": "along_given_route",
                                        "target_x": tx, "target_y": ty,
                                        "target_ori": o,
                                        "route_coordinates": ",".join(rc),
                                        "detour_tolerance": 0}, timeout=10)
                r.raise_for_status()
                mid = r.json().get("id")
            except Exception as e:
                print(f"  이동 명령 실패: {e}", flush=True)
                with lock:
                    S["recording"] = False
                break
            print(f"  출발 (move {mid})", flush=True)

            done, why = wait_still(3.0, a.run_timeout)
            with lock:
                S["recording"] = False
            cancel(a.ip)
            time.sleep(0.8)

            res = analyze(i, x, y, a.yellow, a.red)
            if res is None:
                print("  기록 없음", flush=True)
                continue
            results.append(res)
            if "error" in res:
                print(f"  ⚠ {res['error']}", flush=True)
            else:
                print(f"  출발 {res['start_edge']:.2f} → YELLOW {res['yellow_at']} "
                      f"→ RED {res['red_at']} → 정지 {res['final']} m "
                      f"(임계후 {res['after_red']} m 전진, {res['moved']} m 이동)"
                      f"{'  ※비상취소' if res['aborted'] else ''}", flush=True)
    except KeyboardInterrupt:
        print("\n(사용자 중단)", flush=True)
    finally:
        cancel(a.ip)
        with lock:
            S["recording"] = False
        time.sleep(0.5)
        if speed_before is not None:
            set_speed(a.ip, speed_before)
            print(f"\n속도 원복 → {speed_before} m/s")
        S["stop"] = True
        time.sleep(1.5)
        fout.close()
        fmot.close()

    # ── 전체 통계 ──
    ok = [r for r in results if "error" not in r]
    print(f"\n{'='*60}\n=== 전체 요약  {len(ok)}/{a.runs} 회 성공 ===")
    if ok:
        print(f"\n{'회차':>4} {'출발':>6} {'YELLOW':>7} {'RED':>6} {'정지':>6} "
              f"{'임계후전진':>9} {'후보':>5} {'비상':>5}")
        for r in ok:
            print(f"{r['run']:>4} {r['start_edge']:>6.2f} {str(r['yellow_at']):>7} "
                  f"{str(r['red_at']):>6} {r['final']:>6.3f} "
                  f"{str(r['after_red']):>9} {str(r['cand_avg']):>5} "
                  f"{'Y' if r['aborted'] else '-':>5}")

        def stat(key):
            v = [r[key] for r in ok if r.get(key) is not None]
            if not v:
                return None
            return (min(v), max(v), statistics.mean(v),
                    statistics.pstdev(v) if len(v) > 1 else 0.0)

        print(f"\n{'지표':<16}{'최소':>8}{'최대':>8}{'평균':>8}{'편차':>8}")
        for key, name in (("yellow_at", "YELLOW 진입"), ("red_at", "RED 진입"),
                          ("final", "최종 정지"), ("after_red", "임계후 전진")):
            s = stat(key)
            if s:
                print(f"{name:<16}{s[0]:>8.3f}{s[1]:>8.3f}{s[2]:>8.3f}{s[3]:>8.3f}")

        # 문제 자동 판정
        print(f"\n=== 점검 ===")
        fin = [r["final"] for r in ok]
        ab = sum(1 for r in ok if r["aborted"])
        issues = []
        if min(fin) < 0.35:
            issues.append(f"최소 정지거리 {min(fin):.3f} m — 비상취소 임계({a.abort}) 이하")
        if max(fin) - min(fin) > 0.2:
            issues.append(f"정지 위치 편차 {max(fin)-min(fin):.3f} m — 재현성 부족")
        if ab:
            issues.append(f"비상취소 {ab}회 — 안전존이 제때 못 세운 경우")
        ar = [r["after_red"] for r in ok if r.get("after_red") is not None]
        if ar and max(ar) > 0.4:
            issues.append(f"RED 임계 통과 후 최대 {max(ar):.3f} m 전진 — "
                          f"확인 구간(0.10 m/s) 이 길다")
        if not issues:
            print("  특이사항 없음")
        for s in issues:
            print(f"  ⚠ {s}")

    with open(sum_path, "w", encoding="utf-8") as f:
        json.dump({"runs": results, "start": [sx, sy, sori],
                   "speed": a.speed, "front": front, "half": half},
                  f, ensure_ascii=False, indent=2)
    print(f"\n원본  {out_path}\n요약  {sum_path}")


if __name__ == "__main__":
    main()
