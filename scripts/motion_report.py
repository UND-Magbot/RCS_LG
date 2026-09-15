# -*- coding: utf-8 -*-
"""가감속이 부드러운지 숫자로 판정한다 — `/motion_metrics`(10 Hz) 분석.

왜 필요한가
  사람이 느끼는 '멈칫' 은 속도가 아니라 **가속도의 크기**다. 그런데 로봇 설정은
  가속 0.3 m/s² / 감속 2.0 m/s² 로 **감속이 7배 거칠게** 허용돼 있다.
  눈으로만 보면 "좀 덜컹인다" 로 끝나므로, 설정을 바꿨을 때 좋아졌는지 나빠졌는지
  비교가 안 된다. 이 스크립트가 그 비교 기준을 만든다.

무엇을 재나
  · v_max        그 회차 최고 속도
  · decel_peak   가장 강했던 감속 (음수 가속도의 절댓값). 클수록 덜컹인다
  · decel_p95    상위 5% 감속 — 한 번 튄 값에 휘둘리지 않게 같이 본다
  · 덜컹 횟수    |감속| 이 기준(기본 1.0 m/s²)을 넘은 **구간** 수
                 (샘플 수가 아니라 구간 수다. 0.5초짜리 한 번 = 1회)
  · 정지 횟수    속도가 0 근처로 떨어진 구간 수

  ※ 10 Hz 라 0.1초보다 짧은 사건은 못 본다. 2.0 m/s² 로 0.8→0.5 는 0.15초라
    1~2 샘플에 걸린다 — 잡히긴 하지만 피크가 실제보다 낮게 나올 수 있다.
    **값을 절대 기준으로 쓰지 말고 설정 변경 전후 비교에만 쓸 것.**

쓰는 법
  BackEnd\\venv\\Scripts\\python.exe scripts/motion_report.py \\
      --motion _logs/safety_repeat_20260915_161551_motion.jsonl
  두 개를 비교하려면
      ... --motion 새것.jsonl --base 예전것.jsonl
"""
from __future__ import annotations

import argparse
import io
import json
import os
import statistics
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HARSH = 1.0        # 이 이상 감속하면 '덜컹' 으로 센다 (m/s²)
STILL_V = 0.03     # 이 밑이면 멈춘 것으로 본다 (m/s)
RUN_GAP = 5.0      # 이 이상 비면 다음 회차로 자른다 (초)


def load(path):
    rows = []
    for line in io.open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def split_runs(rows, gap=RUN_GAP):
    runs, cur = [], []
    for r in rows:
        if cur and r["t"] - cur[-1]["t"] > gap:
            runs.append(cur)
            cur = []
        cur.append(r)
    if cur:
        runs.append(cur)
    return runs


def episodes(rows, key, test):
    """`test` 가 참인 **연속 구간**들을 [(시작idx, 끝idx, 피크값)] 으로."""
    out, s, peak = [], None, 0.0
    for i, r in enumerate(rows):
        v = r.get(key)
        ok = v is not None and test(v)
        if ok:
            if s is None:
                s, peak = i, abs(v)
            peak = max(peak, abs(v))
        elif s is not None:
            out.append((s, i, peak))
            s = None
    if s is not None:
        out.append((s, len(rows), peak))
    return out


def run_stats(rows):
    vs = [r["v"] for r in rows if r.get("v") is not None]
    ac = [r["acc"] for r in rows if r.get("acc") is not None]
    if not ac:
        return None
    dec = sorted(-x for x in ac if x < 0)
    acc = [x for x in ac if x > 0]
    harsh = episodes(rows, "acc", lambda v: v < -HARSH)
    stops = episodes(rows, "v", lambda v: abs(v) < STILL_V)
    dur = rows[-1]["t"] - rows[0]["t"]
    return {
        "dur": dur,
        "n": len(rows),
        "hz": len(rows) / dur if dur > 0 else 0,
        "v_max": max(vs) if vs else 0.0,
        "decel_peak": dec[-1] if dec else 0.0,
        "decel_p95": dec[min(len(dec) - 1, int(len(dec) * 0.95))] if dec else 0.0,
        "decel_mean": statistics.mean(dec) if dec else 0.0,
        "accel_peak": max(acc) if acc else 0.0,
        "harsh": len(harsh),
        "harsh_peaks": [round(h[2], 2) for h in harsh],
        "stops": len(stops),
    }


def spark(rows, key, lo, hi, width=72):
    """값을 가로 막대 한 줄로. 눈으로 흐름을 보려는 것."""
    if not rows:
        return ""
    step = max(1, len(rows) // width)
    chars = " ▁▂▃▄▅▆▇█"
    out = ""
    for i in range(0, len(rows), step):
        chunk = [abs(r.get(key) or 0.0) for r in rows[i:i + step]]
        v = max(chunk) if chunk else 0.0
        f = 0.0 if hi == lo else (v - lo) / (hi - lo)
        out += chars[max(0, min(len(chars) - 1, int(f * (len(chars) - 1))))]
    return out


def report(path, label):
    rows = load(path)
    if not rows:
        print(f"  {label}: 데이터 없음 ({path})")
        return None
    runs = split_runs(rows)
    print(f"■ {label}")
    print(f"   {os.path.basename(path)}   샘플 {len(rows)}   회차 {len(runs)}")
    print()
    print("   회차  길이   Hz    최고속도  감속피크  감속p95  가속피크  덜컹  정지")
    print("   " + "-" * 70)
    allst = []
    for i, rr in enumerate(runs, 1):
        st = run_stats(rr)
        if st is None:
            continue
        allst.append(st)
        print("   %3d  %5.1fs %4.1f   %6.3f   %6.2f   %6.2f   %6.2f  %4d  %4d"
              % (i, st["dur"], st["hz"], st["v_max"], st["decel_peak"],
                 st["decel_p95"], st["accel_peak"], st["harsh"], st["stops"]))
    if not allst:
        return None
    agg = {
        "v_max": max(s["v_max"] for s in allst),
        "decel_peak": max(s["decel_peak"] for s in allst),
        "decel_p95": statistics.mean(s["decel_p95"] for s in allst),
        "accel_peak": max(s["accel_peak"] for s in allst),
        "harsh": sum(s["harsh"] for s in allst),
        "harsh_per_run": sum(s["harsh"] for s in allst) / len(allst),
        "runs": len(allst),
    }
    print("   " + "-" * 70)
    print("   전체  최고속도 %.3f   감속피크 %.2f   감속p95(평균) %.2f   "
          "가속피크 %.2f" % (agg["v_max"], agg["decel_peak"],
                             agg["decel_p95"], agg["accel_peak"]))
    print("         덜컹(감속>%.1f) 총 %d회 = 회차당 %.1f회"
          % (HARSH, agg["harsh"], agg["harsh_per_run"]))
    print()
    # 첫 회차 파형
    print("   [1회차 파형]  위=속도(0~%.1f)  아래=감속 크기(0~2.0)"
          % max(0.1, agg["v_max"]))
    print("     v |" + spark(runs[0], "v", 0, max(0.1, agg["v_max"])) + "|")
    dec_rows = [{"acc": min(0.0, r.get("acc") or 0.0)} for r in runs[0]]
    print("     a |" + spark(dec_rows, "acc", 0, 2.0) + "|")
    print()
    return agg


def main():
    global HARSH
    ap = argparse.ArgumentParser(description="가감속 분석 (/motion_metrics)")
    ap.add_argument("--motion", required=True, help="*_motion.jsonl")
    ap.add_argument("--base", default=None, help="비교 대상 *_motion.jsonl")
    ap.add_argument("--harsh", type=float, default=HARSH,
                    help="덜컹으로 셀 감속 기준 m/s² (기본 1.0)")
    a = ap.parse_args()
    HARSH = a.harsh

    print("가감속 분석   덜컹 기준 |감속| > %.1f m/s²" % HARSH)
    print("=" * 74)
    print()
    new = report(a.motion, "측정")
    old = report(a.base, "기준선") if a.base else None

    if new and old:
        print("■ 비교 (기준선 → 측정)")
        print("   %-18s %10s %10s %s" % ("지표", "기준선", "측정", "판정"))
        print("   " + "-" * 60)
        for k, name, good_down in (("decel_peak", "감속 피크", True),
                                   ("decel_p95", "감속 p95", True),
                                   ("harsh_per_run", "회차당 덜컹", True),
                                   ("v_max", "최고 속도", False)):
            o, n = old[k], new[k]
            if good_down:
                v = "개선" if n < o * 0.9 else ("악화" if n > o * 1.1 else "비슷")
            else:
                v = "느려짐" if n < o * 0.9 else ("빨라짐" if n > o * 1.1 else "비슷")
            print("   %-18s %10.2f %10.2f  %s" % (name, o, n, v))
        print()
        print("   ※ 감속 피크·p95 가 내려가고 덜컹 횟수가 줄면 부드러워진 것이다.")
        print("     최고 속도가 같이 떨어졌다면 '느려져서 부드러운' 것이라 별개로 봐야 한다.")


if __name__ == "__main__":
    main()
