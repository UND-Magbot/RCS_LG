# -*- coding: utf-8 -*-
"""주행 중 속도가 떨어진 **모든 구간**을 시각과 함께 기록한다.

왜 이렇게 만들었나
  처음에는 "감속 0.8 m/s² 이상" 같은 기준을 두고 걸렀는데, 그러면 기준 아래 것은
  아예 안 남아서 나중에 "그때 그건 뭐였지" 를 확인할 수가 없다.
  그래서 **거르지 않는다.** 속도가 떨어지는 구간은 크든 작든 전부 잡아 파일에 남기고,
  화면에는 보기 좋을 만큼만 추려 띄운다. 판단 기준은 나중에 바꿔도 된다.

한 '구간' 의 정의
  속도가 계속 떨어지는 동안을 한 구간으로 본다. 다시 오르거나 평평해지면 끊는다.
  구간마다 이렇게 남는다.
      언제(시각·경과초) / 어디서(좌표·가장 가까운 지점)
      얼마나(0.80 -> 0.50, 낙폭 0.30)
      얼마나 빨리(1.8초, 평균 0.17 m/s², 피크 0.31 m/s²)
      누가(서버 안전존인가 로봇 자체인가)

'누가' 를 어떻게 가리나
  0.3초마다 로봇의 속도 상한(max_forward_velocity)을 읽는다.
      상한이 내려갔다  -> 서버 안전존이 눌렀다 (backend.log 에 [safety] 줄이 있다)
      상한은 그대로다  -> 로봇이 스스로 줄였다 (서버 로그에는 아무것도 없다)

쓰는 법 (설치할 것 없음 — 표준 라이브러리만으로 돈다)
  BackEnd\\venv\\Scripts\\python.exe scripts/stutter_watch.py --ip 192.168.39.100
  Ctrl+C 로 끝내면 요약이 나온다. 기본 10분(--sec 600) 뒤 자동 종료.

  --show 0        화면에도 전부 띄운다 (기본 0.05 = 낙폭 5cm/s 이상만 띄움)
  --prefix W      경유지 이름 앞글자. W* 사이 구간만 [경유] 로 묶어 따로 집계한다
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys
import threading
import time
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "BackEnd"))

import base64                      # noqa: E402
import socket                      # noqa: E402
import ssl                         # noqa: E402
import struct                      # noqa: E402
import urllib.request              # noqa: E402
from urllib.parse import urlparse  # noqa: E402


def _get_json(url, timeout=5):
    """requests 대신 표준 라이브러리로. 현장 파이썬에 requests 가 없어도 돌아야 한다."""
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ── WebSocket 클라이언트 ───────────────────────────────────────────
#   현장 서버 노트북은 가상환경을 안 쓰고 시스템 파이썬으로 돌린다.
#   websocket-client 가 없을 수 있으므로 **표준 라이브러리만으로** 붙는 구현을 같이 둔다.
#   (2026-09-16 실제 WebSocket 서버와 대조 시험 통과 — 짧은/373B/70KB 메시지, ping, 송신)
class LiteWS:
    def __init__(self, url, on_message=None, on_open=None, on_error=None):
        self.url = url
        self.on_message = on_message
        self.on_open = on_open
        self.on_error = on_error
        self.sock = None
        self._alive = False
        self._sendlock = threading.Lock()
        self._buf = b""

    # ── 핸드셰이크 ──────────────────────────────────────────────
    def connect(self, timeout=10):
        u = urlparse(self.url)
        host = u.hostname
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query

        s = socket.create_connection((host, port), timeout=timeout)
        if u.scheme == "wss":
            s = ssl.create_default_context().wrap_socket(s, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n" % (path, host, port, key)
        )
        s.sendall(req.encode())

        # 응답 헤더를 \r\n\r\n 까지 읽는다
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = s.recv(1)
            if not chunk:
                raise ConnectionError("핸드셰이크 중 연결이 끊겼습니다")
            head += chunk
        first = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in first:
            raise ConnectionError("업그레이드 거부: %s" % first)

        s.settimeout(1.0)
        self.sock = s
        self._alive = True
        return self

    # ── 프레임 쓰기 (클라이언트는 반드시 마스킹) ─────────────────
    def send(self, text, opcode=0x1):
        if not self._alive:
            return
        data = text.encode("utf-8") if isinstance(text, str) else text
        n = len(data)
        hdr = bytearray([0x80 | opcode])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < (1 << 16):
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        with self._sendlock:
            try:
                self.sock.sendall(bytes(hdr) + masked)
            except Exception:
                self._alive = False

    # ── 프레임 읽기 ────────────────────────────────────────────
    def _read(self, n):
        while len(self._buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                if not self._alive:
                    raise ConnectionError("closed")
                continue
            if not chunk:
                raise ConnectionError("closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _frame(self):
        b0, b1 = self._read(2)
        fin = b0 & 0x80
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        ln = b1 & 0x7F
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
                if op == 0x8:                       # close
                    break
                if op == 0x9:                       # ping -> pong
                    self.send(payload, opcode=0xA)
                    continue
                if op == 0xA:
                    continue
                if op == 0x0:                       # 이어지는 조각
                    frag += payload
                else:
                    frag, fragop = payload, op
                if not fin:
                    continue
                data, frag = frag, b""
                if fragop == 0x1:
                    try:
                        msg = data.decode("utf-8")
                    except Exception:
                        continue
                    if self.on_message:
                        self.on_message(self, msg)
                elif self.on_message:
                    self.on_message(self, data)     # 바이너리는 그대로
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


try:                                   # 패키지가 있으면 그것을 쓴다
    from websocket import WebSocketApp as _WSApp

    def make_ws(url, on_message, on_open, on_error=None):
        return _WSApp(url, on_message=on_message, on_open=on_open,
                      on_error=on_error)
except ImportError:                    # 없으면 위의 내장 구현으로
    def make_ws(url, on_message, on_open, on_error=None):
        return LiteWS(url, on_message=on_message, on_open=on_open,
                      on_error=on_error)


LOG_DIR = os.path.join(_ROOT, "_logs")

# ── 구간을 끊는 조건 (판정 기준이 아니라 '구간 나누기' 기준이다) ──
RISE_END = 0.02     # 이만큼 다시 오르면 하락 구간이 끝난 것으로 본다 (m/s)
FLAT_END = 0.6      # 이 시간 동안 안 떨어지면 끝난 것으로 본다 (초)
NOISE = 0.015       # 이보다 작은 오르내림은 센서 떨림으로 본다 (m/s)
STILL = 0.05        # 이 밑이면 '멈춤' 으로 표시 (m/s)

S = {"pose": None, "cap": None, "move": None, "stuck": None}
raw = []            # 10 Hz 원본
segs = []           # 찾아낸 하락 구간 전부
caps = []           # 속도 상한 변화 이력
lock = threading.Lock()
T0 = 0.0
POIS = []
stop_flag = threading.Event()


def hhmmss(t):
    return datetime.fromtimestamp(t).strftime("%H:%M:%S.") + "%d" % int((t % 1) * 10)


def load_pois(server, area):
    if not server:
        return []
    url = server.rstrip("/") + "/api/maps/active-pois"
    if area:
        url += "?area_id=%s" % area
    try:
        out = []
        for p in _get_json(url, 5):
            x, y = p.get("world_x"), p.get("world_y")
            if x is None or y is None:
                continue
            out.append({"name": str(p.get("name") or "?"), "x": float(x), "y": float(y)})
        return out
    except Exception as e:
        print("  (POI 목록을 못 받았습니다 — 좌표로만 기록합니다)")
        print("   %s" % str(e)[:110])
        return []


def zone_of(pose, prefix):
    if not POIS or pose is None:
        return "[?]", ""
    best, bd = None, 1e9
    for p in POIS:
        d = math.hypot(p["x"] - pose[0], p["y"] - pose[1])
        if d < bd:
            best, bd = p, d
    tag = "[경유]" if best["name"].upper().startswith(prefix.upper()) else "[작업]"
    return tag, "%s(%.1fm)" % (best["name"], bd)


def on_msg(ws, msg):
    if isinstance(msg, (bytes, bytearray)):
        return
    try:
        m = json.loads(msg)
    except Exception:
        return
    tp = m.get("topic")
    now = time.time()
    if tp == "/motion_metrics":
        with lock:
            raw.append({"t": now, "v": m.get("linear_velocity"),
                        "acc": m.get("linear_acc"),
                        "x": S["pose"][0] if S["pose"] else None,
                        "y": S["pose"][1] if S["pose"] else None,
                        "cap": S["cap"], "move": S["move"]})
    elif tp == "/tracked_pose":
        with lock:
            S["pose"] = (m["pos"][0], m["pos"][1])
    elif tp == "/planning_state":
        with lock:
            S["move"] = m.get("move_state")
            S["stuck"] = m.get("stuck_state")


def on_open(ws):
    for t in ("/motion_metrics", "/tracked_pose", "/planning_state"):
        ws.send(json.dumps({"enable_topic": t}))
        time.sleep(0.05)
    print("구독 완료 — 기록 시작\n", flush=True)


def poll_cap(ip):
    """속도 상한. 감속의 '범인' 을 가리는 열쇠다."""
    last = None
    while not stop_flag.is_set():
        try:
            v = _get_json("http://%s:8090/robot-params" % ip, 3).get(
                "/wheel_control/max_forward_velocity")
            if v is not None:
                with lock:
                    S["cap"] = v
                if last is not None and abs(v - last) > 0.005:
                    with lock:
                        pose = S["pose"]
                    caps.append({"t": time.time(), "frm": last, "to": v,
                                 "x": pose[0] if pose else None,
                                 "y": pose[1] if pose else None})
                last = v
        except Exception:
            pass
        stop_flag.wait(0.3)


def who_of(seg):
    """상한이 내려갔으면 서버, 그대로면 로봇 자체."""
    c0, c1 = seg.get("cap_start"), seg.get("cap_end")
    if c1 is None:
        return "?"
    if c0 is not None and c1 < c0 - 0.005:
        return "서버(상한 %.2f→%.2f)" % (c0, c1)
    if c1 <= seg["v_from"] - 0.05:
        return "서버(상한 %.2f)" % c1
    return "로봇 자체(상한 %.2f)" % c1


def tracker(prefix, show, fout):
    """하락 구간을 **거르지 않고** 전부 찾는다."""
    idx = 0
    cur = None          # 진행 중인 하락 구간
    low_t = 0.0
    while not stop_flag.is_set():
        stop_flag.wait(0.15)
        with lock:
            batch = raw[idx:]
            idx = len(raw)
        for r in batch:
            v, t = r.get("v"), r["t"]
            if v is None:
                continue
            if cur is None:
                cur = {"t0": t, "v_from": v, "v_min": v, "t_min": t,
                       "cap_start": r.get("cap"), "x": r.get("x"), "y": r.get("y"),
                       "acc_peak": 0.0, "falling": False}
                low_t = t
                continue

            a = r.get("acc")
            if a is not None and a < 0:
                cur["acc_peak"] = max(cur["acc_peak"], -a)

            if v < cur["v_min"] - NOISE:                 # 계속 떨어진다
                cur["v_min"], cur["t_min"] = v, t
                cur["falling"] = True
                cur["cap_end"] = r.get("cap")
                cur["x_end"], cur["y_end"] = r.get("x"), r.get("y")
                low_t = t
            elif v > cur["v_min"] + RISE_END or (t - low_t) > FLAT_END:
                # 구간 종료 — 떨어진 적이 있으면 남긴다
                if cur["falling"]:
                    seg = {
                        "t": cur["t_min"], "el": cur["t_min"] - T0,
                        "t_start": cur["t0"],
                        "v_from": cur["v_from"], "v_to": cur["v_min"],
                        "drop": cur["v_from"] - cur["v_min"],
                        "dur": max(0.1, cur["t_min"] - cur["t0"]),
                        "decel_peak": cur["acc_peak"],
                        "cap_start": cur["cap_start"], "cap_end": cur.get("cap_end"),
                        "x": cur.get("x_end") or cur.get("x"),
                        "y": cur.get("y_end") or cur.get("y"),
                    }
                    seg["decel_avg"] = seg["drop"] / seg["dur"]
                    seg["stop"] = seg["v_to"] < STILL
                    tag, nm = zone_of((seg["x"], seg["y"]) if seg["x"] is not None else None,
                                      prefix)
                    seg["zone"], seg["poi"] = tag, nm
                    seg["who"] = who_of(seg)
                    segs.append(seg)
                    fout.write(json.dumps(seg, ensure_ascii=False) + "\n")
                    fout.flush()
                    if seg["drop"] >= show:
                        print("  %s (+%6.1fs) %s %s %.2f→%.2f (-%.2f, %.1f초)  "
                              "평균 %.2f 피크 %.2f  %s  %s"
                              % (hhmmss(seg["t"]), seg["el"], tag,
                                 "멈춤" if seg["stop"] else "감속",
                                 seg["v_from"], seg["v_to"], seg["drop"], seg["dur"],
                                 seg["decel_avg"], seg["decel_peak"], seg["who"], nm),
                              flush=True)
                # 새 구간 시작
                cur = {"t0": t, "v_from": v, "v_min": v, "t_min": t,
                       "cap_start": r.get("cap"), "x": r.get("x"), "y": r.get("y"),
                       "acc_peak": 0.0, "falling": False}
                low_t = t


def main():
    global T0, POIS
    ap = argparse.ArgumentParser(description="속도가 떨어진 모든 구간 기록")
    ap.add_argument("--ip", required=True)
    ap.add_argument("--sec", type=float, default=600.0, help="감시 시간(초)")
    ap.add_argument("--server", default="http://127.0.0.1:8002",
                    help="백엔드 주소 (POI 이름용). 안 쓰려면 --server \"\"")
    ap.add_argument("--area", default=None)
    ap.add_argument("--prefix", default="W", help="경유지 이름 앞글자 (기본 W)")
    ap.add_argument("--show", type=float, default=0.05,
                    help="화면에 띄울 최소 낙폭 m/s (0 이면 전부. 파일엔 언제나 전부 남는다)")
    ap.add_argument("--top", type=int, default=25, help="요약에 크기순 몇 건까지")
    a = ap.parse_args()

    T0 = time.time()
    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    seg_path = os.path.join(LOG_DIR, "stutter_%s_구간.jsonl" % stamp)
    raw_path = os.path.join(LOG_DIR, "stutter_%s_raw.jsonl" % stamp)

    print("[stutter_watch] %s" % a.ip)
    print("시작 시각  %s   (경과초는 이 시점 기준)" % hhmmss(T0))
    print("속도가 떨어진 구간은 **크기와 상관없이 전부** 파일에 남습니다.")
    print("화면에는 낙폭 %.2f m/s 이상만 띄웁니다 (--show 0 이면 전부).\n" % a.show)
    POIS = load_pois(a.server, a.area)
    if POIS:
        w = sorted(p["name"] for p in POIS
                   if p["name"].upper().startswith(a.prefix.upper()))
        print("POI %d개 — 경유지(%s*) %d개: %s" % (len(POIS), a.prefix, len(w), ", ".join(w[:12])))
    print("Ctrl+C 로 끝내면 요약이 나옵니다.\n")

    fout = io.open(seg_path, "w", encoding="utf-8")
    ws = make_ws("ws://%s:8090/ws/v2/topics" % a.ip, on_msg, on_open,
                 lambda w, e: print("  WS 오류: %s" % e, flush=True))
    threading.Thread(target=ws.run_forever, daemon=True).start()
    threading.Thread(target=poll_cap, args=(a.ip,), daemon=True).start()
    threading.Thread(target=tracker, args=(a.prefix, a.show, fout), daemon=True).start()

    try:
        while time.time() - T0 < a.sec:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n(중단)")
    stop_flag.set()
    time.sleep(0.5)
    try:
        ws.close()
    except Exception:
        pass
    fout.close()

    with lock:
        allraw = list(raw)
    with io.open(raw_path, "w", encoding="utf-8") as f:
        for r in allraw:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print("\n" + "=" * 104)
    print("=== 요약 ===\n")
    vs = [r["v"] for r in allraw if r["v"] is not None]
    print("표본 %d개 (%.0f초) · 최고속도 %.2f m/s · 하락 구간 %d개 · 상한 변경 %d회"
          % (len(allraw), time.time() - T0, max(vs) if vs else 0, len(segs), len(caps)))
    if not segs:
        print("\n하락 구간이 없습니다. 주행을 안 했거나 데이터가 안 왔습니다.")
        print("원본  %s" % raw_path)
        return

    wp = [s for s in segs if s["zone"] == "[경유]"]
    print()
    hdr = ("  시각        경과      구간   종류  속도변화       낙폭  시간  "
           "평균감속 피크  범인")
    print(hdr)
    print("  " + "-" * 100)
    for s in sorted(segs, key=lambda x: -x["drop"])[:a.top]:
        print("  %s  +%6.1fs  %-6s %-4s  %.2f→%.2f  %5.2f %4.1fs  %6.2f %5.2f  %s %s"
              % (hhmmss(s["t"]), s["el"], s["zone"],
                 "멈춤" if s["stop"] else "감속",
                 s["v_from"], s["v_to"], s["drop"], s["dur"],
                 s["decel_avg"], s["decel_peak"], s["who"], s["poi"]))
    if len(segs) > a.top:
        print("  ... 외 %d건 (전부 파일에 있습니다)" % (len(segs) - a.top))

    print()
    print("낙폭 분포")
    for lo, hi, nm in ((0.5, 9, "0.50 이상"), (0.3, 0.5, "0.30~0.50"),
                       (0.15, 0.3, "0.15~0.30"), (0.05, 0.15, "0.05~0.15"),
                       (0, 0.05, "0.05 미만")):
        n = sum(1 for s in segs if lo <= s["drop"] < hi)
        nw = sum(1 for s in wp if lo <= s["drop"] < hi)
        print("   %-12s %3d건  (경유 %d건)" % (nm, n, nw))

    if wp:
        srv = sum(1 for s in wp if s["who"].startswith("서버"))
        print()
        print("경유 구간(%s*) %d건 — 서버 안전존 %d건 / 로봇 자체 %d건"
              % (a.prefix, len(wp), srv, len(wp) - srv))
        d = [s["decel_peak"] for s in wp]
        print("   감속 피크 최대 %.2f  평균 %.2f m/s²" % (max(d), sum(d) / len(d)))
        stops = [s for s in wp if s["stop"]]
        if stops:
            print("   ★ 완전히 멈춘 것 %d건:" % len(stops))
            for s in stops[:8]:
                print("      %s (+%.1fs) %s" % (hhmmss(s["t"]), s["el"], s["poi"]))

    if caps:
        print()
        print("속도 상한 변경 %d회 (서버가 건드린 순간)" % len(caps))
        for c in caps[:12]:
            print("   %s (+%6.1fs)  %.2f → %.2f"
                  % (hhmmss(c["t"]), c["t"] - T0, c["frm"], c["to"]))
        if len(caps) > 12:
            print("   ... 외 %d회" % (len(caps) - 12))

    print()
    print("  ※ '서버' 면 backend.log 의 같은 시각 [safety] 줄에 좌우 값이 있다.")
    print("    좌우가 0 근처면 정면 장애물, ±0.3 이상이면 옆에 있는 물체다.")
    print("  ※ '로봇 자체' 면 서버 로그에 아무 줄도 없다 — 로봇이 스스로 줄인 것이다.")
    print()
    print("구간  %s" % seg_path)
    print("원본  %s" % raw_path)


if __name__ == "__main__":
    main()
