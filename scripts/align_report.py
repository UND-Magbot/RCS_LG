# -*- coding: utf-8 -*-
"""진입점 → 작업지점 정밀 진입 구간만 떼어내 '왜 돌았는지' 를 낸다.

## 무엇을 답하려는 도구인가
  · 진입점에 **어떤 자세로 도착**했나 (= 우리가 고친 부분이 먹었나)
  · 그 뒤 `align_with_rack` / `to_unload_point` 가 **얼마나 돌았나**
  · 그 회전이 **제자리 회전인가, 이동하면서 돈 것인가**
  · 돌았다면 **왜** — 도착 자세가 틀렸나 / 랙이 틀어져 있었나 / 재시도했나

## 왜 drive_log 를 안 고치고 따로 두나
  drive_log.py 는 현장 이관본(해시 고정)이다. 기록은 이미 충분하다 —
  motion.jsonl 이 10Hz 로 x·y·ori·action_type 을 전부 남긴다.
  **모자란 건 기록이 아니라 해석**이라, 읽기 전용 분석기를 따로 둔다.
  이미 찍어둔 과거 로그에도 그대로 돌릴 수 있다(= 수정 전 기준선을 뽑을 수 있다).

## 쓰는 법
  python scripts/align_report.py _logs/run_20260918_xxxx_ROBOT
  python scripts/align_report.py _logs/run_.../c01_120000      (사이클 하나만)

## 읽는 법
  누적회전 = 구간 동안 yaw 변화량의 절대값 합 (돌았다 되돌아온 것도 전부 더한다)
  순회전   = 시작 자세 → 끝 자세 최단 각도차 (실제로 필요했던 회전)
  헛회전   = 누적 - |순| . 이게 크면 **왔다갔다 한 것**이다
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 정밀 진입 동작. safety_zone.PRECISE_ACTIONS 와 같은 집합이다.
PRECISE = {"align_with_rack", "to_unload_point", "charge"}

# 제자리 회전 판정 — 이 거리보다 덜 움직이면서 돈 구간은 '제자리'로 센다.
# 10Hz 샘플 하나 사이에 0.02 m 면 0.2 m/s 다. 정밀 동작은 이보다 느리게 긴다.
SPOT_MOVE_EPS = 0.02


def _ang_norm(a: float) -> float:
    """-pi ~ pi 로 정규화."""
    return math.atan2(math.sin(a), math.cos(a))


def _deg(a: float) -> float:
    return math.degrees(a)


def read_motion(path: str) -> list[dict]:
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("ori") is None or d.get("x") is None:
                continue
            rows.append(d)
    rows.sort(key=lambda r: r.get("t", 0))
    return rows


def read_moves(path: str) -> list[dict]:
    """moves.jsonl — 어떤 타입·목표로 보냈는지. 없으면 빈 리스트."""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            mv = d.get("move")
            if isinstance(mv, dict):
                out.append({"t": d.get("t"), "id": d.get("action_id"), "mv": mv})
    return out


def segments(rows: list[dict]) -> list[dict]:
    """action_type 이 정밀 동작인 구간을 잘라낸다."""
    segs, cur = [], None
    for r in rows:
        act = r.get("action")
        if act in PRECISE:
            if cur is None or cur["action"] != act:
                if cur:
                    segs.append(cur)
                cur = {"action": act, "rows": [r]}
            else:
                cur["rows"].append(r)
        else:
            if cur:
                segs.append(cur)
                cur = None
    if cur:
        segs.append(cur)
    return [s for s in segs if len(s["rows"]) >= 3]


def analyse(seg: dict, before: list[dict]) -> dict:
    rows = seg["rows"]
    t0, t1 = rows[0].get("t"), rows[-1].get("t")
    o0, o1 = rows[0]["ori"], rows[-1]["ori"]

    total = 0.0      # 누적 회전 (절대값 합)
    spot = 0.0       # 그중 제자리 회전
    dist = 0.0
    prev = rows[0]
    for r in rows[1:]:
        d_ang = abs(_ang_norm(r["ori"] - prev["ori"]))
        d_pos = math.hypot(r["x"] - prev["x"], r["y"] - prev["y"])
        total += d_ang
        dist += d_pos
        if d_pos < SPOT_MOVE_EPS:
            spot += d_ang
        prev = r
    net = abs(_ang_norm(o1 - o0))
    waste = max(0.0, total - net)

    # 진입점 도착 자세 — 정밀 동작 직전 1초 구간의 자세
    entry_ori = None
    if before:
        tail = [b for b in before if b.get("t") and t0 and t0 - b["t"] <= 1.0]
        if tail:
            entry_ori = tail[-1]["ori"]
    # 도착 자세가 최종 자세와 얼마나 달랐나 = 정밀 동작이 떠안은 각도
    entry_gap = None if entry_ori is None else abs(_ang_norm(o1 - entry_ori))

    stucks = sorted({r.get("stuck") for r in rows
                     if r.get("stuck") not in (None, "none")})
    moves = sorted({r.get("move") for r in rows if r.get("move")})
    return {
        "action": seg["action"],
        "t0": t0, "t1": t1,
        "sec": None if (t0 is None or t1 is None) else round(t1 - t0, 1),
        "total_deg": round(_deg(total), 1),
        "net_deg": round(_deg(net), 1),
        "waste_deg": round(_deg(waste), 1),
        "spot_deg": round(_deg(spot), 1),
        "dist_m": round(dist, 2),
        "entry_gap_deg": None if entry_gap is None else round(_deg(entry_gap), 1),
        "start_deg": round(_deg(o0), 1),
        "end_deg": round(_deg(o1), 1),
        "stuck": ",".join(stucks) if stucks else "",
        "move_states": ",".join(moves),
        "n": len(rows),
    }


def verdict(a: dict) -> str:
    """왜 돌았는지 — 숫자가 말해주는 것만 적는다."""
    reasons = []
    if a["total_deg"] < 30:
        return "정상 — 회전 거의 없음"
    if a["entry_gap_deg"] is not None and a["entry_gap_deg"] > 30:
        reasons.append(f"도착 자세가 {a['entry_gap_deg']:.0f}° 틀어져 있었다"
                       f" → 진입점 도착 각도 문제")
    if a["waste_deg"] > 60:
        reasons.append(f"헛회전 {a['waste_deg']:.0f}° — 왔다갔다 반복"
                       f"(재시도/미세조정 실패 의심)")
    if a["spot_deg"] > 45:
        reasons.append(f"제자리 회전 {a['spot_deg']:.0f}°")
    if a["stuck"]:
        reasons.append(f"stuck_state={a['stuck']}")
    if not reasons:
        reasons.append(f"총 {a['total_deg']:.0f}° 회전했으나 원인 단서 없음"
                       f" — 랙 실제 각도 확인 필요")
    return " / ".join(reasons)


def run_cycle(cdir: str) -> list[dict]:
    mpath = os.path.join(cdir, "motion.jsonl")
    if not os.path.exists(mpath):
        return []
    rows = read_motion(mpath)
    if not rows:
        return []
    segs = segments(rows)
    out = []
    for s in segs:
        t0 = s["rows"][0].get("t")
        before = [r for r in rows if r.get("t") and t0 and r["t"] < t0]
        a = analyse(s, before)
        a["cycle"] = os.path.basename(cdir)
        out.append(a)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="run_... 폴더 또는 c01_... 사이클 폴더")
    args = ap.parse_args()
    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        print(f"폴더가 없습니다: {root}")
        return 1

    if os.path.exists(os.path.join(root, "motion.jsonl")):
        cycles = [root]
    else:
        cycles = sorted(
            os.path.join(root, d) for d in os.listdir(root)
            if os.path.isdir(os.path.join(root, d))
            and os.path.exists(os.path.join(root, d, "motion.jsonl")))
    if not cycles:
        print("motion.jsonl 을 못 찾았습니다.")
        return 1

    rows: list[dict] = []
    for c in cycles:
        rows.extend(run_cycle(c))

    print(f"\n대상: {root}")
    print(f"사이클 {len(cycles)}개 / 정밀 진입 구간 {len(rows)}개\n")
    if not rows:
        print("정밀 동작(align_with_rack / to_unload_point) 구간이 없습니다.")
        print("→ 이 로그는 진입 동작을 포함하지 않았거나, action_type 이 안 찍혔습니다.")
        return 0

    hdr = (f"{'사이클':<12} {'동작':<17} {'초':>5} {'누적°':>7} {'순°':>6} "
           f"{'헛°':>6} {'제자리°':>7} {'도착틀어짐°':>10} {'이동m':>6}")
    print(hdr)
    print("-" * len(hdr))
    for a in rows:
        eg = "-" if a["entry_gap_deg"] is None else f"{a['entry_gap_deg']:.1f}"
        print(f"{a['cycle']:<12} {a['action']:<17} {a['sec'] or 0:>5.1f} "
              f"{a['total_deg']:>7.1f} {a['net_deg']:>6.1f} {a['waste_deg']:>6.1f} "
              f"{a['spot_deg']:>7.1f} {eg:>10} {a['dist_m']:>6.2f}")

    print("\n판정")
    print("-" * 70)
    for a in rows:
        print(f"  [{a['cycle']} {a['action']}] {verdict(a)}")

    ok = [a for a in rows if a["total_deg"] < 30]
    print(f"\n요약 — 회전 30° 미만 {len(ok)}/{len(rows)}건")
    if rows:
        tot = [a["total_deg"] for a in rows]
        tot.sort()
        mid = tot[len(tot) // 2]
        print(f"        누적회전 중앙값 {mid:.1f}° / 최대 {max(tot):.1f}°")
        print(f"        (수정 전 LG 현장 실측 247~424° — 목표 30° 이하)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
