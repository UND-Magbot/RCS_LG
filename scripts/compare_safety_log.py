# -*- coding: utf-8 -*-
"""백엔드 `[safety]` 로그 ↔ 프로브 실측(JSONL) 대조 — 판정 지연과 정확도를 잰다.

## 왜 대조가 필요한가
두 기록은 **같은 상황을 다른 시점에서 본 것**이다.

    backend.log   백엔드가 **판단한** 값. 스캔이 밀리면 과거 상황을 지금으로 본다.
    JSONL         측정 도구가 **실시간으로 본** 값. 계산이 가벼워 밀리지 않는다.

같은 거리를 두 기록이 각각 **언제** 봤는지 비교하면 그 차이가 곧 **판정 지연**이다.
지연이 크면 로봇은 이미 지나간 상황에 대해 뒤늦게 서행·정지를 건다
(2026-09-15 실측: 최대 11.5초 → 아무것도 없는 곳에서 정지).

## 대조 방법 (이 스크립트가 하는 일)
    1) backend.log 에서 지정 시간대의 `[safety]` 라인만 뽑는다
    2) JSONL 을 시간 간격으로 잘라 **회차**로 나눈다
    3) 백엔드가 "앞 X m" 라고 적은 시각 T 에 대해,
       JSONL 에서 edge 가 X 에 가장 가까웠던 시각 T' 를 찾는다
    4) **지연 = T − T'**

    ※ 로봇 시계와 서버 시계가 어긋나 있어도(실측 1.1초) 이 방법은 영향을 받지 않는다.
      둘 다 **서버 시각**으로 기록되기 때문이다.

## 쓰는 법
    BackEnd\\venv\\Scripts\\python.exe scripts/compare_safety_log.py \\
        --jsonl _logs/safety_repeat_20260915_150208.jsonl \\
        --ip 192.168.30.130 --from 15:02 --to 15:15
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(_ROOT, "BackEnd", "_logs", "backend.log")

# [safety] 로그에서 뽑을 것들
PAT = [
    ("YELLOW", re.compile(r"YELLOW — 앞 ([\d.]+) m 서행 ([\d.]+)")),
    ("STEP",   re.compile(r"서행 단계 ([\d.]+) → ([\d.]+) m/s \(앞 ([\d.]+) m\)")),
    ("CAND",   re.compile(r"RED 후보 (\d)/(\d) — 앞 ([\d.]+) m")),
    ("RED",    re.compile(r"★ RED — 앞 ([\d.]+) m 정지")),
    ("CLEAR",  re.compile(r"해제 — ")),
    ("STALE",  re.compile(r"밀린 스캔 (\d+)건 버림 \(지연 ([\d.]+)초")),
    ("POSE",   re.compile(r"포즈가 낡아 판정 보류 \(([\d.]+)초")),
    ("WALL",   re.compile(r"맵에 있는 벽 (\d+)개 건너뜀")),
]


def parse_backend(path, ip, t_from, t_to):
    """[safety] 라인 → [(시각문자열, 종류, 거리 or None, 원문)]"""
    out = []
    for line in io.open(path, encoding="utf-8", errors="ignore"):
        if "[safety]" not in line or ip not in line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        hhmmss = parts[1]
        if not (t_from <= hhmmss[:len(t_from)] <= t_to):
            continue
        kind, dist = None, None
        for k, rx in PAT:
            m = rx.search(line)
            if not m:
                continue
            kind = k
            if k == "YELLOW":
                dist = float(m.group(1))
            elif k == "STEP":
                dist = float(m.group(3))
            elif k == "CAND":
                dist = float(m.group(3))
            elif k == "RED":
                dist = float(m.group(1))
            break
        if kind:
            msg = line.split("[safety]", 1)[1].strip()
            msg = msg.replace(ip, "").strip()
            out.append((hhmmss, kind, dist, msg))
    return out


def load_runs(path, gap=5.0):
    rows = [json.loads(l) for l in io.open(path, encoding="utf-8")]
    runs, cur = [], []
    for r in rows:
        if cur and r["t"] - cur[-1]["t"] > gap:
            runs.append(cur)
            cur = []
        cur.append(r)
    if cur:
        runs.append(cur)
    return rows, runs


def hms(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts))


def find_when(rows, dist, around_ts, window=8.0):
    """edge 가 `dist` 에 가장 가까웠던 샘플 시각.

    ★ `window` 를 넓게 잡으면 **다른 회차의 비슷한 값**에 잘못 붙는다
      (2026-09-15: 25초로 뒀더니 복귀 중 YELLOW 가 16초 전 주행 값에 매칭돼
       "지연 +16.7초" 라는 가짜 결과가 나왔다). 8초면 한 회차 안을 넘지 않는다.
    """
    best, bd = None, 9e9
    for r in rows:
        if r["edge"] is None:
            continue
        if not (around_ts - window <= r["t"] <= around_ts + 2.0):
            continue
        d = abs(r["edge"] - dist)
        if d < bd:
            bd, best = d, r
    return best, bd


def run_of(runs, ts):
    """이 시각이 몇 회차 주행 구간 안인가. 밖이면 None(=회차 사이 = 복귀 중)."""
    for i, rr in enumerate(runs, 1):
        if rr[0]["t"] - 1.0 <= ts <= rr[-1]["t"] + 1.0:
            return i
    return None


def main():
    ap = argparse.ArgumentParser(description="백엔드 로그 ↔ 실측 대조")
    ap.add_argument("--jsonl", required=True)
    ap.add_argument("--ip", required=True)
    ap.add_argument("--from", dest="t_from", required=True, help="예 15:02")
    ap.add_argument("--to", dest="t_to", required=True, help="예 15:15")
    ap.add_argument("--out", default=None, help="결과를 저장할 txt")
    a = ap.parse_args()

    ev = parse_backend(LOG, a.ip, a.t_from, a.t_to)
    rows, runs = load_runs(a.jsonl)

    buf = []

    def P(s=""):
        print(s)
        buf.append(s)

    P(f"# 안전거리 시험 로그 대조")
    P(f"  대상        {a.ip}   {a.t_from} ~ {a.t_to}")
    P(f"  백엔드 로그  {LOG}")
    P(f"  실측 JSONL  {a.jsonl}")
    P(f"  [safety] 이벤트 {len(ev)}건 · 실측 {len(rows)}샘플 · {len(runs)}회차")
    P()

    # ── 1) 백엔드 이벤트 요약 ──
    from collections import Counter
    c = Counter(k for _, k, _, _ in ev)
    P("## 1. 백엔드가 남긴 이벤트")
    for k in ("YELLOW", "STEP", "CAND", "RED", "CLEAR", "STALE", "POSE", "WALL"):
        if c.get(k):
            P(f"   {k:7s} {c[k]:3d}건")
    P()

    # ── 2) 회차별 실측 ──
    P("## 2. 회차별 실측 (JSONL)")
    P("   회차   출발   YELLOW    RED    정지   임계후  샘플")
    for i, rr in enumerate(runs, 1):
        e = [r for r in rr if r["edge"] is not None]
        if not e:
            P(f"   {i:>3}   (밴드 안 점 없음)")
            continue
        def below(th):
            return next((r for r in e if r["edge"] <= th), None)
        y, rd = below(3.0), below(1.0)
        fin = e[-1]["edge"]
        after = None if rd is None else rd["edge"] - fin
        ys = "     —" if y is None else format(y["edge"], "6.3f")
        rs = "    —" if rd is None else format(rd["edge"], "5.3f")
        afs = "     —" if after is None else format(after, "6.3f")
        P(f"   {i:>3}  {e[0]['edge']:5.2f}  {ys}  {rs}  {fin:6.3f}  {afs}  {len(rr):4d}")
    P()

    # ── 3) ★ 대조: 백엔드 판정 시각 ↔ 실제 발생 시각 ──
    P("## 3. 판정 지연 — 백엔드가 본 거리가 실제로는 언제였나")
    P("   지연 = (백엔드가 그 값을 로그에 쓴 시각) − (실제로 그 거리였던 시각)")
    P()
    P("   회차  백엔드시각  종류    백엔드값   실제시각   실측값    지연")
    lags = []
    skipped = 0
    for hhmmss, kind, dist, _ in ev:
        if dist is None or kind not in ("YELLOW", "STEP", "CAND", "RED"):
            continue
        t_struct = time.localtime(rows[0]["t"])
        ts = time.mktime((t_struct.tm_year, t_struct.tm_mon, t_struct.tm_mday,
                          int(hhmmss[0:2]), int(hhmmss[3:5]), int(hhmmss[6:8]),
                          0, 0, -1))
        rn = run_of(runs, ts)
        if rn is None:
            skipped += 1          # 회차 사이(복귀 중) — 주행 판정이 아니다
            continue
        hit, diff = find_when(rows, dist, ts)
        if hit is None or diff > 0.15:
            continue
        lag = ts - hit["t"]
        if lag < -2:
            continue
        lags.append(lag)
        P(f"   {rn:>3}   {hhmmss}  {kind:6s} {dist:7.2f}   {hms(hit['t'])}  "
          f"{hit['edge']:7.3f}  {lag:+6.1f}초")
    if lags:
        P()
        P(f"   ▶ 지연 최소 {min(lags):+.1f}초 · 최대 {max(lags):+.1f}초 · "
          f"평균 {sum(lags)/len(lags):+.1f}초   ({len(lags)}건)")
        P(f"     ※ 음수는 백엔드가 더 빨리 본 것이 아니라 초 단위 반올림 오차다"
          f" (로그는 초 단위, 실측은 밀리초)")
    if skipped:
        P(f"   ▶ 회차 밖(복귀 중) 이벤트 {skipped}건은 제외 — 주행 판정이 아니다")
    P()

    # ── 4) 원문 ──
    P("## 4. [safety] 원문")
    for hhmmss, kind, _, msg in ev:
        P(f"   {hhmmss}  {msg}")

    if a.out:
        with io.open(a.out, "w", encoding="utf-8") as f:
            f.write("\n".join(buf) + "\n")
        print(f"\n저장  {a.out}")


if __name__ == "__main__":
    main()
