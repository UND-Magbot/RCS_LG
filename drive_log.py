#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""주행 1회 = 폴더 1개. 멈칫거림(stutter) 원인 규명용 다중 스트림 기록.

왜 이게 필요한가
  2026-09-16 측정에서 감속 102건 중 99건이 "로봇 자체" 였는데, **왜** 로봇이
  스스로 줄였는지는 알 수가 없었다. 장애물 거리·통신 지연·위치추정 상태를
  아무것도 안 남겼기 때문이다. 이 스크립트는 그 공백을 메운다.

설계 원칙 3가지
  1) 단일 시계 - 모든 줄에 같은 기준 t(epoch)를 박는다. 나중에 t 로 join 해서
     "그 순간 앞에 뭐가 있었고 / 서버가 건드렸고 / 통신은 어땠나" 를 한 줄로 붙인다.
  2) 평소엔 요약, 이벤트 근처엔 원본 - 점군을 다 남기면 10분에 수백 MB 다.
     평소엔 최근접 거리만, 감속 순간 앞뒤 ±N초만 원본 점군을 뜬다.
  3) 즉시 flush - 9/16 에 6회를 날린 이유가 "메모리에 모았다가 끝에 한 번에 쓰기"
     였다. 여기서는 모든 스트림이 append + flush 다. 언제 꺼져도 그때까지 남는다.

주행 1회의 기준
  **충전소 출발 → 충전소 복귀** 를 1회로 본다.
  시간(--sec)으로 끊으면 한 파일에 여러 작업이 섞이고 경계가 어디인지
  알 수 없다. 충전소 기준으로 끊으면 한 폴더가 "왕복 1회" 라는 의미를
  갖고, 사이클끼리 비교가 된다. 기준점은 충전소 POI 를 자동으로 찾고,
  없으면 --home x,y 로 직접 준다.

만들어지는 것  (사이클마다 하위 폴더가 하나씩 생긴다)
  _logs/run_YYYYmmdd_HHMMSS/
    c01_HHMMSS/          ← 1번째 왕복
      meta.json          실행 정보 + 로봇 파라미터 전량(읽기만 함)
      motion.jsonl       10Hz  속도·가속도·위치·방향·상한·move/stuck/slam
      obstacle.jsonl    ~1.9Hz 전방/좌/우 최근접 장애물 + 밴드 내 점 개수
      comm.jsonl          1Hz  WS 수신간격·스캔 지연·REST 왕복시간·끊김
      events.jsonl      이벤트  감속 구간 + 그 순간의 장애물·통신 상태
      snapshots/        이벤트 ±N초 원본 점군
      backend_slice.log  이 사이클 시간대의 backend.log
      safety_slice.log   이 사이클 시간대의 [safety] 줄만
      summary.md         사람이 읽는 요약
    c02_HHMMSS/          ← 2번째 왕복 ...

사용
  python scripts/drive_log.py --ip 192.168.30.110
  python scripts/drive_log.py --ip 192.168.30.110 --cycles 3
  python scripts/drive_log.py --ip 192.168.30.110 --home 1.2,-3.4 --tag 시험
  Ctrl+C 로 끊어도 그때까지 전부 남고 요약이 만들어진다.

※ 로봇 설정을 바꾸는 호출은 이 파일에 없다. 전부 읽기(GET)만 한다.
"""

import argparse
import base64
import io
import json
import math
import os
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque

# 콘솔 인코딩 안전망 - 한국어 윈도우 콘솔(cp949)은 일부 기호를 못 쓴다.
# 그대로 두면 print 한 줄 때문에 측정 전체가 죽는다(2026-09-16 실측).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(_ROOT, "_logs")
BACKEND_LOG = os.path.join(_ROOT, "BackEnd", "_logs", "backend.log")

# ── 서버(safety_zone.py)와 같은 기준을 써야 측정값이 서버 판정과 대조된다 ──
ROBOT_FRONT_M = 0.379      # 로봇 단독 풋프린트 앞끝 (/robot_model 실측)
RACK_FRONT_M = 0.475       # 랙 실었을 때 앞쪽 돌출
SELF_CLEARANCE_M = 0.25    # 이 안쪽 점은 자기 몸으로 본다
BAND_HALF_W = 0.30         # 정면 밴드 반폭
SCAN_FAR_M = 6.0           # 이 밖은 안 본다
SIDE_FWD_M = 1.0           # 좌/우 최근접을 볼 전방 범위

RISE_END = 0.02            # 이만큼 다시 오르면 하락 구간 종료


# ==================================================================
#  최소 WebSocket 클라이언트 (stutter_watch.py 에서 검증된 것)
# ==================================================================
class _WS(object):
    def __init__(self, url, on_message=None, on_open=None, on_error=None):
        self.url, self.on_message = url, on_message
        self.on_open, self.on_error = on_open, on_error
        self.sock, self._alive, self._buf = None, True, b""

    def connect(self, timeout=10):
        u = urllib.parse.urlparse(self.url)
        host = u.hostname
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        s = socket.create_connection((host, port), timeout=timeout)
        if u.scheme == "wss":
            s = ssl._create_unverified_context().wrap_socket(s, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        s.send(("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
                % (path, host, port, key)).encode())
        hdr = b""
        while b"\r\n\r\n" not in hdr:
            c = s.recv(1)
            if not c:
                raise ConnectionError("handshake closed")
            hdr += c
        if b"101" not in hdr.split(b"\r\n")[0]:
            raise ConnectionError("handshake failed: %s" % hdr.split(b"\r\n")[0])
        s.settimeout(None)
        self.sock = s

    def send(self, data, opcode=0x1):
        if isinstance(data, str):
            data = data.encode()
        hdr = bytearray([0x80 | opcode])
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        hdr += mask
        hdr += bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.send(bytes(hdr))

    def _read(self, n):
        while len(self._buf) < n:
            c = self.sock.recv(65536)
            if not c:
                raise ConnectionError("closed")
            self._buf += c
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _frame(self):
        b0, b1 = self._read(2)
        fin, opcode = b0 & 0x80, b0 & 0x0F
        masked, ln = b1 & 0x80, b1 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._read(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._read(8))[0]
        mask = self._read(4) if masked else None
        payload = self._read(ln) if ln else b""
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def run_forever(self):
        try:
            if self.sock is None:
                self.connect()
            if self.on_open:
                self.on_open(self)
            frag, fragop = b"", None
            while self._alive:
                fin, op, payload = self._frame()
                if op == 0x8:
                    break
                if op == 0x9:
                    self.send(payload, opcode=0xA)
                    continue
                if op == 0xA:
                    continue
                if op == 0x0:
                    frag += payload
                else:
                    frag, fragop = payload, op
                if not fin:
                    continue
                data, frag = frag, b""
                if fragop == 0x1 and self.on_message:
                    try:
                        self.on_message(self, data.decode("utf-8"))
                    except UnicodeDecodeError:
                        pass
        except Exception as e:
            if self.on_error:
                self.on_error(self, e)
        finally:
            self._alive = False
            try:
                self.sock.close()
            except Exception:
                pass

    def close(self):
        self._alive = False
        try:
            self.sock.close()
        except Exception:
            pass


# ==================================================================
#  공유 상태
# ==================================================================
lock = threading.Lock()
stop_flag = threading.Event()

S = {
    "pose": None,       # (x, y, ori)   - 1 Hz
    "pose_t": 0.0,
    "v": None, "acc": None, "motion_t": 0.0,
    "cap": None,
    "move": None, "stuck": None,
    "slam": None,
    "obs": None,        # 최근 장애물 요약 dict
    "scan_t": 0.0,
    "scan_lag": None,
}

scans = deque(maxlen=60)    # (t, points) - 스냅샷용 링버퍼
ws_gaps = deque(maxlen=400)  # WS 메시지 수신 간격
counters = {"ws_msg": 0, "scan": 0, "reconnect": 0, "rest_fail": 0, "ws_err": 0}
rest_rtt = deque(maxlen=60)
events_n = [0]
snaps_n = [0]

OUT = {}    # 파일 핸들 모음


def w(name, obj):
    """한 줄 쓰고 즉시 flush. 어떤 이유로 꺼져도 여기까지는 남는다."""
    f = OUT.get(name)
    if f is None:
        return
    try:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()
    except Exception:
        pass


def _get_json(url, timeout=3.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ==================================================================
#  장애물 - 점군을 요약한다 (원본은 이벤트 근처에서만 뜬다)
# ==================================================================
def _xy(q):
    if isinstance(q, dict):
        return q.get("x"), q.get("y")
    if isinstance(q, (list, tuple)) and len(q) >= 2:
        return q[0], q[1]
    return None, None


def summarize_scan(points, pose, front_ext):
    """정면 밴드 최근접 + 좌/우 최근접 + 밴드 내 점 개수.

    front_ext = 자기 앞끝까지의 거리. 로봇 중심 기준 거리에서 이걸 빼야
    '앞끝에서 장애물까지' 가 된다 - 서버 safety_zone 과 같은 기준이다.
    """
    if not pose or not points:
        return None
    px, py, ori = pose
    c, s = math.cos(ori), math.sin(ori)
    near_min = max(SELF_CLEARANCE_M, front_ext - 0.05)

    front = None          # (앞끝기준거리, 좌우, x, y)
    n_band = 0
    left = right = None
    n_all = 0

    for q in points:
        qx, qy = _xy(q)
        if qx is None or qy is None:
            continue
        n_all += 1
        dx, dy = qx - px, qy - py
        fwd = dx * c + dy * s
        if fwd > SCAN_FAR_M or fwd < -0.5:
            continue
        lat = -dx * s + dy * c
        if fwd >= near_min and abs(lat) <= BAND_HALF_W:
            n_band += 1
            d = fwd - front_ext
            if front is None or d < front[0]:
                front = (round(d, 3), round(lat, 3), round(qx, 3), round(qy, 3))
        if 0.0 <= fwd <= SIDE_FWD_M and abs(lat) <= 1.2:
            if lat > 0 and (left is None or lat < left):
                left = round(lat, 3)
            elif lat < 0 and (right is None or -lat < -right):
                right = round(lat, 3)

    return {
        "front_m": front[0] if front else None,
        "front_lat": front[1] if front else None,
        "front_x": front[2] if front else None,
        "front_y": front[3] if front else None,
        "n_band": n_band,
        "left_m": left, "right_m": right,
        "n_points": n_all,
    }


# ==================================================================
#  WS 수신
# ==================================================================
ARGS = None


def on_open(ws):
    for t in ("/motion_metrics", "/tracked_pose", "/planning_state",
              "/slam/state", "/scan_matched_points2"):
        ws.send(json.dumps({"enable_topic": t}))
        time.sleep(0.05)
    print("구독 완료 (5개 토픽) - 기록 시작\n", flush=True)


def on_msg(ws, raw_msg):
    now = time.time()
    with lock:
        if counters["ws_msg"]:
            ws_gaps.append(now - S.get("_last_msg_t", now))
        S["_last_msg_t"] = now
        counters["ws_msg"] += 1
    try:
        m = json.loads(raw_msg)
    except Exception:
        return
    tp = m.get("topic")

    if tp == "/motion_metrics":
        with lock:
            S["v"] = m.get("linear_velocity")
            S["acc"] = m.get("linear_acc")
            S["motion_t"] = now

    elif tp == "/tracked_pose":
        p = m.get("pos")
        if p:
            with lock:
                S["pose"] = (float(p[0]), float(p[1]), float(m.get("ori", 0.0)))
                S["pose_t"] = now

    elif tp == "/planning_state":
        with lock:
            S["move"] = m.get("move_state")
            S["stuck"] = m.get("stuck_state")

    elif tp == "/slam/state":
        with lock:
            S["slam"] = m.get("state") or m.get("reliable") or m

    elif tp == "/scan_matched_points2":
        pts = m.get("points") or m.get("data") or []
        stamp = m.get("stamp")
        lag = (now - float(stamp)) if stamp else None
        with lock:
            pose = S["pose"]
            front_ext = ARGS.front
            counters["scan"] += 1
            S["scan_t"] = now
            S["scan_lag"] = round(lag, 3) if lag is not None else None
            scans.append((now, pts))
        obs = summarize_scan(pts, pose, front_ext)
        if obs is not None:
            obs["t"] = now
            obs["scan_lag"] = S["scan_lag"]
            obs["x"] = round(pose[0], 3)
            obs["y"] = round(pose[1], 3)
            obs["ori"] = round(pose[2], 4)
            with lock:
                S["obs"] = obs
            w("obstacle", obs)


# ==================================================================
#  REST 폴링 - 속도 상한 + 왕복시간(통신 품질)
#  ※ 읽기(GET)만 한다. 로봇 설정을 바꾸는 호출은 이 파일에 없다.
# ==================================================================
def poll_params(ip):
    last_cap = None
    while not stop_flag.is_set():
        t0 = time.time()
        try:
            d = _get_json("http://%s:8090/robot-params" % ip, 3)
            rtt = (time.time() - t0) * 1000.0
            cap = d.get("/wheel_control/max_forward_velocity")
            with lock:
                rest_rtt.append(rtt)
                if cap is not None:
                    S["cap"] = cap
            if (cap is not None and last_cap is not None
                    and abs(cap - last_cap) > 0.005):
                with lock:
                    pose = S["pose"]
                    obs = dict(S["obs"]) if S["obs"] else None
                w("events", {
                    "t": time.time(), "kind": "cap_change",
                    "frm": last_cap, "to": cap,
                    "x": round(pose[0], 3) if pose else None,
                    "y": round(pose[1], 3) if pose else None,
                    "front_m": (obs or {}).get("front_m"),
                })
            if cap is not None:
                last_cap = cap
        except Exception:
            with lock:
                counters["rest_fail"] += 1
        stop_flag.wait(1.0)


def comm_writer():
    """1 Hz 로 통신 품질을 남긴다.

    멈칫거림이 판정 지연 때문인지 가리는 근거다. 데이터가 늦게 오면
    서버도 로봇도 늦게 반응한다.
    """
    last = {"ws": 0, "scan": 0}
    while not stop_flag.is_set():
        stop_flag.wait(1.0)
        now = time.time()
        with lock:
            gaps = list(ws_gaps)
            rtts = list(rest_rtt)
            d_ws = counters["ws_msg"] - last["ws"]
            d_scan = counters["scan"] - last["scan"]
            last["ws"], last["scan"] = counters["ws_msg"], counters["scan"]
            scan_age = now - S["scan_t"] if S["scan_t"] else None
            pose_age = now - S["pose_t"] if S["pose_t"] else None
            rest_fail = counters["rest_fail"]
            ws_err = counters["ws_err"]
        w("comm", {
            "t": now,
            "ws_hz": d_ws, "scan_hz": d_scan,
            "ws_gap_max": round(max(gaps), 3) if gaps else None,
            "ws_gap_avg": round(sum(gaps) / len(gaps), 4) if gaps else None,
            "scan_age": round(scan_age, 3) if scan_age is not None else None,
            "pose_age": round(pose_age, 3) if pose_age is not None else None,
            "rest_rtt_ms": round(sum(rtts) / len(rtts), 1) if rtts else None,
            "rest_rtt_max_ms": round(max(rtts), 1) if rtts else None,
            "rest_fail": rest_fail, "ws_err": ws_err,
        })


# ==================================================================
#  스냅샷 - 이벤트 앞뒤 window 초의 원본 점군
# ==================================================================
pending_snaps = []


def dump_snapshot(idx, t_center, window, outdir):
    with lock:
        sel = [(t, p) for (t, p) in scans if abs(t - t_center) <= window]
    if not sel or not outdir:
        return
    path = os.path.join(outdir, "evt_%03d_t%.0f.json" % (idx, t_center))
    try:
        out = []
        for (t, p) in sel:
            pts = []
            for q in p:
                a, b = _xy(q)
                if a is not None and b is not None:
                    pts.append([round(float(a), 3), round(float(b), 3)])
            out.append({"t": t, "n": len(pts), "points": pts})
        with io.open(path, "w", encoding="utf-8") as f:
            json.dump({"event_index": idx, "t_center": t_center,
                       "window": window, "scans": out}, f, ensure_ascii=False)
        snaps_n[0] += 1
    except Exception as e:
        print("  스냅샷 실패: %s" % e, flush=True)


# ==================================================================
#  사이클 = 충전소 출발 → 충전소 복귀
#
#  왜 이 기준인가 - 시간(--sec)으로 끊으면 한 파일 안에 여러 작업이 섞이고
#  작업 경계가 어디인지 알 수 없다. 충전소 기준으로 끊으면 한 폴더가
#  "왕복 1회" 라는 의미를 갖고, 사이클끼리 비교가 된다.
# ==================================================================
CY = {"active": False, "idx": 0, "dir": None, "t0": 0.0, "far": False}


def open_cycle(a, base, idx, home):
    outdir = os.path.join(base, "c%02d_%s" % (idx, time.strftime("%H%M%S")))
    os.makedirs(os.path.join(outdir, "snapshots"), exist_ok=True)
    for key, fn in (("motion", "motion.jsonl"), ("obstacle", "obstacle.jsonl"),
                    ("comm", "comm.jsonl"), ("events", "events.jsonl")):
        OUT[key] = io.open(os.path.join(outdir, fn), "w", encoding="utf-8")
    events_n[0] = 0
    snaps_n[0] = 0
    del pending_snaps[:]
    CY.update({"active": True, "idx": idx, "dir": outdir, "t0": time.time()})
    with io.open(os.path.join(outdir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({
            "cycle": idx, "started_at": CY["t0"],
            "started_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "robot_ip": a.ip, "home": home,
            "leave_r": a.leave_r, "arrive_r": a.arrive_r,
            "front_extent_m": a.front, "band_half_w": BAND_HALF_W,
            "self_clearance": SELF_CLEARANCE_M, "scan_far_m": SCAN_FAR_M,
            "robot_params": META.get("robot_params"),
            "poi_count": len(META.get("pois") or []),
        }, f, ensure_ascii=False, indent=2)
    print("\n▶ 사이클 %02d 시작 - %s" % (idx, os.path.basename(outdir)), flush=True)
    return outdir


def close_cycle(a, reason):
    if not CY["active"]:
        return
    outdir = CY["dir"]
    for it in list(pending_snaps):
        dump_snapshot(it[0], it[1], a.snap_window, os.path.join(outdir, "snapshots"))
    del pending_snaps[:]
    for f in list(OUT.values()):
        try:
            f.close()
        except Exception:
            pass
    OUT.clear()
    t1 = time.time()
    evs = []
    try:
        with io.open(os.path.join(outdir, "events.jsonl"), "r",
                     encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    evs.append(json.loads(line))
    except Exception:
        pass
    n_bk, n_sf = slice_backend_log(outdir, CY["t0"], t1)
    write_summary(outdir, a, CY["t0"], t1, evs, n_bk, n_sf, CY["idx"], reason)
    dec = len([e for e in evs if e.get("kind") == "decel"])
    print("■ 사이클 %02d 종료(%s) - %.0f초 · 감속 %d건 · 스냅샷 %d"
          % (CY["idx"], reason, t1 - CY["t0"], dec, snaps_n[0]), flush=True)
    CY.update({"active": False, "dir": None})


def cycle_watch(a, base, home):
    """충전소 반경을 드나드는 것을 보고 사이클을 열고 닫는다."""
    hx, hy = home
    n = 0
    was_far = None
    while not stop_flag.is_set():
        stop_flag.wait(0.3)
        with lock:
            pose = S["pose"]
        if pose is None:
            continue
        d = math.hypot(pose[0] - hx, pose[1] - hy)
        far = d > a.leave_r
        near = d < a.arrive_r
        if was_far is None:
            was_far = far
            if far:
                print("  (지금 충전소 밖 %.2fm - 복귀부터 기다립니다)" % d, flush=True)
            continue
        if not CY["active"]:
            if far and not was_far:
                n += 1
                open_cycle(a, base, n, {"x": hx, "y": hy})
        else:
            if near:
                close_cycle(a, "충전소 복귀")
                if a.cycles and CY["idx"] >= a.cycles:
                    print("  요청한 사이클 수(%d) 도달 - 종료합니다" % a.cycles,
                          flush=True)
                    stop_flag.set()
                    return
            elif time.time() - CY["t0"] > a.max_cycle_sec:
                close_cycle(a, "시간 초과(복귀 못 함)")
        was_far = far


# ==================================================================
#  10 Hz 샘플러 + 감속 구간 판정
# ==================================================================
def _who(cap_start, cap_end):
    if (cap_start is not None and cap_end is not None
            and abs(cap_start - cap_end) > 0.005):
        return "서버(상한 %.2f→%.2f)" % (cap_start, cap_end)
    if cap_end is not None:
        return "로봇 자체(상한 %.2f)" % cap_end
    return "로봇 자체(상한 미상)"


def _new_seg(now, v, cap, obs):
    return {"t_start": now, "t_end": now, "v_peak": v, "v_min": v,
            "peak_acc": 0.0, "cap_start": cap, "obs_start": obs}


def nearest_poi(pois, x, y):
    best, bd = None, 1e9
    for p in pois or []:
        px, py = p.get("world_x"), p.get("world_y")
        if px is None or py is None:
            continue
        d = math.hypot(px - x, py - y)
        if d < bd:
            best, bd = p, d
    if best is None:
        return None, None
    return "%s(%.1fm)" % (best.get("name") or "?", bd), best.get("poi_type")


def sampler(a):
    cur = None
    pois = META.get("pois") or []
    while not stop_flag.is_set():
        now = time.time()
        with lock:
            v, acc = S["v"], S["acc"]
            pose, cap = S["pose"], S["cap"]
            move, stuck, slam = S["move"], S["stuck"], S["slam"]
            obs = dict(S["obs"]) if S["obs"] else None
            scan_lag = S["scan_lag"]
            gaps = list(ws_gaps)[-10:]

        if CY["active"]:
            w("motion", {
                "t": now, "v": v, "acc": acc,
                "x": round(pose[0], 3) if pose else None,
                "y": round(pose[1], 3) if pose else None,
                "ori": round(pose[2], 4) if pose else None,
                "cap": cap, "move": move, "stuck": stuck, "slam": slam,
                "front_m": (obs or {}).get("front_m"),
                "n_band": (obs or {}).get("n_band"),
                "scan_lag": scan_lag,
                "ws_gap": round(max(gaps), 3) if gaps else None,
            })

        if v is not None and CY["active"]:
            if cur is None:
                cur = _new_seg(now, v, cap, obs)
            elif v > cur["v_min"] + RISE_END or (now - cur["t_start"]) > 20:
                drop = cur["v_peak"] - cur["v_min"]
                if drop > 0.0:
                    events_n[0] += 1
                    idx = events_n[0]
                    dur = max(1e-3, cur["t_end"] - cur["t_start"])
                    poi_name, poi_type = (nearest_poi(pois, pose[0], pose[1])
                                          if pose else (None, None))
                    ev = {
                        "t": cur["t_end"], "kind": "decel", "index": idx,
                        "cycle": CY["idx"], "t_start": cur["t_start"],
                        "v_from": round(cur["v_peak"], 3),
                        "v_to": round(cur["v_min"], 3),
                        "drop": round(drop, 3), "dur": round(dur, 3),
                        "decel_avg": round(drop / dur, 3),
                        "decel_peak": round(abs(cur["peak_acc"]), 3),
                        "stop": cur["v_min"] <= 0.02,
                        "cap_start": cur["cap_start"], "cap_end": cap,
                        "who": _who(cur["cap_start"], cap),
                        "x": round(pose[0], 3) if pose else None,
                        "y": round(pose[1], 3) if pose else None,
                        "poi": poi_name, "poi_type": poi_type,
                        "front_m_start": (cur["obs_start"] or {}).get("front_m"),
                        "front_m_end": (obs or {}).get("front_m"),
                        "n_band_end": (obs or {}).get("n_band"),
                        "move": move, "stuck": stuck, "slam": slam,
                        "scan_lag": scan_lag,
                        "ws_gap_max": round(max(gaps), 3) if gaps else None,
                    }
                    w("events", ev)
                    if drop >= a.snap_min_drop and snaps_n[0] < a.max_snaps:
                        pending_snaps.append((idx, ev["t"], now + a.snap_window))
                    if drop >= a.show:
                        fm = ev["front_m_end"]
                        print("  %s  %.2f→%.2f  낙폭 %.2f  피크 %.2f  앞 %s  %s  %s"
                              % (time.strftime("%H:%M:%S"), ev["v_from"],
                                 ev["v_to"], ev["drop"], ev["decel_peak"],
                                 ("%.2fm" % fm) if fm is not None else "-",
                                 ev["who"], poi_name or ""), flush=True)
                cur = _new_seg(now, v, cap, obs)
            else:
                if v < cur["v_min"]:
                    cur["v_min"] = v
                    cur["t_end"] = now
                if acc is not None and acc < cur["peak_acc"]:
                    cur["peak_acc"] = acc
        elif not CY["active"]:
            cur = None

        if CY["active"]:
            snapdir = os.path.join(CY["dir"], "snapshots")
            for it in list(pending_snaps):
                if now >= it[2]:
                    pending_snaps.remove(it)
                    dump_snapshot(it[0], it[1], a.snap_window, snapdir)

        time.sleep(0.1)


# ==================================================================
#  백엔드 로그 잘라 담기 + 요약
# ==================================================================
def slice_backend_log(outdir, t0, t1):
    """backend.log 에서 이 사이클 시간대만 잘라 온다.

    형식: "09-10 22:47:50  INFO  uvicorn.access  ..."  (연도가 없다)
    연도는 측정 시작 시점의 연도로 본다.
    """
    if not os.path.exists(BACKEND_LOG):
        return 0, 0
    year = time.localtime(t0).tm_year
    n_all = n_safety = 0
    try:
        fa = io.open(os.path.join(outdir, "backend_slice.log"), "w",
                     encoding="utf-8")
        fs = io.open(os.path.join(outdir, "safety_slice.log"), "w",
                     encoding="utf-8")
        with io.open(BACKEND_LOG, "r", encoding="utf-8", errors="replace") as src:
            for line in src:
                head = line[:14]
                try:
                    st = time.mktime(time.strptime(
                        "%d-%s" % (year, head), "%Y-%m-%d %H:%M:%S"))
                except Exception:
                    continue
                if t0 - 2 <= st <= t1 + 2:
                    fa.write(line)
                    n_all += 1
                    if "[safety]" in line:
                        fs.write(line)
                        n_safety += 1
        fa.close()
        fs.close()
    except Exception as e:
        print("  백엔드 로그 추출 실패(무시): %s" % e, flush=True)
    return n_all, n_safety


def write_summary(outdir, a, t0, t1, evs, n_bk, n_sf, cyc, reason):
    decels = [e for e in evs if e.get("kind") == "decel"]
    caps = [e for e in evs if e.get("kind") == "cap_change"]
    L = []
    L.append("# 사이클 %02d - %s" % (cyc, os.path.basename(outdir)))
    L.append("")
    L.append("주행 1회 = **충전소 출발 → 충전소 복귀**. 종료 사유: %s" % reason)
    L.append("")
    L.append("- 로봇 `%s` · %s ~ %s (**%.0f초**)"
             % (a.ip, time.strftime("%H:%M:%S", time.localtime(t0)),
                time.strftime("%H:%M:%S", time.localtime(t1)), t1 - t0))
    L.append("- 감속 이벤트 **%d건** · 속도상한 변경 **%d회** · 스냅샷 %d개"
             % (len(decels), len(caps), snaps_n[0]))
    L.append("- 백엔드 로그 %d줄 (그중 `[safety]` %d줄)" % (n_bk, n_sf))
    L.append("")
    if not decels:
        L.append("> 감속 이벤트가 없습니다. 주행을 안 했거나 데이터가 안 왔습니다.")
    else:
        srv = [e for e in decels if str(e.get("who", "")).startswith("서버")]
        stops = [e for e in decels if e.get("stop")]
        withobs = [e for e in decels if e.get("front_m_end") is not None]
        near = [e for e in withobs if e["front_m_end"] <= 1.0]
        L.append("## 원인 분해")
        L.append("")
        L.append("| 구분 | 건수 |")
        L.append("|---|---:|")
        L.append("| 서버가 상한을 내림 | %d |" % len(srv))
        L.append("| 로봇 자체 감속 | %d |" % (len(decels) - len(srv)))
        L.append("| 완전 정지 | %d |" % len(stops))
        L.append("| 앞 1.0m 안에 장애물이 실제로 있었음 | %d |" % len(near))
        L.append("| 장애물 정보 없음(스캔 미수신) | %d |"
                 % (len(decels) - len(withobs)))
        L.append("")
        ds = [e["drop"] for e in decels]
        ps = [e["decel_peak"] for e in decels]
        L.append("- 속도강하 최대 %.2f / 평균 %.3f m/s" % (max(ds), sum(ds) / len(ds)))
        L.append("- 감속도 피크 최대 %.2f / 평균 %.2f m/s^2"
                 % (max(ps), sum(ps) / len(ps)))
        L.append("")
        L.append("## 낙폭 큰 순 상위 15건")
        L.append("")
        L.append("| 시각 | 속도 | 낙폭 | 피크 | 앞거리 | 밴드점 | 범인 | 위치 |")
        L.append("|---|---|---:|---:|---:|---:|---|---|")
        for e in sorted(decels, key=lambda x: -x["drop"])[:15]:
            fm = e.get("front_m_end")
            L.append("| %s | %.2f→%.2f | %.2f | %.2f | %s | %s | %s | %s |"
                     % (time.strftime("%H:%M:%S", time.localtime(e["t"])),
                        e["v_from"], e["v_to"], e["drop"], e["decel_peak"],
                        ("%.2f m" % fm) if fm is not None else "-",
                        e.get("n_band_end", "-"), e.get("who", ""),
                        e.get("poi") or "-"))
    L.append("")
    L.append("## 읽는 법")
    L.append("")
    L.append("모든 파일의 `t` 는 같은 기준(epoch)이다. `t` 로 맞춰 보면")
    L.append("`motion.jsonl`(속도) · `obstacle.jsonl`(앞에 뭐가 있었나) ·")
    L.append("`comm.jsonl`(데이터가 제때 왔나) · `safety_slice.log`(서버가 뭘 했나)")
    L.append("를 한 시점으로 붙일 수 있다. 그게 원인 판별의 근거다.")
    with io.open(os.path.join(outdir, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


# ==================================================================
META = {}


def find_home(a, pois):
    """기준점(충전소) 정하기 - --home > 충전소 POI > 지금 로봇 위치."""
    if a.home:
        try:
            x, y = [float(s) for s in a.home.split(",")]
            return (x, y), "--home 인자"
        except Exception:
            print("  --home 형식이 잘못됐습니다 (예: --home 1.2,-3.4)")
    ch = [p for p in (pois or [])
          if str(p.get("poi_type") or "").lower() == "charging"
          and p.get("world_x") is not None]
    if ch:
        with lock:
            pose = S["pose"]
        if pose:
            ch.sort(key=lambda p: math.hypot(p["world_x"] - pose[0],
                                             p["world_y"] - pose[1]))
        p = ch[0]
        return (float(p["world_x"]), float(p["world_y"])), \
               "충전소 POI '%s'" % (p.get("name") or "?")
    with lock:
        pose = S["pose"]
    if pose:
        return (pose[0], pose[1]), "지금 로봇 위치(충전소 POI 없음)"
    return None, None


def probe(a):
    """본 측정 전에 30초만 돌려서 전제가 맞는지 확인한다.

    현장에서 10분을 돌리고 나서 "필드 이름이 달랐다" 를 알게 되면 하루가 날아간다.
    그래서 먼저 이걸 돌린다. 아무것도 안 기록하고 화면으로만 보고한다.
    """
    seen = {}
    raw_first = {}

    def _on_msg(_w, msg):
        try:
            m = json.loads(msg)
        except Exception:
            return
        tp = m.get("topic")
        if tp and tp not in seen:
            seen[tp] = 0
            raw_first[tp] = m
        if tp:
            seen[tp] += 1

    def _on_open(ws):
        for t in ("/motion_metrics", "/tracked_pose", "/planning_state",
                  "/slam/state", "/scan_matched_points2"):
            ws.send(json.dumps({"enable_topic": t}))
            time.sleep(0.05)

    print("사전 점검 30초 - 아무것도 기록하지 않습니다.")
    print("")
    ws = _WS("ws://%s:8090/ws/v2/topics" % a.ip, _on_msg, _on_open,
             lambda _w, e: print("  WS 오류: %s" % str(e)[:110], flush=True))
    threading.Thread(target=ws.run_forever, daemon=True).start()
    t0 = time.time()
    try:
        while time.time() - t0 < 30:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    try:
        ws.close()
    except Exception:
        pass

    # 토픽별로 "없으면 무엇이 비는가" 를 미리 정해 둔다.
    # 치명(fatal)이면 측정 자체가 무의미하고, 부분(partial)이면
    # 그 항목만 비고 나머지는 정상으로 기록된다.
    NEED = [
        ("/motion_metrics",       "fatal",
         "속도·가속도. 없으면 감속 이벤트를 아예 못 잡는다"),
        ("/tracked_pose",         "fatal",
         "로봇 위치. 없으면 사이클 감지도 장애물 계산도 안 된다"),
        ("/scan_matched_points2", "partial",
         "장애물. 없으면 obstacle.jsonl 과 스냅샷이 빈다"),
        ("/planning_state",       "partial",
         "move/stuck 상태만 빈다"),
        ("/slam/state",           "partial",
         "위치추정 상태만 빈다"),
    ]
    fatal, partial = [], []
    print("=" * 72)
    for t, sev, why in NEED:
        n = seen.get(t, 0)
        if n:
            mark = "OK  "
        else:
            mark = "없음"
            (fatal if sev == "fatal" else partial).append((t, why))
        print("  [%s] %-26s %4d건 (%.1f Hz)" % (mark, t, n, n / 30.0))
    print("")

    sc = raw_first.get("/scan_matched_points2")
    if sc:
        keys = sorted(k for k in sc.keys() if k != "topic")
        print("  스캔 메시지 필드: %s" % ", ".join(keys))
        pts = sc.get("points") or sc.get("data")
        if pts is None:
            partial.append(("/scan_matched_points2 필드명",
                            "points/data 가 아니다. 위 필드 목록을 알려주세요"))
            print("  ★ points/data 둘 다 없습니다 - 장애물 기록이 빕니다.")
        elif not pts:
            print("  (스캔은 오는데 점이 0개입니다 - 주변이 트여 있으면 정상)")
        else:
            x, y = _xy(pts[0])
            if x is None:
                partial.append(("점 좌표 형식",
                                "첫 점 예시: %s - 이걸 알려주세요" % (pts[0],)))
                print("  ★ 점 형식을 못 읽습니다. 예시: %s" % (pts[0],))
            else:
                print("  점 %d개, 좌표 파싱 OK (x=%.3f, y=%.3f)" % (len(pts), x, y))

    tp = raw_first.get("/tracked_pose")
    if tp:
        print("  위치 메시지 필드: %s"
              % ", ".join(sorted(k for k in tp.keys() if k != "topic")))
        if "ori" not in tp:
            partial.append(("tracked_pose 의 ori",
                            "방향을 몰라 정면 장애물 거리를 못 믿는다"))
            print("  ★ ori(방향) 가 없습니다 - 장애물 거리가 틀어집니다.")

    home_ok = True
    if a.server:
        try:
            pois = _get_json(a.server.rstrip("/") + "/api/map/active-pois", 5)
            ch = [p for p in pois
                  if str(p.get("poi_type") or "").lower() == "charging"]
            print("  POI %d개 / 충전소 %d개" % (len(pois), len(ch)))
            if not ch:
                home_ok = False
                print("  ★ 충전소 POI 가 없습니다.")
        except Exception as e:
            home_ok = False
            partial.append(("POI 조회", str(e)[:80]))
            print("  ★ POI 조회 실패: %s" % str(e)[:80])
    else:
        home_ok = False

    print("")
    print("-" * 72)
    if fatal:
        print("  판정: 진행 불가")
        print("")
        for t, why in fatal:
            print("    X  %s" % t)
            print("       %s" % why)
        print("")
        print("  로봇에 제대로 못 붙었습니다. IP·네트워크부터 확인하세요.")
    elif partial:
        print("  판정: 측정은 되지만 일부가 빕니다")
        print("")
        for t, why in partial:
            print("    !  %s" % t)
            print("       %s" % why)
        print("")
        print("  속도·위치·통신은 정상 기록됩니다. 시간이 급하면 이대로")
        print("  측정하고, 위 항목은 저에게 알려주시면 맞춰 고치겠습니다.")
    else:
        print("  판정: 전부 정상 - 본 측정을 진행하세요")
    if not home_ok:
        print("")
        print("  ※ 충전소 기준점을 자동으로 못 잡습니다.")
        print("     --home x,y 로 직접 주시거나, 로봇을 충전소에 둔 상태로")
        print("     시작하면 그 자리를 기준점으로 씁니다.")
    print("=" * 72)
    return 0 if not fatal else 1


def _drop_if_empty(base):
    """사이클이 하나도 안 생겼으면 빈 상위 폴더를 지운다 (_logs 가 지저분해지는 것 방지)."""
    try:
        if os.path.isdir(base) and not os.listdir(base):
            os.rmdir(base)
            return True
    except Exception:
        pass
    return False


def resolve_ip(a):
    """--ip auto 면 백엔드에서 로봇 목록을 받아 IP 를 고른다.

    여러 대면 첫 번째를 쓰고 경고한다 - 로봇별로 따로 돌리려면
    start_drive_log.ps1 이 대수만큼 프로세스를 띄운다.
    """
    if a.ip.lower() != "auto":
        return a.ip
    if not a.server:
        print("★ --ip auto 는 --server 가 있어야 합니다.")
        return None
    try:
        d = _get_json(a.server.rstrip("/") + "/api/robots?limit=50", 5)
    except Exception as e:
        print("★ 로봇 목록 조회 실패: %s" % str(e)[:100])
        return None
    items = d.get("items") if isinstance(d, dict) else d
    ips = [(r.get("name"), r.get("ip_address")) for r in (items or [])
           if r.get("ip_address")]
    if not ips:
        print("★ IP 가 등록된 로봇이 없습니다. --ip 로 직접 지정하세요.")
        return None
    if len(ips) > 1:
        print("  로봇 %d대 발견: %s" % (len(ips), ", ".join(
            "%s(%s)" % (n or "?", i) for n, i in ips)))
        print("  → 첫 번째(%s)로 진행합니다. 다른 로봇은 --ip 로 지정하세요." % ips[0][1])
    return ips[0][1]


def main():
    global ARGS
    ap = argparse.ArgumentParser(
        description="주행 1회(충전소 출발~복귀)를 폴더 하나로 기록한다")
    ap.add_argument("--ip", required=True,
                    help="로봇 IP. auto 로 주면 백엔드에서 찾아온다")
    ap.add_argument("--sec", type=float, default=0.0,
                    help="전체 실행 시간(초). 0 이면 Ctrl+C 까지 계속")
    ap.add_argument("--cycles", type=int, default=0,
                    help="이 횟수만큼 왕복하면 종료. 0 이면 무제한")
    ap.add_argument("--server", default="http://127.0.0.1:8002",
                    help="백엔드 주소. 안 쓰려면 --server \"\"")
    ap.add_argument("--home", default="",
                    help="기준점 좌표 x,y (안 주면 충전소 POI 를 자동으로 찾는다)")
    ap.add_argument("--leave-r", type=float, default=1.5,
                    help="이만큼 벗어나면 출발로 본다(m)")
    ap.add_argument("--arrive-r", type=float, default=1.2,
                    help="이 안으로 들어오면 복귀로 본다(m)")
    ap.add_argument("--max-cycle-sec", type=float, default=1800.0,
                    help="복귀를 못 해도 이 시간이면 사이클을 닫는다")
    ap.add_argument("--front", type=float, default=ROBOT_FRONT_M,
                    help="자기 앞끝까지 거리. 랙을 실었으면 %.3f" % RACK_FRONT_M)
    ap.add_argument("--show", type=float, default=0.10,
                    help="화면에 띄울 최소 낙폭 m/s (파일엔 언제나 전부 남는다)")
    ap.add_argument("--snap-window", type=float, default=2.0,
                    help="이벤트 앞뒤 몇 초의 원본 점군을 뜰지")
    ap.add_argument("--snap-min-drop", type=float, default=0.15,
                    help="이 낙폭 이상일 때만 스냅샷(용량 관리)")
    ap.add_argument("--max-snaps", type=int, default=40,
                    help="사이클당 스냅샷 최대 개수(용량 관리)")
    ap.add_argument("--tag", default="", help="폴더 이름 뒤에 붙일 메모")
    ap.add_argument("--probe", action="store_true",
                    help="30초 사전 점검 - 토픽별 첫 메시지를 보고 바로 끝낸다")
    a = ap.parse_args()
    ARGS = a

    ip = resolve_ip(a)
    if not ip:
        return 1
    a.ip = ip

    if a.probe:
        return probe(a)

    t_start = time.time()
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(t_start))
    base = os.path.join(LOG_DIR, "run_%s%s"
                        % (stamp, ("_" + a.tag) if a.tag else ""))
    os.makedirs(base, exist_ok=True)

    print("=" * 72)
    print("[drive_log] 로봇 %s" % a.ip)
    print("주행 1회 = 충전소 출발 → 충전소 복귀. 사이클마다 폴더가 하나씩 생깁니다.")
    print("상위 폴더: %s" % base)
    print("=" * 72)

    # 로봇 파라미터 - 읽기만 한다(기준선 기록용). 변경하지 않는다.
    try:
        META["robot_params"] = _get_json("http://%s:8090/robot-params" % a.ip, 5)
        print("로봇 파라미터 %d개 기록" % len(META["robot_params"]))
    except Exception as e:
        META["robot_params"] = None
        print("로봇 파라미터 조회 실패(계속 진행): %s" % str(e)[:90])

    META["pois"] = []
    if a.server:
        try:
            META["pois"] = _get_json(
                a.server.rstrip("/") + "/api/map/active-pois", 5)
            ch = [p for p in META["pois"]
                  if str(p.get("poi_type") or "").lower() == "charging"]
            print("POI %d개 (충전소 %d개)" % (len(META["pois"]), len(ch)))
        except Exception as e:
            print("POI 조회 실패: %s" % str(e)[:90])
            print("  → 백엔드가 떠 있는지 확인하세요. 측정은 그대로 진행됩니다.")

    def on_err(_w, e):
        with lock:
            counters["ws_err"] += 1
        print("  WS 오류: %s" % str(e)[:110], flush=True)

    ws = _WS("ws://%s:8090/ws/v2/topics" % a.ip, on_msg, on_open, on_err)
    threading.Thread(target=ws.run_forever, daemon=True).start()
    threading.Thread(target=poll_params, args=(a.ip,), daemon=True).start()
    threading.Thread(target=comm_writer, daemon=True).start()

    print("로봇 위치 수신 대기...", flush=True)
    for _ in range(100):
        with lock:
            if S["pose"] is not None:
                break
        time.sleep(0.2)
    with lock:
        got_pose = S["pose"] is not None
    if not got_pose:
        print("★ 20초 동안 로봇 위치를 못 받았습니다. IP·네트워크를 확인하세요.")
        print("  그래도 계속 기다립니다 (Ctrl+C 로 중단).")

    home, how = find_home(a, META["pois"])
    if home is None:
        print("★ 기준점을 정할 수 없습니다. --home x,y 로 직접 주세요.")
        _drop_if_empty(base)
        return 1
    print("기준점(충전소): (%.2f, %.2f)  ← %s" % (home[0], home[1], how))
    print("출발 판정 %.1fm 밖 / 복귀 판정 %.1fm 안\n" % (a.leave_r, a.arrive_r))

    threading.Thread(target=sampler, args=(a,), daemon=True).start()
    threading.Thread(target=cycle_watch, args=(a, base, home), daemon=True).start()

    try:
        while not stop_flag.is_set():
            if a.sec and time.time() - t_start >= a.sec:
                break
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\n(사용자 중단)")
    stop_flag.set()
    time.sleep(0.6)
    if CY["active"]:
        close_cycle(a, "측정 종료")
    try:
        ws.close()
    except Exception:
        pass

    with lock:
        c = dict(counters)
    print()
    print("=" * 72)
    print("완료. WS 메시지 %d · 스캔 %d · REST 실패 %d · WS 오류 %d"
          % (c["ws_msg"], c["scan"], c["rest_fail"], c["ws_err"]))
    if _drop_if_empty(base):
        print("사이클이 하나도 없어 빈 폴더를 지웠습니다.")
    else:
        print("상위 폴더: %s" % base)
    if c["ws_msg"] == 0:
        print()
        print("★ WS 메시지가 0건입니다 - 로봇에 못 붙었습니다.")
        print("  IP·네트워크를 확인하세요. 폴더는 만들어졌지만 내용이 비어 있습니다.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
