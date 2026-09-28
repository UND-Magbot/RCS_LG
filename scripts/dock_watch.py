# -*- coding: utf-8 -*-
"""충전 도킹 실시간 관찰 — "어느 위치에서부터 회전 없이 들어가는가"

로봇이 충전소로 들어가는 동안 포즈와 이동 상태를 계속 받아서,
**제자리 회전이 언제·어디서·몇 도 일어났는지**를 잡아낸다.

읽기 전용이다 — 로봇에 명령을 보내지 않는다(--send-charge 를 준 경우만 예외).

왜 필요한가
  dock_log.py 는 **끝난 뒤** 실패 사유를 보여준다. 이건 "회전했는지" 를 못 본다.
  회전 여부는 주행 중 포즈 변화를 봐야 알 수 있어서 실시간 기록이 필요하다.

쓰는 법
  1) 이 스크립트를 먼저 띄운다
        python scripts/dock_watch.py --ip 192.168.30.110 --label 3m지점
  2) 그 상태에서 충전을 건다 (콘솔/백엔드/수동 어느 쪽이든)
  3) 도킹이 끝나면 요약이 출력된다. Ctrl+C 로 종료

  충전 명령까지 이 스크립트로 보내려면 (로봇을 실제로 움직인다):
        python scripts/dock_watch.py --ip 192.168.30.110 --label 3m지점 --send-charge

판정 기준
  제자리 회전 = 연속 두 포즈 사이에서 **위치는 거의 안 변했는데 각도만 변한 것**.
  임계값은 --pos-eps / --ori-eps 로 조정한다.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import time
from datetime import datetime

import requests


def _force_utf8_console():
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_console()

ROBOT_PORT = 8090
HTTP_TIMEOUT = 10

# 제자리 회전 판정 기본값
POS_EPS_M = 0.03      # 이만큼 아래로 움직였으면 '안 움직인 것'
ORI_EPS_DEG = 1.0     # 이만큼 넘게 돌았으면 '돈 것'
# 접근(standard) 이 끝난 뒤 도킹(charge) 을 이만큼 기다린다(초)
WAIT_CHARGE_SEC = 30.0

_stop = False


def _url(ip, path):
    return "http://{}:{}{}".format(ip, ROBOT_PORT, path)


def norm_rad(a):
    return math.atan2(math.sin(a), math.cos(a))


def dd(to_a, from_a):
    """최단 각차(도)"""
    return math.degrees(norm_rad(to_a - from_a))


# ── 기준점(충전소·진입점) 읽기 ───────────────────────────────

def load_reference(ip, db_name="rcs_lg_db", db_user="root", db_pw="1234",
                   db_host="127.0.0.1"):
    """로봇의 현재 맵에 대응하는 C1 / C1-1 좌표를 찾는다.

    C1 은 로봇 오버레이에 있지만 **진입점 C1-1 은 DB 에만 있다**(로봇에 동기화
    하지 않는 규약). 그래서 로봇 오버레이의 C1 좌표로 DB 의 같은 맵을 찾아낸다.
    DB 에 못 붙어도 C1 만으로 계속 진행한다.
    """
    ref = {"C1": None, "C1-1": None, "map_name": None, "pile": None}
    try:
        cm = requests.get(_url(ip, "/chassis/current-map"), timeout=HTTP_TIMEOUT).json()
        ref["map_name"] = cm.get("map_name")
        mp = requests.get(_url(ip, "/maps/{}".format(cm.get("id"))),
                          timeout=HTTP_TIMEOUT).json()
        ov = mp.get("overlays")
        if isinstance(ov, str):
            ov = json.loads(ov)
        for f in (ov or {}).get("features", []):
            p = f.get("properties", {}) or {}
            nm = str(p.get("name", "")).strip().upper()
            c = f.get("geometry", {}).get("coordinates")
            if not c:
                continue
            # 오버레이에는 둘이 따로 있다:
            #   type 9  "C1"              충전기 본체 위치
            #   type 36 "C1 Docking Point" 로봇이 실제로 들어가 서는 자리
            # DB map_pois 의 C1 world 좌표는 **도킹 포인트** 쪽과 일치한다.
            # 거리 판단 기준도 도킹 포인트가 맞다.
            if nm == "C1 DOCKING POINT":
                ref["C1"] = (float(c[0]), float(c[1]))
            elif nm == "C1":
                ref["pile"] = (float(c[0]), float(c[1]))
        if ref["C1"] is None:
            ref["C1"] = ref["pile"]      # 도킹 포인트가 없으면 본체로 대체
    except Exception as e:
        print("  [경고] 로봇 맵에서 C1 을 못 읽었다: {}".format(e))

    # DB 에서 같은 맵의 C1 / C1-1 을 찾는다 (C1 좌표로 맵을 특정)
    try:
        import pymysql
        cn = pymysql.connect(host=db_host, user=db_user, password=db_pw,
                             database=db_name, charset="utf8mb4")
        cu = cn.cursor()
        cu.execute("SELECT map_id,name,world_x,world_y,angle FROM map_pois "
                   "WHERE name IN ('C1','C1-1','c1-1')")
        rows = cu.fetchall()

        # ★ 맵은 **로봇 등록정보** 로 고른다 (백엔드와 같은 기준).
        #   좌표로 맞추면 안 된다 — 맵끼리 C1 좌표가 3cm 밖에 차이 안 나는 경우가 있어
        #   2026-09-14 에 map 30(로봇이 쓰는 것) 대신 map 32 를 잡았다.
        best_map = None
        cu.execute("SELECT charging_id FROM robots WHERE ip_address=%s", (ip,))
        rr = cu.fetchone()
        if rr and rr[0]:
            cu.execute("SELECT map_id, world_x, world_y FROM map_pois WHERE id=%s", (rr[0],))
            cp = cu.fetchone()
            if cp:
                best_map = cp[0]
                if cp[1] is not None:
                    ref["C1"] = (float(cp[1]), float(cp[2]))   # DB 값을 정답으로 쓴다
        cn.close()
        if best_map is None and ref["C1"]:
            for mid, nm, wx, wy, ang in rows:
                if nm.upper() == "C1" and wx is not None:
                    if (abs(float(wx) - ref["C1"][0]) < 0.02
                            and abs(float(wy) - ref["C1"][1]) < 0.02):
                        best_map = mid
                        break
        if best_map is not None:
            for mid, nm, wx, wy, ang in rows:
                if mid == best_map and nm.upper() == "C1-1" and wx is not None:
                    ref["C1-1"] = (float(wx), float(wy))
            ref["map_id"] = best_map
    except Exception as e:
        print("  [참고] DB 조회 생략(백엔드 DB 미가동?): {}".format(e))
    return ref


def rel(pose, pt):
    """pose 에서 pt 까지의 거리(m)와 방위차(도). pt 가 없으면 None."""
    if not pt:
        return None
    dx, dy = pt[0] - pose[0], pt[1] - pose[1]
    dist = math.hypot(dx, dy)
    bearing = math.degrees(norm_rad(math.atan2(dy, dx) - pose[2]))
    return dist, bearing


# ── 관찰 ──────────────────────────────────────────────────────

def watch(ip, ref, label, outdir, pos_eps, ori_eps, send_charge):
    import websocket

    os.makedirs(outdir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = (label + "_") if label else ""
    path = os.path.join(outdir, "dock_{}{}.jsonl".format(tag, stamp))
    jf = open(path, "a", encoding="utf-8")

    def rec(kind, **kw):
        # "t" 는 절대시각. 호출자가 준 키를 덮지 않도록 kind/t 를 먼저 넣지 않는다.
        out = {"kind": kind, "t": round(time.time(), 3)}
        out.update(kw)
        jf.write(json.dumps(out, ensure_ascii=False) + "\n")
        jf.flush()

    rec("meta", ip=ip, label=label, ref={k: v for k, v in ref.items()})

    ws = websocket.create_connection(
        "ws://{}:{}/ws/v2/topics".format(ip, ROBOT_PORT), timeout=8)
    for t in ("/tracked_pose", "/planning_state", "/alerts"):
        ws.send(json.dumps({"enable_topic": t}))
    ws.settimeout(0.5)

    # 시작 포즈
    start = None
    t0 = time.time()
    while time.time() - t0 < 8 and start is None:
        try:
            pkt = json.loads(ws.recv())
        except Exception:
            continue
        if pkt.get("topic") == "/tracked_pose" and pkt.get("pos"):
            start = (float(pkt["pos"][0]), float(pkt["pos"][1]),
                     float(pkt.get("ori", 0.0)))
    if start is None:
        print("[실패] tracked_pose 를 못 읽었다 — 위치추정 상태를 확인하라")
        return None

    print("\n=== 시작 지점 ===")
    print("  포즈   x={:.3f}  y={:.3f}  ori={:.1f}도".format(
        start[0], start[1], math.degrees(start[2])))
    for nm in ("C1-1", "C1"):
        r = rel(start, ref.get(nm))
        if r:
            print("  {:<5} 까지  거리 {:.3f} m   방위차 {:+.1f}도".format(nm, r[0], r[1]))
    print()
    rec("start", pose=start,
        to_C1=rel(start, ref.get("C1")), to_C1_1=rel(start, ref.get("C1-1")))

    if send_charge and ref.get("C1"):
        cx, cy = ref["C1"]
        try:
            body = {"creator": "dock_watch", "type": "charge",
                    "target_x": cx, "target_y": cy, "target_ori": 0,
                    "charge_retry_count": 3}
            mid = requests.post(_url(ip, "/chassis/moves"), json=body,
                                timeout=HTTP_TIMEOUT).json().get("id")
            print("[충전 명령 전송] move id={}\n".format(mid))
            rec("charge_sent", move_id=mid, target=[cx, cy])
        except Exception as e:
            print("[실패] 충전 명령 전송 실패: {}".format(e))

    # 누적 상태
    prev = start
    path_m = 0.0          # 총 이동거리
    turn_total = 0.0      # 총 회전량(절대 누적)
    spin_runs = []        # 제자리 회전 구간 [{deg, at:(x,y), t}]
    cur_run = None
    last_state = None
    done = None
    live_ids = set()      # 움직이는 걸 실제로 본 action_id (latched 상태 걸러내기용)
    seen_moves = []       # 이번 관찰에서 끝난 이동들
    idle_since = None     # 접근이 끝난 시각 — 이때부터 charge 를 기다린다
    cur_phase = None      # 지금 진행 중인 이동 종류(standard / charge)
    back_m = 0.0          # charge 단계에서 뒤로 간 거리(후진 도킹 확인용)

    print("관찰 중... (Ctrl+C 로 종료)\n")
    try:
        while not _stop:
            # 접근이 끝났는데 charge 가 안 오면 무한정 기다리지 않는다.
            if idle_since and time.time() - idle_since > WAIT_CHARGE_SEC:
                print("  [대기 종료] 접근 완료 후 {:.0f}초 동안 도킹(charge) 이 "
                      "시작되지 않았다".format(WAIT_CHARGE_SEC))
                done = {"state": "no_charge", "fail": "charge 미시작",
                        "action_type": "standard"}
                break
            try:
                pkt = json.loads(ws.recv())
            except Exception:
                continue
            tp = pkt.get("topic")

            if tp == "/tracked_pose" and pkt.get("pos"):
                cur = (float(pkt["pos"][0]), float(pkt["pos"][1]),
                       float(pkt.get("ori", 0.0)))
                dpos = math.hypot(cur[0] - prev[0], cur[1] - prev[1])
                dori = dd(cur[2], prev[2])
                path_m += dpos
                turn_total += abs(dori)
                if cur_phase == "charge" and dpos > 0.001:
                    # 진행 방향이 기체 뒤쪽이면 후진으로 센다
                    hd = math.degrees(norm_rad(
                        math.atan2(cur[1] - prev[1], cur[0] - prev[0]) - prev[2]))
                    if abs(hd) > 120:
                        back_m += dpos
                rec("pose", x=cur[0], y=cur[1], ori=cur[2],
                    dpos=round(dpos, 4), dori=round(dori, 3))

                if dpos < pos_eps and abs(dori) > ori_eps:
                    # 제자리 회전 중.
                    # ★ 어느 단계에서 돌았는지가 핵심이다.
                    #   standard = 진입점에서 자세를 잡는 정상 회전(멀리서 돌수록 좋다)
                    #   charge   = **충전기 코앞에서 도는 것** = 우리가 없애려는 동작
                    if cur_run is None:
                        cur_run = {"deg": 0.0, "at": (round(cur[0], 3), round(cur[1], 3)),
                                   "rel_t": round(time.time() - t0, 1),
                                   "phase": cur_phase or "?"}
                    cur_run["deg"] += dori
                elif cur_run is not None:
                    if abs(cur_run["deg"]) >= ori_eps:
                        spin_runs.append(cur_run)
                        d = rel(cur, ref.get("C1"))
                        mark = "  ← ★충전기 앞에서 회전!" if cur_run.get("phase") == "charge" else ""
                        print("  ★ 제자리 회전 {:+.1f}도  [{}단계]  (C1 까지 {:.2f} m){}".format(
                            cur_run["deg"], cur_run.get("phase"),
                            d[0] if d else float("nan"), mark))
                        rec("spin", **cur_run)
                    cur_run = None
                prev = cur

            elif tp == "/planning_state":
                st = pkt.get("move_state")
                aid = pkt.get("action_id")
                atype = pkt.get("action_type")
                TERMINAL = ("succeeded", "failed", "cancelled")

                # ★ /planning_state 는 **붙는 순간 직전 이동의 마지막 상태를 그대로
                #   한 번 밀어준다**(latched). 그걸 "도킹 끝"으로 읽으면 관찰이
                #   시작하자마자 끝나버린다 — 2026-09-14 실측에서 2~5차 로그가
                #   3줄만 남고 종료된 원인이 이것이다.
                #   그래서 **움직이는 걸 한 번이라도 본 action_id** 의 종료만 인정한다.
                if st not in TERMINAL:
                    live_ids.add(aid)
                    cur_phase = atype
                    idle_since = None

                if (st, aid) != last_state:
                    print("  [이동] {} · type={} · creator={} · id={} {}".format(
                        st, atype, pkt.get("creator"), aid,
                        pkt.get("fail_reason_str") or ""))
                    rec("planning", state=st, action_type=atype,
                        creator=pkt.get("creator"),
                        fail=pkt.get("fail_reason_str"), action_id=aid,
                        stale=(st in TERMINAL and aid not in live_ids))
                    last_state = (st, aid)

                if st in TERMINAL and aid in live_ids:
                    seen_moves.append({"id": aid, "type": atype, "state": st,
                                       "fail": pkt.get("fail_reason_str")})
                    live_ids.discard(aid)
                    # ★ 충전 도킹은 **두 단계**다 — 진입점까지 standard, 그다음 charge.
                    #   standard 가 끝났다고 멈추면 **정작 도킹을 못 본다**
                    #   (2026-09-14 1차 로그가 이렇게 잘렸다).
                    #   charge 가 끝나야 진짜 끝이다. 그 전이면 다음 이동을 기다린다.
                    if atype == "charge" or st in ("failed", "cancelled"):
                        done = {"state": st, "fail": pkt.get("fail_reason_str"),
                                "action_type": atype}
                        time.sleep(2.0)
                        break
                    print("      (접근 단계 완료 — 도킹(charge) 을 계속 기다린다)")
                    idle_since = time.time()

            elif tp == "/alerts":
                al = pkt.get("alerts") or []
                if al:
                    rec("alerts", alerts=al)
    except KeyboardInterrupt:
        pass
    finally:
        if cur_run is not None and abs(cur_run["deg"]) >= ori_eps:
            spin_runs.append(cur_run)
        try:
            ws.close()
        except Exception:
            pass

    # ── 요약 ──
    end = prev
    print("\n" + "=" * 64)
    print("조건: {}".format(label or "(이름없음)"))
    print("시작  x={:.3f} y={:.3f} ori={:.1f}도".format(
        start[0], start[1], math.degrees(start[2])))
    for nm in ("C1-1", "C1"):
        r = rel(start, ref.get(nm))
        if r:
            print("      {:<5} 까지 {:.3f} m · 방위차 {:+.1f}도".format(nm, r[0], r[1]))
    print("종료  x={:.3f} y={:.3f} ori={:.1f}도".format(
        end[0], end[1], math.degrees(end[2])))
    print("-" * 64)
    print("총 이동거리 : {:.3f} m".format(path_m))
    print("총 회전량   : {:.1f} 도 (절대 누적)".format(turn_total))
    spin_std = sum(abs(s["deg"]) for s in spin_runs if s.get("phase") == "standard")
    spin_chg = sum(abs(s["deg"]) for s in spin_runs if s.get("phase") == "charge")
    if spin_runs:
        tot = sum(abs(s["deg"]) for s in spin_runs)
        print("제자리 회전 : {} 회 · 합계 {:.1f} 도   ★ 회전 발생".format(
            len(spin_runs), tot))
        for s in spin_runs:
            print("   - {:+.1f}도  [{}]  @ ({}, {})  t+{}s".format(
                s["deg"], s.get("phase"), s["at"][0], s["at"][1], s.get("rel_t")))
    else:
        print("제자리 회전 : 없음")
    print("-" * 64)
    # ★ 이 세 줄이 이번 시험의 판정 기준이다.
    #   진입점에서 도는 것은 정상(자세 잡기). 충전기 앞에서 도는 것이 없애려는 동작이다.
    print("진입점 회전(standard) : {:.1f} 도   ← 여기서 도는 건 정상".format(spin_std))
    print("충전기앞 회전(charge) : {:.1f} 도   {}".format(
        spin_chg, "← ★없애야 할 것" if spin_chg >= 1.0 else "← 없음 (목표 달성)"))
    print("charge 단계 후진거리  : {:.3f} m".format(back_m))
    if done:
        print("결과        : {} {}".format(
            done["state"], "(" + done["fail"] + ")" if done.get("fail") else ""))
    print("기록        : {}".format(path))
    print("=" * 64)
    jf.close()
    return {
        "label": label,
        "start": start,
        "end": end,
        "to_C1": rel(start, ref.get("C1")),
        "to_C1_1": rel(start, ref.get("C1-1")),
        "path_m": path_m,
        "turn_total_deg": turn_total,
        "spins": spin_runs,
        "spin_deg": sum(abs(s["deg"]) for s in spin_runs),
        "spin_standard_deg": spin_std,
        "spin_charge_deg": spin_chg,
        "back_m": back_m,
        "moves": seen_moves,
        "result": (done or {}).get("state"),
        "fail": (done or {}).get("fail"),
        "log": path,
    }


def main():
    global _stop
    ap = argparse.ArgumentParser(description="충전 도킹 실시간 관찰")
    ap.add_argument("--ip", required=True)
    ap.add_argument("--label", default="", help="이 시도의 이름 (예: 3m지점)")
    ap.add_argument("--outdir", default="_logs")
    ap.add_argument("--pos-eps", type=float, default=POS_EPS_M,
                    help="제자리 판정 — 위치 변화 임계(m). 기본 {}".format(POS_EPS_M))
    ap.add_argument("--ori-eps", type=float, default=ORI_EPS_DEG,
                    help="회전 판정 — 각도 변화 임계(도). 기본 {}".format(ORI_EPS_DEG))
    ap.add_argument("--send-charge", action="store_true",
                    help="충전 명령까지 이 스크립트가 보낸다 (로봇이 실제로 움직인다)")
    args = ap.parse_args()

    def _sig(a, b):
        global _stop
        _stop = True
        print("\n[중지] 관찰을 마친다...")
    signal.signal(signal.SIGINT, _sig)

    print("[기준점] 로봇 맵과 DB 에서 C1 / C1-1 을 읽는다...")
    ref = load_reference(args.ip)
    print("  맵      : {}".format(ref.get("map_name")))
    print("  C1      : {}".format(ref.get("C1")))
    print("  C1-1    : {}".format(ref.get("C1-1")))
    if not ref.get("C1"):
        print("  [경고] C1 을 못 찾았다 — 거리 계산 없이 회전만 관찰한다")

    r = watch(args.ip, ref, args.label, args.outdir,
              args.pos_eps, args.ori_eps, args.send_charge)
    return 0 if r else 1


if __name__ == "__main__":
    sys.exit(main())
