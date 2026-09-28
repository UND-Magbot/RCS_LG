# -*- coding: utf-8 -*-
"""C1-1 거리 찾기 — 충전 도킹 반복 시험

"진입점(C1-1)이 충전소에서 얼마나 떨어져 있어야 **회전 없이** 들어가는가" 를
한 창에서 반복해서 찾는다.

한 회차 흐름
  1) 로봇을 시작 위치에 둔다        (사람)
  2) 엔터                            (사람)
  3) 충전 명령 전송 → 도킹 관찰      (스크립트)
  4) 회전 여부·결과 요약 출력        (스크립트)
  5) 로봇을 충전기에서 빼낸다        (사람)
  6) 다음 회차 진입점 거리를 정한다  (엔터=0.3m 더 멀리 / 숫자=그 거리(m) / 0 / q)
  → 1 로 돌아감

무엇을 보는가 (2026-09-14 확정)
  로봇은 충전 접점이 뒤에 있어 **후진 도킹**한다. 그래서 진입점에서는
  독을 등진 자세로 서 있다가 **그대로 뒤로 들어가는 것**이 정상이다.
    · 진입점(standard)에서의 회전 = 자세를 잡는 정상 동작. 충전기에서 멀수록 좋다.
    · 충전기 앞(charge)에서의 회전 = **없애려는 동작.** 독을 못 찾고 헤매는 것이다.
  그래서 회차별로 이 둘을 **나눠서** 보여준다.

  진입점은 **멀어야** 한다 — docs/10 §1 실측: 36cm 재시도 잦음 / 1.2m 한 번에 도킹.

⚠ 3) 에서 **로봇이 실제로 움직인다.** 엔터 전에 주변을 확인할 것.

쓰는 법
  python scripts/dock_cycle.py --ip 192.168.30.110
  python scripts/dock_cycle.py --ip 192.168.30.110 --step-m 0.2    # 회차당 증가폭
  python scripts/dock_cycle.py --ip 192.168.30.110 --no-move      # 좌표 안 건드리고 관찰만
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dock_watch as dw              # noqa: E402
import entry_poi_move as epm         # noqa: E402

BACKEND = "http://127.0.0.1:8002"


def backend_dock(ip):
    """백엔드에 충전소 복귀를 요청한다.

    이 경로가 **C1-1 을 실제로 쓰는 경로**다 — 백엔드가 DB 에서 `<충전소>-1` 을
    읽어 standard 로 먼저 가고, 도착을 확인한 뒤 charge 를 보낸다.
    로봇에 직접 charge 를 쏘면 C1-1 을 아예 안 거치므로 이 시험이 무의미해진다.
    """
    r = requests.post("{}/api/robots/remote/dock/{}".format(BACKEND, ip), timeout=15)
    r.raise_for_status()
    return r.json()


def cur_entry_dist(ip, base="C1"):
    """★ 맵은 **로봇이 실제로 쓰는 것**(robots.charging_id)으로 고른다.

    활성 맵이 여러 개라 '가장 최근 활성 맵' 으로 고르면 엉뚱한 맵을 건드린다.
    2026-09-14 에 실제로 그렇게 돼서, 거리를 세 번 바꿨는데 로봇 동작이
    한 번도 안 바뀌었다(charge 시작 거리가 세 번 다 0.66m 로 동일).
    """
    cn = epm.connect()
    cu = cn.cursor()
    mid, cname = epm.map_for_robot(cu, ip)
    if not mid:
        cn.close()
        return None, None, None
    base = cname or base
    b = epm.get_poi(cu, mid, base)
    e = epm.get_poi(cu, mid, base + "-1")
    cn.close()
    if not b or not e or b["wx"] is None or e["wx"] is None:
        return None, mid, base
    return math.hypot(b["wx"] - e["wx"], b["wy"] - e["wy"]), mid, base


def set_entry_dist(ip, base, dist_m):
    """진입점을 본 POI 로부터 dist_m 거리에 놓는다 (world + pixel 동시 갱신).

    --ip 를 넘겨 **로봇이 쓰는 맵**을 고치게 한다.
    """
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    cmd = [sys.executable, os.path.join(here, "entry_poi_move.py"),
           "--ip", ip, "--base", base, "--set-dist", str(dist_m)]
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    print(p.stdout.strip())
    if p.returncode != 0:
        print(p.stderr.strip())
    return p.returncode == 0


def main():
    ap = argparse.ArgumentParser(description="충전 도킹 반복 시험")
    ap.add_argument("--ip", required=True)
    ap.add_argument("--base", default="C1", help="충전소 POI 이름")
    ap.add_argument("--step-m", type=float, default=0.30,
                    help="엔터만 치면 진입점 거리를 이만큼 바꾼다(m). "
                         "양수=멀어짐 / **음수=가까워짐**. 기본 +0.30. "
                         "가까워지는 쪽을 보려면 예: --step-m -0.1")
    ap.add_argument("--outdir", default="_logs")
    ap.add_argument("--no-move", action="store_true",
                    help="진입점 좌표를 건드리지 않는다 (관찰만 반복)")
    args = ap.parse_args()

    # 백엔드 확인 — 이게 꺼져 있으면 C1-1 이 아예 안 쓰인다
    try:
        requests.get("{}/ping".format(BACKEND), timeout=5)
    except Exception:
        print("[중단] 백엔드({})가 꺼져 있다.".format(BACKEND))
        print("       C1-1 진입점은 **백엔드가 읽어서 쓰는 값**이라, 꺼져 있으면")
        print("       이 시험 자체가 의미가 없다. run_local.ps1 로 먼저 띄워라.")
        return 1

    print("[기준점] 로봇 맵과 DB 에서 C1 / C1-1 을 읽는다...")
    ref = dw.load_reference(args.ip)
    print("  맵   : {} (db map_id={})".format(ref.get("map_name"), ref.get("map_id")))
    print("  C1   : {}".format(ref.get("C1")))
    print("  C1-1 : {}".format(ref.get("C1-1")))
    if not ref.get("C1-1"):
        print("[중단] C1-1 을 못 찾았다. 맵 편집기에서 먼저 찍어라.")
        return 1

    results = []
    trial = 0
    try:
        while True:
            trial += 1
            d, mid, base = cur_entry_dist(args.ip, args.base)
            if d is None:
                print("[중단] 진입점을 찾을 수 없다 (map={} base={})".format(mid, base))
                return 1
            print("\n" + "=" * 66)
            print(" {}차 시도   진입점 거리 {:.3f} m".format(trial, d))
            print("=" * 66)
            print("로봇을 시작 위치에 두고, 주변을 확인한 뒤 엔터를 눌러라.")
            print("  (엔터를 누르면 **로봇이 충전소로 출발한다**)   q = 종료")
            try:
                s = input("  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                break
            if s == "q":
                break

            label = "{}차_{:.2f}m".format(trial, d)
            try:
                resp = backend_dock(args.ip)
                print("  [충전 명령 전송] {}".format(resp))
            except Exception as e:
                print("  [실패] 충전 명령 전송 실패: {}".format(e))
                continue

            # 관찰 — 백엔드가 이미 이동을 시작했으므로 바로 붙는다
            ref2 = dict(ref)
            ref2["C1-1"] = None
            r = dw.watch(args.ip, ref, label, args.outdir,
                         dw.POS_EPS_M, dw.ORI_EPS_DEG, send_charge=False)
            if r:
                r["entry_dist_m"] = d
                r["trial"] = trial
                results.append(r)

            # 다음 회차 진입점 거리
            if args.no_move:
                continue
            # ── 다음 회차 진입점 거리 ──
            # ★ 거리를 **절대값(m)** 으로 받는다. cm 증감으로 받으면 방향을 헷갈려
            #   충전기를 지나치기 쉽다(2026-09-14 실측에서 실제로 넘어갔다).
            #   docs/10 §1 실측: 36cm 재시도 잦음 / 1.2m 한 번에 도킹.
            nxt = max(epm.MIN_DIST_M, d + args.step_m)
            print("\n로봇을 충전기에서 빼내라.")
            print("  엔터      = 다음 거리 {:.2f} m  (현재 {:.2f} m 에서 {:+.2f} — {})".format(
                nxt, d, args.step_m,
                "가까워짐" if args.step_m < 0 else "멀어짐"))
            print("  숫자      = 그 거리(m)로 설정   예: 0.4   (작을수록 C1 에 가까움)")
            print("  0         = 거리 유지")
            print("  q         = 종료")
            try:
                s = input("  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                break
            if s == "q":
                break
            if s == "":
                want = nxt
            else:
                try:
                    want = float(s)
                except ValueError:
                    want = nxt
            if want > 0 and abs(want - d) > 1e-3:
                set_entry_dist(args.ip, base, want)
    except KeyboardInterrupt:
        pass

    # ── 전체 요약 ──
    print("\n\n" + "#" * 66)
    print("# 회차별 결과")
    print("#" * 66)
    if not results:
        print("기록된 시도가 없다.")
        return 0
    print("{:<4} {:>9} {:>9} {:>11} {:>12} {:>9}  {}".format(
        "회차", "진입점거리", "시작C1거리", "진입점회전", "충전기앞회전", "후진거리", "결과"))
    for r in results:
        print("{:<4} {:>8.3f}m {:>8.3f}m {:>10.1f}° {:>11.1f}° {:>8.3f}m  {}".format(
            r["trial"], r["entry_dist_m"],
            r["to_C1"][0] if r.get("to_C1") else float("nan"),
            r.get("spin_standard_deg", 0.0), r.get("spin_charge_deg", 0.0),
            r.get("back_m", 0.0), r["result"] or "?"))

    # 목표 = **충전기 앞에서 추가 회전 없이** 그대로 후진해 도킹.
    # 진입점(standard)에서 도는 것은 정상이므로 판정에 넣지 않는다.
    ok = [r for r in results
          if r.get("spin_charge_deg", 99) < 1.0 and r["result"] == "succeeded"]
    if ok:
        print("\n★ 충전기 앞 추가 회전 없이 후진 도킹 성공한 회차:")
        for r in ok:
            print("   {}차  진입점 {:.3f} m   (후진 {:.3f} m)".format(
                r["trial"], r["entry_dist_m"], r.get("back_m", 0.0)))
        best = min(ok, key=lambda r: r["entry_dist_m"])
        print("   → 그중 가장 가까운 거리 = **{:.3f} m** ({}차)".format(
            best["entry_dist_m"], best["trial"]))
    else:
        print("\n※ 충전기 앞 회전 없이 성공한 회차가 아직 없다.")
        print("   진입점을 **더 멀리** 두고 다시 해볼 것 (docs/10 실측: 1.2m 한 번에 도킹).")
    print("\n진입점을 원래대로 돌리려면:")
    print("  python scripts/entry_poi_move.py --base {} --restore".format(args.base))
    return 0


if __name__ == "__main__":
    sys.exit(main())
