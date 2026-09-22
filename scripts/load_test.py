#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""로봇 CPU 부하 ↔ 주행 중 일시적 멈춤 인과 확인 — **읽기 전용**.

    python scripts/load_test.py --ip 192.168.30.110

무엇을 하는가
  로봇에 **일부러 부하를 걸었다 뺐다** 하면서, 그때 주행이 어떻게 달라지는지 본다.
  키 하나로 부하 단계를 바꾸고, 화면에서 바로 비교한다.

왜 만들었나 (2026-09-22 현장 로그 분석)
  주행 중 1초씩 서는 일이 반복되는데 원인을 못 찾았다. 하나씩 지워보니 —
    · 장애물 아님    정면 라이다 점 ±1.0 m 까지 0개
    · 로봇도 장애물이라 안 함  collide_dir·pushed·slipping 5,397건 전부 0
    · 서버 안전존 아님  상한 0.70 유지한 채로 로봇이 스스로 0.26 으로 낮춤
    · 오히려 "가속 가능"이라 함  accelerability 0.95 (정상 주행 때는 0.26)
  남은 것이 **로봇 CPU 부하**였다.
    · `/alerts` 의 "Load average in 1 minute is 9.1, which is high" 가 51건
    · 멈춤의 93~100% 가 그 경고와 ±30초 안에 겹침
    · 랙 적재 시 부하 경고 4.2배 · 멈춤 2.9배
  다만 **상관만 봤을 뿐 인과는 못 봤다.** 부하를 우리가 직접 걸어보고
  멈춤이 따라 늘면 그때 인과가 선다. 그게 이 도구다.

★ 로봇에 아무 명령도 보내지 않는다
  WebSocket 구독과 GET 조회뿐이다. 이동·잭·속도 명령은 한 줄도 없다.
  부하는 "무거운 토픽을 더 구독한다"로만 만든다.

★ 어떤 토픽이 무거운가 (drive_log.py 가 재놓은 것)
    /maps/1cm/1hz · /maps/5cm/1hz   로봇이 **PNG 를 인코딩해서** 보낸다. 제일 무겁다
    /scan_matched_points2           점 약 1,100개 × 1.86 Hz
  전량 구독이 초당 약 42메시지, 최소 구독이 약 20메시지다.

실험 설계 (2×2)
    ① 랙 없이 · 부하 0     기준
    ② 랙 없이 · 부하 강     부하만의 영향
    ③ 랙 싣고 · 부하 0     랙만의 영향
    ④ 랙 싣고 · 부하 강     둘 다
  ②만 늘면 부하, ③만 늘면 랙(rack.specs 의심), ④가 제일 심하면 둘 다.

⚠ 연구실에서만. 부하를 걸면 로봇이 느려지거나 예상 못 한 반응을 할 수 있다.
  주변에 사람·장애물이 없는 상태에서, 비상정지를 손에 두고 할 것.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from collections import deque

try:
    import requests
    import websocket
except ImportError:
    print("필요한 패키지가 없습니다:  pip install requests websocket-client")
    sys.exit(1)

# ── 감시용(가벼움). 이건 항상 켜둔다 ──────────────────────────────
WATCH_TOPICS = ["/motion_metrics", "/tracked_pose", "/alerts",
                "/fused_sensor_state", "/jack_state"]

# ── 부하용. 단계가 오를수록 더한다 ───────────────────────────────
#   숫자는 "이 토픽을 구독하는 WebSocket 연결 수". 연결을 늘리면
#   로봇이 같은 데이터를 그만큼 더 만들어 보내야 한다.
LOAD_LEVELS = {
    0: [],
    1: [("/scan_matched_points2", 1)],
    2: [("/scan_matched_points2", 2), ("/maps/1cm/1hz", 1)],
    3: [("/scan_matched_points2", 3), ("/maps/1cm/1hz", 2), ("/maps/5cm/1hz", 2)],
}
LEVEL_NAME = {0: "끔", 1: "약", 2: "중", 3: "강"}

# 3단계에서만 추가로 도는 REST 폴링 (초당 요청 수)
REST_HZ_AT_L3 = 10

STOP_V = 0.10          # 이 아래면 '섰다'
MIN_STOP_SEC = 0.3     # 이보다 짧으면 셈에서 뺀다 (측정 잡음)
BASE_WINDOW = 60       # 통상 속도를 정할 때 보는 최근 샘플 수
LOAD_RE = re.compile(r"Load average in 1 minute is ([0-9.]+)")


class Shared:
    """스레드끼리 나눠 보는 현재 상태."""
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.v = 0.0            # 실제 속도
        self.sug = None         # 로봇이 정한 속도
        self.cap = None         # 서버가 건 상한
        self.jack = None        # 잭 상태 (적재 여부)
        self.loads = deque(maxlen=200)     # (t, 부하값)
        self.recent_v = deque(maxlen=BASE_WINDOW)
        self.stops = []         # 감지된 멈춤 [(시작, 끝, 부하단계, 적재)]
        self.marks = []         # 사람이 Space 로 표시한 시각
        self.level = 0
        self.loaded = None
        self.run_start = time.time()
        self.stop_flag = threading.Event()


S = Shared()
REC: list[dict] = []           # 파일로 남길 것


def rec(kind: str, **kw) -> None:
    kw.update(kind=kind, t=time.time())
    REC.append(kw)


# ── 감시 연결 ───────────────────────────────────────────────────
def watcher(ip: str) -> None:
    """가벼운 토픽만 구독해 현재 상태를 갱신한다. 끊기면 다시 붙는다."""
    cur_stop = None
    while not S.stop_flag.is_set():
        ws = None
        try:
            ws = websocket.create_connection(f"ws://{ip}:8090/ws/v2/topics", timeout=10)
            for t in WATCH_TOPICS:
                ws.send(json.dumps({"enable_topic": t}))
                time.sleep(0.04)
            ws.settimeout(2.0)
            while not S.stop_flag.is_set():
                try:
                    m = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                topic = m.get("topic", "")
                now = time.time()

                if topic == "/motion_metrics":
                    v = float(m.get("velocity") or m.get("v") or 0.0)
                    with S.lock:
                        S.v = v
                        S.recent_v.append(v)
                        base = sorted(S.recent_v)[int(len(S.recent_v) * .75)] \
                            if len(S.recent_v) >= 20 else None
                        # 멈춤 감지 — 통상 속도가 있고, 그 절반 아래로 떨어지면
                        if base and base > 0.15:
                            if v < STOP_V:
                                if cur_stop is None:
                                    cur_stop = now
                            else:
                                if cur_stop is not None:
                                    dur = now - cur_stop
                                    if dur >= MIN_STOP_SEC:
                                        S.stops.append((cur_stop, now, S.level, S.loaded))
                                        rec("stop", dur=round(dur, 2),
                                            level=S.level, loaded=S.loaded)
                                    cur_stop = None

                elif topic == "/fused_sensor_state":
                    with S.lock:
                        S.sug = m.get("suggested_speed")

                elif topic == "/jack_state":
                    p = m.get("progress")
                    with S.lock:
                        S.loaded = (p is not None and float(p) > 0.5)
                        S.jack = p

                elif topic == "/alerts":
                    raw = json.dumps(m, ensure_ascii=False)
                    for g in LOAD_RE.findall(raw):
                        with S.lock:
                            S.loads.append((now, float(g)))
                        rec("load_alert", value=float(g), level=S.level, loaded=S.loaded)
        except Exception as e:
            rec("watch_error", msg=str(e)[:120])
            time.sleep(2.0)
        finally:
            try:
                if ws:
                    ws.close()
            except Exception:
                pass


# ── 부하 연결 ───────────────────────────────────────────────────
class LoadConn(threading.Thread):
    """무거운 토픽 하나를 구독만 하는 연결. 받은 건 버린다."""
    def __init__(self, ip: str, topic: str) -> None:
        super().__init__(daemon=True)
        self.ip, self.topic = ip, topic
        self.alive = threading.Event()
        self.alive.set()
        self.n = 0

    def run(self) -> None:
        while self.alive.is_set():
            ws = None
            try:
                ws = websocket.create_connection(
                    f"ws://{self.ip}:8090/ws/v2/topics", timeout=10)
                ws.send(json.dumps({"enable_topic": self.topic}))
                ws.settimeout(1.0)
                while self.alive.is_set():
                    try:
                        ws.recv()          # 받기만 하고 버린다
                        self.n += 1
                    except websocket.WebSocketTimeoutException:
                        continue
            except Exception:
                time.sleep(1.0)
            finally:
                try:
                    if ws:
                        ws.close()
                except Exception:
                    pass

    def stop(self) -> None:
        self.alive.clear()


class RestLoad(threading.Thread):
    """3단계에서만 도는 REST 폴링. 조회만 한다."""
    def __init__(self, ip: str, hz: int) -> None:
        super().__init__(daemon=True)
        self.ip, self.hz = ip, hz
        self.alive = threading.Event()
        self.alive.set()
        self.n = 0

    def run(self) -> None:
        url = f"http://{self.ip}:8090/chassis/status"
        gap = 1.0 / max(1, self.hz)
        while self.alive.is_set():
            try:
                requests.get(url, timeout=2)
                self.n += 1
            except Exception:
                pass
            time.sleep(gap)

    def stop(self) -> None:
        self.alive.clear()


_conns: list = []


def set_level(ip: str, lv: int) -> None:
    """부하 단계를 바꾼다. 기존 연결을 전부 끊고 새로 연다."""
    global _conns
    for c in _conns:
        c.stop()
    _conns = []
    for topic, count in LOAD_LEVELS.get(lv, []):
        for _ in range(count):
            c = LoadConn(ip, topic)
            c.start()
            _conns.append(c)
            time.sleep(0.05)
    if lv >= 3:
        c = RestLoad(ip, REST_HZ_AT_L3)
        c.start()
        _conns.append(c)
    with S.lock:
        S.level = lv
    rec("level", level=lv, conns=len(_conns))


# ── 화면 ────────────────────────────────────────────────────────
def fmt_load() -> str:
    now = time.time()
    with S.lock:
        recent = [(t, v) for t, v in S.loads if now - t <= 60]
        last = S.loads[-1] if S.loads else None
    if not last:
        return "경고 없음"
    age = now - last[0]
    tag = "⚠" if last[1] < 10 else "🔴"
    fresh = f"{tag} {last[1]:.1f}" if age < 30 else f"  {last[1]:.1f} ({age:.0f}초 전)"
    return f"{fresh}   최근 1분 {len(recent)}회"


def screen() -> None:
    while not S.stop_flag.is_set():
        now = time.time()
        with S.lock:
            v, sug, lv, ld = S.v, S.sug, S.level, S.loaded
            stops = list(S.stops)
            marks = len(S.marks)
            el = now - S.run_start
        cur = [s for s in stops if s[2] == lv]
        tot = sum(s[1] - s[0] for s in cur)
        os.system("cls" if os.name == "nt" else "clear")
        print("=" * 58)
        print(" 로봇 부하 테스트   (읽기 전용 — 명령을 보내지 않습니다)")
        print("=" * 58)
        print(f" 경과       {el/60:5.1f}분")
        print(f" CPU 부하   {fmt_load()}")
        print(f" 속도       {v:4.2f} m/s      로봇 제안 {sug if sug is not None else '-'}")
        print(f" 랙         {'적재 중' if ld else ('공차' if ld is not None else '-')}")
        print("-" * 58)
        print(f" 부하 단계  [{lv}] {LEVEL_NAME[lv]}     연결 {len(_conns)}개")
        print(f" 일시 멈춤  이 단계에서 {len(cur)}회 · 총 {tot:.0f}초")
        print(f" 표시       {marks}회")
        print("-" * 58)
        print(" [0] 끄기  [1] 약  [2] 중  [3] 강")
        print(" [Space] 지금 섰음 표시     [q] 종료")
        print("=" * 58)
        # 단계별 요약
        print(" 단계별 누적")
        for L in (0, 1, 2, 3):
            ss = [s for s in stops if s[2] == L]
            if not ss:
                continue
            print(f"   [{L}] {LEVEL_NAME[L]:<3} 멈춤 {len(ss):3d}회 · "
                  f"총 {sum(s[1]-s[0] for s in ss):5.0f}초")
        time.sleep(0.5)


# ── 키 입력 ─────────────────────────────────────────────────────
def keys(ip: str) -> None:
    if os.name == "nt":
        import msvcrt
        while not S.stop_flag.is_set():
            if not msvcrt.kbhit():
                time.sleep(0.05)
                continue
            ch = msvcrt.getch()
            handle(ch, ip)
    else:
        import termios, tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not S.stop_flag.is_set():
                ch = sys.stdin.read(1).encode()
                handle(ch, ip)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def handle(ch: bytes, ip: str) -> None:
    if ch in (b"0", b"1", b"2", b"3"):
        set_level(ip, int(ch))
    elif ch == b" ":
        with S.lock:
            S.marks.append(time.time())
        rec("mark", level=S.level, loaded=S.loaded)
    elif ch in (b"q", b"Q", b"\x1b"):
        S.stop_flag.set()


# ── 저장 ────────────────────────────────────────────────────────
def save(outdir: str) -> None:
    os.makedirs(outdir, exist_ok=True)
    p = os.path.join(outdir, "events.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        for r in REC:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 사람이 읽는 요약
    lines = ["# 부하 테스트 결과", ""]
    by = {}
    for s in S.stops:
        k = (s[2], bool(s[3]))
        d = by.setdefault(k, {"n": 0, "sec": 0.0})
        d["n"] += 1
        d["sec"] += s[1] - s[0]
    la = {}
    for r in REC:
        if r["kind"] == "load_alert":
            k = (r.get("level"), bool(r.get("loaded")))
            la[k] = la.get(k, 0) + 1
    lines.append("| 부하단계 | 랙 | 멈춤 횟수 | 멈춤 총시간 | 부하경고 |")
    lines.append("|---|---|---:|---:|---:|")
    for k in sorted(set(list(by.keys()) + list(la.keys()))):
        lv, ld = k
        d = by.get(k, {"n": 0, "sec": 0.0})
        lines.append(f"| [{lv}] {LEVEL_NAME.get(lv,lv)} | "
                     f"{'적재' if ld else '공차'} | {d['n']} | "
                     f"{d['sec']:.0f}초 | {la.get(k,0)} |")
    lines += ["", f"사람이 표시한 멈춤 : {len(S.marks)}회", ""]
    with open(os.path.join(outdir, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n저장: {outdir}")
    print("\n".join(lines))


def main() -> None:
    ap = argparse.ArgumentParser(description="로봇 CPU 부하 ↔ 일시적 멈춤 확인 (읽기 전용)")
    ap.add_argument("--ip", required=True, help="로봇 IP")
    ap.add_argument("--out", default=None, help="기록 폴더")
    a = ap.parse_args()

    out = a.out or os.path.join("_logs", "loadtest_" + time.strftime("%Y%m%d_%H%M%S"))

    print(f"로봇 {a.ip} 에 연결합니다 (읽기 전용)…")
    try:
        r = requests.get(f"http://{a.ip}:8090/chassis/status", timeout=5)
        r.raise_for_status()
    except Exception as e:
        print(f"연결 실패: {e}")
        print("IP 를 확인하세요. 로봇과 같은 망에 있어야 합니다.")
        sys.exit(1)

    threading.Thread(target=watcher, args=(a.ip,), daemon=True).start()
    threading.Thread(target=screen, daemon=True).start()
    try:
        keys(a.ip)
    except KeyboardInterrupt:
        pass
    finally:
        S.stop_flag.set()
        for c in _conns:
            c.stop()
        time.sleep(0.5)
        os.system("cls" if os.name == "nt" else "clear")
        save(out)


if __name__ == "__main__":
    main()
